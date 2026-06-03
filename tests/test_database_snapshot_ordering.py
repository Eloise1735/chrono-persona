from __future__ import annotations

import asyncio
import json
import subprocess
import sys

import aiosqlite

from server.database import Database
from server.models import StateSnapshot


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


def test_insert_snapshot_normalizes_created_at_to_utc_z():
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
            snap_id = await db.insert_snapshot(
                StateSnapshot(
                    created_at="2026-05-26T10:00:00+08:00",
                    content="local timestamp",
                )
            )
            async with conn.execute("SELECT created_at FROM state_snapshots WHERE id = ?", (snap_id,)) as cur:
                row = await cur.fetchone()
            assert row["created_at"] == "2026-05-26T02:00:00Z"
        finally:
            await db.close()

    run(scenario())


def test_snapshot_keyword_search_uses_actual_instant_order_for_mixed_timezones():
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
                """INSERT INTO state_snapshots
                   (created_at, content, embedding_vector_id) VALUES (?, ?, ?)""",
                ("2026-05-26T10:00:00+08:00", "needle older", "v1"),
            )
            newer = await conn.execute(
                """INSERT INTO state_snapshots
                   (created_at, content, embedding_vector_id) VALUES (?, ?, ?)""",
                ("2026-05-26T06:00:00Z", "needle newer", "v2"),
            )
            await conn.commit()

            results = await db.search_snapshots_by_keyword("needle", limit=2)
            assert [snap.id for snap in results] == [newer.lastrowid, older.lastrowid]
        finally:
            await db.close()

    run(scenario())


def test_normalize_snapshot_created_at_script_is_idempotent(tmp_path):
    async def setup_db(db_path):
        db = Database(str(db_path))
        await db.initialize()
        try:
            await db.conn.execute(
                "INSERT INTO state_snapshots (created_at, content) VALUES (?, ?)",
                ("2026-05-26T10:00:00+08:00", "needs normalize"),
            )
            await db.conn.execute(
                "INSERT INTO state_snapshots (created_at, content) VALUES (?, ?)",
                ("2026-05-26T06:00:00Z", "already normalized"),
            )
            await db.conn.commit()
        finally:
            await db.close()

    async def read_created(db_path):
        conn = await aiosqlite.connect(str(db_path))
        try:
            async with conn.execute("SELECT created_at FROM state_snapshots ORDER BY id ASC") as cur:
                rows = await cur.fetchall()
            return [row[0] for row in rows]
        finally:
            await conn.close()

    db_path = tmp_path / "sample.db"
    run(setup_db(db_path))

    first = subprocess.run(
        [sys.executable, "migrate/normalize_snapshot_created_at.py", "--db", str(db_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    first_report = json.loads(first.stdout)
    assert first_report["updated_count"] == 1
    assert run(read_created(db_path)) == ["2026-05-26T02:00:00Z", "2026-05-26T06:00:00Z"]

    second = subprocess.run(
        [sys.executable, "migrate/normalize_snapshot_created_at.py", "--db", str(db_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    second_report = json.loads(second.stdout)
    assert second_report["updated_count"] == 0
