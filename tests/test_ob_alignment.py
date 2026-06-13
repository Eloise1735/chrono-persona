from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import uuid

from server.ob_client import OBClient
from server.ob_decay import OBDecayEngine, OBDecaySettings
from server.environment import TemplateEnvironmentGenerator
from server.models import EventAnchor, StateSnapshot
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


class FakeMemory:
    async def upsert_event_vector(self, event_id: int):
        return True


class FakePromptManager:
    async def get_system_prompt(self):
        return "system"

    async def get_prompt(self, key: str):
        return (
            "meta={group_meta}\nfragments={fragments_text}\ndedup={dedup_context}\n"
            "Return JSON."
        )


class FakeRollupLLM:
    async def chat(self, messages, **kwargs):
        return json.dumps(
            {
                "title": "晨间节奏债务聚合",
                "objective": "凯尔希连续处理晨间观察、任务反馈和身体延迟信号，将悬置事项推进为可记录的生活节点。",
                "impression": "这组碎片显示她的判断节奏被未完成事项持续牵引。",
                "detail_hooks": ["未发出的短消息", "右腕延迟复测"],
                "open_loop": "后续仍需确认复测结果与任务反馈。",
                "keywords": ["晨间观察", "右腕延迟", "任务反馈"],
                "categories": ["生活", "工作"],
                "rollup_content": "凯尔希连续面对晨间观察、任务反馈和右腕延迟信号，将原本散落的待办推进成一个清晰的生活事件。她开始重新排序任务，并把复测与反馈保留为后续观察线索。",
            },
            ensure_ascii=False,
        )


class FailingRollupLLM:
    async def chat(self, messages, **kwargs):
        raise RuntimeError("simulated rollup failure")


class FakeEventDB:
    def __init__(self):
        self.events: list[EventAnchor] = []

    async def get_all_events(self, *args, **kwargs):
        return list(self.events)

    async def insert_event(self, event: EventAnchor):
        event.id = len(self.events) + 1
        self.events.append(event)
        return event.id

    async def get_event_by_id(self, event_id: int):
        for event in self.events:
            if event.id == event_id:
                return event
        return None


class FakeSnapshotLLM:
    def begin_usage_tracking(self):
        return None

    def end_usage_tracking(self):
        return {"requests": 0}


async def _client(name: str):
    root = Path("data") / "ob_unit_tests" / f"{name}_{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=True)
    return OBClient(root / "ob")


def test_checkpoint_limit_latest_keeps_recent_three():
    start = datetime(2026, 1, 1, 0, 0, 0)
    checkpoints = [start + timedelta(days=i) for i in range(10)]

    limited, meta = StateMachine._limit_checkpoints(
        checkpoints,
        max_steps=3,
        mode="latest",
    )

    assert limited == checkpoints[-3:]
    assert meta["checkpoint_limit_mode"] == "latest"
    assert meta["skipped_older_checkpoint_count"] == 7
    assert meta["remaining_checkpoint_count"] == 0
    assert meta["limited_by_max_steps"] is True


def test_checkpoint_limit_latest_keeps_all_when_under_limit():
    start = datetime(2026, 1, 1, 0, 0, 0)
    checkpoints = [start + timedelta(days=i) for i in range(2)]

    limited, meta = StateMachine._limit_checkpoints(
        checkpoints,
        max_steps=3,
        mode="latest",
    )

    assert limited == checkpoints
    assert meta["skipped_older_checkpoint_count"] == 0
    assert meta["remaining_checkpoint_count"] == 0
    assert meta["limited_by_max_steps"] is False


def test_checkpoint_limit_oldest_preserves_existing_behavior():
    start = datetime(2026, 1, 1, 0, 0, 0)
    checkpoints = [start + timedelta(days=i) for i in range(10)]

    limited, meta = StateMachine._limit_checkpoints(
        checkpoints,
        max_steps=3,
        mode="oldest",
    )

    assert limited == checkpoints[:3]
    assert meta["checkpoint_limit_mode"] == "oldest"
    assert meta["skipped_older_checkpoint_count"] == 0
    assert meta["remaining_checkpoint_count"] == 7
    assert meta["limited_by_max_steps"] is True


def test_snapshot_scheduler_tick_limits_catchup_to_latest_three(monkeypatch):
    async def scenario():
        sm = StateMachine.__new__(StateMachine)
        sm._advance_lock = asyncio.Lock()
        sm.snapshot_llm = FakeSnapshotLLM()

        now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone(timedelta(hours=8)))
        latest_time = now - timedelta(days=10)
        latest_snapshot = StateSnapshot(
            id=1,
            created_at="2026-05-15T04:00:00Z",
            content="previous snapshot",
            environment="{}",
        )
        captured = {}

        class FakeDB:
            async def get_latest_snapshot(self):
                return latest_snapshot

        async def fake_advance_until_locked(**kwargs):
            captured.update(kwargs)
            planned = [latest_time + timedelta(days=i) for i in range(1, 11)]
            due, meta = StateMachine._limit_checkpoints(
                planned,
                max_steps=kwargs.get("max_steps"),
                mode=kwargs.get("checkpoint_limit_mode", "oldest"),
            )
            return {
                "content": "advanced snapshot",
                "schedule": {
                    **meta,
                    "planned_checkpoint_count": len(planned),
                    "checkpoint_count": len(due),
                    "checkpoint_times_cst": [t.isoformat() for t in due],
                    "generated_snapshots": [{"id": 10}],
                },
            }

        sm.db = FakeDB()
        sm._get_snapshot_scheduler_enabled = lambda: _async_value(True)
        sm._get_snapshot_scheduler_interval_sec = lambda: _async_value(60)
        sm._get_snapshot_scheduler_night_pause_state = lambda: _async_value(None)
        sm._get_effective_active_conversation_time_claim = lambda: _async_value(None)
        sm._refresh_slowlines = lambda: _async_value(None)
        sm._resolve_progress_baseline = lambda snapshot, fallback: latest_time
        sm._get_min_time_unit_timedelta = lambda: _async_value(timedelta(days=1))
        sm._get_snapshot_catchup_max_steps = lambda: _async_value(99)
        sm._snapshot_environment_dict = lambda snapshot: {}
        sm._advance_until_locked = fake_advance_until_locked
        sm._run_automation = lambda trigger: _async_value({"ran": True, "trigger": trigger})
        sm._persist_automation_report = lambda report, llm_usage: _async_value(None)

        monkeypatch.setattr("server.state_machine.shanghai_now", lambda: now)

        result = await sm.run_snapshot_scheduler_tick()

        assert captured["checkpoint_limit_mode"] == "latest"
        assert captured["max_steps"] == 3
        assert result["checkpoint_limit_mode"] == "latest"
        assert result["planned_checkpoint_count"] == 10
        assert result["checkpoint_count"] == 3
        assert result["remaining_checkpoint_count"] == 0
        assert result["skipped_older_checkpoint_count"] == 7
        assert result["catchup_max_steps_per_tick"] == 3
        assert result["configured_catchup_max_steps_per_tick"] == 99
        assert result["interruption_hours"] == result["lag_hours"]

    run(scenario())


async def _async_value(value):
    return value


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
        # 显式 include_character_life=True 才能验证 max_character_life=1 的 cap 机制；
        # 否则 breath 默认排除 character_life，这条测试断言的 cap 行为根本无法触发。
        results = await client.breath(domain="feel", limit=5, include_character_life=True)
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


def test_breath_bundle_returns_three_grouped_lists_without_touch():
    async def scenario():
        client = await _client("bundle")
        dynamic_id = await client.hold("dynamic one", importance=7, created="2026-01-01T00:00:00")
        pinned_id = await client.hold(
            "pinned principle",
            bucket_type="permanent",
            pinned=True,
            created="2026-01-03T00:00:00",
        )
        feel_id = await client.hold("feel one", bucket_type="feel", created="2026-01-02T00:00:00")
        before_dynamic = await client.get(dynamic_id)
        before_feel = await client.get(feel_id)
        result = await client.breath_bundle()
        after_dynamic = await client.get(dynamic_id)
        after_feel = await client.get(feel_id)
        # New structure: three keys, no ordinary/feel/guidance.
        assert set(result.keys()) == {"personal", "relational", "free"}
        all_ids = [
            item["id"]
            for group in ("personal", "relational", "free")
            for item in result[group]
        ]
        # pinned principles never leak into any group — they go through get_current_state.
        assert pinned_id not in all_ids
        # Natural surfacing must not touch buckets (no last_active bump).
        assert after_dynamic.metadata["last_active"] == before_dynamic.metadata["last_active"]
        assert after_feel.metadata["last_active"] == before_feel.metadata["last_active"]

    run(scenario())


