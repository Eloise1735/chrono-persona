"""B3 acceptance: post-snapshot side-effect isolation.

Once finalize_snapshot has written status='done', the LLM cost is sunk.
Failures in the OB hold / relationship-thought / life-flow-trace / slowlines
write-paths that follow must NOT raise out of the tick — otherwise the
scheduler's failure counter ticks up and (worse, pre-C1) the loop would
just retry. Instead each side effect is wrapped by
StateMachine._run_side_effect, which logs the failure into a per-snapshot
status accumulator persisted via Database.update_snapshot_side_effects_status.

See docs/fix_plan_snapshot_loop.md B3.
"""

from __future__ import annotations

import asyncio
import json

import aiosqlite

from server.database import Database
from server.state_machine import StateMachine


def run(coro):
    return asyncio.run(coro)


async def _make_db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    db = Database(":memory:")
    db._conn = conn
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
    return db, conn


# ── _run_side_effect: the unit under test ──


def test_run_side_effect_records_ok_on_success():
    async def scenario():
        async def good():
            return 42

        status: dict[str, str] = {}
        result = await StateMachine._run_side_effect("foo", good(), status)
        assert result == 42
        assert status == {"foo": "ok"}

    run(scenario())


def test_run_side_effect_records_failure_and_returns_default():
    async def scenario():
        async def bad():
            raise ValueError("boom")

        status: dict[str, str] = {}
        result = await StateMachine._run_side_effect(
            "writer", bad(), status, default="fallback"
        )
        assert result == "fallback"
        assert status["writer"].startswith("failed:ValueError:")
        assert "boom" in status["writer"]

    run(scenario())


def test_run_side_effect_swallows_unrelated_failures_independently():
    """Two independent side effects: one fails, the other succeeds. The
    failure of the first must NOT prevent the second from running."""

    async def scenario():
        async def explodes():
            raise RuntimeError("first-step-broken")

        async def works():
            return "second-step-output"

        status: dict[str, str] = {}
        r1 = await StateMachine._run_side_effect("first", explodes(), status)
        r2 = await StateMachine._run_side_effect("second", works(), status)
        assert r1 is None
        assert r2 == "second-step-output"
        assert status["first"].startswith("failed:RuntimeError:")
        assert status["second"] == "ok"

    run(scenario())


def test_run_side_effect_truncates_overlong_error_messages():
    """Very long upstream stack traces must not poison the JSON column —
    truncate to ~240 chars."""

    async def scenario():
        long_msg = "x" * 1000

        async def bad():
            raise Exception(long_msg)

        status: dict[str, str] = {}
        await StateMachine._run_side_effect("noisy", bad(), status)
        recorded = status["noisy"]
        assert recorded.startswith("failed:Exception:")
        # 240 message chars + prefix; should not be anywhere near 1000.
        assert len(recorded) < 320, f"recorded length {len(recorded)} not truncated"
        assert recorded.endswith("…")

    run(scenario())


def test_run_side_effect_default_is_none_when_unspecified():
    async def scenario():
        async def bad():
            raise KeyError("absent")

        status: dict[str, str] = {}
        result = await StateMachine._run_side_effect("step", bad(), status)
        assert result is None

    run(scenario())


# ── Persistence layer: update_snapshot_side_effects_status ──


def test_update_snapshot_side_effects_status_writes_json_payload():
    async def scenario():
        db, conn = await _make_db()
        try:
            # Seed a snapshot row (mimics finalize_snapshot output).
            row = await conn.execute(
                "INSERT INTO state_snapshots (created_at, content, status) VALUES (?, ?, 'done')",
                ("2026-06-01T00:00:00Z", "snapshot body"),
            )
            await conn.commit()
            snap_id = row.lastrowid

            payload = {
                "ob_hold": "ok",
                "relationship_thought": "failed:RuntimeError:downstream 502",
                "slowlines": "ok",
            }
            await db.update_snapshot_side_effects_status(snap_id, payload)

            async with conn.execute(
                "SELECT side_effects_status FROM state_snapshots WHERE id=?", (snap_id,)
            ) as cur:
                row = await cur.fetchone()
            stored = json.loads(row["side_effects_status"])
            assert stored == payload
        finally:
            await db.close()

    run(scenario())


