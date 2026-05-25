from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
import uuid

from server.ob_client import OBClient
from server.ob_decay import OBDecayEngine, OBDecaySettings
from server.state_machine import StateMachine


def run(coro):
    return asyncio.run(coro)


class FakeEmbeddingStore:
    def __init__(self, vectors: dict[str, list[float]] | None = None):
        self.vectors = vectors or {}

    def get(self, bucket_id: str):
        return self.vectors.get(bucket_id)

    async def upsert(self, bucket_id: str, content: str):
        self.vectors.setdefault(bucket_id, [1.0, 0.0, 0.0])
        return True


async def _client(name: str):
    root = Path("data") / "ob_unit_tests" / f"{name}_{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=True)
    return OBClient(root / "ob")


def test_natural_breath_does_not_touch():
    async def scenario():
        client = await _client("natural")
        bid = await client.hold(
            "alpha natural memory",
            name="alpha",
            domain=["test"],
            importance=8,
            arousal=0.6,
            created="2026-01-01T00:00:00",
        )
        before = await client.get(bid)
        results = await client.breath(limit=5)
        after = await client.get(bid)
        assert [b.id for b in results] == [bid]
        assert after.metadata["last_active"] == before.metadata["last_active"]
        assert after.metadata["activation_count"] == 0

    run(scenario())


def test_query_breath_touches_recall_hits():
    async def scenario():
        client = await _client("query")
        bid = await client.hold(
            "alpha query memory",
            name="alpha query",
            domain=["test"],
            importance=5,
            created="2026-01-01T00:00:00",
        )
        results = await client.breath(query="alpha query", limit=5)
        after = await client.get(bid)
        assert [b.id for b in results] == [bid]
        assert float(after.metadata["activation_count"]) >= 1
        assert after.metadata["last_active"] != "2026-01-01T00:00:00"

    run(scenario())


def test_feel_channel_is_chronological_and_does_not_touch():
    async def scenario():
        client = await _client("feel")
        old_id = await client.hold(
            "old feel",
            bucket_type="feel",
            created="2026-01-01T00:00:00",
        )
        new_id = await client.hold(
            "new feel",
            bucket_type="feel",
            created="2026-01-02T00:00:00",
        )
        results = await client.breath(domain="feel", limit=10)
        old_after = await client.get(old_id)
        assert [b.id for b in results] == [new_id, old_id]
        assert old_after.metadata["last_active"] == "2026-01-01T00:00:00"
        assert old_after.metadata["activation_count"] == 0

    run(scenario())


def test_feel_channel_caps_character_life_to_one():
    async def scenario():
        client = await _client("feel_cap")
        char_old = await client.hold(
            "old character life feel",
            bucket_type="feel",
            domain=["character_life"],
            created="2026-01-01T00:00:00",
        )
        non_old = await client.hold(
            "old non character feel",
            bucket_type="feel",
            domain=["relationship"],
            created="2026-01-02T00:00:00",
        )
        char_new = await client.hold(
            "new character life feel",
            bucket_type="feel",
            domain=["character_life"],
            created="2026-01-03T00:00:00",
        )
        non_new = await client.hold(
            "new non character feel",
            bucket_type="feel",
            domain=[],
            created="2026-01-04T00:00:00",
        )
        results = await client.breath(domain="feel", limit=5)
        ids = [b.id for b in results]
        assert char_new in ids
        assert char_old not in ids
        assert non_new in ids
        assert non_old in ids
        assert sum(1 for b in results if "character_life" in b.metadata.get("domain", [])) == 1

    run(scenario())


def test_feel_channel_default_limit_is_three():
    async def scenario():
        client = await _client("feel_default")
        ids = []
        for idx in range(5):
            ids.append(await client.hold(
                f"feel {idx}",
                bucket_type="feel",
                created=f"2026-01-0{idx + 1}T00:00:00",
            ))
        results = await client.breath(domain="feel")
        assert len(results) == 3
        assert [item.id for item in results] == list(reversed(ids[-3:]))

    run(scenario())