def test_breath_bundle_personal_group_has_three_slots_active_rollup_feel():
    async def scenario():
        client = await _client("bundle_personal_pins")
        # ① 当下进行：environment_event_summary 固定 bucket
        active_id = await client.hold(
            "- 华法琳会议结束\n- 行政复核完成",
            domain=["character_life"],
            tags=["environment_event_summary", "character_life"],
            importance=7,
            created="2026-01-04T00:00:00",
            extra_metadata={"source_kind": "environment_event_summary"},
        )
        # ② 近期总结：environment_life_rollup 聚合事件
        rollup_id = await client.hold(
            "上午华法琳会议结束后，凯尔希复盘行政流程，随后……（聚合事件叙述）",
            domain=["character_life", "work"],
            tags=["environment_life_rollup", "character_life"],
            importance=6,
            created="2026-01-04T06:00:00",
            extra_metadata={"source_kind": "environment_life_rollup"},
        )
        # ③ 当下感受：character_life feel
        feel_id = await client.hold(
            "握笔时右腕的钝感再次浮现，像被什么东西按住。",
            bucket_type="feel",
            domain=["character_life"],
            tags=["snapshot", "feel"],
            created="2026-01-03T00:00:00",
            extra_metadata={"source_kind": "state_snapshot"},
        )
        # 干扰项：pinned principle + raw fragment 都不能进 personal
        principle_id = await client.hold(
            "stable principle bound to character_life",
            bucket_type="permanent",
            domain=["character_life"],
            pinned=True,
            created="2026-01-05T00:00:00",
        )
        fragment_id = await client.hold(
            "raw environment fragment should not surface",
            domain=["character_life"],
            tags=["environment_life_fragment", "character_life"],
            importance=10,
            created="2026-01-06T00:00:00",
            extra_metadata={"source_kind": "environment_life_fragment"},
        )
        # 关系侧填充
        await client.hold("relational dynamic A", importance=8, created="2026-01-02T00:00:00")
        await client.hold("relational dynamic B", importance=7, created="2026-01-02T01:00:00")
        await client.hold("relational feel A", bucket_type="feel", created="2026-01-02T02:00:00")

        result = await client.breath_bundle()
        personal = result["personal"]
        all_ids = {
            item["id"]
            for group in ("personal", "relational", "free")
            for item in result[group]
        }

        # 三槽并列，顺序固定：当下进行 / 近期总结 / 当下感受
        assert len(personal) == 3
        assert personal[0]["id"] == active_id
        assert personal[0]["metadata"]["slot_role"] == "personal_event_active"
        assert personal[0]["content"].startswith("【个人事件·当下进行】")
        assert personal[1]["id"] == rollup_id
        assert personal[1]["metadata"]["slot_role"] == "personal_event_rollup"
        assert personal[1]["content"].startswith("【个人事件·近期总结】")
        assert personal[2]["id"] == feel_id
        assert personal[2]["metadata"]["slot_role"] == "current_feeling"
        assert personal[2]["content"].startswith("【当下感受】")
        # 干扰项不进任何组
        assert principle_id not in all_ids
        assert fragment_id not in all_ids
        # marker 不再伪造 score=999
        for item in personal:
            assert item["score"] != 999.0

    run(scenario())


def test_breath_personal_returns_only_personal_three_slots():
    """breath_personal() 是 breath_bundle 的第一层切片，只返回 personal 三槽。"""
    async def scenario():
        client = await _client("breath_personal")
        active_id = await client.hold(
            "- 华法琳会议结束\n- 行政复核完成",
            domain=["character_life"],
            tags=["environment_event_summary", "character_life"],
            importance=7,
            created="2026-01-04T00:00:00",
            extra_metadata={"source_kind": "environment_event_summary"},
        )
        rollup_id = await client.hold(
            "上午华法琳会议结束后，凯尔希复盘行政流程……",
            domain=["character_life", "work"],
            tags=["environment_life_rollup", "character_life"],
            importance=6,
            created="2026-01-04T06:00:00",
            extra_metadata={"source_kind": "environment_life_rollup"},
        )
        feel_id = await client.hold(
            "握笔时右腕的钝感再次浮现。",
            bucket_type="feel",
            domain=["character_life"],
            tags=["snapshot", "feel"],
            created="2026-01-03T00:00:00",
            extra_metadata={"source_kind": "state_snapshot"},
        )
        # 关系侧 + free 侧的内容不应出现在 personal-only 返回里
        relational_id = await client.hold(
            "relational dynamic should not surface",
            importance=8,
            created="2026-01-02T00:00:00",
        )

        result = await client.breath_personal()
        # 顶层只有 personal 一个字段
        assert set(result.keys()) == {"personal"}
        personal = result["personal"]
        ids = [item["id"] for item in personal]
        assert ids == [active_id, rollup_id, feel_id]
        assert relational_id not in ids
        # marker 仍然不污染 score
        for item in personal:
            assert item["score"] != 999.0
        # 不 touch
        before = await client.get(active_id)
        await client.breath_personal()
        after = await client.get(active_id)
        assert before.metadata["last_active"] == after.metadata["last_active"]

    run(scenario())


def test_breath_personal_marker_warns_when_speculative_about_user():
    """speculative_about_user=True 的 bucket 在 personal 槽要带 ⚠ 提示。"""
    async def scenario():
        client = await _client("bundle_speculative_warning")
        await client.hold(
            "- 凯尔希预计泳琳此时仍在书房（推测违规：实际写成了'泳琳走到书桌前'）",
            domain=["character_life"],
            tags=["environment_event_summary", "character_life"],
            importance=7,
            created="2026-01-04T00:00:00",
            extra_metadata={
                "source_kind": "environment_event_summary",
                "user_reference_mode": "speculative",
                "speculative_about_user": True,
            },
        )
        # 对照组：非 speculative 不应出现 ⚠
        await client.hold(
            "凯尔希聚合事件叙述（不涉及泳琳）",
            domain=["character_life"],
            tags=["environment_life_rollup", "character_life"],
            importance=6,
            created="2026-01-04T06:00:00",
            extra_metadata={
                "source_kind": "environment_life_rollup",
                "user_reference_mode": "none",
                "speculative_about_user": False,
            },
        )
        result = await client.breath_personal()
        personal = result["personal"]
        active_slot = next(item for item in personal if item["metadata"]["slot_role"] == "personal_event_active")
        rollup_slot = next(item for item in personal if item["metadata"]["slot_role"] == "personal_event_rollup")
        assert "⚠" in active_slot["content"]
        assert "请在对话中确认" in active_slot["content"]
        assert "⚠" not in rollup_slot["content"]

    run(scenario())


def test_plan_engine_open_loop_pool_dedup_and_age_window():
    """plan_engine._build_open_loop_pool_text 应聚合最近 48h fragment.open_loop、去重、按时间排序。"""
    from server.plan_engine import PlanEngine
    from datetime import datetime as _dt, timedelta as _td

    async def scenario():
        client = await _client("plan_open_loop_pool")
        now = _dt.utcnow()
        # 新鲜 fragment（2h 前），含 open_loop
        await client.hold(
            "fragment recent",
            tags=["environment_life_fragment", "character_life"],
            domain=["character_life"],
            created=(now - _td(hours=2)).isoformat(),
            extra_metadata={
                "source_kind": "environment_life_fragment",
                "open_loop": "阿米娅尚未回复 X 报告",
                "last_active": (now - _td(hours=2)).isoformat(),
            },
        )
        # 重复内容，更旧（10h 前）— 应被去重剔除
        await client.hold(
            "fragment dup",
            tags=["environment_life_fragment", "character_life"],
            domain=["character_life"],
            created=(now - _td(hours=10)).isoformat(),
            extra_metadata={
                "source_kind": "environment_life_fragment",
                "open_loop": "阿米娅尚未回复 X 报告",
                "last_active": (now - _td(hours=10)).isoformat(),
            },
        )
        # 不同的新 open_loop
        await client.hold(
            "fragment other",
            tags=["environment_life_fragment", "character_life"],
            domain=["character_life"],
            created=(now - _td(hours=5)).isoformat(),
            extra_metadata={
                "source_kind": "environment_life_fragment",
                "open_loop": "右腕复测仍未完成",
                "last_active": (now - _td(hours=5)).isoformat(),
            },
        )
        # 超过 48h 窗口 — 应被剔除
        await client.hold(
            "fragment stale",
            tags=["environment_life_fragment", "character_life"],
            domain=["character_life"],
            created=(now - _td(hours=72)).isoformat(),
            extra_metadata={
                "source_kind": "environment_life_fragment",
                "open_loop": "三天前的某线索",
                "last_active": (now - _td(hours=72)).isoformat(),
            },
        )
        # 非 fragment 的 character_life bucket — 不应进入
        await client.hold(
            "not a fragment",
            tags=["environment_event_summary", "character_life"],
            domain=["character_life"],
            created=now.isoformat(),
            extra_metadata={
                "source_kind": "environment_event_summary",
                "open_loop": "不应该被池子收集",
            },
        )

        machine = object.__new__(StateMachine)
        machine.ob_client = client
        engine = object.__new__(PlanEngine)
        engine.state_machine = machine
        text = await engine._build_open_loop_pool_text(max_hours=48.0, max_items=8)
        assert "阿米娅尚未回复 X 报告" in text
        assert "右腕复测仍未完成" in text
        assert "三天前的某线索" not in text
        assert "不应该被池子收集" not in text
        # 去重：阿米娅那条只出现一次
        assert text.count("阿米娅尚未回复 X 报告") == 1

    run(scenario())


def test_render_previous_plan_reference_includes_thread_step():
    """续接判断依赖 thread/step——必须在昨日 plan 摘要里显式输出。"""
    from server.plan_engine import _render_previous_plan_reference, PLAN_SCHEMA_VERSION
    from server.models import PlanItem
    item = PlanItem(
        plan_id=1,
        hour_start=9,
        hour_end=10,
        activity="内务复盘",
        action_type="internal",
        reason="thread continuity",
        action_payload=json.dumps({
            "intended_objective": "推完上周遗留的内务整理",
            "progress_status": "advancing",
            "thread_id": "admin_review",
            "current_step": 3,
            "expected_steps": 4,
        }),
        status="done",
    )
    rendered = _render_previous_plan_reference(item, schema_version=PLAN_SCHEMA_VERSION)
    assert "thread=admin_review" in rendered
    assert "step=3/4" in rendered
    assert "progress=advancing" in rendered
    assert "[done]" in rendered

    # 没有 thread_id 的旧条目应优雅退化（不写 thread= 段）
    item_legacy = PlanItem(
        plan_id=1,
        hour_start=14,
        hour_end=15,
        activity="X",
        action_type="internal",
        reason="legacy",
        action_payload=json.dumps({"intended_objective": "legacy item"}),
        status="skipped",
    )
    rendered_legacy = _render_previous_plan_reference(item_legacy, schema_version=PLAN_SCHEMA_VERSION)
    assert "thread=" not in rendered_legacy
    assert "[skipped]" in rendered_legacy


def test_feel_breath_one_week_crystallization_protection():
    """dream 把昨天的 feel 标 crystallized 后，一周内仍应能浮现，超过一周才剔除。"""
    async def scenario():
        client = await _client("feel_crystal_window")
        now = datetime.utcnow()
        recent_id = await client.hold(
            "昨晚 crystallize 的 feel，今天仍应可浮现",
            bucket_type="feel",
            created=(now - timedelta(hours=12)).isoformat(),
            extra_metadata={
                "crystallized": True,
                "crystallized_at": (now - timedelta(hours=10)).isoformat(),
            },
        )
        old_id = await client.hold(
            "10 天前 crystallize 的 feel，应被剔除",
            bucket_type="feel",
            created=(now - timedelta(days=12)).isoformat(),
            extra_metadata={
                "crystallized": True,
                "crystallized_at": (now - timedelta(days=10)).isoformat(),
            },
        )
        # 控制组：未 crystallize 的旧 feel，应永远可浮现
        plain_id = await client.hold(
            "未 crystallize 的旧 feel",
            bucket_type="feel",
            created=(now - timedelta(days=30)).isoformat(),
        )
        result = await client._feel_breath(limit=10)
        ids = {b.id for b in result}
        assert recent_id in ids, "一周保护窗内的 crystallized feel 应仍浮现"
        assert old_id not in ids, "超过一周的 crystallized feel 应被剔除"
        assert plain_id in ids, "非 crystallized feel 应不受影响"

    run(scenario())


