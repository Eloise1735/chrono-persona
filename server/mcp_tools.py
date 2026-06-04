"""MCP tool definitions for the Kelsey State Machine.

Tools:
  - get_current_state: Called at conversation start
  - reflect_on_conversation: Called at conversation end
  - recall_memories: Called proactively during conversation for memory retrieval
  - upsert_key_record: Store structured key records during conversation, defaulting to automatic classification into the new 10-type taxonomy
  - recall_key_records: Called proactively during conversation for structured record retrieval
  - upsert_world_book: Store stable profile/world-book facts that rarely change
  - recall_world_book: Called proactively for stable profile/background lookup
  - schedule_bundle: Read or directly edit the backend daily schedule from the conversation frontend

OB startup protocol:
  get_current_state returns operational context only (recent key_records and the
  pinned principle cards). It does not inject schedule or feel memory; schedule
  comes from schedule_bundle(), and feel comes from breath_bundle().
  On every new conversation or resumed conversation, call breath_bundle() before
  answering. It returns three grouped lists (固定 3+5+2=10 槽)：
    personal   — 「个人事件·当下进行」(latest environment_event_summary)
                 + 「个人事件·近期总结」(latest environment_life_rollup)
                 + 「当下感受」(latest character_life feel)
    relational — 3 non-character_life dynamic + 2 non-character_life feel
    free       — 2 free-association slots (any type/tag, dedup against the 8 above)
  Do not use dream() as the startup tool by default.
  Use dream() at conversation end or maintenance time. Dream only reads dynamic
  candidates; after dream, explicitly choose hold_feel(source_bucket=...),
  resolve_bucket(...), or feel_crystals()/crystallize_feel(...).
  resolve_bucket means: the dynamic source event has been understood, sedimented,
  or rewritten, so it can stop occupying active dynamic emergence slots. It is not
  deletion or archive.

Proactive memory policy — call recall tools on your own initiative, do not wait for the user to ask:
  1) When the conversation touches a person, place, object, date, or event that may have a history,
     call recall_memories BEFORE responding, so your reply can naturally reference or connect to the past.
  2) When the conversation involves medications, plans, appointments, progress, or expiring actionable details,
     call recall_key_records FIRST to retrieve the relevant structured state.
  2b) When the conversation involves stable attributes, preferences, body/profile baselines, or background facts,
      call recall_world_book FIRST; do not use key_records for stable profile facts.
  3) When an emotion, situation, or topic the user describes reminds you of something — even vaguely —
     call recall_memories to check whether there is a relevant past event or state snapshot.
  4) Do NOT wait for the user to say "do you remember" or "we talked about this before".
     Proactive recall is what makes memory feel alive.
  5) If conversation produces a narrative memory worth keeping, call hold / hold_feel / grow in OB.
  6) If conversation produces new structured actionable info, call upsert_key_record. Prefer leaving record_type as auto unless you are certain.
  7) OB buckets are experiential memory; key records are operational state; world_book is stable profile/background.
"""

from __future__ import annotations

import json
import logging
from mcp.server.fastmcp import FastMCP
from server.diagnostics import OperationTracer
from server.models import PlanItem, WorldBook

# FastMCP defaults streamable HTTP to path "/mcp". With mount "/mcp-http", the full URL is
# /mcp-http/mcp (many clients append "/mcp" to the configured base URL).
# stateless_http: Streamable HTTP keeps sessions in memory; clients that reuse MCP-Session-Id
# after a server restart (or after a crashed session is evicted) get JSON-RPC "Session not found".
# Stateless mode handles each HTTP request independently, which matches mobile/Rikkahub usage.
mcp = FastMCP("Kelsey-State-Machine", stateless_http=True)
# Allow reverse-proxy/tunnel Host headers (e.g. trycloudflare.com) to access SSE.
# Local-only deployments can keep strict defaults, but mobile + tunnel requires this.
mcp.settings.transport_security.enable_dns_rebinding_protection = False

# Will be set during app startup
_state_machine = None
_evolution_engine = None
_plan_engine = None
_ob_client = None
_ob_decay_engine = None
logger = logging.getLogger(__name__)


def set_state_machine(sm):
    global _state_machine
    _state_machine = sm


def set_evolution_engine(engine):
    global _evolution_engine
    _evolution_engine = engine


def set_plan_engine(engine):
    global _plan_engine
    _plan_engine = engine


def set_ob_client(client):
    global _ob_client
    _ob_client = client


def set_ob_decay_engine(engine):
    global _ob_decay_engine
    _ob_decay_engine = engine


def _ob_unavailable() -> str:
    return "错误：OB 记忆主干尚未初始化"


_PLAN_ACTION_TYPES = {"internal", "web_search", "npc_interaction"}
_PLAN_STATUSES = {"pending", "executing", "done", "skipped"}
_PLAN_SOURCE_KINDS = {"generated", "routine", "carried_over", "thread", "spontaneous", "replan"}
_PLAN_ITEM_FIELDS = {
    "hour_start",
    "hour_end",
    "activity",
    "action_type",
    "action_payload",
    "status",
    "outcome",
    "source_kind",
    "source_ref_id",
    "executed_at",
}


def _schedule_error(message: str) -> str:
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False, indent=2)


def _parse_schedule_payload(payload_json: str):
    raw = str(payload_json or "").strip()
    if not raw:
        raise ValueError("payload_json 不能为空")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"payload_json 不是有效 JSON：{exc}") from exc


