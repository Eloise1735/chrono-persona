"""B2 acceptance: in-flight idempotency barrier — state-machine wrap contract.

The DB-layer primitives are covered by tests/test_snapshot_in_flight_helpers.py.
These tests verify the small but critical piece on top: that the state machine
computes a stable prompt hash and that the idempotency decision (cache_hit /
in_flight / dead_letter / proceed) honors the DB state.

We deliberately do NOT spin up the full StateMachine — its constructor wires
20+ collaborators (prompt_manager, env_gen, OB client, plan_engine, …) that
are irrelevant to the B2 contract. Instead we use the `object.__new__`
pattern already established in test_ob_alignment.py and exercise the
idempotency primitives the wrap relies on.

See docs/fix_plan_snapshot_loop.md B2.
"""

from __future__ import annotations

import asyncio

import aiosqlite

from server.database import Database
from server.state_machine import StateMachine


def run(coro):
    return asyncio.run(coro)


# ── _compute_prompt_hash: stability and sensitivity ──


def test_compute_prompt_hash_is_deterministic_for_same_inputs():
    h1 = StateMachine._compute_prompt_hash("reflect", "system", "user payload")
    h2 = StateMachine._compute_prompt_hash("reflect", "system", "user payload")
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex


def test_compute_prompt_hash_distinguishes_part_boundaries():
    """A naive concatenation hash would collide on `"a"+"bc"` vs `"ab"+"c"`.
    The B2 helper inserts a NUL delimiter to prevent this — verify it."""
    h1 = StateMachine._compute_prompt_hash("a", "bc")
    h2 = StateMachine._compute_prompt_hash("ab", "c")
    assert h1 != h2


def test_compute_prompt_hash_distinguishes_prompt_changes():
    base = StateMachine._compute_prompt_hash("scheduler:tick", "2026-06-01T08:00", "system", "prompt-v1")
    differ_checkpoint = StateMachine._compute_prompt_hash("scheduler:tick", "2026-06-01T09:00", "system", "prompt-v1")
    differ_prompt = StateMachine._compute_prompt_hash("scheduler:tick", "2026-06-01T08:00", "system", "prompt-v2")
    differ_role = StateMachine._compute_prompt_hash("reflect:conv", "2026-06-01T08:00", "system", "prompt-v1")
    assert len({base, differ_checkpoint, differ_prompt, differ_role}) == 4


def test_compute_prompt_hash_treats_none_as_empty():
    """Callers pass `or ""` defensively; the helper should also tolerate None
    without raising so that a missing system prompt does not crash the tick."""
    h = StateMachine._compute_prompt_hash("a", None, "b")  # type: ignore[arg-type]
    assert isinstance(h, str) and len(h) == 64


# ── Idempotency contract through DB primitives ──
#
# These simulate the decision the state-machine wrap makes around the LLM
# call. The contract is: given a prompt_hash, exactly one of the four
# branches fires:
#   - find_done_snapshot_by_prompt_hash → cache_hit (return cached content,
#     no LLM call)
#   - find_in_flight_snapshot_by_prompt_hash → in_flight (skip / break)
#   - count_failed_snapshot_attempts >= 3 → dead_letter (skip / break)
#   - else → insert placeholder, call LLM, finalize


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
            started_at TEXT
        )"""
    )
    return db, conn


class _CountingLLM:
    """Stub LLM that records every call. Used to assert the idempotency wrap
    does NOT re-invoke the model when a cached/in_flight/dead-letter row
    exists for the same prompt_hash."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def chat(self, prompt: str) -> str:
        self.calls.append(prompt)
        return f"llm-output-for[{prompt}]"


async def _simulate_tick_with_idempotency(
    db: Database,
    llm: _CountingLLM,
    *,
    prompt_hash: str,
    prompt: str,
    snap_type: str = "daily",
    created_at: str = "2026-06-01T08:00:00Z",
    dead_letter_threshold: int = 3,
) -> dict:
    """Mirrors the exact decision sequence the state_machine wrap performs
    around the LLM call. Returns the branch taken."""
    existing_done = await db.find_done_snapshot_by_prompt_hash(prompt_hash)
    if existing_done is not None:
        return {"branch": "cache_hit", "content": existing_done.content, "id": existing_done.id}
    in_flight = await db.find_in_flight_snapshot_by_prompt_hash(prompt_hash)
    if in_flight is not None:
        return {"branch": "in_flight_skip", "id": in_flight.id}
    failed_count = await db.count_failed_snapshot_attempts(prompt_hash)
    if failed_count >= dead_letter_threshold:
        return {"branch": "dead_letter", "failed_count": failed_count}
    placeholder_id = await db.insert_snapshot_placeholder(
        prompt_hash=prompt_hash,
        snap_type=snap_type,
        created_at=created_at,
        attempt_count=failed_count + 1,
    )
    try:
        content = await llm.chat(prompt)
        await db.finalize_snapshot(
            placeholder_id,
            content=content,
            created_at=created_at,
        )
        return {"branch": "proceeded", "id": placeholder_id, "content": content}
    except Exception:
        await db.mark_snapshot_failed(placeholder_id)
        raise


