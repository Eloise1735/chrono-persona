"""C4 regression: end-to-end replay of the 21-hour token-burn incident.

The original incident, abridged from docs/fix_plan_snapshot_loop.md:

1. The state_snapshots table accumulated rows with mixed timezone literals
   ('...Z' and '+08:00'). String-sort returned the wrong "latest" row.
2. The scheduler tick built its prompt from that wrong baseline.
3. The LLM was called (13 161 prompt tokens), but a side-effect downstream
   raised, so finalize_snapshot was never reached and no row was persisted.
4. The next tick re-read the same wrong baseline, rebuilt the same prompt,
   called the LLM AGAIN. This repeated every ~2-3 minutes for 21 hours.
5. No alarm fired because the scheduler loop's only response to a failing
   tick was `logger.exception(...)` then sleep.

This single test walks through the same scenario using the actual production
primitives — Database / SchedulerCircuitBreaker / StateMachine._run_side_effect /
StateMachine._compute_prompt_hash plus the /api/admin/health + resume
endpoints — and asserts that every layer of defense holds:

  • B1: get_latest_snapshot uses julianday() so mixed-timezone rows sort
        by real instant, not by string.
  • B2: a placeholder row is committed BEFORE the LLM call; a side-effect
        failure leaves the snapshot status='done' so the next tick
        cache-hits and does NOT re-call the LLM.
  • B3: side-effect failures log into side_effects_status but do not raise
        out of the tick.
  • C1: 10 consecutive LLM failures (a different bad prompt) trip the
        breaker into paused state; further ticks are skipped without
        calling the LLM.
  • C3: /api/admin/health surfaces the paused state; /api/admin/scheduler/
        {name}/resume clears it.

If this test ever fails, the 21-hour incident has become possible again.
Find the matching subsystem and fix the underlying invariant before
deleting any assertions here.

See tests/SNAPSHOT_LOOP_SAFETY.md for the full invariants registry.
"""

from __future__ import annotations

import asyncio
import json

import aiosqlite
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.api_routes import router as api_router, set_dependencies
from server.database import Database
from server.scheduler_breaker import SchedulerCircuitBreaker
from server.state_machine import StateMachine


def run(coro):
    return asyncio.run(coro)


async def _make_db():
    """In-memory DB with the production state_snapshots schema."""
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
    return db, conn