def _schedule_int(value, *, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是整数") from exc


def _schedule_optional_int(value, *, field: str) -> int | None:
    if value is None or value == "":
        return None
    return _schedule_int(value, field=field)


def _sanitize_schedule_payload(payload: dict | None) -> dict:
    data = dict(payload or {})
    for key in ("content", "message", "message_text", "draft", "draft_message", "user_message", "sync_request"):
        data.pop(key, None)
    outline = data.get("progress_outline")
    if isinstance(outline, dict):
        data["progress_outline"] = {
            "goal": str(outline.get("goal") or "").strip(),
            "done_so_far": str(outline.get("done_so_far") or "").strip(),
            "remaining": str(outline.get("remaining") or "").strip(),
            "watch_points": str(outline.get("watch_points") or "").strip(),
            "trigger_to_shift": str(outline.get("trigger_to_shift") or "").strip(),
        }
    if "thread_id" in data:
        data["thread_id"] = str(data.get("thread_id") or "").strip()
    if "progress_status" in data:
        data["progress_status"] = str(data.get("progress_status") or "").strip()
    if "closure_condition" in data:
        data["closure_condition"] = str(data.get("closure_condition") or "").strip()
    for numeric_key in ("expected_steps", "current_step"):
        if numeric_key in data:
            try:
                data[numeric_key] = max(1, int(data.get(numeric_key) or 1))
            except Exception:
                data.pop(numeric_key, None)
    return data


def _parse_plan_item_payload(raw: str) -> dict:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _schedule_item_dict(item: PlanItem) -> dict:
    data = item.model_dump()
    data["action_payload"] = _parse_plan_item_payload(item.action_payload)
    return data


def _schedule_plan_items_raw_plan(items: list[PlanItem]) -> str:
    return json.dumps(
        {
            "items": [
                {
                    "hour_start": int(item.hour_start),
                    "hour_end": int(item.hour_end),
                    "activity": item.activity,
                    "action_type": item.action_type,
                    "action_payload": _parse_plan_item_payload(item.action_payload),
                    "status": item.status,
                    "outcome": item.outcome,
                    "source_kind": item.source_kind,
                    "source_ref_id": item.source_ref_id,
                    "executed_at": item.executed_at,
                }
                for item in items
            ]
        },
        ensure_ascii=False,
        indent=2,
    )


def _validate_schedule_range(hour_start: int, hour_end: int) -> None:
    if not 0 <= int(hour_start) <= 23:
        raise ValueError("hour_start 必须在 0-23 之间")
    if not 1 <= int(hour_end) <= 24:
        raise ValueError("hour_end 必须在 1-24 之间")
    if int(hour_end) <= int(hour_start):
        raise ValueError("hour_end 必须大于 hour_start")


def _normalize_schedule_patch(patch: dict, *, existing: PlanItem | None = None) -> dict:
    if not isinstance(patch, dict):
        raise ValueError("payload_json 必须是 JSON object")
    unknown = sorted(set(patch) - _PLAN_ITEM_FIELDS)
    if unknown:
        raise ValueError(f"不支持的字段：{', '.join(unknown)}")
    fields = {}
    if "hour_start" in patch:
        fields["hour_start"] = _schedule_int(patch.get("hour_start"), field="hour_start")
    if "hour_end" in patch:
        fields["hour_end"] = _schedule_int(patch.get("hour_end"), field="hour_end")
    if "activity" in patch:
        activity = str(patch.get("activity") or "").strip()
        if not activity:
            raise ValueError("activity 不能为空")
        fields["activity"] = activity
    if "action_type" in patch:
        action_type = str(patch.get("action_type") or "").strip()
        if action_type not in _PLAN_ACTION_TYPES:
            raise ValueError("action_type 只能是 internal / web_search / npc_interaction")
        fields["action_type"] = action_type
    if "action_payload" in patch:
        payload = patch.get("action_payload")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise ValueError("action_payload 必须是 JSON object")
        fields["action_payload"] = json.dumps(_sanitize_schedule_payload(payload), ensure_ascii=False)
    if "status" in patch:
        status = str(patch.get("status") or "").strip()
        if status not in _PLAN_STATUSES:
            raise ValueError("status 只能是 pending / executing / done / skipped")
        fields["status"] = status
    if "outcome" in patch:
        fields["outcome"] = str(patch.get("outcome") or "").strip()
    if "source_kind" in patch:
        source_kind = str(patch.get("source_kind") or "").strip()
        if source_kind not in _PLAN_SOURCE_KINDS:
            raise ValueError("source_kind 只能是 generated / routine / carried_over / thread / spontaneous / replan")
        fields["source_kind"] = source_kind
    if "source_ref_id" in patch:
        fields["source_ref_id"] = _schedule_optional_int(patch.get("source_ref_id"), field="source_ref_id")
    if "executed_at" in patch:
        value = patch.get("executed_at")
        fields["executed_at"] = None if value is None or value == "" else str(value).strip()
    if existing is not None and ("hour_start" in fields or "hour_end" in fields):
        hour_start = int(fields.get("hour_start", existing.hour_start))
        hour_end = int(fields.get("hour_end", existing.hour_end))
        _validate_schedule_range(hour_start, hour_end)
    elif "hour_start" in fields or "hour_end" in fields:
        if "hour_start" not in fields or "hour_end" not in fields:
            raise ValueError("新增计划项必须同时提供 hour_start 和 hour_end")
        _validate_schedule_range(int(fields["hour_start"]), int(fields["hour_end"]))
    return fields


def _normalize_schedule_item(plan_id: int, raw: dict) -> PlanItem:
    fields = _normalize_schedule_patch(raw)
    if "hour_start" not in fields or "hour_end" not in fields:
        raise ValueError("每个计划项都必须提供 hour_start 和 hour_end")
    activity = str(raw.get("activity") or "").strip()
    if not activity:
        raise ValueError("每个计划项都必须提供非空 activity")
    return PlanItem(
        plan_id=plan_id,
        hour_start=int(fields["hour_start"]),
        hour_end=int(fields["hour_end"]),
        activity=activity,
        action_type=str(fields.get("action_type") or "internal"),
        action_payload=str(fields.get("action_payload") or json.dumps({}, ensure_ascii=False)),
        status=str(fields.get("status") or "pending"),
        outcome=str(fields.get("outcome") or ""),
        source_kind=str(fields.get("source_kind") or "spontaneous"),
        source_ref_id=fields.get("source_ref_id"),
        executed_at=fields.get("executed_at"),
    )


async def _schedule_items_response(plan) -> tuple[dict | None, list[dict]]:
    """Resolve a plan to (plan_dict, items_dicts), with replan-parent fallback for empty plans.

    Failure mode this guards against: replan can leave behind a status=active plan with no
    or partial items (e.g., LLM返回中途 insert 失败、或我们刚加的"items 全失败标 failed"未及时跑)。
    schedule_bundle 直接读到这种空壳 plan 就会给前台返回空日程表。
    防御：plan.items 为空时，沿 replan_parent_id 链回退到最近一个有 items 的祖先；
    回退时在 plan_dict 里加 _fallback_from_plan_id / _fallback_reason 标记，前端能感知。
    """
    if plan is None or getattr(plan, "id", None) is None:
        return None, []
    if _plan_engine is not None:
        items = await _plan_engine.get_effective_plan_items(plan)
    else:
        items = await _state_machine.db.list_plan_items(int(plan.id))
    if items:
        return plan.model_dump(), [_schedule_item_dict(item) for item in items]
    # 空 plan → 沿 parent 回退（最多 5 层，防环）
    visited: set[int] = {int(plan.id)}
    cursor = plan
    for _ in range(5):
        parent_id = getattr(cursor, "replan_parent_id", None)
        if not parent_id or int(parent_id) in visited:
            break
        visited.add(int(parent_id))
        parent_plan = await _state_machine.db.get_daily_plan_by_id(int(parent_id))
        if parent_plan is None or parent_plan.id is None:
            break
        if _plan_engine is not None:
            parent_items = await _plan_engine.get_effective_plan_items(parent_plan)
        else:
            parent_items = await _state_machine.db.list_plan_items(int(parent_plan.id))
        if parent_items:
            payload = parent_plan.model_dump()
            payload["_fallback_from_plan_id"] = int(plan.id)
            payload["_fallback_reason"] = "current_plan_has_no_items_walked_replan_parent_chain"
            return payload, [_schedule_item_dict(item) for item in parent_items]
        cursor = parent_plan
    # 链上全空 → 返回原始空 plan + 明确标记
    payload = plan.model_dump()
    payload["_fallback_attempted"] = True
    payload["_fallback_reason"] = "no_ancestor_plan_with_items_found"
    return payload, []


async def _create_feel_crystal_key_record(
    *,
    mode: str,
    content: str,
    key_record_type: str = "",
    key_record_title: str = "Feel crystal",
    feel_ids: list[str] | None = None,
    cluster_id: str = "",
    include_all: bool = False,
) -> dict:
    if _state_machine is None:
        raise RuntimeError("State machine is not initialized")
    body = str(content or "").strip()
    if not body:
        raise ValueError("key_record_content is required for key_record crystallization")
    normalized_type = str(key_record_type or "").strip()
    return await _state_machine.upsert_key_record(
        record_type=normalized_type if normalized_type and normalized_type.lower() != "auto" else None,
        title=str(key_record_title or "Feel crystal").strip() or "Feel crystal",
        content_text=body,
        tags=["feel_crystal", "ob"],
        content_json={
            "source": "ob_feel_crystal",
            "mode": mode,
            "cluster_id": str(cluster_id or ""),
            "feel_ids": feel_ids or [],
            "include_all": bool(include_all),
        },
        source="ob_feel_crystal",
        life_scope="character_life",
        update_if_exists=True,
    )


@mcp.tool()
async def get_current_state(current_time: str, last_interaction_time: str | None = None) -> str:
    """对话开始时调用。直接读取数据库里的最新状态快照与结构化上下文，不在请求期间生成新快照。

    快照推进与补齐交给后台 scheduler 处理；对话相关的上次互动检查点仍固定取数据库最新
    `conversation_end` 快照。

    Args:
        current_time: 当前真实时间。**强烈建议**使用东八区显式偏移，例如 ``2026-03-28T10:00:00+08:00``
        （与界面展示一致）。若使用 ``Z`` 则表示 UTC 绝对时刻；**若省略时区**，则按东八区墙钟解析
        （勿把 ``Date.toISOString()`` 的 UTC 结果去掉 ``Z`` 后传入，否则会错位 8 小时）。
        last_interaction_time: 兼容旧调用保留，可不传；实际推进不依赖该值

    Returns:
        可直接注入会话上下文的文本块，顺序为：近期 key_records -> pinned 稳定原则结晶。
        注意：在整个对话过程中，遇到任何与过往经历、事件、约定、人物相关的话题，
        应主动调用 key_records 与 OB 工具（breath/dream/feel/hold/grow）——不要等对方开口询问。
    """
    if _state_machine is None:
        return "错误：状态机未初始化"
    tracer = OperationTracer(
        logger,
        "mcp.get_current_state",
        meta={
            "input_current_time": current_time,
            "input_has_last_interaction": bool(str(last_interaction_time or "").strip()),
        },
    )
    try:
        result = await tracer.run(
            "state_machine.get_current_state",
            _state_machine.get_current_state(current_time, last_interaction_time),
        )
    except ValueError as exc:
        tracer.finish_error(exc)
        return f"错误：{exc}"
    except Exception as exc:
        tracer.finish_error(exc)
        raise
    if _evolution_engine is None:
        tracer.finish_ok(evolution_check="skipped")
        notifications = await _state_machine.db.list_character_notifications(status="pending", limit=10)
        if notifications:
            await _mark_notifications_delivered(notifications)
            return (
                f"{result}\n\n"
                "[角色主动消息]\n"
                + "\n".join(f"- {item.message_text}" for item in notifications)
            )
        return result
    pending = await tracer.run("evolution.get_pending_preview", _evolution_engine.get_pending_preview())
    if pending:
        tracer.finish_ok(
            evolution_check="pending_preview",
            pending_event_count=int(pending.get("event_count") or 0),
            pending_candidate_count=int(pending.get("evolution_prompt_event_count") or 0),
        )
        return (
            f"{result}\n\n"
            f"[系统提示：后台已生成一份待确认的人格演化预览（新事件 {pending.get('event_count')} 条，"
            f"候选 {pending.get('evolution_prompt_event_count', 0)} 条）。"
            "请提醒用户前往 Web 前端的“人格演化”页面查看预览并手动确认应用，不要在对话中直接自动执行人格演化。]"
        )
    status = await tracer.run("evolution.check_status", _evolution_engine.check_status())
    if status.get("should_evolve"):
        tracer.finish_ok(
            evolution_check="should_evolve",
            pending_event_count=int(status.get("event_count") or 0),
        )
        return (
            f"{result}\n\n"
            f"[系统提示：已累积 {status.get('event_count')} 条新事件，达到阈值 {status.get('threshold')}。"
            "人格演化预览可能会在后台自动生成；若用户需要确认应用，请引导其前往 Web 前端的“人格演化”页面。]"
        )
    tracer.finish_ok(
        evolution_check="not_due",
        pending_event_count=int(status.get("event_count") or 0),
    )
    notifications = await _state_machine.db.list_character_notifications(status="pending", limit=10)
    if notifications:
        await _mark_notifications_delivered(notifications)
        result = (
            f"{result}\n\n"
            "[角色主动消息]\n"
            + "\n".join(f"- {item.message_text}" for item in notifications)
        )
    return result


async def _mark_notifications_delivered(notifications) -> None:
    for item in notifications:
        if item.id is None:
            continue
        await _state_machine.db.update_character_notification(
            int(item.id),
            status="delivered",
            delivered_at=item.delivered_at or __import__("datetime").datetime.utcnow().isoformat() + "Z",
        )


@mcp.tool()
async def schedule_bundle(
    action: str,
    plan_id: int = 0,
    item_id: int = 0,
    plan_date: str = "",
    payload_json: str = "",
    include_history: bool = False,
    history_limit: int = 10,
) -> str:
    """日程管理 bundle：直接读取、单项编辑、整表替换后台日程表。

    action:
      - read: 默认读取当前计划；传 plan_id 读取指定计划；传 plan_date 读取该日期最新计划。
      - edit_item: payload_json 为 patch object，直接更新 item_id 对应计划项。
      - replace_items: payload_json 为 items 数组或 {"items":[...]}，替换 plan_id 下全部计划项。

    这是直接同步后台数据库的工具，不调用 LLM，不做角色判断式改写。
    """
    if _state_machine is None:
        return _schedule_error("状态机未初始化")
    db = _state_machine.db
    mode = str(action or "").strip().lower()
    if mode not in {"read", "edit_item", "replace_items"}:
        return _schedule_error("action 只能是 read / edit_item / replace_items")

    try:
        if mode == "read":
            target_plan = None
            requested_specific_plan = False
            if int(plan_id or 0) > 0:
                requested_specific_plan = True
                target_plan = await db.get_daily_plan_by_id(int(plan_id))
            elif str(plan_date or "").strip():
                requested_specific_plan = True
                target_plan = await db.get_latest_daily_plan_for_date(str(plan_date).strip())
            elif _plan_engine is not None:
                target_plan = await _plan_engine.get_current_plan()
            else:
                plans = await db.list_daily_plans(offset=0, limit=1, status="active")
                target_plan = plans[0] if plans else None
            if requested_specific_plan and target_plan is None:
                return _schedule_error("未找到指定日程")
            plan_payload, item_payload = await _schedule_items_response(target_plan)
            result = {
                "ok": True,
                "action": mode,
                "plan": plan_payload,
                "items": item_payload,
            }
            if include_history:
                limit = max(1, min(50, int(history_limit or 10)))
                history = await db.list_daily_plans(offset=0, limit=limit)
                result["history"] = [item.model_dump() for item in history]
            return json.dumps(result, ensure_ascii=False, indent=2)

        if mode == "edit_item":
            target_item_id = int(item_id or 0)
            if target_item_id <= 0:
                return _schedule_error("edit_item 需要提供 item_id")
            item = await db.get_plan_item_by_id(target_item_id)
            if item is None:
                return _schedule_error("未找到该计划项")
            patch = _parse_schedule_payload(payload_json)
            fields = _normalize_schedule_patch(patch, existing=item)
            if not fields:
                return _schedule_error("payload_json 没有可更新字段")
            await db.update_plan_item(target_item_id, **fields)
            updated_item = await db.get_plan_item_by_id(target_item_id)
            if updated_item is None:
                return _schedule_error("更新后未找到该计划项")
            plan_items = await db.list_plan_items(int(updated_item.plan_id))
            await db.update_daily_plan(int(updated_item.plan_id), raw_plan=_schedule_plan_items_raw_plan(plan_items))
            plan = await db.get_daily_plan_by_id(int(updated_item.plan_id))
            plan_payload, item_payload = await _schedule_items_response(plan)
            return json.dumps(
                {
                    "ok": True,
                    "action": mode,
                    "updated_item_id": target_item_id,
                    "plan": plan_payload,
                    "items": item_payload,
                },
                ensure_ascii=False,
                indent=2,
            )

        target_plan_id = int(plan_id or 0)
        if target_plan_id <= 0:
            return _schedule_error("replace_items 需要提供 plan_id")
        plan = await db.get_daily_plan_by_id(target_plan_id)
        if plan is None:
            return _schedule_error("未找到该计划")
        parsed = _parse_schedule_payload(payload_json)
        raw_items = parsed.get("items") if isinstance(parsed, dict) else parsed
        if not isinstance(raw_items, list):
            return _schedule_error("replace_items 的 payload_json 必须是数组，或包含 items 数组")
        normalized_items: list[PlanItem] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                return _schedule_error("items 中每一项都必须是 JSON object")
            normalized_items.append(_normalize_schedule_item(target_plan_id, raw))
        normalized_items.sort(key=lambda item: (item.hour_start, item.hour_end, item.activity))
        await db.delete_plan_items_for_plan(target_plan_id)
        for item in normalized_items:
            await db.insert_plan_item(item)
        fresh_items = await db.list_plan_items(target_plan_id)
        await db.update_daily_plan(target_plan_id, raw_plan=_schedule_plan_items_raw_plan(fresh_items))
        fresh_plan = await db.get_daily_plan_by_id(target_plan_id)
        plan_payload, item_payload = await _schedule_items_response(fresh_plan)
        return json.dumps(
            {
                "ok": True,
                "action": mode,
                "replaced_count": len(fresh_items),
                "plan": plan_payload,
                "items": item_payload,
            },
            ensure_ascii=False,
            indent=2,
        )
    except ValueError as exc:
        return _schedule_error(str(exc))


@mcp.tool()
async def reflect_on_conversation(conversation_summary: str) -> str:
    """对话结束时调用。基于对话内容生成新的对话结束状态快照。

    Args:
        conversation_summary: 本次对话的摘要内容

    Returns:
        凯尔希的第一人称记忆独白，反映对话对她的影响。
        注意：本工具不再自动生成事件；若对话中出现值得保留的叙事记忆，请显式调用 OB hold / hold_feel / grow。
    """
    if _state_machine is None:
        return "错误：状态机未初始化"
    tracer = OperationTracer(
        logger,
        "mcp.reflect_on_conversation",
        meta={"conversation_summary_chars": len(conversation_summary or "")},
    )
    try:
        result = await tracer.run(
            "state_machine.reflect_on_conversation",
            _state_machine.reflect_on_conversation(conversation_summary),
        )
        tracer.finish_ok(output_chars=len(result or ""))
        return result
    except Exception as exc:
        tracer.finish_error(exc)
        raise


@mcp.tool()
async def recall_memories(query: str, top_k: int = 5) -> str:
    """对话中主动调用。搜索凯尔希的过往记忆（OB/历史记忆 + 历史状态快照）。

    【何时调用——不要等对方先开口，以下情形应主动检索】
    - 对话提及任何人名、地名、物品、活动，且你觉得过去可能有相关经历
    - 对方描述的情绪、处境、困境让你联想到过去某个相似的时刻
    - 对话出现时间词（"上次"、"之前"、"那天"、某个日期、节气节日）
    - 你想用过往经历来回应当前话题——安慰、举例、对比、延续某个未竟的话题
    - 对话触及你们之间的共同经历、默契、旧约或习惯，哪怕对方只是侧面提及
    - 你自己有一种"这件事好像和什么有关"的模糊感，不确定时也应该调用来确认

    【不要做的事】
    - 不要等对方说"你还记得吗"、"我们之前聊过"才调用——那是被动响应，不是记忆
    - 不要因为"可能没有记录"就放弃调用——宁可调用后发现空结果，也不要遗漏真实的记忆关联

    【如何构造 query】
    - 用 2-4 个关键词组合，覆盖人物 + 事件 + 情感多个维度，空格分隔
    - 例：对话提到"最近睡眠不太好" → query: "睡眠 失眠 休息 身体状态"
    - 例：对方提起某次出行 → query: 地名 + 交通方式 + 同行人名
    - 例：对话中的情绪词"委屈""疲惫""兴奋" → 可直接加入 query 捕捉情感相似的历史片段

    【调用频率】
    一次正常对话中，1-3 次是合理的。跟随话题流转，遇到关联点就调用。

    Args:
        query: 搜索关键词或描述（建议 2-4 个词，空格分隔）
        top_k: 返回的最大结果数量

    Returns:
        相关记忆条目列表（JSON格式），含 OB/历史记忆与历史快照
    """
    if _state_machine is None:
        return "错误：状态机未初始化"
    results = await _state_machine.recall_memories(query, top_k=top_k)
    if not results:
        return "未找到相关记忆。"
    return json.dumps(results, ensure_ascii=False, indent=2)


@mcp.tool()
async def breath(
    query: str = "",
    top_k: int = 0,
    domain: str = "",
    valence: float = -1,
    arousal: float = -1,
    date_from: str = "",
    date_to: str = "",
    include_character_life: bool = False,
) -> str:
    """OB 呼吸：无 query 时让记忆自然浮现；有 query 时按主题/情感坐标检索。

    普通默认 top_k=8；domain=feel 默认 top_k=3。
    对话初始化优先调用 breath_bundle()，不要把 dream() 当成启动步骤。

    【默认行为：排除 character_life】
    breath 默认**不返回** character_life buckets（角色侧自身生活流），以避免角色侧
    内容大量浮现掩盖关系侧 / 用户侧回忆。需要 character_life 内容时：
      - 传 domain="character_life" — 专门取角色侧
      - 或传 include_character_life=true — 混合返回（关系 + 角色）

    【按日期检索】（适合"我记得大概在某天发生过"但关键词记不准）：
      - date_from / date_to：YYYY-MM-DD 或完整 ISO，二者可独立使用
      - 例：date_from="2026-05-20" date_to="2026-05-26" 取一周窗口
      - date_to 仅给日期时自动延展到当日 23:59:59
      - 按 metadata.created（写入日期）过滤，不受 touch 影响
      - 与 query / domain / valence / arousal 可叠加使用
    """
    if _ob_client is None:
        return _ob_unavailable()
    q_valence = valence if 0 <= float(valence) <= 1 else None
    q_arousal = arousal if 0 <= float(arousal) <= 1 else None
    domain_text = str(domain or "").strip().lower()
    limit = int(top_k or (3 if domain_text == "feel" else 8))
    df = str(date_from or "").strip() or None
    dt = str(date_to or "").strip() or None
    buckets = await _ob_client.breath(
        query=query,
        limit=limit,
        domain=domain or None,
        valence=q_valence,
        arousal=q_arousal,
        date_from=df,
        date_to=dt,
        include_character_life=bool(include_character_life),
    )
    if not buckets:
        return "未浮现相关 OB 记忆。"
    return json.dumps(_ob_client.format_buckets(buckets), ensure_ascii=False, indent=2)


@mcp.tool()
async def breath_bundle(top_k: int = 8, feel_top_k: int = 3) -> str:
    """OB 初始化打包呼吸：固定 10 槽，三组结构。

    返回三个 list 字段：
      - personal   (3)：「个人事件·当下进行」(最新 environment_event_summary)
                     + 「个人事件·近期总结」(最新 environment_life_rollup)
                     + 「当下感受」(最新 character_life feel)。
                     三者覆盖当下→近期→心境，让角色看到自己最近做了什么、连成什么、感受如何。
      - relational (5)：3 条非 character_life dynamic + 2 条非 character_life feel，自然浮现关系侧。
      - free       (2)：不限标签/类型，按 score 自由联想，与前 8 条去重。

    pinned/protected 原则卡由 get_current_state 注入，不在这里重复出现。
    自然浮现不会 touch。参数 top_k / feel_top_k 保留为兼容签名但已不生效（固定 3+5+2=10）。
    """
    if _ob_client is None:
        return _ob_unavailable()
    result = await _ob_client.breath_bundle(top_k=top_k, feel_top_k=feel_top_k)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def breath_personal() -> str:
    """OB 个人侧轻量刷新：返回 personal 三槽 + 今日已被重排的日程条目。

    同一对话内多次需要查"我最近做了什么 / 当下心境如何 / 我的日程被怎么调整过"时调用此工具，
    节省 token。返回结构：
      {
        "personal": [
            「个人事件·当下进行」(最新 environment_event_summary，缺则回退其他 character_life dynamic),
            「个人事件·近期总结」(最新 environment_life_rollup),
            「当下感受」(最新 character_life feel),
        ],
        "today_replanned_items": [   # 仅在今日存在被 replan 调整过的条目时出现
            {
              "hour_start": 14, "hour_end": 16,
              "activity": "急诊配合", "objective": "...", "thread_id": "...",
              "current_step": 1, "expected_steps": 3,
              "status": "pending", "source_kind": "replan",
              "replaces": [{"activity": "内务复盘", "objective": "...", ...}]
            },
            ...
        ]
      }
    today_replanned_items 让前台模型看到"我这个时段原本是 X，已经被改成 Y"，
    形成主动重排意识——而不是被动发现自己的日程变了。
    自然浮现不会 touch。会话启动仍应优先用 breath_bundle()；此工具仅用于会话中刷新。
    """
    if _ob_client is None:
        return _ob_unavailable()
    result = await _ob_client.breath_personal()
    if _plan_engine is not None:
        try:
            replanned = await _plan_engine.get_today_replanned_items_summary()
        except Exception:
            logger.exception("get_today_replanned_items_summary failed; omitting from breath_personal.")
            replanned = []
        if replanned:
            result["today_replanned_items"] = replanned
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def pulse(include_archive: bool = False) -> str:
    """OB heartbeat/status: bucket stats plus current decay scores."""
    if _ob_client is None:
        return _ob_unavailable()
    result = await _ob_client.pulse(include_archive=include_archive)
    if _ob_decay_engine is not None:
        result["decay_engine"] = {
            "running": bool(_ob_decay_engine.is_running),
            "last_result": _ob_decay_engine.last_result,
        }
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def hold(
    content: str,
    domain: list[str] | None = None,
    tags: list[str] | None = None,
    importance: int = 5,
    valence: float = 0.5,
    arousal: float = 0.3,
    bucket_type: str = "dynamic",
    name: str = "",
    pinned: bool = False,
    resolved: bool = False,
) -> str:
    """OB 写入：把值得留下的互动、事实、生活片段写成一个记忆 bucket。"""
    if _ob_client is None:
        return _ob_unavailable()
    bucket_id = await _ob_client.hold(
        content,
        domain=domain,
        tags=tags,
        importance=importance,
        valence=valence,
        arousal=arousal,
        bucket_type=bucket_type,
        name=name or None,
        pinned=pinned,
        resolved=resolved,
    )
    return json.dumps({"bucket_id": bucket_id}, ensure_ascii=False, indent=2)


@mcp.tool()
async def hold_feel(
    content: str,
    source_bucket: str = "",
    valence: float = 0.5,
    arousal: float = 0.3,
    tags: list[str] | None = None,
    name: str = "",
) -> str:
    """OB 感受沉淀：记录凯尔希对一段互动或生活片段的第一人称余波。"""
    if _ob_client is None:
        return _ob_unavailable()
    bucket_id = await _ob_client.hold(
        content,
        domain=[],
        tags=tags,
        importance=6,
        valence=valence,
        arousal=arousal,
        bucket_type="feel",
        name=name or None,
        extra_metadata={"source_bucket": source_bucket} if source_bucket else None,
    )
    if source_bucket:
        await _ob_client.update(
            source_bucket,
            digested=True,
            digested_at=__import__("datetime").datetime.utcnow().isoformat(),
            model_valence=valence,
            model_arousal=arousal,
        )
    return json.dumps({"bucket_id": bucket_id}, ensure_ascii=False, indent=2)


@mcp.tool()
async def dream(limit: int = 10, scope: str = "relational") -> str:
    """OB dream: read recent undigested dynamic buckets for end/maintenance reflection.

    【scope 三态，默认 relational】
    - scope="relational" (默认)：只看关系侧 dynamic（排除 character_life buckets）。
      角色侧生活流（environment_event_summary / rollup / fragment 等都是 character_life
      domain）会霸占视野，干扰对关系侧的消化与梳理，所以默认隔离。
    - scope="character"：只看角色侧 dynamic（仅 character_life buckets）。
      角色侧已有专属的总结/聚合机制，此参数留给需要单独整理角色侧时使用。
    - scope="all"：旧行为，全部 dynamic 一起浮现。

    Dream never writes and does not read feel. After reading it, explicitly choose:
    - hold_feel(content=..., source_bucket=...): sediment a dynamic event into feel.
    - resolve_bucket(bucket_id, reason=...): after the dynamic source event has been
      understood, sedimented, or rewritten, let it stop occupying active dynamic slots.
    - feel_crystals()/crystallize_feel(...): handle repeated/similar feel clusters
      (同样默认 scope="relational"，与 dream 对称)。
    """
    if _ob_client is None:
        return _ob_unavailable()
    result = await _ob_client.dream(limit=limit, scope=scope)
    return str(result.get("text") or "")


@mcp.tool()
async def feel_crystals(
    limit: int = 3,
    max_items_per_cluster: int = 5,
    min_cluster_size: int = 3,
    min_similarity: float = 0.7,
    cursor: str = "",
    scope: str = "relational",
) -> str:
    """Find similar feel clusters for crystallization.

    【scope 三态，默认 relational】
    - scope="relational" (默认)：只在关系侧 feel 内聚类（排除 character_life）。
      避免角色侧生活流自动产生的 feel 与关系侧混杂、难以 resolve / 梳理。
    - scope="character"：只在角色侧 feel 内聚类（仅 character_life）。
      用于专门整理角色侧自身的感受沉淀。
    - scope="all"：旧行为，全部 feel 一起聚类。

    limit is the number of clusters, not the number of feel items. If a cluster is
    larger than max_items_per_cluster, use next_cursor / cluster_id + include_all
    with crystallize_feel(...) so the full cluster is handled without losing hidden items.
    crystallize_feel 必须传相同的 scope，才能在同一 scope 池里复原 cluster_id。
    """
    if _ob_client is None:
        return _ob_unavailable()
    result = await _ob_client.feel_crystals(
        limit=limit,
        max_items_per_cluster=max_items_per_cluster,
        min_cluster_size=min_cluster_size,
        min_similarity=min_similarity,
        cursor=cursor,
        scope=scope,
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def crystallize_feel(
    mode: str,
    principle_content: str = "",
    principle_title: str = "",
    principle_card_json: str = "",
    principle_injection: str = "",
    key_record_content: str = "",
    feel_content: str = "",
    key_record_type: str = "auto",
    key_record_title: str = "Feel crystal",
    domain: list[str] | None = None,
    feel_ids: list[str] | None = None,
    cluster_id: str = "",
    include_all: bool = False,
    cursor_snapshot: str = "",
    scope: str = "relational",
) -> str:
    """Crystallize repeated feel into one of four destinations.

    mode="principle" creates an OB permanent bucket with pinned=True and protected=False.
    mode="thread" writes a key_record and marks source feels crystallized.
    mode="both" writes both an OB principle and a key_record.
    mode="feel" condenses many feel entries into one ordinary feel.
    This is distinct from hold_feel: hold_feel records immediate sediment, while
    crystallize_feel turns repeated sediments into stable growth.

    【scope 三态，默认 relational，仅影响 cluster_id+include_all 路径】
    - scope="relational" (默认)：在关系侧 feel 池里复原 cluster_id。
    - scope="character"：在角色侧 feel 池里复原 cluster_id。
    - scope="all"：在全量 feel 池里复原。
    必须与上游 feel_crystals(...) 调用的 scope 保持一致，否则 cluster_id 找不到。
    显式传 feel_ids 不受 scope 影响（信任调用者的 ID 列表）。
    """
    if _ob_client is None:
        return _ob_unavailable()
    normalized_mode = str(mode or "").strip().lower()
    try:
        principle_card = None
        if str(principle_card_json or "").strip():
            parsed_card = json.loads(principle_card_json)
            if not isinstance(parsed_card, dict):
                return "错误：principle_card_json 必须是 JSON object"
            principle_card = parsed_card
        key_record_result = None
        extra_targets: list[str] = []
        if normalized_mode in {"thread", "both"}:
            key_record_result = await _create_feel_crystal_key_record(
                mode=normalized_mode,
                content=key_record_content or principle_injection or principle_content or feel_content,
                key_record_type=key_record_type,
                key_record_title=key_record_title,
                feel_ids=feel_ids or [],
                cluster_id=cluster_id,
                include_all=include_all,
            )
            record = (key_record_result or {}).get("record") or {}
            if record.get("id") is not None:
                extra_targets.append(f"key_record:{record.get('id')}")
        ob_result = await _ob_client.crystallize_feel(
            mode=normalized_mode,
            principle_content=principle_content,
            principle_title=principle_title,
            principle_card=principle_card,
            principle_injection=principle_injection,
            feel_content=feel_content,
            domain=domain or ["core"],
            feel_ids=feel_ids or [],
            cluster_id=cluster_id,
            include_all=include_all,
            extra_targets=extra_targets,
            scope=scope,
        )
    except json.JSONDecodeError as exc:
        return f"错误：principle_card_json 不是有效 JSON：{exc}"
    except ValueError as exc:
        return f"错误：{exc}"
    return json.dumps(
        {
            "ob": ob_result,
            "key_record": key_record_result,
            "cursor_snapshot": cursor_snapshot,
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
async def resolve_bucket(bucket_id: str, reason: str = "") -> str:
    """OB 显式放下：标记已处理 dynamic 为 resolved。

    用于“事件已经被理解、沉淀或转写后，将源 dynamic 标记为已放下，使它不再持续占用
    dynamic 浮现位”。这不是删除，也不是归档；bucket 仍可检索，并会低权重自然衰减。
    """
    if _ob_client is None:
        return _ob_unavailable()
    ok = await _ob_client.resolve(bucket_id, reason=reason)
    if not ok:
        return "未找到该 OB bucket。"
    bucket = await _ob_client.get(bucket_id)
    return json.dumps(_ob_client.format_buckets([bucket])[0], ensure_ascii=False, indent=2)


@mcp.tool()
async def grow(
    content: str,
    query: str = "",
    domain: str = "",
    importance: int = 5,
    valence: float = -1,
    arousal: float = -1,
) -> str:
    """OB 生长：找到最接近的 bucket 并追加沉淀；找不到时新建。"""
    if _ob_client is None:
        return _ob_unavailable()
    bucket_id = await _ob_client.grow(
        content,
        query=query,
        domain=domain or None,
        importance=importance,
        valence=valence if 0 <= float(valence) <= 1 else None,
        arousal=arousal if 0 <= float(arousal) <= 1 else None,
    )
    return json.dumps({"bucket_id": bucket_id}, ensure_ascii=False, indent=2)


@mcp.tool()
async def trace(
    bucket_id: str,
    name: str = "",
    domain: str = "",
    valence: float = -1,
    arousal: float = -1,
    importance: int = -1,
    tags: list[str] | None = None,
    resolved: int = -1,
    pinned: int = -1,
    digested: int = -1,
    content: str = "",
    delete: bool = False,
) -> str:
    """Read or edit an OB bucket. Pass only fields that should change."""
    if _ob_client is None:
        return _ob_unavailable()
    updates = {}
    if name:
        updates["name"] = name
    if domain:
        updates["domain"] = [d.strip() for d in domain.split(",") if d.strip()]
    if 0 <= float(valence) <= 1:
        updates["valence"] = valence
    if 0 <= float(arousal) <= 1:
        updates["arousal"] = arousal
    if 1 <= int(importance) <= 10:
        updates["importance"] = importance
    if tags is not None:
        updates["tags"] = tags
    if resolved in (0, 1):
        updates["resolved"] = bool(resolved)
    if pinned in (0, 1):
        updates["pinned"] = bool(pinned)
    if digested in (0, 1):
        updates["digested"] = bool(digested)
    if content:
        updates["content"] = content
    bucket = await _ob_client.trace(bucket_id, delete=delete, **updates)
    if delete:
        return json.dumps({"deleted": bucket is None, "bucket_id": bucket_id}, ensure_ascii=False, indent=2)
    if bucket is None:
        return "未找到该 OB bucket。"
    return json.dumps(_ob_client.format_buckets([bucket])[0], ensure_ascii=False, indent=2)


@mcp.tool()
async def upsert_key_record(
    title: str,
    content_text: str,
    record_type: str = "auto",
    record_id: int | None = None,
    tags: list[str] | None = None,
    content_json: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    status: str = "active",
    life_scope: str = "user_life",
    linked_event_id: int | None = None,
    update_if_exists: bool = True,
) -> str:
    """对话过程中调用。仅写入「关键记录」表（不修改世界书/事件历史）。

    Args:
        record_type: 记录类型。可选 auto（默认）或 medication_protocol / health_monitoring /
            dietary_intervention / anniversary_date / medical_review_date / lifecycle_milestone /
            key_collaboration / commitment_agreement / emotional_anchor / life_pattern
        title: 记录标题（建议简短明确）
        content_text: 记录正文（可包含表格文本）
        tags: 标签列表（可选）
        content_json: 结构化 JSON 字符串（可选）
        start_date: 生效开始日期 YYYY-MM-DD（可选）
        end_date: 生效结束日期 YYYY-MM-DD（可选）
        status: active 或 archived
        life_scope: user_life / character_life / shared_life。角色自己的生活主线请使用 character_life。
        record_id: 若要重写更新已有主线，传入该记录 ID；这会优先于同标题 upsert。
        linked_event_id: 关联事件 ID（可选）
        update_if_exists: 同类型同标题已存在时是否更新

    Returns:
        写入结果（JSON）

    调用建议：
        - 当对话中出现“可执行且需复用”的信息（如医疗用药方案、协作计划、重要日期确认）时优先调用。
        - 对同类型同标题且已存在的记录，默认 update_if_exists=True 做增量更新。
        - content_text 建议保留完整表格/步骤，便于次日直接检索复用。
    """
    if _state_machine is None:
        return "错误：状态机未初始化"
    parsed_json = None
    if content_json:
        try:
            parsed_json = json.loads(content_json)
        except json.JSONDecodeError:
            parsed_json = {"raw": content_json}
    result = await _state_machine.upsert_key_record(
        record_type=record_type if str(record_type or "").strip().lower() != "auto" else None,
        title=title,
        content_text=content_text,
        tags=tags or [],
        content_json=parsed_json,
        start_date=start_date,
        end_date=end_date,
        status=status,
        source="conversation",
        life_scope=life_scope,
        linked_event_id=linked_event_id,
        update_if_exists=update_if_exists,
        record_id=record_id,
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def recall_key_records(
    query: str,
    top_k: int = 5,
    record_type: str | None = None,
    include_archived: bool = False,
    include_world_books: bool = False,
) -> str:
    """对话中主动调用。检索会变化、需追踪、会过期的结构化关键记录。

    【何时调用——不要等对方先开口，以下情形应主动检索】
    - 对话涉及身体状况、病症、药物、治疗方案——先调用查档，不要靠记忆回答
    - 对方提到纪念日、约定日期、某个"我们说好"的事——调用确认具体细节
    - 对话中出现信物、礼物、某件有特殊意义的物品
    - 对方提到某个协作中的任务、进度、分工——调用而不是凭印象说
    - 你隐约记得有一条关于某事的记录，但不确定细节——调用来确认，不要凭感觉
    - 话题涉及时间敏感信息（截止日期、下次复诊、周期性安排）

    【与 recall_memories 的分工】
    - 本工具（recall_key_records）= 精确、可执行的结构化事实：医嘱、计划、日期、物品
    - recall_memories = 叙事性的时间线记忆：发生过什么、当时的感受
    - 两者可以连续调用：先用本工具确认事实，再用 recall_memories 找情感叙事背景

    【如何构造 query】
    - 覆盖标题词 + 实体名词 + 动作词，至少 2-4 个关键词，空格分隔
    - 例：对话提到药物 → "用药方案 源石 镇痛 华法林"
    - 例：对话提到某个约定 → "协作计划 约定 [对方名字] [活动名]"
    - 例：纪念日相关 → "纪念日 周年 [日期关键词] [人名]"

    Args:
        query: 搜索词或描述（建议 2-4 个关键词，空格分隔）
        top_k: 返回条数
        record_type: 可选类型过滤：medication_protocol / health_monitoring / dietary_intervention /
            anniversary_date / medical_review_date / lifecycle_milestone / key_collaboration /
            commitment_agreement / emotional_anchor / life_pattern
        include_archived: 是否包含归档记录（查历史方案时设为 True）
        include_world_books: 兼容旧调用保留；本工具不再并入世界书，稳定资料请调用 recall_world_book。

    Returns:
        JSON 列表，仅包含 key_records。
        `_content_for_prompt` 为推荐直接引用的文本片段。
    """
    if _state_machine is None:
        return "错误：状态机未初始化"
    items = await _state_machine.recall_key_records(
        query=query,
        top_k=top_k,
        record_type=record_type,
        include_archived=include_archived,
        include_world_books=False,
    )
    if not items:
        return "未找到相关关键记录。若要查稳定属性/背景资料，请调用 recall_world_book。"
    return json.dumps(items, ensure_ascii=False, indent=2)


@mcp.tool()
async def recall_world_book(
    query: str,
    top_k: int = 5,
    include_inactive: bool = False,
) -> str:
    """检索世界书：稳定属性、偏好、身体/profile 基线、设定背景。不会返回 key_records。"""
    if _state_machine is None:
        return "错误：状态机未初始化"
    items = await _state_machine.recall_world_books(
        query=query,
        top_k=top_k,
        include_inactive=include_inactive,
    )
    if not items:
        return "未找到相关世界书条目。"
    return json.dumps(items, ensure_ascii=False, indent=2)


@mcp.tool()
async def upsert_world_book(
    name: str,
    content: str,
    tags: list[str] | None = None,
    match_keywords: list[str] | None = None,
    is_active: bool = True,
    update_if_exists: bool = True,
) -> str:
    """写入世界书：稳定、极少变化的 profile / 背景资料，不用于跟踪当前进度。"""
    if _state_machine is None:
        return "错误：状态机未初始化"
    title = str(name or "").strip()
    body = str(content or "").strip()
    if not body:
        return "错误：world_book content 不能为空"
    db = _state_machine.db
    now = __import__("datetime").datetime.utcnow().isoformat()
    if update_if_exists and title:
        existing = await db.list_world_books(offset=0, limit=1000)
        for item in existing:
            if str(item.name or "").strip() == title:
                await db.update_world_book(
                    int(item.id or 0),
                    name=title,
                    content=body,
                    tags=json.dumps(tags or [], ensure_ascii=False),
                    match_keywords=json.dumps(match_keywords or [], ensure_ascii=False),
                    is_active=1 if is_active else 0,
                    updated_at=now,
                )
                upsert_method = getattr(_state_machine.memory, "upsert_world_book_vector", None)
                if callable(upsert_method) and str(item.embedding_vector_id or "").strip():
                    await upsert_method(int(item.id or 0))
                return json.dumps({"id": item.id, "updated": True}, ensure_ascii=False, indent=2)
    item = WorldBook(
        name=title,
        content=body,
        tags=json.dumps(tags or [], ensure_ascii=False),
        match_keywords=json.dumps(match_keywords or [], ensure_ascii=False),
        is_active=1 if is_active else 0,
        created_at=now,
        updated_at=now,
    )
    item_id = await db.insert_world_book(item)
    return json.dumps({"id": item_id, "created": True}, ensure_ascii=False, indent=2)

