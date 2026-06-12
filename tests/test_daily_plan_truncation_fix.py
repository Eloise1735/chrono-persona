"""Regression: daily-plan generation truncation loop (the gemini-3-flash incident).

Root cause: DAILY_PLAN_GENERATION_PROMPT asks for ~20 structured fields per
hourly block across the whole waking day → 6k–12k output tokens, but
generate_daily_plan capped the call at max_tokens=4000. gemini-3-flash hit
the cap on every call (completion≈3996), the JSON array was truncated and
unparseable, _extract_json_array returned [], PlanGenerationEmptyError was
raised, ensure_today_plan swallowed it and wrote no active-plan marker, and
the 60s scheduler retried all day — burning ~14k tokens/tick.

Three layered fixes, each pinned here:
  1. max_tokens is configurable and defaults high enough for a full day.
  2. _salvage_json_array recovers complete leading objects from a truncated
     array so a partial plan still gets written (and the loop stops).
  3. ensure_today_plan enforces a per-day failure cap so a persistent
     failure can't retry every tick for the whole day.
"""

from __future__ import annotations

import asyncio

from server.plan_engine import (
    PlanEngine,
    PlanGenerationEmptyError,
    _extract_json_array,
    _salvage_json_array,
)
from server.prompts import KEY_PLAN_GENERATION_HOUR


def run(coro):
    return asyncio.run(coro)


# ── _salvage_json_array ──────────────────────────────────────────────


def test_salvage_recovers_objects_from_truncated_array():
    """The exact failure shape: a JSON array cut off mid-object at the token
    cap. The complete leading objects must be recovered."""
    truncated = (
        '[{"hour_start":7,"hour_end":8,"activity":"晨间核对"},'
        '{"hour_start":8,"hour_end":9,"activity":"配药台盘点"},'
        '{"hour_start":9,"hour_end":10,"activity":"档案室抽案","reason":"接上未结'  # ← cut here
    )
    items = _salvage_json_array(truncated)
    assert len(items) == 2
    assert items[0]["activity"] == "晨间核对"
    assert items[1]["hour_start"] == 8


def test_salvage_handles_nested_objects_and_arrays_inside_items():
    truncated = (
        '[{"hour_start":7,"action_payload":{"watch_points":["a","b"],'
        '"progress_outline":{"goal":"x"}}},'
        '{"hour_start":8,"action_payload":{"tags":[1,2,3]}},'
        '{"hour_start":9,"action_payload":{"goal":"truncated'  # ← cut inside nested string
    )
    items = _salvage_json_array(truncated)
    assert len(items) == 2
    assert items[0]["action_payload"]["progress_outline"]["goal"] == "x"
    assert items[1]["action_payload"]["tags"] == [1, 2, 3]


def test_salvage_ignores_braces_inside_strings():
    truncated = '[{"activity":"清点 { 药盒 } 库存","hour_start":7},{"activity":"truncated'
    items = _salvage_json_array(truncated)
    assert len(items) == 1
    assert items[0]["activity"] == "清点 { 药盒 } 库存"


def test_salvage_handles_prose_prefix_before_array():
    truncated = 'Here is the plan:\n[{"hour_start":7,"activity":"x"},{"hour'
    items = _salvage_json_array(truncated)
    assert len(items) == 1
    assert items[0]["activity"] == "x"


def test_salvage_on_complete_array_returns_all():
    complete = '[{"a":1},{"b":2},{"c":3}]'
    items = _salvage_json_array(complete)
    assert len(items) == 3


def test_salvage_returns_empty_when_no_array():
    assert _salvage_json_array("no brackets here") == []
    assert _salvage_json_array("") == []


def test_strict_parser_still_fails_on_truncation_so_salvage_is_needed():
    """Confirms the gap salvage fills: strict parse returns [] on a
    truncated array."""
    truncated = '[{"a":1},{"b":2},{"c":'
    assert _extract_json_array(truncated) == []
    assert len(_salvage_json_array(truncated)) == 2


# ── ensure_today_plan per-day failure cap ────────────────────────────


