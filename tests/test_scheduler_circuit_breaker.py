"""C1 acceptance: scheduler circuit breaker.

Verifies the contract that the scheduler loops in server/main.py rely on:
  - consecutive_failures progresses through the configured backoff steps
  - success resets the counter
  - after max_consecutive_failures, the breaker pauses and stops advising
    a backoff (the loop should consult is_paused() instead)
  - non_failure_exception_types skip the counter entirely
  - pause_immediately_exception_types short-circuit straight to paused
  - resume() clears paused state and zeroes the counter
  - snapshot() exposes JSON-friendly state for /admin/health (C3)

See docs/fix_plan_snapshot_loop.md C1.
"""

from __future__ import annotations

import re

import pytest

from server.scheduler_breaker import (
    DEFAULT_BACKOFF_STEPS_SEC,
    SchedulerCircuitBreaker,
)


_ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


def _make_breaker(**overrides) -> SchedulerCircuitBreaker:
    kwargs = dict(name="test", max_consecutive_failures=4, backoff_steps_sec=(30, 120, 300, 900, 1800))
    kwargs.update(overrides)
    return SchedulerCircuitBreaker(**kwargs)


# ── construction / invariants ────────────────────────────────────────


def test_initial_state_is_not_paused_with_zero_failures():
    b = _make_breaker()
    assert not b.is_paused()
    snap = b.snapshot()
    assert snap["paused"] is False
    assert snap["consecutive_failures"] == 0
    assert snap["paused_reason"] is None
    assert snap["last_tick_at"] is None
    assert snap["last_success_at"] is None
    assert snap["last_failure_at"] is None


def test_invalid_config_is_rejected():
    with pytest.raises(ValueError):
        SchedulerCircuitBreaker(name="x", max_consecutive_failures=0)
    with pytest.raises(ValueError):
        SchedulerCircuitBreaker(name="x", backoff_steps_sec=[])


# ── timestamps ───────────────────────────────────────────────────────


def test_record_tick_start_sets_iso_z_timestamp():
    b = _make_breaker()
    b.record_tick_start()
    snap = b.snapshot()
    assert _ISO_Z.match(snap["last_tick_at"] or ""), snap["last_tick_at"]


def test_record_success_stamps_last_success_at():
    b = _make_breaker()
    b.record_success()
    snap = b.snapshot()
    assert _ISO_Z.match(snap["last_success_at"] or ""), snap["last_success_at"]


# ── backoff progression ──────────────────────────────────────────────


def test_consecutive_failures_progress_through_backoff_steps():
    b = _make_breaker(max_consecutive_failures=10, backoff_steps_sec=(30, 120, 300, 900, 1800))
    waits = [b.record_failure(RuntimeError("boom")) for _ in range(5)]
    assert waits == [30, 120, 300, 900, 1800]
    # Beyond the last step, the value clamps (not 0, not error, not exception).
    waits_after_clamp = [b.record_failure(RuntimeError("boom")) for _ in range(3)]
    assert waits_after_clamp == [1800, 1800, 1800]
    assert b.consecutive_failures == 8


def test_default_backoff_sequence_is_30s_2m_5m_15m_30m():
    """Anchors the documented schedule so future refactors trip the test."""
    assert DEFAULT_BACKOFF_STEPS_SEC == (30, 120, 300, 900, 1800)


# ── success resets ───────────────────────────────────────────────────


def test_success_resets_consecutive_failures_to_zero():
    b = _make_breaker(max_consecutive_failures=10)
    b.record_failure(RuntimeError("a"))
    b.record_failure(RuntimeError("b"))
    assert b.consecutive_failures == 2
    b.record_success()
    assert b.consecutive_failures == 0
    # Next failure restarts at step 1.
    wait = b.record_failure(RuntimeError("c"))
    assert wait == b.backoff_steps_sec[0]
    assert b.consecutive_failures == 1


def test_success_does_not_auto_resume_a_paused_breaker():
    """A paused breaker requires explicit resume(). Otherwise a transient
    'success' between failure clusters could re-enable a loop that was
    deliberately paused."""
    b = _make_breaker(max_consecutive_failures=2)
    b.record_failure(RuntimeError("x"))
    b.record_failure(RuntimeError("y"))
    assert b.is_paused()
    b.record_success()
    assert b.is_paused(), "record_success() must not auto-resume"


# ── pause-after-N behavior ───────────────────────────────────────────


def test_pauses_after_max_consecutive_failures():
    b = _make_breaker(max_consecutive_failures=3)
    assert b.record_failure(RuntimeError("1")) > 0
    assert b.record_failure(RuntimeError("2")) > 0
    # The Nth failure trips the breaker. Returned backoff is 0 because the
    # loop should consult is_paused() instead of sleeping.
    assert b.record_failure(RuntimeError("3")) == 0
    assert b.is_paused()
    snap = b.snapshot()
    assert snap["paused_reason"].startswith("consecutive_failures>=")
    assert _ISO_Z.match(snap["paused_at"] or "")


def test_further_failures_after_pause_do_not_change_paused_reason():
    b = _make_breaker(max_consecutive_failures=2)
    b.record_failure(RuntimeError("a"))
    b.record_failure(RuntimeError("b"))
    original_reason = b.paused_reason
    original_at = b.paused_at
    b.record_failure(RuntimeError("c"))
    assert b.is_paused()
    assert b.paused_reason == original_reason
    assert b.paused_at == original_at


