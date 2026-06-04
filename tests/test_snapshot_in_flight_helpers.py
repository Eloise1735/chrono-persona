"""B2 acceptance: in-flight idempotency barrier — Database helpers.

These tests exercise the Database-layer primitives that callers (snapshot
scheduler tick and reflect_on_conversation) will use to reserve a placeholder
row before calling the LLM. See docs/fix_plan_snapshot_loop.md B2.
"""

from __future__ import annotations

import asyncio
import time

import aiosqlite

from server.database import Database
from server.models import StateSnapshot


def run(coro):
    return asyncio.run(coro)


def _make_db_with_schema(conn):
    return conn.execute(
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


async def _make_db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    db = Database(":memory:")
    db._conn = conn
    await _make_db_with_schema(conn)
    return db, conn


def test_insert_placeholder_creates_in_flight_row():
    async def scenario():
        db, conn = await _make_db()
        try:
            row_id = await db.insert_snapshot_placeholder(
                prompt_hash="hash-A", snap_type="daily"
            )
            async with conn.execute(
                "SELECT status, prompt_hash, attempt_count, content, started_at FROM state_snapshots WHERE id=?",
                (row_id,),
            ) as cur:
                row = await cur.fetchone()
            assert row["status"] == "in_flight"
            assert row["prompt_hash"] == "hash-A"
            assert row["attempt_count"] == 1
            assert row["content"] == ""
            assert row["started_at"] is not None
        finally:
            await db.close()

    run(scenario())


def test_finalize_snapshot_marks_done_and_writes_content():
    async def scenario():
        db, conn = await _make_db()
        try:
            row_id = await db.insert_snapshot_placeholder(prompt_hash="hash-B")
            await db.finalize_snapshot(
                row_id,
                content="hello world",
                environment='{"weather":"sunny"}',
                referenced_events="[1,2]",
                created_at="2026-06-01T10:00:00+08:00",
                embedding_vector_id="vec-1",
            )
            async with conn.execute(
                "SELECT status, content, environment, referenced_events, created_at, embedding_vector_id FROM state_snapshots WHERE id=?",
                (row_id,),
            ) as cur:
                row = await cur.fetchone()
            assert row["status"] == "done"
            assert row["content"] == "hello world"
            assert row["environment"] == '{"weather":"sunny"}'
            assert row["referenced_events"] == "[1,2]"
            assert row["created_at"] == "2026-06-01T02:00:00Z"  # normalized
            assert row["embedding_vector_id"] == "vec-1"
        finally:
            await db.close()

    run(scenario())


def test_in_flight_placeholder_is_not_returned_as_latest():
    """Critical safety: a half-written placeholder must not become the
    baseline for the next scheduler tick."""

    async def scenario():
        db, conn = await _make_db()
        try:
            # An older real snapshot.
            await conn.execute(
                "INSERT INTO state_snapshots (created_at, content, status) VALUES (?, ?, 'done')",
                ("2026-05-01T00:00:00Z", "real baseline"),
            )
            await conn.commit()
            # A newer in_flight placeholder for a tick that is mid-flight.
            await db.insert_snapshot_placeholder(prompt_hash="hash-C")

            latest = await db.get_latest_snapshot()
            assert latest is not None
            assert latest.content == "real baseline"

            recent = await db.get_recent_snapshots(limit=5)
            assert all(s.status == "done" for s in recent)
            assert len(recent) == 1
        finally:
            await db.close()

    run(scenario())


def test_find_done_skips_in_flight_and_failed():
    async def scenario():
        db, conn = await _make_db()
        try:
            # Same prompt_hash, three rows: in_flight, failed, done.
            await conn.execute(
                "INSERT INTO state_snapshots (created_at, content, status, prompt_hash) VALUES (?, '', 'in_flight', ?)",
                ("2026-05-01T00:00:00Z", "hash-D"),
            )
            await conn.execute(
                "INSERT INTO state_snapshots (created_at, content, status, prompt_hash) VALUES (?, '', 'failed', ?)",
                ("2026-05-01T00:00:00Z", "hash-D"),
            )
            await conn.execute(
                "INSERT INTO state_snapshots (created_at, content, status, prompt_hash) VALUES (?, 'final result', 'done', ?)",
                ("2026-05-01T00:00:00Z", "hash-D"),
            )
            await conn.commit()

            done = await db.find_done_snapshot_by_prompt_hash("hash-D")
            assert done is not None
            assert done.content == "final result"

            in_flight = await db.find_in_flight_snapshot_by_prompt_hash("hash-D")
            assert in_flight is not None
            assert in_flight.status == "in_flight"

            # A different hash returns nothing.
            assert await db.find_done_snapshot_by_prompt_hash("hash-missing") is None
        finally:
            await db.close()

    run(scenario())


def test_count_failed_attempts_gates_dead_letter():
    async def scenario():
        db, conn = await _make_db()
        try:
            for _ in range(3):
                await conn.execute(
                    "INSERT INTO state_snapshots (created_at, content, status, prompt_hash) VALUES (?, '', 'failed', ?)",
                    ("2026-05-01T00:00:00Z", "hash-E"),
                )
            await conn.execute(
                "INSERT INTO state_snapshots (created_at, content, status, prompt_hash) VALUES (?, '', 'done', ?)",
                ("2026-05-01T00:00:00Z", "hash-E"),
            )
            await conn.commit()

            # Done rows are not counted as failures.
            assert await db.count_failed_snapshot_attempts("hash-E") == 3
            assert await db.count_failed_snapshot_attempts("hash-other") == 0
        finally:
            await db.close()

    run(scenario())


def test_reset_stale_in_flight_flips_old_rows_to_failed():
    async def scenario():
        db, conn = await _make_db()
        try:
            # Fresh placeholder (1s old) — must NOT be reset.
            await conn.execute(
                """INSERT INTO state_snapshots (created_at, status, prompt_hash, started_at)
                   VALUES (?, 'in_flight', ?, datetime('now', '-1 seconds'))""",
                ("2026-05-01T00:00:00Z", "hash-fresh"),
            )
            # Stale placeholder (1h old) — MUST be reset.
            await conn.execute(
                """INSERT INTO state_snapshots (created_at, status, prompt_hash, started_at)
                   VALUES (?, 'in_flight', ?, datetime('now', '-3600 seconds'))""",
                ("2026-05-01T00:00:00Z", "hash-stale"),
            )
            await conn.commit()

            reset_ids = await db.reset_stale_in_flight_snapshots(older_than_seconds=600)
            assert len(reset_ids) == 1

            async with conn.execute(
                "SELECT prompt_hash, status FROM state_snapshots ORDER BY id"
            ) as cur:
                rows = [(r[0], r[1]) for r in await cur.fetchall()]
            assert ("hash-fresh", "in_flight") in rows
            assert ("hash-stale", "failed") in rows

            # Idempotent: a second call with nothing stale returns [].
            assert await db.reset_stale_in_flight_snapshots(older_than_seconds=600) == []
        finally:
            await db.close()

    run(scenario())


def test_mark_failed_transitions_status():
    async def scenario():
        db, conn = await _make_db()
        try:
            row_id = await db.insert_snapshot_placeholder(prompt_hash="hash-F")
            await db.mark_snapshot_failed(row_id)
            async with conn.execute(
                "SELECT status FROM state_snapshots WHERE id=?", (row_id,)
            ) as cur:
                row = await cur.fetchone()
            assert row["status"] == "failed"
            # And it now counts as a failed attempt.
            assert await db.count_failed_snapshot_attempts("hash-F") == 1
        finally:
            await db.close()

    run(scenario())
