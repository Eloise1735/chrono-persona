from __future__ import annotations

import os
import re
from pathlib import Path
from datetime import datetime

import aiosqlite

from server.models import (
    CharacterNotification,
    ConversationTimeClaim,
    DailyPlan,
    DisturbancePulse,
    EventAnchor,
    KeyRecord,
    LifeFlowTrace,
    NPCEntity,
    PlanItem,
    RelationshipThought,
    RelationshipState,
    SlowLine,
    StateSnapshot,
    WorldBook,
    format_utc_instant_z,
)
from server.time_display import normalize_user_instant_to_utc_z

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS state_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'daily',
    content TEXT NOT NULL DEFAULT '',
    environment TEXT NOT NULL DEFAULT '{}',
    referenced_events TEXT NOT NULL DEFAULT '[]',
    embedding_vector_id TEXT,
    status TEXT NOT NULL DEFAULT 'done',
    prompt_hash TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT
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
    meta_json TEXT,
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
    match_keywords TEXT NOT NULL DEFAULT '[]',
    start_date TEXT,
    end_date TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    source TEXT NOT NULL DEFAULT 'manual',
    life_scope TEXT NOT NULL DEFAULT 'user_life',
    linked_event_id INTEGER,
    embedding_vector_id TEXT,
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

CREATE TABLE IF NOT EXISTS daily_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_date TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    raw_plan TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    replan_trigger TEXT,
    replan_parent_id INTEGER,
    context_snapshot TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL,
    hour_start INTEGER NOT NULL,
    hour_end INTEGER NOT NULL,
    activity TEXT NOT NULL DEFAULT '',
    action_type TEXT NOT NULL DEFAULT 'internal',
    action_payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    outcome TEXT NOT NULL DEFAULT '',
    outcome_event_id INTEGER,
    source_kind TEXT NOT NULL DEFAULT 'generated',
    source_ref_id INTEGER,
    created_at TEXT NOT NULL,
    executed_at TEXT
);

