from __future__ import annotations

import re
from collections import Counter

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "情感交流": ["情绪", "安慰", "拥抱", "亲密", "心情", "关系", "告白", "思念", "温柔"],
    "学术探讨": ["研究", "学术", "论文", "理论", "实验", "数据", "模型", "推演", "分析"],
    "生活足迹": ["日常", "散步", "饮食", "休息", "生活", "天气", "地点", "行程", "起居"],
    "床榻私语": ["床", "夜谈", "私语", "入眠", "清晨", "耳语", "贴近", "枕边", "夜晚"],
    "精神碰撞": ["争论", "冲突", "价值", "信念", "质疑", "观点", "对峙", "分歧", "辩论"],
    "工作同步": ["任务", "汇报", "会议", "进度", "计划", "排班", "协作", "执行", "项目"],
}

STOP_TOKENS = {
    "凯尔希", "博士", "我们", "他们", "这次", "这个", "那个", "以及",
    "进行", "完成", "相关", "当前", "情况", "事件", "记录", "状态",
    "讨论", "交流", "沟通", "处理", "推进", "关于", "并且", "然后",
}

CATEGORY_TITLE_HINTS = {
    "情感交流": "情绪对话",
    "学术探讨": "理论研讨",
    "生活足迹": "日常片段",
    "床榻私语": "夜间私语",
    "精神碰撞": "观点交锋",
    "工作同步": "任务同步",
}

ACTION_HINTS = [
    ("争论", "观点交锋"),
    ("分歧", "分歧对话"),
    ("共识", "达成共识"),
    ("协作", "协作推进"),
    ("计划", "计划同步"),
    ("会议", "会议纪要"),
    ("任务", "任务同步"),
    ("研究", "研究讨论"),
    ("实验", "实验复盘"),
    ("安慰", "情绪安抚"),
    ("陪伴", "陪伴时刻"),
    ("夜谈", "夜间私语"),
]


def _extract_keywords(description: str, trigger_keywords: list[str] | None = None) -> list[str]:
    raw = []
    if trigger_keywords:
        raw.extend([k.strip() for k in trigger_keywords if k and k.strip()])
    # 2-6 连续中文词块 + 英文词块
    raw.extend(re.findall(r"[\u4e00-\u9fff]{2,6}|[A-Za-z][A-Za-z0-9_-]{2,20}", description or ""))
    cleaned: list[str] = []
    for token in raw:
        t = token.strip().strip("，。！？、:：;；,.!?[]()（）\"'")
        if not t or t in STOP_TOKENS or len(t) <= 1:
            continue
        if re.fullmatch(r"\d+", t):
            continue
        cleaned.append(t)
    counts = Counter(cleaned)
    return [t for t, _ in counts.most_common(6)]


def _smart_truncate(text: str, max_len: int) -> str:
    t = (text or "").strip()
    if len(t) <= max_len:
        return t
    # 尽量保留“X与Y”结构
    if "与" in t[: max_len + 1]:
        idx = t.rfind("与", 0, max_len + 1)
        if idx > 1:
            return t[:max_len]
    return t[:max_len]


def make_event_title(
    description: str,
    trigger_keywords: list[str] | None = None,
    categories: list[str] | None = None,
    max_len: int = 16,
) -> str:
    text = re.sub(r"\s+", " ", (description or "").strip())
    if not text:
        return "未命名事件"

    cats = [c for c in (categories or []) if c]
    keywords = _extract_keywords(text, trigger_keywords)

    for needle, title in ACTION_HINTS:
        if needle in text:
            return _smart_truncate(title, max_len)

    if len(keywords) >= 2:
        composed = f"{keywords[0]}与{keywords[1]}"
        if 4 <= len(composed) <= max_len:
            return composed
        return _smart_truncate(composed, max_len)

    if len(keywords) == 1:
        head = CATEGORY_TITLE_HINTS.get(cats[0], "事件记录") if cats else "事件记录"
        return _smart_truncate(f"{head}：{keywords[0]}", max_len)

    if cats:
        return _smart_truncate(CATEGORY_TITLE_HINTS.get(cats[0], cats[0]), max_len)

    first = re.split(r"[。！？!?；;\n]", text)[0].strip() or text
    return _smart_truncate(first, max_len)


def classify_event(description: str, keywords: list[str]) -> list[str]:
    haystack = f"{description} {' '.join(keywords or [])}".lower()
    categories: list[str] = []
    for category, words in CATEGORY_KEYWORDS.items():
        if any(w.lower() in haystack for w in words):
            categories.append(category)
    if not categories:
        categories.append("生活足迹")
    return categories
