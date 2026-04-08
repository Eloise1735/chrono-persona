"""MCP tool definitions for the Kelsey State Machine.

Tools:
  - get_current_state: Called at conversation start
  - summarize_conversation: Called before conversation end reflection
  - reflect_on_conversation: Called at conversation end
  - recall_memories: Called during conversation for memory retrieval
  - upsert_event: Store narrative events during conversation
  - upsert_key_record: Store structured key records during conversation
  - recall_key_records: Retrieve structured key records during conversation

Recommended tool policy:
  1) If user asks about medication advice, collaborative plans, anniversaries, gifts,
     or previously agreed actionable details, call recall_key_records first.
  2) If conversation produces a narrative event worth keeping in timeline memory,
     call upsert_event to persist it in event history.
  3) If conversation produces new actionable structured info (tables/checklists/instructions),
     call upsert_key_record to persist it.
  4) Event anchors are narrative timeline memory; key records are precise executable memory.
"""

from __future__ import annotations

import json
import logging
from mcp.server.fastmcp import FastMCP
from server.diagnostics import OperationTracer

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
logger = logging.getLogger(__name__)


def set_state_machine(sm):
    global _state_machine
    _state_machine = sm


def set_evolution_engine(engine):
    global _evolution_engine
    _evolution_engine = engine


@mcp.tool()
async def get_current_state(current_time: str, last_interaction_time: str | None = None) -> str:
    """对话开始时调用。根据时间间隔生成凯尔希的最新状态快照。

    推进与「尾部补一格」会参考数据库中最后一条快照时刻；对话相关的上次互动检查点
    固定取数据库最新 `conversation_end` 快照。仅「不足一整格最小时间单位、只在对话当下补一条」时：要求
    （对话时刻 − 最后快照）大于 2 小时才会生成，避免短时重复刷新。

    Args:
        current_time: 当前真实时间。**强烈建议**使用东八区显式偏移，例如 ``2026-03-28T10:00:00+08:00``
        （与界面展示一致）。若使用 ``Z`` 则表示 UTC 绝对时刻；**若省略时区**，则按东八区墙钟解析
        （勿把 ``Date.toISOString()`` 的 UTC 结果去掉 ``Z`` 后传入，否则会错位 8 小时）。
        last_interaction_time: 兼容旧调用保留，可不传；实际推进不依赖该值

    Returns:
        可直接注入会话上下文的文本块，顺序为：L1 -> L2 -> 当前状态快照
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
    return result


@mcp.tool()
async def summarize_conversation(conversation_text: str) -> str:
    """对话结束前可调用。将本次原始对话整理为可持久化的摘要。"""
    if _state_machine is None:
        return "错误：状态机未初始化"
    return await _state_machine.summarize_conversation(conversation_text)


@mcp.tool()
async def reflect_on_conversation(conversation_summary: str) -> str:
    """对话结束时调用。基于对话内容生成新的对话结束状态快照。

    Args:
        conversation_summary: 本次对话的摘要内容

    Returns:
        凯尔希的第一人称记忆独白，反映对话对她的影响。
        注意：本工具不再自动生成事件；若对话中出现值得保留的事件，请显式调用 upsert_event。
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
    """对话过程中调用。搜索凯尔希的过往记忆（事件锚点和历史快照）。

    Args:
        query: 搜索关键词或描述
        top_k: 返回的最大结果数量

    Returns:
        相关记忆条目列表（JSON格式）
    """
    if _state_machine is None:
        return "错误：状态机未初始化"
    results = await _state_machine.recall_memories(query, top_k=top_k)
    if not results:
        return "未找到相关记忆。"
    return json.dumps(results, ensure_ascii=False, indent=2)