def test_breath_bundle_free_pool_excludes_pinned_and_protected_and_permanent():
    """free 池显式过滤 pinned/protected/permanent，避免 999 分霸榜。"""
    async def scenario():
        client = await _client("free_pool_defense")
        # 注入 pinned 和 permanent 类型 bucket
        pinned_id = await client.hold(
            "pinned principle",
            bucket_type="permanent",
            pinned=True,
            importance=10,
            created="2026-05-26T00:00:00",
        )
        # 普通低分 dynamic，应该有机会进 free
        for i in range(6):
            await client.hold(
                f"ordinary dynamic {i}",
                importance=3,
                created=f"2026-05-2{i % 9}T00:00:00",
            )
        result = await client.breath_bundle()
        all_ids = {
            item["id"]
            for group in ("personal", "relational", "free")
            for item in result[group]
        }
        assert pinned_id not in all_ids, "pinned/permanent 不应出现在 free 池（也不该霸榜）"

    run(scenario())


def test_breath_default_excludes_character_life_unless_explicitly_requested():
    """breath() 默认排除 character_life；显式传 domain 或 include_character_life=True 才包含。"""
    async def scenario():
        client = await _client("breath_default_excludes_char_life")
        char_id = await client.hold(
            "凯尔希自身生活流：今晨晨练",
            domain=["character_life"],
            importance=6,
            created="2026-05-25T08:00:00",
        )
        rel_id = await client.hold(
            "关系侧：与阿米娅的协作",
            importance=6,
            created="2026-05-25T08:00:00",
        )

        # 默认：仅出关系侧
        default_breath = await client.breath()
        ids = {b.id for b in default_breath}
        assert rel_id in ids
        assert char_id not in ids, "默认 breath 不应返回 character_life"

        # 显式 include_character_life=True：两者都出
        mixed = await client.breath(include_character_life=True)
        ids = {b.id for b in mixed}
        assert rel_id in ids
        assert char_id in ids

        # 显式 domain="character_life"：只出 character_life
        char_only = await client.breath(domain="character_life")
        ids = {b.id for b in char_only}
        assert char_id in ids
        assert rel_id not in ids, "domain=character_life 应只返回 character_life"

        # query 模式同样默认排除
        char_id2 = await client.hold(
            "凯尔希在书桌前整理笔记",
            domain=["character_life"],
            importance=6,
            created="2026-05-25T09:00:00",
        )
        rel_id2 = await client.hold(
            "罗德岛会议记录关于书桌整理",
            importance=6,
            created="2026-05-25T09:00:00",
        )
        q_default = await client.breath(query="书桌")
        ids = {b.id for b in q_default}
        assert rel_id2 in ids
        assert char_id2 not in ids, "query 检索默认也应排除 character_life"

    run(scenario())


def test_breath_date_window_filter_inclusive_bounds():
    """breath date_from/date_to 应按 metadata.created 过滤，YYYY-MM-DD 单日窗口能包含当天 23:59。"""
    async def scenario():
        client = await _client("breath_date_window")
        await client.hold("old 2026-05-20", importance=5, created="2026-05-20T10:00:00")
        await client.hold("target 2026-05-25 morning", importance=5, created="2026-05-25T08:00:00")
        await client.hold("target 2026-05-25 night", importance=5, created="2026-05-25T22:00:00")
        await client.hold("after 2026-05-26", importance=5, created="2026-05-26T05:00:00")

        # 单日窗口（date_to 自动延展到 23:59:59）
        same_day = await client.breath(date_from="2026-05-25", date_to="2026-05-25")
        contents = [b.content for b in same_day]
        assert any("morning" in c for c in contents)
        assert any("night" in c for c in contents)
        assert all("2026-05-20" not in c and "2026-05-26" not in c for c in contents)

        # 单边：只 date_from
        from_only = await client.breath(date_from="2026-05-25")
        contents = [b.content for b in from_only]
        assert all("2026-05-20" not in c for c in contents)

        # 单边：只 date_to
        to_only = await client.breath(date_to="2026-05-25")
        contents = [b.content for b in to_only]
        assert all("2026-05-26" not in c for c in contents)

    run(scenario())


def test_stamp_patch_with_replaced_info_preserves_original_activity_and_thread():
    """replan 时旧条目被丢弃前，其 activity/objective/thread_id 必须落到新 patch.action_payload。"""
    from server.plan_engine import PlanEngine
    from server.models import PlanItem
    old = PlanItem(
        plan_id=1,
        hour_start=14,
        hour_end=16,
        activity="内务复盘",
        action_type="internal",
        action_payload=json.dumps({
            "intended_objective": "推完上周遗留的内务整理",
            "thread_id": "admin_review",
            "current_step": 2,
            "expected_steps": 4,
        }),
        status="pending",
    )
    new_patch = PlanItem(
        plan_id=1,
        hour_start=14,
        hour_end=16,
        activity="急诊配合",
        action_type="internal",
        action_payload=json.dumps({
            "intended_objective": "顶上突发急诊调度",
        }),
        status="pending",
        source_kind="replan",
    )
    PlanEngine._stamp_patch_with_replaced_info(new_patch, [old])
    payload = json.loads(new_patch.action_payload)
    replaces = payload["replan_replaces"]
    assert isinstance(replaces, list)
    assert len(replaces) == 1
    r = replaces[0]
    assert r["activity"] == "内务复盘"
    assert r["objective"] == "推完上周遗留的内务整理"
    assert r["thread_id"] == "admin_review"
    assert r["hour_start"] == 14 and r["hour_end"] == 16
    # 原 patch 的字段不应被破坏
    assert payload["intended_objective"] == "顶上突发急诊调度"


def test_stamp_patch_with_replaced_info_handles_multiple_old_items():
    """新 patch 跨过多个旧 pending（例如 13-17 替代三段），全部摘要进入 replan_replaces。"""
    from server.plan_engine import PlanEngine
    from server.models import PlanItem
    olds = [
        PlanItem(plan_id=1, hour_start=h, hour_end=h+1, activity=f"任务{h}",
                 action_type="internal",
                 action_payload=json.dumps({"intended_objective": f"obj{h}", "thread_id": f"t{h}"}),
                 status="pending")
        for h in (13, 14, 15)
    ]
    patch = PlanItem(plan_id=1, hour_start=13, hour_end=17, activity="临时大块",
                     action_type="internal", action_payload="{}", status="pending",
                     source_kind="replan")
    PlanEngine._stamp_patch_with_replaced_info(patch, olds)
    payload = json.loads(patch.action_payload)
    assert len(payload["replan_replaces"]) == 3
    activities = [r["activity"] for r in payload["replan_replaces"]]
    assert activities == ["任务13", "任务14", "任务15"]


def test_env_strong_disturbance_judgement_for_auto_replan():
    """env 强扰动判定：决定是否在 _advance_until_locked 末尾触发 maybe_replan。"""
    machine = object.__new__(StateMachine)
    # 无扰动 → False
    assert machine._env_has_strong_disturbance(None) is False
    assert machine._env_has_strong_disturbance({}) is False
    assert machine._env_has_strong_disturbance({"disturbance_title": "", "disturbance_id": 0}) is False
    # 有扰动但 effect 是弱 → False
    for weak in ["none", "on_track", "inward_digging", "", "  ON_TRACK  "]:
        env = {"disturbance_id": 7, "disturbance_title": "X", "disturbance_schedule_effect": weak}
        assert machine._env_has_strong_disturbance(env) is False, f"weak={weak!r}"
    # 强扰动 → True
    for strong in ["interrupted", "delayed", "replaced_by_conversation", "unexpected_insert"]:
        env = {"disturbance_id": 7, "disturbance_title": "X", "disturbance_schedule_effect": strong}
        assert machine._env_has_strong_disturbance(env) is True, f"strong={strong!r}"


def test_format_disturbance_replan_context_includes_title_effect_and_truncates_context():
    long_ctx = "X" * 1000
    env = {
        "disturbance_title": "急诊紧急会诊",
        "disturbance_schedule_effect": "interrupted",
        "disturbance_context": long_ctx,
    }
    text = StateMachine._format_disturbance_replan_context(env)
    assert "急诊紧急会诊" in text
    assert "schedule_effect=interrupted" in text
    # context 被裁到 600 字 + 省略号
    assert "X" * 600 in text
    assert "X" * 700 not in text
    assert text.endswith("…")
    # 空扰动也能优雅退化
    text2 = StateMachine._format_disturbance_replan_context({})
    assert "未命名扰动" in text2


def test_compute_user_offline_for_checkpoint_low_activity_threshold():
    """offline_hours 计算 + low_activity_mode 阈值（默认 5h）判定。"""
    from datetime import datetime as _dt, timedelta as _td
    from server.state_machine import StateMachine
    machine = object.__new__(StateMachine)
    checkpoint = _dt(2026, 5, 27, 17, 0, 0)
    # 无 conversation_end_instant → 视为始终在线
    assert machine._compute_user_offline_for_checkpoint(checkpoint, None) == (0.0, False)
    # 离线 3h → normal
    offline, low = machine._compute_user_offline_for_checkpoint(checkpoint, checkpoint - _td(hours=3))
    assert offline == 3.0
    assert low is False
    # 刚好达到 5h 阈值 → 触发 low_activity
    offline, low = machine._compute_user_offline_for_checkpoint(checkpoint, checkpoint - _td(hours=5))
    assert offline == 5.0
    assert low is True
    # 离线 10h → 仍为 low_activity
    offline, low = machine._compute_user_offline_for_checkpoint(checkpoint, checkpoint - _td(hours=10))
    assert offline == 10.0
    assert low is True
    # 时序异常（checkpoint 早于 conversation_end）→ 不为负
    offline, low = machine._compute_user_offline_for_checkpoint(checkpoint, checkpoint + _td(hours=1))
    assert offline == 0.0
    assert low is False


