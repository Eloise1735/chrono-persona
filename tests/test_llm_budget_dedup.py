"""A2 + A3 acceptance: budget hard cap + prompt-hash dedup at LLMClient layer.

These are the last-line defenses against the kind of silent loop that hit
the second incident — a plan-generation LLM call that fired every 60s
because the inner `except Exception` swallowed every error. A2/A3 are
LLMClient-layer interceptors, so they protect ALL callers
(plan_engine, npc_engine, evolution, the merge helper, …) without
requiring per-call-site wraps.

Contracts under test:
  • A3 dedup: identical (model, messages, temperature) sent inside the
    cooldown window is refused before any HTTP request.
  • A3 dedup: window expires; same prompt is allowed again afterwards.
  • A3 dedup: failed upstream calls do NOT record the hash, so the
    caller can fix and retry immediately.
  • A2 budget: hourly/daily token windows reject the next call before
    any HTTP request when projected usage would exceed the limit.
  • A2 budget: BudgetExceeded inherits from BaseException, so
    `except Exception` blocks scattered around the codebase do NOT
    swallow it — it must propagate to the scheduler loop.
  • A2 budget: actual usage from the upstream replaces the estimate
    after a successful call.
  • C1 integration: DuplicatePromptError is a non_failure for the
    breaker; BudgetExceeded short-circuits to paused.

See docs/fix_plan_snapshot_loop.md A2 + A3.
"""

from __future__ import annotations

import time

import pytest

from server.llm_client import (
    BudgetExceeded,
    DuplicatePromptError,
    _BudgetTracker,
    _PromptDedupTracker,
    _estimate_prompt_tokens,
    _hash_messages,
    get_budget_tracker,
    get_prompt_dedup_tracker,
)
from server.scheduler_breaker import SchedulerCircuitBreaker


# ── _hash_messages: stability + sensitivity ──────────────────────────


def test_hash_is_stable_for_same_inputs():
    h1 = _hash_messages("m1", [{"role": "u", "content": "hi"}], 0.7)
    h2 = _hash_messages("m1", [{"role": "u", "content": "hi"}], 0.7)
    assert h1 == h2
    assert len(h1) == 64


def test_hash_changes_when_model_changes():
    a = _hash_messages("m1", [{"role": "u", "content": "x"}], 0.7)
    b = _hash_messages("m2", [{"role": "u", "content": "x"}], 0.7)
    assert a != b


def test_hash_changes_when_messages_change():
    a = _hash_messages("m", [{"role": "u", "content": "x"}], 0.7)
    b = _hash_messages("m", [{"role": "u", "content": "y"}], 0.7)
    assert a != b


def test_hash_changes_when_temperature_changes():
    a = _hash_messages("m", [{"role": "u", "content": "x"}], 0.7)
    b = _hash_messages("m", [{"role": "u", "content": "x"}], 0.2)
    assert a != b


# ── _estimate_prompt_tokens ──────────────────────────────────────────


def test_estimate_includes_safety_margin():
    """The estimate must over-count rather than under-count so the budget
    check is conservative."""
    empty = _estimate_prompt_tokens([])
    assert empty >= 500  # safety margin alone


def test_estimate_handles_list_content_parts():
    msg = {"role": "user", "content": [{"type": "text", "text": "abc"}]}
    est = _estimate_prompt_tokens([msg])
    assert est > 500


# ── A3: _PromptDedupTracker ──────────────────────────────────────────


def test_dedup_blocks_identical_hash_inside_window():
    tracker = _PromptDedupTracker()
    tracker.configure(enabled=True, window_sec=60)
    now = 1_000.0
    h = "abc"
    tracker.record(h, now=now)
    with pytest.raises(DuplicatePromptError) as exc:
        tracker.check_or_raise(h, now=now + 30.0)
    assert exc.value.prompt_hash == h
    assert exc.value.window_sec == 60


def test_dedup_allows_after_window_expires():
    tracker = _PromptDedupTracker()
    tracker.configure(enabled=True, window_sec=60)
    tracker.record("h", now=0.0)
    # Just before expiry — still blocked.
    with pytest.raises(DuplicatePromptError):
        tracker.check_or_raise("h", now=59.0)
    # Just after — passes.
    tracker.check_or_raise("h", now=60.5)


def test_dedup_does_not_block_distinct_hashes():
    tracker = _PromptDedupTracker()
    tracker.configure(enabled=True, window_sec=60)
    tracker.record("h1", now=0.0)
    tracker.check_or_raise("h2", now=10.0)  # different hash, no raise


