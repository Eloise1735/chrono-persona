from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from server.llm_client import LLMClient
from server.prompts import KEY_PROMPT_ENVIRONMENT_GENERATION, PromptManager
from server.time_display import DISPLAY_TZ


def environment_text_for_prompt(env: dict | None) -> str:
    if not env or not isinstance(env, dict):
        return ""
    timeline_lines: list[str] = []
    time_text = str(env.get("time") or "").strip()
    if time_text:
        timeline_lines.append(f"时间（UTC+8）：{time_text}")
    date_text = str(env.get("date") or "").strip()
    if date_text:
        timeline_lines.append(f"日期：{date_text}")
    weekday = str(env.get("weekday") or "").strip()
    if weekday:
        timeline_lines.append(f"星期：{weekday}")
    time_period = str(env.get("time_period") or "").strip()
    if time_period:
        timeline_lines.append(f"时段：{time_period}")
    timeline_text = (
        "【环境时序锚点（东八区）】\n" + "\n".join(f"- {line}" for line in timeline_lines)
        if timeline_lines
        else ""
    )
    body = str(env.get("activity") or "").strip()
    synopsis = str(env.get("summary") or "").strip()
    if body and synopsis and body == synopsis:
        base = body
    elif body and synopsis:
        base = f"{body}\n\n{synopsis}"
    else:
        base = body or synopsis
    if timeline_text and base:
        return f"{timeline_text}\n\n{base}"
    return timeline_text or base


def environment_text_for_retrieval(env: dict | None) -> str:
    if not env or not isinstance(env, dict):
        return ""
    retrieval = str(env.get("retrieval_summary") or "").strip()
    if retrieval:
        return retrieval
    summary = str(env.get("summary") or "").strip()
    if summary:
        return summary
    return str(env.get("activity") or "").strip()


class EnvironmentGenerator(ABC):
    @abstractmethod
    async def generate(
        self,
        time_point: datetime,
        previous_env: dict | None,
        context: dict,
        *,
        allow_retry_fallback: bool = True,
    ) -> dict:
        ...