def test_environment_parser_extracts_user_reference_mode():
    """parser 应从 [Summary] 段提取 User reference mode 自评字段。"""
    from server.environment import TemplateEnvironmentGenerator
    summary_with_mode = (
        "Core focus: 凯尔希复盘巡诊路线\n"
        "Open loop: 阿米娅尚未回复\n"
        "Plan delta: on_track\n"
        "User reference mode: anticipation （凯尔希猜测泳琳此时仍在写作）"
    )
    assert TemplateEnvironmentGenerator._extract_user_reference_mode(summary_with_mode) == "anticipation"

    # speculative 自评应被识别
    summary_speculative = "Core focus: x\nUser reference mode: speculative"
    assert TemplateEnvironmentGenerator._extract_user_reference_mode(summary_speculative) == "speculative"

    # 缺失或不合法值返回 unknown
    assert TemplateEnvironmentGenerator._extract_user_reference_mode("") == "unknown"
    assert TemplateEnvironmentGenerator._extract_user_reference_mode("Core focus: x") == "unknown"
    assert TemplateEnvironmentGenerator._extract_user_reference_mode("User reference mode: bogus_value") == "unknown"


def test_breath_bundle_personal_active_slot_falls_back_when_no_summary_bucket():
    """当没有 environment_event_summary 时，当下进行槽回退到其他 character_life dynamic。"""
    async def scenario():
        client = await _client("bundle_personal_fallback")
        fallback_id = await client.hold(
            "凯尔希手记：今晨复盘巡诊路线。",
            domain=["character_life"],
            tags=["character_life"],
            importance=6,
            created="2026-01-04T00:00:00",
        )
        rollup_id = await client.hold(
            "聚合事件叙述：晨间巡诊→午后复核……",
            domain=["character_life"],
            tags=["environment_life_rollup", "character_life"],
            importance=6,
            created="2026-01-04T06:00:00",
            extra_metadata={"source_kind": "environment_life_rollup"},
        )
        result = await client.breath_bundle()
        personal = result["personal"]
        assert personal[0]["id"] == fallback_id
        assert personal[0]["metadata"]["slot_role"] == "personal_event_active"
        assert personal[1]["id"] == rollup_id
        assert personal[1]["metadata"]["slot_role"] == "personal_event_rollup"

    run(scenario())


def test_environment_parser_accepts_event_summary_section():
    raw = """[Environment Body]
body

---
[Summary]
Core focus: focus

---
[Retrieval Summary]
retrieval

---
[Event Summary]
- meeting ended
- review completed"""
    body, summary, retrieval, event_summary = TemplateEnvironmentGenerator.parse_environment_llm_output(raw)
    assert body == "body"
    assert "Core focus" in summary
    assert retrieval == "retrieval"
    assert "- meeting ended" in event_summary


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


def test_environment_life_fragment_compacts_environment_context():
    machine = object.__new__(StateMachine)
    env = {
        "summary": (
            "09:52，凯尔希在医疗部走廊面对晨间观察与鼻炎药提醒两项过期计划项，"
            "与华法琳完成简短临床讨论，随后主动发出短消息打破等待；右腕神经延迟持续，十一点复测不可再推。"
        ),
        "retrieval_summary": "",
        "activity": "不应优先使用的长正文",
        "plan_delta": "Plan delta: 这是一段很长的旧日程与对话上下文，不应该进入碎片正文。",
        "disturbance_context": (
            "[endogenous_reveal/task] 晨间观察的节奏债务显形 "
            "Open thread: 要继续拖延，还是正式调整计划，必须尽快决定。"
        ),
        "recent_disturbances": "Recent disturbances: 旧扰动列表不应该进入正文。",
    }
    fragment = machine._build_environment_life_fragment(
        env,
        checkpoint_cst="2026-05-22T09:52:51+08:00",
        snapshot_id=442,
    )
    assert fragment["content"]
    assert len(fragment["content"]) <= 320
    assert "Plan delta:" not in fragment["content"]
    assert "Recent disturbances:" not in fragment["content"]
    assert fragment["snapshot_id"] == 442
    assert fragment["checkpoint_cst"] == "2026-05-22T09:52:51+08:00"
    assert fragment["group_key"].startswith("environment_life:")
    assert fragment["open_loop"]


def test_environment_life_fragment_is_recent_event_candidate_with_snapshot_id():
    async def scenario():
        client = await _client("fragment_candidate")
        fragment_id = await client.hold(
            "凯尔希处理晨间观察并安排右腕复测。",
            name="fragment",
            tags=["environment_life_fragment", "character_life"],
            domain=["character_life"],
            extra_metadata={
                "source": "snapshot_scheduler",
                "source_kind": "environment_life_fragment",
                "snapshot_id": 1,
                "checkpoint_cst": "2026-05-22T09:00:00+08:00",
            },
        )
        snapshot_id = await client.hold(
            "凯尔希状态快照不应进入近期事件候选。",
            name="snapshot",
            tags=["snapshot", "environment"],
            domain=["character_life"],
            extra_metadata={
                "source": "snapshot_scheduler",
                "source_kind": "state_snapshot",
                "snapshot_id": 2,
                "checkpoint_cst": "2026-05-22T09:00:00+08:00",
            },
        )
        machine = object.__new__(StateMachine)
        machine.ob_client = client
        items = await machine._ob_breath_event_dicts_for_environment(
            query="凯尔希 晨间观察 右腕复测",
            limit=5,
        )
        ids = {item["bucket_id"] for item in items}
        assert fragment_id in ids
        assert snapshot_id not in ids

    run(scenario())


def test_environment_life_rollup_auto_creates_event_and_bucket():
    async def scenario():
        client = await _client("fragment_rollup")
        group_key = "environment_life:medical"
        source_ids = []
        for idx in range(3):
            source_ids.append(
                await client.hold(
                    f"凯尔希第{idx}次处理晨间观察、任务反馈和右腕复测线索。",
                    name=f"fragment {idx}",
                    tags=["environment_life_fragment", "character_life"],
                    domain=["character_life"],
                    extra_metadata={
                        "source_kind": "environment_life_fragment",
                        "memory_role": "recent_life_event_candidate",
                        "life_scope": "character_life",
                        "life_theme": "medical",
                        "group_key": group_key,
                        "open_loop": "右腕复测仍未完成",
                        "plan_effect": "delayed",
                    },
                )
            )
        machine = object.__new__(StateMachine)
        machine.ob_client = client
        machine.db = FakeEventDB()
        machine.memory = FakeMemory()
        machine.prompt_manager = FakePromptManager()
        machine.snapshot_llm = FakeRollupLLM()

        async def noop_refresh(event):
            return None

        machine._refresh_related_slowline_from_event = noop_refresh
        result = await machine._process_pending_environment_life_rollups(
            group_key=group_key,
            reason="test",
        )
        assert result and result[0]["status"] == "created"
        assert len(machine.db.events) == 1
        buckets = await client.list_buckets(include_archive=False)
        rollups = [
            bucket for bucket in buckets
            if bucket.metadata.get("source_kind") == "environment_life_rollup"
        ]
        assert len(rollups) == 1
        for source_id in source_ids:
            source = await client.get(source_id)
            assert source.metadata["digested"] is True
            assert source.metadata["resolved"] is True
            assert source.metadata["linked_event_id"] == 1
            assert source.metadata["rollup_bucket_id"] == rollups[0].id

    run(scenario())


def test_environment_life_rollup_failure_retries_then_falls_back():
    async def scenario():
        client = await _client("fragment_rollup_retry")
        group_key = "environment_life:work"
        source_ids = []
        for idx in range(3):
            source_ids.append(
                await client.hold(
                    f"凯尔希第{idx}次处理上游任务反馈，仍需等待阿米娅确认。",
                    name=f"fragment retry {idx}",
                    tags=["environment_life_fragment", "character_life"],
                    domain=["character_life"],
                    extra_metadata={
                        "source_kind": "environment_life_fragment",
                        "life_theme": "work",
                        "group_key": group_key,
                        "open_loop": "阿米娅确认仍未到账",
                        "plan_effect": "delayed",
                    },
                )
            )
        machine = object.__new__(StateMachine)
        machine.ob_client = client
        machine.db = FakeEventDB()
        machine.memory = FakeMemory()
        machine.prompt_manager = FakePromptManager()
        machine.snapshot_llm = FailingRollupLLM()

        async def noop_refresh(event):
            return None

        machine._refresh_related_slowline_from_event = noop_refresh
        first = await machine._process_pending_environment_life_rollups(group_key=group_key, reason="test")
        assert first == []
        assert len(machine.db.events) == 0
        source = await client.get(source_ids[0])
        assert source.metadata["rollup_attempts"] == 1
        assert not source.metadata.get("digested")

        second = await machine._process_pending_environment_life_rollups(group_key=group_key, reason="test")
        assert second and second[0]["status"] == "created"
        assert len(machine.db.events) == 1
        for source_id in source_ids:
            source = await client.get(source_id)
            assert source.metadata["digested"] is True
            assert source.metadata["resolved"] is True

    run(scenario())


def test_injectable_context_omits_feel_because_breath_bundle_handles_it():
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
        assert "【近期 feel】" not in injectable
        assert "我留下来的第一人称 feel。" not in injectable
        assert "【当前状态快照】" not in injectable
        assert "【当前日程主条目】" not in injectable
        assert "【稳定原则结晶】" in injectable
        assert "（暂无稳定原则结晶）" in injectable
        assert "不应进入注入的当前快照。" not in injectable

    run(scenario())


