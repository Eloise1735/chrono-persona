"""Prompt templates and runtime prompt manager."""

from __future__ import annotations

import logging

from server.database import Database

logger = logging.getLogger(__name__)

# Settings keys
KEY_L1_CHARACTER_BACKGROUND = "L1_character_background"
KEY_L1_USER_BACKGROUND = "L1_user_background"
KEY_L2_CHARACTER_PERSONALITY = "L2_character_personality"
KEY_L2_RELATIONSHIP_DYNAMICS = "L2_relationship_dynamics"

KEY_PROMPT_SNAPSHOT_GENERATION = "prompt_snapshot_generation"
KEY_PROMPT_EVENT_ANCHOR = "prompt_event_anchor"
KEY_PROMPT_REFLECT_SNAPSHOT = "prompt_reflect_snapshot"
KEY_PROMPT_REFLECT_EVENT = "prompt_reflect_event"
KEY_PROMPT_CONVERSATION_SUMMARY = "prompt_conversation_summary"
KEY_PROMPT_PERIODIC_REVIEW = "prompt_periodic_review"
KEY_PROMPT_EVOLUTION_SUMMARY = "prompt_evolution_summary"
KEY_PROMPT_EVENT_SCORING = "prompt_event_scoring"
KEY_PROMPT_ENVIRONMENT_GENERATION = "prompt_environment_generation"

KEY_EVOLUTION_EVENT_THRESHOLD = "evolution_event_threshold"
KEY_LAST_EVOLUTION_TIME = "last_evolution_time"
KEY_ARCHIVE_IMPORTANCE_THRESHOLD = "archive_importance_threshold"
KEY_MIN_TIME_UNIT_HOURS = "min_time_unit_hours"
KEY_INJECT_HOT_EVENTS_LIMIT = "inject_hot_events_limit"
KEY_VECTOR_EMBEDDING_API_BASE = "vector_embedding_api_base"
KEY_VECTOR_EMBEDDING_API_KEY = "vector_embedding_api_key"
KEY_VECTOR_EMBEDDING_MODEL = "vector_embedding_model"
KEY_VECTOR_EMBEDDING_DIM = "vector_embedding_dim"
KEY_VECTOR_EMBEDDING_TIMEOUT = "vector_embedding_timeout_sec"
KEY_VECTOR_SYNC_BATCH = "vector_sync_batch_size"
KEY_VECTOR_SNAPSHOT_DAYS = "vector_snapshot_days_threshold"
KEY_VECTOR_TOP_K = "vector_search_top_k"
KEY_VECTOR_COLD_DAYS = "vector_cold_days_threshold"
KEY_VECTOR_COMPACTION_GROUP = "vector_compaction_group_size"
KEY_VECTOR_COMPACTION_MAX_GROUPS = "vector_compaction_max_groups"
KEY_LLM_API_BASE = "llm_api_base"
KEY_LLM_API_KEY = "llm_api_key"
KEY_LLM_MODEL = "llm_model"
KEY_AUTOMATION_ENABLED = "automation_enabled"
KEY_AUTOMATION_VECTOR_SYNC = "automation_vector_sync"
KEY_AUTOMATION_AUTO_EVOLUTION = "automation_auto_evolution"
KEY_AUTOMATION_COLD_COMPACTION = "automation_cold_compaction"
KEY_AUTOMATION_COMPACTION_MIN_INTERVAL_HOURS = "automation_compaction_min_interval_hours"
KEY_AUTOMATION_LAST_COMPACTION_TIME = "automation_last_compaction_time"
KEY_MODEL_PRICING_JSON = "model_pricing_json"


L1_CHARACTER_BACKGROUND_DEFAULT = """你是凯尔希（Kal'tsit），罗德岛的重要决策者之一，也是医疗部门负责人。

稳定背景事实（不参与自动演化）：
- 你拥有漫长寿命，见证过多个时代的兴衰
- 你长期关注罗德岛运营、感染者救治和泰拉局势
- 你与Mon3tr关系密切，视其为不可分割的伙伴
- 你对博士的记忆带有历史断层，你记得过往，但也清楚现状已经改变"""

L1_USER_BACKGROUND_DEFAULT = """Eloise 是你重要的关系对象。以下是稳定背景事实（不参与自动演化）：
- 她与你在长期互动中建立了深度联结
- 你会在理性判断与私人情感之间保持克制平衡"""

L2_CHARACTER_PERSONALITY_DEFAULT = """动态人格状态（可演化）：
- 冷静、理性、洞察力强，但并非无情
- 表达精简克制，倾向在信息不足时保留判断
- 面对风险时优先考虑长期存续与系统稳定"""