CREATE TABLE IF NOT EXISTS life_flow_traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_date TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'environment',
    summary TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    schedule_alignment TEXT NOT NULL DEFAULT 'on_track',
    related_snapshot_id INTEGER,
    related_event_ids TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS disturbance_pulses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occur_at TEXT NOT NULL,
    reveal_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    channel_type TEXT NOT NULL DEFAULT 'endogenous_reveal',
    source_family TEXT NOT NULL DEFAULT 'task',
    seed_kind TEXT NOT NULL DEFAULT 'event',
    seed_ref_id INTEGER,
    blind_spot_reason TEXT NOT NULL DEFAULT '',
    reveal_channel TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    factual_payload_json TEXT NOT NULL DEFAULT '{}',
    impact_hint TEXT NOT NULL DEFAULT '',
    salience REAL NOT NULL DEFAULT 0.5,
    novelty_score REAL NOT NULL DEFAULT 0.5,
    cooldown_until TEXT,
    fingerprint TEXT NOT NULL DEFAULT '',
    linked_snapshot_id INTEGER,
    linked_event_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_time_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL DEFAULT 'active',
    started_at TEXT NOT NULL,
    ended_at TEXT,
    source TEXT NOT NULL DEFAULT 'get_current_state',
    context_summary TEXT NOT NULL DEFAULT '',
    latest_snapshot_id INTEGER,
    closing_snapshot_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS relationship_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    last_meaningful_contact_at TEXT,
    hours_since_meaningful_contact REAL NOT NULL DEFAULT 0,
    days_since_meaningful_contact INTEGER NOT NULL DEFAULT 0,
    contact_recency_bucket TEXT NOT NULL DEFAULT 'active',
    connection_need REAL NOT NULL DEFAULT 0.5,
    pride_or_distance REAL NOT NULL DEFAULT 0.5,
    valence REAL NOT NULL DEFAULT 0.5,
    arousal REAL NOT NULL DEFAULT 0.5,
    life_immersion REAL NOT NULL DEFAULT 0.5,
    relationship_feeling_summary TEXT NOT NULL DEFAULT '',
    space_need_level REAL NOT NULL DEFAULT 0.5,
    concern_level REAL NOT NULL DEFAULT 0.5,
    proactive_topics TEXT NOT NULL DEFAULT '[]',
    plan_bias_hint TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS relationship_thoughts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thought_date TEXT NOT NULL,
    source_snapshot_id INTEGER,
    source_env_id TEXT,
    topic_line TEXT NOT NULL DEFAULT '',
    thought_type TEXT NOT NULL DEFAULT 'reconsider',
    content TEXT NOT NULL DEFAULT '',
    salience REAL NOT NULL DEFAULT 0.5,
    dedupe_fingerprint TEXT NOT NULL DEFAULT '',
    resolution_status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS slowlines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_key TEXT NOT NULL DEFAULT '',
    theme TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT 'shared',
    source_family TEXT NOT NULL DEFAULT 'autonomous',
    memory_role TEXT NOT NULL DEFAULT 'active_thread_detail',
    progress_status TEXT NOT NULL DEFAULT 'open',
    tension_level TEXT NOT NULL DEFAULT 'medium',
    unresolved_level TEXT NOT NULL DEFAULT 'medium',
    preload_priority REAL NOT NULL DEFAULT 0.5,
    stage_summary TEXT NOT NULL DEFAULT '',
    trajectory_summary TEXT NOT NULL DEFAULT '',
    current_tension TEXT NOT NULL DEFAULT '',
    recent_shift_summary TEXT NOT NULL DEFAULT '',
    recent_movement_summary TEXT NOT NULL DEFAULT '',
    last_meaningful_shift_at TEXT,
    emotional_tension TEXT NOT NULL DEFAULT 'stable',
    affective_direction TEXT NOT NULL DEFAULT 'endurance',
    open_questions TEXT NOT NULL DEFAULT '[]',
    salience REAL NOT NULL DEFAULT 0.5,
    last_touched_at TEXT,
    linked_key_record_ids TEXT NOT NULL DEFAULT '[]',
    linked_event_ids TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS npc_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    background TEXT NOT NULL DEFAULT '',
    relationship_to_character TEXT NOT NULL DEFAULT '',
    personality_traits TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active',
    spawn_source TEXT NOT NULL DEFAULT 'manual',
    spawn_context TEXT NOT NULL DEFAULT '',
    last_interaction_at TEXT,
    interaction_count INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS character_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_type TEXT NOT NULL,
    trigger_item_id INTEGER,
    message_text TEXT NOT NULL DEFAULT '',
    tone TEXT NOT NULL DEFAULT 'neutral',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    expires_at TEXT
);
"""


class Database:
    SNAPSHOT_ORDER_DESC = "julianday(created_at) DESC, created_at DESC, id DESC"
    SNAPSHOT_ORDER_ASC = "julianday(created_at) ASC, created_at ASC, id ASC"

    def __init__(self, db_path: str):
        self._db_path = str(Path(db_path).expanduser().resolve())
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
        await self._ensure_column("event_anchors", "meta_json", "TEXT")
        await self._ensure_column("key_records", "match_keywords", "TEXT NOT NULL DEFAULT '[]'")
        await self._ensure_column("key_records", "embedding_vector_id", "TEXT")
        await self._ensure_column("key_records", "life_scope", "TEXT NOT NULL DEFAULT 'user_life'")
        await self._ensure_column("world_books", "embedding_vector_id", "TEXT")
        await self._ensure_column("state_snapshots", "inserted_at", "TEXT")
        # B2 in-flight idempotency
        await self._ensure_column("state_snapshots", "status", "TEXT NOT NULL DEFAULT 'done'")
        await self._ensure_column("state_snapshots", "prompt_hash", "TEXT")
        await self._ensure_column("state_snapshots", "attempt_count", "INTEGER NOT NULL DEFAULT 0")
        await self._ensure_column("state_snapshots", "started_at", "TEXT")
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_state_snapshots_status ON state_snapshots(status)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_state_snapshots_prompt_hash ON state_snapshots(prompt_hash)"
        )
        await self._ensure_column("plan_items", "source_kind", "TEXT NOT NULL DEFAULT 'generated'")
        await self._ensure_column("plan_items", "source_ref_id", "INTEGER")
        await self._ensure_column("relationship_states", "hours_since_meaningful_contact", "REAL NOT NULL DEFAULT 0")
        await self._ensure_column("slowlines", "thread_key", "TEXT NOT NULL DEFAULT ''")
        await self._ensure_column("slowlines", "scope", "TEXT NOT NULL DEFAULT 'shared'")
        await self._ensure_column("slowlines", "memory_role", "TEXT NOT NULL DEFAULT 'active_thread_detail'")
        await self._ensure_column("slowlines", "progress_status", "TEXT NOT NULL DEFAULT 'open'")
        await self._ensure_column("slowlines", "tension_level", "TEXT NOT NULL DEFAULT 'medium'")
        await self._ensure_column("slowlines", "unresolved_level", "TEXT NOT NULL DEFAULT 'medium'")
        await self._ensure_column("slowlines", "preload_priority", "REAL NOT NULL DEFAULT 0.5")
        await self._ensure_column("slowlines", "trajectory_summary", "TEXT NOT NULL DEFAULT ''")
        await self._ensure_column("slowlines", "recent_shift_summary", "TEXT NOT NULL DEFAULT ''")
        await self._ensure_column("slowlines", "last_meaningful_shift_at", "TEXT")
        await self._ensure_column("slowlines", "emotional_tension", "TEXT NOT NULL DEFAULT 'stable'")
        await self._ensure_column("slowlines", "affective_direction", "TEXT NOT NULL DEFAULT 'endurance'")
        await self.conn.execute(
            "UPDATE slowlines SET source_family = 'relationship' WHERE source_family = 'conversation'"
        )
        await self.conn.execute(
            "UPDATE slowlines SET source_family = 'daily_life' WHERE source_family IN ('autonomous', 'mixed', '')"
        )
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
                match_keywords TEXT NOT NULL DEFAULT '[]',
                start_date TEXT,
                end_date TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                source TEXT NOT NULL DEFAULT 'manual',
                life_scope TEXT NOT NULL DEFAULT 'user_life',
                linked_event_id INTEGER,
                embedding_vector_id TEXT,
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
        await self.conn.execute(
            """CREATE TABLE IF NOT EXISTS daily_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_date TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                raw_plan TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                replan_trigger TEXT,
                replan_parent_id INTEGER,
                context_snapshot TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )"""
        )
        await self.conn.execute(
            """CREATE TABLE IF NOT EXISTS plan_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                hour_start INTEGER NOT NULL,
                hour_end INTEGER NOT NULL,
                activity TEXT NOT NULL DEFAULT '',
                action_type TEXT NOT NULL DEFAULT 'internal',
                action_payload TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                outcome TEXT NOT NULL DEFAULT '',
                outcome_event_id INTEGER,
                source_kind TEXT NOT NULL DEFAULT 'generated',
                source_ref_id INTEGER,
                created_at TEXT NOT NULL,
                executed_at TEXT
            )"""
        )
        await self.conn.execute(
            "UPDATE plan_items SET action_type = 'internal' WHERE action_type = 'message_user'"
        )
        await self.conn.execute(
            """CREATE TABLE IF NOT EXISTS life_flow_traces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_date TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'environment',
                summary TEXT NOT NULL DEFAULT '',
                details_json TEXT NOT NULL DEFAULT '{}',
                schedule_alignment TEXT NOT NULL DEFAULT 'on_track',
                related_snapshot_id INTEGER,
                related_event_ids TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        await self.conn.execute(
            """CREATE TABLE IF NOT EXISTS disturbance_pulses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                occur_at TEXT NOT NULL,
                reveal_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                channel_type TEXT NOT NULL DEFAULT 'endogenous_reveal',
                source_family TEXT NOT NULL DEFAULT 'task',
                seed_kind TEXT NOT NULL DEFAULT 'event',
                seed_ref_id INTEGER,
                blind_spot_reason TEXT NOT NULL DEFAULT '',
                reveal_channel TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                factual_payload_json TEXT NOT NULL DEFAULT '{}',
                impact_hint TEXT NOT NULL DEFAULT '',
                salience REAL NOT NULL DEFAULT 0.5,
                novelty_score REAL NOT NULL DEFAULT 0.5,
                cooldown_until TEXT,
                fingerprint TEXT NOT NULL DEFAULT '',
                linked_snapshot_id INTEGER,
                linked_event_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        await self.conn.execute(
            """CREATE TABLE IF NOT EXISTS conversation_time_claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL DEFAULT 'active',
                started_at TEXT NOT NULL,
                ended_at TEXT,
                source TEXT NOT NULL DEFAULT 'get_current_state',
                context_summary TEXT NOT NULL DEFAULT '',
                latest_snapshot_id INTEGER,
                closing_snapshot_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        await self.conn.execute(
            """CREATE TABLE IF NOT EXISTS relationship_states (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                last_meaningful_contact_at TEXT,
                hours_since_meaningful_contact REAL NOT NULL DEFAULT 0,
                days_since_meaningful_contact INTEGER NOT NULL DEFAULT 0,
                contact_recency_bucket TEXT NOT NULL DEFAULT 'active',
                connection_need REAL NOT NULL DEFAULT 0.5,
                pride_or_distance REAL NOT NULL DEFAULT 0.5,
                valence REAL NOT NULL DEFAULT 0.5,
                arousal REAL NOT NULL DEFAULT 0.5,
                life_immersion REAL NOT NULL DEFAULT 0.5,
                relationship_feeling_summary TEXT NOT NULL DEFAULT '',
                space_need_level REAL NOT NULL DEFAULT 0.5,
                concern_level REAL NOT NULL DEFAULT 0.5,
                proactive_topics TEXT NOT NULL DEFAULT '[]',
                plan_bias_hint TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        await self.conn.execute(
            """CREATE TABLE IF NOT EXISTS relationship_thoughts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thought_date TEXT NOT NULL,
                source_snapshot_id INTEGER,
                source_env_id TEXT,
                topic_line TEXT NOT NULL DEFAULT '',
                thought_type TEXT NOT NULL DEFAULT 'reconsider',
                content TEXT NOT NULL DEFAULT '',
                salience REAL NOT NULL DEFAULT 0.5,
                dedupe_fingerprint TEXT NOT NULL DEFAULT '',
                resolution_status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        await self.conn.execute(
            """CREATE TABLE IF NOT EXISTS slowlines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_key TEXT NOT NULL DEFAULT '',
                theme TEXT NOT NULL DEFAULT '',
                scope TEXT NOT NULL DEFAULT 'shared',
                source_family TEXT NOT NULL DEFAULT 'autonomous',
                memory_role TEXT NOT NULL DEFAULT 'active_thread_detail',
                progress_status TEXT NOT NULL DEFAULT 'open',
                tension_level TEXT NOT NULL DEFAULT 'medium',
                unresolved_level TEXT NOT NULL DEFAULT 'medium',
                preload_priority REAL NOT NULL DEFAULT 0.5,
                stage_summary TEXT NOT NULL DEFAULT '',
                trajectory_summary TEXT NOT NULL DEFAULT '',
                current_tension TEXT NOT NULL DEFAULT '',
                recent_shift_summary TEXT NOT NULL DEFAULT '',
                recent_movement_summary TEXT NOT NULL DEFAULT '',
                last_meaningful_shift_at TEXT,
                emotional_tension TEXT NOT NULL DEFAULT 'stable',
                affective_direction TEXT NOT NULL DEFAULT 'endurance',
                open_questions TEXT NOT NULL DEFAULT '[]',
                salience REAL NOT NULL DEFAULT 0.5,
                last_touched_at TEXT,
                linked_key_record_ids TEXT NOT NULL DEFAULT '[]',
                linked_event_ids TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        await self.conn.execute(
            """CREATE TABLE IF NOT EXISTS npc_entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT '',
                background TEXT NOT NULL DEFAULT '',
                relationship_to_character TEXT NOT NULL DEFAULT '',
                personality_traits TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'active',
                spawn_source TEXT NOT NULL DEFAULT 'manual',
                spawn_context TEXT NOT NULL DEFAULT '',
                last_interaction_at TEXT,
                interaction_count INTEGER NOT NULL DEFAULT 0,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        await self.conn.execute(
            """CREATE TABLE IF NOT EXISTS character_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger_type TEXT NOT NULL,
                trigger_item_id INTEGER,
                message_text TEXT NOT NULL DEFAULT '',
                tone TEXT NOT NULL DEFAULT 'neutral',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                delivered_at TEXT,
                expires_at TEXT
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
        created_at = normalize_user_instant_to_utc_z(snap.created_at or wall)
        cursor = await self.conn.execute(
            """INSERT INTO state_snapshots
               (created_at, inserted_at, type, content, environment, referenced_events, embedding_vector_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                created_at,
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

    # ── B2: In-flight idempotency barrier ──
    #
    # The snapshot scheduler and reflect_on_conversation BOTH used to call the
    # LLM *before* writing anything to the DB. If a downstream side-effect blew
    # up, the LLM tokens were spent but no row appeared, so the next tick saw
    # the exact same baseline and re-sent the exact same prompt — burning the
    # account dry. The four helpers below let callers reserve a placeholder
    # row (status='in_flight', prompt_hash recorded) BEFORE the LLM call, then
    # either finalize or mark_failed afterwards. Subsequent ticks consult
    # find_*_snapshot_by_prompt_hash to skip work that's already done or in
    # progress, and count_failed_snapshot_attempts gates dead-letter handling.
    # See docs/fix_plan_snapshot_loop.md B2.

    async def insert_snapshot_placeholder(
        self,
        *,
        prompt_hash: str,
        snap_type: str = "daily",
        created_at: str | None = None,
        attempt_count: int = 1,
    ) -> int:
        wall = format_utc_instant_z(datetime.utcnow())
        created_at_norm = (
            normalize_user_instant_to_utc_z(created_at) if created_at else wall
        )
        cursor = await self.conn.execute(
            """INSERT INTO state_snapshots
               (created_at, inserted_at, type, content, environment, referenced_events,
                embedding_vector_id, status, prompt_hash, attempt_count, started_at)
               VALUES (?, ?, ?, '', '{}', '[]', NULL, 'in_flight', ?, ?, ?)""",
            (created_at_norm, wall, snap_type, prompt_hash, attempt_count, wall),
        )
        await self.conn.commit()
        return cursor.lastrowid  # type: ignore

    async def finalize_snapshot(
        self,
        row_id: int,
        *,
        content: str,
        environment: str = "{}",
        referenced_events: str = "[]",
        created_at: str | None = None,
        embedding_vector_id: str | None = None,
    ) -> None:
        created_at_norm = (
            normalize_user_instant_to_utc_z(created_at) if created_at else None
        )
        if created_at_norm is None:
            await self.conn.execute(
                """UPDATE state_snapshots
                   SET status='done', content=?, environment=?, referenced_events=?,
                       embedding_vector_id=?
                   WHERE id=?""",
                (content, environment, referenced_events, embedding_vector_id, row_id),
            )
        else:
            await self.conn.execute(
                """UPDATE state_snapshots
                   SET status='done', content=?, environment=?, referenced_events=?,
                       embedding_vector_id=?, created_at=?
                   WHERE id=?""",
                (
                    content,
                    environment,
                    referenced_events,
                    embedding_vector_id,
                    created_at_norm,
                    row_id,
                ),
            )
        await self.conn.commit()

    async def mark_snapshot_failed(self, row_id: int) -> None:
        await self.conn.execute(
            "UPDATE state_snapshots SET status='failed' WHERE id=?", (row_id,)
        )
        await self.conn.commit()

    async def find_done_snapshot_by_prompt_hash(
        self, prompt_hash: str
    ) -> StateSnapshot | None:
        async with self.conn.execute(
            """SELECT * FROM state_snapshots
               WHERE prompt_hash=? AND status='done'
               ORDER BY id DESC LIMIT 1""",
            (prompt_hash,),
        ) as cur:
            row = await cur.fetchone()
            return StateSnapshot(**dict(row)) if row else None

    async def find_in_flight_snapshot_by_prompt_hash(
        self, prompt_hash: str
    ) -> StateSnapshot | None:
        async with self.conn.execute(
            """SELECT * FROM state_snapshots
               WHERE prompt_hash=? AND status='in_flight'
               ORDER BY id DESC LIMIT 1""",
            (prompt_hash,),
        ) as cur:
            row = await cur.fetchone()
            return StateSnapshot(**dict(row)) if row else None

    async def count_failed_snapshot_attempts(self, prompt_hash: str) -> int:
        async with self.conn.execute(
            "SELECT COUNT(*) FROM state_snapshots WHERE prompt_hash=? AND status='failed'",
            (prompt_hash,),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0] or 0)  # type: ignore

    async def reset_stale_in_flight_snapshots(self, older_than_seconds: int = 600) -> list[int]:
        """Flip in_flight rows whose started_at is older than `older_than_seconds`
        to status='failed' and return their ids. Used by the scheduler to recover
        from crashes that left a placeholder hanging."""
        async with self.conn.execute(
            """SELECT id FROM state_snapshots
               WHERE status='in_flight'
                 AND started_at IS NOT NULL
                 AND (julianday('now') - julianday(started_at)) * 86400.0 > ?""",
            (float(older_than_seconds),),
        ) as cur:
            rows = await cur.fetchall()
        ids = [int(r[0]) for r in rows]
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        await self.conn.execute(
            f"UPDATE state_snapshots SET status='failed' WHERE id IN ({placeholders})",
            ids,
        )
        await self.conn.commit()
        return ids

    async def get_latest_snapshot(self) -> StateSnapshot | None:
        # B2: in_flight/failed placeholders are never returned as the "latest"
        # baseline — otherwise the scheduler would plan from a row with empty
        # content and re-burn the LLM. Legacy rows default to status='done'.
        async with self.conn.execute(
            f"SELECT * FROM state_snapshots WHERE status='done' ORDER BY {self.SNAPSHOT_ORDER_DESC} LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
            return StateSnapshot(**dict(row)) if row else None

    async def get_latest_snapshot_by_type(self, snap_type: str) -> StateSnapshot | None:
        async with self.conn.execute(
            # 对话检查点语义优先「最新写入的一条记录」而非 created_at 最大值：
            # created_at 可能来自导入/回填的历史时间，不能稳定代表最近一次互动写入。
            "SELECT * FROM state_snapshots WHERE type = ? AND status='done' ORDER BY id DESC LIMIT 1",
            (snap_type,),
        ) as cur:
            row = await cur.fetchone()
            return StateSnapshot(**dict(row)) if row else None

    async def get_latest_non_conversation_snapshot(self) -> StateSnapshot | None:
        async with self.conn.execute(
            """SELECT * FROM state_snapshots
               WHERE type != 'conversation_end' AND status='done'
               ORDER BY """
            + self.SNAPSHOT_ORDER_DESC
            + " LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
            return StateSnapshot(**dict(row)) if row else None

    async def count_snapshots_since(self, since_timestamp: str) -> int:
        async with self.conn.execute(
            """SELECT COUNT(*) FROM state_snapshots
               WHERE julianday(created_at) > julianday(?) AND status='done'""",
            (since_timestamp,),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0] or 0)  # type: ignore

    async def get_recent_snapshots(self, limit: int = 7) -> list[StateSnapshot]:
        async with self.conn.execute(
            f"SELECT * FROM state_snapshots WHERE status='done' ORDER BY {self.SNAPSHOT_ORDER_DESC} LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
            return [StateSnapshot(**dict(r)) for r in rows]

    async def get_snapshots_in_range(self, start_date: str, end_date: str) -> list[StateSnapshot]:
        start_ts = f"{start_date}T00:00:00"
        end_ts = f"{end_date}T23:59:59"
        async with self.conn.execute(
            """SELECT * FROM state_snapshots
               WHERE julianday(created_at) >= julianday(?)
                 AND julianday(created_at) <= julianday(?)
               ORDER BY """
            + self.SNAPSHOT_ORDER_ASC,
            (start_ts, end_ts),
        ) as cur:
            rows = await cur.fetchall()
            return [StateSnapshot(**dict(r)) for r in rows]

    async def get_all_snapshots(self, offset: int = 0, limit: int = 50) -> list[StateSnapshot]:
        async with self.conn.execute(
            f"SELECT * FROM state_snapshots ORDER BY {self.SNAPSHOT_ORDER_DESC} LIMIT ? OFFSET ?",
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

    async def get_previous_snapshot_before_id(self, snap_id: int) -> StateSnapshot | None:
        async with self.conn.execute(
            """SELECT * FROM state_snapshots
               WHERE id < ?
               ORDER BY id DESC LIMIT 1""",
            (snap_id,),
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
                   SELECT id FROM state_snapshots
                   ORDER BY """
            + self.SNAPSHOT_ORDER_DESC
            + """ LIMIT ?
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
               ORDER BY """
            + self.SNAPSHOT_ORDER_ASC
            + """
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
               (date, title, description, source, created_at, embedding_vector_id, trigger_keywords, categories, meta_json, archived, importance_score, impression_depth)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event.date, getattr(event, "title", ""), event.description, event.source,
             event.created_at, event.embedding_vector_id, event.trigger_keywords, getattr(event, "categories", "[]"),
             getattr(event, "meta_json", None),
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
        sources: list[str] | None = None,
        scored_only: bool = False,
        min_importance_score: float | None = None,
        max_importance_score: float | None = None,
        min_impression_depth: float | None = None,
        max_impression_depth: float | None = None,
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
        if sources:
            valid_sources = [s for s in sources if s]
            if valid_sources:
                placeholders = ",".join("?" for _ in valid_sources)
                sql += f" AND source IN ({placeholders})"
                params.extend(valid_sources)
        if scored_only:
            sql += " AND importance_score IS NOT NULL AND impression_depth IS NOT NULL"
        if min_importance_score is not None:
            sql += " AND COALESCE(importance_score, -999999) >= ?"
            params.append(min_importance_score)
        if max_importance_score is not None:
            sql += " AND COALESCE(importance_score, 999999) <= ?"
            params.append(max_importance_score)
        if min_impression_depth is not None:
            sql += " AND COALESCE(impression_depth, -999999) >= ?"
            params.append(min_impression_depth)
        if max_impression_depth is not None:
            sql += " AND COALESCE(impression_depth, 999999) <= ?"
            params.append(max_impression_depth)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [EventAnchor(**dict(r)) for r in rows]

    async def count_events(
        self,
        *,
        include_archived: bool = False,
        categories: list[str] | None = None,
        sources: list[str] | None = None,
        scored_only: bool = False,
        min_importance_score: float | None = None,
        max_importance_score: float | None = None,
        min_impression_depth: float | None = None,
        max_impression_depth: float | None = None,
    ) -> int:
        sql = "SELECT COUNT(*) FROM event_anchors WHERE 1=1"
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
        if sources:
            valid_sources = [s for s in sources if s]
            if valid_sources:
                placeholders = ",".join("?" for _ in valid_sources)
                sql += f" AND source IN ({placeholders})"
                params.extend(valid_sources)
        if scored_only:
            sql += " AND importance_score IS NOT NULL AND impression_depth IS NOT NULL"
        if min_importance_score is not None:
            sql += " AND COALESCE(importance_score, -999999) >= ?"
            params.append(min_importance_score)
        if max_importance_score is not None:
            sql += " AND COALESCE(importance_score, 999999) <= ?"
            params.append(max_importance_score)
        if min_impression_depth is not None:
            sql += " AND COALESCE(impression_depth, -999999) >= ?"
            params.append(min_impression_depth)
        if max_impression_depth is not None:
            sql += " AND COALESCE(impression_depth, 999999) <= ?"
            params.append(max_impression_depth)
        async with self.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row and row[0] is not None else 0

    async def delete_events_by_filters(
        self,
        *,
        include_archived: bool = False,
        categories: list[str] | None = None,
        sources: list[str] | None = None,
        scored_only: bool = False,
        min_importance_score: float | None = None,
        max_importance_score: float | None = None,
        min_impression_depth: float | None = None,
        max_impression_depth: float | None = None,
    ) -> int:
        sql = "DELETE FROM event_anchors WHERE 1=1"
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
        if sources:
            valid_sources = [s for s in sources if s]
            if valid_sources:
                placeholders = ",".join("?" for _ in valid_sources)
                sql += f" AND source IN ({placeholders})"
                params.extend(valid_sources)
        if scored_only:
            sql += " AND importance_score IS NOT NULL AND impression_depth IS NOT NULL"
        if min_importance_score is not None:
            sql += " AND COALESCE(importance_score, -999999) >= ?"
            params.append(min_importance_score)
        if max_importance_score is not None:
            sql += " AND COALESCE(importance_score, 999999) <= ?"
            params.append(max_importance_score)
        if min_impression_depth is not None:
            sql += " AND COALESCE(impression_depth, -999999) >= ?"
            params.append(min_impression_depth)
        if max_impression_depth is not None:
            sql += " AND COALESCE(impression_depth, 999999) <= ?"
            params.append(max_impression_depth)
        cursor = await self.conn.execute(sql, params)
        await self.conn.commit()
        return int(getattr(cursor, "rowcount", 0) or 0)

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

    async def get_recent_events_before_date(
        self,
        date_str: str,
        limit: int = 5,
        include_archived: bool = False,
    ) -> list[EventAnchor]:
        sql = """SELECT * FROM event_anchors
                 WHERE date <= ?"""
        params: list = [date_str]
        if not include_archived:
            sql += " AND archived = 0"
        sql += " ORDER BY date DESC, created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [EventAnchor(**dict(r)) for r in rows]

    async def get_events_created_since(
        self,
        since_timestamp: str,
        *,
        limit: int = 20,
        include_archived: bool = False,
        sources: list[str] | None = None,
    ) -> list[EventAnchor]:
        sql = "SELECT * FROM event_anchors WHERE created_at >= ?"
        params: list = [since_timestamp]
        if not include_archived:
            sql += " AND archived = 0"
        if sources:
            placeholders = ", ".join("?" for _ in sources)
            sql += f" AND source IN ({placeholders})"
            params.extend(sources)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
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

    async def get_events_by_date(
        self,
        date_str: str,
        limit: int = 10,
        include_archived: bool = False,
        order_by_importance: bool = False,
    ) -> list[EventAnchor]:
        """Return events whose `date` field matches date_str (YYYY-MM-DD).

        When order_by_importance=True, results are sorted by importance_score DESC
        (NULLs sort last in SQLite DESC ordering) then by id DESC, useful for
        picking the top-K most significant events from a given day.
        When order_by_importance=False, results are sorted chronologically (id ASC).
        """
        sql = "SELECT * FROM event_anchors WHERE date = ?"
        params: list = [date_str]
        if not include_archived:
            sql += " AND archived = 0"
        if order_by_importance:
            sql += " ORDER BY importance_score DESC, id DESC LIMIT ?"
        else:
            sql += " ORDER BY id ASC LIMIT ?"
        params.append(limit)
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [EventAnchor(**dict(r)) for r in rows]

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
            f"""SELECT * FROM state_snapshots
               WHERE content LIKE ? AND embedding_vector_id IS NOT NULL
               ORDER BY {self.SNAPSHOT_ORDER_DESC} LIMIT ?""",
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
                ORDER BY {self.SNAPSHOT_ORDER_DESC} LIMIT ?""",
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
               (type, title, content_text, content_json, tags, match_keywords, start_date, end_date, status, source, life_scope, linked_event_id, embedding_vector_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.type,
                record.title,
                record.content_text,
                record.content_json,
                record.tags,
                record.match_keywords,
                record.start_date,
                record.end_date,
                record.status,
                record.source,
                record.life_scope,
                record.linked_event_id,
                record.embedding_vector_id,
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
        life_scope: str | None = None,
    ) -> KeyRecord | None:
        sql = """SELECT * FROM key_records
               WHERE type = ? AND title = ?"""
        params: list = [record_type, title]
        if life_scope:
            sql += " AND life_scope = ?"
            params.append(life_scope)
        sql += " ORDER BY id DESC LIMIT 1"
        async with self.conn.execute(
            sql,
            params,
        ) as cur:
            row = await cur.fetchone()
            return KeyRecord(**dict(row)) if row else None

    async def get_key_records_by_ids(self, record_ids: list[int]) -> list[KeyRecord]:
        if not record_ids:
            return []
        placeholders = ",".join(["?"] * len(record_ids))
        async with self.conn.execute(
            f"SELECT * FROM key_records WHERE id IN ({placeholders}) ORDER BY id DESC",
            record_ids,
        ) as cur:
            rows = await cur.fetchall()
            return [KeyRecord(**dict(r)) for r in rows]

    async def get_all_key_records(
        self,
        offset: int = 0,
        limit: int = 50,
        record_type: str | None = None,
        status: str | None = None,
        life_scope: str | None = None,
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
        if life_scope:
            sql += " AND life_scope = ?"
            params.append(life_scope)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [KeyRecord(**dict(r)) for r in rows]

    async def get_recent_key_records(
        self,
        limit: int = 5,
        include_archived: bool = False,
        life_scope: str | None = None,
    ) -> list[KeyRecord]:
        sql = "SELECT * FROM key_records WHERE 1=1"
        params: list = []
        if not include_archived:
            sql += " AND status != 'archived'"
        if life_scope:
            sql += " AND life_scope = ?"
            params.append(life_scope)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [KeyRecord(**dict(r)) for r in rows]

    async def get_key_records_updated_since(
        self,
        since_timestamp: str,
        *,
        limit: int = 20,
        include_archived: bool = False,
        sources: list[str] | None = None,
        life_scope: str | None = None,
    ) -> list[KeyRecord]:
        sql = "SELECT * FROM key_records WHERE updated_at >= ?"
        params: list = [since_timestamp]
        if not include_archived:
            sql += " AND status != 'archived'"
        if sources:
            placeholders = ", ".join("?" for _ in sources)
            sql += f" AND source IN ({placeholders})"
            params.extend(sources)
        if life_scope:
            sql += " AND life_scope = ?"
            params.append(life_scope)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [KeyRecord(**dict(r)) for r in rows]

    async def search_key_records(
        self,
        query: str,
        top_k: int = 10,
        record_type: str | None = None,
        life_scope: str | None = None,
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
            conditions.append("(title LIKE ? OR content_text LIKE ? OR tags LIKE ? OR match_keywords LIKE ? OR content_json LIKE ?)")
            params.extend([pattern, pattern, pattern, pattern, pattern])
        where = " OR ".join(conditions)
        sql = f"""SELECT * FROM key_records
                 WHERE ({where})"""
        if record_type:
            sql += " AND type = ?"
            params.append(record_type)
        if life_scope:
            sql += " AND life_scope = ?"
            params.append(life_scope)
        if not include_archived:
            sql += " AND status != 'archived'"
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(top_k)
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [KeyRecord(**dict(r)) for r in rows]

    async def get_key_records_without_vector(
        self,
        limit: int = 200,
        include_archived: bool = True,
    ) -> list[KeyRecord]:
        sql = """SELECT * FROM key_records
                 WHERE embedding_vector_id IS NULL"""
        params: list = []
        if not include_archived:
            sql += " AND status != 'archived'"
        sql += " ORDER BY id ASC LIMIT ?"
        params.append(limit)
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [KeyRecord(**dict(r)) for r in rows]

    async def mark_key_record_vectorized(self, record_id: int, vector_id: str):
        await self.conn.execute(
            "UPDATE key_records SET embedding_vector_id = ? WHERE id = ?",
            (vector_id, record_id),
        )
        await self.conn.commit()

    async def clear_key_record_vectorized(self, record_id: int):
        await self.conn.execute(
            "UPDATE key_records SET embedding_vector_id = NULL WHERE id = ?",
            (record_id,),
        )
        await self.conn.commit()

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

    # ── Daily Plans ──

    async def insert_daily_plan(self, plan: DailyPlan) -> int:
        cursor = await self.conn.execute(
            """INSERT INTO daily_plans
               (plan_date, generated_at, raw_plan, status, replan_trigger, replan_parent_id, context_snapshot, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                plan.plan_date,
                plan.generated_at,
                plan.raw_plan,
                plan.status,
                plan.replan_trigger,
                plan.replan_parent_id,
                plan.context_snapshot,
                plan.created_at,
            ),
        )
        await self.conn.commit()
        return cursor.lastrowid  # type: ignore

    async def get_daily_plan_by_id(self, plan_id: int) -> DailyPlan | None:
        async with self.conn.execute(
            "SELECT * FROM daily_plans WHERE id = ?",
            (plan_id,),
        ) as cur:
            row = await cur.fetchone()
            return DailyPlan(**dict(row)) if row else None

    async def get_latest_daily_plan_for_date(
        self,
        plan_date: str,
        *,
        status: str | None = None,
    ) -> DailyPlan | None:
        sql = "SELECT * FROM daily_plans WHERE plan_date = ?"
        params: list = [plan_date]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY id DESC LIMIT 1"
        async with self.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
            return DailyPlan(**dict(row)) if row else None

    async def list_daily_plans(
        self,
        *,
        offset: int = 0,
        limit: int = 30,
        status: str | None = None,
    ) -> list[DailyPlan]:
        sql = "SELECT * FROM daily_plans WHERE 1=1"
        params: list = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY plan_date DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [DailyPlan(**dict(r)) for r in rows]

    async def update_daily_plan(self, plan_id: int, **fields) -> None:
        if not fields:
            return
        allowed = {
            "plan_date",
            "generated_at",
            "raw_plan",
            "status",
            "replan_trigger",
            "replan_parent_id",
            "context_snapshot",
            "created_at",
        }
        payload = {k: v for k, v in fields.items() if k in allowed}
        if not payload:
            return
        sets = ", ".join(f"{key} = ?" for key in payload)
        values = list(payload.values()) + [plan_id]
        await self.conn.execute(
            f"UPDATE daily_plans SET {sets} WHERE id = ?",
            values,
        )
        await self.conn.commit()

    async def delete_daily_plan(self, plan_id: int) -> None:
        await self.conn.execute("DELETE FROM daily_plans WHERE id = ?", (plan_id,))
        await self.conn.commit()

    # ── Plan Items ──

    async def insert_plan_item(self, item: PlanItem) -> int:
        cursor = await self.conn.execute(
            """INSERT INTO plan_items
               (plan_id, hour_start, hour_end, activity, action_type, action_payload, status, outcome, outcome_event_id, source_kind, source_ref_id, created_at, executed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item.plan_id,
                item.hour_start,
                item.hour_end,
                item.activity,
                item.action_type,
                item.action_payload,
                item.status,
                item.outcome,
                item.outcome_event_id,
                item.source_kind,
                item.source_ref_id,
                item.created_at,
                item.executed_at,
            ),
        )
        await self.conn.commit()
        return cursor.lastrowid  # type: ignore

    async def get_plan_item_by_id(self, item_id: int) -> PlanItem | None:
        async with self.conn.execute(
            "SELECT * FROM plan_items WHERE id = ?",
            (item_id,),
        ) as cur:
            row = await cur.fetchone()
            return PlanItem(**dict(row)) if row else None

    async def list_plan_items(
        self,
        plan_id: int,
        *,
        status: str | None = None,
    ) -> list[PlanItem]:
        sql = "SELECT * FROM plan_items WHERE plan_id = ?"
        params: list = [plan_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY hour_start ASC, hour_end ASC, id ASC"
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [PlanItem(**dict(r)) for r in rows]

    async def get_plan_item_for_hour(
        self,
        plan_id: int,
        hour: int,
        *,
        status: str | None = None,
    ) -> PlanItem | None:
        sql = (
            "SELECT * FROM plan_items "
            "WHERE plan_id = ? AND hour_start <= ? AND hour_end > ?"
        )
        params: list = [plan_id, hour, hour]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY hour_start ASC, id ASC LIMIT 1"
        async with self.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
            return PlanItem(**dict(row)) if row else None

    async def update_plan_item(self, item_id: int, **fields) -> None:
        if not fields:
            return
        allowed = {
            "plan_id",
            "hour_start",
            "hour_end",
            "activity",
            "action_type",
            "action_payload",
            "status",
            "outcome",
            "outcome_event_id",
            "source_kind",
            "source_ref_id",
            "created_at",
            "executed_at",
        }
        payload = {k: v for k, v in fields.items() if k in allowed}
        if not payload:
            return
        sets = ", ".join(f"{key} = ?" for key in payload)
        values = list(payload.values()) + [item_id]
        await self.conn.execute(
            f"UPDATE plan_items SET {sets} WHERE id = ?",
            values,
        )
        await self.conn.commit()

    async def delete_plan_items_for_plan(self, plan_id: int) -> int:
        cursor = await self.conn.execute(
            "DELETE FROM plan_items WHERE plan_id = ?",
            (plan_id,),
        )
        await self.conn.commit()
        return cursor.rowcount

    # ── Life Flow Traces ──

    async def insert_life_flow_trace(self, trace: LifeFlowTrace) -> int:
        cursor = await self.conn.execute(
            """INSERT INTO life_flow_traces
               (trace_date, source, summary, details_json, schedule_alignment, related_snapshot_id, related_event_ids, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trace.trace_date,
                trace.source,
                trace.summary,
                trace.details_json,
                trace.schedule_alignment,
                trace.related_snapshot_id,
                trace.related_event_ids,
                trace.created_at,
                trace.updated_at,
            ),
        )
        await self.conn.commit()
        return cursor.lastrowid  # type: ignore

    async def get_recent_life_flow_traces(
        self,
        *,
        limit: int = 6,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[LifeFlowTrace]:
        sql = "SELECT * FROM life_flow_traces WHERE 1=1"
        params: list = []
        if start_date:
            sql += " AND trace_date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND trace_date <= ?"
            params.append(end_date)
        sql += " ORDER BY trace_date DESC, updated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [LifeFlowTrace(**dict(r)) for r in rows]

    async def get_latest_life_flow_trace_for_date(self, trace_date: str) -> LifeFlowTrace | None:
        async with self.conn.execute(
            """SELECT * FROM life_flow_traces
               WHERE trace_date = ?
               ORDER BY updated_at DESC, id DESC LIMIT 1""",
            (trace_date,),
        ) as cur:
            row = await cur.fetchone()
            return LifeFlowTrace(**dict(row)) if row else None

    async def update_life_flow_trace(self, trace_id: int, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = format_utc_instant_z(datetime.utcnow())
        set_clause = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [trace_id]
        await self.conn.execute(
            f"UPDATE life_flow_traces SET {set_clause} WHERE id = ?",
            values,
        )
        await self.conn.commit()

    # ── Disturbance Pulses ──

    async def insert_disturbance_pulse(self, pulse: DisturbancePulse) -> int:
        cursor = await self.conn.execute(
            """INSERT INTO disturbance_pulses
               (occur_at, reveal_at, status, channel_type, source_family, seed_kind, seed_ref_id,
                blind_spot_reason, reveal_channel, title, factual_payload_json, impact_hint,
                salience, novelty_score, cooldown_until, fingerprint, linked_snapshot_id,
                linked_event_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pulse.occur_at,
                pulse.reveal_at,
                pulse.status,
                pulse.channel_type,
                pulse.source_family,
                pulse.seed_kind,
                pulse.seed_ref_id,
                pulse.blind_spot_reason,
                pulse.reveal_channel,
                pulse.title,
                pulse.factual_payload_json,
                pulse.impact_hint,
                pulse.salience,
                pulse.novelty_score,
                pulse.cooldown_until,
                pulse.fingerprint,
                pulse.linked_snapshot_id,
                pulse.linked_event_id,
                pulse.created_at,
                pulse.updated_at,
            ),
        )
        await self.conn.commit()
        return cursor.lastrowid  # type: ignore

    async def get_disturbance_pulse_by_id(self, pulse_id: int) -> DisturbancePulse | None:
        async with self.conn.execute(
            "SELECT * FROM disturbance_pulses WHERE id = ?",
            (pulse_id,),
        ) as cur:
            row = await cur.fetchone()
            return DisturbancePulse(**dict(row)) if row else None

    async def list_disturbance_pulses(
        self,
        *,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[DisturbancePulse]:
        sql = "SELECT * FROM disturbance_pulses WHERE 1=1"
        params: list = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY reveal_at DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [DisturbancePulse(**dict(r)) for r in rows]

    async def get_recent_disturbance_pulses(
        self,
        *,
        limit: int = 6,
        statuses: list[str] | None = None,
    ) -> list[DisturbancePulse]:
        sql = "SELECT * FROM disturbance_pulses WHERE 1=1"
        params: list = []
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            sql += f" AND status IN ({placeholders})"
            params.extend(statuses)
        sql += " ORDER BY reveal_at DESC, id DESC LIMIT ?"
        params.append(limit)
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [DisturbancePulse(**dict(r)) for r in rows]

    async def update_disturbance_pulse(self, pulse_id: int, **fields) -> None:
        if not fields:
            return
        allowed = {
            "occur_at",
            "reveal_at",
            "status",
            "channel_type",
            "source_family",
            "seed_kind",
            "seed_ref_id",
            "blind_spot_reason",
            "reveal_channel",
            "title",
            "factual_payload_json",
            "impact_hint",
            "salience",
            "novelty_score",
            "cooldown_until",
            "fingerprint",
            "linked_snapshot_id",
            "linked_event_id",
            "created_at",
            "updated_at",
        }
        payload = {k: v for k, v in fields.items() if k in allowed}
        if not payload:
            return
        if "updated_at" not in payload:
            payload["updated_at"] = format_utc_instant_z(datetime.utcnow())
        sets = ", ".join(f"{key} = ?" for key in payload)
        values = list(payload.values()) + [pulse_id]
        await self.conn.execute(
            f"UPDATE disturbance_pulses SET {sets} WHERE id = ?",
            values,
        )
        await self.conn.commit()

    # ── Conversation Time Claims ──

    async def insert_conversation_time_claim(self, claim: ConversationTimeClaim) -> int:
        cursor = await self.conn.execute(
            """INSERT INTO conversation_time_claims
               (status, started_at, ended_at, source, context_summary, latest_snapshot_id, closing_snapshot_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                claim.status,
                claim.started_at,
                claim.ended_at,
                claim.source,
                claim.context_summary,
                claim.latest_snapshot_id,
                claim.closing_snapshot_id,
                claim.created_at,
                claim.updated_at,
            ),
        )
        await self.conn.commit()
        return cursor.lastrowid  # type: ignore

    async def get_active_conversation_time_claim(self) -> ConversationTimeClaim | None:
        async with self.conn.execute(
            """SELECT * FROM conversation_time_claims
               WHERE status = 'active'
               ORDER BY started_at DESC, id DESC
               LIMIT 1"""
        ) as cur:
            row = await cur.fetchone()
            return ConversationTimeClaim(**dict(row)) if row else None

    async def update_conversation_time_claim(self, claim_id: int, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = format_utc_instant_z(datetime.utcnow())
        set_clause = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [claim_id]
        await self.conn.execute(
            f"UPDATE conversation_time_claims SET {set_clause} WHERE id = ?",
            values,
        )
        await self.conn.commit()

    async def get_conversation_time_claim_by_id(self, claim_id: int) -> ConversationTimeClaim | None:
        async with self.conn.execute(
            "SELECT * FROM conversation_time_claims WHERE id = ?",
            (claim_id,),
        ) as cur:
            row = await cur.fetchone()
            return ConversationTimeClaim(**dict(row)) if row else None

    async def list_conversation_time_claims(
        self,
        *,
        status: str | None = None,
        limit: int = 10,
    ) -> list[ConversationTimeClaim]:
        sql = "SELECT * FROM conversation_time_claims WHERE 1=1"
        params: list = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY started_at DESC, id DESC LIMIT ?"
        params.append(limit)
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [ConversationTimeClaim(**dict(r)) for r in rows]

    # ── NPC Entities ──

    async def insert_relationship_state(self, state: RelationshipState) -> int:
        cursor = await self.conn.execute(
            """INSERT INTO relationship_states
               (last_meaningful_contact_at, hours_since_meaningful_contact, days_since_meaningful_contact,
                contact_recency_bucket, connection_need, pride_or_distance, valence, arousal, life_immersion,
                relationship_feeling_summary, space_need_level, concern_level, proactive_topics,
                plan_bias_hint, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                state.last_meaningful_contact_at,
                state.hours_since_meaningful_contact,
                state.days_since_meaningful_contact,
                state.contact_recency_bucket,
                state.connection_need,
                state.pride_or_distance,
                state.valence,
                state.arousal,
                state.life_immersion,
                state.relationship_feeling_summary,
                state.space_need_level,
                state.concern_level,
                state.proactive_topics,
                state.plan_bias_hint,
                state.created_at,
                state.updated_at,
            ),
        )
        await self.conn.commit()
        return cursor.lastrowid  # type: ignore

    async def get_latest_relationship_state(self) -> RelationshipState | None:
        async with self.conn.execute(
            "SELECT * FROM relationship_states ORDER BY updated_at DESC, id DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
            return RelationshipState(**dict(row)) if row else None

    async def update_relationship_state(self, state_id: int, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = format_utc_instant_z(datetime.utcnow())
        set_clause = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [state_id]
        await self.conn.execute(
            f"UPDATE relationship_states SET {set_clause} WHERE id = ?",
            values,
        )
        await self.conn.commit()

    async def insert_relationship_thought(self, thought: RelationshipThought) -> int:
        cursor = await self.conn.execute(
            """INSERT INTO relationship_thoughts
               (thought_date, source_snapshot_id, source_env_id, topic_line, thought_type, content,
                salience, dedupe_fingerprint, resolution_status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                thought.thought_date,
                thought.source_snapshot_id,
                thought.source_env_id,
                thought.topic_line,
                thought.thought_type,
                thought.content,
                thought.salience,
                thought.dedupe_fingerprint,
                thought.resolution_status,
                thought.created_at,
                thought.updated_at,
            ),
        )
        await self.conn.commit()
        return cursor.lastrowid  # type: ignore

    async def list_relationship_thoughts(
        self,
        *,
        thought_date: str | None = None,
        resolution_status: str | None = None,
        limit: int = 20,
    ) -> list[RelationshipThought]:
        sql = "SELECT * FROM relationship_thoughts WHERE 1=1"
        params: list = []
        if thought_date:
            sql += " AND thought_date = ?"
            params.append(thought_date)
        if resolution_status:
            sql += " AND resolution_status = ?"
            params.append(resolution_status)
        sql += " ORDER BY salience DESC, updated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [RelationshipThought(**dict(r)) for r in rows]

    async def get_relationship_thought_by_fingerprint(
        self,
        *,
        thought_date: str,
        dedupe_fingerprint: str,
        resolution_status: str | None = None,
    ) -> RelationshipThought | None:
        sql = """SELECT * FROM relationship_thoughts
                 WHERE thought_date = ? AND dedupe_fingerprint = ?"""
        params: list = [thought_date, dedupe_fingerprint]
        if resolution_status:
            sql += " AND resolution_status = ?"
            params.append(resolution_status)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT 1"
        async with self.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
            return RelationshipThought(**dict(row)) if row else None

    async def update_relationship_thought(self, thought_id: int, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = format_utc_instant_z(datetime.utcnow())
        set_clause = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [thought_id]
        await self.conn.execute(
            f"UPDATE relationship_thoughts SET {set_clause} WHERE id = ?",
            values,
        )
        await self.conn.commit()

    async def insert_slowline(self, line: SlowLine) -> int:
        cursor = await self.conn.execute(
            """INSERT INTO slowlines
               (thread_key, theme, scope, source_family, memory_role, progress_status, tension_level, unresolved_level, preload_priority,
                stage_summary, trajectory_summary, current_tension, recent_shift_summary, recent_movement_summary, last_meaningful_shift_at,
                emotional_tension, affective_direction, open_questions, salience, last_touched_at, linked_key_record_ids, linked_event_ids,
                status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                line.thread_key,
                line.theme,
                line.scope,
                line.source_family,
                line.memory_role,
                line.progress_status,
                line.tension_level,
                line.unresolved_level,
                line.preload_priority,
                line.stage_summary,
                line.trajectory_summary,
                line.current_tension,
                line.recent_shift_summary,
                line.recent_movement_summary,
                line.last_meaningful_shift_at,
                line.emotional_tension,
                line.affective_direction,
                line.open_questions,
                line.salience,
                line.last_touched_at,
                line.linked_key_record_ids,
                line.linked_event_ids,
                line.status,
                line.created_at,
                line.updated_at,
            ),
        )
        await self.conn.commit()
        return cursor.lastrowid  # type: ignore

    @staticmethod
    def _normalize_slowline_row(payload: dict) -> dict:
        source_family = str(payload.get("source_family") or "").strip()
        payload["source_family"] = {
            "conversation": "relationship",
            "autonomous": "daily_life",
            "mixed": "daily_life",
            "": "daily_life",
        }.get(source_family, source_family)
        scope = str(payload.get("scope") or "").strip()
        if scope not in {"user_side", "character_side", "shared"}:
            payload["scope"] = "shared"
        progress_status = str(payload.get("progress_status") or "").strip()
        if progress_status not in {"open", "advancing", "paused", "ready_to_close", "completed", "dropped"}:
            payload["progress_status"] = "open"
        memory_role = str(payload.get("memory_role") or "").strip()
        if memory_role not in {"bridge_core", "active_thread_detail", "trigger_only", "archive_reference"}:
            payload["memory_role"] = "active_thread_detail"
        tension_level = str(payload.get("tension_level") or "").strip()
        if tension_level not in {"low", "medium", "high"}:
            payload["tension_level"] = "medium"
        unresolved_level = str(payload.get("unresolved_level") or "").strip()
        if unresolved_level not in {"low", "medium", "high"}:
            payload["unresolved_level"] = "medium"
        try:
            payload["preload_priority"] = float(payload.get("preload_priority") or 0.5)
        except Exception:
            payload["preload_priority"] = 0.5
        emotional_tension = str(payload.get("emotional_tension") or "").strip()
        if emotional_tension not in {"stable", "strained", "brittle", "tender", "suspended", "unresolved"}:
            payload["emotional_tension"] = "stable"
        affective_direction = str(payload.get("affective_direction") or "").strip()
        if affective_direction not in {"approach", "avoidance", "ambivalence", "endurance", "repair"}:
            payload["affective_direction"] = "endurance"
        if not str(payload.get("recent_shift_summary") or "").strip():
            payload["recent_shift_summary"] = str(payload.get("recent_movement_summary") or "").strip()
        return payload

    async def get_slowline_by_thread_key(self, thread_key: str) -> SlowLine | None:
        async with self.conn.execute(
            """SELECT * FROM slowlines
               WHERE thread_key = ? AND status = 'active'
               ORDER BY updated_at DESC, id DESC LIMIT 1""",
            (thread_key,),
        ) as cur:
            row = await cur.fetchone()
            return SlowLine(**self._normalize_slowline_row(dict(row))) if row else None

    async def get_slowline_by_theme(self, theme: str) -> SlowLine | None:
        async with self.conn.execute(
            """SELECT * FROM slowlines
               WHERE theme = ? AND status = 'active'
               ORDER BY updated_at DESC, id DESC LIMIT 1""",
            (theme,),
        ) as cur:
            row = await cur.fetchone()
            return SlowLine(**self._normalize_slowline_row(dict(row))) if row else None

    async def list_slowlines(
        self,
        *,
        status: str = "active",
        limit: int = 12,
    ) -> list[SlowLine]:
        async with self.conn.execute(
            """SELECT * FROM slowlines
               WHERE status = ?
               ORDER BY salience DESC, updated_at DESC, id DESC LIMIT ?""",
            (status, limit),
        ) as cur:
            rows = await cur.fetchall()
            return [SlowLine(**self._normalize_slowline_row(dict(r))) for r in rows]

    async def update_slowline(self, slowline_id: int, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = format_utc_instant_z(datetime.utcnow())
        set_clause = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [slowline_id]
        await self.conn.execute(
            f"UPDATE slowlines SET {set_clause} WHERE id = ?",
            values,
        )
        await self.conn.commit()

    async def insert_npc_entity(self, npc: NPCEntity) -> int:
        cursor = await self.conn.execute(
            """INSERT INTO npc_entities
               (name, role, background, relationship_to_character, personality_traits, status, spawn_source, spawn_context, last_interaction_at, interaction_count, notes, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                npc.name,
                npc.role,
                npc.background,
                npc.relationship_to_character,
                npc.personality_traits,
                npc.status,
                npc.spawn_source,
                npc.spawn_context,
                npc.last_interaction_at,
                npc.interaction_count,
                npc.notes,
                npc.created_at,
                npc.updated_at,
            ),
        )
        await self.conn.commit()
        return cursor.lastrowid  # type: ignore

    async def get_npc_entity_by_id(self, npc_id: int) -> NPCEntity | None:
        async with self.conn.execute(
            "SELECT * FROM npc_entities WHERE id = ?",
            (npc_id,),
        ) as cur:
            row = await cur.fetchone()
            return NPCEntity(**dict(row)) if row else None

    async def list_npc_entities(
        self,
        *,
        status: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[NPCEntity]:
        sql = "SELECT * FROM npc_entities WHERE 1=1"
        params: list = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [NPCEntity(**dict(r)) for r in rows]

    async def update_npc_entity(self, npc_id: int, **fields) -> None:
        if not fields:
            return
        allowed = {
            "name",
            "role",
            "background",
            "relationship_to_character",
            "personality_traits",
            "status",
            "spawn_source",
            "spawn_context",
            "last_interaction_at",
            "interaction_count",
            "notes",
            "created_at",
            "updated_at",
        }
        payload = {k: v for k, v in fields.items() if k in allowed}
        if not payload:
            return
        if "updated_at" not in payload:
            payload["updated_at"] = datetime.utcnow().isoformat()
        sets = ", ".join(f"{key} = ?" for key in payload)
        values = list(payload.values()) + [npc_id]
        await self.conn.execute(
            f"UPDATE npc_entities SET {sets} WHERE id = ?",
            values,
        )
        await self.conn.commit()

    # ── Character Notifications ──

    async def insert_character_notification(self, notification: CharacterNotification) -> int:
        cursor = await self.conn.execute(
            """INSERT INTO character_notifications
               (trigger_type, trigger_item_id, message_text, tone, status, created_at, delivered_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                notification.trigger_type,
                notification.trigger_item_id,
                notification.message_text,
                notification.tone,
                notification.status,
                notification.created_at,
                notification.delivered_at,
                notification.expires_at,
            ),
        )
        await self.conn.commit()
        return cursor.lastrowid  # type: ignore

    async def get_character_notification_by_id(
        self,
        notification_id: int,
    ) -> CharacterNotification | None:
        async with self.conn.execute(
            "SELECT * FROM character_notifications WHERE id = ?",
            (notification_id,),
        ) as cur:
            row = await cur.fetchone()
            return CharacterNotification(**dict(row)) if row else None

    async def list_character_notifications(
        self,
        *,
        status: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[CharacterNotification]:
        sql = "SELECT * FROM character_notifications WHERE 1=1"
        params: list = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [CharacterNotification(**dict(r)) for r in rows]

    async def update_character_notification(self, notification_id: int, **fields) -> None:
        if not fields:
            return
        allowed = {
            "trigger_type",
            "trigger_item_id",
            "message_text",
            "tone",
            "status",
            "created_at",
            "delivered_at",
            "expires_at",
        }
        payload = {k: v for k, v in fields.items() if k in allowed}
        if not payload:
            return
        sets = ", ".join(f"{key} = ?" for key in payload)
        values = list(payload.values()) + [notification_id]
        await self.conn.execute(
            f"UPDATE character_notifications SET {sets} WHERE id = ?",
            values,
        )
        await self.conn.commit()
