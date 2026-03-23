from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import datetime

from server.llm_client import LLMClient
from server.prompts import PromptManager, KEY_PROMPT_ENVIRONMENT_GENERATION


class EnvironmentGenerator(ABC):
    """Abstract interface for generating environmental context.
    MVP: TemplateEnvironmentGenerator (fixed templates)
    Future: LLMEnvironmentGenerator (world book + dedicated LLM)
    """

    @abstractmethod
    async def generate(
        self,
        time_point: datetime,
        previous_env: dict | None,
        context: dict,
    ) -> dict:
        ...


class TemplateEnvironmentGenerator(EnvironmentGenerator):
    """Generate environment context, preferring LLM prompt-based output."""

    PERIODS = {
        (6, 9): "清晨",
        (9, 12): "上午",
        (12, 14): "午后",
        (14, 18): "下午",
        (18, 21): "傍晚",
        (21, 24): "深夜",
        (0, 6): "凌晨",
    }

    WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

    FALLBACK_SYSTEM_PROMPT = (
        "你是环境信息生成器。只输出环境描述正文，不要输出解释、标题或代码块。"
    )

    def __init__(
        self,
        prompt_manager: PromptManager | None = None,
        llm: LLMClient | None = None,
    ):
        self.prompt_manager = prompt_manager
        self.llm = llm

    async def generate(
        self,
        time_point: datetime,
        previous_env: dict | None,
        context: dict,
    ) -> dict:
        period = self._get_period(time_point.hour)
        weekday = self.WEEKDAY_NAMES[time_point.weekday()]

        if previous_env:
            prev_period = previous_env.get("time_period", "")
            prev_summary = previous_env.get("summary", "")
            continuity = f"此前（{prev_period}）的环境摘要：{prev_summary[:120]}"
        else:
            continuity = ""

        date_text = time_point.strftime('%Y年%m月%d日')
        default_summary = f"{date_text} {weekday} {period}。当前环境信息暂未生成。{continuity}"

        summary = default_summary
        template = ""
        if self.prompt_manager is not None:
            template = await self.prompt_manager.get_prompt(KEY_PROMPT_ENVIRONMENT_GENERATION)
        if not template:
            template = (
                "请根据给定时间和上下文生成一段环境摘要，保持叙事连贯且具体。\n"
                "时间：{time}\n"
                "日期：{date}\n"
                "星期：{weekday}\n"
                "时间段：{time_period}\n"
                "上一段环境：{previous_env}\n"
                "最新状态快照：{latest_snapshot}\n"
                "要求：输出 80-180 字中文描述。"
            )
        try:
            rendered_prompt = template.format(
                time=time_point.isoformat(),
                date=date_text,
                weekday=weekday,
                time_period=period,
                location="",
                weather="",
                activity="",
                atmosphere="",
                continuity=continuity,
                latest_snapshot=context.get("latest_snapshot", ""),
                previous_env=json.dumps(previous_env or {}, ensure_ascii=False),
            )
        except Exception:
            rendered_prompt = template

        if self.llm is not None:
            try:
                summary = (
                    await self.llm.chat(
                        [
                            {"role": "system", "content": self.FALLBACK_SYSTEM_PROMPT},
                            {"role": "user", "content": rendered_prompt},
                        ],
                        temperature=0.8,
                        max_tokens=500,
                    )
                ).strip()
                if not summary:
                    summary = default_summary
            except Exception:
                summary = default_summary
        else:
            summary = rendered_prompt

        return {
            "time": time_point.isoformat(),
            "time_period": period,
            "weekday": weekday,
            "location": "",
            "weather": "",
            "activity": summary[:120],
            "atmosphere": "",
            "continuity": continuity,
            "summary": summary,
        }

    def _get_period(self, hour: int) -> str:
        for (start, end), name in self.PERIODS.items():
            if start <= hour < end:
                return name
        return "未知时段"
