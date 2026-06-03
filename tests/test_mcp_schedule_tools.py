from __future__ import annotations

import asyncio
import json

from server import mcp_tools
from server.models import DailyPlan, PlanItem


def run(coro):
    return asyncio.run(coro)


class FakeScheduleDB:
    def __init__(self):
        self.plans: dict[int, DailyPlan] = {
            1: DailyPlan(id=1, plan_date="2026-05-22", status="active"),
            2: DailyPlan(id=2, plan_date="2026-05-21", status="active"),
        }
        self.items: dict[int, PlanItem] = {
            10: PlanItem(
                id=10,
                plan_id=1,
                hour_start=8,
                hour_end=10,
                activity="晨间档案",
                action_payload=json.dumps({"thread_id": "old"}, ensure_ascii=False),
                source_kind="generated",
            ),
            11: PlanItem(id=11, plan_id=1, hour_start=10, hour_end=11, activity="医疗复核"),
            20: PlanItem(id=20, plan_id=2, hour_start=12, hour_end=13, activity="旧日程"),
        }
        self.next_item_id = 100
        self.deleted_plan_ids: list[int] = []

    async def get_daily_plan_by_id(self, plan_id: int):
        return self.plans.get(plan_id)

    async def get_latest_daily_plan_for_date(self, plan_date: str, *, status: str | None = None):
        candidates = [
            plan
            for plan in self.plans.values()
            if plan.plan_date == plan_date and (status is None or plan.status == status)
        ]
        return sorted(candidates, key=lambda item: int(item.id or 0), reverse=True)[0] if candidates else None

    async def list_daily_plans(self, *, offset: int = 0, limit: int = 30, status: str | None = None):
        candidates = [plan for plan in self.plans.values() if status is None or plan.status == status]
        candidates.sort(key=lambda item: (item.plan_date, int(item.id or 0)), reverse=True)
        return candidates[offset: offset + limit]

    async def list_plan_items(self, plan_id: int, *, status: str | None = None):
        candidates = [
            item
            for item in self.items.values()
            if item.plan_id == plan_id and (status is None or item.status == status)
        ]
        candidates.sort(key=lambda item: (item.hour_start, item.hour_end, int(item.id or 0)))
        return candidates

    async def get_plan_item_by_id(self, item_id: int):
        return self.items.get(item_id)

    async def update_plan_item(self, item_id: int, **fields):
        item = self.items[item_id]
        data = item.model_dump()
        data.update(fields)
        self.items[item_id] = PlanItem(**data)

    async def delete_plan_items_for_plan(self, plan_id: int):
        self.deleted_plan_ids.append(plan_id)
        doomed = [item_id for item_id, item in self.items.items() if item.plan_id == plan_id]
        for item_id in doomed:
            del self.items[item_id]
        return len(doomed)

    async def insert_plan_item(self, item: PlanItem):
        item.id = self.next_item_id
        self.next_item_id += 1
        self.items[int(item.id)] = item
        return item.id

    async def update_daily_plan(self, plan_id: int, **fields):
        plan = self.plans[plan_id]
        data = plan.model_dump()
        data.update(fields)
        self.plans[plan_id] = DailyPlan(**data)


class FakeStateMachine:
    def __init__(self, db):
        self.db = db


class FakePlanEngine:
    def __init__(self, db):
        self.db = db

    async def get_current_plan(self):
        return self.db.plans[1]

    async def get_effective_plan_items(self, plan):
        return await self.db.list_plan_items(int(plan.id or 0))


async def with_schedule_tool(scenario):
    db = FakeScheduleDB()
    old = (mcp_tools._state_machine, mcp_tools._plan_engine)
    try:
        mcp_tools.set_state_machine(FakeStateMachine(db))
        mcp_tools.set_plan_engine(FakePlanEngine(db))
        return await scenario(db)
    finally:
        mcp_tools._state_machine, mcp_tools._plan_engine = old


