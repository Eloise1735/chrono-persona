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
        timeline_lines.append(f"Time (UTC+8): {time_text}")
    date_text = str(env.get("date") or "").strip()
    if date_text:
        timeline_lines.append(f"Date: {date_text}")
    weekday = str(env.get("weekday") or "").strip()
    if weekday:
        timeline_lines.append(f"Weekday: {weekday}")
    time_period = str(env.get("time_period") or "").strip()
    if time_period:
        timeline_lines.append(f"Period: {time_period}")
    timeline_text = (
        "[Timeline Anchor]\n" + "\n".join(f"- {line}" for line in timeline_lines)
        if timeline_lines
        else ""
    )
    body = str(env.get("activity") or "").strip()
    synopsis = str(env.get("summary") or "").strip()
    plan_delta = str(env.get("plan_delta") or "").strip()
    if plan_delta:
        synopsis = f"{synopsis}\n\nPlan delta: {plan_delta}".strip() if synopsis else f"Plan delta: {plan_delta}"
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
        (6, 9): "dawn",
        (9, 12): "morning",
        (12, 14): "noon",
        (14, 18): "afternoon",
        (18, 21): "evening",
        (21, 24): "night",
        (0, 6): "late_night",
    }
    WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    ENV_LLM_SYSTEM_PROMPT = (
        "You are the environment generator. Output exactly three sections: [Environment Body], [Summary], [Retrieval Summary]. "
        "Do not output chatty prefaces, JSON, or code blocks."
    )

    def __init__(self, prompt_manager: PromptManager | None = None, llm: LLMClient | None = None):
        self.prompt_manager = prompt_manager
        self.llm = llm

    @staticmethod
    def _coerce_trace_summary(value: object) -> str:
        text = str(value or "").strip()
        return text or "(no extra life-flow trace)"

    @staticmethod
    def _strip_env_section_header(text: str, markers: tuple[str, ...]) -> str:
        s = text.strip()
        for marker in markers:
            if s.startswith(marker):
                s = s[len(marker):].lstrip()
                break
        return s.strip()

    @classmethod
    def parse_environment_llm_output(cls, raw: str) -> tuple[str, str, str]:
        text = (raw or "").strip()
        if not text:
            return "", "", ""
        parts = [p.strip() for p in re.split(r"\n\s*(?:-{3,}|\*{3,})\s*\n", text) if p.strip()]
        if len(parts) >= 3:
            body = cls._strip_env_section_header(parts[0], ("[????]", "????", "[Environment Body]", "Environment Body"))
            summary = cls._strip_env_section_header(parts[1], ("[????]", "????", "[Summary]", "Summary"))
            retrieval = cls._strip_env_section_header(parts[2], ("[????]", "????", "[Retrieval Summary]", "Retrieval Summary"))
            return body.strip(), summary.strip(), retrieval.strip()
        if len(parts) == 2:
            body = cls._strip_env_section_header(parts[0], ("[????]", "????", "[Environment Body]", "Environment Body"))
            summary = cls._strip_env_section_header(parts[1], ("[????]", "????", "[Summary]", "Summary"))
            return body.strip(), summary.strip(), ""
        body = cls._strip_env_section_header(text, ("[????]", "????", "[Environment Body]", "Environment Body"))
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
        return t[: cls.MAX_CHARACTER_STATE_CHARS].rstrip() + "\n...(truncated previous state for continuity)"

    async def generate(self, time_point: datetime, previous_env: dict | None, context: dict, *, allow_retry_fallback: bool = True) -> dict:
        narr = self._narrative_local_time(time_point)
        period = self._get_period(narr.hour)
        weekday = self.WEEKDAY_NAMES[narr.weekday()]
        latest_snapshot = str(context.get("latest_snapshot", "") or "")
        character_state = self._clip_character_state(latest_snapshot)
        time_delta_hours = float(context.get("time_delta_hours", 0.0) or 0.0)
        recent_events = context.get("recent_events") or []
        world_book_entries = context.get("world_book_entries") or []
        current_plan_activity = str(context.get("current_plan_activity") or "").strip()
        current_plan_summary = str(context.get("current_plan_summary") or "").strip()
        current_conversation_state = str(context.get("current_conversation_state") or "").strip()
        recent_trace_summary = self._coerce_trace_summary(context.get("recent_trace_summary"))
        schedule_alignment = str(context.get("schedule_alignment") or "").strip()
        plan_delta = str(context.get("plan_delta") or "").strip()
        recent_events_text = self._format_recent_events(recent_events)
        time_elapsed = self._format_time_elapsed(time_delta_hours)
        continuity = self._build_continuity(previous_env)
        match_keywords = self._extract_keywords(latest_snapshot, recent_events)
        matched_world_books = self._match_world_book(world_book_entries, period, match_keywords)
        world_book_context = "\n".join(f"- {item}" for item in matched_world_books) if matched_world_books else "(no matched world book)"
        template = ""
        if self.prompt_manager is not None:
            template = await self.prompt_manager.get_prompt(KEY_PROMPT_ENVIRONMENT_GENERATION)
        if not template:
            template = (
                "Time: {time}\nDate: {date}\nWeekday: {weekday}\nPeriod: {time_period}\n"
                "Previous environment: {previous_env}\nContinuity hint: {continuity}\n"
                "Elapsed time: {time_elapsed}\nPrevious state summary: {character_state}\n"
                "Current schedule skeleton: {current_plan_summary}\nCurrent conversation state: {current_conversation_state}\n"
                "Recent life-flow trace: {recent_trace_summary}\nSchedule alignment: {schedule_alignment}\nPlan delta: {plan_delta}\n"
                "Recent events: {recent_events}\nWorld book context: {world_book_context}\n\n"
                "Output exactly three sections split by --- :\n"
                "[Environment Body]\n...\n---\n[Summary]\n"
                "Key moments: 1-3 most important scene nodes in the current environment.\n"
                "Dynamic changes: progress, obstruction, adjustment, interruption versus the previous period.\n"
                "Factual info: new agreements, plans, information, commitments.\n"
                "Plan delta: on track / interrupted / delayed / replaced by conversation / unexpected insert.\n"
                "Open loops: interrupted events, emotional blanks, unadvanced relationships for next continuity.\n"
                "---\n[Retrieval Summary]\n1-2 sentences with high-density searchable entities, places, actions, and state lines."
            )
        elif "Retrieval Summary" not in template and "????" not in template:
            template += "\n\nOutput exactly three sections split by --- :\n[Environment Body]\n...\n---\n[Summary]\n...\n---\n[Retrieval Summary]\n1-2 sentences with searchable entities, places, actions, and state lines."
        rendered_prompt = template.format(
            time=narr.isoformat(timespec="seconds"),
            date=narr.strftime("%Y-%m-%d"),
            weekday=weekday,
            time_period=period,
            previous_env=json.dumps(previous_env or {}, ensure_ascii=False),
            continuity=continuity,
            latest_snapshot=character_state,
            time_elapsed=time_elapsed,
            character_state=character_state,
            recent_events=recent_events_text,
            world_book_context=world_book_context,
            current_plan_summary=current_plan_summary or "(no schedule skeleton today)",
            current_conversation_state=current_conversation_state or "(no active conversation time claim)",
            recent_trace_summary=recent_trace_summary,
            schedule_alignment=schedule_alignment or "on_track",
            plan_delta=plan_delta or "(no visible plan delta)",
            location="",
            weather="",
            activity=current_plan_activity,
            atmosphere="",
        )
        time_context_block = (
            "[UTC+8 Context]\n"
            f"- Time: {narr.isoformat(timespec='seconds')}\n"
            f"- Date: {narr.strftime('%Y-%m-%d')}\n"
            f"- Weekday: {weekday}\n"
            f"- Period: {period}"
        )
        rendered_prompt = f"{time_context_block}\n\n{rendered_prompt}"
        if self.llm is None:
            fallback = self._reuse_previous_env(previous_env) if allow_retry_fallback else None
            if fallback is None:
                raise RuntimeError("Environment LLM is not configured.")
            return self._build_fallback_env(previous_env=fallback, narr=narr, period=period, weekday=weekday, continuity=continuity)
        try:
            raw = await self.llm.chat([
                {"role": "system", "content": self.ENV_LLM_SYSTEM_PROMPT},
                {"role": "user", "content": rendered_prompt},
            ], temperature=0.8, max_tokens=8192)
            body, summary, retrieval = self.parse_environment_llm_output((raw or "").strip())
            activity_text = body or (raw or "").strip()
            summary_text = summary
            retrieval_summary = retrieval or self._build_retrieval_summary(summary_text=summary_text, activity_text=activity_text, recent_events_text=recent_events_text)
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
                "current_plan_summary": current_plan_summary,
                "current_conversation_state": current_conversation_state,
                "recent_trace_summary": recent_trace_summary,
                "schedule_alignment": schedule_alignment,
                "plan_delta": plan_delta,
                "stale": False,
                "stale_reason": "",
            }
        except Exception:
            if not allow_retry_fallback:
                raise
            fallback = self._reuse_previous_env(previous_env)
            if fallback is None:
                raise
            return self._build_fallback_env(previous_env=fallback, narr=narr, period=period, weekday=weekday, continuity=continuity)

    @staticmethod
    def _reuse_previous_env(previous_env: dict | None) -> dict | None:
        if not previous_env or not isinstance(previous_env, dict):
            return None
        activity = str(previous_env.get("activity") or "").strip()
        summary = str(previous_env.get("summary") or "").strip()
        retrieval = str(previous_env.get("retrieval_summary") or "").strip()
        if not activity and not summary and not retrieval:
            return None
        return {"activity": activity, "summary": summary, "retrieval_summary": retrieval}

    def _build_fallback_env(self, *, previous_env: dict, narr: datetime, period: str, weekday: str, continuity: str) -> dict:
        activity = str(previous_env.get("activity") or "").strip()
        summary = str(previous_env.get("summary") or "").strip()
        retrieval = str(previous_env.get("retrieval_summary") or "").strip()
        if not retrieval:
            retrieval = self._build_retrieval_summary(summary_text=summary, activity_text=activity, recent_events_text="")
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
        period_prefix = f"Previous period ({prev_period})" if prev_period else "Previous period"
        return f"{period_prefix} summary: {prev_hint}"

    @staticmethod
    def _build_retrieval_summary(*, summary_text: str, activity_text: str, recent_events_text: str) -> str:
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
            return f"about {minutes} minutes"
        if hours < 24:
            return f"about {hours:.1f} hours"
        return f"about {hours / 24:.1f} days"

    def _format_recent_events(self, events: list[dict]) -> str:
        if not events:
            return "(no recent events)"
        lines: list[str] = []
        for event in events[:8]:
            title = str(event.get("title") or "").strip()
            description = str(event.get("description") or "").strip()
            date = str(event.get("date") or "").strip()
            label = title or description[:24] or "unnamed_event"
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
                        entry_keywords.extend([x.strip().lower() for x in re.split(r"[,??\s]+", raw_match) if x.strip()])
            if not content:
                continue
            content_l = content.lower()
            hit = False
            if period and period in content_l:
                hit = True
            if not hit and entry_keywords and kw_set.intersection(entry_keywords):
                hit = True
            if not hit and kw_set:
                hit = any(kw in content_l for kw in kw_set if kw)
            if not hit and not kw_set and len(matched) < 1:
                hit = True
            if hit:
                title = f"{name}: " if name else ""
                matched.append(f"{title}{content[: self.MAX_WORLD_BOOK_ITEM_CHARS]}")
            if len(matched) >= self.MAX_WORLD_BOOK_ITEMS:
                break
        return matched

    def _get_period(self, hour: int) -> str:
        for (start, end), name in self.PERIODS.items():
            if start <= hour < end:
                return name
        return "unknown_period"