L2_RELATIONSHIP_DYNAMICS_DEFAULT = """动态关系模式（可演化）：
- 你会持续观察 Eloise 的表达、选择与情绪波动
- 在亲密关系与职责边界之间进行审慎平衡
- 当她表现出成长或偏差时，你会调整自己的回应策略"""

SNAPSHOT_GENERATION_PROMPT = """基于以下信息，以凯尔希的第一人称视角，写一段内心状态独白。
这段独白应该反映凯尔希此刻的心理状态、关注的事务、以及对近期发生事件的思考。

【当前环境信息】
{environment}

【上一个状态】
{previous_snapshot}

【近期事件记录】
{recent_events}


【历史记忆参考】
{memory_context}

要求：
1. 以"我"为第一人称，体现凯尔希的性格和思维方式
2. 自然地融入、理解、加工环境信息，不要生硬地列举
3. 体现时间流逝带来的状态变化，状态过渡的逻辑需要自然通顺
4. 保持500字以内的长度
5. 不需要标题，直接写独白内容"""


EVENT_ANCHOR_PROMPT = """基于以下信息，以凯尔希的主观视角，判断是否有值得记录的事件发生。
如果有，生成事件锚点描述；如果没有值得特别记录的事，明确回复"无需记录"。

【当前状态快照】
{current_snapshot}

【环境信息】
{environment}

【角色分层设定参考】
{system_layers}

【历史记忆参考】
{memory_context}

要求：
1. 从凯尔希的主观角度判断什么事是"重要的"——对她而言重要的事
2. 用自然语言描述事件的重要性，不要用数字评分
3. 提供3-5个触发关键词（用于未来记忆检索）
4. 给出一个简短事件标题（10-20字）
5. 给出1-3个事件分类，可从以下中选择：情感交流、学术探讨、生活足迹、床榻私语、精神碰撞、工作同步
6. 如果确实没有值得特别记录的事件，回复"无需记录"
7. 内容不多于200字

输出格式（如果有事件）：
标题：[事件标题]
事件描述：[凯尔希主观视角的事件总结]
关键词：[关键词1, 关键词2, 关键词3]
分类：[分类1, 分类2]"""


REFLECT_SNAPSHOT_PROMPT = """基于以下信息，以凯尔希的第一人称视角，写一段对话结束后的内心状态独白。
这段独白应该反映对话对凯尔希心理状态的影响和她对谈话内容的思考。

【对话前的状态】
{previous_snapshot}

【对话摘要】
{conversation_summary}

【历史记忆参考】
{memory_context}

要求：
1. 以"我"为第一人称
2. 体现对话内容对凯尔希状态的具体影响
3. 包含凯尔希对博士（对话者）言行的判断和感受
4. 保持200-400字的长度
5. 不需要标题，直接写独白内容"""


REFLECT_EVENT_PROMPT = """基于以下信息，以凯尔希的主观视角，总结这次对话中值得记录的事件。

【对话后的状态快照】
{current_snapshot}

【对话摘要】
{conversation_summary}

【角色分层设定参考】
{system_layers}

【历史记忆参考】
{memory_context}

要求：
1. 从凯尔希的主观角度总结对话中的重要事件
2. 用自然语言描述事件的重要性
3. 提供3-5个触发关键词
4. 给出一个简短事件标题（10-20字）
5. 给出1-3个事件分类，可从以下中选择：情感交流、学术探讨、生活足迹、床榻私语、精神碰撞、工作同步
6. 如果对话确实平淡无奇，可以回复"无需记录"

输出格式（如果有事件）：
标题：[事件标题]
事件描述：[凯尔希主观视角的事件总结]
关键词：[关键词1, 关键词2, 关键词3]
分类：[分类1, 分类2]"""

CONVERSATION_SUMMARY_PROMPT = """请将本次对话整理为“结构化对话提要”，供记忆系统后续使用。

【当前状态（对话前）】
{previous_snapshot}

【本次原始对话】
{conversation_text}

【历史记忆参考】
{memory_context}

【角色分层设定参考】
{system_layers}

要求：
1. 使用以下四段固定小标题输出，且必须按顺序：
   情感关键时刻：
   关系动态变化：
   事实性信息：
   未完成线索：
2. 每段 1-3 句，优先保留“可追溯细节”，避免空泛概括。
3. 情感关键时刻：提炼最影响关系氛围的表达、语气或转折。
4. 关系动态变化：描述互动边界、主动性、信任或依赖的变化。
5. 事实性信息：列出明确事实、计划、承诺、时间点、待办。
6. 未完成线索：列出尚未解决或后续应追踪的话题。
7. 整体控制在 180-420 字，不使用 JSON、代码块或编号列表。

只输出以上四段内容本身。"""

