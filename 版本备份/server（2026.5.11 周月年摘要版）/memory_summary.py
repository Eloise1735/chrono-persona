from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

from server.database import Database
from server.llm_client import LLMClient
from server.models import (
    DailyPlan,
    EventAnchor,
    LifeDigestNode,
    LifeFlowTrace,
    MemorySummaryRun,
    PlanItem,
    RelationshipThought,
    StateSnapshot,
)
from server.prompts import PromptManager
from server.time_display import shanghai_now

logger = logging.getLogger(__name__)


DAY_SUMMARY_PROMPT = """
你是角色生活流日摘要整理器。请只输出 JSON，不要输出其他文本。

目标：
1. 同时整理关系线与角色自己的生活线，不能默认缺席任一侧。
2. 不要把生活线强行升级成正式事件；生活线只基于轻量生活节点、环境、快照、trace、计划偏移来整理。
3. 语言要具体，强调“今天如何推进”，少空泛情绪词。

输出结构：
{
  "relationship_lines": ["..."],
  "life_lines": ["..."],
  "plan_vs_reality": "...",
  "carry_forward_points": ["..."],
  "linked_event_ids": [1, 2],
  "linked_life_node_ids": [3, 4],
  "change_summary": "..."
}

日期窗口：{window_start} ~ {window_end}
输入材料：
{source_material}
""".strip()


HIERARCHICAL_SUMMARY_PROMPT = """
你是中景记忆摘要整理器。请只输出 JSON，不要输出其他文本。

目标：
1. 根据给定层级整理 1-3 条主线，允许关系线与角色生活线并重。
2. 如果当前层级是 week，优先从 day 摘要概括；如果是 month/year，优先从下层摘要概括。
3. 保留真正需要延续的中景线索，不要抄原文。

输出结构：
{
  "summary_pack": [
    {
      "line_title": "...",
      "line_summary": "...",
      "recent_shift": "...",
      "open_questions": ["..."],
      "linked_event_ids": [1, 2],
      "linked_life_node_ids": [3, 4]
    }
  ],
  "bridge_pack": [
    {
      "bridge_title": "...",
      "bridge_summary": "...",
      "why_it_still_matters_now": "...",
      "linked_event_ids": [1, 2],
      "linked_life_node_ids": [3, 4]
    }
  ],
  "event_line_digest": [
    {
      "line_title": "...",
      "line_summary": "...",
      "linked_event_ids": [1, 2],
      "linked_life_node_ids": [3, 4]
    }
  ],
  "change_summary": "..."
}

当前层级：{summary_level}
时间窗口：{window_start} ~ {window_end}
输入材料：
{source_material}
""".strip()