class TemplateEnvironmentGenerator(EnvironmentGenerator):
    MAX_CHARACTER_STATE_CHARS = 10000
    MAX_WORLD_BOOK_ITEMS = 3
    MAX_WORLD_BOOK_ITEM_CHARS = 200

    PERIODS = {
        (6, 9): "清晨",
        (9, 12): "上午",
        (12, 14): "中午",
        (14, 18): "下午",
        (18, 21): "傍晚",
        (21, 24): "深夜",
        (0, 6): "凌晨",
    }

    WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

    ENV_LLM_SYSTEM_PROMPT = (
        "你正在执行凯尔希状态机中的环境信息生成任务。"
        "必须严格遵循用户消息中的输入、原则与输出格式。"
        "仅输出三个正文分段，不要输出闲聊、JSON 或 Markdown 代码块。"
        "三个分段按顺序为：[环境正文]、[内容小结]、[检索摘要]。"
        "其中 [检索摘要] 必须非常短，只保留供后续记忆检索使用的高信息密度摘要。"
    )

    def __init__(
        self,
        prompt_manager: PromptManager | None = None,
        llm: LLMClient | None = None,
    ):
        self.prompt_manager = prompt_manager
        self.llm = llm

    @staticmethod
    def _strip_env_section_header(text: str, markers: tuple[str, ...]) -> str:
        s = text.strip()
        for marker in markers:
            if s.startswith(marker):
                s = s[len(marker) :].lstrip()
                break
        return s.strip()

    @classmethod
    def parse_environment_llm_output(cls, raw: str) -> tuple[str, str, str]:
        text = (raw or "").strip()
        if not text:
            return "", "", ""

        parts = [
            p.strip()
            for p in re.split(r"\n\s*(?:-{3,}|\*{3,})\s*\n", text)
            if p.strip()
        ]
        if len(parts) >= 3:
            body = cls._strip_env_section_header(
                parts[0],
                ("[环境正文]", "环境正文", "[Environment Body]", "Environment Body"),
            )
            summary = cls._strip_env_section_header(
                parts[1],
                ("[内容小结]", "内容小结", "[Summary]", "Summary"),
            )
            retrieval = cls._strip_env_section_header(
                parts[2],
                ("[检索摘要]", "检索摘要", "[Retrieval Summary]", "Retrieval Summary"),
            )
            return body.strip(), summary.strip(), retrieval.strip()

        if len(parts) == 2:
            body = cls._strip_env_section_header(
                parts[0],
                ("[环境正文]", "环境正文", "[Environment Body]", "Environment Body"),
            )
            summary = cls._strip_env_section_header(
                parts[1],
                ("[内容小结]", "内容小结", "[Summary]", "Summary"),
            )
            return body.strip(), summary.strip(), ""

        body = cls._strip_env_section_header(
            text,
            ("[环境正文]", "环境正文", "[Environment Body]", "Environment Body"),
        )
        return body.strip(), "", ""

    @staticmethod
    def _narrative_local_time(time_point: datetime) -> datetime:
        if time_point.tzinfo is not None:
            utc_naive = time_point.astimezone(timezone.utc).replace(tzinfo=None)
        else:
            utc_naive = time_point
        return utc_naive.replace(tzinfo=timezone.utc).astimezone(DISPLAY_TZ)

    @classmethod
    def _clip_character_state(cls, text: str) -> str:
        t = (text or "").strip()
        if len(t) <= cls.MAX_CHARACTER_STATE_CHARS:
            return t
        return (
            t[: cls.MAX_CHARACTER_STATE_CHARS].rstrip()
            + "\n…（角色前一状态摘要过长，此处仅保留前段供连贯参考。）"
        )

    async def generate(
        self,
        time_point: datetime,
        previous_env: dict | None,
        context: dict,
        *,
        allow_retry_fallback: bool = True,
    ) -> dict:
        narr = self._narrative_local_time(time_point)
        period = self._get_period(narr.hour)
        weekday = self.WEEKDAY_NAMES[narr.weekday()]

        latest_snapshot = str(context.get("latest_snapshot", "") or "")
        character_state = self._clip_character_state(latest_snapshot)
        time_delta_hours = float(context.get("time_delta_hours", 0.0) or 0.0)
        recent_events = context.get("recent_events") or []
        world_book_entries = context.get("world_book_entries") or []

        recent_events_text = self._format_recent_events(recent_events)
        time_elapsed = self._format_time_elapsed(time_delta_hours)
        continuity = self._build_continuity(previous_env)
        match_keywords = self._extract_keywords(latest_snapshot, recent_events)
        matched_world_books = self._match_world_book(
            world_book_entries,
            period,
            match_keywords,
        )
        world_book_context = (
            "\n".join(f"- {item}" for item in matched_world_books)
            if matched_world_books
            else "（无匹配世界书）"
        )

        template = ""
        if self.prompt_manager is not None:
            template = await self.prompt_manager.get_prompt(KEY_PROMPT_ENVIRONMENT_GENERATION)
        if not template:
            template = (
                "时间：{time}\n日期：{date}\n星期：{weekday}\n时间段：{time_period}\n"
                "上一段环境：{previous_env}\n连续性提示：{continuity}\n"
                "间隔时长：{time_elapsed}\n角色前一状态摘要：{character_state}\n"
                "期间事件摘要：{recent_events}\n世界书注入：{world_book_context}\n\n"
                "请严格输出三段，用分隔线 --- 隔开：\n"
                "[环境正文]\n"
                "...\n"
                "---\n"
                "[内容小结]\n"
                "...\n"
                "---\n"
                "[检索摘要]\n"
                "用 1-2 句提炼环境中最值得检索的实体、地点、动作与状态线索。"
            )
        elif "检索摘要" not in template and "Retrieval Summary" not in template:
            template += (
                "\n\n请严格输出三段，并用分隔线 --- 隔开：\n"
                "[环境正文]\n"
                "...\n"
                "---\n"
                "[内容小结]\n"
                "...\n"
                "---\n"
                "[检索摘要]\n"
                "用 1-2 句提炼环境中最值得检索的实体、地点、动作与状态线索。"
            )

        time_context_block = (
            "【东八区时间上下文（UTC+8）】\n"
            f"- 时间：{narr.isoformat(timespec='seconds')}\n"
            f"- 日期：{narr.strftime('%Y年%m月%d日')}\n"
            f"- 星期：{weekday}\n"
            f"- 时段：{period}"
        )

        rendered_prompt = template.format(
            time=narr.isoformat(timespec="seconds"),
            date=narr.strftime("%Y年%m月%d日"),
            weekday=weekday,
            time_period=period,
            previous_env=json.dumps(previous_env or {}, ensure_ascii=False),
            continuity=continuity,
            latest_snapshot=character_state,
            time_elapsed=time_elapsed,
            character_state=character_state,
            recent_events=recent_events_text,
            world_book_context=world_book_context,
            location="",
            weather="",
            activity="",
            atmosphere="",
        )
        rendered_prompt = f"{time_context_block}\n\n{rendered_prompt}"

        if self.llm is None:
            fallback = self._reuse_previous_env(previous_env) if allow_retry_fallback else None
            if fallback is None:
                raise RuntimeError("Environment LLM is not configured.")
            return self._build_fallback_env(
                previous_env=fallback,
                narr=narr,
                period=period,
                weekday=weekday,
                continuity=continuity,
            )

        try:
            raw = await self.llm.chat(
                [
                    {"role": "system", "content": self.ENV_LLM_SYSTEM_PROMPT},
                    {"role": "user", "content": rendered_prompt},
                ],
                temperature=0.8,
                max_tokens=8192,
            )
            body, summary, retrieval = self.parse_environment_llm_output((raw or "").strip())
            activity_text = body or (raw or "").strip()
            summary_text = summary
            retrieval_summary = retrieval or self._build_retrieval_summary(
                summary_text=summary_text,
                activity_text=activity_text,
                recent_events_text=recent_events_text,
            )
            return {
                "time": narr.isoformat(timespec="seconds"),
                "time_period": period,
                "weekday": weekday,
                "location": "",
                "weather": "",
                "activity": activity_text,
                "atmosphere": "",
                "continuity": continuity,
                "summary": summary_text,
                "retrieval_summary": retrieval_summary,
                "stale": False,
                "stale_reason": "",
            }
        except Exception:
            if not allow_retry_fallback:
                raise
            fallback = self._reuse_previous_env(previous_env)
            if fallback is None:
                raise
            return self._build_fallback_env(
                previous_env=fallback,
                narr=narr,
                period=period,
                weekday=weekday,
                continuity=continuity,
            )

    @staticmethod
    def _reuse_previous_env(previous_env: dict | None) -> dict | None:
        if not previous_env or not isinstance(previous_env, dict):
            return None
        activity = str(previous_env.get("activity") or "").strip()
        summary = str(previous_env.get("summary") or "").strip()
        retrieval = str(previous_env.get("retrieval_summary") or "").strip()
        if not activity and not summary and not retrieval:
            return None
        return {
            "activity": activity,
            "summary": summary,
            "retrieval_summary": retrieval,
        }

    def _build_fallback_env(
        self,
        *,
        previous_env: dict,
        narr: datetime,
        period: str,
        weekday: str,
        continuity: str,
    ) -> dict:
        activity = str(previous_env.get("activity") or "").strip()
        summary = str(previous_env.get("summary") or "").strip()
        retrieval = str(previous_env.get("retrieval_summary") or "").strip()
        if not retrieval:
            retrieval = self._build_retrieval_summary(
                summary_text=summary,
                activity_text=activity,
                recent_events_text="",
            )
        return {
            "time": narr.isoformat(timespec="seconds"),
            "time_period": period,
            "weekday": weekday,
            "location": "",
            "weather": "",
            "activity": activity,
            "atmosphere": "",
            "continuity": continuity,
            "summary": summary,
            "retrieval_summary": retrieval,
            "stale": True,
            "stale_reason": "env_llm_failed_reused_previous",
        }

    def _build_continuity(self, previous_env: dict | None) -> str:
        if not previous_env:
            return ""
        prev_period = str(previous_env.get("time_period", "") or "")
        prev_hint = str(previous_env.get("summary", "") or "").strip()
        if not prev_hint:
            prev_hint = str(previous_env.get("activity") or "").strip()
        if not prev_hint:
            return ""
        period_prefix = f"上一时段（{prev_period}）" if prev_period else "上一时段"
        return f"{period_prefix}环境摘要：{prev_hint}"

    @staticmethod
    def _build_retrieval_summary(
        *,
        summary_text: str,
        activity_text: str,
        recent_events_text: str,
    ) -> str:
        summary = (summary_text or "").strip()
        if summary:
            return summary[:220]
        activity = re.sub(r"\s+", " ", (activity_text or "").strip())
        if activity:
            return activity[:220]
        recent = re.sub(r"\s+", " ", (recent_events_text or "").strip())
        return recent[:220]

    def _format_time_elapsed(self, hours: float) -> str:
        if hours <= 0:
            return ""
        if hours < 1:
            minutes = max(1, int(hours * 60))
            return f"约 {minutes} 分钟"
        if hours < 24:
            return f"约 {hours:.1f} 小时"
        return f"约 {hours / 24:.1f} 天"

    def _format_recent_events(self, events: list[dict]) -> str:
        if not events:
            return "（无近期事件）"
        lines: list[str] = []
        for event in events[:8]:
            title = str(event.get("title") or "").strip()
            description = str(event.get("description") or "").strip()
            date = str(event.get("date") or "").strip()
            label = title or description[:24] or "未命名事件"
            prefix = f"[{date}] " if date else ""
            lines.append(f"- {prefix}{label}")
        return "\n".join(lines)

    def _extract_keywords(self, latest_snapshot: str, events: list[dict]) -> list[str]:
        words: list[str] = []
        for token in re.findall(r"[A-Za-z0-9_\u4e00-\u9fff]{2,}", latest_snapshot or ""):
            words.append(token.lower())
        for event in events[:20]:
            for field in ("title", "description"):
                text = str(event.get(field) or "")
                for token in re.findall(r"[A-Za-z0-9_\u4e00-\u9fff]{2,}", text):
                    words.append(token.lower())
            raw_keywords = event.get("trigger_keywords")
            if isinstance(raw_keywords, list):
                words.extend([str(x).lower() for x in raw_keywords if str(x).strip()])
            elif isinstance(raw_keywords, str):
                try:
                    parsed = json.loads(raw_keywords)
                    if isinstance(parsed, list):
                        words.extend([str(x).lower() for x in parsed if str(x).strip()])
                except Exception:
                    pass
        return list(dict.fromkeys(words))[:120]

    def _match_world_book(self, entries: list, time_period: str, keywords: list[str]) -> list[str]:
        matched: list[str] = []
        kw_set = {k.lower() for k in keywords if k}
        period = (time_period or "").strip().lower()

        for entry in entries:
            name = ""
            content = ""
            entry_keywords: list[str] = []
            if isinstance(entry, str):
                content = entry.strip()
            elif isinstance(entry, dict):
                name = str(entry.get("name") or "").strip()
                content = str(entry.get("content") or "").strip()
                raw_match = entry.get("match_keywords")
                if isinstance(raw_match, list):
                    entry_keywords.extend([str(x).lower() for x in raw_match if str(x).strip()])
                elif isinstance(raw_match, str):
                    try:
                        parsed = json.loads(raw_match)
                        if isinstance(parsed, list):
                            entry_keywords.extend([str(x).lower() for x in parsed if str(x).strip()])
                    except Exception:
                        entry_keywords.extend(
                            [x.strip().lower() for x in re.split(r"[,，、\s]+", raw_match) if x.strip()]
                        )
            if not content:
                continue
            content_l = content.lower()
            hit = False
            if period and period in content_l:
                hit = True
            if not hit and entry_keywords and kw_set.intersection(entry_keywords):
                hit = True
            if not hit and kw_set:
                for kw in kw_set:
                    if kw and kw in content_l:
                        hit = True
                        break
            if not hit and not kw_set and len(matched) < 1:
                hit = True
            if hit:
                title = f"{name}：" if name else ""
                matched.append(f"{title}{content[: self.MAX_WORLD_BOOK_ITEM_CHARS]}")
            if len(matched) >= self.MAX_WORLD_BOOK_ITEMS:
                break
        return matched

    def _get_period(self, hour: int) -> str:
        for (start, end), name in self.PERIODS.items():
            if start <= hour < end:
                return name
        return "未知时段"