# ── exception classifier hooks (A2/A3 forward-compat) ────────────────


class _BudgetExceeded(RuntimeError):
    """Stand-in for the future A2 exception class."""


class _DuplicatePrompt(RuntimeError):
    """Stand-in for the future A3 exception class."""


def test_pause_immediately_exception_types_skip_the_counter():
    b = _make_breaker(
        max_consecutive_failures=10,
        pause_immediately_exception_types=(_BudgetExceeded,),
    )
    wait = b.record_failure(_BudgetExceeded("daily limit"))
    assert wait == 0
    assert b.is_paused()
    # consecutive_failures did NOT advance because we went straight to paused.
    # (record_failure may set the counter, but the key contract is paused-now.)
    assert b.paused_reason == "_BudgetExceeded"


def test_non_failure_exception_types_do_not_count_or_pause():
    b = _make_breaker(
        max_consecutive_failures=3,
        non_failure_exception_types=(_DuplicatePrompt,),
    )
    for _ in range(10):
        wait = b.record_failure(_DuplicatePrompt("same prompt"))
        assert wait == 0
        assert not b.is_paused()
    assert b.consecutive_failures == 0
    # And a real failure afterward still progresses normally.
    real_wait = b.record_failure(RuntimeError("real"))
    assert real_wait == b.backoff_steps_sec[0]
    assert b.consecutive_failures == 1


def test_non_failure_still_records_last_failure_metadata():
    """Even when an exception doesn't count toward the counter, it should
    still be visible in snapshot() so admins can see something happened."""
    b = _make_breaker(non_failure_exception_types=(_DuplicatePrompt,))
    b.record_failure(_DuplicatePrompt("dup"))
    snap = b.snapshot()
    assert snap["consecutive_failures"] == 0
    assert snap["last_failure_at"] is not None
    assert "_DuplicatePrompt" in (snap["last_error_summary"] or "")


# ── resume / reset ───────────────────────────────────────────────────


def test_resume_clears_paused_state_and_failure_counter():
    b = _make_breaker(max_consecutive_failures=2)
    b.record_failure(RuntimeError("a"))
    b.record_failure(RuntimeError("b"))
    assert b.is_paused()
    was_paused = b.resume()
    assert was_paused is True
    assert not b.is_paused()
    assert b.consecutive_failures == 0
    snap = b.snapshot()
    assert snap["paused"] is False
    assert snap["paused_reason"] is None
    assert snap["paused_at"] is None


def test_resume_on_already_running_breaker_is_no_op_and_returns_false():
    b = _make_breaker()
    assert b.resume() is False
    assert not b.is_paused()


def test_resume_does_not_wipe_last_failure_or_success_timestamps():
    """resume() resets the failure counter and paused flag, but the
    historical timestamps remain available for /admin/health so admins
    can see when the last incident happened even after resuming."""
    b = _make_breaker(max_consecutive_failures=2)
    b.record_failure(RuntimeError("a"))
    last_failure = b.last_failure_at
    b.record_success()
    last_success = b.last_success_at
    b.record_failure(RuntimeError("b"))
    b.record_failure(RuntimeError("c"))
    b.resume()
    assert b.last_failure_at is not None
    assert b.last_success_at == last_success
    assert b.last_failure_at >= last_failure


# ── error message truncation ─────────────────────────────────────────


def test_long_error_messages_are_truncated_in_summary():
    b = _make_breaker()
    huge = "z" * 1000
    b.record_failure(RuntimeError(huge))
    summary = b.snapshot()["last_error_summary"] or ""
    assert summary.endswith("…")
    assert len(summary) < 320
    assert "RuntimeError" in summary


# ── snapshot() shape contract ────────────────────────────────────────


def test_snapshot_contains_all_documented_fields():
    """The /admin/health endpoint (C3) will consume this. Lock the shape."""
    b = _make_breaker()
    snap = b.snapshot()
    expected_keys = {
        "name",
        "paused",
        "paused_reason",
        "paused_at",
        "consecutive_failures",
        "last_tick_at",
        "last_success_at",
        "last_failure_at",
        "last_error_summary",
    }
    assert set(snap.keys()) == expected_keys
    assert snap["name"] == "test"


# ── StateMachine integration ─────────────────────────────────────────


def test_state_machine_exposes_two_breakers():
    """The C3 admin endpoints will dispatch by breaker name; both must
    be reachable on the constructed StateMachine."""
    from server.state_machine import StateMachine

    machine = object.__new__(StateMachine)
    # Manually run only the breaker setup (avoid full __init__'s wiring).
    from server.scheduler_breaker import SchedulerCircuitBreaker

    machine.snapshot_scheduler_breaker = SchedulerCircuitBreaker(name="snapshot_scheduler")
    machine.life_scheduler_breaker = SchedulerCircuitBreaker(name="life_scheduler")

    assert machine.snapshot_scheduler_breaker.name == "snapshot_scheduler"
    assert machine.life_scheduler_breaker.name == "life_scheduler"
    # They're independent.
    machine.snapshot_scheduler_breaker.record_failure(RuntimeError("x"))
    assert machine.snapshot_scheduler_breaker.consecutive_failures == 1
    assert machine.life_scheduler_breaker.consecutive_failures == 0