def test_surface_breath_uses_new_quotas():
    async def scenario():
        client = await _client("surface_quotas")
        for idx in range(5):
            await client.hold(
                f"pinned core {idx}",
                name=f"pinned {idx}",
                bucket_type="permanent",
                pinned=True,
                created=f"2026-01-0{idx + 1}T00:00:00",
            )
        for idx in range(3):
            await client.hold(
                f"character life dynamic {idx}",
                domain=["character_life"],
                importance=8,
                created=f"2026-01-1{idx}T00:00:00",
            )
        for idx in range(8):
            await client.hold(
                f"relationship dynamic {idx}",
                domain=["relationship"],
                importance=6,
                created=f"2026-01-2{idx}T00:00:00",
            )
        results = await client.breath(limit=8)
        assert len(results) == 8
        assert sum(1 for item in results if item.metadata.get("pinned") or item.metadata.get("protected")) <= 2
        assert sum(
            1 for item in results
            if item.metadata.get("type") == "dynamic" and "character_life" in item.metadata.get("domain", [])
        ) <= 2
        assert sum(1 for item in results if item.metadata.get("type") == "dynamic") >= 4

    run(scenario())


def test_breath_bundle_returns_ordinary_and_feel_without_touch():
    async def scenario():
        client = await _client("bundle")
        dynamic_id = await client.hold("dynamic one", importance=7, created="2026-01-01T00:00:00")
        feel_id = await client.hold("feel one", bucket_type="feel", created="2026-01-02T00:00:00")
        before_dynamic = await client.get(dynamic_id)
        before_feel = await client.get(feel_id)
        result = await client.breath_bundle()
        after_dynamic = await client.get(dynamic_id)
        after_feel = await client.get(feel_id)
        assert result["ordinary"]
        assert result["feel"]
        assert after_dynamic.metadata["last_active"] == before_dynamic.metadata["last_active"]
        assert after_feel.metadata["last_active"] == before_feel.metadata["last_active"]

    run(scenario())


def test_hold_feel_source_digest_contract():
    async def scenario():
        client = await _client("feel_digest")
        source_id = await client.hold("source memory", name="source", importance=7)
        feel_id = await client.hold(
            "model-side sediment",
            bucket_type="feel",
            domain=[],
            valence=0.4,
            arousal=0.2,
            extra_metadata={"source_bucket": source_id},
        )
        await client.update(source_id, digested=True, model_valence=0.4, model_arousal=0.2)
        source = await client.get(source_id)
        feel = await client.get(feel_id)
        assert feel.metadata["type"] == "feel"
        assert feel.metadata["domain"] == []
        assert feel.metadata["source_bucket"] == source_id
        assert source.metadata["digested"] is True
        assert source.metadata["model_valence"] == 0.4

    run(scenario())


def test_snapshot_feel_hold_preserves_first_person_content():
    async def scenario():
        client = await _client("snapshot_feel")
        machine = object.__new__(StateMachine)
        machine.ob_client = client
        snapshot_text = "我把这份迟来的安静收进身体里。"
        bid = await machine._ob_hold_snapshot_feel(
            title="snapshot feel",
            content=snapshot_text,
            tags=["snapshot", "feel"],
            created="2026-01-02T00:00:00",
            extra_metadata={"snapshot_id": 42, "source_kind": "state_snapshot"},
        )
        bucket = await client.get(bid)
        assert bucket.content == snapshot_text
        assert bucket.metadata["type"] == "feel"
        assert bucket.metadata["snapshot_id"] == 42
        assert bucket.metadata["source_kind"] == "state_snapshot"

    run(scenario())