@mcp.tool()
async def upsert_key_record(
    record_type: str,
    title: str,
    content_text: str,
    tags: list[str] | None = None,
    content_json: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    status: str = "active",
    linked_event_id: int | None = None,
    update_if_exists: bool = True,
) -> str:
    """对话过程中调用。仅写入「关键记录」表（不修改世界书/事件锚点）。

    Args:
        record_type: 记录类型。可选 important_date / important_item / key_collaboration / medical_advice
        title: 记录标题（建议简短明确）
        content_text: 记录正文（可包含表格文本）
        tags: 标签列表（可选）
        content_json: 结构化 JSON 字符串（可选）
        start_date: 生效开始日期 YYYY-MM-DD（可选）
        end_date: 生效结束日期 YYYY-MM-DD（可选）
        status: active 或 archived
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
        record_type=record_type,
        title=title,
        content_text=content_text,
        tags=tags or [],
        content_json=parsed_json,
        start_date=start_date,
        end_date=end_date,
        status=status,
        source="conversation",
        linked_event_id=linked_event_id,
        update_if_exists=update_if_exists,
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def upsert_event(
    objective: str,
    impression: str,
    title: str = "",
    date: str | None = None,
    keywords: list[str] | None = None,
    categories: list[str] | None = None,
    update_if_exists: bool = True,
) -> str:
    """对话过程中调用。将事件直接写入「事件历史」表，而非关键记录。

    Args:
        objective: 客观记录。应写清发生了什么、涉及谁/何物/场景/关键转折
        impression: 主观印象。写凯尔希对此事的浓缩感受与评价
        title: 事件标题（可留空，系统会自动生成）
        date: 事件日期 YYYY-MM-DD（可选，默认当天/东八区）
        keywords: 关键词列表（可选）
        categories: 分类列表（可选；留空时自动分类）
        update_if_exists: 同日期同标题已存在时是否更新

    Returns:
        写入结果（JSON）

    调用建议：
        - 适合保留对话中的具体事件、转折、决定、情感节点，但不适合承载表格化医嘱/计划等结构化复用信息。
        - 事件正文会固定写成「客观记录 + 主观印象」两段，便于和后台快照生成事件保持一致。
        - 若事件标题暂不确定，可留空让系统基于客观记录自动生成。
    """
    if _state_machine is None:
        return "错误：状态机未初始化"
    result = await _state_machine.upsert_event(
        title=title,
        objective=objective,
        impression=impression,
        date=date,
        keywords=keywords or [],
        categories=categories or [],
        source="conversation",
        update_if_exists=update_if_exists,
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def recall_key_records(
    query: str,
    top_k: int = 5,
    record_type: str | None = None,
    include_archived: bool = False,
    include_world_books: bool = True,
) -> str:
    """对话过程中调用。检索结构化关键记录；可选合并世界书（关键词匹配 + 已向量化时的向量相似度）。

    Args:
        query: 搜索词或描述。建议至少提供2-4个关键词并用空格分隔（例如：用药方案 源石 镇痛 华法林）
        top_k: 返回条数（关键记录与世界书条目统一排序后截断）
        record_type: 可选的类型过滤（仅作用于关键记录表）
        include_archived: 是否包含归档记录
        include_world_books: 是否并入启用中的世界书检索结果

    Returns:
        JSON 列表，顺序固定：先关键记录（按 updated_at 新近优先），后世界书（固定至多 3 条，且不超过 top_k）。
        字段说明：`_memory_tier` 为 `primary`（关键记录）或 `supplementary`（世界书）；`_usage_hint`、`_content_for_prompt`
        供拼入模型上下文时区分「对话沉淀事实」与「静态设定参考」，避免把世界书当成用户刚说的话。

    调用建议：
        - 当用户提到病症、药名、纪念日、信物、共同计划等“具体可执行信息”时优先调用本工具。
        - query 优先使用“多关键词组合”而非单词查询，建议覆盖标题词、实体名词、动作词与正文关键短语。
        - 需要设定/背景条目的语义或关键词命中时保持 include_world_books=True。
        - 若命中后仍需时间线叙事，再调用 recall_memories（事件/快照向量库）。
        - 写入持久化事实请仅用 upsert_key_record，不要试图写入世界书。
        - 默认先查 active 记录；需要历史方案时再 include_archived=True。
    """
    if _state_machine is None:
        return "错误：状态机未初始化"
    items = await _state_machine.recall_key_records(
        query=query,
        top_k=top_k,
        record_type=record_type,
        include_archived=include_archived,
        include_world_books=include_world_books,
    )
    if not items:
        return "未找到相关关键记录或世界书条目。"
    return json.dumps(items, ensure_ascii=False, indent=2)


@mcp.tool()
async def execute_profile_evolution() -> str:
    """直接执行人格演化：评分事件、更新L2层、归档低分事件。"""
    if _evolution_engine is None:
        return "错误：演化引擎未初始化"
    preview = await _evolution_engine.preview()
    result = await _evolution_engine.apply(preview)
    return json.dumps(
        {
            "change_summary": preview.get("change_summary", ""),
            "archived_events": result.get("archived_count", 0),
            "updated_layers": result.get("updated_keys", []),
            "applied_at": result.get("applied_at", ""),
        },
        ensure_ascii=False,
        indent=2,
    )