def test_schedule_bundle_reads_current_plan_and_history():
    async def scenario(db):
        raw = await mcp_tools.schedule_bundle("read", include_history=True, history_limit=2)
        result = json.loads(raw)
        assert result["ok"] is True
        assert result["action"] == "read"
        assert result["plan"]["id"] == 1
        assert [item["id"] for item in result["items"]] == [10, 11]
        assert len(result["history"]) == 2

    run(with_schedule_tool(scenario))


def test_schedule_bundle_reads_by_plan_date():
    async def scenario(db):
        raw = await mcp_tools.schedule_bundle("read", plan_date="2026-05-21")
        result = json.loads(raw)
        assert result["ok"] is True
        assert result["plan"]["id"] == 2
        assert result["items"][0]["activity"] == "旧日程"

    run(with_schedule_tool(scenario))


def test_schedule_bundle_read_missing_specific_plan_returns_error():
    async def scenario(db):
        raw = await mcp_tools.schedule_bundle("read", plan_id=999)
        result = json.loads(raw)
        assert result["ok"] is False
        assert "未找到指定日程" in result["error"]

    run(with_schedule_tool(scenario))


def test_schedule_bundle_edit_item_updates_raw_plan_and_sanitizes_payload():
    async def scenario(db):
        payload = json.dumps(
            {
                "hour_start": 9,
                "hour_end": 12,
                "activity": "医疗档案复核",
                "status": "executing",
                "action_payload": {"thread_id": "case-a", "message": "remove me"},
            },
            ensure_ascii=False,
        )
        raw = await mcp_tools.schedule_bundle("edit_item", item_id=10, payload_json=payload)
        result = json.loads(raw)
        assert result["ok"] is True
        updated = db.items[10]
        assert updated.hour_start == 9
        assert updated.hour_end == 12
        assert updated.activity == "医疗档案复核"
        assert updated.status == "executing"
        parsed_payload = json.loads(updated.action_payload)
        assert parsed_payload == {"thread_id": "case-a"}
        assert "医疗档案复核" in db.plans[1].raw_plan

    run(with_schedule_tool(scenario))


def test_schedule_bundle_invalid_edit_does_not_write():
    async def scenario(db):
        before = db.items[10].model_dump()
        raw = await mcp_tools.schedule_bundle(
            "edit_item",
            item_id=10,
            payload_json=json.dumps({"status": "impossible"}),
        )
        result = json.loads(raw)
        assert result["ok"] is False
        assert db.items[10].model_dump() == before
        assert not db.plans[1].raw_plan

    run(with_schedule_tool(scenario))


def test_schedule_bundle_replace_items_accepts_items_object_and_defaults_source_kind():
    async def scenario(db):
        payload = json.dumps(
            {
                "items": [
                    {"hour_start": 15, "hour_end": 16, "activity": "后勤确认"},
                    {
                        "hour_start": 13,
                        "hour_end": 14,
                        "activity": "会议复盘",
                        "source_kind": "replan",
                    },
                ]
            },
            ensure_ascii=False,
        )
        raw = await mcp_tools.schedule_bundle("replace_items", plan_id=1, payload_json=payload)
        result = json.loads(raw)
        assert result["ok"] is True
        assert result["replaced_count"] == 2
        assert [item["activity"] for item in result["items"]] == ["会议复盘", "后勤确认"]
        assert result["items"][0]["source_kind"] == "replan"
        assert result["items"][1]["source_kind"] == "spontaneous"
        assert 1 in db.deleted_plan_ids
        assert "会议复盘" in db.plans[1].raw_plan

    run(with_schedule_tool(scenario))


def test_schedule_bundle_replace_items_accepts_array_payload():
    async def scenario(db):
        payload = json.dumps(
            [
                {
                    "hour_start": 18,
                    "hour_end": 19,
                    "activity": "夜间巡检",
                    "action_payload": {"content": "remove me", "progress_status": "open"},
                }
            ],
            ensure_ascii=False,
        )
        raw = await mcp_tools.schedule_bundle("replace_items", plan_id=1, payload_json=payload)
        result = json.loads(raw)
        assert result["ok"] is True
        assert result["items"][0]["activity"] == "夜间巡检"
        assert result["items"][0]["action_payload"] == {"progress_status": "open"}

    run(with_schedule_tool(scenario))