def test_dedup_can_be_disabled_via_configure():
    tracker = _PromptDedupTracker()
    tracker.configure(enabled=False, window_sec=60)
    tracker.record("h", now=0.0)
    tracker.check_or_raise("h", now=1.0)  # disabled = no raise


def test_dedup_rejection_counters_tick_up():
    tracker = _PromptDedupTracker()
    tracker.configure(enabled=True, window_sec=60)
    tracker.record("h", now=0.0)
    for i in range(3):
        with pytest.raises(DuplicatePromptError):
            tracker.check_or_raise("h", now=10.0 + i)
    snap = tracker.snapshot()
    assert snap["rejected_count"] == 3
    assert snap["last_rejected_hash_short"] == "h"


def test_dedup_reset_clears_state():
    tracker = _PromptDedupTracker()
    tracker.configure(enabled=True, window_sec=60)
    tracker.record("h", now=0.0)
    with pytest.raises(DuplicatePromptError):
        tracker.check_or_raise("h", now=10.0)
    tracker.reset()
    # Now allowed again.
    tracker.check_or_raise("h", now=11.0)
    snap = tracker.snapshot()
    assert snap["tracked_prompts"] == 0
    assert snap["rejected_count"] == 0


def test_dedup_lru_eviction_caps_memory():
    """The tracker keeps at most _MAX_ENTRIES so a long-running process
    can't accumulate unbounded state. Older entries fall out FIFO."""
    tracker = _PromptDedupTracker()
    tracker.configure(enabled=True, window_sec=10_000)
    cap = _PromptDedupTracker._MAX_ENTRIES
    for i in range(cap + 50):
        tracker.record(f"h{i}", now=float(i))
    snap = tracker.snapshot()
    assert snap["tracked_prompts"] == cap
    # Oldest must have been evicted; newest must still be present.
    tracker.check_or_raise("h0", now=float(cap + 60))  # evicted → no raise
    with pytest.raises(DuplicatePromptError):
        tracker.check_or_raise(f"h{cap + 49}", now=float(cap + 60))


# ── A2: _BudgetTracker ───────────────────────────────────────────────


def test_budget_allows_calls_under_hourly_limit():
    tracker = _BudgetTracker()
    tracker.configure(enabled=True, hourly_limit=1000, daily_limit=10_000)
    tracker.check_or_raise(500, now=0.0)
    tracker.record_actual(500, now=0.0)
    tracker.check_or_raise(400, now=1.0)


def test_budget_blocks_when_hourly_limit_would_be_exceeded():
    tracker = _BudgetTracker()
    tracker.configure(enabled=True, hourly_limit=1000, daily_limit=10_000)
    tracker.record_actual(800, now=0.0)
    with pytest.raises(BudgetExceeded) as exc:
        tracker.check_or_raise(300, now=1.0)  # 800 + 300 > 1000
    assert exc.value.window == "hourly"
    assert exc.value.used_tokens == 800
    assert exc.value.limit_tokens == 1000


def test_budget_blocks_when_daily_limit_would_be_exceeded():
    tracker = _BudgetTracker()
    tracker.configure(enabled=True, hourly_limit=10_000, daily_limit=1000)
    tracker.record_actual(900, now=0.0)
    with pytest.raises(BudgetExceeded) as exc:
        tracker.check_or_raise(200, now=1.0)
    assert exc.value.window == "daily"


def test_budget_window_eviction_lets_calls_through_after_an_hour():
    tracker = _BudgetTracker()
    tracker.configure(enabled=True, hourly_limit=1000, daily_limit=10_000)
    tracker.record_actual(900, now=0.0)
    # Same window — blocked.
    with pytest.raises(BudgetExceeded):
        tracker.check_or_raise(500, now=10.0)
    # 1 hour + 1s later — eligible again (the old usage drops out of the
    # sliding hourly window).
    tracker.check_or_raise(500, now=3601.0)


def test_budget_can_be_disabled_via_configure():
    tracker = _BudgetTracker()
    tracker.configure(enabled=False, hourly_limit=1, daily_limit=1)
    # Should never raise when disabled, regardless of usage.
    tracker.check_or_raise(10_000_000, now=0.0)