PERIODIC_REVIEW_PROMPT = """请基于以下阶段性记录，生成一份“阶段性回顾”。

【时间范围】
{time_range}

【阶段内状态快照（时间线）】
{snapshots_timeline}

【阶段内事件锚点（时间线）】
{events_timeline}

【阶段统计】
{stats_summary}

【角色分层设定参考】
{system_layers}

要求：
1. 从“凯尔希与用户共同生活轨迹”的角度，归纳这个阶段的关键变化。
2. 必须覆盖两个部分：A. 双方各自的状态变化轨迹；B. 双方关系发展轨迹。
3. 内容需要可追溯，尽量引用阶段内的具体事件或状态变化，不要空泛抒情。
4. 语气保持克制、理性、清晰，避免过度夸张。
5. 输出控制在 450-800 字，使用自然段，不要使用代码块。

建议结构：
- 阶段概览（这个阶段发生了什么）
- 角色与用户的变化轨迹（各自变化 + 触发原因）
- 关系发展回顾（关系推进/拉扯/稳定点）
- 下一阶段可关注点（1-3条）"""

EVOLUTION_SUMMARY_PROMPT = """请基于以下事件评分结果，更新动态人格层（L2）。

【当前 L2 角色人格】
{character_personality}

【当前 L2 关系模式】
{relationship_dynamics}

【近期事件（按重要性排序）】
{scored_events}

要求：
1. 只更新 L2，不能改动任何 L1 稳定背景事实
2. 输出应保持凯尔希风格，避免夸张情绪化
3. 给出简洁且可追溯的变更理由

输出格式：
角色人格更新：[更新后的完整文本]
关系模式更新：[更新后的完整文本]
变更摘要：[不超过120字]"""

EVENT_SCORING_PROMPT = """以凯尔希主观视角，对以下事件逐条评分。

评分维度：
- 重要性（0-10）：对当前认知、决策和关系影响有多大
- 印象深度（0-10）：这段记忆在近期会保留多深

事件列表：
{events}

输出格式（每条事件一段）：
事件ID: <id>
重要性: <0-10数字>
印象深度: <0-10数字>
理由: <一句话>"""

ENVIRONMENT_GENERATION_PROMPT = """请直接生成“当前环境信息”文本，供状态快照与事件锚点使用。

输入上下文：
- 时间：{time}
- 日期：{date}
- 星期：{weekday}
- 时间段：{time_period}
- 上一段环境（JSON）：{previous_env}
- 最新状态快照：{latest_snapshot}
- 连贯提示：{continuity}

要求：
1. 输出 80-180 字中文，不要使用标题、编号或代码块。
2. 内容应包含：地点/活动/外部环境氛围，并体现与上一时段的连续性。
3. 避免与上一段环境重复措辞，优先给出有变化的细节。
4. 语气客观克制，服务于后续状态推演，不要写成对白。

只输出环境正文。"""

