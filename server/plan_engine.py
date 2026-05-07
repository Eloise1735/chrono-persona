"""Daily plan generation, execution, and replanning."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
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
PLAN_BASELINE_PLAN_ID_KEY = "plan_baseline_plan_id"
PLAN_SCHEMA_VERSION = "v2_structural_schedule"
_DEFAULT_PROGRESS_OUTLINE = {
    "goal": "",
    "done_so_far": "",
    "remaining": "",
    "watch_points": "",
    "trigger_to_shift": "",
}
_DEFAULT_PROGRESS_STATE = {
    "thread_id": "",
    "expected_steps": 2,
    "current_step": 1,
    "progress_status": "open",
    "closure_condition": "",
}
_PLAN_MESSAGE_PAYLOAD_KEYS = {
    "content",
    "message",
    "message_text",
    "draft",
    "draft_message",
    "user_message",
    "sync_request",
}
_DIRECT_USER_CONTACT_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(向|给).{0,6}(用户|eloise|泳琳).{0,10}(发|发送|同步|联系|分享|汇报|询问)",
        r"(发|发送|同步|联系|回复|询问).{0,8}(用户|eloise|泳琳)",
        r"(问候|晚安|早安).{0,8}(用户|eloise|泳琳)",
        r"(通过终端|经由终端).{0,12}(陪伴|联系|同步)",
    )
]


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
    if action_type not in {"internal", "web_search", "npc_interaction"}:
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


def _parse_action_payload_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _looks_like_user_contact(text: str) -> bool:
    sample = str(text or "").strip()
    if not sample:
        return False
    return any(pattern.search(sample) for pattern in _DIRECT_USER_CONTACT_PATTERNS)


def _neutralize_plan_text(text: Any, *, fallback: str) -> str:
    sample = str(text or "").strip()
    if not sample:
        return fallback
    if _looks_like_user_contact(sample):
        return fallback
    return sample


def _normalize_progress_outline(raw: Any) -> dict[str, str]:
    source = raw if isinstance(raw, dict) else {}
    return {
        key: str(source.get(key) or "").strip()
        for key in _DEFAULT_PROGRESS_OUTLINE
    }


def _default_progress_outline(objective: str, *, activity: str) -> dict[str, str]:
    focus = objective or activity or "当前结构任务"
    return {
        "goal": focus,
        "done_so_far": "已具备基本上下文与执行条件",
        "remaining": f"推进并完成与“{focus}”直接相关的关键部分",
        "watch_points": "",
        "trigger_to_shift": "",
    }


def _make_activity_label(activity: str, objective: str) -> str:
    for candidate in (activity, objective):
        text = str(candidate or "").strip()
        if not text:
            continue
        short = re.split(r"[，。；;、\n]", text, maxsplit=1)[0].strip()
        short = short[:28].strip()
        if short:
            return short
    return "结构任务"


def _classify_plan_focus(dominant_mode: str, objective: str, constraint_source: str) -> str:
    text = f"{dominant_mode} {constraint_source} {objective}".lower()
    if any(token in text for token in ["medical", "monitor", "监测", "复核", "代谢", "临床", "药物"]):
        return "medical"
    if any(token in text for token in ["recovery", "rest", "休息", "恢复", "调节", "低负载"]):
        return "recovery"
    if any(token in text for token in ["search", "采购", "筛选", "检索", "查询", "比对"]):
        return "research"
    if any(token in text for token in ["external", "collaboration", "npc", "协作", "对接", "反馈"]):
        return "coordination"
    if any(token in text for token in ["buffer", "wait", "等待", "缓冲"]):
        return "buffer"
    if any(token in text for token in ["deep", "writing", "批注", "整理", "推进", "撰写"]):
        return "deep_work"
    return "routine"


def _default_failure_cost(dominant_mode: str, flexibility: str, constraint_source: str, objective: str) -> str:
    focus = _classify_plan_focus(dominant_mode, objective, constraint_source)
    if flexibility == "flexible" or focus == "buffer":
        return "minimal"
    if focus == "medical":
        return "medical_continuity_break"
    if focus == "recovery":
        return "recovery_window_lost"
    if focus == "coordination":
        return "coordination_delay"
    if focus == "deep_work":
        return "context_loss"
    return "mild_schedule_drift"


def _default_watch_points(dominant_mode: str, constraint_source: str, objective: str) -> str:
    focus = _classify_plan_focus(dominant_mode, objective, constraint_source)
    if focus == "medical":
        return "监测指标波动；身体负荷变化；关键资料或样本不齐"
    if focus == "recovery":
        return "疲劳堆积；恢复窗口被压缩；外部任务突然插入"
    if focus == "research":
        return "检索结果质量；库存或参数变化；时间被前序任务挤压"
    if focus == "coordination":
        return "协作方反馈延迟；临时改口；对接信息不完整"
    if focus == "buffer":
        return "前序任务拖尾；突发插入；注意力无法重新收束"
    if focus == "deep_work":
        return "注意力衰减；参考材料不齐；被高优先级事务打断"
    return "身体负荷波动；外部打断；前序任务拖尾"


def _default_trigger_to_shift(
    dominant_mode: str,
    flexibility: str,
    constraint_source: str,
    objective: str,
) -> str:
    focus = _classify_plan_focus(dominant_mode, objective, constraint_source)
    if focus == "medical":
        return "若监测异常或关键资料迟到，则延长本段并压缩后续低刚性任务"
    if focus == "recovery":
        return "若负荷继续上升，则直接切入恢复或低负载块，不再维持原推进强度"
    if focus == "research":
        return "若检索条件不足或外部参数变化，则转为整理已有材料并等待下一窗口"
    if focus == "coordination":
        return "若协作反馈未到位，则改为内部整理或顺延，避免空等"
    if focus == "buffer" or flexibility == "flexible":
        return "若前序拖尾或突发插入，则吸收偏移并顺延原目标，不额外制造新任务"
    if focus == "deep_work":
        return "若专注连续性被破坏，则改为收束已有进度，并把深度推进后移"
    return "若出现更高优先级打断或执行条件不足，则顺延或转入缓冲"


def _default_expected_steps(dominant_mode: str, objective: str, constraint_source: str) -> int:
    focus = _classify_plan_focus(dominant_mode, objective, constraint_source)
    if focus in {"medical", "deep_work"}:
        return 4
    if focus in {"research", "coordination"}:
        return 3
    if focus in {"recovery", "buffer"}:
        return 2
    return 2


def _default_closure_condition(dominant_mode: str, objective: str, constraint_source: str) -> str:
    focus = _classify_plan_focus(dominant_mode, objective, constraint_source)
    if focus == "medical":
        return "关键监测或复核结论明确后收束；若出现新异常则转入新线程"
    if focus == "recovery":
        return "身体负荷回落或恢复窗口结束后收束，不再继续细化同一恢复动作"
    if focus == "research":
        return "完成本轮筛选或形成可用结论后收束；若问题改写则另起新线"
    if focus == "coordination":
        return "收到明确反馈或完成交接后收束；若对方改口则拆出新线"
    if focus == "deep_work":
        return "阶段性产出成形后收束；若进入新议题则新开线程"
    if focus == "buffer":
        return "吸收当前偏移后结束，不把缓冲本身扩写成长期事项"
    return "完成当前目标或确认暂时搁置后收束；若议题改变则开启新线"


def _build_thread_id(objective: str, activity: str) -> str:
    base = str(objective or activity or "plan-thread").strip()
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:10]
    return f"plan-{digest}"


def _normalize_progress_state(raw: Any) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    normalized: dict[str, Any] = dict(_DEFAULT_PROGRESS_STATE)
    normalized["thread_id"] = str(source.get("thread_id") or "").strip()
    for key in ("expected_steps", "current_step"):
        try:
            normalized[key] = max(1, int(source.get(key) or normalized[key]))
        except Exception:
            normalized[key] = _DEFAULT_PROGRESS_STATE[key]
    normalized["progress_status"] = str(source.get("progress_status") or "").strip()
    normalized["closure_condition"] = str(source.get("closure_condition") or "").strip()
    return normalized


def _derive_progress_status(
    requested_status: str,
    *,
    current_step: int,
    expected_steps: int,
) -> str:
    allowed = {"open", "advancing", "paused", "completed", "dropped", "ready_to_close"}
    status = str(requested_status or "").strip()
    if status not in allowed:
        status = "advancing" if current_step > 1 else "open"
    if status in {"completed", "dropped", "paused"}:
        return status
    if current_step >= expected_steps:
        return "ready_to_close"
    if current_step > 1:
        return "advancing"
    return "open"


def _is_generic_failure_cost(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    return lowered in {
        "",
        "interruption will create mild schedule drift",
        "mild schedule drift",
        "打断会带来轻微节奏损失",
    }


def _is_generic_watch_points(text: str) -> bool:
    sample = str(text or "").strip()
    return sample in {
        "",
        "留意身体负荷、外部打断、协作依赖与时间挤压",
        "身体负荷波动；外部打断；协作依赖与时间挤压",
    }


def _is_generic_trigger_to_shift(text: str) -> bool:
    sample = str(text or "").strip()
    return sample in {
        "",
        "若出现更高优先级打断或执行条件不足，则顺延或转入缓冲",
    }


def _context_snapshot_with_version(extra: dict[str, Any]) -> str:
    payload = {"plan_schema_version": PLAN_SCHEMA_VERSION}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _plan_schema_version_from_snapshot(raw: str) -> str:
    data = _extract_json_object(raw)
    return str(data.get("plan_schema_version") or "").strip()


def _normalize_plan_structure_fields(raw: dict[str, Any], *, fallback_activity: str) -> dict[str, Any]:
    activity = str(raw.get("activity") or fallback_activity or "").strip()
    payload = _parse_action_payload_dict(raw.get("action_payload"))
    dominant_mode = str(
        raw.get("dominant_mode")
        or payload.get("dominant_mode")
        or payload.get("mode")
        or "internal_maintenance"
    ).strip()
    intended_objective = str(
        raw.get("intended_objective")
        or payload.get("intended_objective")
        or payload.get("objective")
        or activity
    ).strip()
    constraint_source = str(
        raw.get("constraint_source")
        or payload.get("constraint_source")
        or payload.get("constraint")
        or "routine"
    ).strip()
    flexibility = str(
        raw.get("flexibility")
        or payload.get("flexibility")
        or "semi_flexible"
    ).strip()
    if flexibility not in {"rigid", "semi_flexible", "flexible"}:
        flexibility = "semi_flexible"
    failure_cost = str(
        raw.get("failure_cost")
        or payload.get("failure_cost")
        or "打断会带来轻微节奏损失"
    ).strip()
    payload.update(
        {
            "dominant_mode": dominant_mode or "internal_maintenance",
            "intended_objective": intended_objective or activity,
            "constraint_source": constraint_source or "routine",
            "flexibility": flexibility,
            "failure_cost": failure_cost or "打断会带来轻微节奏损失",
        }
    )
    return payload


def _render_plan_item_focus(item: PlanItem) -> str:
    payload = _parse_action_payload_dict(item.action_payload)
    objective = str(payload.get("intended_objective") or "").strip()
    dominant_mode = str(payload.get("dominant_mode") or "").strip()
    flexibility = str(payload.get("flexibility") or "").strip()
    constraint_source = str(payload.get("constraint_source") or "").strip()
    activity = str(item.activity or "").strip()
    parts: list[str] = []
    if objective:
        parts.append(objective)
    elif activity:
        parts.append(activity)
    if dominant_mode:
        parts.append(f"mode={dominant_mode}")
    if flexibility:
        parts.append(f"flex={flexibility}")
    if constraint_source:
        parts.append(f"constraint={constraint_source}")
    return " | ".join(parts)


def _normalize_plan_structure_fields_v2(raw: dict[str, Any], *, fallback_activity: str) -> dict[str, Any]:
    activity = _neutralize_plan_text(
        raw.get("activity") or fallback_activity or "",
        fallback=fallback_activity or "整理与用户相关的话题材料并评估后续推进条件",
    )
    payload = _parse_action_payload_dict(raw.get("action_payload"))
    for key in list(payload.keys()):
        if key in _PLAN_MESSAGE_PAYLOAD_KEYS:
            payload.pop(key, None)
    dominant_mode = str(
        raw.get("dominant_mode")
        or payload.get("dominant_mode")
        or payload.get("mode")
        or "internal_maintenance"
    ).strip()
    intended_objective = str(
        raw.get("intended_objective")
        or payload.get("intended_objective")
        or payload.get("objective")
        or activity
    ).strip()
    intended_objective = _neutralize_plan_text(
        intended_objective,
        fallback="整理与用户相关的话题材料并评估后续推进条件",
    )
    constraint_source = str(
        raw.get("constraint_source")
        or payload.get("constraint_source")
        or payload.get("constraint")
        or "routine"
    ).strip()
    flexibility = str(
        raw.get("flexibility")
        or payload.get("flexibility")
        or "semi_flexible"
    ).strip()
    if flexibility not in {"rigid", "semi_flexible", "flexible"}:
        flexibility = "semi_flexible"
    failure_cost = str(
        raw.get("failure_cost")
        or payload.get("failure_cost")
        or ""
    ).strip()
    if _looks_like_user_contact(failure_cost) or _is_generic_failure_cost(failure_cost):
        failure_cost = _default_failure_cost(dominant_mode, flexibility, constraint_source, intended_objective or activity)
    outline = _normalize_progress_outline(
        raw.get("progress_outline") or payload.get("progress_outline")
    )
    if not any(outline.values()):
        outline = _default_progress_outline(intended_objective, activity=activity)
    else:
        if _looks_like_user_contact(" ".join(outline.values())):
            outline = _default_progress_outline(intended_objective, activity=activity)
        else:
            if not outline["goal"]:
                outline["goal"] = intended_objective or activity
            if not outline["done_so_far"]:
                outline["done_so_far"] = "已具备基本上下文与执行条件"
            if not outline["remaining"]:
                outline["remaining"] = f"推进并完成与“{intended_objective or activity}”直接相关的关键部分"
            if _is_generic_watch_points(outline["watch_points"]):
                outline["watch_points"] = _default_watch_points(dominant_mode, constraint_source, intended_objective or activity)
            if _is_generic_trigger_to_shift(outline["trigger_to_shift"]):
                outline["trigger_to_shift"] = _default_trigger_to_shift(
                    dominant_mode,
                    flexibility,
                    constraint_source,
                    intended_objective or activity,
                )
    if _is_generic_watch_points(outline["watch_points"]):
        outline["watch_points"] = _default_watch_points(dominant_mode, constraint_source, intended_objective or activity)
    if _is_generic_trigger_to_shift(outline["trigger_to_shift"]):
        outline["trigger_to_shift"] = _default_trigger_to_shift(
            dominant_mode,
            flexibility,
            constraint_source,
            intended_objective or activity,
        )
    progress_state = _normalize_progress_state(
        raw.get("progress_state") or payload.get("progress_state") or payload
    )
    expected_steps = max(
        int(progress_state.get("expected_steps") or 0),
        _default_expected_steps(dominant_mode, intended_objective or activity, constraint_source),
    )
    current_step = max(1, int(progress_state.get("current_step") or 1))
    if current_step > expected_steps:
        expected_steps = current_step
    thread_id = str(progress_state.get("thread_id") or "").strip() or _build_thread_id(
        intended_objective or activity,
        activity,
    )
    closure_condition = str(progress_state.get("closure_condition") or "").strip() or _default_closure_condition(
        dominant_mode,
        intended_objective or activity,
        constraint_source,
    )
    progress_status = _derive_progress_status(
        str(progress_state.get("progress_status") or "").strip(),
        current_step=current_step,
        expected_steps=expected_steps,
    )
    payload.update(
        {
            "dominant_mode": dominant_mode or "internal_maintenance",
            "intended_objective": intended_objective or activity,
            "constraint_source": constraint_source or "routine",
            "flexibility": flexibility,
            "failure_cost": failure_cost or _default_failure_cost(dominant_mode, flexibility, constraint_source, intended_objective or activity),
            "progress_outline": outline,
            "thread_id": thread_id,
            "expected_steps": expected_steps,
            "current_step": current_step,
            "progress_status": progress_status,
            "closure_condition": closure_condition,
        }
    )
    return payload


def _render_plan_item_focus_v2(item: PlanItem) -> str:
    payload = _parse_action_payload_dict(item.action_payload)
    objective = str(payload.get("intended_objective") or "").strip()
    outline = _normalize_progress_outline(payload.get("progress_outline"))
    dominant_mode = str(payload.get("dominant_mode") or "").strip()
    flexibility = str(payload.get("flexibility") or "").strip()
    constraint_source = str(payload.get("constraint_source") or "").strip()
    expected_steps = max(1, int(payload.get("expected_steps") or 1))
    current_step = max(1, int(payload.get("current_step") or 1))
    progress_status = str(payload.get("progress_status") or "").strip()
    activity = str(item.activity or "").strip()
    parts: list[str] = []
    if objective:
        parts.append(objective)
    elif activity:
        parts.append(activity)
    parts.append(f"step={current_step}/{expected_steps}")
    if progress_status:
        parts.append(f"progress={progress_status}")
    if outline.get("remaining"):
        parts.append(f"remaining={outline['remaining']}")
    if outline.get("watch_points"):
        parts.append(f"watch={outline['watch_points']}")
    if dominant_mode:
        parts.append(f"mode={dominant_mode}")
    if flexibility:
        parts.append(f"flex={flexibility}")
    if constraint_source:
        parts.append(f"constraint={constraint_source}")
    return " | ".join(parts)


def _render_previous_plan_reference(item: PlanItem, *, schema_version: str) -> str:
    payload = _parse_action_payload_dict(item.action_payload)
    objective = str(payload.get("intended_objective") or "").strip()
    outline = _normalize_progress_outline(payload.get("progress_outline"))
    flexibility = str(payload.get("flexibility") or "").strip()
    dominant_mode = str(payload.get("dominant_mode") or "").strip()
    expected_steps = max(1, int(payload.get("expected_steps") or 1))
    current_step = max(1, int(payload.get("current_step") or 1))
    progress_status = str(payload.get("progress_status") or "").strip()
    focus = objective or (item.activity if schema_version == PLAN_SCHEMA_VERSION else "")
    parts = [f"- {item.hour_start:02d}:00-{item.hour_end:02d}:00"]
    if focus:
        parts.append(focus)
    parts.append(f"step={current_step}/{expected_steps}")
    if progress_status:
        parts.append(f"progress={progress_status}")
    if outline.get("remaining"):
        parts.append(f"remaining={outline['remaining']}")
    if outline.get("watch_points"):
        parts.append(f"watch={outline['watch_points']}")
    if dominant_mode:
        parts.append(f"mode={dominant_mode}")
    if flexibility:
        parts.append(f"flex={flexibility}")
    parts.append(f"[{item.status}]")
    return " | ".join(parts)


def _effective_progress_status_for_item(item: PlanItem, payload: dict[str, Any]) -> str:
    if item.status == "done":
        return "completed"
    if item.status == "skipped":
        return "paused"
    return _derive_progress_status(
        str(payload.get("progress_status") or "").strip(),
        current_step=max(1, int(payload.get("current_step") or 1)),
        expected_steps=max(1, int(payload.get("expected_steps") or 1)),
    )


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

    async def get_baseline_plan_id(self) -> int | None:
        row = await self.db.get_setting(PLAN_BASELINE_PLAN_ID_KEY)
        raw = str((row or {}).get("value") or "").strip()
        return int(raw) if raw.isdigit() and int(raw) > 0 else None

    async def set_baseline_plan(self, plan_id: int) -> DailyPlan:
        plan = await self.db.get_daily_plan_by_id(int(plan_id))
        if plan is None or plan.id is None:
            raise ValueError("Plan not found")
        await self.db.set_setting(
            key=PLAN_BASELINE_PLAN_ID_KEY,
            value=str(int(plan.id)),
            category="plan",
            description="Manual baseline plan id for future daily plan generation",
        )
        return plan

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
        normalized_payload = _normalize_plan_structure_fields_v2(raw, fallback_activity=activity)
        activity = _make_activity_label(activity, str(normalized_payload.get("intended_objective") or ""))
        action_payload = json.dumps(normalized_payload, ensure_ascii=False)
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
            context_snapshot=_context_snapshot_with_version(context_payload),
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
            payload = _parse_action_payload_dict(it.action_payload)
            focus = _render_plan_item_focus_v2(it)
            failure_cost = str(payload.get("failure_cost") or "").strip()
            line = f"- {it.hour_start:02d}:00-{it.hour_end:02d}:00 [{st}] {focus}"
            if failure_cost:
                line += f" | cost={failure_cost}"
            lines.append(line)
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
        return f"{item.hour_start:02d}:00-{item.hour_end:02d}:00 {_render_plan_item_focus_v2(item)}".strip()

    async def get_plan_progress_awareness_text(self) -> str:
        plan = await self.get_current_plan()
        if plan is None or plan.id is None:
            return "（今日暂无可跟踪的事项推进线）"
        items = await self.db.list_plan_items(int(plan.id))
        if not items:
            return "（今日暂无可跟踪的事项推进线）"
        now_hour = shanghai_now().hour
        lines: list[str] = []
        for item in items:
            payload = _parse_action_payload_dict(item.action_payload)
            status = _effective_progress_status_for_item(item, payload)
            expected_steps = max(1, int(payload.get("expected_steps") or 1))
            current_step = max(1, int(payload.get("current_step") or 1))
            objective = str(payload.get("intended_objective") or item.activity or "").strip()
            if item.status == "pending" and item.hour_end <= now_hour and status not in {"completed", "dropped"}:
                status = "paused"
            if status not in {"paused", "advancing", "ready_to_close", "open"}:
                continue
            emotional_hint = {
                "paused": "这条线带着一点未尽与受阻感",
                "advancing": "这条线仍在稳定推进",
                "ready_to_close": "这条线已接近收束，不宜再无限细化",
                "open": "这条线还在铺垫阶段",
            }.get(status, "这条线仍在场")
            lines.append(
                f"- {objective or '当前事项'} | {current_step}/{expected_steps} | {status} | {emotional_hint}"
            )
        return "\n".join(lines[:4]) if lines else "（今日事项推进总体平稳，暂无强烈滞涩线）"

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
        web_ok = await self._bool_setting(KEY_PLAN_WEB_SEARCH_ENABLED)
        npc_ok = await self._bool_setting(KEY_PLAN_NPC_INTERACTION_ENABLED)
        web_base = (await self.prompt_manager.get_config_value(KEY_PLAN_WEB_SEARCH_API_BASE)).strip()
        web_key = (await self.prompt_manager.get_config_value(KEY_PLAN_WEB_SEARCH_API_KEY)).strip()

        latest = await self.db.get_latest_snapshot()
        latest_snapshot = latest.content if latest else "(no snapshot)"
        recent = await self.db.get_recent_events_by_event_time(limit=8, include_archived=False)
        recent_events = "\n".join(f"- [{e.date}] {e.title or e.description[:120]}" for e in recent) or "(none)"

        extra_ctx = ""
        action_type = _normalize_action_type(item.action_type)
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
        remaining = "\n".join(
            json.dumps(
                {
                    **i.model_dump(),
                    "action_payload": _parse_action_payload_dict(i.action_payload),
                },
                ensure_ascii=False,
            )
            for i in pending
        )
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
        remaining = "\n".join(
            json.dumps(
                {
                    **i.model_dump(),
                    "action_payload": _parse_action_payload_dict(i.action_payload),
                },
                ensure_ascii=False,
            )
            for i in pending
        )
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
        baseline_plan_id = await self.get_baseline_plan_id()
        prev_plan = await self.db.get_daily_plan_by_id(baseline_plan_id) if baseline_plan_id else None
        if prev_plan is None:
            prev_plan = await self.db.get_latest_daily_plan_for_date(yday, status=None)
        previous_plan_summary = "(no previous plan)"
        if prev_plan and prev_plan.id is not None:
            pitems = await self.db.list_plan_items(int(prev_plan.id))
            if pitems:
                prev_schema_version = _plan_schema_version_from_snapshot(prev_plan.context_snapshot)
                previous_plan_summary = "\n".join(
                    _render_previous_plan_reference(p, schema_version=prev_schema_version) for p in pitems
                )
                if baseline_plan_id and int(prev_plan.id) == int(baseline_plan_id):
                    previous_plan_summary = f"[manual baseline plan #{int(prev_plan.id)}]\n{previous_plan_summary}"

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
        relationship_feeling_summary = ""
        plan_bias_hint = ""
        try:
            relationship_state = await self.db.get_latest_relationship_state()
            if relationship_state is not None:
                relationship_feeling_summary = str(relationship_state.relationship_feeling_summary or "").strip()
                plan_bias_hint = str(relationship_state.plan_bias_hint or "").strip()
        except Exception:
            relationship_feeling_summary = ""
            plan_bias_hint = ""

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
            relationship_feeling_summary=relationship_feeling_summary,
            plan_bias_hint=plan_bias_hint,
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
        ctx = _context_snapshot_with_version({"generated_for": plan_date})
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
            if action_type not in {"internal", "web_search", "npc_interaction"}:
                action_type = "internal"
            normalized_payload = _normalize_plan_structure_fields_v2(raw, fallback_activity=activity)
            activity = _make_activity_label(activity, str(normalized_payload.get("intended_objective") or ""))
            action_payload = json.dumps(normalized_payload, ensure_ascii=False)
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
