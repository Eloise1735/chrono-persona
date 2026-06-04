"""C3 acceptance: /api/admin/health and /api/admin/scheduler/{name}/resume.

The admin surface is the user-visible payoff for everything in B1–B3+C1:
without these endpoints (and the web page that consumes them) the user
would still have no way to notice a stuck scheduler before the next
21-hour incident. These tests pin down the JSON shape that
web/admin-health.html depends on, and verify the resume contract.

See docs/fix_plan_snapshot_loop.md C3.
"""

from __future__ import annotations

import asyncio

import aiosqlite
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.api_routes as api_routes_mod
from server.api_routes import router as api_router, set_dependencies
from server.database import Database
from server.scheduler_breaker import SchedulerCircuitBreaker
from server.state_machine import StateMachine


def run(coro):
    return asyncio.run(coro)


async def _make_db_with_schema():
    """In-memory DB with the state_snapshots schema needed for the admin
    queries. Matches the production columns added in B1/B2/B3."""
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


def _make_state_machine_skeleton() -> StateMachine:
    """A skeletal StateMachine carrying only the two C1 breakers — enough
    for the admin endpoints to dispatch by name."""
    sm = object.__new__(StateMachine)
    sm.snapshot_scheduler_breaker = SchedulerCircuitBreaker(name="snapshot_scheduler")
    sm.life_scheduler_breaker = SchedulerCircuitBreaker(name="life_scheduler")
    return sm


def _client_for(db: Database, sm: StateMachine) -> TestClient:
    app = FastAPI()
    app.include_router(api_router)
    set_dependencies(db, sm, memory_store=None)
    return TestClient(app)


# ── GET /api/admin/health ────────────────────────────────────────────


def test_admin_health_returns_both_breakers_with_documented_shape():
    async def setup():
        db, _ = await _make_db_with_schema()
        return db

    db = run(setup())
    try:
        sm = _make_state_machine_skeleton()
        with _client_for(db, sm) as client:
            r = client.get("/api/admin/health")
            assert r.status_code == 200, r.text
            payload = r.json()
            # Top-level shape that admin-health.js depends on:
            for key in (
                "now",
                "schedulers",
                "in_flight_snapshots",
                "in_flight_count",
                "last_snapshot_at",
                "last_reflect_at",
                "age_since_last_snapshot_s",
                "age_since_last_reflect_s",
            ):
                assert key in payload, f"missing {key} in /api/admin/health"
            assert set(payload["schedulers"].keys()) == {
                "snapshot_scheduler",
                "life_scheduler",
            }
            # Each breaker carries the C1 snapshot fields plus the C3 ages.
            for name, snap in payload["schedulers"].items():
                for k in (
                    "name",
                    "paused",
                    "paused_reason",
                    "consecutive_failures",
                    "last_tick_at",
                    "last_success_at",
                    "last_failure_at",
                    "age_since_last_success_s",
                    "age_since_last_failure_s",
                ):
                    assert k in snap, f"breaker {name} missing {k}"
                assert snap["paused"] is False
                assert snap["consecutive_failures"] == 0
    finally:
        run(db.close())


