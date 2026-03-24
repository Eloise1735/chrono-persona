"""MCP tool definitions for the Kelsey State Machine.

Tools:
  - get_current_state: Called at conversation start
  - summarize_conversation: Called before conversation end reflection
  - reflect_on_conversation: Called at conversation end
  - recall_memories: Called during conversation for memory retrieval
  - upsert_key_record: Store structured key records during conversation
  - recall_key_records: Retrieve structured key records during conversation

Recommended tool policy:
  1) If user asks about medication advice, collaborative plans, anniversaries, gifts,
     or previously agreed actionable details, call recall_key_records first.
  2) If conversation produces new actionable structured info (tables/checklists/instructions),
     call upsert_key_record to persist it.
  3) Event anchors are narrative timeline memory; key records are precise executable memory.
"""

from __future__ import annotations

import json
from mcp.server.fastmcp import FastMCP

# FastMCP defaults streamable HTTP to path "/mcp". With mount "/mcp-http", the full URL is
# /mcp-http/mcp (many clients append "/mcp" to the configured base URL).
mcp = FastMCP("Kelsey-State-Machine")
# Allow reverse-proxy/tunnel Host headers (e.g. trycloudflare.com) to access SSE.
# Local-only deployments can keep strict defaults, but mobile + tunnel requires this.
mcp.settings.transport_security.enable_dns_rebinding_protection = False

# Will be set during app startup
_state_machine = None
_evolution_engine = None


def set_state_machine(sm):
    global _state_machine
    _state_machine = sm


def set_evolution_engine(engine):
    global _evolution_engine
    _evolution_engine = engine


@mcp.tool()
async def get_current_state(current_time: str, last_interaction_time: str) -> str:
    """对话开始时调用。根据时间间隔生成凯尔希的最新状态快照。

    Args:
        current_time: 当前真实时间 (ISO格式, 如 2026-03-22T15:00:00)
        last_interaction_time: 上次对话结束时间 (ISO格式)

    Returns:
        可直接注入会话上下文的文本块，顺序为：L1 -> L2 -> 当前状态快照
    """
    if _state_machine is None:
        return "错误：状态机未初始化"
    result = await _state_machine.get_current_state(current_time, last_interaction_time)
    if _evolution_engine is None:
        return result
    status = await _evolution_engine.check_status()
    if status.get("should_evolve"):
        return (
            f"{result}\n\n"
            f"[系统提示：已累积 {status.get('event_count')} 条新事件，达到阈值 {status.get('threshold')}。"
            "如你同意，可直接调用 execute_profile_evolution 完成人格演化。]"
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
    """对话结束时调用。基于对话内容生成新的状态快照和事件锚点。

    Args:
        conversation_summary: 本次对话的摘要内容

    Returns:
        凯尔希的第一人称记忆独白，反映对话对她的影响
    """
    if _state_machine is None:
        return "错误：状态机未初始化"
    return await _state_machine.reflect_on_conversation(conversation_summary)


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
    """对话过程中调用。写入或更新关键记录（关键日期/关键物品/关键协作/医疗建议）。

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
async def recall_key_records(
    query: str,
    top_k: int = 5,
    record_type: str | None = None,
    include_archived: bool = False,
) -> str:
    """对话过程中调用。检索结构化关键记录（例如医疗建议、关键计划、纪念日等）。

    Args:
        query: 搜索词或描述。建议至少提供2-4个关键词并用空格分隔（例如：用药方案 源石 镇痛 华法林）
        top_k: 返回条数
        record_type: 可选的类型过滤
        include_archived: 是否包含归档记录

    Returns:
        关键记录列表（JSON）

    调用建议：
        - 当用户提到病症、药名、纪念日、信物、共同计划等“具体可执行信息”时优先调用本工具。
        - query 优先使用“多关键词组合”而非单词查询，建议覆盖标题词、实体名词、动作词与正文关键短语。
        - 若命中后仍需补充背景叙事，再调用 recall_memories 获取事件锚点上下文。
        - 默认先查 active 记录；需要历史方案时再 include_archived=True。
    """
    if _state_machine is None:
        return "错误：状态机未初始化"
    items = await _state_machine.recall_key_records(
        query=query,
        top_k=top_k,
        record_type=record_type,
        include_archived=include_archived,
    )
    if not items:
        return "未找到相关关键记录。"
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
