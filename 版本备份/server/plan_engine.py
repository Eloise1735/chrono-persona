"""Daily plan generation, execution, and replanning."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from server.database import Database
from server.llm_client import LLMClient
from server.models import (
    CharacterNotification,
    DailyPlan,
    PlanItem,
    format_utc_instant_z,
)
from server.npc_engine import NPCEngine, _safe_prompt_format
from server.prompts import (
    KEY_L1_CHARACTER_BACKGROUND,
    KEY_L2_CHARACTER_PERSONALITY,
    KEY_L2_LIFE_STATUS,
    KEY_L2_RELATIONSHIP_DYNAMICS,
    KEY_PLAN_ENABLED,
    KEY_PLAN_GENERATION_HOUR,
    KEY_PLAN_HOUR_END,
    KEY_PLAN_HOUR_START,
    KEY_PLAN_NPC_INTERACTION_ENABLED,
    KEY_PLAN_PROACTIVE_MESSAGE_ENABLED,
    KEY_PLAN_REPLAN_ON_CONVERSATION,
    KEY_PLAN_REPLAN_ON_DRIFT,
    KEY_PLAN_WEB_SEARCH_API_BASE,
    KEY_PLAN_WEB_SEARCH_API_KEY,
    KEY_PLAN_WEB_SEARCH_ENABLED,
    KEY_PROMPT_DAILY_PLAN_GENERATION,
    KEY_PROMPT_PLAN_DRIFT_CHECK,
    KEY_PROMPT_PLAN_ITEM_EXECUTE,
    KEY_PROMPT_PLAN_REPLAN,
    PromptManager,
)
from server.state_machine import StateMachine
from server.time_display import DISPLAY_TZ, parse_db_instant_to_shanghai, shanghai_now
from server.web_search import WebSearchClient

logger = logging.getLogger(__name__)

NotificationDispatcher = Callable[[CharacterNotification], Awaitable[None]]


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(raw[start : end + 1])
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _extract_json_array(text: str) -> list[Any]:
    raw = (text or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception:
        pass
    start = raw.find("[")
    end = raw.rfind("]")
    if start >= 0 and end > start:
        try:
            data = json.loads(raw[start : end + 1])
            return data if isinstance(data, list) else []
        except Exception:
            return []
    return []


def _bool_from_setting(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _shanghai_dt(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc).astimezone(DISPLAY_TZ)
    return dt.astimezone(DISPLAY_TZ)


def _normalize_action_type(value: str) -> str:
    action_type = str(value or "internal")
    if action_type not in {"internal", "message_user", "web_search", "npc_interaction"}:
        return "internal"
    return action_type


def _normalize_source_kind(value: str, *, default: str) -> str:
    source_kind = str(value or default)
    if source_kind not in {
        "generated",
        "routine",
        "carried_over",
        "thread",
        "spontaneous",
        "replan",
    }:
        return default
    return source_kind


def _normalize_source_ref_id(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


class PlanEngine:
    def __init__(
        self,
        db: Database,
        llm: LLMClient,
        prompt_manager: PromptManager,
        state_machine: StateMachine,
        npc_engine: NPCEngine,
    ):
        self.db = db
        self.llm = llm
        self.prompt_manager = prompt_manager
        self.state_machine = state_machine
        self.npc_engine = npc_engine
        self._notify: NotificationDispatcher | None = None
        self._execute_lock = asyncio.Lock()
        self._web_search: WebSearchClient | None = None
        self._last_drift_at: datetime | None = None

    def set_notification_dispatcher(self, dispatcher: NotificationDispatcher | None) -> None:
        self._notify = dispatcher

    async def _bool_setting(self, key: str) -> bool:
        v = await self.prompt_manager.get_config_value(key)
        return _bool_from_setting(v)

    async def _int_setting(self, key: str, default: int) -> int:
        raw = (await self.prompt_manager.get_config_value(key)).strip()
        try:
            return int(raw)
        except Exception:
            return default

    async def is_enabled(self) -> bool:
        return await self._bool_setting(KEY_PLAN_ENABLED)

    async def get_current_plan(self) -> DailyPlan | None:
        today = shanghai_now().date().isoformat()
        return await self.db.get_latest_daily_plan_for_date(today, status="active")

    async def get_effective_plan_items(self, plan: DailyPlan) -> list[PlanItem]:
        if plan.id is None:
            return []
        return await self.db.list_plan_items(int(plan.id))

    def _patch_item_to_plan_item(self, plan_id: int, raw: dict[str, Any]) -> PlanItem | None:
        try:
            hs = int(raw.get("hour_start", -1))
            he = int(raw.get("hour_end", hs + 1))
        except Exception:
            return None
        if hs < 0 or he <= hs:
            return None
        activity = str(raw.get("activity") or "").strip() or "閲嶆帓琛岀▼"
        action_payload_raw = raw.get("action_payload")
        action_payload = json.dumps(
            action_payload_raw if isinstance(action_payload_raw, dict) else {},
            ensure_ascii=False,
        )
        return PlanItem(
            plan_id=plan_id,
            hour_start=hs,
            hour_end=he,
            activity=activity,
            action_type=_normalize_action_type(str(raw.get("action_type") or "internal")),  # type: ignore[arg-type]
            action_payload=action_payload,
            status="pending",
            outcome="",
            source_kind=_normalize_source_kind(str(raw.get("source_kind") or "replan"), default="replan"),  # type: ignore[arg-type]
            source_ref_id=_normalize_source_ref_id(raw.get("source_ref_id")),
        )

    @staticmethod
    def _items_overlap(left: PlanItem, right: PlanItem) -> bool:
        return left.hour_start < right.hour_end and right.hour_start < left.hour_end

    def _merge_replan_items(
        self,
        *,
        plan_id: int,
        existing_items: list[PlanItem],
        patch_rows: list[dict[str, Any]],
    ) -> list[PlanItem]:
        patch_items = [
            item
            for item in (self._patch_item_to_plan_item(plan_id, raw) for raw in patch_rows)
            if item is not None
        ]
        if not patch_items:
            return list(existing_items)

        survivors: list[PlanItem] = []
        for item in existing_items:
            if item.status != "pending":
                survivors.append(item)
                continue
            if any(self._items_overlap(item, patch_item) for patch_item in patch_items):
                continue
            survivors.append(item)

        merged = survivors + patch_items
        merged.sort(key=lambda item: (item.hour_start, item.hour_end, int(item.id or 0)))
        return merged

    async def _create_replanned_version(
        self,
        *,
        current_plan: DailyPlan,
        merged_items: list[PlanItem],
        trigger: str,
        context: str,
        raw_plan: str,
        llm_payload: dict[str, Any],
    ) -> DailyPlan:
        assert current_plan.id is not None
        context_payload = {
            "context": context,
            "trigger": trigger,
            "replan_parent_id": int(current_plan.id),
            "llm_payload": llm_payload,
        }
        next_plan = DailyPlan(
            plan_date=current_plan.plan_date,
            raw_plan=raw_plan,
            status="active",
            replan_trigger=trigger,
            replan_parent_id=int(current_plan.id),
            context_snapshot=json.dumps(context_payload, ensure_ascii=False),
        )
        next_plan_id = await self.db.insert_daily_plan(next_plan)

        for item in merged_items:
            cloned = PlanItem(
                plan_id=next_plan_id,
                hour_start=item.hour_start,
                hour_end=item.hour_end,
                activity=item.activity,
                action_type=item.action_type,
                action_payload=item.action_payload,
                status=item.status,
                outcome=item.outcome,
                outcome_event_id=item.outcome_event_id,
                source_kind=item.source_kind,
                source_ref_id=item.source_ref_id,
                created_at=item.created_at,
                executed_at=item.executed_at,
            )
            await self.db.insert_plan_item(cloned)

        await self.db.update_daily_plan(int(current_plan.id), status="replanned")
        created = await self.db.get_daily_plan_by_id(next_plan_id)
        assert created is not None
        return created

    async def get_plan_summary_text(self) -> str:
        plan = await self.get_current_plan()
        if plan is None or plan.id is None:
            return "(no plan today)"
        items = await self.db.list_plan_items(int(plan.id))
        if not items:
            return f"{plan.plan_date} has no plan items"
        lines: list[str] = [f"Date {plan.plan_date}"]
        for it in items:
            st = it.status
            lines.append(f"- {it.hour_start:02d}:00-{it.hour_end:02d}:00 [{st}] {it.activity}")
        return "\n".join(lines)

    async def get_plan_activity_for_time(self, checkpoint_time: datetime) -> str:
        if not await self.is_enabled():
            return ""
        plan = await self.get_current_plan()
        if plan is None or plan.id is None:
            return ""
        sh = _shanghai_dt(checkpoint_time)
        if sh.date().isoformat() != plan.plan_date:
            return ""
        hour = sh.hour
        item = await self.db.get_plan_item_for_hour(int(plan.id), hour)
        if item is None:
            return ""
        return f"{item.hour_start:02d}:00-{item.hour_end:02d}:00 {item.activity}".strip()

    async def ensure_today_plan(self) -> None:
        if not await self.is_enabled():
            return
        today = shanghai_now().date().isoformat()
        existing = await self.db.get_latest_daily_plan_for_date(today, status="active")
        if existing is not None:
            return
        gen_hour = await self._int_setting(KEY_PLAN_GENERATION_HOUR, 6)
        if shanghai_now().hour < gen_hour:
            return
        await self.generate_daily_plan(today)

    async def get_current_item(self) -> PlanItem | None:
        if not await self.is_enabled():
            return None
        plan = await self.get_current_plan()
        if plan is None or plan.id is None:
            return None
        hour = shanghai_now().hour
        item = await self.db.get_plan_item_for_hour(int(plan.id), hour, status="pending")
        return item

    async def execute_item(self, item: PlanItem) -> None:
        async with self._execute_lock:
            fresh = await self.db.get_plan_item_by_id(int(item.id or 0))
            if fresh is None or fresh.status != "pending":
                return
            await self.db.update_plan_item(int(fresh.id), status="executing")

        try:
            await self._execute_item_body(fresh)
        except Exception:
            logger.exception("Plan item execution failed for #%s", fresh.id)
            await self.db.update_plan_item(
                int(fresh.id),
                status="skipped",
                outcome="鎵ц寮傚父锛屽凡璺宠繃",
                executed_at=format_utc_instant_z(datetime.utcnow()),
            )

    async def _execute_item_body(self, item: PlanItem) -> None:
        proactive = await self._bool_setting(KEY_PLAN_PROACTIVE_MESSAGE_ENABLED)
        web_ok = await self._bool_setting(KEY_PLAN_WEB_SEARCH_ENABLED)
        npc_ok = await self._bool_setting(KEY_PLAN_NPC_INTERACTION_ENABLED)
        web_base = (await self.prompt_manager.get_config_value(KEY_PLAN_WEB_SEARCH_API_BASE)).strip()
        web_key = (await self.prompt_manager.get_config_value(KEY_PLAN_WEB_SEARCH_API_KEY)).strip()

        latest = await self.db.get_latest_snapshot()
        latest_snapshot = latest.content if latest else "(no snapshot)"
        recent = await self.db.get_recent_events_by_event_time(limit=8, include_archived=False)
        recent_events = "\n".join(f"- [{e.date}] {e.title or e.description[:120]}" for e in recent) or "(none)"

        extra_ctx = ""
        action_type = item.action_type
        payload: dict[str, Any] = {}
        try:
            payload = json.loads(item.action_payload or "{}")
        except Exception:
            payload = {}

        if action_type == "web_search" and web_ok and web_base and web_key:
            if self._web_search is None:
                self._web_search = WebSearchClient(web_base, web_key)
            q = str(payload.get("query") or item.activity or "").strip()
            if q:
                hits = await self._web_search.search(q, max_results=4)
                extra_ctx = "[Web search summary]\n" + json.dumps(hits, ensure_ascii=False)

        npc_context = "(npc disabled)"
        if action_type == "npc_interaction" and npc_ok:
            raw_id = payload.get("npc_id")
            npc = None
            if isinstance(raw_id, int) or (isinstance(raw_id, str) and str(raw_id).isdigit()):
                npc = await self.npc_engine.get_npc(int(raw_id))
            if npc is None and str(raw_id).strip().lower() == "auto":
                npc = await self.npc_engine.maybe_spawn_npc(item.activity)
            if npc is not None:
                res = await self.npc_engine.resolve_interaction(
                    npc,
                    activity_context=item.activity,
                    character_state=latest_snapshot,
                    recent_events=recent_events,
                )
                npc_context = json.dumps(res, ensure_ascii=False)

        template = await self.prompt_manager.get_prompt(KEY_PROMPT_PLAN_ITEM_EXECUTE)
        plan_item_text = json.dumps(item.model_dump(), ensure_ascii=False) + "\n" + extra_ctx
        prompt = _safe_prompt_format(
            template,
            plan_item=plan_item_text,
            character_personality=await self.prompt_manager.get_layer_content(KEY_L2_CHARACTER_PERSONALITY),
            relationship_dynamics=await self.prompt_manager.get_layer_content(KEY_L2_RELATIONSHIP_DYNAMICS),
            life_status=await self.prompt_manager.get_layer_content(KEY_L2_LIFE_STATUS),
            latest_snapshot=latest_snapshot,
            recent_events=recent_events,
            npc_context=npc_context,
        )
        system_prompt = await self.prompt_manager.get_system_prompt()
        response = await self.llm.chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            temperature=0.5,
            max_tokens=1400,
        )
        data = _extract_json_object(response or "")
        outcome = str(data.get("outcome") or data.get("narrative") or "completed").strip()

        event_id: int | None = None
        if _bool_from_setting(str(data.get("should_create_event", True))):
            try:
                title = str(data.get("event_title") or "").strip()
                objective = str(data.get("event_objective") or outcome).strip()
                impression = str(data.get("event_impression") or objective).strip()
                kw = data.get("event_keywords") if isinstance(data.get("event_keywords"), list) else []
                cat = data.get("event_categories") if isinstance(data.get("event_categories"), list) else []
                keywords = [str(k) for k in kw if str(k).strip()]
                categories = [str(c) for c in cat if str(c).strip()]
                if objective and impression:
                    r = await self.state_machine.upsert_event(
                        title=title,
                        objective=objective,
                        impression=impression,
                        date=shanghai_now().date().isoformat(),
                        keywords=keywords or None,
                        categories=categories or None,
                        source="generated",
                        update_if_exists=True,
                    )
                    rec = r.get("record") or {}
                    if isinstance(rec, dict) and rec.get("id"):
                        event_id = int(rec["id"])
            except Exception:
                logger.exception("Plan item event upsert failed for item #%s", item.id)

        if action_type == "message_user" and proactive:
            text = str(data.get("narrative") or data.get("outcome") or item.activity).strip()
            if text:
                notif = CharacterNotification(
                    trigger_type="plan_item",
                    trigger_item_id=int(item.id or 0) or None,
                    message_text=text,
                    tone="neutral",
                    status="pending",
                )
                nid = await self.db.insert_character_notification(notif)
                notif.id = nid
                if self._notify is not None:
                    try:
                        await self._notify(notif)
                    except Exception:
                        logger.exception("Notification dispatcher failed for #%s", nid)

        await self.db.update_plan_item(
            int(item.id or 0),
            status="done",
            outcome=outcome,
            outcome_event_id=event_id,
            executed_at=format_utc_instant_z(datetime.utcnow()),
        )

    async def maybe_replan_on_drift(self) -> None:
        if not await self.is_enabled():
            return
        if not await self._bool_setting(KEY_PLAN_REPLAN_ON_DRIFT):
            return
        now = datetime.now(timezone.utc)
        if self._last_drift_at and (now - self._last_drift_at).total_seconds() < 3000:
            return
        self._last_drift_at = now
        plan = await self.get_current_plan()
        if plan is None or plan.id is None:
            return
        pending = await self.db.list_plan_items(int(plan.id), status="pending")
        if not pending:
            return
        latest = await self.db.get_latest_snapshot()
        snap_text = latest.content if latest else ""
        recent = await self.db.get_recent_events_by_event_time(limit=6, include_archived=False)
        recent_txt = "\n".join(f"- [{e.date}] {e.description[:200]}" for e in recent) or "(none)"
        remaining = "\n".join(json.dumps(i.model_dump(), ensure_ascii=False) for i in pending)
        template = await self.prompt_manager.get_prompt(KEY_PROMPT_PLAN_DRIFT_CHECK)
        prompt = _safe_prompt_format(
            template,
            current_environment="[latest snapshot]\n" + snap_text[:4000],
            latest_snapshot=snap_text[:4000],
            remaining_plan_items=remaining,
            recent_events=recent_txt,
        )
        system_prompt = await self.prompt_manager.get_system_prompt()
        response = await self.llm.chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=800,
        )
        data = _extract_json_object(response or "")
        if not _bool_from_setting(str(data.get("should_replan", False))):
            return
        ctx = str(data.get("context") or data.get("reason") or "").strip()
        await self.maybe_replan(trigger="scheduler_drift", context=ctx)

    async def maybe_replan(self, *, trigger: str, context: str = "") -> DailyPlan | None:
        if not await self.is_enabled():
            return None
        if trigger == "conversation_end" and not await self._bool_setting(KEY_PLAN_REPLAN_ON_CONVERSATION):
            return None
        plan = await self.get_current_plan()
        if plan is None or plan.id is None:
            return None
        existing_items = await self.db.list_plan_items(int(plan.id))
        pending = [item for item in existing_items if item.status == "pending"]
        if not pending:
            return None
        remaining = "\n".join(json.dumps(i.model_dump(), ensure_ascii=False) for i in pending)
        latest = await self.db.get_latest_snapshot()
        snap_text = latest.content if latest else ""
        template = await self.prompt_manager.get_prompt(KEY_PROMPT_PLAN_REPLAN)
        prompt = _safe_prompt_format(
            template,
            remaining_plan_items=remaining,
            trigger=trigger,
            context=context or "(none)",
            character_personality=await self.prompt_manager.get_layer_content(KEY_L2_CHARACTER_PERSONALITY),
            relationship_dynamics=await self.prompt_manager.get_layer_content(KEY_L2_RELATIONSHIP_DYNAMICS),
            life_status=await self.prompt_manager.get_layer_content(KEY_L2_LIFE_STATUS),
            latest_snapshot=snap_text[:6000],
        )
        system_prompt = await self.prompt_manager.get_system_prompt()
        response = await self.llm.chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            temperature=0.4,
            max_tokens=2000,
        )
        data = _extract_json_object(response or "")
        if not _bool_from_setting(str(data.get("should_replan", False))):
            return None
        items_raw = data.get("items")
        if not isinstance(items_raw, list) or not items_raw:
            return None
        patch_rows = [row for row in items_raw if isinstance(row, dict)]
        if not patch_rows:
            return None
        merged_items = self._merge_replan_items(
            plan_id=int(plan.id),
            existing_items=existing_items,
            patch_rows=patch_rows,
        )
        raw_plan = json.dumps(
            [item.model_dump(mode="json") for item in merged_items],
            ensure_ascii=False,
        )
        return await self._create_replanned_version(
            current_plan=plan,
            merged_items=merged_items,
            trigger=trigger,
            context=context,
            raw_plan=raw_plan,
            llm_payload=data,
        )

    async def generate_daily_plan(self, plan_date: str) -> DailyPlan:
        char_bg = await self.prompt_manager.get_layer_content(KEY_L1_CHARACTER_BACKGROUND)
        personality = await self.prompt_manager.get_layer_content(KEY_L2_CHARACTER_PERSONALITY)
        rel = await self.prompt_manager.get_layer_content(KEY_L2_RELATIONSHIP_DYNAMICS)
        life = await self.prompt_manager.get_layer_content(KEY_L2_LIFE_STATUS)
        latest = await self.db.get_latest_snapshot()
        latest_snapshot = latest.content if latest else "(no snapshot)"
        recent = await self.db.get_recent_events_by_event_time(limit=12, include_archived=False)
        recent_events = "\n".join(f"- [{e.date}] {e.title or e.description[:160]}" for e in recent) or "(none)"
        npcs = await self.npc_engine.list_active_npcs()
        npc_list = json.dumps([n.model_dump() for n in npcs], ensure_ascii=False)

        yday = (datetime.fromisoformat(plan_date).date() - timedelta(days=1)).isoformat()
        prev_plan = await self.db.get_latest_daily_plan_for_date(yday, status=None)
        previous_plan_summary = "(no previous plan)"
        if prev_plan and prev_plan.id is not None:
            pitems = await self.db.list_plan_items(int(prev_plan.id))
            if pitems:
                previous_plan_summary = "\n".join(
                    f"- {p.hour_start:02d}:00-{p.hour_end:02d}:00 {p.activity} [{p.status}]" for p in pitems
                )

        conv = await self.db.get_latest_snapshot_by_type("conversation_end")
        days_since = "0"
        if conv and conv.created_at:
            try:
                t = parse_db_instant_to_shanghai(conv.created_at)
                delta = shanghai_now().date() - t.date()
                days_since = str(max(0, delta.days))
            except Exception:
                days_since = "0"

        h0 = await self._int_setting(KEY_PLAN_HOUR_START, 7)
        h1 = await self._int_setting(KEY_PLAN_HOUR_END, 23)

        template = await self.prompt_manager.get_prompt(KEY_PROMPT_DAILY_PLAN_GENERATION)
        prompt = _safe_prompt_format(
            template,
            character_background=char_bg,
            character_personality=personality,
            relationship_dynamics=rel,
            life_status=life,
            latest_snapshot=latest_snapshot,
            recent_events=recent_events,
            npc_list=npc_list,
            previous_plan_summary=previous_plan_summary,
            days_since_last_chat=days_since,
            plan_date=plan_date,
            hour_start=h0,
            hour_end=h1,
        )
        system_prompt = await self.prompt_manager.get_system_prompt()
        response = await self.llm.chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            temperature=0.55,
            max_tokens=4000,
        )
        rows = _extract_json_array(response or "")
        raw_plan = (response or "").strip()

        existing = await self.db.get_latest_daily_plan_for_date(plan_date, status="active")
        ctx = json.dumps({"generated_for": plan_date}, ensure_ascii=False)
        if existing and existing.id is not None:
            await self.db.delete_plan_items_for_plan(int(existing.id))
            await self.db.update_daily_plan(
                int(existing.id),
                raw_plan=raw_plan,
                generated_at=format_utc_instant_z(datetime.utcnow()),
                context_snapshot=ctx,
                status="active",
            )
            plan_id = int(existing.id)
            plan = await self.db.get_daily_plan_by_id(plan_id)
            assert plan is not None
        else:
            dp = DailyPlan(
                plan_date=plan_date,
                raw_plan=raw_plan,
                status="active",
                context_snapshot=ctx,
            )
            pid = await self.db.insert_daily_plan(dp)
            plan = await self.db.get_daily_plan_by_id(pid)
            assert plan is not None

        assert plan.id is not None
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            try:
                hs = int(raw.get("hour_start", 0))
                he = int(raw.get("hour_end", hs + 1))
            except Exception:
                continue
            if he <= hs:
                continue
            activity = str(raw.get("activity") or "").strip() or "瀹夋帓"
            action_type = str(raw.get("action_type") or "internal")
            if action_type not in {"internal", "message_user", "web_search", "npc_interaction"}:
                action_type = "internal"
            ap = raw.get("action_payload")
            if not isinstance(ap, dict):
                ap = {}
            action_payload = json.dumps(ap, ensure_ascii=False)
            sk = _normalize_source_kind(str(raw.get("source_kind") or "generated"), default="generated")
            source_ref_id = _normalize_source_ref_id(raw.get("source_ref_id"))
            item = PlanItem(
                plan_id=int(plan.id),
                hour_start=hs,
                hour_end=he,
                activity=activity,
                action_type=action_type,  # type: ignore[arg-type]
                action_payload=action_payload,
                status="pending",
                outcome="",
                source_kind=sk,  # type: ignore[arg-type]
                source_ref_id=source_ref_id,
            )
            await self.db.insert_plan_item(item)

        return plan