def _make_engine_with_stubs(*, generate_behavior, max_attempts=3, gen_hour=0):
    """Build a skeletal PlanEngine whose ensure_today_plan dependencies are
    stubbed. `generate_behavior` is an async callable(today) controlling
    whether generate_daily_plan succeeds or raises."""
    pe = object.__new__(PlanEngine)
    pe._plan_gen_attempts = {}
    pe._plan_gen_giveup_logged = {}

    state = {"generate_calls": 0, "active_plan": None}

    async def fake_is_enabled():
        return True

    async def fake_int_setting(key, default):
        if key == KEY_PLAN_GENERATION_HOUR:
            return gen_hour  # 0 → always past generation hour
        if key == "plan_generation_max_attempts_per_day":
            return max_attempts
        return default

    class _DB:
        async def get_latest_daily_plan_for_date(self, d, status=None):
            return state["active_plan"]

    async def fake_generate(today):
        state["generate_calls"] += 1
        await generate_behavior(today, state)

    pe.is_enabled = fake_is_enabled
    pe._int_setting = fake_int_setting
    pe.db = _DB()
    pe.generate_daily_plan = fake_generate
    return pe, state


def test_persistent_failure_stops_after_cap():
    """The headline structural fix: a generation that fails every time must
    NOT keep calling the LLM every tick. After max_attempts the loop stops."""

    async def always_fail(today, state):
        raise PlanGenerationEmptyError(today)

    pe, state = _make_engine_with_stubs(generate_behavior=always_fail, max_attempts=3)

    async def scenario():
        # Simulate 8 scheduler ticks.
        for _ in range(8):
            await pe.ensure_today_plan()

    run(scenario())
    # generate_daily_plan was attempted at most max_attempts times, not 8.
    assert state["generate_calls"] == 3


def test_success_clears_failure_counter():
    """After some failures, a success must clear the counter so a future
    bad day starts clean."""
    flips = {"calls": 0}

    async def fail_twice_then_succeed(today, state):
        flips["calls"] += 1
        if flips["calls"] <= 2:
            raise PlanGenerationEmptyError(today)
        # On the 3rd call, pretend a plan now exists.
        state["active_plan"] = object()

    pe, state = _make_engine_with_stubs(
        generate_behavior=fail_twice_then_succeed, max_attempts=3
    )

    async def scenario():
        for _ in range(5):
            await pe.ensure_today_plan()

    run(scenario())
    # 2 failures + 1 success = 3 generate calls; subsequent ticks see the
    # active plan and short-circuit before generate.
    assert state["generate_calls"] == 3
    # Counter cleared on success.
    assert pe._plan_gen_attempts == {}


def test_existing_active_plan_skips_generation_and_clears_counter():
    async def always_fail(today, state):
        raise PlanGenerationEmptyError(today)

    pe, state = _make_engine_with_stubs(generate_behavior=always_fail)
    # Pre-seed a stale failure counter and an existing active plan.
    import datetime as _dt
    # Use whatever 'today' resolves to inside ensure_today_plan; seed broadly.
    pe._plan_gen_attempts["stale-date"] = 2
    state["active_plan"] = object()

    async def scenario():
        await pe.ensure_today_plan()

    run(scenario())
    assert state["generate_calls"] == 0


def test_giveup_is_logged_once(caplog):
    import logging

    async def always_fail(today, state):
        raise PlanGenerationEmptyError(today)

    pe, state = _make_engine_with_stubs(generate_behavior=always_fail, max_attempts=2)

    async def scenario():
        for _ in range(6):
            await pe.ensure_today_plan()

    with caplog.at_level(logging.ERROR):
        run(scenario())

    giveup_logs = [r for r in caplog.records if "GIVING UP daily plan" in r.getMessage()]
    assert len(giveup_logs) == 1  # logged exactly once, not every tick
    assert state["generate_calls"] == 2


def test_prune_drops_other_dates():
    pe = object.__new__(PlanEngine)
    pe._plan_gen_attempts = {"2026-06-10": 3, "2026-06-11": 1, "2026-06-12": 2}
    pe._plan_gen_giveup_logged = {"2026-06-10": True, "2026-06-12": False}
    pe._prune_plan_gen_attempts("2026-06-12")
    assert set(pe._plan_gen_attempts.keys()) == {"2026-06-12"}
    assert set(pe._plan_gen_giveup_logged.keys()) == {"2026-06-12"}