def test_budget_snapshot_reports_used_remaining_and_rejections():
    tracker = _BudgetTracker()
    tracker.configure(enabled=True, hourly_limit=1000, daily_limit=10_000)
    tracker.record_actual(400, now=0.0)
    with pytest.raises(BudgetExceeded):
        tracker.check_or_raise(700, now=1.0)
    snap = tracker.snapshot()
    assert snap["hourly_used"] == 400
    assert snap["hourly_remaining"] == 600
    assert snap["daily_used"] == 400
    assert snap["daily_remaining"] == 9600
    assert snap["rejected_count"] == 1
    assert snap["last_rejected_reason"] == "hourly"


def test_budget_reset_clears_counters():
    tracker = _BudgetTracker()
    tracker.configure(enabled=True, hourly_limit=1000, daily_limit=10_000)
    tracker.record_actual(800, now=0.0)
    with pytest.raises(BudgetExceeded):
        tracker.check_or_raise(500, now=1.0)
    tracker.reset()
    snap = tracker.snapshot()
    assert snap["hourly_used"] == 0
    assert snap["daily_used"] == 0
    assert snap["rejected_count"] == 0
    # Calls allowed again.
    tracker.check_or_raise(500, now=2.0)


# ── BudgetExceeded must not be caught by `except Exception` ─────────


def test_budget_exceeded_is_not_caught_by_except_exception():
    """The headline invariant: a stray `except Exception:` block elsewhere
    in the codebase MUST NOT silently swallow a budget exhaustion. This
    is why BudgetExceeded inherits from BaseException, not RuntimeError."""
    tracker = _BudgetTracker()
    tracker.configure(enabled=True, hourly_limit=1, daily_limit=1)
    tracker.record_actual(2, now=0.0)
    caught_as_exception = False
    try:
        try:
            tracker.check_or_raise(1, now=1.0)
        except Exception:  # noqa: BLE001 — that's the point
            caught_as_exception = True
    except BudgetExceeded:
        pass  # propagated as expected
    assert caught_as_exception is False, (
        "BudgetExceeded was caught by `except Exception` — that breaks the "
        "last-line backstop. It must inherit from BaseException."
    )


def test_duplicate_prompt_error_IS_caught_by_except_exception():
    """Symmetric: DuplicatePromptError IS just RuntimeError. Legacy
    callers that do `try: llm.chat(); except Exception: fallback` should
    naturally absorb a dup as a fallback path, not crash."""
    tracker = _PromptDedupTracker()
    tracker.configure(enabled=True, window_sec=60)
    tracker.record("h", now=0.0)
    caught = False
    try:
        tracker.check_or_raise("h", now=1.0)
    except Exception:  # noqa: BLE001
        caught = True
    assert caught is True


# ── C1 forward-compat integration ────────────────────────────────────


def test_breaker_classifies_duplicate_prompt_as_non_failure():
    b = SchedulerCircuitBreaker(
        name="test", non_failure_exception_types=(DuplicatePromptError,)
    )
    err = DuplicatePromptError(prompt_hash="abc", last_sent_at=0.0, window_sec=60.0)
    wait = b.record_failure(err)
    assert wait == 0
    assert b.consecutive_failures == 0
    assert not b.is_paused()


def test_breaker_pauses_immediately_on_budget_exceeded():
    b = SchedulerCircuitBreaker(
        name="test",
        max_consecutive_failures=10,
        pause_immediately_exception_types=(BudgetExceeded,),
    )
    err = BudgetExceeded(window="daily", used_tokens=1000, limit_tokens=900, est_tokens=200)
    b.record_failure(err)
    assert b.is_paused()
    assert "BudgetExceeded" in (b.paused_reason or "")


# ── Module-level singletons ──────────────────────────────────────────


def test_singleton_getters_return_same_instances():
    a = get_prompt_dedup_tracker()
    b = get_prompt_dedup_tracker()
    assert a is b
    c = get_budget_tracker()
    d = get_budget_tracker()
    assert c is d


def test_global_trackers_are_independent_instances():
    assert get_prompt_dedup_tracker() is not get_budget_tracker()


# ── Admin endpoints round-trip ───────────────────────────────────────