def test_admin_health_surfaces_in_flight_placeholders_oldest_first():
    async def scenario():
        db, conn = await _make_db_with_schema()
        try:
            # Fresh in_flight (1s old)
            await conn.execute(
                """INSERT INTO state_snapshots (created_at, status, prompt_hash, attempt_count, started_at)
                   VALUES (?, 'in_flight', ?, 1, datetime('now', '-1 seconds'))""",
                ("2026-06-01T00:00:00Z", "hash-fresh-aaaaaaaa"),
            )
            # Stale in_flight (1h old)
            await conn.execute(
                """INSERT INTO state_snapshots (created_at, status, prompt_hash, attempt_count, started_at)
                   VALUES (?, 'in_flight', ?, 2, datetime('now', '-3600 seconds'))""",
                ("2026-06-01T00:00:00Z", "hash-stale-bbbbbbbb"),
            )
            # Done row — should NOT appear in in_flight list
            await conn.execute(
                "INSERT INTO state_snapshots (created_at, content, status) VALUES (?, 'finished', 'done')",
                ("2026-06-01T00:00:00Z",),
            )
            await conn.commit()
        finally:
            pass  # keep db open for endpoint use
        return db

    db = run(scenario())
    try:
        sm = _make_state_machine_skeleton()
        with _client_for(db, sm) as client:
            r = client.get("/api/admin/health")
            assert r.status_code == 200
            payload = r.json()
            in_flight = payload["in_flight_snapshots"]
            assert payload["in_flight_count"] == 2
            # Stale row first (oldest).
            assert in_flight[0]["prompt_hash"] == "hash-stale-bbbbbbbb"
            assert in_flight[0]["prompt_hash_short"] == "hash-stale-b"
            assert in_flight[0]["age_s"] >= 60.0
            assert in_flight[1]["prompt_hash"] == "hash-fresh-aaaaaaaa"
            assert in_flight[1]["age_s"] < 60.0
            # Done row didn't leak in.
            assert all(
                row["prompt_hash"] != "finished" for row in in_flight
            )
    finally:
        run(db.close())


def test_admin_health_reports_last_snapshot_and_reflect_timestamps():
    async def scenario():
        db, conn = await _make_db_with_schema()
        await conn.execute(
            "INSERT INTO state_snapshots (created_at, content, status, type) VALUES (?, 'daily-old', 'done', 'daily')",
            ("2026-05-01T00:00:00Z",),
        )
        await conn.execute(
            "INSERT INTO state_snapshots (created_at, content, status, type) VALUES (?, 'reflect-mid', 'done', 'conversation_end')",
            ("2026-05-15T10:00:00Z",),
        )
        await conn.execute(
            "INSERT INTO state_snapshots (created_at, content, status, type) VALUES (?, 'daily-new', 'done', 'daily')",
            ("2026-06-01T12:00:00Z",),
        )
        # An in_flight placeholder must NOT shift "last_snapshot_at".
        await conn.execute(
            """INSERT INTO state_snapshots (created_at, status, prompt_hash, started_at, type)
               VALUES (?, 'in_flight', 'hash-x', datetime('now'), 'daily')""",
            ("2026-07-01T00:00:00Z",),
        )
        await conn.commit()
        return db

    db = run(scenario())
    try:
        sm = _make_state_machine_skeleton()
        with _client_for(db, sm) as client:
            r = client.get("/api/admin/health")
            payload = r.json()
            assert payload["last_snapshot_at"] == "2026-06-01T12:00:00Z"
            assert payload["last_reflect_at"] == "2026-05-15T10:00:00Z"
            # Ages are non-negative floats (we used past timestamps).
            assert payload["age_since_last_snapshot_s"] >= 0
            assert payload["age_since_last_reflect_s"] >= 0
    finally:
        run(db.close())


def test_admin_health_handles_empty_database():
    """A fresh deployment with no snapshots yet must still return 200
    and a well-formed payload — never blow up the dashboard."""

    async def scenario():
        db, _ = await _make_db_with_schema()
        return db

    db = run(scenario())
    try:
        sm = _make_state_machine_skeleton()
        with _client_for(db, sm) as client:
            r = client.get("/api/admin/health")
            assert r.status_code == 200
            payload = r.json()
            assert payload["in_flight_snapshots"] == []
            assert payload["in_flight_count"] == 0
            assert payload["last_snapshot_at"] is None
            assert payload["last_reflect_at"] is None
            assert payload["age_since_last_snapshot_s"] is None
            assert payload["age_since_last_reflect_s"] is None
    finally:
        run(db.close())