def test_injectable_context_includes_recent_pinned_principles():
    async def scenario():
        client = await _client("injectable_pinned")
        await client.hold(
            "旧原则",
            bucket_type="permanent",
            pinned=True,
            name="旧原则",
            created="2026-01-01T00:00:00",
            extra_metadata={
                "principle_title": "旧原则",
                "principle_injection": "旧原则注入。",
            },
        )
        await client.hold(
            "标题：在场原则\n核心原则：计算与在场可以同时存在。",
            bucket_type="permanent",
            pinned=True,
            name="在场原则",
            created="2026-01-02T00:00:00",
            extra_metadata={
                "principle_title": "在场不是任务",
                "principle_injection": "照护中的计算仍然必要，但它可以和身体层面的在场同时发生。",
            },
        )
        machine = object.__new__(StateMachine)
        machine.ob_client = client
        machine.plan_engine = None

        async def key_records_context(limit: int = 5):
            return "（暂无近期关键记录）"

        machine._build_recent_key_records_context = key_records_context
        injectable = await machine._build_injectable_context("ignored")
        assert "【稳定原则结晶】" in injectable
        assert "在场不是任务" in injectable
        assert "照护中的计算仍然必要" in injectable
        assert "旧原则注入" in injectable

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
        # environment_trace 是 character_life domain；breath 默认排除 character_life，
        # 此处显式 include_character_life=True 验证它能被浮现的能力。
        surfaced = await client.breath(limit=5, include_character_life=True)
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
        assert "commit_feel_crystal" in result["text"]
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
            principle_title="Stable principle",
            principle_card={
                "principle": "The repeated feeling should become a stable stance.",
                "response_rule": "When the pattern returns, answer from the stance first.",
                "anchors": ["first anchor", "second anchor"],
                "avoid": "Do not flatten the feeling into analysis.",
            },
            principle_injection="Remember the stable stance before reacting.",
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
        assert principle.metadata["principle_title"] == "Stable principle"
        assert principle.metadata["principle_injection"] == "Remember the stable stance before reacting."
        assert principle.metadata["principle_card"]["principle"].startswith("The repeated feeling")
        assert "标题：Stable principle" in principle.content
        assert "回应规则：" in principle.content
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


def test_crystallize_principle_does_not_create_feel_and_is_idempotent():
    async def scenario():
        client = await _client("principle_idempotent")
        ids = [
            await client.hold("first source feel", bucket_type="feel"),
            await client.hold("second source feel", bucket_type="feel"),
        ]
        kwargs = {
            "mode": "principle",
            "principle_content": "A stable principle from these sources.",
            "feel_ids": ids,
        }
        first = await client.crystallize_feel(**kwargs)
        second = await client.crystallize_feel(**kwargs)
        assert first["principle_bucket_id"] == second["principle_bucket_id"]
        assert "feel_bucket_id" not in first
        assert "feel_bucket_id" not in second
        buckets = await client.list_buckets(include_archive=False)
        crystal_principles = [
            bucket for bucket in buckets
            if bucket.metadata.get("source_kind") == "feel_crystal"
            and bucket.metadata.get("crystal_output") == "principle"
        ]
        crystal_feels = [
            bucket for bucket in buckets
            if bucket.metadata.get("source_kind") == "feel_crystal"
            and bucket.metadata.get("crystal_output") == "feel"
        ]
        assert len(crystal_principles) == 1
        assert crystal_feels == []
        assert second["deduped"] is True

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
        # feel aging: a fresh feel starts near the historic 50 plateau, decays
        # gently with age, and a crystallized feel sinks faster than an
        # uncrystallized one of the same age. (Surfacing is unaffected — breath
        # overrides feel score to 50 at selection; this only drives archival.)
        now_iso = datetime.utcnow().isoformat()
        fresh_feel = client.calculate_score({"type": "feel", "created": now_iso, "last_active": now_iso})
        assert abs(fresh_feel - 50.0) < 0.5
        old_iso = (datetime.utcnow() - timedelta(days=120)).isoformat()
        old_feel = client.calculate_score({"type": "feel", "created": old_iso, "last_active": old_iso})
        assert old_feel < fresh_feel
        crystallized_old = client.calculate_score(
            {"type": "feel", "created": old_iso, "last_active": old_iso, "crystallized": True}
        )
        assert crystallized_old < old_feel
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


def test_feel_decay_is_arousal_weighted():
    async def scenario():
        client = await _client("feel_arousal_decay")
        old_iso = (datetime.utcnow() - timedelta(days=120)).isoformat()
        flat = client.calculate_score(
            {"type": "feel", "created": old_iso, "last_active": old_iso, "arousal": 0.0}
        )
        deep = client.calculate_score(
            {"type": "feel", "created": old_iso, "last_active": old_iso, "arousal": 0.9}
        )
        # An intense old feel decays slower, so it scores higher than a flat one
        # of the same age (a deep mark lingers).
        assert deep > flat

        # Diagnostic: higher arousal => smaller effective lambda => longer life.
        flat_diag = client.feel_decay_diagnostic({"type": "feel", "arousal": 0.0})
        deep_diag = client.feel_decay_diagnostic({"type": "feel", "arousal": 0.9})
        assert flat_diag["effective_lambda"] == 0.04
        assert deep_diag["effective_lambda"] < flat_diag["effective_lambda"]
        assert deep_diag["half_life_days"] > flat_diag["half_life_days"]
        assert deep_diag["archive_days"] > flat_diag["archive_days"]
        # k<1 keeps a deep mark mortal (not turned into a de-facto permanent).
        assert deep_diag["effective_lambda"] > 0
        # format_buckets surfaces the diagnostic for the dashboard.
        fid = await client.hold("an intense moment", bucket_type="feel",
                                extra_metadata={"arousal": 0.9})
        formatted = client.format_buckets([await client.get(fid)])
        assert "feel_decay" in formatted[0]
        assert formatted[0]["feel_decay"]["half_life_days"] > flat_diag["half_life_days"]

    run(scenario())


def test_find_merge_candidates_surfaces_near_duplicate_but_does_not_merge():
    async def scenario():
        client = await _client("merge_suggest")
        first = await client.hold("今天和阿米娅复核了三号样本的污染读数", domain=["工作"])
        second = await client.hold("今天和阿米娅复核了三号样本的污染读数。", domain=["工作"])
        # Both buckets exist — the backend never merges on its own.
        assert first != second
        assert len(await client.list_buckets(include_archive=False)) == 2
        candidates = await client.find_merge_candidates(
            "今天和阿米娅复核了三号样本的污染读数。", bucket_type="dynamic", exclude_id=second
        )
        ids = [c["id"] for c in candidates]
        assert first in ids
        assert second not in ids  # exclude_id respected

    run(scenario())


def test_find_merge_candidates_empty_for_distinct_content():
    async def scenario():
        client = await _client("merge_distinct")
        await client.hold("和阿米娅讨论源石技艺的伦理边界", domain=["工作"])
        cands = await client.find_merge_candidates(
            "晚饭煮了关东煮，罗德岛的走廊很安静", bucket_type="dynamic"
        )
        assert cands == []

    run(scenario())


def test_find_merge_candidates_skips_pinned_and_crystallized():
    async def scenario():
        client = await _client("merge_gate")
        await client.hold("一条会被钉住的原则", domain=["core"], pinned=True)
        # pinned target must never be suggested.
        assert await client.find_merge_candidates("一条会被钉住的原则", bucket_type="dynamic") == []
        fid = await client.hold("这次对话后心里有种久违的松动", bucket_type="feel")
        await client.update(fid, crystallized=True)
        # crystallized feel must not be a merge target.
        assert await client.find_merge_candidates("这次对话后心里有种久违的松动", bucket_type="feel") == []

    run(scenario())


def test_merge_buckets_merges_and_deletes_source():
    async def scenario():
        client = await _client("merge_apply")
        target = await client.hold("和阿米娅复核三号样本", domain=["工作"], importance=5)
        source = await client.hold("和阿米娅复核三号样本（补充读数）", domain=["工作"], importance=8)
        result = await client.merge_buckets(source, target)
        assert result["ok"] is True
        assert result["target_id"] == target
        # source removed, target retains merged content + max importance.
        assert await client.get(source) is None
        merged = await client.get(target)
        assert "---" in merged.content
        assert int(merged.metadata.get("importance")) == 8
        assert len(await client.list_buckets(include_archive=False)) == 1

    run(scenario())


def test_merge_buckets_refuses_type_mismatch_and_pinned_target():
    async def scenario():
        client = await _client("merge_refuse")
        dyn = await client.hold("一条 dynamic", domain=["工作"])
        feel = await client.hold("一条 feel", bucket_type="feel")
        mismatch = await client.merge_buckets(dyn, feel)
        assert mismatch["ok"] is False and "类型不一致" in mismatch["error"]
        pinned = await client.hold("钉住的原则", domain=["core"], pinned=True)
        perm_dyn = await client.hold("另一条 dynamic", domain=["工作"])
        refuse_pin = await client.merge_buckets(perm_dyn, pinned)
        assert refuse_pin["ok"] is False
        # nothing was deleted on a refused merge.
        assert await client.get(dyn) is not None
        assert await client.get(perm_dyn) is not None

    run(scenario())


async def _with_ob_tool(client, scenario):
    from server import mcp_tools

    old = mcp_tools._ob_client
    try:
        mcp_tools.set_ob_client(client)
        return await scenario(mcp_tools)
    finally:
        mcp_tools._ob_client = old


def test_hold_tool_pending_then_merge_into_is_single_bucket():
    async def scenario():
        client = await _client("hold_tool_merge")

        async def inner(mcp_tools):
            r1 = json.loads(await mcp_tools.hold(content="和阿米娅复核三号样本读数", domain=["工作"]))
            first = r1["bucket_id"]
            # near-duplicate → pending, nothing written yet
            r2 = json.loads(await mcp_tools.hold(content="和阿米娅复核三号样本读数。", domain=["工作"]))
            assert r2.get("status") == "pending_merge_decision"
            assert first in [c["id"] for c in r2["merge_candidates"]]
            assert len(await client.list_buckets(include_archive=False)) == 1  # not written on pending
            # decide to merge
            r3 = json.loads(
                await mcp_tools.hold(content="和阿米娅复核三号样本读数。", domain=["工作"], merge_into=first)
            )
            assert r3.get("merged_into") == first
            assert len(await client.list_buckets(include_archive=False)) == 1

        await _with_ob_tool(client, inner)

    run(scenario())