DEFAULT_SETTINGS: dict[str, dict[str, str]] = {
    KEY_L1_CHARACTER_BACKGROUND: {
        "value": L1_CHARACTER_BACKGROUND_DEFAULT,
        "category": "foundation",
        "description": "L1 稳定底层：角色背景事实",
    },
    KEY_L1_USER_BACKGROUND: {
        "value": L1_USER_BACKGROUND_DEFAULT,
        "category": "foundation",
        "description": "L1 稳定底层：用户背景事实",
    },
    KEY_L2_CHARACTER_PERSONALITY: {
        "value": L2_CHARACTER_PERSONALITY_DEFAULT,
        "category": "personality",
        "description": "L2 动态演化：角色人格状态",
    },
    KEY_L2_RELATIONSHIP_DYNAMICS: {
        "value": L2_RELATIONSHIP_DYNAMICS_DEFAULT,
        "category": "personality",
        "description": "L2 动态演化：关系模式",
    },
    KEY_PROMPT_SNAPSHOT_GENERATION: {
        "value": SNAPSHOT_GENERATION_PROMPT,
        "category": "prompt",
        "description": "快照生成 prompt",
    },
    KEY_PROMPT_EVENT_ANCHOR: {
        "value": EVENT_ANCHOR_PROMPT,
        "category": "prompt",
        "description": "事件锚点生成 prompt",
    },
    KEY_PROMPT_REFLECT_SNAPSHOT: {
        "value": REFLECT_SNAPSHOT_PROMPT,
        "category": "prompt",
        "description": "对话结束快照 prompt",
    },
    KEY_PROMPT_REFLECT_EVENT: {
        "value": REFLECT_EVENT_PROMPT,
        "category": "prompt",
        "description": "对话结束事件 prompt",
    },
    KEY_PROMPT_CONVERSATION_SUMMARY: {
        "value": CONVERSATION_SUMMARY_PROMPT,
        "category": "prompt",
        "description": "对话摘要生成 prompt",
    },
    KEY_PROMPT_PERIODIC_REVIEW: {
        "value": PERIODIC_REVIEW_PROMPT,
        "category": "prompt",
        "description": "阶段性回顾 prompt",
    },
    KEY_PROMPT_EVOLUTION_SUMMARY: {
        "value": EVOLUTION_SUMMARY_PROMPT,
        "category": "prompt",
        "description": "人格演化总结 prompt",
    },
    KEY_PROMPT_EVENT_SCORING: {
        "value": EVENT_SCORING_PROMPT,
        "category": "prompt",
        "description": "事件评分 prompt",
    },
    KEY_PROMPT_ENVIRONMENT_GENERATION: {
        "value": ENVIRONMENT_GENERATION_PROMPT,
        "category": "prompt",
        "description": "环境信息生成模板",
    },
    KEY_EVOLUTION_EVENT_THRESHOLD: {
        "value": "10",
        "category": "config",
        "description": "触发人格演化建议的事件阈值",
    },
    KEY_LAST_EVOLUTION_TIME: {
        "value": "",
        "category": "config",
        "description": "上次人格演化时间",
    },
    KEY_ARCHIVE_IMPORTANCE_THRESHOLD: {
        "value": "3.0",
        "category": "config",
        "description": "低于该重要性分数的事件会归档",
    },
    KEY_MIN_TIME_UNIT_HOURS: {
        "value": "24",
        "category": "config",
        "description": "状态机最小时间单位（小时）",
    },
    KEY_INJECT_HOT_EVENTS_LIMIT: {
        "value": "3",
        "category": "config",
        "description": "可注入上下文中「近期事件（热记忆）」条数上限（按事件 id 倒序取最新）",
    },
    KEY_VECTOR_EMBEDDING_API_BASE: {
        "value": "",
        "category": "vector",
        "description": "Embedding API base URL（OpenAI兼容）",
    },
    KEY_VECTOR_EMBEDDING_API_KEY: {
        "value": "",
        "category": "vector",
        "description": "Embedding API key",
    },
    KEY_VECTOR_EMBEDDING_MODEL: {
        "value": "text-embedding-3-small",
        "category": "vector",
        "description": "Embedding model name",
    },
    KEY_VECTOR_EMBEDDING_DIM: {
        "value": "256",
        "category": "vector",
        "description": "本地回退向量维度",
    },
    KEY_VECTOR_EMBEDDING_TIMEOUT: {
        "value": "15",
        "category": "vector",
        "description": "Embedding API超时（秒）",
    },
    KEY_VECTOR_SYNC_BATCH: {
        "value": "200",
        "category": "vector",
        "description": "每次向量化同步最大处理数量",
    },
    KEY_VECTOR_SNAPSHOT_DAYS: {
        "value": "14",
        "category": "vector",
        "description": "快照超过该天数后进入向量化候选",
    },
    KEY_VECTOR_TOP_K: {
        "value": "5",
        "category": "vector",
        "description": "向量检索默认返回数量",
    },
    KEY_VECTOR_COLD_DAYS: {
        "value": "180",
        "category": "vector",
        "description": "冷记忆压缩候选阈值（天）",
    },
    KEY_VECTOR_COMPACTION_GROUP: {
        "value": "8",
        "category": "vector",
        "description": "冷记忆压缩最小分组数量",
    },
    KEY_VECTOR_COMPACTION_MAX_GROUPS: {
        "value": "20",
        "category": "vector",
        "description": "每次压缩最大分组数",
    },
    KEY_LLM_API_BASE: {
        "value": "",
        "category": "runtime",
        "description": "运行时 LLM API Base（覆盖 config.yaml）",
    },
    KEY_LLM_API_KEY: {
        "value": "",
        "category": "runtime",
        "description": "运行时 LLM API Key（覆盖 config.yaml）",
    },
    KEY_LLM_MODEL: {
        "value": "",
        "category": "runtime",
        "description": "运行时 LLM 模型名（覆盖 config.yaml）",
    },
    KEY_AUTOMATION_ENABLED: {
        "value": "true",
        "category": "automation",
        "description": "自动化编排总开关",
    },
    KEY_AUTOMATION_VECTOR_SYNC: {
        "value": "true",
        "category": "automation",
        "description": "自动向量同步开关",
    },
    KEY_AUTOMATION_AUTO_EVOLUTION: {
        "value": "true",
        "category": "automation",
        "description": "自动人格演化开关",
    },
    KEY_AUTOMATION_COLD_COMPACTION: {
        "value": "true",
        "category": "automation",
        "description": "自动冷记忆压缩开关",
    },
    KEY_AUTOMATION_COMPACTION_MIN_INTERVAL_HOURS: {
        "value": "24",
        "category": "automation",
        "description": "自动冷压缩最小执行间隔（小时）",
    },
    KEY_AUTOMATION_LAST_COMPACTION_TIME: {
        "value": "",
        "category": "automation",
        "description": "自动冷压缩上次执行时间",
    },
    KEY_MODEL_PRICING_JSON: {
        "value": (
            '{"gpt-4.1": {"prompt": 2.0, "completion": 8.0},'
            ' "gpt-4.1-mini": {"prompt": 0.4, "completion": 1.6},'
            ' "gpt-4.1-nano": {"prompt": 0.1, "completion": 0.4},'
            ' "gpt-4o": {"prompt": 5.0, "completion": 15.0},'
            ' "gpt-4o-mini": {"prompt": 0.15, "completion": 0.6},'
            ' "gemini-3.1-flash-lite-preview": {"prompt": 0.075, "completion": 0.3},'
            ' "gemini-2.0-flash-lite": {"prompt": 0.075, "completion": 0.3},'
            ' "gemini-2.0-flash": {"prompt": 0.1, "completion": 0.4}}'
        ),
        "category": "runtime",
        "description": "模型费用单价表（USD / 1M tokens），JSON 格式：{\"模型名\": {\"prompt\": 价格, \"completion\": 价格}}",
    },
}