class LifeDigestExtractor:
    DOMAIN_KEYWORDS = {
        "work": ("工作", "项目", "会议", "任务", "文件", "排班", "研究", "交付"),
        "body": ("疼", "痛", "身体", "体温", "炎症", "不适", "药", "症状", "经期"),
        "routine": ("作息", "吃饭", "睡", "洗漱", "节律", "日常", "起床", "收尾"),
        "study": ("学习", "课程", "复习", "作业", "论文", "阅读"),
        "logistics": ("通勤", "路上", "外出", "排队", "采购", "搬", "快递"),
        "recovery": ("休息", "恢复", "缓一缓", "放空", "修整", "补觉"),
        "hobby": ("爱好", "画", "看剧", "游戏", "音乐", "手作"),
        "home": ("房间", "整理", "清洁", "收纳", "家里", "厨房"),
    }

    TITLE_PREFIX = {
        "work": "工作推进",
        "body": "身体状态",
        "routine": "生活节律",
        "study": "学习推进",
        "logistics": "日常事务",
        "recovery": "恢复进程",
        "hobby": "个人兴趣",
        "home": "居家整理",
    }

    @classmethod
    def extract_candidates(
        cls,
        *,
        node_date: str,
        snapshot: StateSnapshot | None,
        trace: LifeFlowTrace | None,
        environment: dict | None,
        plan: DailyPlan | None,
        plan_items: list[PlanItem],
        existing_nodes: list[LifeDigestNode],
        limit: int = 2,
    ) -> list[LifeDigestNode]:
        existing_texts = [cls._normalize_text(node.summary) for node in existing_nodes]
        candidates: list[LifeDigestNode] = []

        env_summary = cls._normalize_text((environment or {}).get("summary"))
        env_body = cls._normalize_text((environment or {}).get("activity"))
        plan_delta = cls._normalize_text((environment or {}).get("plan_delta"))
        schedule_alignment = str((environment or {}).get("schedule_alignment") or (trace.schedule_alignment if trace else "on_track")).strip()
        trace_summary = cls._normalize_text(trace.summary if trace else "")
        snapshot_text = cls._normalize_snapshot_text(snapshot.content if snapshot else "")

        merged_parts = [part for part in (trace_summary, env_summary, env_body, plan_delta) if part]
        merged_text = cls._dedupe_join(merged_parts)
        if cls._is_effective_life_progress(merged_text, schedule_alignment=schedule_alignment):
            candidates.append(
                cls._make_node(
                    node_date=node_date,
                    source_kind="merged",
                    text=merged_text,
                    life_domain=cls._infer_domain(merged_text),
                    linked_snapshot_id=int(snapshot.id or 0) or None if snapshot else None,
                    linked_trace_id=int(trace.id or 0) or None if trace else None,
                    linked_env_ref=f"snapshot:{int(snapshot.id or 0)}" if snapshot and snapshot.id else None,
                    existing_texts=existing_texts,
                )
            )

        if snapshot_text and cls._is_effective_life_progress(snapshot_text):
            candidates.append(
                cls._make_node(
                    node_date=node_date,
                    source_kind="snapshot",
                    text=snapshot_text,
                    life_domain=cls._infer_domain(snapshot_text),
                    linked_snapshot_id=int(snapshot.id or 0) or None if snapshot else None,
                    linked_trace_id=None,
                    linked_env_ref=f"snapshot:{int(snapshot.id or 0)}" if snapshot and snapshot.id else None,
                    existing_texts=existing_texts,
                )
            )

        if plan and plan_items:
            plan_text = cls._summarize_plan_drift(plan_items=plan_items, schedule_alignment=schedule_alignment, plan_delta=plan_delta)
            if plan_text and cls._is_effective_life_progress(plan_text, schedule_alignment=schedule_alignment):
                candidates.append(
                    cls._make_node(
                        node_date=node_date,
                        source_kind="environment",
                        text=plan_text,
                        life_domain=cls._infer_domain(plan_text),
                        linked_snapshot_id=int(snapshot.id or 0) or None if snapshot else None,
                        linked_trace_id=int(trace.id or 0) or None if trace else None,
                        linked_env_ref=f"plan:{int(plan.id or 0)}" if plan and plan.id else None,
                        existing_texts=existing_texts,
                    )
                )

        filtered: list[LifeDigestNode] = []
        seen_fingerprints: set[str] = set()
        for node in sorted(candidates, key=lambda item: (item.salience, item.carry_forward, item.novelty_score), reverse=True):
            if not node.summary:
                continue
            if node.source_fingerprint in seen_fingerprints:
                continue
            if any(cls._similarity(node.summary, existing) >= 0.82 for existing in existing_texts):
                continue
            seen_fingerprints.add(node.source_fingerprint)
            filtered.append(node)
            if len(filtered) >= limit:
                break
        return filtered

    @classmethod
    def _make_node(
        cls,
        *,
        node_date: str,
        source_kind: str,
        text: str,
        life_domain: str,
        linked_snapshot_id: int | None,
        linked_trace_id: int | None,
        linked_env_ref: str | None,
        existing_texts: list[str],
    ) -> LifeDigestNode:
        summary = cls._compact(text, 200)
        title = f"{cls.TITLE_PREFIX.get(life_domain, '生活推进')}：{cls._title_tail(summary)}"
        novelty = cls._estimate_novelty(summary, existing_texts)
        salience = cls._estimate_salience(summary)
        carry_forward = cls._estimate_carry_forward(summary)
        fingerprint = hashlib.md5(
            f"{node_date}|{source_kind}|{life_domain}|{linked_snapshot_id}|{linked_trace_id}|{summary}".encode("utf-8")
        ).hexdigest()
        return LifeDigestNode(
            node_date=node_date,
            source_kind=source_kind,  # type: ignore[arg-type]
            title=title[:80],
            summary=summary,
            life_domain=life_domain,  # type: ignore[arg-type]
            salience=salience,
            novelty_score=novelty,
            carry_forward=carry_forward,
            linked_snapshot_id=linked_snapshot_id,
            linked_trace_id=linked_trace_id,
            linked_env_ref=linked_env_ref,
            source_fingerprint=fingerprint,
        )

    @classmethod
    def _normalize_snapshot_text(cls, text: str) -> str:
        raw = cls._normalize_text(text)
        if not raw:
            return ""
        if any(label in raw for label in ("事实性信息", "关系动态变化", "情感关键时刻", "未完成线索")):
            raw = re.sub(r"【[^】]+】", "", raw)
        return cls._compact(raw, 220)

    @classmethod
    def _summarize_plan_drift(cls, *, plan_items: list[PlanItem], schedule_alignment: str, plan_delta: str) -> str:
        done_count = sum(1 for item in plan_items if str(item.status or "") == "done")
        skipped_count = sum(1 for item in plan_items if str(item.status or "") == "skipped")
        pending_count = sum(1 for item in plan_items if str(item.status or "") in {"pending", "executing"})
        if not plan_delta and schedule_alignment == "on_track" and skipped_count == 0:
            return ""
        parts = []
        if plan_delta:
            parts.append(plan_delta)
        parts.append(f"当天计划执行概况：完成 {done_count} 项，待处理 {pending_count} 项，跳过 {skipped_count} 项。")
        if schedule_alignment and schedule_alignment != "on_track":
            parts.append(f"节奏状态为 {schedule_alignment}，说明实际推进和原计划出现了偏移。")
        return cls._dedupe_join(parts)

    @classmethod
    def _infer_domain(cls, text: str) -> str:
        lowered = str(text or "").lower()
        for domain, keywords in cls.DOMAIN_KEYWORDS.items():
            if any(word in text or word in lowered for word in keywords):
                return domain
        return "routine"

    @staticmethod
    def _title_tail(summary: str) -> str:
        first = re.split(r"[。；！？\n]", summary)[0].strip()
        return first[:26] if first else "当日推进"

    @staticmethod
    def _normalize_text(text: Any) -> str:
        return re.sub(r"\s+", " ", str(text or "")).strip()

    @classmethod
    def _compact(cls, text: str, max_len: int) -> str:
        raw = cls._normalize_text(text)
        if len(raw) <= max_len:
            return raw
        return raw[: max_len - 1].rstrip() + "…"

    @classmethod
    def _dedupe_join(cls, parts: list[str]) -> str:
        seen: list[str] = []
        for part in parts:
            clean = cls._normalize_text(part)
            if clean and not any(cls._similarity(clean, old) >= 0.84 for old in seen):
                seen.append(clean)
        return " ".join(seen)

    @staticmethod
    def _similarity(a: str, b: str) -> float:
        set_a = {token for token in re.split(r"[\s，。；：、,.!?]+", a) if token}
        set_b = {token for token in re.split(r"[\s，。；：、,.!?]+", b) if token}
        if not set_a or not set_b:
            return 0.0
        return len(set_a & set_b) / max(1, len(set_a | set_b))

    @classmethod
    def _estimate_novelty(cls, summary: str, existing_texts: list[str]) -> float:
        if not existing_texts:
            return 0.82
        overlap = max((cls._similarity(summary, item) for item in existing_texts), default=0.0)
        return max(0.18, min(0.95, 0.92 - overlap))

    @staticmethod
    def _estimate_salience(summary: str) -> float:
        score = 0.38
        if any(token in summary for token in ("改", "推迟", "中断", "恢复", "复诊", "完成", "确认", "处理")):
            score += 0.18
        if any(token in summary for token in ("疼", "累", "睡", "吃", "通勤", "收拾", "经期")):
            score += 0.12
        if len(summary) >= 48:
            score += 0.08
        return max(0.2, min(0.95, score))

    @staticmethod
    def _estimate_carry_forward(summary: str) -> float:
        score = 0.34
        if any(token in summary for token in ("还要", "仍", "继续", "等待", "后续", "明天", "未完成", "偏移")):
            score += 0.24
        if any(token in summary for token in ("恢复", "复诊", "任务", "排班", "学习", "工作")):
            score += 0.12
        return max(0.15, min(0.95, score))

    @staticmethod
    def _is_effective_life_progress(text: str, *, schedule_alignment: str = "") -> bool:
        raw = str(text or "").strip()
        if len(raw) < 16:
            return False
        if any(token in raw for token in ("天气", "光线", "安静", "空气")) and len(raw) < 28:
            return False
        if schedule_alignment and schedule_alignment != "on_track":
            return True
        return any(
            token in raw
            for token in (
                "完成", "处理", "推迟", "中断", "恢复", "休息", "睡", "吃", "收拾",
                "工作", "学习", "通勤", "不适", "复诊", "任务", "计划", "偏移",
            )
        )