class _CountingLLM:
    """Records every (prompt) -> output pair. The headline metric is
    `len(llm.calls)` — if any layer of defense fails, this number grows
    in the cache-hit / paused scenarios where it must not."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.next_raises: list[Exception] = []
        self.next_output: str = "llm-output"

    async def chat(self, prompt: str) -> str:
        self.calls.append(prompt)
        if self.next_raises:
            raise self.next_raises.pop(0)
        return self.next_output


async def _simulate_scheduler_tick(
    *,
    db: Database,
    llm: _CountingLLM,
    breaker: SchedulerCircuitBreaker,
    checkpoint_cst: str,
    prompt: str,
    baseline_content: str | None = None,
    side_effect_to_raise: Exception | None = None,
) -> dict:
    """Mirror the production tick: latest baseline → idempotency barrier →
    placeholder → LLM → finalize → side effects → update side_effects_status.

    `baseline_content` is taken explicitly when provided; otherwise it's read
    from db.get_latest_snapshot(). This mirrors _advance_until_locked, which
    reads the baseline ONCE at the top of the tick and reuses it for the
    whole checkpoint loop — so callers can pin the baseline to demonstrate
    cache-hit semantics for the same logical tick re-firing.

    Returns a result dict for assertions. NEVER raises; on failure inside
    the tick body the breaker.record_failure path runs and the function
    returns {'branch': 'tick_failed', 'error': ...}.
    """
    if breaker.is_paused():
        return {"branch": "skipped_paused"}

    breaker.record_tick_start()

    try:
        if baseline_content is None:
            baseline = await db.get_latest_snapshot()
            baseline_content = baseline.content if baseline else "(no baseline)"

        prompt_hash = StateMachine._compute_prompt_hash(
            "snapshot_scheduler", checkpoint_cst, baseline_content, prompt
        )

        # B2 idempotency: stale recovery + done/in-flight/dead-letter gates.
        await db.reset_stale_in_flight_snapshots(older_than_seconds=600)
        done = await db.find_done_snapshot_by_prompt_hash(prompt_hash)
        if done is not None:
            breaker.record_success()
            return {"branch": "cache_hit", "snapshot_id": done.id, "content": done.content}
        in_flight = await db.find_in_flight_snapshot_by_prompt_hash(prompt_hash)
        if in_flight is not None:
            breaker.record_success()  # idempotent skip is not a failure
            return {"branch": "in_flight_skip", "snapshot_id": in_flight.id}
        failed = await db.count_failed_snapshot_attempts(prompt_hash)
        if failed >= 3:
            breaker.record_success()
            return {"branch": "dead_letter", "failed_count": failed}

        placeholder_id = await db.insert_snapshot_placeholder(
            prompt_hash=prompt_hash,
            snap_type="daily",
            created_at="2026-06-01T00:00:00Z",
            attempt_count=failed + 1,
        )

        # B2 main transaction.
        try:
            content = await llm.chat(prompt)
            await db.finalize_snapshot(placeholder_id, content=content)
        except Exception:
            await db.mark_snapshot_failed(placeholder_id)
            raise

        # B3 side effects: optionally inject one failure.
        side_effects: dict[str, str] = {}

        async def side_a():
            return "ok-a"

        async def side_b():
            if side_effect_to_raise is not None:
                raise side_effect_to_raise
            return "ok-b"

        async def side_c():
            return "ok-c"

        await StateMachine._run_side_effect("a", side_a(), side_effects)
        await StateMachine._run_side_effect("b", side_b(), side_effects)
        await StateMachine._run_side_effect("c", side_c(), side_effects)

        await db.update_snapshot_side_effects_status(placeholder_id, side_effects)
        breaker.record_success()
        return {
            "branch": "proceeded",
            "snapshot_id": placeholder_id,
            "content": content,
            "side_effects": side_effects,
        }
    except Exception as exc:
        breaker.record_failure(exc)
        return {"branch": "tick_failed", "error": f"{type(exc).__name__}: {exc}"}


# ─────────────────────────────────────────────────────────────────────
#  The incident replay
# ─────────────────────────────────────────────────────────────────────


def test_incident_replay_all_defenses_hold():
    """Single end-to-end walkthrough of the 21-hour incident with every
    defense layer asserted at the spot where it must fire."""

    async def scenario():
        db, conn = await _make_db()
        try:
            # ─ Step 0: Seed the pre-incident DB. Three historical "done"
            # snapshots in mixed-timezone formats — exactly the kind of
            # data that broke get_latest_snapshot in the original incident.
            await conn.execute(
                "INSERT INTO state_snapshots (created_at, content, status) VALUES (?, ?, 'done')",
                ("2026-05-26T01:00:00Z", "very-old"),
            )
            await conn.execute(
                "INSERT INTO state_snapshots (created_at, content, status) VALUES (?, ?, 'done')",
                ("2026-05-26T10:00:00+08:00", "actually-02:00Z"),  # 02:00 UTC
            )
            await conn.execute(
                "INSERT INTO state_snapshots (created_at, content, status) VALUES (?, ?, 'done')",
                ("2026-05-26T06:00:00Z", "true-latest"),
            )
            await conn.commit()

            # ━━━ B1 invariant: get_latest_snapshot returns the row with
            # the latest REAL instant, not the latest string. The original
            # incident corrupted the baseline at exactly this point.
            latest = await db.get_latest_snapshot()
            assert latest.content == "true-latest", (
                "B1 regressed: get_latest_snapshot returned a row that is "
                "not the latest real-time instant. This would corrupt the "
                "scheduler's baseline and replay the original incident."
            )

            # ─ Step 1: A normal scheduler tick succeeds end-to-end.
            # We pin the baseline explicitly to mirror production:
            # _advance_until_locked reads baseline once per tick.
            llm = _CountingLLM()
            breaker = SchedulerCircuitBreaker(
                name="snapshot_scheduler", max_consecutive_failures=10
            )
            llm.next_output = "snapshot for 08:00"
            BASELINE_FROZEN = "true-latest"

            result = await _simulate_scheduler_tick(
                db=db, llm=llm, breaker=breaker,
                checkpoint_cst="2026-06-01T08:00:00+08:00",
                prompt="prompt-A",
                baseline_content=BASELINE_FROZEN,
            )
            assert result["branch"] == "proceeded"
            assert len(llm.calls) == 1
            assert breaker.consecutive_failures == 0
            assert not breaker.is_paused()

            # ─ Step 2: The same logical tick re-fires (same baseline + same
            # checkpoint + same prompt). In production this happens when a
            # process restarts mid-tick or when a fast-cycle loop fires
            # before the new snapshot has propagated. Without B2 this would
            # burn the same 13 161-token prompt a second time.
            result = await _simulate_scheduler_tick(
                db=db, llm=llm, breaker=breaker,
                checkpoint_cst="2026-06-01T08:00:00+08:00",
                prompt="prompt-A",
                baseline_content=BASELINE_FROZEN,
            )
            assert result["branch"] == "cache_hit", (
                "B2 regressed: an identical prompt was allowed to call the LLM "
                "again. This is the headline invariant — without it, the "
                "original 21-hour incident replays."
            )
            assert len(llm.calls) == 1, (
                "B2 regressed: LLM was invoked on a cache-hit prompt; "
                f"calls={len(llm.calls)}."
            )

            # ─ Step 3: A NEW checkpoint where one side effect fails. The
            # snapshot must still be persisted as done (B3); the next tick
            # for that prompt must still cache-hit (B2 main transaction
            # survived even though a side effect raised).
            llm.next_output = "snapshot for 09:00"
            result = await _simulate_scheduler_tick(
                db=db, llm=llm, breaker=breaker,
                checkpoint_cst="2026-06-01T09:00:00+08:00",
                prompt="prompt-B",
                baseline_content=BASELINE_FROZEN,
                side_effect_to_raise=RuntimeError("OB endpoint down"),
            )
            assert result["branch"] == "proceeded", (
                "B3 regressed: a side-effect failure propagated out of the "
                "tick. This would tick up scheduler.consecutive_failures "
                "even though the snapshot itself was fine."
            )
            assert result["side_effects"]["a"] == "ok"
            assert result["side_effects"]["b"].startswith("failed:RuntimeError:")
            assert result["side_effects"]["c"] == "ok"
            assert breaker.consecutive_failures == 0

            # B3 persisted the per-effect outcomes on the snapshot row.
            row = await db.get_snapshot_by_id(result["snapshot_id"])
            stored = json.loads(row.side_effects_status or "{}")
            assert stored["a"] == "ok"
            assert stored["b"].startswith("failed:RuntimeError:")
            assert stored["c"] == "ok"

            # B2 cache-hit invariant survives even though a side effect
            # had failed earlier — the main transaction had already
            # committed status='done' before the side effect ran.
            calls_before = len(llm.calls)
            result = await _simulate_scheduler_tick(
                db=db, llm=llm, breaker=breaker,
                checkpoint_cst="2026-06-01T09:00:00+08:00",
                prompt="prompt-B",
                baseline_content=BASELINE_FROZEN,
            )
            assert result["branch"] == "cache_hit"
            assert len(llm.calls) == calls_before, (
                "B2 regressed for the side-effect-failure case: the next "
                "tick burned another LLM call even though the main snapshot "
                "had already been committed."
            )

            # ─ Step 4: The LLM itself goes down (transient 502). Each
            # failing tick must (a) NOT mark the snapshot as done and
            # (b) trip C1 after max_consecutive_failures.
            for i in range(10):
                llm.next_raises.append(RuntimeError(f"upstream 502 #{i}"))
                result = await _simulate_scheduler_tick(
                    db=db, llm=llm, breaker=breaker,
                    checkpoint_cst="2026-06-01T10:00:00+08:00",
                    prompt=f"prompt-flaky-{i}",  # unique each time
                    baseline_content=BASELINE_FROZEN,
                )
                assert result["branch"] == "tick_failed"
            assert breaker.is_paused(), (
                "C1 regressed: 10 consecutive failures did not trip the "
                "circuit breaker. Without C1 the scheduler would keep "
                "retrying at full cadence."
            )
            assert breaker.consecutive_failures == 10

            # ─ Step 5: Once paused, further ticks short-circuit without
            # calling the LLM. This is the property that would have
            # stopped the original 21-hour bleed within minutes.
            calls_before = len(llm.calls)
            for i in range(5):
                result = await _simulate_scheduler_tick(
                    db=db, llm=llm, breaker=breaker,
                    checkpoint_cst="2026-06-01T11:00:00+08:00",
                    prompt=f"prompt-while-paused-{i}",
                    baseline_content=BASELINE_FROZEN,
                )
                assert result["branch"] == "skipped_paused"
            assert len(llm.calls) == calls_before, (
                "C1 regressed: paused breaker still allowed LLM calls."
            )

            # ─ Step 6: /api/admin/health reflects the paused state and
            # the recent in-flight rows. This is the property that would
            # have flagged the original incident in the first 10 minutes.
            sm = object.__new__(StateMachine)
            sm.snapshot_scheduler_breaker = breaker
            sm.life_scheduler_breaker = SchedulerCircuitBreaker(name="life_scheduler")
            app = FastAPI()
            app.include_router(api_router)
            set_dependencies(db, sm, memory_store=None)

            with TestClient(app) as client:
                r = client.get("/api/admin/health")
                assert r.status_code == 200
                payload = r.json()
                assert payload["schedulers"]["snapshot_scheduler"]["paused"] is True
                assert payload["schedulers"]["snapshot_scheduler"]["consecutive_failures"] >= 10
                # last_snapshot_at must still see the legit done rows
                # from steps 1 and 3, not any of the failed placeholder rows.
                assert payload["last_snapshot_at"] is not None

                # ─ Step 7: One-click admin resume restores the loop.
                r = client.post("/api/admin/scheduler/snapshot_scheduler/resume")
                assert r.status_code == 200
                assert r.json()["was_paused"] is True
                assert not breaker.is_paused()
                assert breaker.consecutive_failures == 0

            # ─ Step 8: After resume, a fresh good tick works again.
            llm.next_output = "post-recovery snapshot"
            result = await _simulate_scheduler_tick(
                db=db, llm=llm, breaker=breaker,
                checkpoint_cst="2026-06-01T12:00:00+08:00",
                prompt="prompt-post-recovery",
                baseline_content=BASELINE_FROZEN,
            )
            assert result["branch"] == "proceeded"
            assert not breaker.is_paused()
            assert breaker.consecutive_failures == 0
        finally:
            await db.close()

    run(scenario())


def test_stale_in_flight_recovery_unblocks_a_stuck_prompt():
    """A separate regression: if a previous tick crashed mid-flight (placeholder
    inserted, LLM call hung, process killed), the row stays in_flight forever.
    B2's reset_stale_in_flight_snapshots flips rows older than 10 min to
    failed so the next tick can either retry or dead-letter — instead of
    being permanently blocked by the in_flight check."""

    async def scenario():
        db, conn = await _make_db()
        try:
            # Inject a stale in_flight row (11 minutes old).
            await conn.execute(
                """INSERT INTO state_snapshots (created_at, status, prompt_hash, started_at, attempt_count)
                   VALUES ('2026-06-01T08:00:00Z', 'in_flight', 'hash-stuck', datetime('now', '-660 seconds'), 1)"""
            )
            await conn.commit()

            # find_in_flight returns the row, so without recovery the next
            # tick would skip indefinitely.
            assert await db.find_in_flight_snapshot_by_prompt_hash("hash-stuck") is not None

            # Simulate the tick's stale-recovery call.
            reset = await db.reset_stale_in_flight_snapshots(older_than_seconds=600)
            assert len(reset) == 1

            # Now the in_flight gate clears; failure count is 1, so a new
            # attempt would proceed (< dead-letter threshold of 3).
            assert await db.find_in_flight_snapshot_by_prompt_hash("hash-stuck") is None
            assert await db.count_failed_snapshot_attempts("hash-stuck") == 1
        finally:
            await db.close()

    run(scenario())


def test_admin_health_renders_during_an_active_in_flight_window():
    """While a tick is mid-LLM (placeholder committed, finalize not yet
    called), /api/admin/health must list the row in in_flight_snapshots
    AND must NOT report it as last_snapshot_at — otherwise the admin
    page would show a stale baseline."""

    async def scenario():
        db, conn = await _make_db()
        try:
            # Done baseline.
            await conn.execute(
                "INSERT INTO state_snapshots (created_at, content, status, type) VALUES (?, 'real', 'done', 'daily')",
                ("2026-05-01T00:00:00Z",),
            )
            # In-flight placeholder for a NEWER checkpoint that hasn't
            # finalized yet.
            await conn.execute(
                """INSERT INTO state_snapshots (created_at, status, prompt_hash, started_at, attempt_count, type)
                   VALUES ('2026-07-01T00:00:00Z', 'in_flight', 'hash-active', datetime('now', '-3 seconds'), 1, 'daily')"""
            )
            await conn.commit()

            sm = object.__new__(StateMachine)
            sm.snapshot_scheduler_breaker = SchedulerCircuitBreaker(name="snapshot_scheduler")
            sm.life_scheduler_breaker = SchedulerCircuitBreaker(name="life_scheduler")
            app = FastAPI()
            app.include_router(api_router)
            set_dependencies(db, sm, memory_store=None)

            with TestClient(app) as client:
                payload = client.get("/api/admin/health").json()
                # The in-flight row appears in the list.
                assert payload["in_flight_count"] == 1
                assert payload["in_flight_snapshots"][0]["prompt_hash"] == "hash-active"
                # But it must NOT have shifted last_snapshot_at.
                assert payload["last_snapshot_at"] == "2026-05-01T00:00:00Z"
        finally:
            await db.close()

    run(scenario())
