"""Regression: a DB row with an out-of-range enum must not 500 a list endpoint.

Root cause of the GET /api/npcs 500
(`ValidationError: status Input should be 'active'/'inactive'/'departed',
input_value=''`): an npc_entities row had empty-string status/spawn_source
(written by a manual SQL edit / external import / partial insert). The
strict Literal fields on NPCEntity rejected '', and
`[NPCEntity(**dict(r)) for r in rows]` propagated that one row's failure to
the whole endpoint.

Two layers of defense:
  - models._DomainModel coerces out-of-range Literal values to the field
    default (graceful degradation everywhere the model is used).
  - database._rows_to_models skips any row that STILL fails to validate
    (NULL in a required non-Literal column, etc.) instead of crashing.

API request models intentionally stay strict (reject bad client input).

See docs/fix_plan_snapshot_loop.md (enum-drift incident).
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from server.database import Database, _rows_to_models
from server.models import (
    CreateNPCRequest,
    DailyPlan,
    NPCEntity,
    PlanItem,
    _DomainModel,
    _literal_options,
)


def run(coro):
    return asyncio.run(coro)


# ── _literal_options helper ──────────────────────────────────────────


def test_literal_options_extracts_plain_literal():
    from typing import Literal

    assert _literal_options(Literal["a", "b", "c"]) == ("a", "b", "c")


def test_literal_options_peers_into_optional_literal():
    from typing import Literal, Optional

    assert _literal_options(Optional[Literal["x", "y"]]) == ("x", "y")
    assert _literal_options(Literal["x", "y"] | None) == ("x", "y")


def test_literal_options_returns_none_for_non_literal():
    assert _literal_options(str) is None
    assert _literal_options(int | None) is None


# ── _DomainModel: graceful enum coercion ─────────────────────────────


def test_npc_empty_enums_coerce_to_default():
    """The exact failing payload from the /api/npcs traceback."""
    npc = NPCEntity(name="阿米娅", status="", spawn_source="")
    assert npc.status == "active"  # default
    assert npc.spawn_source == "manual"  # default


def test_npc_unknown_enum_value_coerces_to_default():
    npc = NPCEntity(name="x", status="banished")  # not a valid literal
    assert npc.status == "active"


def test_valid_enum_values_pass_through_unchanged():
    npc = NPCEntity(name="x", status="departed", spawn_source="auto_generated")
    assert npc.status == "departed"
    assert npc.spawn_source == "auto_generated"


def test_coercion_does_not_touch_non_enum_fields():
    npc = NPCEntity(name="凯尔希", role="医生", interaction_count=7)
    assert npc.name == "凯尔希"
    assert npc.role == "医生"
    assert npc.interaction_count == 7


def test_coercion_applies_across_domain_models():
    """Spot-check that the base-class fix reaches other models too."""
    plan = DailyPlan(status="")  # invalid → default 'active'
    assert plan.status == "active"
    item = PlanItem(plan_id=1, status="bogus", action_type="")
    assert item.status == "pending"
    assert item.action_type == "internal"


def test_required_literal_with_no_default_uses_first_option():
    """If a required Literal field (no default) ever gets a bad value, the
    coercer falls back to the first allowed option rather than raising.
    KeyRecord.type has a default, so synthesize the required case via a
    throwaway model."""
    from typing import Literal

    class _Req(_DomainModel):
        kind: Literal["a", "b", "c"]  # required, no default

    obj = _Req(kind="zzz")
    assert obj.kind == "a"


# ── API request models stay strict ───────────────────────────────────


def test_api_request_model_still_rejects_bad_enum():
    """CreateNPCRequest is an API request model — it must NOT inherit the
    lenient coercion, so bad client input is still rejected."""
    # A valid one works.
    ok = CreateNPCRequest(name="x", spawn_source="manual")
    assert ok.spawn_source == "manual"
    # An invalid enum must raise (strict).
    with pytest.raises(ValidationError):
        CreateNPCRequest(name="x", spawn_source="not-a-source")


# ── _rows_to_models: per-row tolerance backstop ──────────────────────


def test_rows_to_models_skips_unparseable_row():
    # PlanItem requires plan_id (int, no default). A row missing it can't
    # be coerced by the literal-fixer, so it must be skipped, not fatal.
    rows = [
        {"id": 1, "plan_id": 10, "activity": "ok"},
        {"id": 2, "activity": "missing plan_id"},  # unparseable
        {"id": 3, "plan_id": 11, "activity": "also ok"},
    ]
    out = _rows_to_models(PlanItem, rows, context="test")
    assert [m.activity for m in out] == ["ok", "also ok"]


def test_rows_to_models_empty_input():
    assert _rows_to_models(NPCEntity, [], context="test") == []


# ── End-to-end: list_npc_entities survives the corrupt row ───────────


async def _make_db_with_bad_npc():
    tmp = tempfile.mkdtemp()
    db = Database(str(Path(tmp) / "npc.db"))
    await db.initialize()
    # One healthy NPC, one with empty enum columns (the corruption).
    await db.conn.execute(
        "INSERT INTO npc_entities (name, status, spawn_source, created_at, updated_at) "
        "VALUES (?, 'active', 'manual', ?, ?)",
        ("好 NPC", "2026-06-12T00:00:00Z", "2026-06-12T00:00:00Z"),
    )
    await db.conn.execute(
        "INSERT INTO npc_entities (name, status, spawn_source, created_at, updated_at) "
        "VALUES (?, '', '', ?, ?)",
        ("坏 NPC", "2026-06-12T00:00:00Z", "2026-06-12T00:00:00Z"),
    )
    await db.conn.commit()
    return db


def test_list_npc_entities_survives_empty_enum_row():
    async def scenario():
        db = await _make_db_with_bad_npc()
        try:
            npcs = await db.list_npc_entities(limit=100)
            assert len(npcs) == 2  # both returned, none crashed the call
            bad = next(n for n in npcs if n.name == "坏 NPC")
            assert bad.status == "active"  # coerced
            assert bad.spawn_source == "manual"  # coerced
        finally:
            await db.close()

    run(scenario())