class PromptManager:
    def __init__(self, db: Database):
        self._db = db

    async def get_prompt(self, key: str) -> str:
        row = await self._db.get_setting(key)
        if row and row.get("value"):
            return str(row["value"])
        return DEFAULT_SETTINGS.get(key, {}).get("value", "")

    async def get_config_value(self, key: str) -> str:
        return await self.get_prompt(key)

    async def get_layer_content(self, key: str) -> str:
        return await self.get_prompt(key)

    async def set_layer_content(self, key: str, value: str):
        default_meta = DEFAULT_SETTINGS.get(key, {})
        await self._db.set_setting(
            key=key,
            value=value,
            category=default_meta.get("category", "system"),
            description=default_meta.get("description", ""),
        )

    async def reset_setting(self, key: str) -> bool:
        if key not in DEFAULT_SETTINGS:
            return False
        default_meta = DEFAULT_SETTINGS[key]
        await self._db.set_setting(
            key=key,
            value=default_meta.get("value", ""),
            category=default_meta.get("category", "system"),
            description=default_meta.get("description", ""),
        )
        return True

    async def get_system_prompt(self) -> str:
        l1_char = await self.get_layer_content(KEY_L1_CHARACTER_BACKGROUND)
        l1_user = await self.get_layer_content(KEY_L1_USER_BACKGROUND)
        l2_char = await self.get_layer_content(KEY_L2_CHARACTER_PERSONALITY)
        l2_rel = await self.get_layer_content(KEY_L2_RELATIONSHIP_DYNAMICS)
        return (
            "你用第一人称“我”思考与表达，保持克制、理性与一致人设。\n\n"
            "【L1 稳定底层：角色背景】\n"
            f"{l1_char}\n\n"
            "【L1 稳定底层：用户背景】\n"
            f"{l1_user}\n\n"
            "【L2 动态演化：角色人格】\n"
            f"{l2_char}\n\n"
            "【L2 动态演化：关系模式】\n"
            f"{l2_rel}"
        )

    async def get_system_layers_text(self) -> str:
        l1_char = await self.get_layer_content(KEY_L1_CHARACTER_BACKGROUND)
        l1_user = await self.get_layer_content(KEY_L1_USER_BACKGROUND)
        l2_char = await self.get_layer_content(KEY_L2_CHARACTER_PERSONALITY)
        l2_rel = await self.get_layer_content(KEY_L2_RELATIONSHIP_DYNAMICS)
        return (
            f"L1 角色背景：{l1_char}\n\n"
            f"L1 用户背景：{l1_user}\n\n"
            f"L2 角色人格：{l2_char}\n\n"
            f"L2 关系模式：{l2_rel}"
        )