def test_injectable_context_uses_recent_feel_not_snapshot_text():
    async def scenario():
        client = await _client("injectable_feel")
        await client.hold(
            "我留下来的第一人称 feel。",
            bucket_type="feel",
            created="2026-01-02T00:00:00",
        )
        machine = object.__new__(StateMachine)
        machine.ob_client = client
        machine.plan_engine = None

        async def key_records_context(limit: int = 5):
            return "（暂无近期关键记录）"

        machine._build_recent_key_records_context = key_records_context
        injectable = await machine._build_injectable_context("不应进入注入的当前快照。")
        assert "【近期 feel】" in injectable
        assert "我留下来的第一人称 feel。" in injectable
        assert "【当前状态快照】" not in injectable
        assert "不应进入注入的当前快照。" not in injectable

    run(scenario())


def test_backend_feel_context_uses_one_character_life_and_two_other_feels():
    async def scenario():
        client = await _client("backend_feel_mix")
        await client.hold(
            "较旧的 character_life feel。",
            bucket_type="feel",
            domain=["character_life"],
            created="2026-01-01T00:00:00",
        )
        await client.hold(
            "新的 character_life feel。",
            bucket_type="feel",
            domain=["character_life"],
            created="2026-01-04T00:00:00",
        )
        await client.hold(
            "第一条非 character_life feel。",
            bucket_type="feel",
            domain=["relationship"],
            created="2026-01-03T00:00:00",
        )
        await client.hold(
            "第二条非 character_life feel。",
            bucket_type="feel",
            domain=[],
            created="2026-01-02T00:00:00",
        )
        machine = object.__new__(StateMachine)
        machine.ob_client = client
        text = await machine._ob_feel_context_text(character_life_limit=1, other_limit=2)
        assert "新的 character_life feel。" in text
        assert "较旧的 character_life feel。" not in text
        assert "第一条非 character_life feel。" in text
        assert "第二条非 character_life feel。" in text

    run(scenario())


def test_environment_trace_hold_is_dynamic_character_life_memory():
    async def scenario():
        client = await _client("environment_trace")
        machine = object.__new__(StateMachine)
        machine.ob_client = client
        env = {
            "activity": "凯尔希在医疗部处理延迟递交的感染者复查名单。",
            "summary": "Core focus: 医疗部复查名单被临时延后。\nImmediate changes: 她重新调整了下午的审批顺序。\nOpen loop: 仍有一份外勤报告没有回传。",
            "retrieval_summary": "医疗部复查名单延迟，凯尔希调整下午审批顺序，外勤报告尚未回传。",
            "plan_delta": "delayed",
        }
        content = machine._build_environment_trace_content(env)
        bid = await machine._ob_hold_environment_trace(
            title="环境推进",
            content=content,
            tags=["environment_trace", "character_life", "snapshot_scheduler"],
            created="2026-01-02T00:00:00",
            extra_metadata={"snapshot_id": 7, "source_kind": "environment_trace"},
        )
        bucket = await client.get(bid)
        assert bucket.content.startswith("医疗部复查名单延迟")
        assert bucket.metadata["type"] == "dynamic"
        assert bucket.metadata["domain"] == ["character_life"]
        assert bucket.metadata["source_kind"] == "environment_trace"
        assert "environment_trace" in bucket.metadata["tags"]
        surfaced = await client.breath(limit=5)
        assert bid in [item.id for item in surfaced]

    run(scenario())


def test_dream_excludes_resolved_digested_feel_and_archived():
    async def scenario():
        client = await _client("dream")
        keep_id = await client.hold("keep dream", name="keep", created="2026-01-05T00:00:00")
        resolved_id = await client.hold("resolved dream", name="resolved", resolved=True)
        digested_id = await client.hold("digested dream", name="digested")
        feel_id = await client.hold("feel dream", bucket_type="feel")
        archived_id = await client.hold("archived dream", name="archived")
        await client.update(digested_id, digested=True)
        await client.archive(archived_id)
        result = await client.dream(limit=10)
        ids = [item["id"] for item in result["items"]]
        assert ids == [keep_id]
        assert "hold_feel" in result["text"]
        assert "resolve_bucket" in result["text"]
        assert "crystallize_feel" in result["text"]
        assert resolved_id not in ids
        assert digested_id not in ids
        assert feel_id not in ids
        assert archived_id not in ids

    run(scenario())


