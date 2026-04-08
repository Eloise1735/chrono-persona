from __future__ import annotations

import os
import re
from pathlib import Path
from datetime import datetime

import aiosqlite

from server.models import StateSnapshot, EventAnchor, KeyRecord, WorldBook, format_utc_instant_z
from server.time_display import normalize_user_instant_to_utc_z

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS state_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'daily',
    content TEXT NOT NULL DEFAULT '',
    environment TEXT NOT NULL DEFAULT '{}',
    referenced_events TEXT NOT NULL DEFAULT '[]',
    embedding_vector_id TEXT
);

CREATE TABLE IF NOT EXISTS event_anchors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'generated',
    created_at TEXT NOT NULL,
    embedding_vector_id TEXT,
    trigger_keywords TEXT NOT NULL DEFAULT '[]',
    categories TEXT NOT NULL DEFAULT '[]',
    archived INTEGER NOT NULL DEFAULT 0,
    importance_score REAL,
    impression_depth REAL
);

CREATE TABLE IF NOT EXISTS system_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'system',
    description TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_recall_stats (
    entry_id TEXT PRIMARY KEY,
    recall_count INTEGER NOT NULL DEFAULT 0,
    last_recalled_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS key_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    content_text TEXT NOT NULL DEFAULT '',
    content_json TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    start_date TEXT,
    end_date TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    source TEXT NOT NULL DEFAULT 'manual',
    linked_event_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS world_books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    match_keywords TEXT NOT NULL DEFAULT '[]',
    is_active INTEGER NOT NULL DEFAULT 1,
    embedding_vector_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_vectors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL,
    source_id INTEGER NOT NULL,
    text_content TEXT NOT NULL DEFAULT '',
    vector_json TEXT NOT NULL DEFAULT '[]',
    vector_dim INTEGER NOT NULL DEFAULT 0,
    vector_model TEXT NOT NULL DEFAULT '',
    vector_provider TEXT NOT NULL DEFAULT 'local',
    status TEXT NOT NULL DEFAULT 'active',
    tier TEXT NOT NULL DEFAULT 'warm',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS automation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger TEXT NOT NULL,
    ran INTEGER NOT NULL DEFAULT 0,
    report_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self):
        os.makedirs(Path(self._db_path).parent, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_CREATE_TABLES)
        await self._ensure_schema_updates()
        await self._conn.commit()

    async def close(self):
        if self._conn:
            await self._conn.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database not initialized"
        return self._conn

    async def _ensure_schema_updates(self):
        await self._ensure_column("event_anchors", "archived", "INTEGER NOT NULL DEFAULT 0")
        await self._ensure_column("event_anchors", "importance_score", "REAL")
        await self._ensure_column("event_anchors", "impression_depth", "REAL")
        await self._ensure_column("event_anchors", "title", "TEXT NOT NULL DEFAULT ''")
        await self._ensure_column("event_anchors", "categories", "TEXT NOT NULL DEFAULT '[]'")
        await self._ensure_column("world_books", "embedding_vector_id", "TEXT")
        await self._ensure_column("state_snapshots", "inserted_at", "TEXT")
        await self.conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_recall_stats (
                entry_id TEXT PRIMARY KEY,
                recall_count INTEGER NOT NULL DEFAULT 0,
                last_recalled_at TEXT,
                created_at TEXT NOT NULL
            )"""
        )
        await self.conn.execute(
            """CREATE TABLE IF NOT EXISTS key_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                content_text TEXT NOT NULL DEFAULT '',
                content_json TEXT,
                tags TEXT NOT NULL DEFAULT '[]',
                start_date TEXT,
                end_date TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                source TEXT NOT NULL DEFAULT 'manual',
                linked_event_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        await self.conn.execute(
            """CREATE TABLE IF NOT EXISTS world_books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '[]',
                match_keywords TEXT NOT NULL DEFAULT '[]',
                is_active INTEGER NOT NULL DEFAULT 1,
                embedding_vector_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        await self.conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_vectors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_id TEXT NOT NULL UNIQUE,
                source_type TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                text_content TEXT NOT NULL DEFAULT '',
                vector_json TEXT NOT NULL DEFAULT '[]',
                vector_dim INTEGER NOT NULL DEFAULT 0,
                vector_model TEXT NOT NULL DEFAULT '',
                vector_provider TEXT NOT NULL DEFAULT 'local',
                status TEXT NOT NULL DEFAULT 'active',
                tier TEXT NOT NULL DEFAULT 'warm',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        await self.conn.execute(
            """CREATE TABLE IF NOT EXISTS automation_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger TEXT NOT NULL,
                ran INTEGER NOT NULL DEFAULT 0,
                report_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )"""
        )

    async def _ensure_column(self, table: str, column: str, definition: str):
        async with self.conn.execute(f"PRAGMA table_info({table})") as cur:
            rows = await cur.fetchall()
        existing = {row["name"] for row in rows}
        if column not in existing:
            await self.conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )

    # ── Snapshots ──

    async def insert_snapshot(self, snap: StateSnapshot) -> int:
        wall = format_utc_instant_z(datetime.utcnow())
        cursor = await self.conn.execute(
            """INSERT INTO state_snapshots
               (created_at, inserted_at, type, content, environment, referenced_events, embedding_vector_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                snap.created_at,
                wall,
                snap.type,
                snap.content,
                snap.environment,
                snap.referenced_events,
                snap.embedding_vector_id,
            ),
        )
        await self.conn.commit()
        return cursor.lastrowid  # type: ignore

    async def get_latest_snapshot(self) -> StateSnapshot | None:
        async with self.conn.execute(
            "SELECT * FROM state_snapshots ORDER BY created_at DESC, id DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
            return StateSnapshot(**dict(row)) if row else None

    async def get_latest_snapshot_by_type(self, snap_type: str) -> StateSnapshot | None:
        async with self.conn.execute(
            # 对话检查点语义优先「最新写入的一条记录」而非 created_at 最大值：
            # created_at 可能来自导入/回填的历史时间，不能稳定代表最近一次互动写入。
            "SELECT * FROM state_snapshots WHERE type = ? ORDER BY id DESC LIMIT 1",
            (snap_type,),
        ) as cur:
            row = await cur.fetchone()
            return StateSnapshot(**dict(row)) if row else None

    async def count_snapshots_since(self, since_timestamp: str) -> int:
        async with self.conn.execute(
            "SELECT COUNT(*) FROM state_snapshots WHERE created_at > ?",
            (since_timestamp,),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0] or 0)  # type: ignore

    async def get_recent_snapshots(self, limit: int = 7) -> list[StateSnapshot]:
        async with self.conn.execute(
            "SELECT * FROM state_snapshots ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
            return [StateSnapshot(**dict(r)) for r in rows]

    async def get_snapshots_in_range(self, start_date: str, end_date: str) -> list[StateSnapshot]:
        start_ts = f"{start_date}T00:00:00"
        end_ts = f"{end_date}T23:59:59"
        async with self.conn.execute(
            """SELECT * FROM state_snapshots
               WHERE created_at >= ? AND created_at <= ?
               ORDER BY created_at ASC, id ASC""",
            (start_ts, end_ts),
        ) as cur:
            rows = await cur.fetchall()
            return [StateSnapshot(**dict(r)) for r in rows]

    async def get_all_snapshots(self, offset: int = 0, limit: int = 50) -> list[StateSnapshot]:
        async with self.conn.execute(
            "SELECT * FROM state_snapshots ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ) as cur:
            rows = await cur.fetchall()
            return [StateSnapshot(**dict(r)) for r in rows]

    async def count_snapshots(self) -> int:
        async with self.conn.execute("SELECT COUNT(*) FROM state_snapshots") as cur:
            row = await cur.fetchone()
            return row[0]  # type: ignore

    async def get_snapshot_by_id(self, snap_id: int) -> StateSnapshot | None:
        async with self.conn.execute(
            "SELECT * FROM state_snapshots WHERE id = ?", (snap_id,)
        ) as cur:
            row = await cur.fetchone()
            return StateSnapshot(**dict(row)) if row else None

    async def update_snapshot(self, snap_id: int, **fields):
        if not fields:
            return
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [snap_id]
        await self.conn.execute(
            f"UPDATE state_snapshots SET {set_clause} WHERE id = ?",
            values,
        )
        await self.conn.commit()

    async def get_oldest_snapshots_beyond_limit(self, max_keep: int) -> list[StateSnapshot]:
        """Return snapshots that exceed the retention limit (oldest first)."""
        async with self.conn.execute(
            """SELECT * FROM state_snapshots
               WHERE id NOT IN (
                   SELECT id FROM state_snapshots ORDER BY created_at DESC, id DESC LIMIT ?
               )
               AND embedding_vector_id IS NULL
               ORDER BY id ASC""",
            (max_keep,),
        ) as cur:
            rows = await cur.fetchall()
            return [StateSnapshot(**dict(r)) for r in rows]

    async def mark_snapshot_vectorized(self, snap_id: int, vector_id: str):
        await self.conn.execute(
            "UPDATE state_snapshots SET embedding_vector_id = ? WHERE id = ?",
            (vector_id, snap_id),
        )
        await self.conn.commit()

    async def clear_snapshot_vectorized(self, snap_id: int):
        await self.conn.execute(
            "UPDATE state_snapshots SET embedding_vector_id = NULL WHERE id = ?",
            (snap_id,),
        )
        await self.conn.commit()

    async def repair_snapshot_timezones(self, *, dry_run: bool = False) -> dict:
        async with self.conn.execute(
            "SELECT id, created_at FROM state_snapshots ORDER BY id ASC"
        ) as cur:
            rows = await cur.fetchall()

        updates: list[tuple[str, int]] = []
        examples: list[dict[str, str | int]] = []
        errors: list[str] = []
        scanned = 0
        skipped = 0

        for row in rows:
            scanned += 1
            snap_id = int(row["id"])
            raw_created_at = str(row["created_at"] or "").strip()
            if not raw_created_at:
                skipped += 1
                continue
            try:
                normalized = normalize_user_instant_to_utc_z(raw_created_at)
            except ValueError as exc:
                errors.append(f"id={snap_id}: {exc}")
                continue
            if normalized == raw_created_at:
                skipped += 1
                continue
            updates.append((normalized, snap_id))
            if len(examples) < 20:
                examples.append(
                    {
                        "id": snap_id,
                        "from": raw_created_at,
                        "to": normalized,
                    }
                )

        if updates and not dry_run:
            await self.conn.executemany(
                "UPDATE state_snapshots SET created_at = ? WHERE id = ?",
                updates,
            )
            await self.conn.commit()

        return {
            "dry_run": dry_run,
            "scanned": scanned,
            "candidate_count": len(updates),
            "updated_count": 0 if dry_run else len(updates),
            "skipped_count": skipped,
            "error_count": len(errors),
            "examples": examples,
            "errors": errors,
        }

    async def get_snapshots_older_than_days_without_vector(
        self,
        days: int,
        limit: int = 200,
    ) -> list[StateSnapshot]:
        async with self.conn.execute(
            """SELECT * FROM state_snapshots
               WHERE datetime(created_at) <= datetime('now', ?)
                 AND embedding_vector_id IS NULL
               ORDER BY created_at ASC, id ASC
               LIMIT ?""",
            (f"-{max(1, days)} days", limit),
        ) as cur:
            rows = await cur.fetchall()
            return [StateSnapshot(**dict(r)) for r in rows]

    async def delete_snapshot(self, snap_id: int):
        await self.conn.execute("DELETE FROM state_snapshots WHERE id = ?", (snap_id,))
        await self.conn.commit()

    # ── Event Anchors ──

    async def insert_event(self, event: EventAnchor) -> int:
        cursor = await self.conn.execute(
            """INSERT INTO event_anchors
               (date, title, description, source, created_at, embedding_vector_id, trigger_keywords, categories, archived, importance_score, impression_depth)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event.date, getattr(event, "title", ""), event.description, event.source,
             event.created_at, event.embedding_vector_id, event.trigger_keywords, getattr(event, "categories", "[]"),
             getattr(event, "archived", 0),
             getattr(event, "importance_score", None),
             getattr(event, "impression_depth", None)),
        )
        await self.conn.commit()
        return cursor.lastrowid  # type: ignore

    async def get_events_in_range(
        self, start_date: str, end_date: str, include_archived: bool = False
    ) -> list[EventAnchor]:
        sql = """SELECT * FROM event_anchors
                 WHERE date >= ? AND date <= ?"""
        params: list = [start_date, end_date]
        if not include_archived:
            sql += " AND archived = 0"
        sql += " ORDER BY date ASC"
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [EventAnchor(**dict(r)) for r in rows]

    async def get_all_events(
        self,
        offset: int = 0,
        limit: int = 50,
        include_archived: bool = False,
        categories: list[str] | None = None,
    ) -> list[EventAnchor]:
        sql = "SELECT * FROM event_anchors WHERE 1=1"
        params: list = []
        if not include_archived:
            sql += " AND archived = 0"
        if categories:
            valid = [c for c in categories if c]
            if valid:
                clauses = []
                for c in valid:
                    clauses.append("categories LIKE ?")
                    params.append(f"%{c}%")
                sql += " AND (" + " OR ".join(clauses) + ")"
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [EventAnchor(**dict(r)) for r in rows]

    async def get_recent_events_by_event_time(
        self,
        limit: int = 50,
        include_archived: bool = False,
    ) -> list[EventAnchor]:
        sql = "SELECT * FROM event_anchors WHERE 1=1"
        params: list = []
        if not include_archived:
            sql += " AND archived = 0"
        sql += " ORDER BY date DESC, created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [EventAnchor(**dict(r)) for r in rows]

    async def get_event_by_id(self, event_id: int) -> EventAnchor | None:
        async with self.conn.execute(
            "SELECT * FROM event_anchors WHERE id = ?", (event_id,)
        ) as cur:
            row = await cur.fetchone()
            return EventAnchor(**dict(row)) if row else None

    async def get_event_by_date_title(
        self,
        date: str,
        title: str,
    ) -> EventAnchor | None:
        async with self.conn.execute(
            """SELECT * FROM event_anchors
               WHERE date = ? AND title = ?
               ORDER BY id DESC LIMIT 1""",
            (date, title),
        ) as cur:
            row = await cur.fetchone()
            return EventAnchor(**dict(row)) if row else None

    async def update_event(self, event_id: int, **fields):
        if not fields:
            return
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [event_id]
        await self.conn.execute(
            f"UPDATE event_anchors SET {set_clause} WHERE id = ?", values
        )
        await self.conn.commit()

    async def mark_event_vectorized(self, event_id: int, vector_id: str):
        await self.conn.execute(
            "UPDATE event_anchors SET embedding_vector_id = ? WHERE id = ?",
            (vector_id, event_id),
        )
        await self.conn.commit()

    async def clear_event_vectorized(self, event_id: int):
        await self.conn.execute(
            "UPDATE event_anchors SET embedding_vector_id = NULL WHERE id = ?",
            (event_id,),
        )
        await self.conn.commit()

    async def get_events_without_vector(
        self,
        limit: int = 200,
        include_archived: bool = True,
    ) -> list[EventAnchor]:
        sql = """SELECT * FROM event_anchors
                 WHERE embedding_vector_id IS NULL"""
        params: list = []
        if not include_archived:
            sql += " AND archived = 0"
        sql += " ORDER BY id ASC LIMIT ?"
        params.append(limit)
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [EventAnchor(**dict(r)) for r in rows]

    async def get_archived_events_without_vector(self, limit: int = 200) -> list[EventAnchor]:
        async with self.conn.execute(
            """SELECT * FROM event_anchors
               WHERE archived = 1
                 AND embedding_vector_id IS NULL
               ORDER BY id ASC
               LIMIT ?""",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
            return [EventAnchor(**dict(r)) for r in rows]

    async def delete_event(self, event_id: int):
        await self.conn.execute("DELETE FROM event_anchors WHERE id = ?", (event_id,))
        await self.conn.commit()

    async def search_events_by_keyword(
        self, keyword: str, limit: int = 10, include_archived: bool = False
    ) -> list[EventAnchor]:
        pattern = f"%{keyword}%"
        sql = """SELECT * FROM event_anchors
                 WHERE (title LIKE ? OR description LIKE ? OR trigger_keywords LIKE ?)"""
        params: list = [pattern, pattern, pattern]
        if not include_archived:
            sql += " AND archived = 0"
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [EventAnchor(**dict(r)) for r in rows]

    async def search_events_by_keywords(
        self, keywords: list[str], limit: int = 50, include_archived: bool = False
    ) -> list[EventAnchor]:
        """Search events matching ANY of the given keywords, returning a wide
        candidate set for downstream scoring."""
        if not keywords:
            return []
        conditions = []
        params: list[str] = []
        for kw in keywords:
            pattern = f"%{kw}%"
            conditions.append("(title LIKE ? OR description LIKE ? OR trigger_keywords LIKE ?)")
            params.extend([pattern, pattern, pattern])
        where = " OR ".join(conditions)
        sql = f"SELECT * FROM event_anchors WHERE ({where})"
        if not include_archived:
            sql += " AND archived = 0"
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        async with self.conn.execute(
            sql,
            params,
        ) as cur:
            rows = await cur.fetchall()
            return [EventAnchor(**dict(r)) for r in rows]

    async def search_snapshots_by_keyword(self, keyword: str, limit: int = 10) -> list[StateSnapshot]:
        pattern = f"%{keyword}%"
        async with self.conn.execute(
            """SELECT * FROM state_snapshots
               WHERE content LIKE ? AND embedding_vector_id IS NOT NULL
               ORDER BY created_at DESC, id DESC LIMIT ?""",
            (pattern, limit),
        ) as cur:
            rows = await cur.fetchall()
            return [StateSnapshot(**dict(r)) for r in rows]

    async def search_snapshots_by_keywords(
        self, keywords: list[str], limit: int = 50
    ) -> list[StateSnapshot]:
        """Search archived snapshots matching ANY keyword."""
        if not keywords:
            return []
        conditions = []
        params: list[str] = []
        for kw in keywords:
            pattern = f"%{kw}%"
            conditions.append("content LIKE ?")
            params.extend([pattern])
        where = " OR ".join(conditions)
        params.append(limit)
        async with self.conn.execute(
            f"""SELECT * FROM state_snapshots
                WHERE ({where}) AND embedding_vector_id IS NOT NULL
                ORDER BY created_at DESC, id DESC LIMIT ?""",
            params,
        ) as cur:
            rows = await cur.fetchall()
            return [StateSnapshot(**dict(r)) for r in rows]

    async def count_events_since(
        self, since_timestamp: str, include_archived: bool = False
    ) -> int:
        sql = "SELECT COUNT(*) FROM event_anchors WHERE created_at > ?"
        params: list = [since_timestamp]
        if not include_archived:
            sql += " AND archived = 0"
        async with self.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
            return row[0]  # type: ignore

    async def get_events_since(
        self, since_timestamp: str, limit: int = 200, include_archived: bool = False
    ) -> list[EventAnchor]:
        sql = "SELECT * FROM event_anchors WHERE created_at > ?"
        params: list = [since_timestamp]
        if not include_archived:
            sql += " AND archived = 0"
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [EventAnchor(**dict(r)) for r in rows]

    async def get_events_by_ids(self, event_ids: list[int]) -> list[EventAnchor]:
        if not event_ids:
            return []
        placeholders = ",".join(["?"] * len(event_ids))
        async with self.conn.execute(
            f"SELECT * FROM event_anchors WHERE id IN ({placeholders}) ORDER BY id DESC",
            event_ids,
        ) as cur:
            rows = await cur.fetchall()
            return [EventAnchor(**dict(r)) for r in rows]

    async def get_events_for_archive_recalc(
        self,
        start_id: int | None = None,
        end_id: int | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[EventAnchor]:
        sql = "SELECT * FROM event_anchors WHERE 1=1"
        params: list = []
        if start_id is not None:
            sql += " AND id >= ?"
            params.append(start_id)
        if end_id is not None:
            sql += " AND id <= ?"
            params.append(end_id)
        if start_date:
            sql += " AND date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND date <= ?"
            params.append(end_date)
        sql += " ORDER BY id ASC"
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [EventAnchor(**dict(r)) for r in rows]

    async def update_event_archived_flags(self, updates: list[tuple[int, int]]) -> int:
        if not updates:
            return 0
        cursor = await self.conn.executemany(
            "UPDATE event_anchors SET archived = ? WHERE id = ?",
            updates,
        )
        await self.conn.commit()
        return cursor.rowcount

    async def archive_events_by_ids(self, event_ids: list[int]) -> int:
        if not event_ids:
            return 0
        placeholders = ",".join(["?"] * len(event_ids))
        cursor = await self.conn.execute(
            f"UPDATE event_anchors SET archived = 1 WHERE id IN ({placeholders})",
            event_ids,
        )
        await self.conn.commit()
        return cursor.rowcount

    # ── Memory recall stats ──

    async def get_memory_recall_stats(self, entry_ids: list[str]) -> dict[str, dict]:
        if not entry_ids:
            return {}
        placeholders = ",".join(["?"] * len(entry_ids))
        async with self.conn.execute(
            f"""SELECT entry_id, recall_count, last_recalled_at
                FROM memory_recall_stats
                WHERE entry_id IN ({placeholders})""",
            entry_ids,
        ) as cur:
            rows = await cur.fetchall()
            return {
                str(r["entry_id"]): {
                    "recall_count": int(r["recall_count"] or 0),
                    "last_recalled_at": r["last_recalled_at"],
                }
                for r in rows
            }

    async def record_memory_recalls(self, entry_ids: list[str]):
        if not entry_ids:
            return
        now = datetime.utcnow().isoformat()
        # Keep order while deduplicating
        unique_ids = list(dict.fromkeys(entry_ids))
        await self.conn.executemany(
            """INSERT INTO memory_recall_stats (entry_id, recall_count, last_recalled_at, created_at)
               VALUES (?, 1, ?, ?)
               ON CONFLICT(entry_id) DO UPDATE SET
                 recall_count = recall_count + 1,
                 last_recalled_at = excluded.last_recalled_at""",
            [(entry_id, now, now) for entry_id in unique_ids],
        )
        await self.conn.commit()

    # ── Key Records ──

    async def insert_key_record(self, record: KeyRecord) -> int:
        cursor = await self.conn.execute(
            """INSERT INTO key_records
               (type, title, content_text, content_json, tags, start_date, end_date, status, source, linked_event_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.type,
                record.title,
                record.content_text,
                record.content_json,
                record.tags,
                record.start_date,
                record.end_date,
                record.status,
                record.source,
                record.linked_event_id,
                record.created_at,
                record.updated_at,
            ),
        )
        await self.conn.commit()
        return cursor.lastrowid  # type: ignore

    async def get_key_record_by_id(self, record_id: int) -> KeyRecord | None:
        async with self.conn.execute(
            "SELECT * FROM key_records WHERE id = ?",
            (record_id,),
        ) as cur:
            row = await cur.fetchone()
            return KeyRecord(**dict(row)) if row else None

    async def get_key_record_by_type_title(
        self,
        record_type: str,
        title: str,
    ) -> KeyRecord | None:
        async with self.conn.execute(
            """SELECT * FROM key_records
               WHERE type = ? AND title = ?
               ORDER BY id DESC LIMIT 1""",
            (record_type, title),
        ) as cur:
            row = await cur.fetchone()
            return KeyRecord(**dict(row)) if row else None

    async def get_all_key_records(
        self,
        offset: int = 0,
        limit: int = 50,
        record_type: str | None = None,
        status: str | None = None,
        include_archived: bool = False,
    ) -> list[KeyRecord]:
        sql = "SELECT * FROM key_records WHERE 1=1"
        params: list = []
        if record_type:
            sql += " AND type = ?"
            params.append(record_type)
        if status:
            sql += " AND status = ?"
            params.append(status)
        elif not include_archived:
            sql += " AND status != 'archived'"
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [KeyRecord(**dict(r)) for r in rows]

    async def search_key_records(
        self,
        query: str,
        top_k: int = 10,
        record_type: str | None = None,
        include_archived: bool = False,
    ) -> list[KeyRecord]:
        raw_query = (query or "").strip()
        if not raw_query:
            return []
        keywords = [k.strip() for k in re.split(r"[\s,，。;；、|/]+", raw_query) if k.strip()]
        if not keywords:
            keywords = [raw_query]
        conditions = []
        params: list = []
        for kw in keywords:
            pattern = f"%{kw}%"
            conditions.append("(title LIKE ? OR content_text LIKE ? OR tags LIKE ? OR content_json LIKE ?)")
            params.extend([pattern, pattern, pattern, pattern])
        where = " OR ".join(conditions)
        sql = f"""SELECT * FROM key_records
                 WHERE ({where})"""
        if record_type:
            sql += " AND type = ?"
            params.append(record_type)
        if not include_archived:
            sql += " AND status != 'archived'"
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(top_k)
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [KeyRecord(**dict(r)) for r in rows]

    async def update_key_record(self, record_id: int, **fields):
        if not fields:
            return
        fields["updated_at"] = datetime.utcnow().isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [record_id]
        await self.conn.execute(
            f"UPDATE key_records SET {set_clause} WHERE id = ?",
            values,
        )
        await self.conn.commit()

    async def delete_key_record(self, record_id: int):
        await self.conn.execute(
            "DELETE FROM key_records WHERE id = ?",
            (record_id,),
        )
        await self.conn.commit()

    # ── World Books ──

    async def insert_world_book(self, item: WorldBook) -> int:
        cursor = await self.conn.execute(
            """INSERT INTO world_books
               (name, content, tags, match_keywords, is_active, embedding_vector_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item.name,
                item.content,
                item.tags,
                item.match_keywords,
                item.is_active,
                item.embedding_vector_id,
                item.created_at,
                item.updated_at,
            ),
        )
        await self.conn.commit()
        return cursor.lastrowid  # type: ignore

    async def get_world_book_by_id(self, item_id: int) -> WorldBook | None:
        async with self.conn.execute(
            "SELECT * FROM world_books WHERE id = ?",
            (item_id,),
        ) as cur:
            row = await cur.fetchone()
            return WorldBook(**dict(row)) if row else None

    async def list_world_books(self, offset: int = 0, limit: int = 100) -> list[WorldBook]:
        async with self.conn.execute(
            "SELECT * FROM world_books ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ) as cur:
            rows = await cur.fetchall()
            return [WorldBook(**dict(r)) for r in rows]

    async def update_world_book(self, item_id: int, **fields):
        if not fields:
            return
        fields["updated_at"] = datetime.utcnow().isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [item_id]
        await self.conn.execute(
            f"UPDATE world_books SET {set_clause} WHERE id = ?",
            values,
        )
        await self.conn.commit()

    async def delete_world_book(self, item_id: int):
        await self.conn.execute(
            "DELETE FROM world_books WHERE id = ?",
            (item_id,),
        )
        await self.conn.commit()

    async def get_active_world_books(self) -> list[WorldBook]:
        async with self.conn.execute(
            "SELECT * FROM world_books WHERE is_active = 1 ORDER BY updated_at DESC, id DESC",
        ) as cur:
            rows = await cur.fetchall()
            return [WorldBook(**dict(r)) for r in rows]

    async def get_world_books_by_ids(self, item_ids: list[int]) -> list[WorldBook]:
        if not item_ids:
            return []
        placeholders = ",".join(["?"] * len(item_ids))
        async with self.conn.execute(
            f"SELECT * FROM world_books WHERE id IN ({placeholders}) ORDER BY id DESC",
            item_ids,
        ) as cur:
            rows = await cur.fetchall()
            return [WorldBook(**dict(r)) for r in rows]

    async def mark_world_book_vectorized(self, item_id: int, vector_id: str):
        await self.conn.execute(
            "UPDATE world_books SET embedding_vector_id = ?, updated_at = ? WHERE id = ?",
            (vector_id, datetime.utcnow().isoformat(), item_id),
        )
        await self.conn.commit()

    async def clear_world_book_vectorized(self, item_id: int):
        await self.conn.execute(
            "UPDATE world_books SET embedding_vector_id = NULL, updated_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), item_id),
        )
        await self.conn.commit()

    # ── System Settings ──

    async def get_setting(self, key: str) -> dict | None:
        async with self.conn.execute(
            "SELECT * FROM system_settings WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_all_settings(self) -> list[dict]:
        async with self.conn.execute(
            "SELECT * FROM system_settings ORDER BY category ASC, key ASC"
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def get_settings_by_category(self, category: str) -> list[dict]:
        async with self.conn.execute(
            "SELECT * FROM system_settings WHERE category = ? ORDER BY key ASC",
            (category,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def set_setting(
        self, key: str, value: str, category: str = "system", description: str = ""
    ):
        now = datetime.utcnow().isoformat()
        await self.conn.execute(
            """INSERT INTO system_settings (key, value, category, description, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 value = excluded.value,
                 category = excluded.category,
                 description = excluded.description,
                 updated_at = excluded.updated_at""",
            (key, value, category, description, now),
        )
        await self.conn.commit()

    async def initialize_default_settings(self, defaults: dict[str, dict[str, str]]):
        now = datetime.utcnow().isoformat()
        for key, data in defaults.items():
            row = await self.get_setting(key)
            if row is not None:
                continue
            await self.conn.execute(
                """INSERT INTO system_settings (key, value, category, description, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    key,
                    data.get("value", ""),
                    data.get("category", "system"),
                    data.get("description", ""),
                    now,
                ),
            )
        await self.conn.commit()

    # ── Memory Vectors ──

    async def upsert_memory_vector(
        self,
        *,
        entry_id: str,
        source_type: str,
        source_id: int,
        text_content: str,
        vector_json: str,
        vector_dim: int,
        vector_model: str,
        vector_provider: str,
        tier: str = "warm",
        status: str = "active",
    ):
        now = datetime.utcnow().isoformat()
        await self.conn.execute(
            """INSERT INTO memory_vectors
               (entry_id, source_type, source_id, text_content, vector_json, vector_dim, vector_model, vector_provider, status, tier, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(entry_id) DO UPDATE SET
                 source_type = excluded.source_type,
                 source_id = excluded.source_id,
                 text_content = excluded.text_content,
                 vector_json = excluded.vector_json,
                 vector_dim = excluded.vector_dim,
                 vector_model = excluded.vector_model,
                 vector_provider = excluded.vector_provider,
                 status = excluded.status,
                 tier = excluded.tier,
                 updated_at = excluded.updated_at""",
            (
                entry_id,
                source_type,
                source_id,
                text_content,
                vector_json,
                vector_dim,
                vector_model,
                vector_provider,
                status,
                tier,
                now,
                now,
            ),
        )
        await self.conn.commit()

    async def get_memory_vector(self, entry_id: str) -> dict | None:
        async with self.conn.execute(
            "SELECT * FROM memory_vectors WHERE entry_id = ?",
            (entry_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def list_memory_vectors(
        self,
        offset: int = 0,
        limit: int = 50,
        source_type: str | None = None,
        status: str | None = None,
        tier: str | None = None,
    ) -> list[dict]:
        sql = "SELECT * FROM memory_vectors WHERE 1=1"
        params: list = []
        if source_type:
            sql += " AND source_type = ?"
            params.append(source_type)
        if status:
            sql += " AND status = ?"
            params.append(status)
        if tier:
            sql += " AND tier = ?"
            params.append(tier)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def get_active_memory_vectors(self, limit: int = 5000) -> list[dict]:
        async with self.conn.execute(
            """SELECT * FROM memory_vectors
               WHERE status = 'active'
               ORDER BY updated_at DESC
               LIMIT ?""",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def get_active_memory_vectors_older_than_days(
        self,
        days: int,
        limit: int = 1000,
    ) -> list[dict]:
        async with self.conn.execute(
            """SELECT * FROM memory_vectors
               WHERE status = 'active'
                 AND datetime(updated_at) <= datetime('now', ?)
               ORDER BY updated_at ASC, id ASC
               LIMIT ?""",
            (f"-{max(1, days)} days", limit),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def mark_memory_vector_deleted(self, entry_id: str):
        now = datetime.utcnow().isoformat()
        await self.conn.execute(
            "UPDATE memory_vectors SET status = 'deleted', updated_at = ? WHERE entry_id = ?",
            (now, entry_id),
        )
        await self.conn.commit()

    async def mark_memory_vectors_deleted(self, entry_ids: list[str]) -> int:
        if not entry_ids:
            return 0
        now = datetime.utcnow().isoformat()
        placeholders = ",".join(["?"] * len(entry_ids))
        params: list = [now]
        params.extend(entry_ids)
        cursor = await self.conn.execute(
            f"""UPDATE memory_vectors
                SET status = 'deleted', updated_at = ?
                WHERE entry_id IN ({placeholders})""",
            params,
        )
        await self.conn.commit()
        return cursor.rowcount

    async def delete_memory_vector(self, entry_id: str):
        await self.conn.execute(
            "DELETE FROM memory_vectors WHERE entry_id = ?",
            (entry_id,),
        )
        await self.conn.commit()

    async def count_memory_vectors(self, status: str | None = None) -> int:
        if status:
            async with self.conn.execute(
                "SELECT COUNT(*) FROM memory_vectors WHERE status = ?",
                (status,),
            ) as cur:
                row = await cur.fetchone()
                return row[0]  # type: ignore
        async with self.conn.execute("SELECT COUNT(*) FROM memory_vectors") as cur:
            row = await cur.fetchone()
            return row[0]  # type: ignore

    async def count_memory_vectors_by_source(self) -> dict[str, int]:
        async with self.conn.execute(
            """SELECT source_type, COUNT(*) as cnt
               FROM memory_vectors
               WHERE status = 'active'
               GROUP BY source_type"""
        ) as cur:
            rows = await cur.fetchall()
            result: dict[str, int] = {}
            for row in rows:
                result[str(row["source_type"])] = int(row["cnt"] or 0)
            return result

    # ── Automation Runs ──

    async def insert_automation_run(self, trigger: str, ran: bool, report_json: str) -> int:
        cursor = await self.conn.execute(
            """INSERT INTO automation_runs (trigger, ran, report_json, created_at)
               VALUES (?, ?, ?, ?)""",
            (trigger, 1 if ran else 0, report_json, datetime.utcnow().isoformat()),
        )
        await self.conn.commit()
        return cursor.lastrowid  # type: ignore

    async def get_latest_automation_run(self) -> dict | None:
        async with self.conn.execute(
            "SELECT * FROM automation_runs ORDER BY id DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_automation_runs(
        self,
        offset: int = 0,
        limit: int = 20,
    ) -> list[dict]:
        async with self.conn.execute(
            "SELECT * FROM automation_runs ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def get_automation_runs_since(self, since_iso: str) -> list[dict]:
        async with self.conn.execute(
            """SELECT * FROM automation_runs
               WHERE created_at >= ?
               ORDER BY id DESC""",
            (since_iso,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