class MemorySummaryEngine:
    SUMMARY_MAX_TOKENS = 7000
    PREVIEW_LIMIT = 24

    def __init__(self, db: Database, llm: LLMClient, prompt_manager: PromptManager):
        self.db = db
        self.llm = llm
        self.prompt_manager = prompt_manager

    async def get_status(self, summary_level: str) -> dict:
        latest_applied = await self.db.get_latest_memory_summary_run(summary_level=summary_level, status="applied")
        latest_preview = await self.db.get_latest_memory_summary_run(summary_level=summary_level, status="preview")
        return {
            "summary_level": summary_level,
            "has_applied": latest_applied is not None,
            "has_pending_preview": latest_preview is not None,
            "latest_applied_window_end": latest_applied.window_end if latest_applied else None,
            "latest_preview_window_end": latest_preview.window_end if latest_preview else None,
        }

    async def list_runs(self, summary_level: str, *, status: str | None = None, limit: int = 20) -> list[dict]:
        runs = await self.db.list_memory_summary_runs(summary_level=summary_level, status=status, limit=limit)
        return [await self._hydrate_run_with_sources(run) for run in runs]

    async def get_pending_preview(self, summary_level: str) -> dict | None:
        run = await self.db.get_latest_memory_summary_run(summary_level=summary_level, status="preview")
        if run is None:
            return None
        payload = await self._hydrate_run_with_sources(run)
        payload["pending_preview_generated_at"] = run.created_at
        return payload

    async def preview(
        self,
        *,
        summary_level: str,
        window_start: str | None = None,
        window_end: str | None = None,
        store_pending: bool = False,
        source: str = "manual",
    ) -> dict:
        resolved_start, resolved_end = self._resolve_window(summary_level, window_start, window_end)
        source_bundle = await self._load_sources(
            summary_level=summary_level,
            window_start=resolved_start,
            window_end=resolved_end,
        )
        package = await self._build_summary_package(
            summary_level=summary_level,
            window_start=resolved_start,
            window_end=resolved_end,
            source_bundle=source_bundle,
        )
        package.update(
            {
                "summary_level": summary_level,
                "window_start": resolved_start,
                "window_end": resolved_end,
                "source_event_ids": [int(event.id or 0) for event in source_bundle["events"] if int(event.id or 0) > 0],
                "source_summary_run_ids": [int(run.id or 0) for run in source_bundle["runs"] if int(run.id or 0) > 0],
                "source_life_node_ids": [int(node.id or 0) for node in source_bundle["life_nodes"] if int(node.id or 0) > 0],
                "source_events_resolved": [self._serialize_event_preview(event) for event in source_bundle["events"][:12]],
                "source_summary_runs_resolved": [self._serialize_summary_run_preview(run) for run in source_bundle["runs"][:12]],
                "source_life_nodes_resolved": [self._serialize_life_node_preview(node) for node in source_bundle["life_nodes"][:12]],
                "preview_source": source,
            }
        )
        if store_pending:
            package = await self.save_pending_preview(summary_level, package, source=source)
        return package

    async def save_pending_preview(self, summary_level: str, preview: dict, *, source: str = "manual_edit") -> dict:
        payload = dict(preview)
        payload["summary_level"] = summary_level
        payload["preview_source"] = source
        now = datetime.utcnow().isoformat()
        await self.db.supersede_memory_summary_runs(summary_level=summary_level, status="preview")
        run = self._build_run_from_preview(summary_level=summary_level, preview=payload, created_at=now, status="preview")
        run_id = await self.db.insert_memory_summary_run(run)
        payload["memory_summary_run_id"] = run_id
        payload["pending_preview_generated_at"] = now
        return payload

    async def apply(self, summary_level: str, preview: dict) -> dict:
        payload = dict(preview)
        now = datetime.utcnow().isoformat()
        await self.db.supersede_memory_summary_runs(summary_level=summary_level, status="applied")
        run = self._build_run_from_preview(summary_level=summary_level, preview=payload, created_at=now, status="applied", applied_at=now)
        run_id = await self.db.insert_memory_summary_run(run)
        await self.db.supersede_memory_summary_runs(summary_level=summary_level, status="preview")
        await self._mark_summarized(
            event_ids=[int(x) for x in (payload.get("source_event_ids") or []) if int(x) > 0],
            summarized_level=summary_level,
        )
        return {
            "summary_level": summary_level,
            "memory_summary_run_id": run_id,
            "applied_at": now,
            "window_start": payload.get("window_start"),
            "window_end": payload.get("window_end"),
        }

    async def ensure_life_digest_nodes_for_date(self, target_date: str) -> list[LifeDigestNode]:
        existing = await self.db.list_life_digest_nodes(node_date=target_date, limit=8)
        snapshots = await self.db.get_snapshots_in_range(target_date, target_date)
        snapshot = snapshots[-1] if snapshots else None
        traces = await self.db.get_recent_life_flow_traces(start_date=target_date, end_date=target_date, limit=4)
        trace = traces[0] if traces else None
        environment = self._snapshot_environment_dict(snapshot)
        plan = await self.db.get_latest_daily_plan_for_date(target_date)
        plan_items = await self.db.list_plan_items(plan.id, limit=64) if plan and plan.id else []
        candidates = LifeDigestExtractor.extract_candidates(
            node_date=target_date,
            snapshot=snapshot,
            trace=trace,
            environment=environment,
            plan=plan,
            plan_items=plan_items,
            existing_nodes=existing,
            limit=max(0, 2 - len(existing)),
        )
        for node in candidates:
            duplicate = await self.db.get_life_digest_node_by_fingerprint(
                node_date=node.node_date,
                source_fingerprint=node.source_fingerprint,
            )
            if duplicate is None:
                node.id = await self.db.insert_life_digest_node(node)
                existing.append(node)
        return await self.db.list_life_digest_nodes(node_date=target_date, limit=8)

    async def ingest_life_digest_from_materials(
        self,
        *,
        target_date: str,
        snapshot: StateSnapshot | None,
        trace: LifeFlowTrace | None,
        environment: dict | None,
        plan: DailyPlan | None,
        plan_items: list[PlanItem],
    ) -> list[int]:
        existing = await self.db.list_life_digest_nodes(node_date=target_date, limit=8)
        candidates = LifeDigestExtractor.extract_candidates(
            node_date=target_date,
            snapshot=snapshot,
            trace=trace,
            environment=environment,
            plan=plan,
            plan_items=plan_items,
            existing_nodes=existing,
        )
        created_ids: list[int] = []
        for node in candidates:
            duplicate = await self.db.get_life_digest_node_by_fingerprint(
                node_date=node.node_date,
                source_fingerprint=node.source_fingerprint,
            )
            if duplicate is not None:
                continue
            node.id = await self.db.insert_life_digest_node(node)
            if node.id:
                created_ids.append(int(node.id))
        return created_ids

    async def _load_sources(
        self,
        *,
        summary_level: str,
        window_start: str,
        window_end: str,
    ) -> dict[str, Any]:
        if summary_level == "day":
            for cursor in self._date_span(window_start, window_end):
                await self.ensure_life_digest_nodes_for_date(cursor)
        events = await self.db.get_events_in_range(window_start, window_end, include_archived=False)
        relationship_events = [event for event in events if self._is_relationship_event(event)]
        life_nodes = await self.db.list_life_digest_nodes(start_date=window_start, end_date=window_end, limit=64)
        traces = await self.db.get_recent_life_flow_traces(start_date=window_start, end_date=window_end, limit=32)
        snapshots = await self.db.get_snapshots_in_range(window_start, window_end)
        plans = await self.db.list_daily_plans(start_date=window_start, end_date=window_end, limit=16)
        plan_map: dict[int, list[PlanItem]] = {}
        for plan in plans:
            if plan.id is not None:
                plan_map[int(plan.id)] = await self.db.list_plan_items(int(plan.id), limit=48)
        thoughts: list[RelationshipThought] = []
        for cursor in self._date_span(window_start, window_end):
            thoughts.extend(await self.db.list_relationship_thoughts(thought_date=cursor, limit=6))

        if summary_level == "day":
            runs: list[MemorySummaryRun] = []
        elif summary_level == "week":
            runs = await self.db.get_memory_summary_runs_in_window(
                summary_level="day",
                window_start=window_start,
                window_end=window_end,
                status="applied",
                limit=14,
            )
        else:
            lower_level = "week" if summary_level == "month" else "month"
            runs = await self.db.get_memory_summary_runs_in_window(
                summary_level=lower_level,
                window_start=window_start,
                window_end=window_end,
                status="applied",
                limit=40,
            )
        return {
            "events": relationship_events,
            "runs": runs,
            "life_nodes": life_nodes,
            "traces": traces,
            "snapshots": snapshots,
            "plans": plans,
            "plan_map": plan_map,
            "thoughts": thoughts,
        }

    async def _build_summary_package(
        self,
        *,
        summary_level: str,
        window_start: str,
        window_end: str,
        source_bundle: dict[str, Any],
    ) -> dict:
        if summary_level == "day":
            return await self._build_day_summary_package(window_start=window_start, window_end=window_end, source_bundle=source_bundle)
        if not source_bundle["events"] and not source_bundle["runs"] and not source_bundle["life_nodes"]:
            return {
                "summary_pack": [],
                "bridge_pack": [],
                "event_line_digest": [],
                "change_summary": "当前时间窗口内没有足够的关系或生活推进材料。",
            }
        source_material = self._format_source_material(summary_level=summary_level, source_bundle=source_bundle)
        prompt = HIERARCHICAL_SUMMARY_PROMPT.format(
            summary_level=summary_level,
            window_start=window_start,
            window_end=window_end,
            source_material=source_material,
        )
        parsed = await self._run_json_prompt(prompt)
        if not parsed:
            return self._fallback_hierarchical_package(summary_level=summary_level, source_bundle=source_bundle)
        event_lookup = {int(event.id or 0): event for event in source_bundle["events"]}
        life_lookup = {int(node.id or 0): node for node in source_bundle["life_nodes"]}
        return self._normalize_hierarchical_package(parsed, event_lookup=event_lookup, life_lookup=life_lookup)

    async def _build_day_summary_package(self, *, window_start: str, window_end: str, source_bundle: dict[str, Any]) -> dict:
        source_material = self._format_source_material(summary_level="day", source_bundle=source_bundle)
        prompt = DAY_SUMMARY_PROMPT.format(
            window_start=window_start,
            window_end=window_end,
            source_material=source_material,
        )
        parsed = await self._run_json_prompt(prompt)
        if not parsed:
            return self._fallback_day_package(source_bundle=source_bundle)
        event_ids = self._normalize_id_list(parsed.get("linked_event_ids"))
        life_ids = self._normalize_id_list(parsed.get("linked_life_node_ids"))
        return {
            "relationship_lines": self._normalize_string_list(parsed.get("relationship_lines"), limit=4),
            "life_lines": self._normalize_string_list(parsed.get("life_lines"), limit=4),
            "plan_vs_reality": self._compact(str(parsed.get("plan_vs_reality") or "").strip(), 180),
            "carry_forward_points": self._normalize_string_list(parsed.get("carry_forward_points"), limit=5),
            "linked_event_ids": event_ids,
            "linked_life_node_ids": life_ids,
            "change_summary": self._compact(str(parsed.get("change_summary") or "").strip(), 220),
            "summary_pack": [],
            "bridge_pack": [],
            "event_line_digest": [],
        }

    async def _run_json_prompt(self, prompt: str) -> dict:
        system_prompt = await self.prompt_manager.get_system_prompt()
        try:
            response = await self.llm.chat(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=self.SUMMARY_MAX_TOKENS,
            )
            return self._extract_json_object(response)
        except Exception:
            logger.exception("Memory summary generation failed; using fallback package.")
            return {}

    def _normalize_hierarchical_package(
        self,
        parsed: dict,
        *,
        event_lookup: dict[int, EventAnchor],
        life_lookup: dict[int, LifeDigestNode],
    ) -> dict:
        summary_pack: list[dict] = []
        for item in list(parsed.get("summary_pack") or [])[:3]:
            if not isinstance(item, dict):
                continue
            summary_pack.append(
                {
                    "line_title": self._compact(str(item.get("line_title") or "").strip(), 80),
                    "line_summary": self._compact(str(item.get("line_summary") or "").strip(), 220),
                    "recent_shift": self._compact(str(item.get("recent_shift") or "").strip(), 160),
                    "open_questions": self._normalize_string_list(item.get("open_questions"), limit=3),
                    "linked_event_ids": self._normalize_id_list(item.get("linked_event_ids")),
                    "linked_life_node_ids": self._normalize_id_list(item.get("linked_life_node_ids")),
                }
            )
        bridge_pack: list[dict] = []
        for item in list(parsed.get("bridge_pack") or [])[:4]:
            if not isinstance(item, dict):
                continue
            linked_event_ids = self._normalize_id_list(item.get("linked_event_ids"))
            linked_life_node_ids = self._normalize_id_list(item.get("linked_life_node_ids"))
            bridge_pack.append(
                {
                    "bridge_title": self._compact(str(item.get("bridge_title") or "").strip(), 80),
                    "bridge_summary": self._compact(str(item.get("bridge_summary") or "").strip(), 180),
                    "why_it_still_matters_now": self._compact(str(item.get("why_it_still_matters_now") or "").strip(), 180),
                    "linked_event_ids": linked_event_ids,
                    "linked_life_node_ids": linked_life_node_ids,
                    "key_details_resolved": self._resolve_material_details(linked_event_ids, linked_life_node_ids, event_lookup, life_lookup),
                }
            )
        event_line_digest: list[dict] = []
        for item in list(parsed.get("event_line_digest") or [])[:3]:
            if not isinstance(item, dict):
                continue
            event_line_digest.append(
                {
                    "line_title": self._compact(str(item.get("line_title") or "").strip(), 80),
                    "line_summary": self._compact(str(item.get("line_summary") or "").strip(), 220),
                    "linked_event_ids": self._normalize_id_list(item.get("linked_event_ids")),
                    "linked_life_node_ids": self._normalize_id_list(item.get("linked_life_node_ids")),
                }
            )
        return {
            "summary_pack": summary_pack,
            "bridge_pack": bridge_pack,
            "event_line_digest": event_line_digest,
            "change_summary": self._compact(str(parsed.get("change_summary") or "").strip(), 220),
        }

    def _fallback_day_package(self, *, source_bundle: dict[str, Any]) -> dict:
        relationship_lines = [
            self._compact(self._event_core_text(event) or str(event.title or "").strip(), 140)
            for event in source_bundle["events"][:3]
            if self._event_core_text(event) or str(event.title or "").strip()
        ]
        for thought in source_bundle["thoughts"][:2]:
            text = self._compact(str(thought.content or "").strip(), 140)
            if text:
                relationship_lines.append(text)
        life_lines = [
            self._compact(str(node.summary or "").strip(), 140)
            for node in source_bundle["life_nodes"][:4]
            if str(node.summary or "").strip()
        ]
        if not life_lines:
            life_lines = [
                self._compact(str(trace.summary or "").strip(), 140)
                for trace in source_bundle["traces"][:2]
                if str(trace.summary or "").strip()
            ]
        plan_vs_reality = self._build_plan_vs_reality_text(source_bundle["plans"], source_bundle["plan_map"], source_bundle["traces"])
        carry_forward_points = self._build_carry_forward_points(source_bundle["events"], source_bundle["life_nodes"])
        return {
            "relationship_lines": relationship_lines[:4],
            "life_lines": life_lines[:4],
            "plan_vs_reality": plan_vs_reality,
            "carry_forward_points": carry_forward_points[:5],
            "linked_event_ids": [int(event.id or 0) for event in source_bundle["events"][:4] if int(event.id or 0) > 0],
            "linked_life_node_ids": [int(node.id or 0) for node in source_bundle["life_nodes"][:4] if int(node.id or 0) > 0],
            "change_summary": "已基于关系事件、生活节点、trace 与计划偏移生成日摘要。",
            "summary_pack": [],
            "bridge_pack": [],
            "event_line_digest": [],
        }

    def _fallback_hierarchical_package(self, *, summary_level: str, source_bundle: dict[str, Any]) -> dict:
        if source_bundle["runs"]:
            lines = []
            for run in source_bundle["runs"][:3]:
                day_digest = self._parse_json_object(run.day_digest_json)
                summary_items = self._parse_json_list_of_dicts(run.summary_pack_json)
                title = self._compact(
                    str(
                        (day_digest.get("relationship_lines") or [""])[0]
                        or (day_digest.get("life_lines") or [""])[0]
                        or (summary_items[0].get("line_title") if summary_items else "")
                        or f"{run.summary_level} 摘要"
                    ).strip(),
                    80,
                )
                body = self._compact(
                    " ".join(
                        [
                            *((day_digest.get("relationship_lines") or [])[:1]),
                            *((day_digest.get("life_lines") or [])[:1]),
                            str(day_digest.get("plan_vs_reality") or "").strip(),
                        ]
                    ).strip(),
                    180,
                )
                lines.append(
                    {
                        "line_title": title,
                        "line_summary": body or title,
                        "recent_shift": "",
                        "open_questions": (day_digest.get("carry_forward_points") or [])[:2],
                        "linked_event_ids": self._normalize_id_list(day_digest.get("linked_event_ids")),
                        "linked_life_node_ids": self._normalize_id_list(day_digest.get("linked_life_node_ids")),
                    }
                )
            return {
                "summary_pack": lines,
                "bridge_pack": [
                    {
                        "bridge_title": line["line_title"],
                        "bridge_summary": line["line_summary"],
                        "why_it_still_matters_now": "这条线仍在影响接下来的生活推进或关系判断。",
                        "linked_event_ids": line["linked_event_ids"],
                        "linked_life_node_ids": line["linked_life_node_ids"],
                        "key_details_resolved": [],
                    }
                    for line in lines[:3]
                ],
                "event_line_digest": [
                    {
                        "line_title": line["line_title"],
                        "line_summary": line["line_summary"],
                        "linked_event_ids": line["linked_event_ids"],
                        "linked_life_node_ids": line["linked_life_node_ids"],
                    }
                    for line in lines[:3]
                ],
                "change_summary": f"已基于下层摘要生成 {summary_level} 层主线概括。",
            }

        lines: list[dict] = []
        for event in source_bundle["events"][:2]:
            lines.append(
                {
                    "line_title": self._compact(str(event.title or "").strip() or "关系推进", 80),
                    "line_summary": self._compact(self._event_core_text(event), 180),
                    "recent_shift": "",
                    "open_questions": [],
                    "linked_event_ids": [int(event.id or 0)] if int(event.id or 0) > 0 else [],
                    "linked_life_node_ids": [],
                }
            )
        for node in source_bundle["life_nodes"][:2]:
            lines.append(
                {
                    "line_title": self._compact(str(node.title or "").strip() or "生活推进", 80),
                    "line_summary": self._compact(str(node.summary or "").strip(), 180),
                    "recent_shift": "",
                    "open_questions": [],
                    "linked_event_ids": [],
                    "linked_life_node_ids": [int(node.id or 0)] if int(node.id or 0) > 0 else [],
                }
            )
        lines = lines[:3]
        return {
            "summary_pack": lines,
            "bridge_pack": [
                {
                    "bridge_title": line["line_title"],
                    "bridge_summary": line["line_summary"],
                    "why_it_still_matters_now": "这条线仍会影响后续一段时间的生活或关系节奏。",
                    "linked_event_ids": line["linked_event_ids"],
                    "linked_life_node_ids": line["linked_life_node_ids"],
                    "key_details_resolved": [],
                }
                for line in lines
            ],
            "event_line_digest": [
                {
                    "line_title": line["line_title"],
                    "line_summary": line["line_summary"],
                    "linked_event_ids": line["linked_event_ids"],
                    "linked_life_node_ids": line["linked_life_node_ids"],
                }
                for line in lines
            ],
            "change_summary": f"已基于事件与生活节点生成 {summary_level} 层摘要。",
        }

    async def _mark_summarized(self, *, event_ids: list[int], summarized_level: str) -> None:
        for event_id in list(dict.fromkeys(event_ids)):
            event = await self.db.get_event_by_id(int(event_id))
            if event is None:
                continue
            payload = self._parse_json_object(getattr(event, "meta_json", None))
            payload["summarized_level"] = summarized_level
            payload["activation_count"] = max(1, int(payload.get("activation_count") or 1))
            payload["last_activated_at"] = datetime.utcnow().isoformat()
            await self.db.update_event(int(event_id), meta_json=json.dumps(payload, ensure_ascii=False))

    def _build_run_from_preview(
        self,
        *,
        summary_level: str,
        preview: dict,
        created_at: str,
        status: str,
        applied_at: str | None = None,
    ) -> MemorySummaryRun:
        day_digest = {}
        if summary_level == "day":
            day_digest = {
                "relationship_lines": preview.get("relationship_lines") or [],
                "life_lines": preview.get("life_lines") or [],
                "plan_vs_reality": preview.get("plan_vs_reality") or "",
                "carry_forward_points": preview.get("carry_forward_points") or [],
                "linked_event_ids": preview.get("linked_event_ids") or [],
                "linked_life_node_ids": preview.get("linked_life_node_ids") or [],
                "change_summary": preview.get("change_summary") or "",
            }
        return MemorySummaryRun(
            summary_level=summary_level,  # type: ignore[arg-type]
            window_start=str(preview.get("window_start") or ""),
            window_end=str(preview.get("window_end") or ""),
            created_at=created_at,
            applied_at=applied_at,
            status=status,  # type: ignore[arg-type]
            source_event_ids_json=json.dumps(preview.get("source_event_ids") or [], ensure_ascii=False),
            source_summary_run_ids_json=json.dumps(preview.get("source_summary_run_ids") or [], ensure_ascii=False),
            source_life_node_ids_json=json.dumps(preview.get("source_life_node_ids") or [], ensure_ascii=False),
            summary_pack_json=json.dumps(preview.get("summary_pack") or [], ensure_ascii=False),
            bridge_pack_json=json.dumps(preview.get("bridge_pack") or [], ensure_ascii=False),
            event_line_digest_json=json.dumps(preview.get("event_line_digest") or [], ensure_ascii=False),
            day_digest_json=json.dumps(day_digest, ensure_ascii=False),
            editor_notes=str(preview.get("editor_notes") or "").strip() or None,
        )

    def _resolve_window(self, summary_level: str, window_start: str | None, window_end: str | None) -> tuple[str, str]:
        if window_start and window_end:
            return window_start, window_end
        today = shanghai_now().date()
        if summary_level == "day":
            yesterday = today - timedelta(days=1)
            return yesterday.isoformat(), yesterday.isoformat()
        if summary_level == "week":
            start = today - timedelta(days=today.weekday())
            end = start + timedelta(days=6)
            return start.isoformat(), end.isoformat()
        if summary_level == "month":
            start = today.replace(day=1)
            if start.month == 12:
                end = date(start.year + 1, 1, 1) - timedelta(days=1)
            else:
                end = date(start.year, start.month + 1, 1) - timedelta(days=1)
            return start.isoformat(), end.isoformat()
        start = date(today.year, 1, 1)
        end = date(today.year, 12, 31)
        return start.isoformat(), end.isoformat()

    def _is_relationship_event(self, event: EventAnchor) -> bool:
        payload = self._parse_json_object(getattr(event, "meta_json", None))
        if payload.get("evolution_candidate") or payload.get("bridge_candidate"):
            return True
        scope = str(payload.get("scope") or "").strip()
        if scope in {"shared", "user_side"}:
            return True
        text = "\n".join([str(event.title or ""), str(event.description or ""), str(event.categories or "")]).lower()
        return any(token in text for token in ("关系", "对话", "陪", "嫉妒", "分身", "用户", "eloise"))

    def _format_source_material(self, *, summary_level: str, source_bundle: dict[str, Any]) -> str:
        blocks: list[str] = []
        if summary_level == "day":
            for event in source_bundle["events"][:8]:
                blocks.append(
                    "\n".join(
                        [
                            f"REL_EVENT #{int(event.id or 0)}",
                            f"DATE: {event.date}",
                            f"TITLE: {str(event.title or '').strip()}",
                            f"BODY: {self._event_core_text(event)}",
                        ]
                    )
                )
            for node in source_bundle["life_nodes"][:8]:
                blocks.append(
                    "\n".join(
                        [
                            f"LIFE_NODE #{int(node.id or 0)}",
                            f"DATE: {node.node_date}",
                            f"DOMAIN: {node.life_domain}",
                            f"TITLE: {node.title}",
                            f"SUMMARY: {node.summary}",
                        ]
                    )
                )
            for trace in source_bundle["traces"][:4]:
                blocks.append(
                    "\n".join(
                        [
                            f"TRACE #{int(trace.id or 0)}",
                            f"DATE: {trace.trace_date}",
                            f"ALIGNMENT: {trace.schedule_alignment}",
                            f"SUMMARY: {trace.summary}",
                        ]
                    )
                )
            for thought in source_bundle["thoughts"][:4]:
                blocks.append(
                    "\n".join(
                        [
                            f"REL_THOUGHT #{int(thought.id or 0)}",
                            f"TOPIC: {thought.topic_line}",
                            f"CONTENT: {thought.content}",
                        ]
                    )
                )
            plan_text = self._build_plan_vs_reality_text(source_bundle["plans"], source_bundle["plan_map"], source_bundle["traces"])
            if plan_text:
                blocks.append(f"PLAN_VS_REALITY: {plan_text}")
            return "\n\n".join(blocks)

        for run in source_bundle["runs"][: self.PREVIEW_LIMIT]:
            day_digest = self._parse_json_object(run.day_digest_json)
            if day_digest:
                blocks.append(
                    "\n".join(
                        [
                            f"DAY_RUN #{int(run.id or 0)}",
                            f"WINDOW: {run.window_start} ~ {run.window_end}",
                            f"REL: {' | '.join((day_digest.get('relationship_lines') or [])[:2])}",
                            f"LIFE: {' | '.join((day_digest.get('life_lines') or [])[:2])}",
                            f"PLAN: {str(day_digest.get('plan_vs_reality') or '').strip()}",
                            f"CARRY: {' | '.join((day_digest.get('carry_forward_points') or [])[:3])}",
                        ]
                    )
                )
                continue
            blocks.append(
                "\n".join(
                    [
                        f"SUMMARY_RUN #{int(run.id or 0)}",
                        f"LEVEL: {run.summary_level}",
                        f"WINDOW: {run.window_start} ~ {run.window_end}",
                        f"SUMMARY: {self._compact(self._summary_run_core_text(run), 320)}",
                    ]
                )
            )
        for event in source_bundle["events"][:6]:
            blocks.append(
                "\n".join(
                    [
                        f"SUP_REL_EVENT #{int(event.id or 0)}",
                        f"DATE: {event.date}",
                        f"TITLE: {str(event.title or '').strip()}",
                        f"BODY: {self._event_core_text(event)}",
                    ]
                )
            )
        for node in source_bundle["life_nodes"][:6]:
            blocks.append(
                "\n".join(
                    [
                        f"SUP_LIFE_NODE #{int(node.id or 0)}",
                        f"DATE: {node.node_date}",
                        f"DOMAIN: {node.life_domain}",
                        f"SUMMARY: {node.summary}",
                    ]
                )
            )
        return "\n\n".join(blocks)

    def _build_plan_vs_reality_text(self, plans: list[DailyPlan], plan_map: dict[int, list[PlanItem]], traces: list[LifeFlowTrace]) -> str:
        if not plans:
            if traces:
                top_trace = traces[0]
                if str(top_trace.schedule_alignment or "") != "on_track":
                    return self._compact(f"当天没有单独计划记录，但生活节奏呈现 {top_trace.schedule_alignment}。{top_trace.summary}", 180)
            return ""
        plan = plans[-1]
        items = plan_map.get(int(plan.id or 0), [])
        done_count = sum(1 for item in items if str(item.status or "") == "done")
        skipped_count = sum(1 for item in items if str(item.status or "") == "skipped")
        pending_count = sum(1 for item in items if str(item.status or "") in {"pending", "executing"})
        alignment = ""
        if traces:
            latest_same_day = next((trace for trace in traces if trace.trace_date == plan.plan_date), traces[0])
            alignment = str(latest_same_day.schedule_alignment or "").strip()
        text = f"计划侧记录了 {len(items)} 项事项，完成 {done_count} 项，待处理 {pending_count} 项，跳过 {skipped_count} 项。"
        if alignment and alignment != "on_track":
            text += f" 实际推进呈现 {alignment}，说明当天节奏和原安排存在偏移。"
        return self._compact(text, 180)

    def _build_carry_forward_points(self, events: list[EventAnchor], life_nodes: list[LifeDigestNode]) -> list[str]:
        points: list[str] = []
        for node in sorted(life_nodes, key=lambda item: (item.carry_forward, item.salience), reverse=True)[:3]:
            text = self._compact(str(node.summary or "").strip(), 120)
            if text:
                points.append(text)
        for event in events[:2]:
            text = self._compact(self._event_core_text(event), 120)
            if text:
                points.append(text)
        deduped: list[str] = []
        for item in points:
            if item and item not in deduped:
                deduped.append(item)
        return deduped

    def _summary_run_core_text(self, run: MemorySummaryRun) -> str:
        day_digest = self._parse_json_object(run.day_digest_json)
        if day_digest:
            return " ".join(
                [
                    *((day_digest.get("relationship_lines") or [])[:1]),
                    *((day_digest.get("life_lines") or [])[:1]),
                    str(day_digest.get("plan_vs_reality") or "").strip(),
                ]
            ).strip()
        summary_items = self._parse_json_list_of_dicts(run.summary_pack_json)
        bridge_items = self._parse_json_list_of_dicts(run.bridge_pack_json)
        parts = [str(item.get("line_summary") or "").strip() for item in summary_items[:2]]
        parts.extend(str(item.get("why_it_still_matters_now") or "").strip() for item in bridge_items[:1])
        return " ".join(part for part in parts if part)

    def _resolve_material_details(
        self,
        event_ids: list[int],
        life_ids: list[int],
        event_lookup: dict[int, EventAnchor],
        life_lookup: dict[int, LifeDigestNode],
    ) -> list[str]:
        details: list[str] = []
        for event_id in event_ids[:2]:
            event = event_lookup.get(int(event_id))
            if event:
                details.append(f"[{event.date}] {self._compact(str(event.title or '').strip(), 40)} {self._compact(self._event_core_text(event), 80)}")
        for node_id in life_ids[:2]:
            node = life_lookup.get(int(node_id))
            if node:
                details.append(f"[{node.node_date}] {self._compact(str(node.title or '').strip(), 40)} {self._compact(str(node.summary or '').strip(), 80)}")
        return details[:4]

    def _serialize_event_preview(self, event: EventAnchor) -> dict:
        return {
            "id": int(event.id or 0),
            "date": event.date,
            "title": str(event.title or "").strip(),
            "description": self._event_core_text(event),
        }

    def _serialize_life_node_preview(self, node: LifeDigestNode) -> dict:
        return {
            "id": int(node.id or 0),
            "node_date": node.node_date,
            "source_kind": node.source_kind,
            "title": node.title,
            "summary": node.summary,
            "life_domain": node.life_domain,
            "salience": node.salience,
            "novelty_score": node.novelty_score,
            "carry_forward": node.carry_forward,
        }

    def _serialize_summary_run_preview(self, run: MemorySummaryRun) -> dict:
        payload = {
            "id": int(run.id or 0),
            "summary_level": run.summary_level,
            "window_start": run.window_start,
            "window_end": run.window_end,
        }
        day_digest = self._parse_json_object(run.day_digest_json)
        if day_digest:
            payload["day_digest"] = day_digest
        else:
            payload["summary_pack"] = self._parse_json_list_of_dicts(run.summary_pack_json)
            payload["bridge_pack"] = self._parse_json_list_of_dicts(run.bridge_pack_json)
        return payload

    def _hydrate_run(self, run: MemorySummaryRun) -> dict:
        day_digest = self._parse_json_object(run.day_digest_json)
        payload = {
            "id": int(run.id or 0),
            "summary_level": run.summary_level,
            "window_start": run.window_start,
            "window_end": run.window_end,
            "created_at": run.created_at,
            "applied_at": run.applied_at,
            "status": run.status,
            "source_event_ids": self._normalize_id_list(self._parse_json_maybe(run.source_event_ids_json)),
            "source_summary_run_ids": self._normalize_id_list(self._parse_json_maybe(run.source_summary_run_ids_json)),
            "source_life_node_ids": self._normalize_id_list(self._parse_json_maybe(run.source_life_node_ids_json)),
            "summary_pack": self._parse_json_list_of_dicts(run.summary_pack_json),
            "bridge_pack": self._parse_json_list_of_dicts(run.bridge_pack_json),
            "event_line_digest": self._parse_json_list_of_dicts(run.event_line_digest_json),
            "editor_notes": run.editor_notes,
        }
        if day_digest:
            payload.update(day_digest)
        return payload

    async def _hydrate_run_with_sources(self, run: MemorySummaryRun) -> dict:
        payload = self._hydrate_run(run)
        source_event_ids = list(payload.get("source_event_ids") or [])
        source_summary_run_ids = list(payload.get("source_summary_run_ids") or [])
        source_life_node_ids = list(payload.get("source_life_node_ids") or [])
        source_events = await self.db.get_events_by_ids(source_event_ids[:12]) if source_event_ids else []
        source_life_nodes = await self.db.list_life_digest_nodes(limit=128)
        life_lookup = {int(node.id or 0): node for node in source_life_nodes}
        source_summary_runs: list[MemorySummaryRun] = []
        for summary_level in ("day", "week", "month", "year"):
            source_summary_runs.extend(
                [
                    item
                    for item in await self.db.list_memory_summary_runs(summary_level=summary_level, limit=64)
                    if int(item.id or 0) in source_summary_run_ids
                ]
            )
        payload["source_events_resolved"] = [self._serialize_event_preview(event) for event in source_events]
        payload["source_summary_runs_resolved"] = [self._serialize_summary_run_preview(item) for item in source_summary_runs[:12]]
        payload["source_life_nodes_resolved"] = [
            self._serialize_life_node_preview(life_lookup[item_id])
            for item_id in source_life_node_ids
            if item_id in life_lookup
        ][:12]
        return payload

    def _event_core_text(self, event: EventAnchor) -> str:
        description = str(event.description or "").strip()
        match = re.search(r"(客观记录|主要内容|触发情境|时间)\s*[:：]\s*(.+)", description, flags=re.S)
        if match:
            return self._compact(match.group(2).strip(), 260)
        return self._compact(description, 260)

    def _extract_json_object(self, text: str) -> dict:
        raw = str(text or "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            pass
        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.S)
        if fence:
            try:
                parsed = json.loads(fence.group(1))
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                pass
        obj = re.search(r"(\{.*\})", raw, flags=re.S)
        if obj:
            try:
                parsed = json.loads(obj.group(1))
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                return {}
        return {}

    def _parse_json_object(self, value: Any) -> dict:
        if isinstance(value, dict):
            return dict(value)
        try:
            parsed = json.loads(str(value or "{}"))
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    def _parse_json_maybe(self, value: Any) -> Any:
        try:
            return json.loads(str(value or "null"))
        except Exception:
            return None

    def _parse_json_list_of_dicts(self, value: Any) -> list[dict]:
        parsed = self._parse_json_maybe(value)
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
        return []

    def _normalize_id_list(self, value: Any) -> list[int]:
        if isinstance(value, list):
            output = []
            for item in value:
                try:
                    parsed = int(item)
                except Exception:
                    continue
                if parsed > 0:
                    output.append(parsed)
            return list(dict.fromkeys(output))
        return []

    def _normalize_string_list(self, value: Any, *, limit: int = 3) -> list[str]:
        if not isinstance(value, list):
            return []
        items = []
        for item in value:
            text = self._compact(str(item).strip(), 120)
            if text:
                items.append(text)
        return items[:limit]

    def _compact(self, text: str, max_len: int) -> str:
        raw = re.sub(r"\s+", " ", str(text or "")).strip()
        if len(raw) <= max_len:
            return raw
        return raw[: max(0, max_len - 1)].rstrip() + "…"

    @staticmethod
    def _snapshot_environment_dict(snapshot: StateSnapshot | None) -> dict | None:
        if snapshot is None:
            return None
        raw = str(snapshot.environment or "").strip()
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _date_span(start_date: str, end_date: str) -> list[str]:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        cursor = start
        result: list[str] = []
        while cursor <= end:
            result.append(cursor.isoformat())
            cursor += timedelta(days=1)
        return result