def test_admin_health_includes_llm_budget_and_dedup_blocks():
    """Lock the shape that web/admin-health.html will consume."""
    import asyncio
    import aiosqlite
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.api_routes import router as api_router, set_dependencies
    from server.database import Database
    from server.scheduler_breaker import SchedulerCircuitBreaker
    from server.state_machine import StateMachine

    async def _make_db():
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await conn.execute(
            """CREATE TABLE state_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                inserted_at TEXT,
                type TEXT NOT NULL DEFAULT 'daily',
                content TEXT NOT NULL DEFAULT '',
                environment TEXT NOT NULL DEFAULT '{}',
                referenced_events TEXT NOT NULL DEFAULT '[]',
                embedding_vector_id TEXT,
                status TEXT NOT NULL DEFAULT 'done',
                prompt_hash TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                started_at TEXT,
                side_effects_status TEXT NOT NULL DEFAULT '{}'
            )"""
        )
        db = Database(":memory:")
        db._conn = conn
        return db

    db = asyncio.run(_make_db())
    try:
        sm = object.__new__(StateMachine)
        sm.snapshot_scheduler_breaker = SchedulerCircuitBreaker(name="snapshot_scheduler")
        sm.life_scheduler_breaker = SchedulerCircuitBreaker(name="life_scheduler")
        # Seed the trackers so we can verify the snapshot fields are populated.
        get_budget_tracker().reset()
        get_budget_tracker().configure(enabled=True, hourly_limit=1000, daily_limit=10_000)
        get_budget_tracker().record_actual(123, now=time.time())
        get_prompt_dedup_tracker().reset()
        get_prompt_dedup_tracker().configure(enabled=True, window_sec=60)
        get_prompt_dedup_tracker().record("seeded-hash", now=time.time())

        app = FastAPI()
        app.include_router(api_router)
        set_dependencies(db, sm, memory_store=None)

        with TestClient(app) as client:
            payload = client.get("/api/admin/health").json()
            assert "llm_budget" in payload
            assert "llm_dedup" in payload
            assert payload["llm_budget"]["hourly_used"] >= 123
            assert payload["llm_budget"]["hourly_limit"] == 1000
            assert payload["llm_dedup"]["tracked_prompts"] >= 1
            assert payload["llm_dedup"]["window_sec"] == 60
    finally:
        asyncio.run(db.close())
        # Leave trackers in a clean state for other tests.
        get_budget_tracker().reset()
        get_prompt_dedup_tracker().reset()


def test_admin_reset_endpoints_clear_tracker_state():
    import asyncio
    import aiosqlite
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.api_routes import router as api_router, set_dependencies
    from server.database import Database
    from server.scheduler_breaker import SchedulerCircuitBreaker
    from server.state_machine import StateMachine

    async def _make_db():
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await conn.execute(
            """CREATE TABLE state_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                inserted_at TEXT,
                type TEXT NOT NULL DEFAULT 'daily',
                content TEXT NOT NULL DEFAULT '',
                environment TEXT NOT NULL DEFAULT '{}',
                referenced_events TEXT NOT NULL DEFAULT '[]',
                embedding_vector_id TEXT,
                status TEXT NOT NULL DEFAULT 'done',
                prompt_hash TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                started_at TEXT,
                side_effects_status TEXT NOT NULL DEFAULT '{}'
            )"""
        )
        db = Database(":memory:")
        db._conn = conn
        return db

    db = asyncio.run(_make_db())
    try:
        sm = object.__new__(StateMachine)
        sm.snapshot_scheduler_breaker = SchedulerCircuitBreaker(name="snapshot_scheduler")
        sm.life_scheduler_breaker = SchedulerCircuitBreaker(name="life_scheduler")

        get_budget_tracker().reset()
        get_budget_tracker().configure(enabled=True, hourly_limit=1000, daily_limit=10_000)
        get_budget_tracker().record_actual(500, now=time.time())
        get_prompt_dedup_tracker().reset()
        get_prompt_dedup_tracker().configure(enabled=True, window_sec=60)
        get_prompt_dedup_tracker().record("h", now=time.time())

        app = FastAPI()
        app.include_router(api_router)
        set_dependencies(db, sm, memory_store=None)

        with TestClient(app) as client:
            # Reset budget.
            r = client.post("/api/admin/llm/budget/reset")
            assert r.status_code == 200
            body = r.json()
            assert body["ok"] is True
            assert body["before"]["hourly_used"] >= 500
            assert body["after"]["hourly_used"] == 0

            # Reset dedup.
            r = client.post("/api/admin/llm/dedup/reset")
            assert r.status_code == 200
            body = r.json()
            assert body["before"]["tracked_prompts"] >= 1
            assert body["after"]["tracked_prompts"] == 0
    finally:
        asyncio.run(db.close())
        get_budget_tracker().reset()
        get_prompt_dedup_tracker().reset()