def test_hold_tool_force_new_creates_despite_candidate():
    async def scenario():
        client = await _client("hold_tool_forcenew")

        async def inner(mcp_tools):
            json.loads(await mcp_tools.hold(content="走廊尽头那盏灯又坏了", domain=["生活"]))
            pending = json.loads(await mcp_tools.hold(content="走廊尽头那盏灯又坏了", domain=["生活"]))
            assert pending.get("status") == "pending_merge_decision"
            forced = json.loads(
                await mcp_tools.hold(content="走廊尽头那盏灯又坏了", domain=["生活"], force_new=True)
            )
            assert "bucket_id" in forced and "status" not in forced
            assert len(await client.list_buckets(include_archive=False)) == 2

        await _with_ob_tool(client, inner)

    run(scenario())


def test_hold_feel_tool_digests_source_only_after_persist():
    async def scenario():
        client = await _client("hold_feel_digest")

        async def inner(mcp_tools):
            src = await client.hold("一段值得回味的对话", domain=["关系"])
            # first feel persists immediately (no similar feel yet) → source digested
            json.loads(await mcp_tools.hold_feel(content="这次对话让我心里一松", source_bucket=src))
            assert (await client.get(src)).metadata.get("digested") is True
            # a second source + near-duplicate feel → pending, must NOT digest src2
            src2 = await client.hold("另一段对话", domain=["关系"])
            pending = json.loads(
                await mcp_tools.hold_feel(content="这次对话让我心里一松。", source_bucket=src2)
            )
            assert pending.get("status") == "pending_merge_decision"
            assert (await client.get(src2)).metadata.get("digested") in (None, False)

        await _with_ob_tool(client, inner)

    run(scenario())


def test_effective_role_explicit_and_legacy_fallback():
    async def scenario():
        client = await _client("role_resolve")
        rid = await client.hold("一条原则", bucket_type="permanent", extra_metadata={"role": "standing_invariant"})
        assert client.effective_role((await client.get(rid)).metadata) == "standing_invariant"
        # legacy fallback: pinned/protected permanent → evolving_principle
        pid = await client.hold("钉住的原则", bucket_type="permanent", pinned=True)
        assert client.effective_role((await client.get(pid)).metadata) == "evolving_principle"
        # legacy fallback: bare permanent → anchor (recall-only)
        bid = await client.hold("裸 permanent 关键事件", bucket_type="permanent")
        assert client.effective_role((await client.get(bid)).metadata) == "anchor"
        # non-permanent → None
        did = await client.hold("一条 dynamic", domain=["x"])
        assert client.effective_role((await client.get(did)).metadata) is None

    run(scenario())


def test_list_injectable_principles_groups_by_role():
    async def scenario():
        client = await _client("role_inject")
        s = await client.hold("一条边界", bucket_type="permanent", extra_metadata={"role": "standing_invariant"})
        evolving_ids = []
        for i in range(7):
            eid = await client.hold(
                f"相处模式 {i}", bucket_type="permanent", pinned=True,
                created=f"2026-01-0{i + 1}T00:00:00",
            )
            evolving_ids.append(eid)
        anchor = await client.hold("珍贵原始事件", bucket_type="permanent")  # bare → anchor
        grouped = await client.list_injectable_principles()
        assert [b.id for b in grouped["standing"]] == [s]  # all standing injected
        ev_ids = [b.id for b in grouped["evolving"]]
        assert len(ev_ids) == 5  # newest 5 of 7
        assert evolving_ids[6] in ev_ids and evolving_ids[0] not in ev_ids
        # anchor never enters injection
        assert anchor not in {b.id for b in grouped["standing"] + grouped["evolving"]}

    run(scenario())


def test_surface_breath_excludes_anchor_from_core():
    async def scenario():
        client = await _client("role_surface")
        anchor_pinned = await client.hold(
            "珍贵但不该自动浮现", bucket_type="permanent", pinned=True,
            extra_metadata={"role": "anchor"},
        )
        evolving = await client.hold(
            "演化中的相处模式", bucket_type="permanent", pinned=True,
            extra_metadata={"role": "evolving_principle"},
        )
        surfaced = await client._surface_breath(limit=8, domains=set(), include_core=True)
        ids = {b.id for b in surfaced}
        assert anchor_pinned not in ids  # anchor stays recall-only
        assert evolving in ids           # evolving/standing still eligible for core

    run(scenario())


def _fake_principle(bid, text, created):
    return type("B", (), {
        "id": bid,
        "content": text,
        "metadata": {"id": bid, "name": bid, "principle_injection": text, "created": created},
    })()


def _sm_with_principles(standing, evolving):
    from server.state_machine import StateMachine

    class FakeOB:
        async def list_injectable_principles(self):
            return {"standing": list(standing), "evolving": list(evolving)}

    sm = StateMachine.__new__(StateMachine)
    sm.ob_client = FakeOB()
    return sm


def test_principle_injection_standing_full_evolving_floored_over_budget():
    async def scenario():
        # 8 standing × ~300 chars > 2400 budget. Standing renders in full; the
        # evolving FLOOR (3) still renders despite the overflow (soft budget),
        # but evolving entries beyond the floor are trimmed.
        standing = [_fake_principle(f"s{i}", "标" * 300, f"2026-01-0{i}T00:00:00") for i in range(1, 9)]
        evolving = [_fake_principle(f"e{i}", "演" * 100, f"2026-02-0{i}T00:00:00") for i in range(1, 6)]
        sm = _sm_with_principles(standing, evolving)
        text = await sm._build_pinned_principles_context()
        for i in range(1, 9):
            assert f"s{i}" in text                      # every standing rendered
        assert ("标" * 300) in text                     # standing NOT truncated (cap 480 > 300)
        assert "〔当前相处模式·新近〕" in text            # evolving floor still shows
        for i in range(1, 4):
            assert f"e{i}" in text                      # first 3 (the floor) rendered
        assert "e4" not in text and "e5" not in text    # beyond floor trimmed by budget

    run(scenario())


def test_principle_injection_evolving_floor_survives_full_starvation():
    async def scenario():
        # Standing alone hugely exceeds the budget (would starve evolving to 0
        # under the old logic). The floor guarantees evolving still appears.
        standing = [_fake_principle(f"s{i}", "标" * 470, f"2026-01-{i:02d}T00:00:00") for i in range(1, 13)]
        evolving = [_fake_principle(f"e{i}", "演" * 80, f"2026-02-0{i}T00:00:00") for i in range(1, 4)]
        sm = _sm_with_principles(standing, evolving)
        text = await sm._build_pinned_principles_context()
        assert "〔当前相处模式·新近〕" in text
        for i in range(1, 4):  # all 3 (== floor) survive despite standing overflow
            assert f"e{i}" in text

    run(scenario())


def test_principle_injection_shows_evolving_and_caps_it_when_under_budget():
    async def scenario():
        standing = [_fake_principle("s1", "边界一", "2026-01-01T00:00:00")]
        evolving = [_fake_principle("e1", "演" * 400, "2026-02-01T00:00:00")]  # 400 > evolving cap 240
        sm = _sm_with_principles(standing, evolving)
        text = await sm._build_pinned_principles_context()
        assert "〔当前相处模式·新近〕" in text
        assert "e1" in text
        assert ("演" * 240) in text          # evolving rendered up to its cap
        assert ("演" * 241) not in text       # but capped at 240

    run(scenario())


def test_decay_archives_old_feel():
    async def scenario():
        client = await _client("feel_aging")
        old_iso = (datetime.utcnow() - timedelta(days=200)).isoformat()
        fresh_iso = datetime.utcnow().isoformat()
        old_feel = await client.hold("很久以前的一段感受", bucket_type="feel", created=old_iso)
        fresh_feel = await client.hold("刚刚才有的一段感受", bucket_type="feel", created=fresh_iso)
        engine = OBDecayEngine(client)
        result = await engine.run_decay_cycle(dry_run=False)
        # Old feel decays below the archive threshold; fresh feel stays.
        assert old_feel in result["archived_ids"]
        assert fresh_feel not in result["archived_ids"]
        # Feel must NOT be auto-resolved (that lifecycle is dynamics-only).
        assert old_feel not in result["auto_resolved_ids"]

    run(scenario())


def test_dream_scope_default_excludes_character_life():
    """dream(scope='relational') default — character_life dynamics must not surface."""

    async def scenario():
        client = await _client("dream_scope")
        rel_id = await client.hold(
            "relational dynamic event",
            bucket_type="dynamic",
            domain=["relationship"],
            created="2026-01-01T00:00:00",
        )
        char_id = await client.hold(
            "character life dynamic event",
            bucket_type="dynamic",
            domain=["character_life"],
            tags=["environment_event_summary", "character_life"],
            created="2026-01-02T00:00:00",
        )

        relational = await client.dream(limit=10)
        ids_rel = {item["id"] for item in relational["items"]}
        assert relational["scope"] == "relational"
        assert rel_id in ids_rel
        assert char_id not in ids_rel

        character = await client.dream(limit=10, scope="character")
        ids_char = {item["id"] for item in character["items"]}
        assert character["scope"] == "character"
        assert char_id in ids_char
        assert rel_id not in ids_char

        full = await client.dream(limit=10, scope="all")
        ids_all = {item["id"] for item in full["items"]}
        assert full["scope"] == "all"
        assert {rel_id, char_id} <= ids_all

    run(scenario())


def test_feel_crystals_scope_isolates_relational_and_character():
    """feel_crystals must cluster only within the requested scope."""

    async def scenario():
        client = await _client("feel_crystals_scope")
        rel_ids = []
        for idx in range(3):
            rel_ids.append(await client.hold(
                f"relational similar feel {idx}",
                bucket_type="feel",
                domain=["relationship"],
                created=f"2026-01-0{idx + 1}T00:00:00",
            ))
        char_ids = []
        for idx in range(3):
            char_ids.append(await client.hold(
                f"character similar feel {idx}",
                bucket_type="feel",
                domain=["character_life"],
                created=f"2026-01-1{idx}T00:00:00",
            ))

        client.set_embedding_store(FakeEmbeddingStore({
            rel_ids[0]: [1.0, 0.0, 0.0],
            rel_ids[1]: [0.99, 0.01, 0.0],
            rel_ids[2]: [0.98, 0.02, 0.0],
            char_ids[0]: [0.0, 1.0, 0.0],
            char_ids[1]: [0.0, 0.99, 0.01],
            char_ids[2]: [0.0, 0.98, 0.02],
        }))

        relational = await client.feel_crystals(
            limit=5, min_cluster_size=3, min_similarity=0.9,
        )
        assert relational["scope"] == "relational"
        assert relational["total_clusters"] == 1
        rel_cluster_ids = set(relational["clusters"][0]["feel_ids"])
        assert rel_cluster_ids == set(rel_ids)

        character = await client.feel_crystals(
            limit=5, min_cluster_size=3, min_similarity=0.9, scope="character",
        )
        assert character["scope"] == "character"
        assert character["total_clusters"] == 1
        char_cluster_ids = set(character["clusters"][0]["feel_ids"])
        assert char_cluster_ids == set(char_ids)

        full = await client.feel_crystals(
            limit=5, min_cluster_size=3, min_similarity=0.9, scope="all",
        )
        assert full["scope"] == "all"
        assert full["total_clusters"] == 2

    run(scenario())


