from __future__ import annotations

import asyncio

import aiosqlite

from server.database import Database


def run(coro):
    return asyncio.run(coro)


def test_snapshot_ordering_uses_actual_instant_for_mixed_timezone_strings():
    async def scenario():
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        db = Database(":memory:")
        db._conn = conn
        try:
            await conn.execute(
                """CREATE TABLE state_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    inserted_at TEXT,
                    type TEXT NOT NULL DEFAULT 'daily',
                    content TEXT NOT NULL DEFAULT '',
                    environment TEXT NOT NULL DEFAULT '{}',
                    referenced_events TEXT NOT NULL DEFAULT '[]',
                    embedding_vector_id TEXT
                )"""
            )
            older = await conn.execute(
                "INSERT INTO state_snapshots (created_at, content) VALUES (?, ?)",
                ("2026-05-26T10:00:00+08:00", "older local"),
            )
            older_id = older.lastrowid
            newer = await conn.execute(
                "INSERT INTO state_snapshots (created_at, content) VALUES (?, ?)",
                ("2026-05-26T06:00:00Z", "newer utc"),
            )
            newer_id = newer.lastrowid
            await conn.execute(
                "INSERT INTO state_snapshots (created_at, content) VALUES (?, ?)",
                ("2026-05-26T01:00:00Z", "oldest utc"),
            )
            await conn.commit()

            latest = await db.get_latest_snapshot()
            recent = await db.get_recent_snapshots(limit=2)
            overflow = await db.get_oldest_snapshots_beyond_limit(max_keep=1)

            assert latest is not None
            assert latest.id == newer_id
            assert [snap.id for snap in recent] == [newer_id, older_id]
            assert newer_id not in {snap.id for snap in overflow}
        finally:
            await db.close()

    run(scenario())