def test_admin_health_reflects_paused_breaker_state():
    """A paused breaker MUST be visible on /admin/health — otherwise the
    web page can't render the red badge that the human needs to act."""

    async def scenario():
        db, _ = await _make_db_with_schema()
        return db

    db = run(scenario())
    try:
        sm = _make_state_machine_skeleton()
        # Trip the snapshot scheduler.
        for _ in range(10):
            sm.snapshot_scheduler_breaker.record_failure(RuntimeError("upstream 502"))
        assert sm.snapshot_scheduler_breaker.is_paused()

        with _client_for(db, sm) as client:
            r = client.get("/api/admin/health")
            payload = r.json()
            snap = payload["schedulers"]["snapshot_scheduler"]
            assert snap["paused"] is True
            assert snap["paused_reason"].startswith("consecutive_failures>=")
            assert snap["consecutive_failures"] >= 10
            # The other breaker is unaffected.
            assert payload["schedulers"]["life_scheduler"]["paused"] is False
    finally:
        run(db.close())


# ── POST /api/admin/scheduler/{name}/resume ──────────────────────────


def test_resume_clears_paused_breaker_via_endpoint():
    async def scenario():
        db, _ = await _make_db_with_schema()
        return db

    db = run(scenario())
    try:
        sm = _make_state_machine_skeleton()
        for _ in range(10):
            sm.life_scheduler_breaker.record_failure(RuntimeError("x"))
        assert sm.life_scheduler_breaker.is_paused()

        with _client_for(db, sm) as client:
            r = client.post("/api/admin/scheduler/life_scheduler/resume")
            assert r.status_code == 200, r.text
            payload = r.json()
            assert payload["ok"] is True
            assert payload["was_paused"] is True
            assert payload["breaker"]["paused"] is False
            assert payload["breaker"]["consecutive_failures"] == 0
        # In-memory state actually changed.
        assert not sm.life_scheduler_breaker.is_paused()
    finally:
        run(db.close())


def test_resume_on_not_paused_breaker_is_idempotent():
    async def scenario():
        db, _ = await _make_db_with_schema()
        return db

    db = run(scenario())
    try:
        sm = _make_state_machine_skeleton()
        assert not sm.snapshot_scheduler_breaker.is_paused()
        with _client_for(db, sm) as client:
            r = client.post("/api/admin/scheduler/snapshot_scheduler/resume")
            assert r.status_code == 200
            assert r.json()["was_paused"] is False
            # Second call: still works, still was_paused=False.
            r2 = client.post("/api/admin/scheduler/snapshot_scheduler/resume")
            assert r2.status_code == 200
            assert r2.json()["was_paused"] is False
    finally:
        run(db.close())


def test_resume_unknown_scheduler_returns_404():
    async def scenario():
        db, _ = await _make_db_with_schema()
        return db

    db = run(scenario())
    try:
        sm = _make_state_machine_skeleton()
        with _client_for(db, sm) as client:
            r = client.post("/api/admin/scheduler/does_not_exist/resume")
            assert r.status_code == 404
            detail = r.json()["detail"]
            assert "unknown scheduler" in detail
            assert "snapshot_scheduler" in detail
            assert "life_scheduler" in detail
    finally:
        run(db.close())


def test_health_reflects_resume_after_full_round_trip():
    """End-to-end: trip breaker → see paused via GET → POST resume → see
    not-paused via GET. This is the user journey the admin page enables."""

    async def scenario():
        db, _ = await _make_db_with_schema()
        return db

    db = run(scenario())
    try:
        sm = _make_state_machine_skeleton()
        for _ in range(10):
            sm.snapshot_scheduler_breaker.record_failure(RuntimeError("boom"))

        with _client_for(db, sm) as client:
            paused_payload = client.get("/api/admin/health").json()
            assert paused_payload["schedulers"]["snapshot_scheduler"]["paused"] is True

            client.post("/api/admin/scheduler/snapshot_scheduler/resume")

            after_payload = client.get("/api/admin/health").json()
            after = after_payload["schedulers"]["snapshot_scheduler"]
            assert after["paused"] is False
            assert after["consecutive_failures"] == 0
            # Recovery history is preserved — admin can still see what happened.
            assert after["last_failure_at"] is not None
    finally:
        run(db.close())