def test_update_snapshot_side_effects_status_overwrites_previous_payload():
    async def scenario():
        db, conn = await _make_db()
        try:
            row = await conn.execute(
                "INSERT INTO state_snapshots (created_at, content) VALUES (?, ?)",
                ("2026-06-01T00:00:00Z", "body"),
            )
            await conn.commit()
            snap_id = row.lastrowid

            await db.update_snapshot_side_effects_status(snap_id, {"a": "ok"})
            await db.update_snapshot_side_effects_status(
                snap_id, {"a": "ok", "b": "failed:X:y"}
            )

            async with conn.execute(
                "SELECT side_effects_status FROM state_snapshots WHERE id=?", (snap_id,)
            ) as cur:
                row = await cur.fetchone()
            assert json.loads(row["side_effects_status"]) == {
                "a": "ok",
                "b": "failed:X:y",
            }
        finally:
            await db.close()

    run(scenario())


def test_update_snapshot_side_effects_status_empty_payload_is_safe():
    """The DB column default is '{}', and reflect/scheduler may legitimately
    record zero effects (e.g. on cache_hit return). Writing an empty dict
    must not crash."""

    async def scenario():
        db, conn = await _make_db()
        try:
            row = await conn.execute(
                "INSERT INTO state_snapshots (created_at, content) VALUES (?, ?)",
                ("2026-06-01T00:00:00Z", "body"),
            )
            await conn.commit()
            await db.update_snapshot_side_effects_status(row.lastrowid, {})

            async with conn.execute(
                "SELECT side_effects_status FROM state_snapshots WHERE id=?",
                (row.lastrowid,),
            ) as cur:
                got = await cur.fetchone()
            assert json.loads(got["side_effects_status"]) == {}
        finally:
            await db.close()

    run(scenario())


# ── End-to-end contract: a failing side effect does not block subsequent ones ──


def test_chained_side_effects_continue_after_a_failure_and_persist_status():
    """Simulate the post-finalize chain of side effects. The 2nd one raises;
    the 1st, 3rd and 4th still run; the per-effect status is fully
    persisted onto the snapshot row."""

    async def scenario():
        db, conn = await _make_db()
        try:
            row = await conn.execute(
                "INSERT INTO state_snapshots (created_at, content, status) VALUES (?, ?, 'done')",
                ("2026-06-01T00:00:00Z", "snapshot body"),
            )
            await conn.commit()
            snap_id = row.lastrowid

            calls: list[str] = []

            async def ob_hold():
                calls.append("ob_hold")
                return "bucket-1"

            async def relationship_thought():
                calls.append("relationship_thought")
                raise RuntimeError("OB endpoint down")

            async def slowlines_refresh():
                calls.append("slowlines_refresh")
                return None

            async def life_flow_trace():
                calls.append("life_flow_trace")
                return None

            side_effects: dict[str, str] = {}
            assert (
                await StateMachine._run_side_effect("ob_hold", ob_hold(), side_effects)
                == "bucket-1"
            )
            await StateMachine._run_side_effect(
                "relationship_thought", relationship_thought(), side_effects
            )
            await StateMachine._run_side_effect(
                "slowlines_refresh", slowlines_refresh(), side_effects
            )
            await StateMachine._run_side_effect(
                "life_flow_trace", life_flow_trace(), side_effects
            )

            # All four ran in order, regardless of #2's failure.
            assert calls == [
                "ob_hold",
                "relationship_thought",
                "slowlines_refresh",
                "life_flow_trace",
            ]
            # Status reflects mixed outcomes.
            assert side_effects["ob_hold"] == "ok"
            assert side_effects["relationship_thought"].startswith(
                "failed:RuntimeError:"
            )
            assert side_effects["slowlines_refresh"] == "ok"
            assert side_effects["life_flow_trace"] == "ok"

            # Persist and round-trip through the DB.
            await db.update_snapshot_side_effects_status(snap_id, side_effects)
            async with conn.execute(
                "SELECT status, side_effects_status FROM state_snapshots WHERE id=?",
                (snap_id,),
            ) as cur:
                row = await cur.fetchone()
            # Main transaction is still 'done' — side-effect failures do not
            # corrupt the snapshot's success state.
            assert row["status"] == "done"
            stored = json.loads(row["side_effects_status"])
            assert stored == side_effects
        finally:
            await db.close()

    run(scenario())