def test_crystallize_feel_cluster_lookup_respects_scope():
    """crystallize_feel(cluster_id, include_all) must resolve cluster within scope."""

    async def scenario():
        client = await _client("crystallize_scope")
        rel_ids = []
        for idx in range(3):
            rel_ids.append(await client.hold(
                f"rel feel {idx}",
                bucket_type="feel",
                domain=["relationship"],
                created=f"2026-01-0{idx + 1}T00:00:00",
            ))
        char_ids = []
        for idx in range(3):
            char_ids.append(await client.hold(
                f"char feel {idx}",
                bucket_type="feel",
                domain=["character_life"],
                created=f"2026-01-1{idx}T00:00:00",
            ))
        client.set_embedding_store(FakeEmbeddingStore({
            rel_ids[0]: [1.0, 0.0, 0.0],
            rel_ids[1]: [0.99, 0.01, 0.0],
            rel_ids[2]: [0.98, 0.02, 0.0],
            char_ids[0]: [0.0, 1.0, 0.0],
            char_ids[1]: [0.0, 0.99, 0.01],
            char_ids[2]: [0.0, 0.98, 0.02],
        }))

        relational = await client.feel_crystals(
            limit=5, min_cluster_size=3, min_similarity=0.9,
        )
        rel_cluster_id = relational["clusters"][0]["cluster_id"]

        # Wrong scope ("character") cannot find a relational-side cluster_id.
        wrong = await client.crystallize_feel(
            mode="feel",
            feel_content="cross-scope condensed",
            cluster_id=rel_cluster_id,
            include_all=True,
            scope="character",
            min_cluster_size=3,
            min_similarity=0.9,
        )
        assert wrong["marked_count"] == 0

        # Correct relational scope resolves the cluster and marks all 3 sources.
        good = await client.crystallize_feel(
            mode="feel",
            feel_content="relational condensed",
            cluster_id=rel_cluster_id,
            include_all=True,
            scope="relational",
            min_cluster_size=3,
            min_similarity=0.9,
        )
        assert good["marked_count"] == 3
        assert set(good["source_feel_ids"]) == set(rel_ids)
        for source_id in good["source_feel_ids"]:
            source = await client.get(source_id)
            assert source.metadata["crystallized"] is True

        # character_life feels remain untouched by the relational crystallization.
        for cid in char_ids:
            char_feel = await client.get(cid)
            assert not char_feel.metadata.get("crystallized")

    run(scenario())


# --- Phase 2: feel -> principle consolidation primitive ---------------------


async def _seven_feel_cluster(name: str):
    """Build a client with one 7-member relational feel cluster + embeddings."""
    client = await _client(name)
    ids = []
    for idx in range(7):
        ids.append(await client.hold(
            f"我又一次感到被理解 variant {idx}",
            bucket_type="feel",
            domain=["relationship"],
            created=f"2026-01-0{idx + 1}T00:00:00",
            extra_metadata={"source_bucket": f"d_{idx}", "arousal": 0.3 + idx * 0.05},
        ))
    # Six near-identical vectors + one mild outlier (highest uniqueness).
    vectors = {
        ids[0]: [1.0, 0.00, 0.0],
        ids[1]: [0.99, 0.01, 0.0],
        ids[2]: [0.985, 0.02, 0.0],
        ids[3]: [0.98, 0.03, 0.0],
        ids[4]: [0.975, 0.04, 0.0],
        ids[5]: [0.97, 0.05, 0.0],
        ids[6]: [0.90, 0.20, 0.0],  # outlier -> max uniqueness
    }
    client.set_embedding_store(FakeEmbeddingStore(vectors))
    return client, ids


def test_review_feel_cluster_batches_full_text_by_uniqueness():
    async def scenario():
        client, ids = await _seven_feel_cluster("review_batch")
        page = await client.feel_crystals(limit=1, min_cluster_size=3, min_similarity=0.8)
        cluster_id = page["clusters"][0]["cluster_id"]

        first = await client.review_feel_cluster(
            cluster_id=cluster_id, batch_size=6, min_cluster_size=3, min_similarity=0.8,
        )
        # Whole cluster visible as total; only 6 full-text items in the batch.
        assert first["total"] == 7
        assert first["size"] == 6
        assert first["has_more"] is True
        assert first["degraded"] is False
        # Full text is not truncated and the outlier (max uniqueness) comes first.
        assert first["items"][0]["id"] == ids[6]
        assert first["items"][0]["full_text"].startswith("我又一次感到被理解")
        assert first["items"][0]["source_dynamic"] == "d_6"
        assert first["items"][0]["uniqueness"] >= first["items"][1]["uniqueness"]

        second = await client.review_feel_cluster(
            cluster_id=cluster_id, cursor=first["next_cursor"],
            batch_size=6, min_cluster_size=3, min_similarity=0.8,
        )
        assert second["size"] == 1
        assert second["has_more"] is False

    run(scenario())


def test_commit_feel_crystal_keeps_whole_cluster_as_anchor_refs():
    async def scenario():
        client, ids = await _seven_feel_cluster("commit_anchor_refs")
        page = await client.feel_crystals(limit=1, min_cluster_size=3, min_similarity=0.8)
        cluster_id = page["clusters"][0]["cluster_id"]

        result = await client.commit_feel_crystal(
            synthesis="我们之间反复出现的模式：被理解后我会卸下防备。",
            cluster_id=cluster_id,
            title="相处模式·被理解",
            min_cluster_size=3, min_similarity=0.8,
        )
        assert result["updated"] is False
        assert result["source_count"] == 7
        # anchor_refs cover the WHOLE cluster, including unread items (I2).
        assert result["anchor_refs_count"] == 7

        crystal = await client.get(result["crystal_bucket_id"])
        assert crystal.metadata["type"] == "permanent"
        assert crystal.metadata["role"] == "evolving_principle"
        assert len(crystal.metadata["anchor_refs"]) == 7
        assert set(crystal.metadata["source_ids"]) == set(ids)
        assert crystal.metadata["principle_pattern"].startswith("我们之间反复出现")

        # Source feels are NOT crystallized — they age on their own track.
        for fid in ids:
            src = await client.get(fid)
            assert src.metadata["type"] == "feel"
            assert not src.metadata.get("crystallized")

    run(scenario())


def test_commit_feel_crystal_promotes_keep_and_demotes_redundant():
    async def scenario():
        client, ids = await _seven_feel_cluster("commit_keep_demote")
        page = await client.feel_crystals(limit=1, min_cluster_size=3, min_similarity=0.8)
        cluster_id = page["clusters"][0]["cluster_id"]

        result = await client.commit_feel_crystal(
            synthesis="模式综合。",
            cluster_id=cluster_id,
            anchor_ids=[ids[6]],       # irreplaceable moment -> anchor
            standing_ids=[ids[0]],     # durable consensus -> standing_invariant
            demote_ids=[ids[1], ids[2]],  # redundant -> exit surfacing
            min_cluster_size=3, min_similarity=0.8,
        )
        assert result["promoted_to_anchor"] == [ids[6]]
        assert result["promoted_to_standing"] == [ids[0]]
        assert set(result["demoted"]) == {ids[1], ids[2]}

        anchor = await client.get(ids[6])
        assert anchor.metadata["type"] == "permanent"
        assert anchor.metadata["role"] == "anchor"
        standing = await client.get(ids[0])
        assert standing.metadata["type"] == "permanent"
        assert standing.metadata["role"] == "standing_invariant"

        # Demoted feels stay as feel, flagged demoted, still recallable (not deleted).
        for fid in (ids[1], ids[2]):
            d = await client.get(fid)
            assert d.metadata["type"] == "feel"
            assert d.metadata["demoted"] is True
            # Demoted feels sink in score but are not zero (recall still works).
            assert client.calculate_score(d.metadata) < client.calculate_score(
                {"type": "feel", "created": d.metadata["created"], "last_active": d.metadata["created"]}
            )

    run(scenario())


def test_commit_feel_crystal_is_idempotent_via_crystal_id():
    async def scenario():
        client, ids = await _seven_feel_cluster("commit_idempotent")
        page = await client.feel_crystals(limit=1, min_cluster_size=3, min_similarity=0.8)
        cluster_id = page["clusters"][0]["cluster_id"]

        first = await client.commit_feel_crystal(
            synthesis="第一版综合。", cluster_id=cluster_id,
            min_cluster_size=3, min_similarity=0.8,
        )
        second = await client.commit_feel_crystal(
            synthesis="第二版·精修后的综合。",
            crystal_id=first["crystal_id"], source_ids=ids,
        )
        assert second["updated"] is True
        assert second["crystal_bucket_id"] == first["crystal_bucket_id"]

        # Only one crystal bucket exists; content reflects the latest synthesis.
        permanents = [
            b for b in await client.list_buckets()
            if b.metadata.get("crystal_id") == first["crystal_id"]
        ]
        assert len(permanents) == 1
        assert permanents[0].metadata["principle_pattern"] == "第二版·精修后的综合。"

    run(scenario())


def test_review_feel_cluster_degraded_without_embeddings():
    async def scenario():
        client = await _client("review_degraded")
        for idx in range(4):
            await client.hold(
                f"feel {idx}", bucket_type="feel", domain=["relationship"],
                created=f"2026-01-0{idx + 1}T00:00:00",
            )
        # No embedding store set -> clustering returns nothing, review degrades.
        out = await client.review_feel_cluster(cluster_id="nonexistent")
        assert out["items"] == []
        assert out["degraded"] is True
        assert "error" in out

    run(scenario())


