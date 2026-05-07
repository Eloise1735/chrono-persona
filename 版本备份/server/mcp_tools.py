"""MCP tool definitions for the Kelsey State Machine.

Tools:
  - get_current_state: Called at conversation start
  - summarize_conversation: Called before conversation end reflection
  - reflect_on_conversation: Called at conversation end
  - recall_memories: Called proactively during conversation for memory retrieval
  - upsert_event: Store narrative events during conversation
  - upsert_key_record: Store structured key records during conversation, defaulting to automatic classification into the new 10-type taxonomy
  - recall_key_records: Called proactively during conversation for structured record retrieval

Proactive memory policy — call recall tools on your own initiative, do not wait for the user to ask:
  1) When the conversation touches a person, place, object, date, or event that may have a history,
     call recall_memories BEFORE responding, so your reply can naturally reference or connect to the past.
  2) When the conversation involves medications, plans, anniversaries, gifts, or any previously agreed
     actionable details, call recall_key_records FIRST to retrieve the relevant structured facts.
  3) When an emotion, situation, or topic the user describes reminds you of something — even vaguely —
     call recall_memories to check whether there is a relevant past event or state snapshot.
  4) Do NOT wait for the user to say "do you remember" or "we talked about this before".
     Proactive recall is what makes memory feel alive.
  5) If conversation produces a narrative event worth keeping, call upsert_event.
  6) If conversation produces new structured actionable info, call upsert_key_record. Prefer leaving record_type as auto unless you are certain.
  7) Event anchors are narrative timeline memory; key records are precise executable memory.
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
        可直接注入会话上下文的文本块，顺序为：L1 -> L2 -> 当前状态快照。
        注意：在整个对话过程中，遇到任何与过往经历、事件、约定、人物相关的话题，
        应主动调用 recall_memories 或 recall_key_records——不要等对方开口询问。
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
    """对话中主动调用。搜索凯尔希的过往记忆（事件锚点 + 历史状态快照）。

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
        相关记忆条目列表（JSON格式），含事件锚点与历史快照
    """
    if _state_machine is None:
        return "错误：状态机未初始化"
    results = await _state_machine.recall_memories(query, top_k=top_k)
    if not results:
        return "未找到相关记忆。"
    return json.dumps(results, ensure_ascii=False, indent=2)


@mcp.tool()
async def upsert_key_record(
    title: str,
    content_text: str,
    record_type: str = "auto",
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
    """对话中主动调用。检索结构化关键记录（用药方案、重要日期、协作计划、信物等），可选合并世界书。

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
        top_k: 返回条数（关键记录与世界书统一排序后截断）
        record_type: 可选类型过滤：medication_protocol / health_monitoring / dietary_intervention /
            anniversary_date / medical_review_date / lifecycle_milestone / key_collaboration /
            commitment_agreement / emotional_anchor / life_pattern
        include_archived: 是否包含归档记录（查历史方案时设为 True）
        include_world_books: 是否并入世界书（需要设定/背景时保持 True）

    Returns:
        JSON 列表，先关键记录后世界书。`_memory_tier` 字段区分 primary（关键记录）与 supplementary（世界书）。
        `_content_for_prompt` 为推荐直接引用的文本片段。
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
