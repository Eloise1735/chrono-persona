"""Regression: a TEXT column holding non-UTF-8 bytes must not 500 reads.

Root cause of the GET /api/plans/history crash: a daily_plans.raw_plan row
was written with non-UTF-8 bytes (a plan uploaded/edited in a non-UTF-8
encoding such as Windows GBK, or truncated mid-multibyte-char). SQLite's
default text_factory decodes TEXT as strict UTF-8 and raises
`OperationalError: Could not decode to UTF-8 column ...` on fetch, taking
down the whole endpoint.

The app's own write paths bind Python `str` and always produce valid
UTF-8, so this can only come from outside the app — but a single such row
must never crash a read. _lenient_text_factory decodes with replacement;
repair_non_utf8_text() cleans the stored bytes permanently.

See docs/fix_plan_snapshot_loop.md (daily-plan UTF-8 incident).
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import aiosqlite

from server.database import Database, _lenient_text_factory


def run(coro):
    return asyncio.run(coro)


# Valid JSON skeleton + a truncated/garbled multibyte run inside one value —
# mirrors the bytes seen in the production traceback.
CORRUPT_RAW_PLAN_BYTES = b'[{"hour_start":7,"activity":"\xe4\xbd\xa0\xe5\xa5"}]'


# ── _lenient_text_factory unit behavior ──────────────────────────────


def test_lenient_factory_passes_valid_utf8_unchanged():
    assert _lenient_text_factory("正常文本".encode("utf-8")) == "正常文本"


def test_lenient_factory_replaces_invalid_bytes_without_raising():
    out = _lenient_text_factory(CORRUPT_RAW_PLAN_BYTES)
    assert isinstance(out, str)
    assert "�" in out  # replacement char where the bad bytes were
    assert out.startswith('[{"hour_start":7')  # ASCII skeleton preserved


# ── End-to-end: corrupt row no longer crashes a read ─────────────────


async def _make_db_with_corrupt_plan() -> tuple[Database, str]:
    tmp = tempfile.mkdtemp()
    db_path = str(Path(tmp) / "corrupt.db")
    db = Database(db_path)
    await db.initialize()
    # Force a TEXT-class value with invalid UTF-8 (CAST(blob AS TEXT)) —
    # exactly how an external non-UTF-8 write lands in the column.
    await db.conn.execute(
        "INSERT INTO daily_plans (plan_date, generated_at, raw_plan, status, created_at) "
        "VALUES (?, ?, CAST(? AS TEXT), 'active', ?)",
        ("2026-06-12", "2026-06-12T00:00:00Z", CORRUPT_RAW_PLAN_BYTES, "2026-06-12T00:00:00Z"),
    )
    await db.conn.commit()
    return db, db_path


def test_list_daily_plans_survives_corrupt_row():
    async def scenario():
        db, _ = await _make_db_with_corrupt_plan()
        try:
            # This is the exact call path that used to 500 /api/plans/history.
            plans = await db.list_daily_plans(limit=30)
            assert len(plans) == 1
            assert "�" in plans[0].raw_plan  # decoded leniently, no crash
            assert plans[0].plan_date == "2026-06-12"
        finally:
            await db.close()

    run(scenario())


def test_default_factory_would_have_crashed():
    """Guard the premise: prove the same row DOES crash under SQLite's
    default strict text_factory, so the lenient one is load-bearing."""

    async def scenario():
        conn = await aiosqlite.connect(":memory:")
        try:
            await conn.execute("CREATE TABLE t (raw TEXT)")
            await conn.execute(
                "INSERT INTO t (raw) VALUES (CAST(? AS TEXT))", (CORRUPT_RAW_PLAN_BYTES,)
            )
            await conn.commit()
            raised = False
            try:
                async with conn.execute("SELECT raw FROM t") as cur:
                    await cur.fetchone()
            except Exception as e:  # sqlite3.OperationalError
                raised = "decode" in str(e).lower() or "utf-8" in str(e).lower()
            assert raised, "expected strict factory to raise on corrupt row"
        finally:
            await conn.close()

    run(scenario())


# ── repair_non_utf8_text ─────────────────────────────────────────────


def test_repair_rewrites_corrupt_row_to_valid_utf8():
    async def scenario():
        db, db_path = await _make_db_with_corrupt_plan()
        try:
            report = await db.repair_non_utf8_text()
            assert report["repaired"] == 1
            assert report["by_table"].get("daily_plans") == 1

            # After repair, the stored bytes are valid UTF-8: a fresh
            # connection using the STRICT default factory can read it.
            await db.close()
            conn = await aiosqlite.connect(db_path)
            conn.row_factory = aiosqlite.Row
            try:
                async with conn.execute("SELECT raw_plan FROM daily_plans") as cur:
                    row = await cur.fetchone()  # must NOT raise under strict factory
                assert row["raw_plan"].startswith('[{"hour_start":7')
            finally:
                await conn.close()
        finally:
            try:
                await db.close()
            except Exception:
                pass

    run(scenario())


def test_repair_is_idempotent_and_clean_db_is_noop():
    async def scenario():
        db, _ = await _make_db_with_corrupt_plan()
        try:
            first = await db.repair_non_utf8_text()
            assert first["repaired"] == 1
            # Second pass: nothing left to repair.
            second = await db.repair_non_utf8_text()
            assert second["repaired"] == 0
        finally:
            await db.close()

    run(scenario())


def test_repair_dry_run_reports_without_writing():
    async def scenario():
        db, _ = await _make_db_with_corrupt_plan()
        try:
            report = await db.repair_non_utf8_text(dry_run=True)
            assert report["dry_run"] is True
            assert report["repaired"] == 1  # would repair
            # But nothing was written — a real pass still finds it.
            after = await db.repair_non_utf8_text()
            assert after["repaired"] == 1
        finally:
            await db.close()

    run(scenario())


def test_repair_leaves_clean_rows_untouched():
    async def scenario():
        tmp = tempfile.mkdtemp()
        db = Database(str(Path(tmp) / "clean.db"))
        await db.initialize()
        try:
            await db.conn.execute(
                "INSERT INTO daily_plans (plan_date, generated_at, raw_plan, status, created_at) "
                "VALUES (?, ?, ?, 'active', ?)",
                ("2026-06-12", "2026-06-12T00:00:00Z", '[{"activity":"正常计划"}]', "2026-06-12T00:00:00Z"),
            )
            await db.conn.commit()
            report = await db.repair_non_utf8_text()
            assert report["repaired"] == 0
            plans = await db.list_daily_plans()
            assert plans[0].raw_plan == '[{"activity":"正常计划"}]'
        finally:
            await db.close()

    run(scenario())


# ── Admin endpoint ───────────────────────────────────────────────────


def test_admin_repair_text_endpoint():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from server.api_routes import router as api_router, set_dependencies

    async def scenario():
        db, _ = await _make_db_with_corrupt_plan()
        return db

    db = run(scenario())
    try:
        app = FastAPI()
        app.include_router(api_router)
        set_dependencies(db, None, memory_store=None)
        with TestClient(app) as client:
            # Dry run first.
            r = client.post("/api/admin/db/repair-text", json={"dry_run": True})
            assert r.status_code == 200, r.text
            assert r.json()["repaired"] == 1
            # Real repair.
            r = client.post("/api/admin/db/repair-text", json={})
            assert r.status_code == 200
            assert r.json()["repaired"] == 1
            # Idempotent.
            r = client.post("/api/admin/db/repair-text", json={})
            assert r.json()["repaired"] == 0
    finally:
        run(db.close())