def test_feel_crystals_paginates_clusters_and_crystallize_principle_marks_sources():
    async def scenario():
        client = await _client("feel_crystals")
        ids = []
        for idx in range(6):
            ids.append(await client.hold(
                f"similar feel {idx}",
                bucket_type="feel",
                created=f"2026-01-0{idx + 1}T00:00:00",
            ))
        client.set_embedding_store(FakeEmbeddingStore({
            ids[0]: [1.0, 0.0, 0.0],
            ids[1]: [0.99, 0.01, 0.0],
            ids[2]: [0.98, 0.02, 0.0],
            ids[3]: [0.0, 1.0, 0.0],
            ids[4]: [0.0, 0.99, 0.01],
            ids[5]: [0.0, 0.98, 0.02],
        }))
        page = await client.feel_crystals(limit=1, max_items_per_cluster=2, min_cluster_size=3, min_similarity=0.9)
        assert page["total_clusters"] == 2
        assert page["has_more"] is True
        assert page["clusters"][0]["hidden_count"] == 1
        assert page["next_cursor"]

        cluster_id = page["clusters"][0]["cluster_id"]
        result = await client.crystallize_feel(
            mode="principle",
            principle_content="A stable principle from repeated feel.",
            cluster_id=cluster_id,
            include_all=True,
            min_cluster_size=3,
            min_similarity=0.9,
        )
        assert result["principle_bucket_id"]
        assert result["marked_count"] == 3
        principle = await client.get(result["principle_bucket_id"])
        assert principle.metadata["pinned"] is True
        assert principle.metadata.get("protected") in (None, False)
        for source_id in result["source_feel_ids"]:
            source = await client.get(source_id)
            assert source.metadata["crystallized"] is True
            assert source.metadata["digested"] is True

    run(scenario())


def test_crystallize_feel_mode_creates_condensed_feel_only():
    async def scenario():
        client = await _client("feel_condense")
        ids = [
            await client.hold("first feel", bucket_type="feel"),
            await client.hold("second feel", bucket_type="feel"),
        ]
        result = await client.crystallize_feel(
            mode="feel",
            feel_content="condensed feel",
            feel_ids=ids,
        )
        assert result["feel_bucket_id"]
        assert "principle_bucket_id" not in result
        condensed = await client.get(result["feel_bucket_id"])
        assert condensed.metadata["type"] == "feel"
        assert not condensed.metadata.get("pinned")
        assert result["marked_count"] == 2

    run(scenario())


def test_decay_auto_resolves_and_archives():
    async def scenario():
        client = await _client("decay")
        old = (datetime.utcnow() - timedelta(days=120)).isoformat()
        bid = await client.hold(
            "old low importance memory",
            importance=1,
            arousal=0.1,
            created=old,
        )
        engine = OBDecayEngine(client, OBDecaySettings(threshold=0.3))
        dry = await engine.run_decay_cycle(dry_run=True)
        assert bid in dry["auto_resolved_ids"]
        assert bid in dry["archived_ids"]
        real = await engine.run_decay_cycle(dry_run=False)
        archived = await client.get(bid)
        assert bid in real["archived_ids"]
        assert archived.metadata["type"] == "archived"

    run(scenario())


def test_special_decay_scores():
    async def scenario():
        client = await _client("scores")
        assert client.calculate_score({"type": "permanent"}) == 999.0
        assert client.calculate_score({"pinned": True}) == 999.0
        assert client.calculate_score({"protected": True}) == 999.0
        assert client.calculate_score({"type": "feel"}) == 50.0
        base = {
            "type": "dynamic",
            "importance": 7,
            "activation_count": 3,
            "arousal": 0.5,
            "created": datetime.utcnow().isoformat(),
            "last_active": datetime.utcnow().isoformat(),
        }
        normal = client.calculate_score(base)
        resolved = client.calculate_score(base | {"resolved": True})
        both = client.calculate_score(base | {"resolved": True, "digested": True})
        assert resolved < normal
        assert both < resolved

    run(scenario())