def test_dream_crystal_hint_counts_mature_clusters():
    async def scenario():
        client, ids = await _seven_feel_cluster("dream_hint")
        result = await client.dream(scope="relational")
        assert "结晶提示" in result["crystal_hint"]
        assert "1 簇" in result["crystal_hint"]
        assert "commit_feel_crystal" in result["crystal_hint"]

    run(scenario())


def test_demoted_feel_excluded_from_clusters():
    async def scenario():
        client, ids = await _seven_feel_cluster("demoted_excluded")
        page = await client.feel_crystals(limit=1, min_cluster_size=3, min_similarity=0.8)
        cluster_id = page["clusters"][0]["cluster_id"]
        await client.commit_feel_crystal(
            synthesis="综合。", cluster_id=cluster_id,
            demote_ids=[ids[1], ids[2], ids[3], ids[4], ids[5]],
            min_cluster_size=3, min_similarity=0.8,
        )
        # After demoting 5 of 7, only 2 active feels remain -> below min_cluster_size.
        page2 = await client.feel_crystals(limit=5, min_cluster_size=3, min_similarity=0.8)
        assert page2["total_clusters"] == 0

    run(scenario())


def test_oversized_component_is_split_into_subclusters():
    async def scenario():
        client = await _client("oversized_split")
        client.FEEL_CLUSTER_MAX_SIZE = 3  # force the split path with few buckets
        ids = []
        for idx in range(6):
            ids.append(await client.hold(
                f"group feel {idx}", bucket_type="feel", domain=["relationship"],
                created=f"2026-01-0{idx + 1}T00:00:00",
            ))
        # Two tight triplets (intra cos ~0.999) connected across at cos ~0.85:
        # one component of 6 at min_similarity=0.7, splits at a tighter threshold.
        client.set_embedding_store(FakeEmbeddingStore({
            ids[0]: [1.0, 0.0, 0.0],
            ids[1]: [0.999, 0.04, 0.0],
            ids[2]: [0.998, 0.06, 0.0],
            ids[3]: [0.85, 0.527, 0.0],
            ids[4]: [0.86, 0.510, 0.0],
            ids[5]: [0.845, 0.535, 0.0],
        }))
        page = await client.feel_crystals(limit=5, min_cluster_size=3, min_similarity=0.7)
        # Without the cap this is one component of 6; the cap splits it into two.
        assert page["total_clusters"] == 2
        for cluster in page["clusters"]:
            assert len(cluster["feel_ids"]) == 3

    run(scenario())


def test_review_ceiling_summarises_tail_and_blocks_reading_it():
    async def scenario():
        client, ids = await _seven_feel_cluster("review_ceiling")
        page = await client.feel_crystals(limit=1, min_cluster_size=3, min_similarity=0.8)
        cluster_id = page["clusters"][0]["cluster_id"]
        # batch=2, REVIEW_MAX_BATCHES=3 -> readable_cap=6; 7 members -> tail of 1.
        first = await client.review_feel_cluster(
            cluster_id=cluster_id, batch_size=2, min_cluster_size=3, min_similarity=0.8,
        )
        assert first["total"] == 7
        assert first["readable_total"] == 6
        assert first["tail"]["count"] == 1
        # Walk to the end of the readable window; the 7th item is never reachable.
        seen = list(first["items"])
        cursor = first["next_cursor"]
        while cursor:
            nxt = await client.review_feel_cluster(
                cluster_id=cluster_id, cursor=cursor, batch_size=2,
                min_cluster_size=3, min_similarity=0.8,
            )
            seen.extend(nxt["items"])
            cursor = nxt["next_cursor"]
        assert len(seen) == 6

    run(scenario())


def test_review_orders_by_salience_not_pure_uniqueness():
    async def scenario():
        client = await _client("salience_order")
        # Four near-identical feels (uniqueness ~0 for all) so arousal decides order.
        ids = []
        arousals = [0.2, 0.9, 0.3, 0.25]
        for idx, ar in enumerate(arousals):
            ids.append(await client.hold(
                f"near identical feel {idx}", bucket_type="feel", domain=["relationship"],
                created=f"2026-01-0{idx + 1}T00:00:00",
                extra_metadata={"arousal": ar},
            ))
        client.set_embedding_store(FakeEmbeddingStore({
            ids[0]: [1.0, 0.000, 0.0],
            ids[1]: [0.9999, 0.002, 0.0],
            ids[2]: [0.9998, 0.003, 0.0],
            ids[3]: [0.9997, 0.004, 0.0],
        }))
        page = await client.feel_crystals(limit=1, min_cluster_size=3, min_similarity=0.9)
        cluster_id = page["clusters"][0]["cluster_id"]
        review = await client.review_feel_cluster(
            cluster_id=cluster_id, batch_size=4, min_cluster_size=3, min_similarity=0.9,
        )
        # The high-arousal item leads despite ~equal uniqueness (deep mark surfaces).
        assert review["items"][0]["id"] == ids[1]

    run(scenario())


def test_demote_vetoes_high_arousal_unless_forced():
    async def scenario():
        client = await _client("demote_veto")
        ids = []
        for idx in range(3):
            ids.append(await client.hold(
                f"intense feel {idx}", bucket_type="feel", domain=["relationship"],
                created=f"2026-01-0{idx + 1}T00:00:00",
                extra_metadata={"arousal": 0.9},  # above DEMOTE_AROUSAL_VETO
            ))
        client.set_embedding_store(FakeEmbeddingStore({
            ids[0]: [1.0, 0.0, 0.0],
            ids[1]: [0.999, 0.02, 0.0],
            ids[2]: [0.998, 0.03, 0.0],
        }))
        page = await client.feel_crystals(limit=1, min_cluster_size=3, min_similarity=0.9)
        cluster_id = page["clusters"][0]["cluster_id"]

        soft = await client.commit_feel_crystal(
            synthesis="综合。", cluster_id=cluster_id,
            demote_ids=[ids[0]], min_cluster_size=3, min_similarity=0.9,
        )
        assert soft["demoted"] == []
        assert soft["demote_vetoed"] == [ids[0]]
        assert not (await client.get(ids[0])).metadata.get("demoted")

        forced = await client.commit_feel_crystal(
            synthesis="综合。", crystal_id=soft["crystal_id"], source_ids=ids,
            demote_ids=[ids[0]], force_demote=True,
        )
        assert forced["demoted"] == [ids[0]]
        assert (await client.get(ids[0])).metadata.get("demoted") is True

    run(scenario())


def test_committed_cluster_goes_quiet_but_recoverable_with_include_settled():
    async def scenario():
        client, ids = await _seven_feel_cluster("settled_quiet")
        page = await client.feel_crystals(limit=5, min_cluster_size=3, min_similarity=0.8)
        assert page["total_clusters"] == 1
        cluster_id = page["clusters"][0]["cluster_id"]

        res = await client.commit_feel_crystal(
            synthesis="我们之间反复出现的模式。", cluster_id=cluster_id,
            min_cluster_size=3, min_similarity=0.8,
        )
        # Every source feel (none promoted/demoted) is stamped "settled".
        assert set(res["settled"]) == set(ids)
        for fid in ids:
            src = await client.get(fid)
            assert src.metadata["type"] == "feel"  # still a feel, recallable
            assert src.metadata["consolidated_into"] == res["crystal_id"]

        # The cluster now goes quiet in the default menu and the dream hint.
        quiet = await client.feel_crystals(limit=5, min_cluster_size=3, min_similarity=0.8)
        assert quiet["total_clusters"] == 0
        dreamt = await client.dream(scope="relational")
        assert dreamt["crystal_hint"] == ""

        # But it is recoverable for manual re-review with include_settled=True.
        shown = await client.feel_crystals(
            limit=5, min_cluster_size=3, min_similarity=0.8, include_settled=True,
        )
        assert shown["total_clusters"] == 1
        assert shown["clusters"][0]["settled"] is True
        assert shown["clusters"][0]["unsettled_count"] == 0

    run(scenario())


def test_resurfaced_cluster_folds_into_same_crystal():
    async def scenario():
        client, ids = await _seven_feel_cluster("resurface_same_crystal")
        page = await client.feel_crystals(limit=5, min_cluster_size=3, min_similarity=0.8)
        cluster_id = page["clusters"][0]["cluster_id"]
        first = await client.commit_feel_crystal(
            synthesis="第一版。", cluster_id=cluster_id,
            min_cluster_size=3, min_similarity=0.8,
        )
        assert (await client.feel_crystals(
            limit=5, min_cluster_size=3, min_similarity=0.8
        ))["total_clusters"] == 0  # quiet

        # New feels on the same theme accumulate (un-crystallized -> fresh).
        new_ids = []
        for k in range(3):
            nid = await client.hold(
                f"又一次被理解 new {k}", bucket_type="feel", domain=["relationship"],
                created=f"2026-02-0{k + 1}T00:00:00",
                extra_metadata={"source_bucket": f"dn_{k}"},
            )
            new_ids.append(nid)
            client.embedding_store.vectors[nid] = [0.97 - k * 0.002, 0.05, 0.0]

        # The cluster re-surfaces, carrying only the fresh material as unsettled.
        page2 = await client.feel_crystals(limit=5, min_cluster_size=3, min_similarity=0.8)
        assert page2["total_clusters"] == 1
        assert page2["clusters"][0]["unsettled_count"] == 3
        cluster_id2 = page2["clusters"][0]["cluster_id"]
        assert cluster_id2 != cluster_id  # membership grew -> new cluster_id

        # Committing without an explicit crystal_id folds back into the SAME one.
        second = await client.commit_feel_crystal(
            synthesis="第二版·纳入新材料。", cluster_id=cluster_id2,
            min_cluster_size=3, min_similarity=0.8,
        )
        assert second["updated"] is True
        assert second["crystal_id"] == first["crystal_id"]
        crystals = [
            b for b in await client.list_buckets()
            if b.metadata.get("crystal_id") == first["crystal_id"]
        ]
        assert len(crystals) == 1  # no duplicate crystal
        assert crystals[0].metadata["principle_pattern"] == "第二版·纳入新材料。"

    run(scenario())