def test_cache_hit_after_successful_run_skips_llm():
    """The headline B2 invariant: once a snapshot for a given prompt_hash is
    'done', a subsequent tick with the same prompt_hash MUST NOT call the LLM
    again. This is the property that would have prevented the original
    21-hour token-burn incident."""

    async def scenario():
        db, _ = await _make_db()
        llm = _CountingLLM()
        try:
            ph = StateMachine._compute_prompt_hash("scheduler:tick", "ckp-1", "sys", "prompt body")
            r1 = await _simulate_tick_with_idempotency(db, llm, prompt_hash=ph, prompt="prompt body")
            assert r1["branch"] == "proceeded"
            assert len(llm.calls) == 1

            # Second tick: same prompt_hash. The wrap MUST short-circuit.
            r2 = await _simulate_tick_with_idempotency(db, llm, prompt_hash=ph, prompt="prompt body")
            assert r2["branch"] == "cache_hit"
            assert r2["content"] == r1["content"]
            assert len(llm.calls) == 1, "LLM was re-invoked on a cache-hit prompt"
        finally:
            await db.close()

    run(scenario())


def test_in_flight_placeholder_blocks_duplicate_llm_call():
    """If a concurrent tick (or a crashed previous tick still within the
    10-minute stale window) has an in_flight row for this prompt, the
    second tick MUST refuse to call the LLM."""

    async def scenario():
        db, _ = await _make_db()
        llm = _CountingLLM()
        try:
            ph = StateMachine._compute_prompt_hash("scheduler:tick", "ckp-2", "sys", "prompt body")
            # Reserve an in_flight placeholder (simulating a tick mid-call).
            await db.insert_snapshot_placeholder(prompt_hash=ph, attempt_count=1)

            r = await _simulate_tick_with_idempotency(db, llm, prompt_hash=ph, prompt="prompt body")
            assert r["branch"] == "in_flight_skip"
            assert llm.calls == []
        finally:
            await db.close()

    run(scenario())


def test_dead_letter_after_three_failures_refuses_llm():
    """After 3 failed attempts on the same prompt, the wrap must dead-letter
    and refuse further LLM calls — otherwise a persistently broken prompt
    burns ~3 ticks' worth of tokens forever."""

    async def scenario():
        db, _ = await _make_db()
        llm = _CountingLLM()
        try:
            ph = StateMachine._compute_prompt_hash("scheduler:tick", "ckp-3", "sys", "prompt body")
            # Simulate 3 prior failed attempts.
            for n in range(1, 4):
                pid = await db.insert_snapshot_placeholder(prompt_hash=ph, attempt_count=n)
                await db.mark_snapshot_failed(pid)

            r = await _simulate_tick_with_idempotency(db, llm, prompt_hash=ph, prompt="prompt body")
            assert r["branch"] == "dead_letter"
            assert r["failed_count"] == 3
            assert llm.calls == []
        finally:
            await db.close()

    run(scenario())


def test_first_two_failures_still_allow_retry():
    """Symmetric guard: dead-letter must not fire below the threshold,
    otherwise transient LLM errors permanently disable the prompt."""

    async def scenario():
        db, _ = await _make_db()
        llm = _CountingLLM()
        try:
            ph = StateMachine._compute_prompt_hash("scheduler:tick", "ckp-4", "sys", "prompt body")
            for n in range(1, 3):  # 2 prior failures
                pid = await db.insert_snapshot_placeholder(prompt_hash=ph, attempt_count=n)
                await db.mark_snapshot_failed(pid)

            r = await _simulate_tick_with_idempotency(db, llm, prompt_hash=ph, prompt="prompt body")
            assert r["branch"] == "proceeded"
            assert len(llm.calls) == 1
        finally:
            await db.close()

    run(scenario())


def test_llm_failure_marks_placeholder_failed_and_does_not_advance_done_count():
    """When the LLM call itself raises, the placeholder must transition to
    'failed' so future ticks can either retry or dead-letter — but the
    'done' query must NOT see it (otherwise a one-shot failure would
    permanently masquerade as success)."""

    async def scenario():
        db, _ = await _make_db()

        class _FlakyLLM:
            async def chat(self, prompt):
                raise RuntimeError("upstream 502")

        llm = _FlakyLLM()
        try:
            ph = StateMachine._compute_prompt_hash("scheduler:tick", "ckp-5", "sys", "prompt body")
            try:
                await _simulate_tick_with_idempotency(db, llm, prompt_hash=ph, prompt="prompt body")  # type: ignore[arg-type]
                assert False, "expected RuntimeError to propagate"
            except RuntimeError:
                pass

            # Placeholder now status='failed' and counted as a failed attempt.
            assert await db.count_failed_snapshot_attempts(ph) == 1
            assert await db.find_done_snapshot_by_prompt_hash(ph) is None
            assert await db.find_in_flight_snapshot_by_prompt_hash(ph) is None
        finally:
            await db.close()

    run(scenario())
