from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta

from server.config import AppConfig
from server.database import Database
from server.diagnostics import OperationTracer
from server.environment import (
    EnvironmentGenerator,
    environment_text_for_prompt,
    environment_text_for_retrieval,
)
from server.llm_client import LLMClient
from server.memory_store import MemoryStore
from server.models import (
    ConversationTimeClaim,
    DisturbancePulse,
    StateSnapshot,
    EventAnchor,
    KeyRecord,
    LifeFlowTrace,
    RelationshipState,
    RelationshipThought,
    SlowLine,
    KEY_RECORD_TYPES,
    LEGACY_KEY_RECORD_TYPE_MAP,
    format_utc_instant_z,
)
from server.time_display import (
    iso_string_for_cst_display,
    parse_db_instant_to_shanghai,
    parse_user_instant_to_shanghai,
    shanghai_now,
    shanghai_time_to_utc_naive,
    utc_naive_to_shanghai_iso,
)
from server.event_taxonomy import classify_event, make_event_title
from server.prompts import (
    PromptManager,
    KEY_PROMPT_SNAPSHOT_GENERATION,
    KEY_PROMPT_EVENT_ANCHOR,
    KEY_PROMPT_EVENT_TRIGGER_JUDGE,
    KEY_PROMPT_EVENT_MATERIALIZE,
    KEY_PROMPT_DISTURBANCE_JUDGE,
    KEY_PROMPT_DISTURBANCE_MATERIALIZE,
    KEY_PROMPT_KEY_RECORD_CANDIDATE_ROUTE,
    KEY_PROMPT_REFLECT_SNAPSHOT,
    KEY_PROMPT_CONVERSATION_SUMMARY,
    KEY_PROMPT_PERIODIC_REVIEW,
    KEY_MIN_TIME_UNIT_HOURS,
    KEY_INJECT_HOT_EVENTS_LIMIT,
    KEY_INJECT_YESTERDAY_EVENTS_LIMIT,
    KEY_SNAPSHOT_CATCHUP_MAX_STEPS_PER_RUN,
    KEY_SNAPSHOT_RECENT_EVENTS_LIMIT,
    KEY_SNAPSHOT_EVENT_CANDIDATE_ENABLED,
    KEY_SNAPSHOT_SCHEDULER_ENABLED,
    KEY_SNAPSHOT_SCHEDULER_INTERVAL_SEC,
    KEY_L1_CHARACTER_BACKGROUND,
    KEY_L1_USER_BACKGROUND,
    KEY_L2_CHARACTER_PERSONALITY,
    KEY_L2_LIFE_STATUS,
    KEY_L2_RELATIONSHIP_DYNAMICS,
)
from server.automation_engine import AutomationEngine

logger = logging.getLogger(__name__)


class StateMachine:
    DEFAULT_MEMORY_TOP_K = 2
    DEFAULT_RECENT_EVENTS_LIMIT = 5
    DEFAULT_SCHEDULER_INTERVAL_SEC = 60
    DEFAULT_CATCHUP_MAX_STEPS = 3
    REQUEST_CATCHUP_MAX_STEPS = 1
    CONVERSATION_CLAIM_IDLE_TIMEOUT_MINUTES = 120
    DISTURBANCE_EXTERNAL_TARGET_SHARE = 0.22
    DISTURBANCE_EXTERNAL_MAX_SHARE = 0.28
    DISTURBANCE_RECENT_LIMIT = 6
    # 仅「无整格、仅尾部对齐到对话当下」时：对话时刻与最后一条快照间隔需大于该值才生成（否则认为间隔过短不必刷新）
    TAIL_ONLY_SNAPSHOT_MIN_GAP_HOURS = 2.0

    @staticmethod
    def _extract_event_field_block(text: str, field_labels: list[str]) -> str:
        """提取事件字段，支持多行内容，直到下一个标准字段开始。"""
        if not text:
            return ""
        labels_group = "|".join(re.escape(label) for label in field_labels)
        next_labels_group = (
            r"标题|title|日期|date|客观记录|objective|主观印象|impression|关键词|keywords?|分类|categories?"
        )
        pattern = (
            rf"(?:{labels_group})\s*[:：]\s*"
            rf"(.*?)"
            rf"(?=\n\s*(?:{next_labels_group})\s*[:：]|\Z)"
        )
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        return (match.group(1) if match else "").strip()

    def __init__(
        self,
        config: AppConfig,
        db: Database,
        llm: LLMClient,
        env_gen: EnvironmentGenerator,
        memory: MemoryStore,
        prompt_manager: PromptManager,
        snapshot_llm: LLMClient | None = None,
        automation_engine: AutomationEngine | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.llm = llm
        self.snapshot_llm = snapshot_llm or llm
        self.env_gen = env_gen
        self.memory = memory
        self.prompt_manager = prompt_manager
        self.automation_engine = automation_engine
        self.max_snapshots = config.memory_store.max_snapshots
        self._advance_lock = asyncio.Lock()
        self._maintenance_lock = asyncio.Lock()
        self._deferred_maintenance_queue: list[dict] = []
        self._deferred_maintenance_task: asyncio.Task | None = None
        self._deferred_event_queue: list[dict] = []
        self._deferred_event_task: asyncio.Task | None = None
        self._deferred_event_snapshot_ids: set[int] = set()
        self._env_retry_lock = asyncio.Lock()
        self._deferred_env_retry_queue: list[dict] = []
        self._deferred_env_retry_task: asyncio.Task | None = None
        self.plan_engine = None
        self.memory_summary_engine = None

    def set_plan_engine(self, plan_engine) -> None:
        self.plan_engine = plan_engine

    def set_memory_summary_engine(self, memory_summary_engine) -> None:
        self.memory_summary_engine = memory_summary_engine

    async def _ensure_active_conversation_time_claim(
        self,
        *,
        started_at: datetime,
        latest_snapshot_id: int | None,
        context_summary: str = "",
    ) -> ConversationTimeClaim | None:
        existing = await self._get_effective_active_conversation_time_claim()
        if existing is not None:
            if existing.id is not None:
                fields: dict[str, object] = {
                    "latest_snapshot_id": latest_snapshot_id or existing.latest_snapshot_id,
                    "context_summary": (context_summary or existing.context_summary or "")[:400],
                }
                await self.db.update_conversation_time_claim(int(existing.id), **fields)
                return await self.db.get_conversation_time_claim_by_id(int(existing.id))
            return existing
        claim = ConversationTimeClaim(
            status="active",
            started_at=format_utc_instant_z(shanghai_time_to_utc_naive(started_at)),
            source="get_current_state",
            context_summary=context_summary[:400],
            latest_snapshot_id=latest_snapshot_id,
        )
        claim_id = await self.db.insert_conversation_time_claim(claim)
        return await self.db.get_conversation_time_claim_by_id(int(claim_id))

    async def _get_effective_active_conversation_time_claim(self) -> ConversationTimeClaim | None:
        claim = await self.db.get_active_conversation_time_claim()
        if claim is None or claim.id is None:
            return None
        updated_at = str(claim.updated_at or claim.started_at or "").strip()
        if not updated_at:
            return claim
        try:
            updated_dt = self._parse_iso_datetime(updated_at)
        except Exception:
            return claim
        idle_seconds = (shanghai_now() - updated_dt).total_seconds()
        if idle_seconds <= self.CONVERSATION_CLAIM_IDLE_TIMEOUT_MINUTES * 60:
            return claim
        try:
            await self.db.update_conversation_time_claim(
                int(claim.id),
                status="closed",
                ended_at=format_utc_instant_z(datetime.utcnow()),
                context_summary=(
                    str(claim.context_summary or "").strip()[:700]
                    + f"\n[auto-closed after {self.CONVERSATION_CLAIM_IDLE_TIMEOUT_MINUTES}m idle]"
                )[:800],
            )
            return None
        except Exception:
            logger.exception("Failed to auto-close stale conversation time claim.")
            return claim

    async def _close_active_conversation_time_claim(
        self,
        *,
        ended_at: datetime,
        closing_snapshot_id: int | None,
        context_summary: str,
    ) -> ConversationTimeClaim | None:
        claim = await self._get_effective_active_conversation_time_claim()
        if claim is None or claim.id is None:
            return None
        await self.db.update_conversation_time_claim(
            int(claim.id),
            status="closed",
            ended_at=format_utc_instant_z(shanghai_time_to_utc_naive(ended_at)),
            closing_snapshot_id=closing_snapshot_id,
            context_summary=(context_summary or claim.context_summary or "")[:800],
        )
        return await self.db.get_conversation_time_claim_by_id(int(claim.id))

    async def _append_life_flow_trace(
        self,
        *,
        trace_date: str,
        source: str,
        summary: str,
        details: dict | None = None,
        schedule_alignment: str = "on_track",
        related_snapshot_id: int | None = None,
        related_event_ids: list[int] | None = None,
    ) -> int | None:
        text = str(summary or "").strip()
        if not text:
            return None
        details = dict(details or {})
        details.setdefault("life_theme", self._infer_life_flow_theme(text, details, source=source))
        details.setdefault(
            "has_path_rewrite",
            self._detect_path_rewrite(
                text,
                schedule_alignment=schedule_alignment,
                plan_delta=str(details.get("plan_delta") or ""),
            ),
        )
        keep_mode = self._classify_life_flow_trace_keep_mode(
            text,
            details,
            schedule_alignment=schedule_alignment,
            source=source,
        )
        if keep_mode == "drop":
            return None
        latest = await self.db.get_latest_life_flow_trace_for_date(trace_date)
        if (
            latest is not None
            and latest.id is not None
            and keep_mode == "merge_into_latest_trace"
            and self._should_merge_life_flow_trace(
                latest,
                new_summary=text,
                new_details=details,
                new_schedule_alignment=schedule_alignment,
                new_source=source,
            )
        ):
            merged_summary = self._merge_life_flow_trace_summary(str(latest.summary or ""), text)
            merged_details = self._merge_life_flow_trace_details(latest.details_json, details)
            merged_event_ids = self._merge_related_event_ids(latest.related_event_ids, related_event_ids or [])
            await self.db.update_life_flow_trace(
                int(latest.id),
                summary=merged_summary,
                details_json=json.dumps(merged_details, ensure_ascii=False),
                schedule_alignment=schedule_alignment,
                related_snapshot_id=related_snapshot_id or latest.related_snapshot_id,
                related_event_ids=json.dumps(merged_event_ids, ensure_ascii=False),
            )
            return int(latest.id)
        now = format_utc_instant_z(datetime.utcnow())
        trace = LifeFlowTrace(
            trace_date=trace_date,
            source=source,  # type: ignore[arg-type]
            summary=text,
            details_json=json.dumps(details, ensure_ascii=False),
            schedule_alignment=schedule_alignment,  # type: ignore[arg-type]
            related_snapshot_id=related_snapshot_id,
            related_event_ids=json.dumps(related_event_ids or [], ensure_ascii=False),
            created_at=now,
            updated_at=now,
        )
        return await self.db.insert_life_flow_trace(trace)

    async def _build_recent_life_flow_trace_text(self) -> str:
        now_shanghai = shanghai_now()
        yesterday_str = (now_shanghai.date() - timedelta(days=1)).isoformat()
        traces = await self.db.get_recent_life_flow_traces(
            limit=6,
            start_date=yesterday_str,
            end_date=now_shanghai.date().isoformat(),
        )
        if not traces:
            return "（最近没有额外生活流痕迹）"
        lines: list[str] = []
        for trace in traces[:6]:
            summary = str(trace.summary or "").strip()
            if not summary:
                continue
            lines.append(f"- [{trace.trace_date}] {summary}")
        return "\n".join(lines) if lines else "（最近没有额外生活流痕迹）"

    @staticmethod
    def _stable_unit_interval(seed: str) -> float:
        digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()
        return int(digest[:8], 16) / 0xFFFFFFFF

    @staticmethod
    def _extract_event_description_field(description: str, label: str) -> str:
        text = str(description or "").strip()
        if not text:
            return ""
        pattern = (
            rf"{re.escape(label)}\s*[:：]\s*(.*?)"
            rf"(?=\n\s*(?:客观记录|主观印象|细节钩子|未完成线索)\s*[:：]|\Z)"
        )
        match = re.search(pattern, text, re.DOTALL)
        return (match.group(1) if match else "").strip()

    def _event_open_loop_text(self, event: EventAnchor) -> str:
        return self._extract_event_description_field(str(event.description or ""), "未完成线索")

    def _event_objective_text(self, event: EventAnchor) -> str:
        return self._extract_event_description_field(str(event.description or ""), "客观记录")

    def _event_detail_hooks_text(self, event: EventAnchor) -> str:
        return self._extract_event_description_field(str(event.description or ""), "细节钩子")

    @staticmethod
    def _truncate_text(text: str, limit: int = 180) -> str:
        compact = re.sub(r"\s+", " ", str(text or "").strip())
        return compact[:limit]

    def _format_disturbance_for_prompt(self, pulse: DisturbancePulse) -> str:
        payload = self._parse_life_flow_details(pulse.factual_payload_json)
        visible = self._truncate_text(str(payload.get("visible_manifestation") or payload.get("visible") or ""))
        open_thread = self._truncate_text(str(payload.get("open_thread") or ""))
        parts = [
            f"- [{pulse.channel_type}/{pulse.source_family}] {str(pulse.title or '').strip() or '(untitled disturbance)'}",
        ]
        if visible:
            parts.append(f"  Visible: {visible}")
        if open_thread:
            parts.append(f"  Open thread: {open_thread}")
        return "\n".join(parts)

    def _format_recent_disturbances_text(self, pulses: list[DisturbancePulse]) -> str:
        if not pulses:
            return "(no recent disturbances)"
        return "\n".join(
            self._format_disturbance_for_prompt(pulse)
            for pulse in pulses[: self.DISTURBANCE_RECENT_LIMIT]
        )

    async def _list_recent_disturbances(self, *, statuses: list[str] | None = None) -> list[DisturbancePulse]:
        return await self.db.get_recent_disturbance_pulses(
            limit=self.DISTURBANCE_RECENT_LIMIT,
            statuses=statuses or ["injected", "consumed"],
        )

    def _build_recent_open_loops_text(
        self,
        recent_events: list[EventAnchor],
        recent_trace_summary: str,
        plan_delta: str,
    ) -> str:
        lines: list[str] = []
        for event in recent_events[:4]:
            open_loop = self._event_open_loop_text(event)
            if not open_loop:
                continue
            title = str(event.title or "").strip() or self._truncate_text(self._event_objective_text(event), 80)
            lines.append(f"- [{event.date}] {title}: {self._truncate_text(open_loop, 120)}")
        if recent_trace_summary and "no extra" not in recent_trace_summary.lower():
            lines.append(f"- Recent trace: {self._truncate_text(recent_trace_summary, 120)}")
        if plan_delta:
            lines.append(f"- Plan delta carry-over: {self._truncate_text(plan_delta, 120)}")
        return "\n".join(lines) if lines else "(no strong open loops)"

    def _build_disturbance_fingerprint(self, payload: dict) -> str:
        source_family = str(payload.get("source_family") or "")
        seed_kind = str(payload.get("seed_kind") or "")
        seed_ref_id = str(payload.get("seed_ref_id") or "")
        title = self._truncate_text(str(payload.get("title") or ""), 80)
        channel_type = str(payload.get("channel_type") or "")
        base = "|".join([channel_type, source_family, seed_kind, seed_ref_id, title])
        return hashlib.sha1(base.encode("utf-8")).hexdigest()[:20]

    def _recent_external_share(self, pulses: list[DisturbancePulse]) -> float:
        if not pulses:
            return 0.0
        external = sum(1 for pulse in pulses if str(pulse.channel_type or "") == "external_incident")
        return external / max(len(pulses), 1)

    def _disturbance_overlap_penalty(self, candidate: dict, recent: list[DisturbancePulse]) -> float:
        fingerprint = str(candidate.get("fingerprint") or "")
        source_family = str(candidate.get("source_family") or "")
        penalty = 0.0
        for pulse in recent:
            if fingerprint and fingerprint == str(pulse.fingerprint or ""):
                penalty += 0.8
            elif source_family and source_family == str(pulse.source_family or ""):
                penalty += 0.15
        return min(penalty, 1.0)

    def _score_disturbance_candidate(self, candidate: dict, recent_pulses: list[DisturbancePulse]) -> dict:
        continuity = float(candidate.get("continuity_score") or 0.0)
        pressure = float(candidate.get("pressure_score") or 0.0)
        novelty = float(candidate.get("novelty_score") or 0.0)
        timing = float(candidate.get("timing_score") or 0.0)
        intrusion_cost = float(candidate.get("intrusion_cost") or 0.0)
        blindness = float(candidate.get("blindness_score") or 0.0)
        world_relevance = float(candidate.get("world_relevance_score") or 0.0)
        plausibility = float(candidate.get("plausibility_score") or 0.0)
        overlap_penalty = self._disturbance_overlap_penalty(candidate, recent_pulses)
        total = continuity + pressure + novelty + timing - intrusion_cost - overlap_penalty
        if str(candidate.get("channel_type") or "") == "endogenous_reveal":
            total += blindness
        else:
            total += world_relevance + plausibility
            external_share = self._recent_external_share(recent_pulses)
            if external_share >= self.DISTURBANCE_EXTERNAL_TARGET_SHARE:
                total -= 0.25
            if external_share >= self.DISTURBANCE_EXTERNAL_MAX_SHARE:
                total -= 0.45
        candidate["score"] = round(total, 4)
        return candidate

    def _normalize_disturbance_candidate(self, payload: dict) -> dict:
        normalized = dict(payload)
        normalized["title"] = self._truncate_text(str(payload.get("title") or ""), 120)
        normalized["impact_hint"] = self._truncate_text(str(payload.get("impact_hint") or ""), 180)
        normalized["visible_manifestation"] = self._truncate_text(str(payload.get("visible_manifestation") or ""), 180)
        normalized["open_thread"] = self._truncate_text(str(payload.get("open_thread") or ""), 180)
        normalized["detail_hook"] = self._truncate_text(str(payload.get("detail_hook") or ""), 120)
        normalized["fingerprint"] = self._build_disturbance_fingerprint(normalized)
        return normalized

    @staticmethod
    def _parse_life_flow_details(raw: str | None) -> dict:
        try:
            data = json.loads(str(raw or "").strip() or "{}")
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _build_external_disturbance_candidates(
        self,
        *,
        checkpoint_time: datetime,
        current_plan_summary: str,
        current_plan_activity: str,
        world_book_entries: list[dict],
        recent_events: list[EventAnchor],
    ) -> list[dict]:
        candidates: list[dict] = []
        plan_blob = "\n".join([current_plan_summary, current_plan_activity]).strip()
        if plan_blob:
            candidates.append(
                self._normalize_disturbance_candidate(
                    {
                        "channel_type": "external_incident",
                        "source_family": "task",
                        "seed_kind": "world_state",
                        "seed_ref_id": 0,
                        "blind_spot_reason": "deferred",
                        "reveal_channel": "schedule_conflict",
                        "title": "上游任务链路出现计划外变更",
                        "what_changed": "当前任务所依附的外部协作或行政链路出现了未预先纳入本轮节奏的变化。",
                        "why_now": "原本按部就班的推进在这个 checkpoint 撞上了新的外部要求。",
                        "visible_manifestation": "计划顺序需要被重新确认，原定动作被迫让位或延后。",
                        "impact_hint": "这会对当前生活流造成轻到中度的计划外插入。",
                        "detail_hook": "终端上的待处理项突然多出一条未在原计划里的要求",
                        "open_thread": "需要判断这条外部要求是否真的值得优先处理。",
                        "continuity_score": 0.6,
                        "pressure_score": 0.55,
                        "novelty_score": 0.48,
                        "timing_score": 0.5 if checkpoint_time.hour >= 10 else 0.36,
                        "intrusion_cost": 0.2,
                        "world_relevance_score": 0.52,
                        "plausibility_score": 0.58,
                    }
                )
            )
        if world_book_entries:
            top_entry = world_book_entries[0]
            entry_name = str(top_entry.get("name") or "").strip() or "当前环境"
            candidates.append(
                self._normalize_disturbance_candidate(
                    {
                        "channel_type": "external_incident",
                        "source_family": "environment",
                        "seed_kind": "world_state",
                        "seed_ref_id": int(top_entry.get("id") or 0) or None,
                        "blind_spot_reason": "ambient_accumulation",
                        "reveal_channel": "ambient_shift",
                        "title": f"{entry_name} 的外部波动开始压进当下场景",
                        "what_changed": f"与 {entry_name} 相关的外部条件出现了实际变化，并开始影响当前位置或手头安排。",
                        "why_now": "之前只是背景信息，到这个 checkpoint 才转化为不得不处理的现实摩擦。",
                        "visible_manifestation": "环境条件、空间使用、通行或工作手感突然变得不再顺滑。",
                        "impact_hint": "这更像世界本身主动逼近，而不是角色主观情绪的放大。",
                        "detail_hook": f"与 {entry_name} 相关的提示信息在屏幕边缘弹出",
                        "open_thread": "需要判断这次外界波动是短噪声还是会继续扩大。",
                        "continuity_score": 0.54,
                        "pressure_score": 0.44,
                        "novelty_score": 0.46,
                        "timing_score": 0.42,
                        "intrusion_cost": 0.26,
                        "world_relevance_score": 0.6,
                        "plausibility_score": 0.62,
                    }
                )
            )
        if recent_events:
            last_event = recent_events[0]
            event_title = str(last_event.title or "").strip() or "近期事务"
            candidates.append(
                self._normalize_disturbance_candidate(
                    {
                        "channel_type": "external_incident",
                        "source_family": "device",
                        "seed_kind": "event",
                        "seed_ref_id": int(last_event.id or 0) or None,
                        "blind_spot_reason": "not_checked",
                        "reveal_channel": "device_alert",
                        "title": f"与“{event_title}”有关的新提醒突然进入前台",
                        "what_changed": "外部信息链并未停止，只是此前没有被拉到前台。",
                        "why_now": "到这个 checkpoint，积压的提示或回执终于顶到了注意力前沿。",
                        "visible_manifestation": "一条带有具体对象或进度信号的新提醒打断了当前节奏。",
                        "impact_hint": "这会迫使角色重新评估手头事务的轻重。",
                        "detail_hook": "消息提示在终端右上角停了一瞬",
                        "open_thread": "需要判断这条提醒是立即处理还是暂时压后。",
                        "continuity_score": 0.51,
                        "pressure_score": 0.4,
                        "novelty_score": 0.45,
                        "timing_score": 0.45,
                        "intrusion_cost": 0.22,
                        "world_relevance_score": 0.5,
                        "plausibility_score": 0.56,
                    }
                )
            )
        return candidates

    async def _build_disturbance_candidates(
        self,
        *,
        checkpoint_time: datetime,
        recent_events: list[EventAnchor],
        recent_key_records: list[KeyRecord],
        current_plan_summary: str,
        current_plan_activity: str,
        recent_trace_summary: str,
        plan_delta: str,
        world_book_entries: list[dict],
    ) -> list[dict]:
        candidates: list[dict] = []
        for event in recent_events[:4]:
            open_loop = self._event_open_loop_text(event)
            if not open_loop:
                continue
            objective = self._event_objective_text(event) or str(event.title or "").strip()
            candidates.append(
                self._normalize_disturbance_candidate(
                    {
                        "channel_type": "endogenous_reveal",
                        "source_family": "relationship" if any(token in open_loop for token in ("回复", "联系", "误解", "牵挂")) else "task",
                        "seed_kind": "event",
                        "seed_ref_id": int(event.id or 0) or None,
                        "blind_spot_reason": "deferred",
                        "reveal_channel": "message" if "回复" in open_loop or "消息" in open_loop else "schedule_conflict",
                        "title": str(event.title or "").strip() or self._truncate_text(objective, 60),
                        "what_changed": objective,
                        "why_now": "之前只是未闭合线索，到这个 checkpoint 已经开始反过来影响当下节奏。",
                        "visible_manifestation": open_loop,
                        "impact_hint": "旧线索不再只是背景，而开始占用现实注意力。",
                        "detail_hook": self._event_detail_hooks_text(event) or "那个未处理的问题重新顶到前台",
                        "open_thread": open_loop,
                        "continuity_score": 0.72,
                        "pressure_score": 0.56,
                        "novelty_score": 0.52,
                        "timing_score": 0.48,
                        "intrusion_cost": 0.12,
                        "blindness_score": 0.22,
                    }
                )
            )
        if recent_trace_summary and "no extra" not in recent_trace_summary.lower():
            candidates.append(
                self._normalize_disturbance_candidate(
                    {
                        "channel_type": "endogenous_reveal",
                        "source_family": "task",
                        "seed_kind": "life_flow",
                        "seed_ref_id": 0,
                        "blind_spot_reason": "ambient_accumulation",
                        "reveal_channel": "schedule_conflict",
                        "title": "既有生活路径的偏移开始显出实际代价",
                        "what_changed": recent_trace_summary,
                        "why_now": "此前只是轻微偏斜的路径，在当前 checkpoint 开始转化为必须处理的现实摩擦。",
                        "visible_manifestation": plan_delta or recent_trace_summary,
                        "impact_hint": "这会促使她重新排列当下优先级。",
                        "detail_hook": "原本顺滑的节奏在某个动作上短暂停了一下",
                        "open_thread": plan_delta or "这次路径偏移是否需要真正改写后续安排，仍未定。",
                        "continuity_score": 0.68,
                        "pressure_score": 0.5,
                        "novelty_score": 0.45,
                        "timing_score": 0.46,
                        "intrusion_cost": 0.15,
                        "blindness_score": 0.18,
                    }
                )
            )
        if self.plan_engine is not None:
            plan = await self.plan_engine.get_current_plan()
            if plan is not None and plan.id is not None:
                items = await self.db.list_plan_items(int(plan.id), status="pending")
                for item in items[:4]:
                    if checkpoint_time.hour < int(item.hour_end):
                        continue
                    candidates.append(
                        self._normalize_disturbance_candidate(
                            {
                                "channel_type": "endogenous_reveal",
                                "source_family": "task",
                                "seed_kind": "plan_item",
                                "seed_ref_id": int(item.id or 0) or None,
                                "blind_spot_reason": "not_checked",
                                "reveal_channel": "schedule_conflict",
                                "title": f"未完成计划项“{item.activity}”开始反咬当前节奏",
                                "what_changed": f"原定在 {item.hour_start:02d}:00-{item.hour_end:02d}:00 处理的事项仍悬而未决。",
                                "why_now": "时间窗已经过去，未执行状态不再是抽象记录，而转化为当前压力。",
                                "visible_manifestation": f"计划项 {item.activity} 形成了实际的后压。",
                                "impact_hint": "这更像节奏债务，而不是新的外部任务。",
                                "detail_hook": f"{item.activity} 仍停在待办列表里",
                                "open_thread": "要继续拖延，还是正式调整计划，必须尽快决定。",
                                "continuity_score": 0.7,
                                "pressure_score": 0.62,
                                "novelty_score": 0.44,
                                "timing_score": 0.58,
                                "intrusion_cost": 0.1,
                                "blindness_score": 0.24,
                            }
                        )
                    )
        for record in recent_key_records[:3]:
            normalized_type = self._normalize_key_record_type(str(record.type or ""))
            if normalized_type not in {"health_monitoring", "medication_protocol"}:
                continue
            content_text = str(record.content_text or "").strip()
            candidates.append(
                self._normalize_disturbance_candidate(
                    {
                        "channel_type": "endogenous_reveal",
                        "source_family": "body",
                        "seed_kind": "key_record",
                        "seed_ref_id": int(record.id or 0) or None,
                        "blind_spot_reason": "ambient_accumulation",
                        "reveal_channel": "body_signal",
                        "title": str(record.title or "").strip() or "身体负荷开始显形",
                        "what_changed": content_text or "近期健康监测与照护线索并未真正消失。",
                        "why_now": "原本只被压在背景里的身体负荷，到这个 checkpoint 开始以具体感受进入前台。",
                        "visible_manifestation": "轻微但真实的身体信号开始干扰判断和节奏。",
                        "impact_hint": "这不是纯情绪波动，而是身体条件在要求被重新计入。",
                        "detail_hook": "某个细小但持续的身体反馈没有像预想中那样退下去",
                        "open_thread": "需要判断它只是短暂波动，还是应转化为明确应对。",
                        "continuity_score": 0.66,
                        "pressure_score": 0.51,
                        "novelty_score": 0.43,
                        "timing_score": 0.44,
                        "intrusion_cost": 0.11,
                        "blindness_score": 0.23,
                    }
                )
            )
        npcs = await self.db.list_npc_entities(status="active", limit=4)
        for npc in npcs[:2]:
            if not str(npc.last_interaction_at or "").strip():
                continue
            candidates.append(
                self._normalize_disturbance_candidate(
                    {
                        "channel_type": "endogenous_reveal",
                        "source_family": "npc",
                        "seed_kind": "npc",
                        "seed_ref_id": int(npc.id or 0) or None,
                        "blind_spot_reason": "misread",
                        "reveal_channel": "third_party",
                        "title": f"{npc.name} 的迟到反应开始进入当前场景",
                        "what_changed": f"{npc.name} 与既有线路的关联并未结束，只是此前尚未显形。",
                        "why_now": "到这个 checkpoint，对方的行动、等待或反应开始逼近当前生活流。",
                        "visible_manifestation": f"与 {npc.name} 有关的后续动作不再适合继续搁置。",
                        "impact_hint": "这是人物网络自己回流出来的压力。",
                        "detail_hook": f"{npc.name} 的名字突然重新进入注意力范围",
                        "open_thread": f"她需要判断是否主动回应 {npc.name} 的这条后续线索。",
                        "continuity_score": 0.55,
                        "pressure_score": 0.38,
                        "novelty_score": 0.4,
                        "timing_score": 0.35,
                        "intrusion_cost": 0.16,
                        "blindness_score": 0.16,
                    }
                )
            )
        candidates.extend(
            self._build_external_disturbance_candidates(
                checkpoint_time=checkpoint_time,
                current_plan_summary=current_plan_summary,
                current_plan_activity=current_plan_activity,
                world_book_entries=world_book_entries,
                recent_events=recent_events,
            )
        )
        return candidates

    async def _judge_disturbance_candidate(
        self,
        *,
        checkpoint_time: datetime,
        snapshot_excerpt: str,
        plan_context: str,
        recent_trace_summary: str,
        schedule_alignment: str,
        plan_delta: str,
        recent_open_loops: str,
        candidates: list[dict],
        recent_disturbances_text: str,
    ) -> dict:
        fallback = {"should_inject": False}
        if not candidates:
            return fallback
        prompt_template = await self.prompt_manager.get_prompt(KEY_PROMPT_DISTURBANCE_JUDGE)
        if not prompt_template.strip():
            return fallback
        system_prompt = await self.prompt_manager.get_system_prompt()
        try:
            response = await self.snapshot_llm.chat(
                [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": prompt_template.format(
                            checkpoint_time=utc_naive_to_shanghai_iso(checkpoint_time),
                            snapshot_excerpt=snapshot_excerpt[:1200],
                            plan_context=plan_context[:1200],
                            recent_trace_summary=recent_trace_summary[:1200],
                            schedule_alignment=schedule_alignment or "on_track",
                            plan_delta=plan_delta[:500],
                            recent_open_loops=recent_open_loops[:1000],
                            candidate_disturbances=json.dumps(candidates, ensure_ascii=False, indent=2),
                            recent_disturbances=recent_disturbances_text[:1200],
                        ),
                    },
                ],
                max_tokens=500,
            )
            parsed = self._extract_json_object(response)
            if not isinstance(parsed, dict):
                return fallback
            parsed["should_inject"] = bool(parsed.get("should_inject"))
            return parsed
        except Exception:
            return fallback

    async def _materialize_disturbance_payload(
        self,
        *,
        checkpoint_time: datetime,
        candidate: dict,
        judge_reason: str,
        plan_context: str,
        recent_trace_summary: str,
    ) -> dict:
        fallback = {
            "title": str(candidate.get("title") or "").strip(),
            "channel_type": str(candidate.get("channel_type") or "").strip(),
            "what_changed": str(candidate.get("what_changed") or "").strip(),
            "why_now": str(candidate.get("why_now") or "").strip(),
            "visible_manifestation": str(candidate.get("visible_manifestation") or "").strip(),
            "immediate_pressure": str(candidate.get("impact_hint") or "").strip(),
            "detail_hook": str(candidate.get("detail_hook") or "").strip(),
            "open_thread": str(candidate.get("open_thread") or judge_reason or "").strip(),
        }
        prompt_template = await self.prompt_manager.get_prompt(KEY_PROMPT_DISTURBANCE_MATERIALIZE)
        if not prompt_template.strip():
            return fallback
        system_prompt = await self.prompt_manager.get_system_prompt()
        try:
            response = await self.snapshot_llm.chat(
                [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": prompt_template.format(
                            checkpoint_time=utc_naive_to_shanghai_iso(checkpoint_time),
                            selected_candidate=json.dumps(candidate, ensure_ascii=False, indent=2),
                            judge_reason=judge_reason,
                            plan_context=plan_context[:1200],
                            recent_trace_summary=recent_trace_summary[:1200],
                        ),
                    },
                ],
                max_tokens=450,
            )
            parsed: dict[str, str] = {}
            for raw_line in str(response or "").splitlines():
                line = raw_line.strip()
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                parsed[key.strip().lower()] = value.strip()
            return {
                "title": parsed.get("title") or fallback["title"],
                "channel_type": parsed.get("channel type") or fallback["channel_type"],
                "what_changed": parsed.get("what was already brewing / what changed outside") or fallback["what_changed"],
                "why_now": parsed.get("why it surfaced now") or fallback["why_now"],
                "visible_manifestation": parsed.get("visible manifestation") or fallback["visible_manifestation"],
                "immediate_pressure": parsed.get("immediate pressure on current flow") or fallback["immediate_pressure"],
                "detail_hook": parsed.get("suggested detail hook") or fallback["detail_hook"],
                "open_thread": parsed.get("open thread") or fallback["open_thread"],
            }
        except Exception:
            return fallback

    async def _maybe_inject_disturbance(
        self,
        *,
        checkpoint_time: datetime,
        current_content: str,
        previous_env: dict | None,
        environment_context_details: dict,
        recent_events: list[EventAnchor],
        recent_key_records: list[KeyRecord],
        world_book_entries: list[dict],
    ) -> dict:
        del previous_env
        recent_pulses = await self._list_recent_disturbances()
        recent_disturbances_text = self._format_recent_disturbances_text(recent_pulses)
        current_plan_summary = str(environment_context_details.get("current_plan_summary") or "").strip()
        current_plan_activity = str(environment_context_details.get("current_plan_activity") or "").strip()
        recent_trace_summary = str(environment_context_details.get("recent_trace_summary") or "").strip()
        schedule_alignment = str(environment_context_details.get("schedule_alignment") or "").strip()
        plan_delta = str(environment_context_details.get("plan_delta") or "").strip()
        recent_open_loops = self._build_recent_open_loops_text(recent_events, recent_trace_summary, plan_delta)
        candidates = await self._build_disturbance_candidates(
            checkpoint_time=checkpoint_time,
            recent_events=recent_events,
            recent_key_records=recent_key_records,
            current_plan_summary=current_plan_summary,
            current_plan_activity=current_plan_activity,
            recent_trace_summary=recent_trace_summary,
            plan_delta=plan_delta,
            world_book_entries=world_book_entries,
        )
        scored = [self._score_disturbance_candidate(candidate, recent_pulses) for candidate in candidates]
        scored = [candidate for candidate in scored if float(candidate.get("score") or 0.0) >= 1.65]
        scored.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
        shortlist = scored[:3]
        if not shortlist:
            return {"should_inject": False, "recent_disturbances_text": recent_disturbances_text}
        plan_context = "\n".join(
            [
                current_plan_summary or "(no current plan summary)",
                current_plan_activity or "(no current plan activity)",
            ]
        ).strip()
        judgment = await self._judge_disturbance_candidate(
            checkpoint_time=checkpoint_time,
            snapshot_excerpt=current_content,
            plan_context=plan_context,
            recent_trace_summary=recent_trace_summary,
            schedule_alignment=schedule_alignment,
            plan_delta=plan_delta,
            recent_open_loops=recent_open_loops,
            candidates=shortlist,
            recent_disturbances_text=recent_disturbances_text,
        )
        selected_fp = str(judgment.get("selected_fingerprint") or "").strip()
        selected = next(
            (candidate for candidate in shortlist if selected_fp and selected_fp == str(candidate.get("fingerprint") or "")),
            None,
        )
        if selected is None:
            selected = shortlist[0]
            seed_value = self._stable_unit_interval(f"{selected.get('fingerprint')}|{checkpoint_time.isoformat()}")
            if seed_value > min(0.92, 0.45 + float(selected.get("score") or 0.0) / 4.0):
                return {"should_inject": False, "recent_disturbances_text": recent_disturbances_text}
        if not judgment.get("should_inject") and selected_fp:
            return {"should_inject": False, "recent_disturbances_text": recent_disturbances_text}
        materialized = await self._materialize_disturbance_payload(
            checkpoint_time=checkpoint_time,
            candidate=selected,
            judge_reason=str(judgment.get("reason") or "").strip(),
            plan_context=plan_context,
            recent_trace_summary=recent_trace_summary,
        )
        now = format_utc_instant_z(datetime.utcnow())
        pulse_payload = {
            "what_changed": materialized.get("what_changed") or selected.get("what_changed") or "",
            "why_now": materialized.get("why_now") or selected.get("why_now") or "",
            "visible_manifestation": materialized.get("visible_manifestation") or selected.get("visible_manifestation") or "",
            "immediate_pressure": materialized.get("immediate_pressure") or selected.get("impact_hint") or "",
            "detail_hook": materialized.get("detail_hook") or selected.get("detail_hook") or "",
            "open_thread": materialized.get("open_thread") or selected.get("open_thread") or "",
            "reveal_focus": str(judgment.get("reveal_focus") or "").strip(),
            "schedule_effect": str(judgment.get("schedule_effect") or "none").strip() or "none",
        }
        pulse = DisturbancePulse(
            occur_at=now,
            reveal_at=format_utc_instant_z(shanghai_time_to_utc_naive(checkpoint_time)),
            status="injected",
            channel_type=str(judgment.get("channel_type") or selected.get("channel_type") or "endogenous_reveal"),
            source_family=str(selected.get("source_family") or "task"),
            seed_kind=str(selected.get("seed_kind") or "event"),
            seed_ref_id=selected.get("seed_ref_id"),
            blind_spot_reason=str(selected.get("blind_spot_reason") or ""),
            reveal_channel=str(selected.get("reveal_channel") or ""),
            title=str(materialized.get("title") or selected.get("title") or "").strip(),
            factual_payload_json=json.dumps(pulse_payload, ensure_ascii=False),
            impact_hint=str(materialized.get("immediate_pressure") or selected.get("impact_hint") or ""),
            salience=max(0.1, min(float(selected.get("score") or 0.5) / 3.0, 1.0)),
            novelty_score=max(0.1, min(float(selected.get("novelty_score") or 0.5), 1.0)),
            cooldown_until=format_utc_instant_z(shanghai_time_to_utc_naive(checkpoint_time + timedelta(hours=8))),
            fingerprint=str(selected.get("fingerprint") or ""),
            created_at=now,
            updated_at=now,
        )
        pulse_id = await self.db.insert_disturbance_pulse(pulse)
        disturbance_context = (
            f"[{pulse.channel_type}/{pulse.source_family}] {pulse.title}\n"
            f"Visible manifestation: {self._truncate_text(str(pulse_payload.get('visible_manifestation') or ''), 180)}\n"
            f"Immediate pressure: {self._truncate_text(str(pulse_payload.get('immediate_pressure') or ''), 180)}\n"
            f"Detail hook: {self._truncate_text(str(pulse_payload.get('detail_hook') or ''), 120)}\n"
            f"Open thread: {self._truncate_text(str(pulse_payload.get('open_thread') or ''), 160)}"
        )
        schedule_effect = str(judgment.get("schedule_effect") or pulse_payload.get("schedule_effect") or "none")
        plan_delta_patch = f"Disturbance: {pulse.title} | {self._truncate_text(str(pulse_payload.get('immediate_pressure') or ''), 120)}"
        new_recent_pulses = [DisturbancePulse(id=pulse_id, **pulse.model_dump(exclude={"id"}))] + recent_pulses
        return {
            "should_inject": True,
            "disturbance_id": int(pulse_id),
            "disturbance_payload": pulse_payload,
            "disturbance_title": pulse.title,
            "disturbance_context": disturbance_context,
            "disturbance_schedule_effect": schedule_effect,
            "plan_delta_patch": plan_delta_patch,
            "detail_hook": str(pulse_payload.get("detail_hook") or ""),
            "open_thread": str(pulse_payload.get("open_thread") or ""),
            "recent_disturbances_text": self._format_recent_disturbances_text(new_recent_pulses[: self.DISTURBANCE_RECENT_LIMIT]),
            "channel_type": pulse.channel_type,
        }

    @staticmethod
    def _clamp_unit(value: float) -> float:
        return max(0.0, min(round(float(value), 4), 1.0))

    async def _get_or_create_relationship_state(self) -> RelationshipState:
        state = await self.db.get_latest_relationship_state()
        if state is not None:
            return state
        now = format_utc_instant_z(datetime.utcnow())
        state = RelationshipState(
            last_meaningful_contact_at=now,
            hours_since_meaningful_contact=0.0,
            days_since_meaningful_contact=0,
            contact_recency_bucket="active",
            relationship_feeling_summary="最近仍保持着可感知的联结，关系影响清晰但并不压倒生活本身。",
            proactive_topics=json.dumps([], ensure_ascii=False),
            plan_bias_hint="今日节律可按角色自身生活推进，不预设与用户的共同行动。",
            created_at=now,
            updated_at=now,
        )
        state_id = await self.db.insert_relationship_state(state)
        latest = await self.db.get_latest_relationship_state()
        if latest is not None:
            return latest
        state.id = int(state_id)
        return state

    @staticmethod
    def _relationship_bucket(hours_since: float) -> str:
        if hours_since <= 24:
            return "active"
        if hours_since <= 72:
            return "cooling"
        if hours_since <= 168:
            return "distant"
        return "stale"

    async def _refresh_relationship_state(
        self,
        *,
        conversation_summary: str = "",
        latest_snapshot_text: str = "",
    ) -> RelationshipState:
        state = await self._get_or_create_relationship_state()
        now = shanghai_now()
        last_contact_dt = None
        if str(state.last_meaningful_contact_at or "").strip():
            try:
                last_contact_dt = self._parse_iso_datetime(str(state.last_meaningful_contact_at))
            except Exception:
                last_contact_dt = None
        if str(conversation_summary or "").strip():
            last_contact_dt = now
        hours_since = 0.0
        if last_contact_dt is not None:
            hours_since = max(0.0, (now - last_contact_dt).total_seconds() / 3600.0)
        days_since = max(0, int(hours_since // 24))
        connection_need = 0.35 + min(hours_since, 240.0) * 0.0021
        pride_or_distance = 0.55 if hours_since >= 72 else 0.42
        concern_level = min(0.35 + hours_since * 0.0025, 0.92)
        space_need_level = 0.42
        valence = max(0.18, 0.68 - hours_since * 0.0017)
        arousal = min(0.82, 0.38 + hours_since * 0.0021)
        life_immersion = min(0.88, 0.45 + hours_since * 0.0017)
        topics: list[str] = []
        summary = "关系仍在近场影响中，联结感明确。"
        plan_bias_hint = "今日节律仍可按角色自身安排推进，同时保留少量关系层面的注意力余波。"
        bucket = self._relationship_bucket(hours_since)

        if bucket == "active":
            summary = "最近联系仍近，关系的余温仍在，注意力容易回到刚刚发生的互动与未说尽的话。"
            topics = ["延续上次未说完的话题", "确认对方当前状态", "轻轻追问仍悬而未决的线索"]
            plan_bias_hint = "今日更容易被近期互动牵引，但不应预设与用户的共同行动。"
        elif bucket == "cooling":
            summary = "联系刚刚拉开一点距离，关系仍在心里，但会更多转化为留意、回想与判断是否适合靠近。"
            topics = ["询问近况", "回收上次对话留下的线索", "确认是否需要空间或支持"]
            plan_bias_hint = "今日更适合维持自己的生活节律，同时为可能的再次对话留出一点弹性。"
        elif bucket == "distant":
            summary = "已有一段时间没有实质联系，关系感开始转为更明显的想念、担忧或克制的观察。"
            topics = ["确认近况是否安稳", "提起仍在意的旧线索", "在不逼近的前提下表达关心"]
            plan_bias_hint = "今日更适合沉入自身事务，但情绪与注意力可能间歇性回到这段关系。"
            space_need_level = 0.5
        else:
            summary = "联系已经明显疏远，关系不至于消失，但它会以更压缩、更隐性的方式影响情绪与节律。"
            topics = ["若重新开启对话，可先从近况与低压话题入手", "避免预设亲密互动", "优先确认彼此当下是否有谈话空间"]
            plan_bias_hint = "今日应以角色自身生活为主，仅保留非常轻的关系感知背景。"
            pride_or_distance = 0.64
            life_immersion = 0.74

        thought_items = await self.db.list_relationship_thoughts(
            thought_date=now.date().isoformat(),
            resolution_status="open",
            limit=12,
        )
        conv_text = str(conversation_summary or "").strip()
        if conv_text:
            lowered = conv_text.lower()
            if any(word in conv_text for word in ("承诺", "共识", "确认", "决定", "原则")):
                valence += 0.08
                connection_need -= 0.08
                summary = "最近的对话带来了更清楚的关系确认，短期内联结感更稳，靠近的意愿也更明确。"
                topics = ["承接刚刚形成的共识", "确认落实方式", "继续推进已经打开的话题"]
                plan_bias_hint = "今日情绪更稳，但仍应让后续安排以角色自身事务为主，不将用户写入日程。"
            elif any(word in conv_text for word in ("害怕", "担心", "不安", "想念", "舍不得", "难过")):
                concern_level += 0.1
                arousal += 0.06
                connection_need += 0.06
                summary = "最近的对话留下了明显的情绪余波，关系感更敏感，也更容易引出牵挂或想确认近况的冲动。"
                topics = ["确认对方是否安好", "回应刚刚出现的情绪线索", "温和延续尚未安放的感受"]
            elif any(word in lowered for word in ("空间", "暂停", "冷静", "之后再说")):
                space_need_level += 0.18
                pride_or_distance += 0.08
                summary = "最近的对话提示这段关系暂时更需要边界与空间，靠近的意愿仍在，但表达方式应更克制。"
                topics = ["低压力确认", "尊重空间的前提下留一条可回来的线", "避免逼近式推进"]

        if thought_items:
            thought_topics: list[str] = []
            for thought in thought_items:
                content = self._compact_structured_memory_text(str(thought.content or "").strip())
                if not content:
                    continue
                if any(self._keyword_overlap_score(content.lower(), existing.lower()) >= 0.8 for existing in thought_topics):
                    continue
                thought_topics.append(content[:120])
            if thought_topics:
                topics = thought_topics[:4]

        connection_need = self._clamp_unit(connection_need)
        pride_or_distance = self._clamp_unit(pride_or_distance)
        concern_level = self._clamp_unit(concern_level)
        space_need_level = self._clamp_unit(space_need_level)
        valence = self._clamp_unit(valence)
        arousal = self._clamp_unit(arousal)
        life_immersion = self._clamp_unit(life_immersion)

        payload = {
            "last_meaningful_contact_at": format_utc_instant_z(shanghai_time_to_utc_naive(last_contact_dt or now)),
            "hours_since_meaningful_contact": round(hours_since, 1),
            "days_since_meaningful_contact": days_since,
            "contact_recency_bucket": bucket,
            "connection_need": connection_need,
            "pride_or_distance": pride_or_distance,
            "valence": valence,
            "arousal": arousal,
            "life_immersion": life_immersion,
            "relationship_feeling_summary": summary,
            "space_need_level": space_need_level,
            "concern_level": concern_level,
            "proactive_topics": json.dumps(topics[:3], ensure_ascii=False),
            "plan_bias_hint": plan_bias_hint,
        }
        if state.id is not None:
            await self.db.update_relationship_state(int(state.id), **payload)
        else:
            created = RelationshipState(**payload)
            await self.db.insert_relationship_state(created)
        latest = await self.db.get_latest_relationship_state()
        return latest or RelationshipState(**payload)

    def _infer_life_flow_theme(self, summary: str, details: dict | None = None, *, source: str = "environment") -> str:
        text = "\n".join(
            [
                str(summary or ""),
                json.dumps(details or {}, ensure_ascii=False),
                str(source or ""),
            ]
        )
        if any(word in text for word in ("对话", "关系", "承诺", "约定", "共识", "陪着", "理解", "害怕被忘记")):
            return "relationship"
        if any(word in text for word in ("复诊", "诊断", "用药", "剂量", "症状", "炎症", "吸入", "指标")):
            return "medical"
        if any(word in text for word in ("课程", "上课", "学习", "作业", "复习", "论文")):
            return "study"
        if any(word in text for word in ("工作", "项目", "会议", "任务", "文件", "排班", "研究")):
            return "work"
        if any(word in text for word in ("出行", "通勤", "搬家", "路上", "外出")):
            return "mobility"
        if any(word in text for word in ("作息", "吃饭", "睡觉", "洗漱", "休息", "节律")):
            return "routine"
        if source == "conversation":
            return "conversation"
        return "general"

    @staticmethod
    def _detect_path_rewrite(text: str, *, schedule_alignment: str = "", plan_delta: str = "") -> bool:
        alignment = str(schedule_alignment or "").strip()
        delta_text = f"{text}\n{plan_delta}".strip()
        if alignment in {"delayed", "interrupted", "replaced_by_conversation", "cancelled", "unexpected_inserted"}:
            return True
        rewrite_words = ("改写", "推迟", "延后", "取消", "替换", "中断", "转为", "改成", "重排", "重新安排")
        return any(word in delta_text for word in rewrite_words)

    def _classify_life_flow_trace_keep_mode(
        self,
        summary: str,
        details: dict,
        *,
        schedule_alignment: str,
        source: str,
    ) -> str:
        text = str(summary or "").strip()
        if not text:
            return "drop"
        if source == "conversation":
            return "append_new_trace"
        if self._detect_path_rewrite(text, schedule_alignment=schedule_alignment, plan_delta=str(details.get("plan_delta") or "")):
            return "append_new_trace"
        if any(word in text for word in ("未完成", "留白", "中断", "打断", "改为", "重新", "决定", "承诺", "约定")):
            return "append_new_trace"
        theme = str(details.get("life_theme") or "")
        if theme in {"routine", "general"} and len(text) < 18:
            return "drop"
        return "merge_into_latest_trace"

    def _should_merge_life_flow_trace(
        self,
        latest: LifeFlowTrace,
        *,
        new_summary: str,
        new_details: dict,
        new_schedule_alignment: str,
        new_source: str,
    ) -> bool:
        latest_details = self._parse_life_flow_details(latest.details_json)
        if str(latest.source or "") != str(new_source or ""):
            return False
        if str(latest.schedule_alignment or "") != str(new_schedule_alignment or ""):
            return False
        old_theme = str(latest_details.get("life_theme") or self._infer_life_flow_theme(str(latest.summary or ""), latest_details, source=str(latest.source or "")))
        new_theme = str(new_details.get("life_theme") or self._infer_life_flow_theme(new_summary, new_details, source=new_source))
        if old_theme != new_theme:
            return False
        if bool(latest_details.get("has_path_rewrite")) or bool(new_details.get("has_path_rewrite")):
            return False
        overlap = self._keyword_overlap_score(new_summary.lower(), str(latest.summary or "").lower())
        return overlap >= 0.35

    @staticmethod
    def _merge_life_flow_trace_summary(old_summary: str, new_summary: str, limit: int = 420) -> str:
        old_text = str(old_summary or "").strip()
        new_text = str(new_summary or "").strip()
        if not old_text:
            return new_text[:limit]
        if not new_text:
            return old_text[:limit]
        if new_text in old_text:
            return old_text[:limit]
        if old_text in new_text:
            return new_text[:limit]
        merged = f"{old_text}；{new_text}"
        return merged[:limit]

    def _merge_life_flow_trace_details(self, old_raw: str, new_details: dict) -> dict:
        merged = self._parse_life_flow_details(old_raw)
        for key, value in (new_details or {}).items():
            if value in (None, "", [], {}):
                continue
            if key in {"open_loops", "special_details"}:
                old_list = merged.get(key) if isinstance(merged.get(key), list) else []
                new_list = value if isinstance(value, list) else [value]
                merged[key] = list(dict.fromkeys([str(item).strip() for item in old_list + new_list if str(item).strip()]))[:6]
            else:
                merged[key] = value
        return merged

    @staticmethod
    def _merge_related_event_ids(old_raw: str, new_ids: list[int]) -> list[int]:
        old_ids: list[int] = []
        try:
            parsed = json.loads(str(old_raw or "").strip() or "[]")
            if isinstance(parsed, list):
                old_ids = [int(x) for x in parsed if str(x).strip().isdigit()]
        except Exception:
            old_ids = []
        merged = list(dict.fromkeys(old_ids + [int(x) for x in new_ids if int(x) > 0]))
        return merged[:12]

    async def _build_conversation_schedule_impact(
        self,
        claim: ConversationTimeClaim | None,
    ) -> tuple[str, str, list[dict]]:
        if claim is None or not claim.started_at or not claim.ended_at:
            return "", "unexpected_inserted", []
        try:
            start_dt = self._parse_iso_datetime(claim.started_at)
            end_dt = self._parse_iso_datetime(claim.ended_at)
        except Exception:
            return "", "unexpected_inserted", []
        impacted: list[dict] = []
        alignment = "unexpected_inserted"
        if self.plan_engine is None:
            return "", alignment, impacted
        day_cursor = start_dt.date()
        end_day = end_dt.date()
        while day_cursor <= end_day:
            plan = await self.db.get_latest_daily_plan_for_date(day_cursor.isoformat(), status="active")
            if plan is None or plan.id is None:
                day_cursor += timedelta(days=1)
                continue
            items = await self.db.list_plan_items(int(plan.id))
            for item in items:
                if item.status != "pending":
                    continue
                item_start = start_dt.replace(
                    year=day_cursor.year,
                    month=day_cursor.month,
                    day=day_cursor.day,
                    hour=item.hour_start,
                    minute=0,
                    second=0,
                    microsecond=0,
                )
                item_end = start_dt.replace(
                    year=day_cursor.year,
                    month=day_cursor.month,
                    day=day_cursor.day,
                    hour=item.hour_end,
                    minute=0,
                    second=0,
                    microsecond=0,
                )
                if item_end <= start_dt or item_start >= end_dt:
                    continue
                impacted.append(
                    {
                        "plan_id": int(plan.id),
                        "item_id": int(item.id or 0),
                        "activity": item.activity,
                        "hour_start": item.hour_start,
                        "hour_end": item.hour_end,
                    }
                )
                if item.id is not None:
                    await self.db.update_plan_item(
                        int(item.id),
                        status="skipped",
                        outcome="被对话占时，等待后续重排",
                    )
            day_cursor += timedelta(days=1)
        if impacted:
            alignment = "replaced_by_conversation"
        summary = "；".join(
            f"{row['hour_start']:02d}:00-{row['hour_end']:02d}:00 {row['activity']}"
            for row in impacted[:4]
        )
        return summary, alignment, impacted

    async def _build_environment_context_details(
        self,
        checkpoint_time: datetime,
    ) -> dict:
        current_plan_summary = ""
        current_plan_activity = ""
        recent_trace_text = await self._build_recent_life_flow_trace_text()
        recent_disturbances_text = self._format_recent_disturbances_text(
            await self._list_recent_disturbances()
        )
        latest_claims = await self.db.list_conversation_time_claims(status="closed", limit=1)
        latest_claim_summary = "（最近没有对话占时改写记录）"
        if latest_claims:
            latest_claim_summary = (
                str(latest_claims[0].context_summary or "").strip()[:220]
                or latest_claim_summary
            )
        if self.plan_engine is not None:
            try:
                current_plan_summary = await self.plan_engine.get_plan_summary_text()
                current_plan_activity = await self.plan_engine.get_plan_activity_for_time(checkpoint_time)
            except Exception:
                logger.exception("Failed to load plan context for environment generation.")
        recent_trace_summary = recent_trace_text
        active_claim = await self._get_effective_active_conversation_time_claim()
        conversation_state = ""
        if active_claim is not None and active_claim.started_at:
            conversation_state = f"对话正在占用角色生活时间，自 {active_claim.started_at} 起持续中"
        latest_trace = await self.db.get_latest_life_flow_trace_for_date(
            checkpoint_time.date().isoformat()
        )
        schedule_alignment = str((latest_trace.schedule_alignment if latest_trace else "") or "on_track")
        plan_delta = str((latest_trace.summary if latest_trace else latest_claim_summary) or "").strip()
        return {
            "current_plan_summary": current_plan_summary,
            "current_plan_activity": current_plan_activity,
            "current_conversation_state": conversation_state,
            "recent_trace_summary": recent_trace_summary,
            "recent_disturbances": recent_disturbances_text,
            "schedule_alignment": schedule_alignment,
            "plan_delta": plan_delta[:300],
        }

    @staticmethod
    async def _trace_await(
        tracer: OperationTracer | None,
        stage_name: str,
        awaitable,
        **meta,
    ):
        if tracer is None:
            return await awaitable
        return await tracer.run(stage_name, awaitable, **meta)

    async def get_current_state(
        self,
        current_time: str,
        last_interaction_time: str | None = None,
        *,
        return_schedule: bool = False,
    ) -> str | tuple[str, dict]:
        tracer = OperationTracer(
            logger,
            "state_machine.get_current_state",
            meta={
                "input_current_time": current_time,
                "input_has_last_interaction": bool(str(last_interaction_time or "").strip()),
                "return_schedule": bool(return_schedule),
            },
        )
        now: datetime | None = None
        requested_last: datetime | None = None
        schedule_meta: dict = {}
        llm_usage: dict | None = None
        lock_held = False
        try:
            with tracer.stage("parse_inputs"):
                now = parse_user_instant_to_shanghai(current_time)
                requested_last = (
                    parse_user_instant_to_shanghai(last_interaction_time)
                    if str(last_interaction_time or "").strip()
                    else None
                )

            await tracer.run("wait_advance_lock", self._advance_lock.acquire())
            lock_held = True
            self.snapshot_llm.begin_usage_tracking()
            try:
                latest_snapshot = await self._trace_await(
                    tracer,
                    "db.get_latest_snapshot",
                    self.db.get_latest_snapshot(),
                )
                latest_conversation_end = await self._trace_await(
                    tracer,
                    "db.get_latest_snapshot_by_type.conversation_end",
                    self.db.get_latest_snapshot_by_type("conversation_end"),
                )
                snapshot_instant = self._snapshot_created_instant(latest_snapshot)
                conversation_end_instant = self._snapshot_created_instant(
                    latest_conversation_end
                )
                effective_last_interaction = conversation_end_instant
                baseline_time = self._resolve_get_current_state_baseline(
                    latest_snapshot, now, snapshot_instant
                )
                previous_content = (
                    latest_snapshot.content if latest_snapshot else "（尚无历史状态记录）"
                )
                previous_env = self._snapshot_environment_dict(latest_snapshot)
                catchup_max_steps = self.REQUEST_CATCHUP_MAX_STEPS

                advance_result = await self._advance_until_locked(
                    baseline_time=baseline_time,
                    target_time=now,
                    current_content=previous_content,
                    previous_env=previous_env,
                    max_steps=catchup_max_steps,
                    trigger="get_current_state",
                    snapshot_anchor_for_tail=snapshot_instant,
                    enforce_tail_min_gap_rule=True,
                    defer_maintenance=True,
                    diagnostic=tracer,
                )
                current_content = str(advance_result["content"] or previous_content)
                schedule_meta = dict(advance_result["schedule"])
                schedule_meta["baseline_source"] = (
                    "latest_snapshot"
                    if snapshot_instant is not None
                    else "current_time"
                )
                schedule_meta["latest_snapshot_cst"] = (
                    utc_naive_to_shanghai_iso(snapshot_instant)
                    if snapshot_instant is not None
                    else None
                )
                schedule_meta["requested_last_interaction_cst"] = (
                    utc_naive_to_shanghai_iso(requested_last)
                    if requested_last is not None
                    else None
                )
                schedule_meta["conversation_end_last_interaction_cst"] = (
                    utc_naive_to_shanghai_iso(conversation_end_instant)
                    if conversation_end_instant is not None
                    else None
                )
                schedule_meta["last_interaction_source"] = (
                    "conversation_end_snapshot"
                    if conversation_end_instant is not None
                    else "none"
                )
                # get_current_state 内部使用的 last_interaction（优先采用 DB 最新 conversation_end）
                schedule_meta["input_last_interaction_cst"] = (
                    utc_naive_to_shanghai_iso(effective_last_interaction)
                    if effective_last_interaction is not None
                    else None
                )
                schedule_meta["returned_content_mode"] = (
                    "latest_only"
                    if not schedule_meta.get("generated_snapshots")
                    else "catchup"
                )
                schedule_meta["request_checkpoint_cap"] = catchup_max_steps
                schedule_meta["memory_search_mode"] = "per_checkpoint"
                schedule_meta["event_anchor_mode"] = "per_checkpoint"
                schedule_meta["maintenance_mode"] = "deferred"
                logger.info(
                    "get_current_state schedule: %s",
                    json.dumps(schedule_meta, ensure_ascii=False),
                )
            finally:
                llm_usage = self.snapshot_llm.end_usage_tracking()

            if schedule_meta.get("generated_snapshots"):
                self._schedule_deferred_maintenance(
                    trigger="get_current_state",
                    llm_usage=llm_usage,
                )
            latest_snapshot_id = None
            generated = schedule_meta.get("generated_snapshots") or []
            if generated:
                latest_snapshot_id = int(generated[-1].get("id") or 0) or None
            elif latest_snapshot and latest_snapshot.id is not None:
                latest_snapshot_id = int(latest_snapshot.id)
            await self._trace_await(
                tracer,
                "ensure_active_conversation_claim",
                self._ensure_active_conversation_time_claim(
                    started_at=now or shanghai_now(),
                    latest_snapshot_id=latest_snapshot_id,
                    context_summary=str(current_content or "")[:240],
                ),
            )
            await self._trace_await(
                tracer,
                "refresh_slowlines.get_current_state",
                self._refresh_slowlines(),
            )
            await self._trace_await(
                tracer,
                "refresh_relationship_state.get_current_state",
                self._refresh_relationship_state(latest_snapshot_text=current_content),
            )
            injectable = await self._trace_await(
                tracer,
                "build_injectable_context",
                self._build_injectable_context(current_content),
                snapshot_chars=len(current_content or ""),
            )
            tracer.finish_ok(
                generated_snapshot_count=len(schedule_meta.get("generated_snapshots") or []),
                llm_requests=int((llm_usage or {}).get("requests") or 0),
            )
            if return_schedule:
                return injectable, schedule_meta
            return injectable
        except Exception as exc:
            tracer.finish_error(
                exc,
                generated_snapshot_count=len(schedule_meta.get("generated_snapshots") or []),
                llm_requests=int((llm_usage or {}).get("requests") or 0),
            )
            raise
        finally:
            if lock_held:
                self._advance_lock.release()

    async def get_snapshot_scheduler_interval_seconds(self) -> int:
        interval_sec = await self._get_snapshot_scheduler_interval_sec()
        if not await self._get_snapshot_scheduler_enabled():
            return interval_sec
        pause_state = await self._get_snapshot_scheduler_night_pause_state()
        if not pause_state:
            return interval_sec
        return max(interval_sec, int(pause_state["resume_in_seconds"]))

    async def get_snapshot_scheduler_public_info(self) -> dict[str, bool | int | str | None]:
        enabled = await self._get_snapshot_scheduler_enabled()
        interval_sec = await self._get_snapshot_scheduler_interval_sec()
        info: dict[str, bool | int | str | None] = {
            "enabled": enabled,
            "interval_sec": interval_sec,
            "paused": False,
            "pause_reason": None,
            "resume_at_cst": None,
            "last_auto_snapshot_cst": None,
        }
        if not enabled:
            return info
        pause_state = await self._get_snapshot_scheduler_night_pause_state()
        if not pause_state:
            return info
        info.update(
            {
                "paused": True,
                "pause_reason": str(pause_state["reason"]),
                "resume_at_cst": str(pause_state["resume_at_cst"]),
                "last_auto_snapshot_cst": str(pause_state["last_auto_snapshot_cst"]),
            }
        )
        return info

    async def run_snapshot_scheduler_tick(self) -> dict:
        async with self._advance_lock:
            enabled = await self._get_snapshot_scheduler_enabled()
            interval_sec = await self._get_snapshot_scheduler_interval_sec()
            if not enabled:
                return {
                    "status": "disabled",
                    "interval_sec": interval_sec,
                }

            pause_state = await self._get_snapshot_scheduler_night_pause_state()
            if pause_state:
                return {
                    "status": "paused",
                    "reason": str(pause_state["reason"]),
                    "interval_sec": max(interval_sec, int(pause_state["resume_in_seconds"])),
                    "resume_at_cst": str(pause_state["resume_at_cst"]),
                    "resume_in_seconds": int(pause_state["resume_in_seconds"]),
                    "last_auto_snapshot_cst": str(pause_state["last_auto_snapshot_cst"]),
                    "now_cst": str(pause_state["now_cst"]),
                }

            active_claim = await self._get_effective_active_conversation_time_claim()
            if active_claim is not None:
                return {
                    "status": "paused",
                    "reason": "active_conversation",
                    "interval_sec": interval_sec,
                    "conversation_started_at": str(active_claim.started_at or ""),
                    "conversation_context": str(active_claim.context_summary or ""),
                }

            await self._refresh_relationship_state()
            await self._refresh_slowlines()

            latest_snapshot = await self.db.get_latest_snapshot()
            if latest_snapshot is None:
                return {
                    "status": "idle",
                    "reason": "no_snapshot_baseline",
                    "interval_sec": interval_sec,
                }

            now = shanghai_now()
            latest_time = self._resolve_progress_baseline(latest_snapshot, None)
            min_time_unit = await self._get_min_time_unit_timedelta()
            raw_lag_seconds = (now - latest_time).total_seconds()
            lag_seconds = max(0.0, raw_lag_seconds)
            min_sec = min_time_unit.total_seconds()
            if raw_lag_seconds < -1.0:
                logger.warning(
                    "Snapshot scheduler: latest snapshot time is after current time; lag is clamped to 0, so no auto-advance will run. "
                    "Common cause: legacy snapshot created_at was written without timezone and interpreted as local wall clock, while newer rows use Z. "
                    "latest_cst=%s now_cst=%s raw_lag_h=%.4f min_unit_h=%.4f",
                    utc_naive_to_shanghai_iso(latest_time),
                    utc_naive_to_shanghai_iso(now),
                    raw_lag_seconds / 3600.0,
                    min_sec / 3600.0,
                )
            if lag_seconds < min_sec:
                logger.debug(
                    "Snapshot scheduler idle not_due: lag_h=%.4f min_unit_h=%.4f latest_cst=%s",
                    lag_seconds / 3600.0,
                    min_sec / 3600.0,
                    utc_naive_to_shanghai_iso(latest_time),
                )
                return {
                    "status": "idle",
                    "reason": "not_due",
                    "interval_sec": interval_sec,
                    "lag_hours": round(lag_seconds / 3600.0, 4),
                    "min_time_unit_hours": round(min_sec / 3600.0, 6),
                    "latest_snapshot_cst": utc_naive_to_shanghai_iso(latest_time),
                    "now_cst": utc_naive_to_shanghai_iso(now),
                    "raw_lag_hours": round(raw_lag_seconds / 3600.0, 6),
                }

            # 与 get_current_state 共用上限：单次 tick 只推进多格，避免大缺口靠「轮询次数 × 1 步」慢慢磨
            catchup_max_steps = await self._get_snapshot_catchup_max_steps()
            self.snapshot_llm.begin_usage_tracking()
            report: dict | None = None
            try:
                advance_result = await self._advance_until_locked(
                    baseline_time=latest_time,
                    target_time=now,
                    current_content=latest_snapshot.content,
                    previous_env=self._snapshot_environment_dict(latest_snapshot),
                    max_steps=catchup_max_steps,
                    trigger="snapshot_scheduler",
                    allow_tail_checkpoint=False,
                )
                if advance_result["schedule"].get("generated_snapshots"):
                    report = await self._run_automation(trigger="snapshot_scheduler")
            finally:
                llm_usage = self.snapshot_llm.end_usage_tracking()

            await self._persist_automation_report(report, llm_usage)
            result = dict(advance_result["schedule"])
            gen = result.get("generated_snapshots") or []
            result.update(
                {
                    "status": "advanced" if gen else "idle",
                    "interval_sec": interval_sec,
                    "lag_hours": round(lag_seconds / 3600.0, 4),
                    "llm_usage": llm_usage,
                    "catchup_max_steps_per_tick": catchup_max_steps,
                }
            )
            if not gen:
                result["reason"] = "advance_no_new_snapshots"
                result["now_cst"] = utc_naive_to_shanghai_iso(now)
                result["latest_snapshot_cst"] = utc_naive_to_shanghai_iso(latest_time)
                logger.warning(
                    "Snapshot scheduler: lag already reached the minimum interval but no snapshot was generated (planned=%s executed=%s).",
                    result.get("planned_checkpoint_count"),
                    result.get("checkpoint_count"),
                )
            return result

    async def reflect_on_conversation(self, conversation_summary: str) -> str:
        tracer = OperationTracer(
            logger,
            "state_machine.reflect_on_conversation",
            meta={"conversation_summary_chars": len(conversation_summary or "")},
        )
        llm_usage: dict | None = None
        new_content = ""
        lock_held = False
        try:
            await tracer.run("wait_advance_lock", self._advance_lock.acquire())
            lock_held = True
            self.snapshot_llm.begin_usage_tracking()
            try:
                latest_snapshot = await self._trace_await(
                    tracer,
                    "db.get_latest_snapshot",
                    self.db.get_latest_snapshot(),
                )
                previous_content = (
                    latest_snapshot.content if latest_snapshot else "（尚无历史状态记录）"
                )

                memory_results = await self._trace_await(
                    tracer,
                    "memory.search_for_reflection",
                    self.memory.search(
                        conversation_summary,
                        top_k=self.DEFAULT_MEMORY_TOP_K,
                    ),
                    query_chars=len(conversation_summary or ""),
                    top_k=self.DEFAULT_MEMORY_TOP_K,
                )
                memory_text, memory_meta = self._build_memory_context(memory_results)

                with tracer.stage(
                    "load_reflection_prompts_and_layers",
                    memory_count=int(memory_meta.get("selected_count", 0)),
                ):
                    system_prompt = await self.prompt_manager.get_system_prompt()
                    reflect_template = await self.prompt_manager.get_prompt(
                        KEY_PROMPT_REFLECT_SNAPSHOT
                    )
                    character_background = await self.prompt_manager.get_layer_content(
                        KEY_L1_CHARACTER_BACKGROUND
                    )
                    character_personality = await self.prompt_manager.get_layer_content(
                        KEY_L2_CHARACTER_PERSONALITY
                    )
                    relationship_dynamics = await self.prompt_manager.get_layer_content(
                        KEY_L2_RELATIONSHIP_DYNAMICS
                    )
                    life_status = await self.prompt_manager.get_layer_content(
                        KEY_L2_LIFE_STATUS
                    )

                reflect_prompt = reflect_template.format(
                    character_background=character_background,
                    character_personality=character_personality,
                    relationship_dynamics=relationship_dynamics,
                    life_status=life_status,
                    previous_snapshot=previous_content,
                    conversation_summary=conversation_summary,
                    memory_context=memory_text,
                )

                new_content = await self._trace_await(
                    tracer,
                    "snapshot_llm.reflect_snapshot",
                    self.snapshot_llm.chat(
                        [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": reflect_prompt},
                        ],
                        max_tokens=None,
                    ),
                    prompt_chars=len(reflect_prompt or ""),
                    memory_chars=len(memory_text or ""),
                )

                snap = StateSnapshot(
                    created_at=format_utc_instant_z(datetime.utcnow()),
                    type="conversation_end",
                    content=new_content,
                    environment="{}",
                    referenced_events="[]",
                )
                snap_id = await self._trace_await(
                    tracer,
                    "db.insert_conversation_end_snapshot",
                    self.db.insert_snapshot(snap),
                    snapshot_chars=len(new_content or ""),
                )
                closed_claim = await self._trace_await(
                    tracer,
                    "close_active_conversation_claim",
                    self._close_active_conversation_time_claim(
                        ended_at=shanghai_now(),
                        closing_snapshot_id=int(snap_id or 0),
                        context_summary=conversation_summary,
                    ),
                )
                impacted_summary, schedule_alignment, impacted_items = await self._trace_await(
                    tracer,
                    "build_conversation_schedule_impact",
                    self._build_conversation_schedule_impact(closed_claim),
                )
                trace_details = {
                    "conversation_summary": conversation_summary,
                    "impacted_plan_items": impacted_items,
                    "closing_snapshot_id": int(snap_id or 0),
                }
                trace_summary = self._build_trace_digest_from_conversation_summary(conversation_summary)
                if impacted_summary:
                    trace_summary = (
                        f"对话占用了原日程：{impacted_summary}。{trace_summary}"
                    ).strip()
                await self._trace_await(
                    tracer,
                    "append_conversation_life_flow_trace",
                    self._append_life_flow_trace(
                        trace_date=shanghai_now().date().isoformat(),
                        source="conversation",
                        summary=trace_summary,
                        details=trace_details,
                        schedule_alignment=schedule_alignment,
                        related_snapshot_id=int(snap_id or 0),
                    ),
                )
                await self._trace_await(
                    tracer,
                    "refresh_slowlines.after_conversation",
                    self._refresh_slowlines(),
                )
                await self._trace_await(
                    tracer,
                    "append_relationship_thought.after_conversation",
                    self._append_relationship_thought_from_context(
                        source_snapshot_id=int(snap_id or 0),
                        source_env_id="conversation_end",
                        snapshot_text=new_content,
                        conversation_summary=conversation_summary,
                    ),
                )
                await self._trace_await(
                    tracer,
                    "refresh_relationship_state.after_conversation",
                    self._refresh_relationship_state(
                        conversation_summary=conversation_summary,
                        latest_snapshot_text=new_content,
                    ),
                )
                self._schedule_deferred_event_generation(
                    snapshot_id=int(snap_id or 0),
                    snapshot_content=new_content,
                    environment={},
                    memory_text=memory_text,
                    checkpoint_time=shanghai_now(),
                    defer_vectorization=True,
                    conversation_summary=conversation_summary,
                    conversation_started_at=str((closed_claim.started_at if closed_claim else "") or ""),
                )
            finally:
                llm_usage = self.snapshot_llm.end_usage_tracking()

            self._schedule_deferred_maintenance(
                trigger="reflect_on_conversation",
                llm_usage=llm_usage,
            )
            if self.plan_engine is not None:
                try:
                    await self.plan_engine.maybe_replan(
                        trigger="conversation_end",
                        context=(
                            f"{conversation_summary}\n\n"
                            f"对话占时与日程影响：{impacted_summary or '无直接重叠计划项'}"
                        ),
                    )
                except Exception:
                    logger.exception("Plan replan after reflection failed.")
            tracer.finish_ok(
                llm_requests=int((llm_usage or {}).get("requests") or 0),
                output_snapshot_chars=len(new_content or ""),
                reflect_event_mode="disabled",
            )
            return new_content
        except Exception as exc:
            tracer.finish_error(
                exc,
                llm_requests=int((llm_usage or {}).get("requests") or 0),
                output_snapshot_chars=len(new_content or ""),
            )
            raise
        finally:
            if lock_held:
                self._advance_lock.release()

    def _extract_summary_section(self, summary_text: str, labels: list[str]) -> str:
        raw = str(summary_text or "").strip()
        if not raw:
            return ""
        label_group = "|".join(re.escape(label) for label in labels)
        next_group = r"事实性信息|关系动态变化|情感关键时刻|未完成线索|关键时刻|动态变化"
        match = re.search(
            rf"(?:{label_group})\s*[:：]\s*(.*?)(?=\n\s*(?:{next_group})\s*[:：]|\Z)",
            raw,
            re.DOTALL,
        )
        return (match.group(1) if match else "").strip()

    def _build_trace_digest_from_conversation_summary(self, conversation_summary: str) -> str:
        raw = str(conversation_summary or "").strip()
        if not raw:
            return ""
        facts = self._extract_summary_section(raw, ["事实性信息"])
        relation = self._extract_summary_section(raw, ["关系动态变化"])
        emotion = self._extract_summary_section(raw, ["情感关键时刻"])
        open_loops = self._extract_summary_section(raw, ["未完成线索"])
        parts: list[str] = []
        if relation:
            parts.append(f"这次对话让关系与理解出现了新的位移：{relation}")
        if facts:
            parts.append(f"对话中形成或确认的事实推进包括：{facts}")
        if emotion and not any(self._keyword_overlap_score(emotion.lower(), part.lower()) >= 0.55 for part in parts):
            parts.append(f"情绪上的关键波动是：{emotion}")
        if open_loops:
            parts.append(f"仍未收束的线索有：{open_loops}")
        if parts:
            return " ".join(parts)
        cleaned = re.sub(r"【[^】]+】", "", raw)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    def _compact_structured_memory_text(self, text: str) -> str:
        raw = str(text or "").strip()
        if not raw:
            return ""
        if any(label in raw for label in ("事实性信息", "关系动态变化", "情感关键时刻", "未完成线索")):
            return self._build_trace_digest_from_conversation_summary(raw)
        if any(token in raw for token in ('"content_text"', '"content_json"', '"latest_change"', '"previous_baseline"', '"next_watch_point"')):
            extracted_parts: list[str] = []
            for key in ("previous_baseline", "latest_change", "next_watch_point", "content_text"):
                matches = re.findall(rf'"{key}"\s*:\s*"([^"]+)"', raw)
                for match in matches[:2]:
                    cleaned_match = re.sub(r"\s+", " ", str(match or "").strip())
                    if cleaned_match and not any(self._keyword_overlap_score(cleaned_match.lower(), part.lower()) >= 0.82 for part in extracted_parts):
                        extracted_parts.append(cleaned_match)
            if extracted_parts:
                return "；".join(extracted_parts)[:480]
        cleaned = re.sub(r"【[^】]+】\s*", "", raw)
        cleaned = re.sub(r"(最近：\s*\{+|起点：\s*\{+)", "", cleaned)
        cleaned = cleaned.replace("{", " ").replace("}", " ")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if "；" in cleaned:
            parts: list[str] = []
            for part in [seg.strip("； ").strip() for seg in cleaned.split("；")]:
                if not part:
                    continue
                if any(self._keyword_overlap_score(part.lower(), existing.lower()) >= 0.84 for existing in parts):
                    continue
                parts.append(part)
            cleaned = "；".join(parts)
        return cleaned[:480]

    async def summarize_conversation(self, conversation_text: str) -> str:
        latest_snapshot = await self.db.get_latest_snapshot()
        previous_content = latest_snapshot.content if latest_snapshot else "（尚无历史状态记录）"
        memory_results = await self.memory.search(
            conversation_text,
            top_k=self.DEFAULT_MEMORY_TOP_K,
        )
        memory_text = self._format_memories(memory_results) if memory_results else "（无相关历史记忆）"
        system_prompt = await self.prompt_manager.get_system_prompt()
        summary_template = await self.prompt_manager.get_prompt(KEY_PROMPT_CONVERSATION_SUMMARY)
        summary_prompt = summary_template.format(
            previous_snapshot=previous_content,
            conversation_text=conversation_text,
            memory_context=memory_text,
            system_layers=await self.prompt_manager.get_system_layers_text(),
        )
        summary = await self.llm.chat([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": summary_prompt},
        ])
        return (summary or "").strip()

    def _source_family_for_trace(self, trace: LifeFlowTrace) -> str:
        details = self._parse_life_flow_details(trace.details_json)
        theme = str(
            details.get("life_theme")
            or self._infer_life_flow_theme(str(trace.summary or ""), details, source=str(trace.source or ""))
        ).strip().lower()
        return {
            "relationship": "relationship",
            "medical": "health",
            "study": "study",
            "work": "work",
            "economic": "logistics",
            "economy": "logistics",
            "mobility": "logistics",
            "routine": "daily_life",
            "conversation": "relationship",
            "general": "daily_life",
        }.get(theme, "daily_life")

    def _source_family_for_event(self, event: EventAnchor) -> str:
        categories = [str(item).strip().lower() for item in self._parse_json_list(getattr(event, "categories", "[]"))]
        joined = " ".join(categories)
        if any(token in joined for token in ("medical", "health")):
            return "health"
        if "study" in categories:
            return "study"
        if "work" in categories:
            return "work"
        if any(token in joined for token in ("economic", "economy", "mobility", "logistics")):
            return "logistics"
        if any(token in joined for token in ("relationship", "conversation")):
            return "relationship"
        return "daily_life"

    @staticmethod
    def _json_int_list(raw: str | None) -> list[int]:
        try:
            parsed = json.loads(str(raw or "").strip() or "[]")
            if isinstance(parsed, list):
                return [int(item) for item in parsed if str(item).strip().isdigit()]
        except Exception:
            pass
        return []

    @staticmethod
    def _parse_json_object(raw: str | None) -> dict:
        try:
            parsed = json.loads(str(raw or "").strip() or "{}")
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        return {}

    def _record_supports_slowline(self, record: KeyRecord) -> bool:
        return str(record.type or "") in {
            "health_monitoring",
            "medical_review_date",
            "lifecycle_milestone",
            "key_collaboration",
            "commitment_agreement",
            "life_pattern",
        }

    def _slowline_theme_from_record(self, record: KeyRecord) -> str:
        title = str(record.title or "").strip()
        if title:
            return title[:80]
        return {
            "health_monitoring": "健康监测线",
            "medical_review_date": "复诊推进线",
            "lifecycle_milestone": "人生阶段线",
            "key_collaboration": "协作项目线",
            "commitment_agreement": "关系原则线",
            "life_pattern": "生活节律线",
        }.get(str(record.type or ""), "持续生活线")

    def _slowline_family_from_record(self, record: KeyRecord) -> str:
        record_type = str(record.type or "").strip()
        return {
            "medication_protocol": "health",
            "health_monitoring": "health",
            "dietary_intervention": "health",
            "medical_review_date": "health",
            "lifecycle_milestone": "daily_life",
            "key_collaboration": "work",
            "commitment_agreement": "relationship",
            "emotional_anchor": "relationship",
            "life_pattern": "daily_life",
            "anniversary_date": "daily_life",
        }.get(record_type, "daily_life")

    def _slowline_scope_from_text(self, text: str, *, source_family: str, explicit_scope: str | None = None) -> str:
        if explicit_scope in {"user_side", "character_side", "shared"}:
            return str(explicit_scope)
        lowered = str(text or "").lower()
        if any(token in lowered for token in ("用户", "泳琳", "eloise", "论文", "实习", "mcp", "呼吸道", "花粉")):
            return "user_side"
        if any(token in lowered for token in ("凯尔希", "罗德岛", "医疗部", "左臂", "办公室")):
            return "character_side"
        if source_family == "relationship":
            return "shared"
        return "shared"

    def _normalize_thread_key(self, value: str) -> str:
        cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "-", str(value or "").strip().lower())
        cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
        return cleaned[:96]

    def _build_thread_key(self, *, scope: str, source_family: str, title: str, fallback_id: int | None = None) -> str:
        stem = self._normalize_thread_key(title)
        if not stem:
            stem = f"{source_family}-{fallback_id or 'line'}"
        return self._normalize_thread_key(f"{scope}-{source_family}-{stem}")

    def _extract_embedded_record_payload(self, text: str) -> dict:
        payload = self._parse_json_object(text)
        if not payload:
            return {}
        if any(key in payload for key in ("content_text", "content_json", "title", "type")):
            return payload
        return {}

    def _safe_parse_iso_date(self, value: str | None) -> datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw[:10])
        except Exception:
            return None

    def _build_key_record_match_blob(
        self,
        *,
        title: str,
        content_text: str,
        tags: list[str],
        match_keywords: list[str],
        content_json: dict | None,
    ) -> str:
        payload = content_json or {}
        latest_change = self._compact_structured_memory_text(str(payload.get("latest_change") or "").strip())
        previous_baseline = self._compact_structured_memory_text(str(payload.get("previous_baseline") or "").strip())
        next_watch_point = self._compact_structured_memory_text(
            str(payload.get("next_watch_point") or payload.get("watch_point") or "").strip()
        )
        body = self._compact_structured_memory_text(content_text)
        return self._build_record_thread_hint(
            title=title,
            tags=tags,
            match_keywords=match_keywords,
            latest_change=latest_change or body,
            previous_baseline=previous_baseline,
            next_watch_point=next_watch_point,
        )

    def _build_record_thread_hint(
        self,
        *,
        title: str,
        tags: list[str],
        match_keywords: list[str],
        latest_change: str,
        previous_baseline: str,
        next_watch_point: str,
    ) -> str:
        tokens: list[str] = []
        if title:
            tokens.append(title)
        tokens.extend(tags[:4])
        tokens.extend(match_keywords[:4])
        for block in (latest_change, previous_baseline, next_watch_point):
            cleaned = self._compact_structured_memory_text(block)
            if cleaned:
                tokens.append(cleaned[:60])
        return " ".join(token for token in tokens if token).strip()[:320]

    def _score_key_record_upsert_candidate(
        self,
        *,
        normalized_type: str,
        incoming_title: str,
        incoming_blob: str,
        incoming_start_date: str | None,
        incoming_end_date: str | None,
        candidate: KeyRecord,
    ) -> float:
        if str(candidate.type or "") != normalized_type:
            return 0.0
        score = 0.0
        candidate_payload = self._parse_json_object(candidate.content_json)
        candidate_tags = self._parse_json_list(candidate.tags)
        candidate_keywords = self._parse_json_list(getattr(candidate, "match_keywords", "[]"))
        candidate_blob = self._build_key_record_match_blob(
            title=str(candidate.title or ""),
            content_text=str(candidate.content_text or ""),
            tags=candidate_tags,
            match_keywords=candidate_keywords,
            content_json=candidate_payload,
        )
        title_overlap = self._keyword_overlap_score(
            self._normalize_thread_key(incoming_title),
            self._normalize_thread_key(str(candidate.title or "")),
        )
        blob_overlap = self._keyword_overlap_score(
            self._normalize_thread_key(incoming_blob),
            self._normalize_thread_key(candidate_blob),
        )
        score += min(0.42, title_overlap * 0.42)
        score += min(0.58, blob_overlap * 0.58)
        if str(candidate.status or "") == "active":
            score += 0.04
        start_a = self._safe_parse_iso_date(incoming_start_date)
        start_b = self._safe_parse_iso_date(candidate.start_date)
        if start_a and start_b:
            diff_days = abs((start_a - start_b).days)
            if diff_days == 0:
                score += 0.08
            elif diff_days <= 3:
                score += 0.04
        end_a = self._safe_parse_iso_date(incoming_end_date)
        end_b = self._safe_parse_iso_date(candidate.end_date)
        if end_a and end_b and abs((end_a - end_b).days) == 0:
            score += 0.04
        if title_overlap < 0.3 and blob_overlap < 0.62:
            return 0.0
        return score

    async def _find_key_record_upsert_candidate(
        self,
        *,
        normalized_type: str,
        title: str,
        content_text: str,
        tags: list[str],
        content_json: dict | None,
        start_date: str | None,
        end_date: str | None,
    ) -> KeyRecord | None:
        exact = await self.db.get_key_record_by_type_title(normalized_type, title)
        if exact is not None:
            return exact
        incoming_blob = self._build_key_record_match_blob(
            title=title,
            content_text=content_text,
            tags=tags,
            match_keywords=[],
            content_json=content_json,
        )
        candidates = await self.db.get_all_key_records(
            offset=0,
            limit=24,
            record_type=normalized_type,
            include_archived=True,
        )
        best: KeyRecord | None = None
        best_score = 0.0
        for candidate in candidates:
            score = self._score_key_record_upsert_candidate(
                normalized_type=normalized_type,
                incoming_title=title,
                incoming_blob=incoming_blob,
                incoming_start_date=start_date,
                incoming_end_date=end_date,
                candidate=candidate,
            )
            if score > best_score:
                best_score = score
                best = candidate
        return best if best is not None and best_score >= 0.74 else None

    def _build_event_match_blob(
        self,
        *,
        title: str,
        objective_text: str,
        impression_text: str,
        keyword_list: list[str],
        category_list: list[str],
    ) -> str:
        tokens = [
            str(title or "").strip(),
            self._compact_structured_memory_text(objective_text)[:100],
            self._compact_structured_memory_text(impression_text)[:80],
            " ".join(keyword_list[:4]),
            " ".join(category_list[:3]),
        ]
        return " ".join(token for token in tokens if token).strip()[:360]

    def _score_event_upsert_candidate(
        self,
        *,
        event_date: str,
        normalized_title: str,
        incoming_blob: str,
        keyword_list: list[str],
        category_list: list[str],
        candidate: EventAnchor,
    ) -> float:
        score = 0.0
        date_a = self._safe_parse_iso_date(event_date)
        date_b = self._safe_parse_iso_date(candidate.date)
        if date_a and date_b:
            diff_days = abs((date_a - date_b).days)
            if diff_days == 0:
                score += 0.24
            elif diff_days == 1:
                score += 0.12
            elif diff_days <= 3:
                score += 0.04
            else:
                return 0.0
        title_overlap = self._keyword_overlap_score(
            self._normalize_thread_key(normalized_title),
            self._normalize_thread_key(str(candidate.title or "")),
        )
        candidate_keywords = self._parse_json_list(getattr(candidate, "trigger_keywords", "[]"))
        candidate_categories = self._parse_json_list(getattr(candidate, "categories", "[]"))
        desc = self._compact_structured_memory_text(self._normalize_event_detail(candidate.description or candidate.title or ""))
        candidate_blob = self._build_event_match_blob(
            title=str(candidate.title or ""),
            objective_text=desc,
            impression_text="",
            keyword_list=candidate_keywords,
            category_list=candidate_categories,
        )
        blob_overlap = self._keyword_overlap_score(
            self._normalize_thread_key(incoming_blob),
            self._normalize_thread_key(candidate_blob),
        )
        keyword_overlap = self._keyword_overlap_score(
            self._normalize_thread_key(" ".join(keyword_list)),
            self._normalize_thread_key(" ".join(candidate_keywords)),
        ) if keyword_list and candidate_keywords else 0.0
        category_overlap = self._keyword_overlap_score(
            self._normalize_thread_key(" ".join(category_list)),
            self._normalize_thread_key(" ".join(candidate_categories)),
        ) if category_list and candidate_categories else 0.0
        score += min(0.32, title_overlap * 0.32)
        score += min(0.46, blob_overlap * 0.46)
        score += min(0.12, keyword_overlap * 0.12)
        score += min(0.08, category_overlap * 0.08)
        if title_overlap < 0.28 and blob_overlap < 0.64:
            return 0.0
        return score

    async def _find_event_upsert_candidate(
        self,
        *,
        event_date: str,
        normalized_title: str,
        objective_text: str,
        impression_text: str,
        keyword_list: list[str],
        category_list: list[str],
    ) -> EventAnchor | None:
        exact = await self.db.get_event_by_date_title(event_date, normalized_title)
        if exact is not None:
            return exact
        incoming_blob = self._build_event_match_blob(
            title=normalized_title,
            objective_text=objective_text,
            impression_text=impression_text,
            keyword_list=keyword_list,
            category_list=category_list,
        )
        candidates = await self.db.get_recent_events_by_event_time(limit=36, include_archived=True)
        best: EventAnchor | None = None
        best_score = 0.0
        for candidate in candidates:
            score = self._score_event_upsert_candidate(
                event_date=event_date,
                normalized_title=normalized_title,
                incoming_blob=incoming_blob,
                keyword_list=keyword_list,
                category_list=category_list,
                candidate=candidate,
            )
            if score > best_score:
                best_score = score
                best = candidate
        return best if best is not None and best_score >= 0.76 else None

    def _infer_progress_status_from_text(self, text: str, *, fallback: str = "advancing") -> str:
        lowered = str(text or "").lower()
        if any(token in lowered for token in ("完成", "结束", "收束", "落实", "提交", "解决", "closed", "completed")):
            return "completed"
        if any(token in lowered for token in ("暂停", "搁置", "中断", "停滞", "延迟", "paused", "delayed")):
            return "paused"
        if any(token in lowered for token in ("待收尾", "接近完成", "ready_to_close", "收尾")):
            return "ready_to_close"
        if any(token in lowered for token in ("放弃", "终止", "dropped")):
            return "dropped"
        return fallback

    def _extract_detail_hooks_from_text(self, text: str, *, limit: int = 3) -> list[str]:
        normalized = self._compact_structured_memory_text(str(text or "").strip())
        if not normalized:
            return []
        parts = re.split(r"[；;。.!?]\s*", normalized)
        hooks: list[str] = []
        for part in parts:
            cleaned = re.sub(r"\s+", " ", str(part or "").strip())
            if len(cleaned) < 6:
                continue
            hooks.append(cleaned[:80])
            if len(hooks) >= limit:
                break
        return hooks

    def _score_slowline_candidate(
        self,
        *,
        thread_key: str,
        theme: str,
        hint_blob: str,
        source_family: str,
        scope: str,
        candidate: SlowLine,
    ) -> float:
        score = 0.0
        candidate_key = str(getattr(candidate, "thread_key", "") or "").strip().lower()
        if candidate_key and candidate_key == thread_key.lower():
            return 1.0
        if str(getattr(candidate, "scope", "") or "") == scope:
            score += 0.12
        if str(candidate.source_family or "") == source_family:
            score += 0.22
        theme_score = min(
            0.66,
            self._keyword_overlap_score(
                self._normalize_thread_key(theme),
                self._normalize_thread_key(str(candidate.theme or "")),
            ),
        )
        score += theme_score
        trajectory = self._normalize_thread_key(str(getattr(candidate, "trajectory_summary", "") or ""))
        if trajectory:
            score += min(0.18, self._keyword_overlap_score(self._normalize_thread_key(theme), trajectory))
        hint_score = self._keyword_overlap_score(
            self._normalize_thread_key(hint_blob),
            self._normalize_thread_key(
                " ".join(
                    [
                        str(candidate.theme or ""),
                        str(getattr(candidate, "stage_summary", "") or ""),
                        str(getattr(candidate, "trajectory_summary", "") or ""),
                        str(getattr(candidate, "current_tension", "") or ""),
                    ]
                )
            ),
        )
        score += min(0.34, hint_score)
        return score

    def _merge_trajectory_summary(
        self,
        *,
        existing: str,
        previous_baseline: str,
        latest_change: str,
        progress_status: str,
    ) -> str:
        parts: list[str] = []
        existing_compact = self._compact_structured_memory_text(existing) if existing else ""
        if existing_compact:
            parts.append(existing_compact)
        elif previous_baseline:
            parts.append(f"起点：{self._compact_structured_memory_text(previous_baseline)}")
        if latest_change:
            latest_text = self._compact_structured_memory_text(latest_change)
            if latest_text and not any(self._keyword_overlap_score(latest_text.lower(), part.lower()) >= 0.8 for part in parts):
                parts.append(f"最近：{latest_text}")
        elif previous_baseline and not parts:
            parts.append(self._compact_structured_memory_text(previous_baseline))
        status_hint = {
            "paused": "当前处于停滞或被外部条件压住的阶段",
            "ready_to_close": "当前已接近收束，重点转向收尾与确认",
            "completed": "当前已形成阶段性完成",
            "dropped": "当前已转入放弃或失效状态",
        }.get(progress_status, "")
        if status_hint:
            parts.append(status_hint)
        deduped: list[str] = []
        for part in parts:
            compact = self._compact_structured_memory_text(part)
            if not compact:
                continue
            if any(self._keyword_overlap_score(compact.lower(), existing_part.lower()) >= 0.84 for existing_part in deduped):
                continue
            deduped.append(compact)
        return "；".join(deduped)[:560]

    def _looks_like_archive_payload(self, text: str) -> bool:
        compact = self._compact_structured_memory_text(text)
        if not compact:
            return False
        parameter_tokens = (
            "度数", "散光", "频率", "每周", "颜色", "尺码", "面料", "参数", "配给",
            "偏好", "记录", "计划", "提醒", "建议", "镜片", "验光", "咖啡", "运动",
            "血样", "检测项", "路线", "强度", "禁忌", "用途", "决策方式",
        )
        archive_hits = sum(1 for token in parameter_tokens if token in compact)
        list_markers = sum(compact.count(marker) for marker in ("1.", "2.", "3.", "-", "："))
        return archive_hits >= 3 or (archive_hits >= 2 and list_markers >= 4)

    def _extract_display_core(self, text: str, *, max_len: int = 120) -> str:
        compact = self._compact_structured_memory_text(text)
        if not compact:
            return ""
        compact = re.sub(r"^(起点|最近|当前卡点|待观察|纵向概括|关键细节)[:：]\s*", "", compact)
        parts = [seg.strip() for seg in re.split(r"[；;。]\s*", compact) if seg.strip()]
        if not parts:
            return compact[:max_len]
        preferred = []
        for part in parts:
            if any(token in part for token in ("危机", "崩溃", "嫉妒", "退出", "疼", "麻木", "等待", "拒绝", "决定", "转向", "卡点", "观察", "未定", "待")):
                preferred.append(part)
        candidate = preferred[0] if preferred else parts[0]
        return candidate[:max_len]

    def _build_display_stage_summary(
        self,
        *,
        source_family: str,
        title: str,
        latest_change: str,
        next_watch_point: str,
        fallback_text: str,
    ) -> str:
        title_compact = self._compact_structured_memory_text(title)[:36]
        latest_core = self._extract_display_core(latest_change, max_len=120)
        watch_core = self._extract_display_core(next_watch_point, max_len=90)
        fallback_core = self._extract_display_core(fallback_text, max_len=120)
        if self._looks_like_archive_payload("\n".join([title, latest_change, next_watch_point, fallback_text])):
            if latest_core:
                return f"{title_compact}：{latest_core}" if title_compact else latest_core
            if watch_core:
                return f"{title_compact}：{watch_core}" if title_compact else watch_core
        core = latest_core or watch_core or fallback_core
        if not core:
            return title_compact
        if title_compact and self._keyword_overlap_score(title_compact.lower(), core.lower()) < 0.5:
            return f"{title_compact}：{core}"[:150]
        return core[:150]

    def _build_display_trajectory_summary(
        self,
        *,
        previous_baseline: str,
        latest_change: str,
        current_tension: str,
        existing: str,
    ) -> str:
        parts: list[str] = []
        start = self._extract_display_core(previous_baseline or existing, max_len=90)
        shift = self._extract_display_core(latest_change, max_len=100)
        tension = self._extract_display_core(current_tension, max_len=80)
        if start:
            parts.append(f"起点：{start}")
        if shift and not any(self._keyword_overlap_score(shift.lower(), p.lower()) >= 0.8 for p in parts):
            parts.append(f"最近：{shift}")
        if tension and not any(self._keyword_overlap_score(tension.lower(), p.lower()) >= 0.8 for p in parts):
            parts.append(f"当前卡点：{tension}")
        return "；".join(parts)[:240]

    def _strip_title_prefix(self, label: str, text: str) -> str:
        compact_label = self._compact_structured_memory_text(label)
        compact_text = self._compact_structured_memory_text(text)
        if not compact_text:
            return ""
        if compact_label and compact_text.startswith(f"{compact_label}："):
            return compact_text[len(compact_label) + 1 :].strip()
        if compact_label and self._keyword_overlap_score(compact_label.lower(), compact_text.lower()) >= 0.92:
            return compact_text
        return compact_text

    def _build_slowline_theme(self, *, title: str, source_family: str) -> str:
        cleaned = self._compact_structured_memory_text(title)
        if cleaned:
            return cleaned[:80]
        return {
            "health": "健康演化线",
            "study": "学习推进线",
            "work": "工作推进线",
            "relationship": "关系动态线",
            "logistics": "事务与后勤线",
            "daily_life": "日常生活线",
        }.get(source_family, "持续生活线")

    def _dedupe_memory_bridge_text(self, memory_bridge_text: str, recent_life_line_text: str) -> str:
        bridge_lines = [line.strip() for line in str(memory_bridge_text or "").splitlines() if line.strip()]
        recent_blob = self._normalize_thread_key(str(recent_life_line_text or ""))
        if not bridge_lines or not recent_blob:
            return memory_bridge_text
        kept: list[str] = []
        for line in bridge_lines:
            normalized_line = self._normalize_thread_key(line)
            if normalized_line and self._keyword_overlap_score(normalized_line, recent_blob) >= 0.86:
                continue
            kept.append(line)
        return "\n".join(kept) if kept else "（更早的背景已被近期生活主线充分覆盖）"

    def _detail_fingerprint(self, text: str) -> str:
        compact = self._compact_structured_memory_text(str(text or "").strip())
        normalized = self._normalize_thread_key(compact)
        return normalized[:180]

    def _collect_recent_life_detail_fingerprints(self, recent_life_text: str) -> set[str]:
        lines = str(recent_life_text or "").splitlines()
        fingerprints: set[str] = set()
        in_detail_block = False
        for raw_line in lines:
            line = str(raw_line or "").rstrip()
            stripped = line.strip()
            if not stripped:
                in_detail_block = False
                continue
            if "关键细节" in stripped:
                in_detail_block = True
                continue
            if in_detail_block and stripped.startswith("-"):
                bullet_text = re.sub(r"^\s*-\s*", "", stripped)
                if "：" in bullet_text:
                    bullet_text = bullet_text.split("：", 1)[1]
                fp = self._detail_fingerprint(bullet_text)
                if fp:
                    fingerprints.add(fp)
                continue
            if in_detail_block:
                in_detail_block = False
        return fingerprints

    def _build_relationship_thought_fingerprint(self, topic_line: str, thought_type: str, content: str) -> str:
        base = self._normalize_thread_key(f"{topic_line} {thought_type} {content}")
        if not base:
            base = "relationship-thought"
        return hashlib.sha1(base.encode("utf-8")).hexdigest()[:24]

    def _infer_relationship_shadow_attributes(self, text: str) -> dict[str, str]:
        lowered = str(text or "").lower()
        urgency = "medium"
        if any(token in lowered for token in ("立刻", "马上", "尽快", "今天", "今晚", "明天", "按时", "到点")):
            urgency = "high"
        elif any(token in lowered for token in ("改天", "之后", "以后", "有空", "长期", "慢慢")):
            urgency = "low"
        horizon = "near_term"
        if any(token in lowered for token in ("长期", "以后", "慢慢", "一直")):
            horizon = "long_term"
        elif any(token in lowered for token in ("今天", "今晚", "立刻", "马上", "到点")):
            horizon = "immediate"
        activation_style = "opportunistic"
        if any(token in lowered for token in ("氛围", "时机", "合适", "状态", "看情况")):
            activation_style = "mood_sensitive"
        elif any(token in lowered for token in ("等她", "等你", "她先", "用户先", "对方先")):
            activation_style = "user_initiated"
        elif any(token in lowered for token in ("几点", "按时", "定好", "固定", "约好时间")):
            activation_style = "fixed_time"
        shared_assumption_strength = "medium"
        if any(token in lowered for token in ("约定", "说好", "共识", "承诺", "答应")):
            shared_assumption_strength = "high"
        elif any(token in lowered for token in ("想法", "也许", "如果", "看情况")):
            shared_assumption_strength = "low"
        return {
            "urgency": urgency,
            "horizon": horizon,
            "activation_style": activation_style,
            "shared_assumption_strength": shared_assumption_strength,
        }

    def _build_relationship_thought_content(
        self,
        *,
        topic_line: str,
        anchor_text: str,
        attrs: dict[str, str],
    ) -> tuple[str, str]:
        horizon = attrs.get("horizon", "near_term")
        activation_style = attrs.get("activation_style", "opportunistic")
        urgency = attrs.get("urgency", "medium")
        thought_type = "reconsider"
        if activation_style == "user_initiated":
            thought_type = "wait"
            lead = f"{topic_line} 这条线更适合等她先把入口打开，我暂时不把它推进成既定动作。"
        elif activation_style == "mood_sensitive":
            thought_type = "mood_dependent"
            lead = f"{topic_line} 还在心里，但更像需要看时机、氛围和当下状态再决定是否提起。"
        elif horizon == "long_term":
            thought_type = "timing_sensitive"
            lead = f"{topic_line} 更像一条长期挂念，不需要在今天立刻兑现，先保留它的余波即可。"
        elif horizon == "immediate" and urgency == "high":
            thought_type = "ask"
            lead = f"如果她此刻出现，我更可能先从 {topic_line} 试着确认一步，但没有合适入口时也不急着硬推。"
        else:
            thought_type = "reconsider"
            lead = f"{topic_line} 这条线还在心里，若她主动提起，我大概会顺着它继续判断下一步。"
        anchor = self._compact_structured_memory_text(anchor_text)
        if anchor and self._keyword_overlap_score(anchor.lower(), lead.lower()) < 0.58:
            lead = f"{lead} 目前更在意的是：{anchor}"
        return thought_type, lead[:220]

    async def _list_plan_relationship_shadow_hints(self, *, limit: int = 8) -> list[dict]:
        if self.plan_engine is None:
            return []
        plan = await self.plan_engine.get_current_plan()
        if plan is None or plan.id is None:
            return []
        items = await self.db.list_plan_items(int(plan.id))
        hints: list[dict] = []
        for item in items:
            payload = self._parse_json_object(item.action_payload)
            shadow_hint = self._compact_structured_memory_text(str(payload.get("relationship_shadow_hint") or "").strip())
            if not shadow_hint:
                continue
            hints.append(
                {
                    "topic_line": self._compact_structured_memory_text(
                        str(payload.get("intended_objective") or item.activity or "与用户相关的未启动线").strip()
                    )[:80],
                    "anchor_text": shadow_hint,
                    "salience": 0.84 if item.status == "pending" else 0.7,
                }
            )
        return hints[:limit]

    async def _append_relationship_thought_from_context(
        self,
        *,
        source_snapshot_id: int | None,
        source_env_id: str | None,
        snapshot_text: str,
        environment_text: str = "",
        conversation_summary: str = "",
    ) -> RelationshipThought | None:
        today = shanghai_now().date().isoformat()
        slowlines = await self.db.list_slowlines(status="active", limit=12)
        candidates: list[dict] = []
        for hint in await self._list_plan_relationship_shadow_hints():
            anchor_text = str(hint.get("anchor_text") or "").strip()
            if not anchor_text:
                continue
            candidates.append(
                {
                    "topic_line": str(hint.get("topic_line") or "与用户相关的未启动线").strip(),
                    "anchor_text": anchor_text,
                    "salience": float(hint.get("salience") or 0.72),
                }
            )
        for line in slowlines:
            scope = str(getattr(line, "scope", "shared") or "shared")
            if scope not in {"user_side", "shared"}:
                continue
            topic_line = self._compact_structured_memory_text(str(line.theme or "").strip())[:80]
            anchor_text = self._compact_structured_memory_text(
                str(line.current_tension or "").strip()
                or ", ".join(self._parse_json_list(line.open_questions)[:1])
                or str(line.stage_summary or "").strip()
            )
            if not topic_line or not anchor_text:
                continue
            salience = float(line.salience or 0.0)
            if scope == "user_side":
                salience += 0.08
            candidates.append(
                {
                    "topic_line": topic_line,
                    "anchor_text": anchor_text,
                    "salience": salience,
                }
            )
        if conversation_summary.strip():
            candidates.append(
                {
                    "topic_line": "刚刚结束的对话余波",
                    "anchor_text": self._compact_structured_memory_text(conversation_summary)[:140],
                    "salience": 0.92,
                }
            )
        if not candidates:
            return None
        context_blob = self._normalize_thread_key(
            "\n".join(
                part for part in [snapshot_text or "", environment_text or "", conversation_summary or ""] if part
            )
        )
        ranked: list[tuple[float, dict]] = []
        for candidate in candidates:
            topic_line = str(candidate.get("topic_line") or "").strip()
            anchor_text = str(candidate.get("anchor_text") or "").strip()
            attrs = self._infer_relationship_shadow_attributes(f"{topic_line}\n{anchor_text}")
            score = float(candidate.get("salience") or 0.0)
            if context_blob:
                score += min(
                    0.18,
                    self._keyword_overlap_score(
                        self._normalize_thread_key(f"{topic_line} {anchor_text}"),
                        context_blob,
                    ),
                )
            ranked.append((score, {"topic_line": topic_line, "anchor_text": anchor_text, "attrs": attrs}))
        ranked.sort(key=lambda item: item[0], reverse=True)
        top_score, top = ranked[0]
        if top_score < 0.48:
            return None
        thought_type, content = self._build_relationship_thought_content(
            topic_line=str(top["topic_line"]),
            anchor_text=str(top["anchor_text"]),
            attrs=dict(top["attrs"]),
        )
        fingerprint = self._build_relationship_thought_fingerprint(str(top["topic_line"]), thought_type, content)
        existing = await self.db.get_relationship_thought_by_fingerprint(
            thought_date=today,
            dedupe_fingerprint=fingerprint,
            resolution_status="open",
        )
        if existing is not None and existing.id is not None:
            await self.db.update_relationship_thought(
                int(existing.id),
                source_snapshot_id=source_snapshot_id,
                source_env_id=source_env_id,
                salience=max(float(existing.salience or 0.0), float(min(top_score, 0.98))),
                content=content,
            )
            refreshed = await self.db.get_relationship_thought_by_fingerprint(
                thought_date=today,
                dedupe_fingerprint=fingerprint,
                resolution_status="open",
            )
            return refreshed or existing
        thought = RelationshipThought(
            thought_date=today,
            source_snapshot_id=source_snapshot_id,
            source_env_id=source_env_id,
            topic_line=str(top["topic_line"]),
            thought_type=thought_type,
            content=content,
            salience=float(min(top_score, 0.98)),
            dedupe_fingerprint=fingerprint,
            resolution_status="open",
        )
        thought_id = await self.db.insert_relationship_thought(thought)
        thoughts = await self.db.list_relationship_thoughts(thought_date=today, resolution_status="open", limit=24)
        for item in thoughts:
            if int(item.id or 0) == int(thought_id):
                return item
        thought.id = int(thought_id)
        return thought

    @staticmethod
    def _infer_tension_level(*texts: str) -> str:
        joined = " ".join(str(text or "").strip() for text in texts if str(text or "").strip())
        if not joined:
            return "low"
        high_markers = (
            "危机", "崩溃", "嫉妒", "厌恶", "撕裂", "死亡", "自杀", "求救", "痛苦", "冲突", "边界", "无法接受",
            "受阻", "麻木", "压垮", "绝望", "震荡", "审判", "过载", "存在性",
        )
        medium_markers = (
            "观察", "等待", "跟进", "犹豫", "拖延", "调整", "波动", "变化", "重写", "摩擦", "担心", "预约",
        )
        if any(token in joined for token in high_markers):
            return "high"
        if any(token in joined for token in medium_markers):
            return "medium"
        return "low"

    @staticmethod
    def _infer_unresolved_level(progress_status: str, current_tension: str, open_question: str) -> str:
        if str(progress_status or "") in {"completed", "dropped"}:
            return "low"
        if current_tension or open_question:
            return "high" if len((current_tension or "") + (open_question or "")) >= 20 else "medium"
        if str(progress_status or "") in {"paused", "open", "ready_to_close"}:
            return "medium"
        return "low"

    def _infer_emotional_tension(self, *, tension_level: str, unresolved_level: str, scope: str, text: str) -> str:
        lowered = str(text or "")
        if tension_level == "high" and "嫉妒" in lowered:
            return "brittle"
        if tension_level == "high" and unresolved_level == "high":
            return "unresolved"
        if "安抚" in lowered or "照顾" in lowered or "陪伴" in lowered:
            return "tender"
        if str(scope or "") == "user_side" and unresolved_level != "low":
            return "strained"
        if tension_level == "medium":
            return "suspended"
        return "stable"

    @staticmethod
    def _infer_affective_direction(text: str) -> str:
        lowered = str(text or "")
        if any(token in lowered for token in ("靠近", "陪伴", "确认", "安抚", "拥抱", "回到", "联系")):
            return "approach"
        if any(token in lowered for token in ("推迟", "回避", "删减", "停止", "不想再", "离开")):
            return "avoidance"
        if any(token in lowered for token in ("修复", "重建", "重排", "干预", "支持")):
            return "repair"
        if any(token in lowered for token in ("等待", "承受", "维持", "观察")):
            return "endurance"
        return "ambivalence"

    def _infer_memory_role(
        self,
        *,
        source_family: str,
        progress_status: str,
        scope: str,
        tension_level: str,
        unresolved_level: str,
        current_tension: str,
        text: str,
    ) -> str:
        blob = f"{text}\n{current_tension}"
        if self._looks_like_archive_payload(blob):
            return "trigger_only"
        if source_family in {"logistics"} or any(token in blob for token in ("尺码", "参数", "颜色", "频率", "度数", "配方", "偏好")):
            return "trigger_only"
        if any(token in blob for token in ("计划", "偏好", "验光", "运动", "饮食", "采购", "账本", "费用", "收入来源", "短期", "中期", "长期", "框架")) and tension_level != "high" and unresolved_level != "high":
            return "trigger_only"
        if progress_status in {"completed", "dropped"} and tension_level == "low" and unresolved_level == "low":
            return "archive_reference"
        if unresolved_level == "high" or tension_level == "high":
            return "bridge_core" if scope in {"user_side", "shared"} else "active_thread_detail"
        if progress_status in {"open", "advancing", "paused", "ready_to_close"}:
            return "active_thread_detail"
        return "archive_reference"

    @staticmethod
    def _compute_preload_priority(
        *,
        scope: str,
        memory_role: str,
        progress_status: str,
        tension_level: str,
        unresolved_level: str,
        source_family: str,
    ) -> float:
        score = 0.18
        if scope in {"user_side", "shared"}:
            score += 0.16
        if memory_role == "bridge_core":
            score += 0.28
        elif memory_role == "active_thread_detail":
            score += 0.18
        elif memory_role == "trigger_only":
            score -= 0.18
        elif memory_role == "archive_reference":
            score -= 0.24
        score += {"low": 0.0, "medium": 0.08, "high": 0.18}.get(tension_level, 0.0)
        score += {"low": 0.0, "medium": 0.06, "high": 0.14}.get(unresolved_level, 0.0)
        if progress_status in {"paused", "open", "ready_to_close"}:
            score += 0.06
        if progress_status in {"completed", "dropped"}:
            score -= 0.16
        if source_family == "relationship":
            score += 0.06
        return max(0.0, min(1.0, score))

    @staticmethod
    def _score_level(level: str) -> float:
        return {"low": 0.18, "medium": 0.56, "high": 0.92}.get(str(level or "").strip(), 0.4)

    def _compute_recency_score(self, timestamp: str | None, *, now: datetime | None = None) -> float:
        raw = str(timestamp or "").strip()
        if not raw:
            return 0.22
        now_dt = now or shanghai_now()
        try:
            shifted = self._parse_iso_datetime(raw)
        except Exception:
            parsed_date = self._safe_parse_iso_date(raw)
            if parsed_date is None:
                return 0.22
            shifted = parsed_date
        age_hours = max(0.0, (now_dt - shifted).total_seconds() / 3600.0)
        if age_hours <= 12:
            return 1.0
        if age_hours <= 24:
            return 0.88
        if age_hours <= 72:
            return 0.68
        if age_hours <= 168:
            return 0.46
        if age_hours <= 336:
            return 0.28
        return 0.14

    def _compute_continuity_score(self, slowline: SlowLine) -> float:
        linked_events = len(self._json_int_list(getattr(slowline, "linked_event_ids", "[]")))
        linked_records = len(self._json_int_list(getattr(slowline, "linked_key_record_ids", "[]")))
        trajectory = self._compact_structured_memory_text(str(getattr(slowline, "trajectory_summary", "") or ""))
        score = 0.18
        score += min(0.42, linked_events * 0.08 + linked_records * 0.05)
        if len(trajectory) >= 40:
            score += 0.16
        if str(getattr(slowline, "recent_shift_summary", "") or "").strip():
            score += 0.08
        return min(1.0, score)

    @staticmethod
    def _compute_relationship_score(scope: str, source_family: str) -> float:
        score = 0.18
        if scope == "user_side":
            score += 0.44
        elif scope == "shared":
            score += 0.36
        if source_family == "relationship":
            score += 0.12
        elif source_family in {"health", "study", "work"}:
            score += 0.06
        return min(1.0, score)

    def _compute_novelty_score(self, *, summary: str, recent_shift: str) -> float:
        shift = self._compact_structured_memory_text(recent_shift)
        if not shift:
            return 0.18
        overlap = self._keyword_overlap_score(
            self._normalize_thread_key(summary),
            self._normalize_thread_key(shift),
        )
        return max(0.08, min(1.0, 0.92 - overlap))

    def _compute_embodiment_score(self, text: str) -> float:
        compact = self._compact_structured_memory_text(text)
        if not compact:
            return 0.0
        score = 0.12
        if any(token in compact for token in ("疼", "痛", "麻木", "哭", "抱", "摸", "吃", "睡", "呼吸", "心跳", "布洛芬", "生理期")):
            score += 0.38
        if any(token in compact for token in ("说", "问", "回", "低语", "报告", "承认")):
            score += 0.24
        if any(token in compact for token in ("凌晨", "中午", "下午", "深夜", "下课", "返回")):
            score += 0.12
        return min(1.0, score)

    def _compute_archive_penalty(self, *, text: str, source_family: str, memory_role: str) -> float:
        if memory_role == "archive_reference":
            return 0.92
        if memory_role == "trigger_only":
            return 0.76
        if self._looks_like_archive_payload(text):
            return 0.74
        if source_family == "logistics":
            return 0.62
        return 0.08

    def _compute_current_relevance_score(self, *, text: str, context_blob: str) -> float:
        compact = self._compact_structured_memory_text(text)
        if not compact or not context_blob:
            return 0.18
        overlap = self._keyword_overlap_score(
            self._normalize_thread_key(compact),
            self._normalize_thread_key(context_blob),
        )
        return min(1.0, 0.18 + overlap * 0.82)

    def _compute_historical_depth_score(self, slowline: SlowLine, *, now: datetime | None = None) -> float:
        score = 0.1
        touched = str(getattr(slowline, "last_meaningful_shift_at", "") or getattr(slowline, "last_touched_at", "") or "").strip()
        if touched:
            recency = self._compute_recency_score(touched, now=now)
            score += max(0.0, 0.72 - recency)
        linked_total = len(self._json_int_list(getattr(slowline, "linked_event_ids", "[]"))) + len(self._json_int_list(getattr(slowline, "linked_key_record_ids", "[]")))
        if linked_total >= 3:
            score += 0.16
        if linked_total >= 6:
            score += 0.12
        return min(1.0, score)

    async def _persist_key_record_process_payload(
        self,
        record: KeyRecord,
        *,
        previous_record: KeyRecord | None = None,
    ) -> tuple[KeyRecord, dict]:
        embedded = self._extract_embedded_record_payload(str(record.content_text or ""))
        if embedded:
            embedded_content_text = str(embedded.get("content_text") or "").strip()
            embedded_content_json = embedded.get("content_json")
            embedded_title = str(embedded.get("title") or "").strip()
            update_fields: dict[str, object] = {}
            if embedded_content_text and embedded_content_text != str(record.content_text or "").strip():
                record.content_text = embedded_content_text
                update_fields["content_text"] = embedded_content_text
            if embedded_title and not str(record.title or "").strip():
                record.title = embedded_title
                update_fields["title"] = embedded_title
            if isinstance(embedded_content_json, dict):
                serialized_embedded = json.dumps(embedded_content_json, ensure_ascii=False)
                if serialized_embedded != str(record.content_json or ""):
                    record.content_json = serialized_embedded
                    update_fields["content_json"] = serialized_embedded
            if update_fields and record.id is not None:
                await self.db.update_key_record(int(record.id), **update_fields)
                refreshed = await self.db.get_key_record_by_id(int(record.id))
                if refreshed is not None:
                    record = refreshed
        payload = self._parse_json_object(record.content_json)
        source_family = self._slowline_family_from_record(record)
        tags = self._parse_json_list(record.tags)
        match_keywords = self._parse_json_list(getattr(record, "match_keywords", "[]"))
        scope = self._slowline_scope_from_text(
            "\n".join(
                [
                    str(record.title or ""),
                    str(record.content_text or ""),
                    " ".join(tags),
                    " ".join(match_keywords),
                ]
            ),
            source_family=source_family,
            explicit_scope=str(payload.get("scope") or "").strip() or None,
        )
        latest_change = self._compact_structured_memory_text(
            str(payload.get("latest_change") or "").strip() or str(record.content_text or "").strip()
        )[:220]
        previous_baseline = self._compact_structured_memory_text(
            str(payload.get("previous_baseline") or "").strip()
            or str((previous_record.content_text if previous_record else "") or "").strip()
        )[:220]
        next_watch_point = self._compact_structured_memory_text(
            str(payload.get("next_watch_point") or payload.get("watch_point") or "").strip()
        )[:140]
        progress_status = self._infer_progress_status_from_text(
            str(payload.get("progress_status") or "").strip() or latest_change,
            fallback="completed" if str(record.status or "") == "archived" else "advancing",
        )
        memory_role = str(payload.get("memory_role") or "").strip()
        if memory_role not in {"bridge_core", "active_thread_detail", "trigger_only", "archive_reference"}:
            memory_role = self._infer_memory_role(
                source_family=source_family,
                progress_status=progress_status,
                scope=scope,
                tension_level=self._infer_tension_level(latest_change, next_watch_point, str(record.content_text or "")),
                unresolved_level=self._infer_unresolved_level(progress_status, next_watch_point, next_watch_point),
                current_tension=next_watch_point,
                text="\n".join([str(record.title or ""), str(record.content_text or ""), latest_change]),
            )
        thread_hint = self._build_record_thread_hint(
            title=str(record.title or ""),
            tags=tags,
            match_keywords=match_keywords,
            latest_change=latest_change,
            previous_baseline=previous_baseline,
            next_watch_point=next_watch_point,
        )
        thread_key = self._normalize_thread_key(str(payload.get("thread_id") or "").strip())
        if not thread_key:
            thread_key = self._build_thread_key(
                scope=scope,
                source_family=source_family,
                title=thread_hint or str(record.title or ""),
                fallback_id=int(record.id or 0) or None,
            )
        normalized_payload = dict(payload)
        normalized_payload.update(
            {
                "thread_id": thread_key,
                "thread_hint": thread_hint,
                "scope": scope,
                "progress_status": progress_status,
                "latest_change": latest_change,
                "previous_baseline": previous_baseline,
                "next_watch_point": next_watch_point,
                "memory_role": memory_role,
            }
        )
        serialized = json.dumps(normalized_payload, ensure_ascii=False)
        if serialized != str(record.content_json or "") and record.id is not None:
            await self.db.update_key_record(int(record.id), content_json=serialized)
            refreshed = await self.db.get_key_record_by_id(int(record.id))
            if refreshed is not None:
                return refreshed, normalized_payload
        record.content_json = serialized
        return record, normalized_payload

    async def _persist_event_process_payload(self, event: EventAnchor) -> tuple[EventAnchor, dict]:
        payload = self._parse_json_object(getattr(event, "meta_json", None))
        source_family = self._source_family_for_event(event)
        keywords = self._parse_json_list(getattr(event, "trigger_keywords", "[]"))
        categories = self._parse_json_list(getattr(event, "categories", "[]"))
        combined_text = "\n".join(
            [
                str(event.title or ""),
                str(event.description or ""),
                " ".join(keywords),
                " ".join(categories),
            ]
        )
        scope = self._slowline_scope_from_text(
            combined_text,
            source_family=source_family,
            explicit_scope=str(payload.get("scope") or "").strip() or None,
        )
        progress_effect = self._infer_progress_status_from_text(
            str(payload.get("progress_effect") or event.description or event.title),
            fallback="advancing",
        )
        open_loop = self._compact_structured_memory_text(
            self._extract_event_field_block(str(event.description or ""), ["Open loop", "open_loop"])
            or str(payload.get("open_loop") or "").strip()
        )[:140]
        detail_hooks = payload.get("detail_hooks")
        if not isinstance(detail_hooks, list) or not detail_hooks:
            block = self._extract_event_field_block(str(event.description or ""), ["Key detail hooks", "detail_hooks"])
            detail_hooks = self._extract_detail_hooks_from_text(block or str(event.description or ""), limit=3)
        detail_hooks = [self._compact_structured_memory_text(str(item).strip())[:80] for item in detail_hooks if str(item).strip()][:3]
        memory_role = str(payload.get("memory_role") or "").strip()
        if memory_role not in {"bridge_core", "active_thread_detail", "trigger_only", "archive_reference"}:
            memory_role = self._infer_memory_role(
                source_family=source_family,
                progress_status=progress_effect,
                scope=scope,
                tension_level=self._infer_tension_level(str(event.title or ""), str(event.description or ""), open_loop, " ".join(detail_hooks)),
                unresolved_level=self._infer_unresolved_level(progress_effect, open_loop, open_loop),
                current_tension=open_loop,
                text="\n".join([str(event.title or ""), str(event.description or ""), " ".join(detail_hooks)]),
            )
        thread_hint = " ".join(
            token
            for token in [
                str(event.title or "").strip(),
                " ".join(keywords[:4]),
                " ".join(categories[:3]),
                " ".join(detail_hooks[:2]),
                open_loop,
            ]
            if token
        )[:320]
        thread_key = self._normalize_thread_key(str(payload.get("thread_id") or "").strip())
        if not thread_key:
            thread_key = self._build_thread_key(
                scope=scope,
                source_family=source_family,
                title=thread_hint or str(event.title or ""),
                fallback_id=int(event.id or 0) or None,
            )
        normalized_payload = dict(payload)
        normalized_payload.update(
            {
                "thread_id": thread_key,
                "thread_hint": thread_hint,
                "scope": scope,
                "progress_effect": progress_effect,
                "detail_hooks": detail_hooks,
                "open_loop": open_loop,
                "memory_role": memory_role,
            }
        )
        serialized = json.dumps(normalized_payload, ensure_ascii=False)
        if serialized != str(getattr(event, "meta_json", None) or "") and event.id is not None:
            await self.db.update_event(int(event.id), meta_json=serialized)
            refreshed = await self.db.get_event_by_id(int(event.id))
            if refreshed is not None:
                return refreshed, normalized_payload
        event.meta_json = serialized
        return event, normalized_payload

    async def _find_best_slowline_candidate(
        self,
        *,
        thread_key: str,
        theme: str,
        hint_blob: str,
        source_family: str,
        scope: str,
    ) -> SlowLine | None:
        if thread_key:
            exact = await self.db.get_slowline_by_thread_key(thread_key)
            if exact is not None:
                return exact
        candidates = await self.db.list_slowlines(status="active", limit=24)
        best: SlowLine | None = None
        best_score = 0.0
        for candidate in candidates:
            score = self._score_slowline_candidate(
                thread_key=thread_key,
                theme=theme,
                hint_blob=hint_blob,
                source_family=source_family,
                scope=scope,
                candidate=candidate,
            )
            if score > best_score:
                best_score = score
                best = candidate
        return best if best is not None and best_score >= 0.72 else None

    async def _refresh_related_slowline_from_key_record(
        self,
        record: KeyRecord,
        *,
        previous_record: KeyRecord | None = None,
    ) -> None:
        if record.id is None or not self._record_supports_slowline(record):
            return
        record, payload = await self._persist_key_record_process_payload(record, previous_record=previous_record)
        source_family = self._slowline_family_from_record(record)
        scope = str(payload.get("scope") or "shared")
        progress_status = str(payload.get("progress_status") or "advancing")
        latest_change = self._compact_structured_memory_text(str(payload.get("latest_change") or "").strip())
        previous_baseline = self._compact_structured_memory_text(str(payload.get("previous_baseline") or "").strip())
        next_watch_point = self._compact_structured_memory_text(str(payload.get("next_watch_point") or "").strip())
        memory_role = str(payload.get("memory_role") or "active_thread_detail").strip()
        tension_level = self._infer_tension_level(latest_change, next_watch_point, str(record.content_text or ""))
        unresolved_level = self._infer_unresolved_level(progress_status, next_watch_point, next_watch_point)
        emotional_tension = self._infer_emotional_tension(
            tension_level=tension_level,
            unresolved_level=unresolved_level,
            scope=scope,
            text="\n".join([str(record.title or ""), str(record.content_text or ""), latest_change, next_watch_point]),
        )
        affective_direction = self._infer_affective_direction(
            "\n".join([str(record.title or ""), str(record.content_text or ""), latest_change, next_watch_point])
        )
        preload_priority = self._compute_preload_priority(
            scope=scope,
            memory_role=memory_role,
            progress_status=progress_status,
            tension_level=tension_level,
            unresolved_level=unresolved_level,
            source_family=source_family,
        )
        thread_key = self._normalize_thread_key(str(payload.get("thread_id") or "").strip())
        theme = self._build_slowline_theme(title=str(record.title or ""), source_family=source_family)
        hint_blob = self._compact_structured_memory_text(str(payload.get("thread_hint") or "").strip()) or " ".join(
            token for token in [theme, latest_change, previous_baseline, next_watch_point] if token
        )
        slowline = await self._find_best_slowline_candidate(
            thread_key=thread_key,
            theme=theme,
            hint_blob=hint_blob,
            source_family=source_family,
            scope=scope,
        )
        linked_key_ids = {int(record.id)}
        linked_event_ids: set[int] = set()
        existing_trajectory = ""
        if slowline is not None:
            linked_key_ids.update(self._json_int_list(slowline.linked_key_record_ids))
            linked_event_ids.update(self._json_int_list(slowline.linked_event_ids))
            existing_trajectory = str(getattr(slowline, "trajectory_summary", "") or "")
        if record.linked_event_id:
            linked_event_ids.add(int(record.linked_event_id))
        trajectory_summary = self._merge_trajectory_summary(
            existing=existing_trajectory,
            previous_baseline=previous_baseline,
            latest_change=latest_change,
            progress_status=progress_status,
        )
        stage_summary = self._build_display_stage_summary(
            source_family=source_family,
            title=str(record.title or ""),
            latest_change=latest_change,
            next_watch_point=next_watch_point,
            fallback_text=str(record.content_text or ""),
        )
        trajectory_display = self._build_display_trajectory_summary(
            previous_baseline=previous_baseline,
            latest_change=latest_change,
            current_tension=next_watch_point,
            existing=trajectory_summary,
        ) or trajectory_summary
        fields = {
            "thread_key": thread_key,
            "theme": theme,
            "scope": scope,
            "source_family": source_family,
            "memory_role": memory_role,
            "progress_status": progress_status,
            "tension_level": tension_level,
            "unresolved_level": unresolved_level,
            "preload_priority": preload_priority,
            "stage_summary": stage_summary[:180],
            "trajectory_summary": trajectory_display[:240],
            "current_tension": ("" if progress_status in {"completed", "dropped"} else next_watch_point[:180]),
            "recent_shift_summary": self._extract_display_core(latest_change, max_len=140),
            "recent_movement_summary": self._extract_display_core(latest_change, max_len=140),
            "last_meaningful_shift_at": str(record.updated_at or record.created_at or "") or None,
            "emotional_tension": emotional_tension,
            "affective_direction": affective_direction,
            "open_questions": json.dumps([next_watch_point] if next_watch_point and progress_status not in {"completed", "dropped"} else [], ensure_ascii=False),
            "salience": 0.78 if scope == "user_side" else 0.66,
            "last_touched_at": str(record.updated_at or record.created_at or "") or None,
            "linked_key_record_ids": json.dumps(sorted(linked_key_ids)[-16:], ensure_ascii=False),
            "linked_event_ids": json.dumps(sorted(linked_event_ids)[-16:], ensure_ascii=False),
            "status": "active",
        }
        if slowline is not None and slowline.id is not None:
            await self.db.update_slowline(int(slowline.id), **fields)
        else:
            await self.db.insert_slowline(SlowLine(**fields))

    async def _refresh_related_slowline_from_event(self, event: EventAnchor) -> None:
        if event.id is None:
            return
        event, payload = await self._persist_event_process_payload(event)
        source_family = self._source_family_for_event(event)
        scope = str(payload.get("scope") or "shared")
        progress_status = str(payload.get("progress_effect") or "advancing")
        detail_hooks = [self._compact_structured_memory_text(str(item).strip()) for item in (payload.get("detail_hooks") or []) if str(item).strip()]
        open_loop = self._compact_structured_memory_text(str(payload.get("open_loop") or "").strip())
        memory_role = str(payload.get("memory_role") or "active_thread_detail").strip()
        stage_summary = detail_hooks[0] if detail_hooks else self._compact_structured_memory_text(str(event.title or event.description or "").strip())
        tension_level = self._infer_tension_level(str(event.title or ""), str(event.description or ""), open_loop, " ".join(detail_hooks))
        unresolved_level = self._infer_unresolved_level(progress_status, open_loop, open_loop)
        emotional_tension = self._infer_emotional_tension(
            tension_level=tension_level,
            unresolved_level=unresolved_level,
            scope=scope,
            text="\n".join([str(event.title or ""), str(event.description or ""), open_loop, " ".join(detail_hooks)]),
        )
        affective_direction = self._infer_affective_direction(
            "\n".join([str(event.title or ""), str(event.description or ""), open_loop, " ".join(detail_hooks)])
        )
        preload_priority = self._compute_preload_priority(
            scope=scope,
            memory_role=memory_role,
            progress_status=progress_status,
            tension_level=tension_level,
            unresolved_level=unresolved_level,
            source_family=source_family,
        )
        thread_key = self._normalize_thread_key(str(payload.get("thread_id") or "").strip())
        theme = self._build_slowline_theme(title=str(event.title or ""), source_family=source_family)
        hint_blob = self._compact_structured_memory_text(str(payload.get("thread_hint") or "").strip()) or " ".join(
            token for token in [theme, " ".join(detail_hooks[:2]), open_loop] if token
        )
        slowline = await self._find_best_slowline_candidate(
            thread_key=thread_key,
            theme=theme,
            hint_blob=hint_blob,
            source_family=source_family,
            scope=scope,
        )
        linked_key_ids: set[int] = set()
        linked_event_ids = {int(event.id)}
        existing_trajectory = ""
        previous_stage = ""
        if slowline is not None:
            linked_key_ids.update(self._json_int_list(slowline.linked_key_record_ids))
            linked_event_ids.update(self._json_int_list(slowline.linked_event_ids))
            existing_trajectory = str(getattr(slowline, "trajectory_summary", "") or "")
            previous_stage = str(getattr(slowline, "stage_summary", "") or "")
        trajectory_summary = self._merge_trajectory_summary(
            existing=existing_trajectory,
            previous_baseline=previous_stage,
            latest_change=stage_summary,
            progress_status=progress_status,
        )
        stage_display = self._build_display_stage_summary(
            source_family=source_family,
            title=str(event.title or ""),
            latest_change=stage_summary,
            next_watch_point=open_loop,
            fallback_text=str(event.description or ""),
        )
        trajectory_display = self._build_display_trajectory_summary(
            previous_baseline=previous_stage,
            latest_change=stage_summary,
            current_tension=open_loop,
            existing=trajectory_summary,
        ) or trajectory_summary
        open_questions = self._parse_json_list(getattr(slowline, "open_questions", "[]")) if slowline is not None else []
        if open_loop:
            open_questions.append(open_loop)
        fields = {
            "thread_key": thread_key,
            "theme": theme,
            "scope": scope,
            "source_family": source_family,
            "memory_role": memory_role,
            "progress_status": progress_status,
            "tension_level": tension_level,
            "unresolved_level": unresolved_level,
            "preload_priority": preload_priority,
            "stage_summary": stage_display[:180],
            "trajectory_summary": trajectory_display[:240],
            "current_tension": ("" if progress_status in {"completed", "dropped"} else open_loop[:180]),
            "recent_shift_summary": self._extract_display_core(stage_summary, max_len=140),
            "recent_movement_summary": self._extract_display_core(stage_summary, max_len=140),
            "last_meaningful_shift_at": str(event.created_at or "") or None,
            "emotional_tension": emotional_tension,
            "affective_direction": affective_direction,
            "open_questions": json.dumps(list(dict.fromkeys([q for q in open_questions if q]))[:6], ensure_ascii=False),
            "salience": max(0.62, min(0.92, 0.55 + float(event.importance_score or 0.0) / 20.0)),
            "last_touched_at": str(event.created_at or "") or None,
            "linked_key_record_ids": json.dumps(sorted(linked_key_ids)[-16:], ensure_ascii=False),
            "linked_event_ids": json.dumps(sorted(linked_event_ids)[-16:], ensure_ascii=False),
            "status": "active",
        }
        if slowline is not None and slowline.id is not None:
            await self.db.update_slowline(int(slowline.id), **fields)
        else:
            await self.db.insert_slowline(SlowLine(**fields))

    async def refresh_related_slowline_from_key_record_id(
        self,
        record_id: int,
        *,
        previous_record: KeyRecord | None = None,
    ) -> None:
        record = await self.db.get_key_record_by_id(record_id)
        if record is None:
            return
        await self._refresh_related_slowline_from_key_record(record, previous_record=previous_record)

    async def refresh_related_slowline_from_event_id(self, event_id: int) -> None:
        event = await self.db.get_event_by_id(event_id)
        if event is None:
            return
        await self._refresh_related_slowline_from_event(event)

    async def _refresh_slowlines(self) -> None:
        today = shanghai_now().date().isoformat()
        key_records = list(reversed(await self.db.get_recent_key_records(limit=24, include_archived=False)))
        events = list(reversed(await self.db.get_recent_events_before_date(today, limit=24, include_archived=False)))
        for record in key_records:
            try:
                await self._refresh_related_slowline_from_key_record(record)
            except Exception:
                logger.exception("Failed to refresh slowline from key record #%s", record.id)
        for event in events:
            try:
                await self._refresh_related_slowline_from_event(event)
            except Exception:
                logger.exception("Failed to refresh slowline from event #%s", event.id)

    async def _load_recent_injection_materials(
        self,
        *,
        today_str: str,
        yesterday_str: str,
        today_limit: int,
        yesterday_limit: int,
    ) -> tuple[list[LifeFlowTrace], list[EventAnchor]]:
        traces = await self.db.get_recent_life_flow_traces(
            limit=8,
            start_date=yesterday_str,
            end_date=today_str,
        )
        today_events = await self.db.get_events_by_date(
            date_str=today_str,
            limit=max(1, today_limit),
            include_archived=False,
            order_by_importance=False,
        )
        yesterday_events = await self.db.get_events_by_date(
            date_str=yesterday_str,
            limit=max(1, yesterday_limit),
            include_archived=False,
            order_by_importance=True,
        )
        return traces, today_events + yesterday_events

    def _infer_event_theme(self, event: EventAnchor) -> str:
        payload = {
            "title": str(event.title or ""),
            "description": str(event.description or ""),
            "categories": str(event.categories or ""),
        }
        return self._infer_life_flow_theme(
            f"{payload['title']}\n{payload['description']}",
            payload,
            source=str(event.source or "generated"),
        )

    @staticmethod
    def _normalize_event_detail(text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "").strip())

    def _display_label_for_theme(self, theme: str) -> str:
        normalized = str(theme or "").strip().lower()
        return {
            "relationship": "关系/对话线",
            "medical": "医疗线",
            "study": "学习线",
            "work": "工作/项目线",
            "economic": "经济线",
            "economy": "经济线",
            "mobility": "出行线",
            "routine": "生活节律线",
            "conversation": "对话线",
            "general": "日常推进线",
        }.get(normalized, str(theme or "").strip() or "持续生活线")

    def _theme_matches_slowline(self, theme: str, slowline: SlowLine) -> bool:
        line_theme = str(slowline.theme or "").strip().lower()
        theme_text = str(theme or "").strip().lower()
        if not line_theme or not theme_text:
            return False
        if line_theme == theme_text:
            return True
        if theme_text in line_theme or line_theme in theme_text:
            return True
        fallback_map = {
            "relationship": ("关系", "对话", "承诺", "原则"),
            "medical": ("医疗", "复诊", "健康", "监测"),
            "study": ("学习", "论文", "求职", "课程"),
            "work": ("工作", "项目", "协作"),
            "routine": ("生活", "节律", "作息"),
        }
        return any(keyword in line_theme for keyword in fallback_map.get(theme_text, ()))

    def _compute_mainline_score(self, group: dict, *, now: datetime | None = None) -> float:
        summary = str(group.get("summary") or "")
        recent_shift = str(group.get("recent_shift") or "")
        trajectory = str(group.get("trajectory") or "")
        scope = str(group.get("scope") or "shared")
        source_family = str(group.get("source_family") or "daily_life")
        memory_role = str(group.get("memory_role") or "active_thread_detail")
        recency = self._compute_recency_score(str(group.get("last_shift_at") or ""), now=now)
        tension = self._score_level(str(group.get("tension_level") or "medium"))
        unresolved = self._score_level(str(group.get("unresolved_level") or "medium"))
        relationship = self._compute_relationship_score(scope, source_family)
        embodiment = self._compute_embodiment_score("\n".join([summary, recent_shift]))
        continuity = min(1.0, 0.18 + 0.12 * len(group.get("events") or []) + 0.08 * len(group.get("records") or []))
        novelty = self._compute_novelty_score(summary=summary or trajectory, recent_shift=recent_shift)
        archive_penalty = self._compute_archive_penalty(
            text="\n".join([summary, trajectory, recent_shift]),
            source_family=source_family,
            memory_role=memory_role,
        )
        score = (
            tension * 0.22
            + unresolved * 0.2
            + relationship * 0.18
            + recency * 0.16
            + embodiment * 0.12
            + continuity * 0.07
            + novelty * 0.05
        ) - archive_penalty * 0.24
        return max(0.0, min(1.0, score))

    def _compute_bridge_score(
        self,
        slowline: SlowLine,
        *,
        context_blob: str,
        excluded_thread_keys: set[str],
        now: datetime | None = None,
    ) -> float:
        thread_key = self._normalize_thread_key(str(getattr(slowline, "thread_key", "") or ""))
        if thread_key and thread_key in excluded_thread_keys:
            return 0.0
        memory_role = str(getattr(slowline, "memory_role", "active_thread_detail") or "active_thread_detail")
        source_family = str(getattr(slowline, "source_family", "daily_life") or "daily_life")
        scope = str(getattr(slowline, "scope", "shared") or "shared")
        summary_blob = "\n".join(
            [
                str(getattr(slowline, "trajectory_summary", "") or ""),
                str(getattr(slowline, "recent_shift_summary", "") or ""),
                str(getattr(slowline, "current_tension", "") or ""),
                ", ".join(self._parse_json_list(getattr(slowline, "open_questions", "[]"))[:2]),
            ]
        )
        recency = self._compute_recency_score(
            str(getattr(slowline, "last_meaningful_shift_at", "") or getattr(slowline, "last_touched_at", "") or ""),
            now=now,
        )
        continuity = self._compute_continuity_score(slowline)
        unresolved = self._score_level(str(getattr(slowline, "unresolved_level", "medium") or "medium"))
        relationship = self._compute_relationship_score(scope, source_family)
        current_relevance = self._compute_current_relevance_score(text=summary_blob, context_blob=context_blob)
        historical_depth = self._compute_historical_depth_score(slowline, now=now)
        archive_penalty = self._compute_archive_penalty(text=summary_blob, source_family=source_family, memory_role=memory_role)
        mainline_overlap_penalty = min(1.0, self._compute_current_relevance_score(text=summary_blob, context_blob=context_blob) if recency > 0.68 else 0.0)
        score = (
            continuity * 0.22
            + unresolved * 0.18
            + relationship * 0.14
            + current_relevance * 0.2
            + historical_depth * 0.18
            + max(0.0, 1.0 - recency) * 0.1
        ) - archive_penalty * 0.28 - mainline_overlap_penalty * 0.12
        return max(0.0, min(1.0, score))

    def _score_mainline_detail_candidate(
        self,
        *,
        text: str,
        summary: str,
        scope: str,
        source_family: str,
        kind: str,
    ) -> float:
        compact = self._compact_structured_memory_text(text)
        if not compact:
            return 0.0
        if self._looks_like_archive_payload(compact):
            return 0.0
        score = 0.16
        if self._keyword_overlap_score(compact.lower(), summary.lower()) < 0.82:
            score += 0.16
        score += {"low": 0.02, "medium": 0.12, "high": 0.28}.get(self._infer_tension_level(compact), 0.08)
        if any(token in compact for token in ("说", "问", "回", "抱", "摸", "疼", "哭", "吃", "睡", "停", "看", "去")):
            score += 0.14
        if any(token in compact for token in ("转向", "决定", "拒绝", "改写", "确认", "约定", "进入", "报告", "出现")):
            score += 0.16
        if scope in {"user_side", "shared"}:
            score += 0.12
        if source_family == "relationship":
            score += 0.08
        if kind == "event":
            score += 0.05
        return score

    def _score_fragment_candidate(
        self,
        *,
        text: str,
        scope: str,
        source_family: str,
    ) -> float:
        compact = self._compact_structured_memory_text(text)
        if not compact:
            return 0.0
        if self._looks_like_archive_payload(compact):
            return 0.0
        score = 0.1
        score += {"low": 0.02, "medium": 0.1, "high": 0.18}.get(self._infer_tension_level(compact), 0.06)
        if len(compact) >= 36:
            score += 0.08
        if any(token in compact for token in ("凌晨", "中午", "下午", "傍晚", "深夜", "返回", "下课", "终端", "布洛芬", "生理期")):
            score += 0.1
        if scope in {"user_side", "shared"}:
            score += 0.08
        if source_family == "relationship":
            score += 0.06
        return score

    async def _collect_recent_mainline_groups(self, today_str: str, yesterday_str: str) -> list[dict]:
        traces, events = await self._load_recent_injection_materials(
            today_str=today_str,
            yesterday_str=yesterday_str,
            today_limit=8,
            yesterday_limit=8,
        )
        recent_key_records = await self.db.get_recent_key_records(limit=16, include_archived=False)
        slowlines = await self.db.list_slowlines(status="active", limit=32)
        groups: list[dict] = []
        for slowline in slowlines:
            memory_role = str(getattr(slowline, "memory_role", "active_thread_detail") or "active_thread_detail")
            if memory_role in {"trigger_only", "archive_reference"}:
                continue
            existing_events, existing_records = await self._load_thread_all_materials(slowline)
            if not existing_events and not existing_records:
                if getattr(slowline, "id", None) is not None:
                    await self.db.update_slowline(int(slowline.id), status="archived")
                continue
            if self._looks_like_archive_payload(
                "\n".join(
                    [
                        str(slowline.theme or ""),
                        str(slowline.stage_summary or ""),
                        str(getattr(slowline, "trajectory_summary", "") or ""),
                        str(slowline.current_tension or ""),
                    ]
                )
            ) and memory_role != "bridge_core":
                continue
            line_traces: list[LifeFlowTrace] = []
            line_events: list[EventAnchor] = []
            line_records: list[KeyRecord] = []
            thread_key = str(getattr(slowline, "thread_key", "") or "").strip()
            for trace in traces:
                details = self._parse_life_flow_details(trace.details_json)
                theme = str(
                    details.get("life_theme")
                    or self._infer_life_flow_theme(str(trace.summary or ""), details, source=str(trace.source or ""))
                )
                if self._theme_matches_slowline(theme, slowline):
                    line_traces.append(trace)
            for event in events:
                payload = self._parse_json_object(getattr(event, "meta_json", None))
                if thread_key and self._normalize_thread_key(str(payload.get("thread_id") or "").strip()) == thread_key:
                    line_events.append(event)
                    continue
                if self._theme_matches_slowline(self._infer_event_theme(event), slowline):
                    line_events.append(event)
            for record in recent_key_records:
                payload = self._parse_json_object(record.content_json)
                if thread_key and self._normalize_thread_key(str(payload.get("thread_id") or "").strip()) == thread_key:
                    line_records.append(record)
                    continue
                record_theme = self._build_slowline_theme(
                    title=str(record.title or ""),
                    source_family=self._slowline_family_from_record(record),
                )
                if self._theme_matches_slowline(record_theme, slowline):
                    line_records.append(record)
            if not line_traces and not line_events and not line_records:
                continue
            label = self._resolve_thread_label(slowline, events=existing_events, records=existing_records)
            groups.append(
                {
                    "thread_key": self._normalize_thread_key(thread_key),
                    "label": label,
                    "scope": str(getattr(slowline, "scope", "shared") or "shared"),
                    "source_family": str(getattr(slowline, "source_family", "daily_life") or "daily_life"),
                    "memory_role": memory_role,
                    "progress_status": str(getattr(slowline, "progress_status", "open") or "open"),
                    "tension_level": str(getattr(slowline, "tension_level", "medium") or "medium"),
                    "unresolved_level": str(getattr(slowline, "unresolved_level", "medium") or "medium"),
                    "preload_priority": float(getattr(slowline, "preload_priority", 0.5) or 0.5),
                    "summary": self._compact_structured_memory_text(
                        str(slowline.stage_summary or slowline.recent_movement_summary or "").strip()
                    ),
                    "trajectory": self._compact_structured_memory_text(
                        str(getattr(slowline, "trajectory_summary", "") or "").strip()
                    ),
                    "recent_shift": self._compact_structured_memory_text(
                        str(getattr(slowline, "recent_shift_summary", "") or getattr(slowline, "recent_movement_summary", "") or "").strip()
                    ),
                    "open_question": self._compact_structured_memory_text(
                        ", ".join(self._parse_json_list(slowline.open_questions)[:1])
                    ),
                    "events": line_events[:6],
                    "traces": line_traces[:4],
                    "records": line_records[:4],
                    "salience": float(slowline.salience or 0.0),
                    "last_shift_at": str(getattr(slowline, "last_meaningful_shift_at", "") or getattr(slowline, "last_touched_at", "") or ""),
                }
            )
        return groups

    async def _build_relationship_state_text(self) -> str:
        state = await self._refresh_relationship_state()
        topics = self._parse_json_list(state.proactive_topics)
        tendency = "靠近"
        if state.space_need_level >= 0.7:
            tendency = "保留空间"
        elif state.concern_level >= 0.72:
            tendency = "想确认近况"
        elif state.pride_or_distance >= 0.62:
            tendency = "观察"
        hours_gap = float(getattr(state, "hours_since_meaningful_contact", 0.0) or 0.0)
        hours_text = f"{hours_gap:.1f}".rstrip("0").rstrip(".")
        lines = [
            f"- 最近实质联系间隔：{hours_text} 小时（{state.contact_recency_bucket}）",
            f"- 当前关系感受：{str(state.relationship_feeling_summary or '').strip() or '关系处在可感知但克制的短期波动中。'}",
            f"- 当前更偏向：{tendency}",
        ]
        if topics:
            lines.append("- 可主动开启的话题：")
            lines.extend(f"  - {topic}" for topic in topics[:3])
        return "\n".join(lines)



    async def _build_recent_life_line_digest(self, today_str: str, yesterday_str: str) -> str:
        groups = await self._collect_recent_mainline_groups(today_str, yesterday_str)
        if not groups:
            return "（近两日暂无可聚合的生活主线）"
        now_dt = shanghai_now()
        ordered_groups = sorted(groups, key=lambda item: self._compute_mainline_score(item, now=now_dt), reverse=True)
        lines: list[str] = []
        used_detail_fingerprints: set[str] = set()
        for group in ordered_groups[:4]:
            label = str(group.get("label") or "生活线")
            summary = self._strip_title_prefix(label, str(group.get("summary") or "").strip())
            if not summary:
                continue
            status_hint = {
                "paused": "已被压住",
                "ready_to_close": "接近收束",
                "completed": "阶段完成",
                "dropped": "暂时放弃",
            }.get(str(group.get("progress_status") or ""), "持续推进")
            lines.append(f"【{label}】{summary}（{status_hint}）")
            trajectory = self._strip_title_prefix(label, str(group.get("trajectory") or "").strip())
            if trajectory and self._keyword_overlap_score(trajectory.lower(), summary.lower()) < 0.72:
                lines.append(f"纵向概括：{trajectory}")
            detail_candidates: list[tuple[float, str]] = []
            for trace in group.get("traces") or []:
                trace_text = self._extract_display_core(str(trace.summary or "").strip(), max_len=160)
                score = self._score_mainline_detail_candidate(
                    text=trace_text,
                    summary=summary,
                    scope=str(group.get("scope") or "shared"),
                    source_family=str(group.get("source_family") or "daily_life"),
                    kind="trace",
                )
                if score > 0.18:
                    if self._keyword_overlap_score(trace_text.lower(), summary.lower()) < 0.9:
                        detail_candidates.append((score, f"  - [{trace.trace_date}] {trace_text}"))
            for event in group.get("events") or []:
                if not isinstance(event, EventAnchor):
                    continue
                title = (event.title or "").strip() or "未命名事件"
                desc = self._extract_display_core(self._normalize_event_detail(event.description or event.title or ""), max_len=180)
                score = self._score_mainline_detail_candidate(
                    text=f"{title}\n{desc}",
                    summary=summary,
                    scope=str(group.get("scope") or "shared"),
                    source_family=str(group.get("source_family") or "daily_life"),
                    kind="event",
                )
                if score > 0.18:
                    if self._keyword_overlap_score(desc.lower(), summary.lower()) < 0.88:
                        detail_candidates.append((score, f"  - [{event.date}] {title}：{desc}"))
            for record in group.get("records") or []:
                if not isinstance(record, KeyRecord):
                    continue
                payload = self._parse_json_object(record.content_json)
                if str(payload.get("memory_role") or "") in {"trigger_only", "archive_reference"}:
                    continue
                if self._looks_like_archive_payload(
                    "\n".join(
                        [
                            str(record.title or ""),
                            str(payload.get("latest_change") or ""),
                            str(payload.get("next_watch_point") or ""),
                            str(record.content_text or ""),
                        ]
                    )
                ):
                    continue
                record_text = self._extract_display_core(
                    str(payload.get("latest_change") or record.content_text or "").strip(),
                    max_len=140,
                )
                score = self._score_mainline_detail_candidate(
                    text=f"{record.title}\n{record_text}",
                    summary=summary,
                    scope=str(group.get("scope") or "shared"),
                    source_family=str(group.get("source_family") or "daily_life"),
                    kind="record",
                )
                if score > 0.22:
                    if self._keyword_overlap_score(record_text.lower(), summary.lower()) < 0.88:
                        detail_candidates.append((score, f"  - {record.title}：{record_text}"))
            detail_lines: list[str] = []
            for _, line in sorted(detail_candidates, key=lambda item: item[0], reverse=True):
                fp = self._detail_fingerprint(line)
                if not fp or fp in used_detail_fingerprints:
                    continue
                used_detail_fingerprints.add(fp)
                detail_lines.append(line)
                if len(detail_lines) >= 3:
                    break
            if detail_lines:
                lines.append("关键细节：")
                lines.extend(detail_lines)
            open_question = self._compact_structured_memory_text(str(group.get("open_question") or "").strip())
            if open_question:
                lines.append(f"待续：{open_question}")
            lines.append("")
        return "\n".join(line for line in lines if line is not None).strip() if lines else "（近两日暂无可聚合的生活主线）"

    async def _build_recent_turning_details_text(
        self,
        *,
        today_str: str,
        yesterday_str: str,
        today_limit: int,
        yesterday_limit: int,
    ) -> str:
        traces, events = await self._load_recent_injection_materials(
            today_str=today_str,
            yesterday_str=yesterday_str,
            today_limit=today_limit,
            yesterday_limit=yesterday_limit,
        )
        slowline_text = await self._build_recent_life_line_digest(today_str, yesterday_str)
        reference = slowline_text.lower()
        used_detail_fingerprints = self._collect_recent_life_detail_fingerprints(slowline_text)
        fragment_candidates: list[tuple[float, str]] = []
        for trace in traces:
            summary = self._extract_display_core(str(trace.summary or "").strip(), max_len=180)
            if not summary:
                continue
            if self._keyword_overlap_score(summary.lower(), reference) >= 0.72:
                continue
            if self._detail_fingerprint(summary) in used_detail_fingerprints:
                continue
            source_family = self._source_family_for_trace(trace)
            scope = "user_side" if source_family in {"health", "study", "relationship"} else "character_side"
            score = self._score_fragment_candidate(text=summary, scope=scope, source_family=source_family)
            if score > 0.12:
                fragment_candidates.append((score, f"- [{trace.trace_date}] {summary}"))
        for event in events:
            title = (event.title or "").strip() or "未命名事件"
            desc = self._extract_display_core(self._normalize_event_detail(event.description or ""), max_len=200)
            combined = f"{title}\n{desc}".lower()
            if self._keyword_overlap_score(combined, reference) >= 0.68:
                continue
            if self._detail_fingerprint(desc) in used_detail_fingerprints:
                continue
            payload = self._parse_json_object(getattr(event, "meta_json", None))
            if str(payload.get("memory_role") or "") in {"bridge_core", "trigger_only", "archive_reference"}:
                continue
            scope = str(payload.get("scope") or "shared")
            source_family = self._source_family_for_event(event)
            score = self._score_fragment_candidate(text=f"{title}\n{desc}", scope=scope, source_family=source_family)
            if score > 0.12:
                fragment_candidates.append((score, f"- [{event.date}] {title}：{desc}"))
        deduped: list[str] = []
        seen: set[str] = set()
        for _, line in sorted(fragment_candidates, key=lambda item: item[0], reverse=True):
            key = re.sub(r"\s+", " ", line.strip().lower())
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(line)
        return "\n".join(deduped[:6]) if deduped else "（近景碎片已被上方主线充分覆盖）"

    async def _load_thread_history_materials(
        self,
        slowline: SlowLine,
        *,
        before_date: str,
    ) -> tuple[list[EventAnchor], list[KeyRecord]]:
        events: list[EventAnchor] = []
        records: list[KeyRecord] = []
        for event_id in self._json_int_list(getattr(slowline, "linked_event_ids", "[]")):
            event = await self.db.get_event_by_id(event_id)
            if event is None or str(event.date or "") >= before_date:
                continue
            if getattr(event, "archived", 0):
                continue
            events.append(event)
        for record_id in self._json_int_list(getattr(slowline, "linked_key_record_ids", "[]")):
            record = await self.db.get_key_record_by_id(record_id)
            if record is None:
                continue
            start_marker = str(record.start_date or record.updated_at or record.created_at or "")[:10]
            if start_marker and start_marker >= before_date:
                continue
            if str(record.status or "") == "archived":
                continue
            records.append(record)
        events.sort(key=lambda item: (str(item.date or ""), str(item.created_at or "")))
        records.sort(key=lambda item: (str(item.start_date or item.updated_at or item.created_at or ""), int(item.id or 0)))
        return events, records

    async def _load_thread_all_materials(self, slowline: SlowLine) -> tuple[list[EventAnchor], list[KeyRecord]]:
        events: list[EventAnchor] = []
        records: list[KeyRecord] = []
        for event_id in self._json_int_list(getattr(slowline, "linked_event_ids", "[]")):
            event = await self.db.get_event_by_id(event_id)
            if event is None or getattr(event, "archived", 0):
                continue
            events.append(event)
        for record_id in self._json_int_list(getattr(slowline, "linked_key_record_ids", "[]")):
            record = await self.db.get_key_record_by_id(record_id)
            if record is None or str(record.status or "") == "archived":
                continue
            records.append(record)
        events.sort(key=lambda item: (str(item.date or ""), str(item.created_at or ""), int(item.id or 0)))
        records.sort(key=lambda item: (str(item.start_date or item.updated_at or item.created_at or ""), int(item.id or 0)))
        return events, records

    def _resolve_thread_label(
        self,
        slowline: SlowLine,
        *,
        events: list[EventAnchor] | None = None,
        records: list[KeyRecord] | None = None,
    ) -> str:
        label = self._compact_structured_memory_text(str(slowline.theme or "").strip())
        if label and label.lower() not in {"relationship", "study", "work", "health", "daily_life", "logistics"}:
            return label
        for record in records or []:
            title = self._compact_structured_memory_text(str(record.title or "").strip())
            if title and title.lower() not in {"relationship", "study", "work", "health", "daily_life", "logistics"}:
                return title
        for event in events or []:
            title = self._compact_structured_memory_text(str(event.title or "").strip())
            if title and title.lower() not in {"relationship", "study", "work", "health", "daily_life", "logistics"}:
                return title
        return label or "持续生活线"

    def _build_bridge_line_from_thread(
        self,
        *,
        slowline: SlowLine,
        older_events: list[EventAnchor],
        older_records: list[KeyRecord],
        label_override: str | None = None,
    ) -> str:
        label = self._compact_structured_memory_text(str(label_override or slowline.theme or "").strip()) or "持续生活线"
        start_text = ""
        shift_text = ""
        if older_records:
            first_payload = self._parse_json_object(older_records[0].content_json)
            start_text = self._extract_display_core(
                str(first_payload.get("previous_baseline") or older_records[0].content_text or older_records[0].title or ""),
                max_len=100,
            )
            last_record_payload = self._parse_json_object(older_records[-1].content_json)
            shift_text = self._extract_display_core(
                str(last_record_payload.get("latest_change") or older_records[-1].content_text or older_records[-1].title or ""),
                max_len=110,
            )
        if older_events:
            if not start_text:
                start_text = self._extract_display_core(
                    self._normalize_event_detail(older_events[0].description or older_events[0].title or ""),
                    max_len=100,
                )
            shift_text = self._extract_display_core(
                self._normalize_event_detail(older_events[-1].description or older_events[-1].title or ""),
                max_len=110,
            ) or shift_text
        if not start_text:
            start_text = self._extract_display_core(str(getattr(slowline, "trajectory_summary", "") or ""), max_len=100)
        if not shift_text:
            shift_text = self._extract_display_core(
                str(getattr(slowline, "recent_shift_summary", "") or getattr(slowline, "recent_movement_summary", "") or ""),
                max_len=110,
            )
        tension = self._extract_display_core(
            str(slowline.current_tension or "") or ", ".join(self._parse_json_list(getattr(slowline, "open_questions", "[]"))[:1]),
            max_len=90,
        )
        parts = [f"- {label}：起点：{start_text}"] if start_text else [f"- {label}"]
        if shift_text and self._keyword_overlap_score(shift_text.lower(), start_text.lower()) < 0.78:
            parts.append(f"历史偏转：{shift_text}")
        if tension:
            parts.append(f"今日牵引：{tension}")
        return "；".join(parts)

    async def _build_memory_bridge_text(self, *, before_date: str, limit: int = 5, context_blob: str = "") -> str:
        now_dt = shanghai_now()
        yesterday_str = (now_dt.date() - timedelta(days=1)).isoformat()
        recent_groups = await self._collect_recent_mainline_groups(now_dt.date().isoformat(), yesterday_str)
        ordered_mainlines = sorted(recent_groups, key=lambda item: self._compute_mainline_score(item, now=now_dt), reverse=True)
        excluded_thread_keys = {
            str(group.get("thread_key") or "").strip()
            for group in ordered_mainlines[:4]
            if str(group.get("thread_key") or "").strip()
        }
        current_context_blob = "\n".join(
            [
                context_blob,
                " ".join(str(group.get("summary") or "") for group in ordered_mainlines[:4]),
                " ".join(str(group.get("recent_shift") or "") for group in ordered_mainlines[:4]),
            ]
        )
        slowlines = await self.db.list_slowlines(status="active", limit=48)
        candidates: list[tuple[float, SlowLine]] = []
        for slowline in slowlines:
            existing_events, existing_records = await self._load_thread_all_materials(slowline)
            if not existing_events and not existing_records:
                if getattr(slowline, "id", None) is not None:
                    await self.db.update_slowline(int(slowline.id), status="archived")
                continue
            score = self._compute_bridge_score(
                slowline,
                context_blob=current_context_blob,
                excluded_thread_keys=excluded_thread_keys,
                now=now_dt,
            )
            if score <= 0.24:
                continue
            candidates.append((score, slowline))
        lines: list[str] = []
        for _, slowline in sorted(candidates, key=lambda item: item[0], reverse=True):
            older_events, older_records = await self._load_thread_history_materials(slowline, before_date=before_date)
            if not older_events and not older_records:
                continue
            label = self._resolve_thread_label(slowline, events=older_events, records=older_records)
            line = self._build_bridge_line_from_thread(
                slowline=slowline,
                older_events=older_events,
                older_records=older_records,
                label_override=label,
            )
            if not line or self._looks_like_archive_payload(line):
                continue
            lines.append(line)
            if len(lines) >= 4:
                break
        if lines:
            return "\n".join(lines[:4])
        events = await self.db.get_recent_events_before_date(before_date, limit=limit, include_archived=False)
        older_events = [event for event in events if str(event.date or "") < before_date]
        traces = await self.db.get_recent_life_flow_traces(limit=limit + 2)
        older_traces = [trace for trace in traces if str(trace.trace_date or "") < before_date]
        lines: list[str] = []
        if older_traces:
            lines.append("更早的生活背景仍延续在这些阶段线上：")
            for trace in older_traces[:2]:
                lines.append(f"- [{trace.trace_date}] {self._compact_structured_memory_text(str(trace.summary or '').strip())}")
        if older_events:
            lines.append("更早但仍有影响的事件锚点包括：")
            for event in older_events[:3]:
                title = (event.title or "").strip() or "未命名事件"
                desc = self._compact_structured_memory_text(str(event.description or "").strip())
                if desc:
                    lines.append(f"- [{event.date}] {title}：{desc}")
                else:
                    lines.append(f"- [{event.date}] {title}")
        return "\n".join(lines) if lines else "（更早的背景暂时已被近景主线充分吸收）"

    async def _build_injectable_context(self, snapshot_text: str) -> str:
        l1_char = await self.prompt_manager.get_layer_content(KEY_L1_CHARACTER_BACKGROUND)
        l1_user = await self.prompt_manager.get_layer_content(KEY_L1_USER_BACKGROUND)
        l2_char = await self.prompt_manager.get_layer_content(KEY_L2_CHARACTER_PERSONALITY)
        l2_rel = await self.prompt_manager.get_layer_content(KEY_L2_RELATIONSHIP_DYNAMICS)
        l2_life = await self.prompt_manager.get_layer_content(KEY_L2_LIFE_STATUS)
        today_limit = await self._get_inject_hot_events_limit()
        yesterday_limit = await self._get_inject_yesterday_events_limit()
        now_shanghai = shanghai_now()
        today_str = now_shanghai.date().isoformat()
        yesterday_str = (now_shanghai.date() - timedelta(days=1)).isoformat()
        plan_summary = "（今日尚无计划）"
        if self.plan_engine is not None:
            try:
                plan_summary = await self.plan_engine.get_plan_summary_text()
            except Exception:
                logger.exception("Failed to load plan summary for injectable context.")
        recent_trace_text = await self._build_recent_life_line_digest(today_str, yesterday_str)
        recent_event_detail_text = await self._build_recent_turning_details_text(
            today_str=today_str,
            yesterday_str=yesterday_str,
            today_limit=today_limit,
            yesterday_limit=yesterday_limit,
        )
        relationship_block = await self._build_relationship_state_text()
        memory_bridge_text = await self._build_memory_bridge_text(
            before_date=yesterday_str,
            context_blob="\n".join([snapshot_text, recent_trace_text, relationship_block]),
        )
        memory_bridge_text = self._dedupe_memory_bridge_text(memory_bridge_text, recent_trace_text)
        schedule_block = plan_summary.strip() or "（今日尚无计划）"
        return (
            "【L1 稳定层】\n"
            f"角色背景：{l1_char}\n\n"
            f"用户背景：{l1_user}\n\n"
            "【L2 动态层】\n"
            f"角色人格：{l2_char}\n\n"
            f"关系模式：{l2_rel}\n\n"
            f"生活状态：{l2_life}\n\n"
            "【近程记忆桥】\n"
            f"{memory_bridge_text}\n\n"
            "【近期生活主线】\n"
            f"{recent_trace_text}\n\n"
            "【近景碎片池】\n"
            f"{recent_event_detail_text}\n\n"
            "【当前日程主条目】\n"
            f"{schedule_block}\n\n"
            "【当前状态快照】\n"
            f"{snapshot_text}\n\n"
            "【短期关系感知】\n"
            f"{relationship_block}"
        )

    async def _build_recent_events_text(self, limit: int = 2) -> str:
        events = await self.db.get_recent_events_by_event_time(
            limit=max(1, limit),
            include_archived=False,
        )
        if not events:
            return "（暂无近期事件）"
        lines: list[str] = []
        # Use event's own time field first, then created_at/id as tie-breakers.
        for event in events:
            title = (event.title or "").strip() or "未命名事件"
            desc = (event.description or "").strip()
            lines.append(f"- [{event.date}] {title}：{desc}")
        return "\n".join(lines)

    async def _build_today_events_text(self, today_str: str, limit: int = 5) -> str:
        """Return today's events in chronological order (up to limit)."""
        events = await self.db.get_events_by_date(
            date_str=today_str,
            limit=max(1, limit),
            include_archived=False,
            order_by_importance=False,
        )
        if not events:
            return "（今日暂无事件）"
        lines: list[str] = []
        for event in events:
            title = (event.title or "").strip() or "未命名事件"
            desc = (event.description or "").strip()
            lines.append(f"- {title}：{desc}")
        return "\n".join(lines)

    async def _build_yesterday_events_text(self, yesterday_str: str, limit: int = 5) -> str:
        """Return yesterday's top-K events ordered by importance_score DESC."""
        events = await self.db.get_events_by_date(
            date_str=yesterday_str,
            limit=max(1, limit),
            include_archived=False,
            order_by_importance=True,
        )
        if not events:
            return "（昨日无事件记录）"
        lines: list[str] = []
        for event in events:
            title = (event.title or "").strip() or "未命名事件"
            desc = (event.description or "").strip()
            lines.append(f"- {title}：{desc}")
        return "\n".join(lines)

    async def recall_memories(self, query: str, top_k: int = 5) -> list[dict]:
        results = await self.memory.search(query, top_k=top_k)
        return [
            {
                "id": r.id,
                "text": r.text,
                "source_type": r.source_type,
                "metadata": r.metadata,
            }
            for r in results
        ]

    async def upsert_key_record(
        self,
        record_type: str | None,
        title: str,
        content_text: str,
        tags: list[str] | None = None,
        content_json: dict | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        status: str = "active",
        source: str = "conversation",
        linked_event_id: int | None = None,
        update_if_exists: bool = True,
    ) -> dict:
        normalized_type = self._normalize_key_record_type(record_type)
        if not normalized_type:
            normalized_type = self._classify_key_record_type(
                title=title,
                content_text=content_text,
                tags=tags or [],
                content_json=content_json,
                start_date=start_date,
                end_date=end_date,
            )
        tags = tags or []
        existing = None
        if update_if_exists:
            existing = await self._find_key_record_upsert_candidate(
                normalized_type=normalized_type,
                title=title,
                content_text=content_text,
                tags=tags,
                content_json=content_json,
                start_date=start_date,
                end_date=end_date,
            )
        if existing and update_if_exists:
            previous_record = existing.model_copy(deep=True)
            fields = {
                "type": normalized_type,
                "content_text": content_text,
                "content_json": json.dumps(content_json, ensure_ascii=False) if content_json is not None else None,
                "tags": json.dumps(tags, ensure_ascii=False),
                "start_date": start_date,
                "end_date": end_date,
                "status": status,
                "source": source,
                "linked_event_id": linked_event_id,
            }
            await self.db.update_key_record(existing.id, **fields)  # type: ignore[arg-type]
            upsert_method = getattr(self.memory, "upsert_key_record_vector", None)
            if callable(upsert_method):
                try:
                    await upsert_method(int(existing.id or 0))  # type: ignore[arg-type]
                except Exception:
                    logger.exception("Failed to refresh key record vector: %s", existing.id)
            updated = await self.db.get_key_record_by_id(existing.id)  # type: ignore[arg-type]
            if updated is not None:
                try:
                    await self._refresh_related_slowline_from_key_record(updated, previous_record=previous_record)
                except Exception:
                    logger.exception("Failed to refresh slowline after key record update: %s", existing.id)
            return {
                "action": "updated",
                "record": updated.model_dump() if updated else existing.model_dump(),
            }

        now = datetime.utcnow().isoformat()
        record = KeyRecord(
            type=normalized_type,  # type: ignore[arg-type]
            title=title,
            content_text=content_text,
            content_json=json.dumps(content_json, ensure_ascii=False) if content_json is not None else None,
            tags=json.dumps(tags, ensure_ascii=False),
            start_date=start_date,
            end_date=end_date,
            status=status,  # type: ignore[arg-type]
            source=source,  # type: ignore[arg-type]
            linked_event_id=linked_event_id,
            created_at=now,
            updated_at=now,
        )
        record_id = await self.db.insert_key_record(record)
        upsert_method = getattr(self.memory, "upsert_key_record_vector", None)
        if callable(upsert_method):
            try:
                await upsert_method(int(record_id))
            except Exception:
                logger.exception("Failed to create key record vector: %s", record_id)
        created = await self.db.get_key_record_by_id(record_id)
        if created is not None:
            try:
                await self._refresh_related_slowline_from_key_record(created)
            except Exception:
                logger.exception("Failed to refresh slowline after key record create: %s", record_id)
        return {
            "action": "created",
            "record": created.model_dump() if created else {"id": record_id, "title": title, "type": normalized_type},
        }

    async def upsert_event(
        self,
        *,
        title: str = "",
        objective: str,
        impression: str,
        date: str | None = None,
        keywords: list[str] | None = None,
        categories: list[str] | None = None,
        source: str = "conversation",
        update_if_exists: bool = True,
    ) -> dict:
        objective_text = str(objective or "").strip()
        impression_text = str(impression or "").strip()
        if not objective_text:
            raise ValueError("objective 不能为空")
        if not impression_text:
            raise ValueError("impression 不能为空")

        event_date = str(date or shanghai_now().date().isoformat()).strip()
        keyword_list = [str(k).strip() for k in (keywords or []) if str(k).strip()]
        description = self._compose_event_description(objective_text, impression_text)
        category_list = [
            str(c).strip() for c in (categories or []) if str(c).strip()
        ] or classify_event(description, keyword_list)
        normalized_title = str(title or "").strip() or make_event_title(
            objective_text,
            keyword_list,
            category_list,
        )

        existing = None
        if update_if_exists:
            existing = await self._find_event_upsert_candidate(
                event_date=event_date,
                normalized_title=normalized_title,
                objective_text=objective_text,
                impression_text=impression_text,
                keyword_list=keyword_list,
                category_list=category_list,
            )
        if existing and update_if_exists:
            fields = {
                "description": description,
                "source": source,
                "trigger_keywords": json.dumps(keyword_list, ensure_ascii=False),
                "categories": json.dumps(category_list, ensure_ascii=False),
            }
            await self.db.update_event(existing.id, **fields)  # type: ignore[arg-type]
            updated = await self.db.get_event_by_id(existing.id)  # type: ignore[arg-type]
            event_id = int((updated or existing).id or 0)
            result = {
                "action": "updated",
                "record": updated.model_dump() if updated else existing.model_dump(),
            }
            upsert_event_vector = getattr(self.memory, "upsert_event_vector", None)
            if callable(upsert_event_vector) and event_id > 0:
                try:
                    await upsert_event_vector(event_id)
                except Exception as exc:
                    logger.warning("Event vector upsert skipped for #%d: %s", event_id, exc)
            if updated is not None:
                try:
                    await self._refresh_related_slowline_from_event(updated)
                except Exception:
                    logger.exception("Failed to refresh slowline after event update: %s", event_id)
            return result

        now_shanghai = shanghai_now()
        event = EventAnchor(
            date=event_date,
            title=normalized_title,
            description=description,
            source=source,  # type: ignore[arg-type]
            created_at=format_utc_instant_z(shanghai_time_to_utc_naive(now_shanghai)),
            trigger_keywords=json.dumps(keyword_list, ensure_ascii=False),
            categories=json.dumps(category_list, ensure_ascii=False),
        )
        event_id = await self.db.insert_event(event)
        created = await self.db.get_event_by_id(event_id)
        result = {
            "action": "created",
            "record": created.model_dump() if created else {"id": event_id, "title": normalized_title},
        }

        upsert_event_vector = getattr(self.memory, "upsert_event_vector", None)
        if callable(upsert_event_vector):
            try:
                await upsert_event_vector(int(event_id))
            except Exception as exc:
                logger.warning("Event vector upsert skipped for #%d: %s", event_id, exc)
        if created is not None:
            try:
                await self._refresh_related_slowline_from_event(created)
            except Exception:
                logger.exception("Failed to refresh slowline after event create: %s", event_id)
        return result

    async def recall_key_records(
        self,
        query: str,
        top_k: int = 5,
        record_type: str | None = None,
        include_archived: bool = False,
        include_world_books: bool = True,
    ) -> list[dict]:
        tk = max(1, int(top_k))
        cap = max(tk * 4, 24)
        hinted_types = [record_type] if record_type else self._infer_key_record_types_from_query(query)
        rows = await self.db.search_key_records(
            query=query,
            top_k=cap,
            record_type=record_type,
            include_archived=include_archived,
        )
        if hinted_types and not record_type:
            extra_rows: list[KeyRecord] = []
            for hinted_type in hinted_types[:3]:
                extra_rows.extend(
                    await self.db.get_all_key_records(
                        offset=0,
                        limit=min(cap, 40),
                        record_type=hinted_type,
                        include_archived=include_archived,
                    )
                )
            existing_ids = {int(r.id or 0) for r in rows if int(r.id or 0) > 0}
            for row in extra_rows:
                row_id = int(row.id or 0)
                if row_id > 0 and row_id not in existing_ids:
                    rows.append(row)
                    existing_ids.add(row_id)
        rows_by_id: dict[int, KeyRecord] = {
            int(r.id or 0): r for r in rows if int(r.id or 0) > 0
        }
        search_kr = getattr(self.memory, "search_key_records", None)
        vector_scores: dict[int, float] = {}
        if callable(search_kr):
            try:
                candidate_ids = list(rows_by_id.keys()) or None
                hits = await search_kr(
                    query=query,
                    top_k=cap,
                    candidate_ids=candidate_ids,
                )
                for hit in hits:
                    rid = int(hit.get("id") or 0)
                    if rid > 0:
                        vector_scores[rid] = max(vector_scores.get(rid, 0.0), float(hit.get("score") or 0.0))
            except Exception:
                logger.exception("Key record vector recall failed.")

        kr_hint = (
            "【关键记录】用于承载对话中沉淀下来的结构化事实，例如约定、医嘱、计划、日期等，"
            "优先于下方设定条目采信。"
        )
        kr_list: list[dict] = []
        merged_rows = list(rows)
        if rows_by_id and vector_scores:
            missing_ids = [rid for rid in vector_scores if rid not in rows_by_id]
            if missing_ids:
                extra_rows = await self.db.get_key_records_by_ids(missing_ids)
                merged_rows.extend(extra_rows)
        seen_ids: set[int] = set()
        for r in merged_rows:
            rid = int(r.id or 0)
            if rid > 0 and rid in seen_ids:
                continue
            if rid > 0:
                seen_ids.add(rid)
            keyword_score = self._key_record_query_strength(
                query,
                r,
                hinted_types=hinted_types,
            )
            vector_score = vector_scores.get(rid, 0.0)
            s = round(max(keyword_score, vector_score * 0.98), 4)
            d = r.model_dump()
            d["tags"] = self._parse_json_list(r.tags)
            d["match_keywords"] = self._parse_json_list(getattr(r, "match_keywords", "[]"))
            d["vectorized"] = bool(str(getattr(r, "embedding_vector_id", "") or "").strip())
            d["_result_kind"] = "key_record"
            d["_memory_tier"] = "primary"
            d["_relevance_score"] = s
            d["_match_modes"] = [mode for mode, enabled in (("keyword", keyword_score > 0.25), ("vector", vector_score > 0.0)) if enabled]
            d["_usage_hint"] = kr_hint
            title = str(d.get("title") or "").strip() or "（未命名）"
            body = str(d.get("content_text") or "").strip()
            d["_content_for_prompt"] = f"【关键记录·优先采信】\n{title}\n{body}"
            d["_sort_recency"] = (r.updated_at or r.created_at or "").strip()
            kr_list.append(d)
        kr_list.sort(
            key=lambda d: (d.get("_relevance_score") or 0, d.get("_sort_recency") or ""),
            reverse=True,
        )
        for d in kr_list:
            d.pop("_sort_recency", None)

        wb_max = min(2, max(0, tk // 2))
        kr_slots = max(1 if kr_list else 0, tk - wb_max)
        out: list[dict] = []
        seen_kr: set[int] = set()
        for d in kr_list:
            if len(out) >= kr_slots:
                break
            rid = int(d.get("id") or 0)
            if rid in seen_kr:
                continue
            seen_kr.add(rid)
            out.append(d)

        if not include_world_books or wb_max <= 0:
            return out[:tk]

        books = await self.db.get_active_world_books()
        entries = [self._world_book_to_dict(b) for b in books]
        wb_scored: list[tuple[float, dict]] = []
        if entries:
            kw_scores = self._world_book_keyword_scores(query, entries)
            vec_by_id: dict[int, float] = {}
            search_wb = getattr(self.memory, "search_world_books", None)
            if callable(search_wb):
                try:
                    cands = [int(e.get("id") or 0) for e in entries if int(e.get("id") or 0) > 0]
                    hits = await search_wb(
                        query=query,
                        top_k=min(8, max(len(cands), 1)),
                        candidate_ids=cands or None,
                    )
                    for h in hits:
                        wid = int(h.get("id") or 0)
                        if wid > 0:
                            vec_by_id[wid] = max(
                                vec_by_id.get(wid, 0.0),
                                float(h.get("score") or 0.0),
                            )
                except Exception:
                    pass

            by_id: dict[int, dict] = {
                int(e["id"]): e for e in entries if int(e.get("id") or 0) > 0
            }
            wb_seen: set[int] = set()
            for wid, raw_kw in kw_scores.items():
                if wid <= 0 or wid not in by_id:
                    continue
                vec = vec_by_id.get(wid, 0.0)
                kw_n = min(1.0, raw_kw / 3.0)
                score = max(kw_n, vec * 0.95)
                modes: list[str] = []
                if raw_kw > 0:
                    modes.append("keyword")
                if vec > 0:
                    modes.append("vector")
                wb_scored.append(
                    (
                        score,
                        self._world_book_hit_dict(by_id[wid], score, modes),
                    )
                )
                wb_seen.add(wid)
            for wid, vec in vec_by_id.items():
                if wid <= 0 or wid in wb_seen or wid not in by_id:
                    continue
                wb_scored.append(
                    (vec * 0.95, self._world_book_hit_dict(by_id[wid], vec * 0.95, ["vector"]))
                )

        wb_scored.sort(key=lambda x: x[0], reverse=True)
        wb_hint = (
            "【世界书】是静态设定与背景参考，只作为补充。"
            "不要把它当成用户本轮新说出的事实。"
        )
        seen_wb: set[int] = set()
        for _score, item in wb_scored:
            if len(seen_wb) >= wb_max:
                break
            wid = int(item.get("id") or 0)
            if wid in seen_wb:
                continue
            seen_wb.add(wid)
            item["_memory_tier"] = "supplementary"
            item["_usage_hint"] = wb_hint
            out.append(item)

        return out[:tk]

    @staticmethod
    def _key_record_type_aliases() -> dict[str, list[str]]:
        return {
            "medication_protocol": ["用药", "药", "服药", "剂量", "吸入", "停药", "药单", "方案"],
            "health_monitoring": ["监测", "指标", "症状", "波动", "体征", "炎症", "数值"],
            "dietary_intervention": ["饮食", "食疗", "忌口", "营养", "早餐", "晚饭", "加餐"],
            "anniversary_date": ["纪念日", "周年", "生日", "节日"],
            "medical_review_date": ["复诊", "复查", "看诊", "门诊", "挂号", "医院"],
            "lifecycle_milestone": ["节点", "阶段", "变化", "转折", "里程碑"],
            "key_collaboration": ["协作", "分工", "任务", "交接", "计划", "项目", "同步"],
            "commitment_agreement": ["承诺", "约定", "协议", "共识", "规则", "原则"],
            "emotional_anchor": ["情绪", "安抚", "锚点", "安全感", "陪伴", "拥抱"],
            "life_pattern": ["习惯", "作息", "模式", "日常", "规律"],
        }

    @classmethod
    def _infer_key_record_types_from_query(cls, query: str) -> list[str]:
        text = str(query or "").strip().lower()
        if not text:
            return []
        hits: list[str] = []
        for record_type, aliases in cls._key_record_type_aliases().items():
            if any(alias.lower() in text for alias in aliases):
                hits.append(record_type)
        return hits

    @classmethod
    def _key_record_query_strength(
        cls,
        query: str,
        record: KeyRecord,
        *,
        hinted_types: list[str] | None = None,
    ) -> float:
        raw = (query or "").strip()
        if not raw:
            return 0.5
        kws = [k.strip() for k in re.split(r"[\s,，。;；、|/]+", raw) if k.strip()]
        if not kws:
            kws = [raw]
        title = str(record.title or "").lower()
        content = str(record.content_text or "").lower()
        tags = [str(x).strip().lower() for x in cls._parse_json_list(record.tags)]
        match_keywords = [str(x).strip().lower() for x in cls._parse_json_list(getattr(record, "match_keywords", "[]"))]
        content_json = str(record.content_json or "").lower()

        weighted = 0.0
        for kw in kws:
            kw_l = kw.lower()
            if kw_l in tags:
                weighted += 1.35
            elif any(kw_l in tag for tag in tags):
                weighted += 0.95
            if kw_l in match_keywords:
                weighted += 1.2
            elif any(kw_l in mk for mk in match_keywords):
                weighted += 0.85
            if kw_l in title:
                weighted += 1.05
            if kw_l in content:
                weighted += 0.55
            if kw_l in content_json:
                weighted += 0.35

        type_bonus = 0.0
        record_type = str(record.type or "").strip()
        if hinted_types and record_type in hinted_types:
            type_bonus += 0.9
        aliases = cls._key_record_type_aliases().get(record_type, [])
        if any(alias.lower() in raw.lower() for alias in aliases):
            type_bonus += 0.45

        score = (weighted / max(len(kws), 1)) + type_bonus
        return round(max(0.2, min(score, 2.5)), 4)

    @staticmethod
    def _world_book_hit_dict(entry: dict, score: float, modes: list[str]) -> dict:
        return {
            "_result_kind": "world_book",
            "_relevance_score": round(float(score), 4),
            "_match_modes": modes,
            "_content_for_prompt": (
                "【世界书·仅作背景参考】\n"
                f"条目：{str(entry.get('name') or '').strip() or '（未命名）'}\n"
                f"{str(entry.get('content') or '').strip()}"
            ),
            "id": int(entry.get("id") or 0),
            "name": str(entry.get("name") or ""),
            "content": str(entry.get("content") or ""),
            "tags": list(entry.get("tags") or []),
            "match_keywords": list(entry.get("match_keywords") or []),
        }

    @staticmethod
    def _normalize_key_record_type(record_type: str | None) -> str:
        raw = str(record_type or "").strip()
        if not raw:
            return ""
        if raw in KEY_RECORD_TYPES:
            return raw
        return str(LEGACY_KEY_RECORD_TYPE_MAP.get(raw) or "")

    def _classify_key_record_type(
        self,
        *,
        title: str,
        content_text: str,
        tags: list[str],
        content_json: dict | None,
        start_date: str | None,
        end_date: str | None,
    ) -> str:
        blob = "\n".join(
            [
                str(title or ""),
                str(content_text or ""),
                " ".join(str(t) for t in (tags or [])),
                json.dumps(content_json, ensure_ascii=False) if isinstance(content_json, dict) else "",
                str(start_date or ""),
                str(end_date or ""),
            ]
        )
        if any(word in blob for word in ("吸入", "剂量", "早晚", "停药", "用药", "规律")):
            return "medication_protocol"
        if any(word in blob for word in ("指标", "阈值", "波动", "监测", "炎症", "症状", "体质")):
            return "health_monitoring"
        if any(word in blob for word in ("食疗", "饮食", "红枣", "生姜", "配比", "食用")):
            return "dietary_intervention"
        if any(word in blob for word in ("复诊", "复查", "门诊", "携带", "截止")):
            return "medical_review_date"
        if any(word in blob for word in ("纪念", "那一天", "晨间告别", "周年")):
            return "anniversary_date"
        if any(word in blob for word in ("休学", "复学", "毕业", "工作变动", "搬迁")):
            return "lifecycle_milestone"
        if any(word in blob for word in ("项目", "部署", "适配", "进度", "下一步", "协作")):
            return "key_collaboration"
        if any(word in blob for word in ("承诺", "原则", "共识", "协议", "准则")):
            return "commitment_agreement"
        if any(word in blob for word in ("害怕被忘记", "锚点", "焦虑时", "身体接触", "忘记")):
            return "emotional_anchor"
        return "life_pattern"

    @staticmethod
    def _world_book_keyword_scores(query: str, entries: list[dict]) -> dict[int, float]:
        keywords = StateMachine._extract_keywords_for_world_books(query)
        keyword_scores: dict[int, float] = {}
        if not keywords:
            return keyword_scores
        for entry in entries:
            content = str(entry.get("content") or "").lower()
            name = str(entry.get("name") or "").lower()
            match_keywords = [
                str(x).lower() for x in (entry.get("match_keywords") or []) if str(x).strip()
            ]
            tags = [str(x).lower() for x in (entry.get("tags") or []) if str(x).strip()]
            score = 0.0
            for kw in keywords:
                if kw in match_keywords:
                    score += 1.0
                elif kw in tags:
                    score += 0.7
                elif kw in name:
                    score += 0.6
                elif kw in content:
                    score += 0.45
            if score > 0:
                keyword_scores[int(entry.get("id") or 0)] = score
        return keyword_scores

    async def generate_periodic_review(
        self,
        start_date: str,
        end_date: str,
        include_archived: bool = False,
    ) -> dict:
        events = await self.db.get_events_in_range(
            start_date=start_date,
            end_date=end_date,
            include_archived=include_archived,
        )
        snapshots = await self.db.get_snapshots_in_range(start_date, end_date)

        events_timeline = self._format_periodic_events(events)
        snapshots_timeline = self._format_periodic_snapshots(snapshots)
        stats_summary = self._build_periodic_stats(events, snapshots)

        system_prompt = await self.prompt_manager.get_system_prompt()
        prompt_template = await self.prompt_manager.get_prompt(KEY_PROMPT_PERIODIC_REVIEW)
        prompt = prompt_template.format(
            time_range=f"{start_date} ~ {end_date}",
            snapshots_timeline=snapshots_timeline,
            events_timeline=events_timeline,
            stats_summary=stats_summary,
            system_layers=await self.prompt_manager.get_system_layers_text(),
        )

        content = await self.llm.chat([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ])
        return {
            "content": content,
            "stats": {
                "start_date": start_date,
                "end_date": end_date,
                "event_count": len(events),
                "snapshot_count": len(snapshots),
            },
        }

    # --- Internal helpers ---

    async def _advance_until_locked(
        self,
        *,
        baseline_time: datetime,
        target_time: datetime,
        current_content: str,
        previous_env: dict | None,
        max_steps: int | None,
        trigger: str,
        allow_tail_checkpoint: bool = True,
        snapshot_anchor_for_tail: datetime | None = None,
        enforce_tail_min_gap_rule: bool = False,
        defer_maintenance: bool = False,
        diagnostic: OperationTracer | None = None,
    ) -> dict:
        min_time_unit = await self._trace_await(
            diagnostic,
            f"{trigger}.load_min_time_unit",
            self._get_min_time_unit_timedelta(),
        )
        planned_checkpoints, base_meta = self._plan_exact_checkpoints(
            baseline_time,
            target_time,
            min_time_unit,
            allow_tail_checkpoint=allow_tail_checkpoint,
            snapshot_anchor_for_tail=snapshot_anchor_for_tail,
            enforce_tail_min_gap_rule=enforce_tail_min_gap_rule,
            tail_min_gap_hours=self.TAIL_ONLY_SNAPSHOT_MIN_GAP_HOURS,
        )
        due_checkpoints = list(planned_checkpoints)
        if max_steps is not None and max_steps > 0:
            due_checkpoints = due_checkpoints[:max_steps]

        schedule_meta = {
            **base_meta,
            "trigger": trigger,
            "min_time_unit_hours": min_time_unit.total_seconds() / 3600.0,
            "baseline_time_cst": utc_naive_to_shanghai_iso(baseline_time),
            "target_time_cst": utc_naive_to_shanghai_iso(target_time),
            "planned_checkpoint_count": len(planned_checkpoints),
            "checkpoint_count": len(due_checkpoints),
            "checkpoint_times_cst": [utc_naive_to_shanghai_iso(t) for t in due_checkpoints],
            "remaining_checkpoint_count": max(0, len(planned_checkpoints) - len(due_checkpoints)),
            "limited_by_max_steps": len(due_checkpoints) < len(planned_checkpoints),
            "generated_snapshots": [],
        }
        if snapshot_anchor_for_tail is not None:
            schedule_meta["tail_gap_anchor_cst"] = utc_naive_to_shanghai_iso(
                snapshot_anchor_for_tail
            )
        if not due_checkpoints:
            return {
                "content": current_content,
                "schedule": schedule_meta,
            }

        start_date = baseline_time.date().isoformat()
        end_date = target_time.date().isoformat()
        all_events = await self._trace_await(
            diagnostic,
            f"{trigger}.db.get_events_in_range",
            self.db.get_events_in_range(start_date, end_date),
            start_date=start_date,
            end_date=end_date,
        )
        world_books = await self._trace_await(
            diagnostic,
            f"{trigger}.db.get_active_world_books",
            self.db.get_active_world_books(),
        )
        world_book_payload: list[dict] = [self._world_book_to_dict(wb) for wb in world_books]

        recent_events_limit = await self._trace_await(
            diagnostic,
            f"{trigger}.load_snapshot_recent_events_limit",
            self._get_snapshot_recent_events_limit(),
        )
        generated_snapshots: list[dict] = []
        prev_time = baseline_time
        if diagnostic is not None:
            with diagnostic.stage(
                f"{trigger}.load_snapshot_prompts_and_layers",
                checkpoint_count=len(due_checkpoints),
            ):
                system_prompt = await self.prompt_manager.get_system_prompt()
                prompt_template = await self.prompt_manager.get_prompt(
                    KEY_PROMPT_SNAPSHOT_GENERATION
                )
                character_background = await self.prompt_manager.get_layer_content(
                    KEY_L1_CHARACTER_BACKGROUND
                )
                character_personality = await self.prompt_manager.get_layer_content(
                    KEY_L2_CHARACTER_PERSONALITY
                )
                relationship_dynamics = await self.prompt_manager.get_layer_content(
                    KEY_L2_RELATIONSHIP_DYNAMICS
                )
                life_status = await self.prompt_manager.get_layer_content(
                    KEY_L2_LIFE_STATUS
                )
        else:
            system_prompt = await self.prompt_manager.get_system_prompt()
            prompt_template = await self.prompt_manager.get_prompt(
                KEY_PROMPT_SNAPSHOT_GENERATION
            )
            character_background = await self.prompt_manager.get_layer_content(
                KEY_L1_CHARACTER_BACKGROUND
            )
            character_personality = await self.prompt_manager.get_layer_content(
                KEY_L2_CHARACTER_PERSONALITY
            )
            relationship_dynamics = await self.prompt_manager.get_layer_content(
                KEY_L2_RELATIONSHIP_DYNAMICS
            )
            life_status = await self.prompt_manager.get_layer_content(KEY_L2_LIFE_STATUS)

        for i, checkpoint_time in enumerate(due_checkpoints):
            checkpoint_index = i + 1
            checkpoint_cst = utc_naive_to_shanghai_iso(checkpoint_time)
            checkpoint_events, events_text, events_meta = self._build_checkpoint_recent_events(
                all_events,
                checkpoint_time,
                recent_events_limit,
            )
            world_book_entries = await self._trace_await(
                diagnostic,
                f"{trigger}.checkpoint_{checkpoint_index}.retrieve_world_books",
                self._retrieve_world_book_entries(
                    query=f"{current_content}\n{events_text}",
                    entries=world_book_payload,
                ),
                checkpoint_time_cst=checkpoint_cst,
                event_count=len(checkpoint_events),
            )
            time_delta = checkpoint_time - prev_time
            time_delta_hours = max(0.0, time_delta.total_seconds() / 3600.0)
            prev_time = checkpoint_time
            environment_context_details = await self._trace_await(
                diagnostic,
                f"{trigger}.checkpoint_{checkpoint_index}.build_environment_context_details",
                self._build_environment_context_details(checkpoint_time),
                checkpoint_time_cst=checkpoint_cst,
            )
            recent_key_records = await self._trace_await(
                diagnostic,
                f"{trigger}.checkpoint_{checkpoint_index}.load_recent_key_records",
                self.db.get_recent_key_records(limit=6, include_archived=False),
                checkpoint_time_cst=checkpoint_cst,
            )
            disturbance_result = await self._trace_await(
                diagnostic,
                f"{trigger}.checkpoint_{checkpoint_index}.maybe_inject_disturbance",
                self._maybe_inject_disturbance(
                    checkpoint_time=checkpoint_time,
                    current_content=current_content,
                    previous_env=previous_env,
                    environment_context_details=environment_context_details,
                    recent_events=checkpoint_events,
                    recent_key_records=recent_key_records,
                    world_book_entries=world_book_entries,
                ),
                checkpoint_time_cst=checkpoint_cst,
            )
            if disturbance_result.get("should_inject"):
                disturbance_schedule_effect = str(
                    disturbance_result.get("disturbance_schedule_effect") or ""
                ).strip()
                if disturbance_schedule_effect and disturbance_schedule_effect != "none":
                    environment_context_details["schedule_alignment"] = disturbance_schedule_effect
                plan_delta_patch = str(disturbance_result.get("plan_delta_patch") or "").strip()
                merged_plan_delta = str(environment_context_details.get("plan_delta") or "").strip()
                if plan_delta_patch:
                    merged_plan_delta = (
                        f"{merged_plan_delta}\n{plan_delta_patch}".strip()
                        if merged_plan_delta
                        else plan_delta_patch
                    )
                environment_context_details["plan_delta"] = merged_plan_delta[:300]
                environment_context_details["disturbance_context"] = str(
                    disturbance_result.get("disturbance_context") or ""
                ).strip()
                environment_context_details["disturbance_schedule_effect"] = disturbance_schedule_effect or "none"
                environment_context_details["recent_disturbances"] = str(
                    disturbance_result.get("recent_disturbances_text") or environment_context_details.get("recent_disturbances") or ""
                ).strip()
            else:
                environment_context_details["disturbance_context"] = ""
                environment_context_details["disturbance_schedule_effect"] = "none"

            env = await self._trace_await(
                diagnostic,
                f"{trigger}.checkpoint_{checkpoint_index}.generate_environment",
                self.env_gen.generate(
                    time_point=checkpoint_time,
                    previous_env=previous_env,
                    context={
                        "latest_snapshot": current_content,
                        "time_delta_hours": time_delta_hours,
                        "recent_events": [e.model_dump() for e in checkpoint_events],
                        "world_book_entries": world_book_entries,
                        "current_plan_activity": environment_context_details.get("current_plan_activity", ""),
                        "current_plan_summary": environment_context_details.get("current_plan_summary", ""),
                        "current_conversation_state": environment_context_details.get("current_conversation_state", ""),
                        "recent_trace_summary": environment_context_details.get("recent_trace_summary", ""),
                        "recent_disturbances": environment_context_details.get("recent_disturbances", ""),
                        "schedule_alignment": environment_context_details.get("schedule_alignment", ""),
                        "plan_delta": environment_context_details.get("plan_delta", ""),
                        "disturbance_context": environment_context_details.get("disturbance_context", ""),
                        "disturbance_schedule_effect": environment_context_details.get("disturbance_schedule_effect", "none"),
                    },
                ),
                checkpoint_time_cst=checkpoint_cst,
                time_delta_hours=round(time_delta_hours, 4),
                world_book_count=len(world_book_entries),
            )
            if disturbance_result.get("should_inject"):
                env["disturbance_id"] = int(disturbance_result.get("disturbance_id") or 0)
                env["disturbance_title"] = str(disturbance_result.get("disturbance_title") or "").strip()
                env["disturbance_channel_type"] = str(disturbance_result.get("channel_type") or "").strip()
                env["disturbance_context"] = str(disturbance_result.get("disturbance_context") or "").strip()
                env["recent_disturbances"] = str(disturbance_result.get("recent_disturbances_text") or "").strip()
                env["disturbance_schedule_effect"] = str(
                    disturbance_result.get("disturbance_schedule_effect") or "none"
                ).strip()
            environment_text = environment_text_for_prompt(env)
            environment_retrieval_text = environment_text_for_retrieval(env)
            memory_results = await self._trace_await(
                diagnostic,
                f"{trigger}.checkpoint_{checkpoint_index}.memory_search",
                self.memory.search(
                    environment_retrieval_text or environment_text,
                    top_k=self.DEFAULT_MEMORY_TOP_K,
                ),
                checkpoint_time_cst=checkpoint_cst,
                query_chars=len((environment_retrieval_text or environment_text) or ""),
                top_k=self.DEFAULT_MEMORY_TOP_K,
            )
            memory_text, memory_meta = self._build_memory_context(memory_results)
            prior_snapshot_content = current_content
            prompt = prompt_template.format(
                character_background=character_background,
                character_personality=character_personality,
                relationship_dynamics=relationship_dynamics,
                life_status=life_status,
                environment=environment_text,
                previous_snapshot=prior_snapshot_content,
                recent_events=events_text,
                memory_context=memory_text,
            )
            self._log_checkpoint_prompt_stats(
                checkpoint_time=checkpoint_time,
                trigger=trigger,
                previous_snapshot=prior_snapshot_content,
                recent_events_text=events_text,
                recent_events_meta=events_meta,
                memory_text=memory_text,
                memory_meta=memory_meta,
                environment_text=environment_text,
                prompt_text=prompt,
            )

            current_content = await self._trace_await(
                diagnostic,
                f"{trigger}.checkpoint_{checkpoint_index}.snapshot_llm",
                self.snapshot_llm.chat(
                    [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    max_tokens=None,
                ),
                checkpoint_time_cst=checkpoint_cst,
                prompt_chars=len(prompt or ""),
                memory_chars=len(memory_text or ""),
            )

            is_final_executed_checkpoint = (
                i == len(due_checkpoints) - 1
                and len(due_checkpoints) == len(planned_checkpoints)
            )
            is_tail_checkpoint = (
                bool(base_meta.get("tail_appended"))
                and is_final_executed_checkpoint
                and abs((checkpoint_time - target_time).total_seconds()) <= 1e-6
            )
            snap = StateSnapshot(
                created_at=format_utc_instant_z(shanghai_time_to_utc_naive(checkpoint_time)),
                type="accumulated" if is_tail_checkpoint else "daily",
                content=current_content,
                environment=json.dumps(env, ensure_ascii=False),
                referenced_events=json.dumps(
                    [e.id for e in checkpoint_events if e.id is not None],
                    ensure_ascii=False,
                ),
            )
            snap_id = await self._trace_await(
                diagnostic,
                f"{trigger}.checkpoint_{checkpoint_index}.db.insert_snapshot",
                self.db.insert_snapshot(snap),
                checkpoint_time_cst=checkpoint_cst,
                snapshot_type=snap.type,
                snapshot_chars=len(current_content or ""),
            )
            await self._trace_await(
                diagnostic,
                f"{trigger}.checkpoint_{checkpoint_index}.append_relationship_thought",
                self._append_relationship_thought_from_context(
                    source_snapshot_id=int(snap_id or 0),
                    source_env_id=f"{trigger}:{checkpoint_index}:{snap.type}",
                    snapshot_text=current_content,
                    environment_text=environment_text,
                ),
                checkpoint_time_cst=checkpoint_cst,
            )
            if disturbance_result.get("should_inject") and int(disturbance_result.get("disturbance_id") or 0) > 0:
                await self.db.update_disturbance_pulse(
                    int(disturbance_result.get("disturbance_id") or 0),
                    linked_snapshot_id=int(snap_id),
                )
            generated_snapshots.append(
                {
                    "id": snap_id,
                    "created_at": snap.created_at,
                    "created_at_cst": iso_string_for_cst_display(str(snap.created_at or "")),
                    "type": snap.type,
                    "content": current_content,
                    "referenced_event_count": len(checkpoint_events),
                }
            )

            generated_event_id: int | None = None
            if not env.get("stale"):
                self._schedule_deferred_event_generation(
                    snapshot_id=snap_id,
                    snapshot_content=current_content,
                    environment=env,
                    memory_text=memory_text,
                    checkpoint_time=checkpoint_time,
                    defer_vectorization=bool(defer_maintenance),
                )
            generated_snapshots[-1]["generated_event_id"] = generated_event_id
            if env.get("stale"):
                generated_snapshots[-1]["environment_stale"] = True
                self._schedule_deferred_env_retry(
                    snapshot_id=snap_id,
                    event_id=generated_event_id,
                    checkpoint_time=checkpoint_time,
                    previous_snapshot_content=prior_snapshot_content,
                    previous_env=previous_env,
                    checkpoint_events=[e.model_dump() for e in checkpoint_events],
                    snapshot_type=snap.type,
                    snapshot_created_at=snap.created_at,
                )
            if not defer_maintenance:
                await self._trace_await(
                    diagnostic,
                    f"{trigger}.checkpoint_{checkpoint_index}.enforce_snapshot_limit",
                    self._enforce_snapshot_limit(),
                    checkpoint_time_cst=checkpoint_cst,
                )
            previous_env = env

        schedule_meta["generated_snapshots"] = generated_snapshots
        schedule_meta["advanced_to_time_cst"] = utc_naive_to_shanghai_iso(due_checkpoints[-1])
        return {
            "content": current_content,
            "schedule": schedule_meta,
        }

    @staticmethod
    def _plan_exact_checkpoints(
        last: datetime,
        now: datetime,
        min_time_unit: timedelta,
        *,
        allow_tail_checkpoint: bool = True,
        snapshot_anchor_for_tail: datetime | None = None,
        enforce_tail_min_gap_rule: bool = False,
        tail_min_gap_hours: float = 2.0,
    ) -> tuple[list[datetime], dict]:
        interval = now - last
        interval_sec = interval.total_seconds()
        min_sec = max(min_time_unit.total_seconds(), 1e-9)
        gap_snap_sec = (
            (now - snapshot_anchor_for_tail).total_seconds()
            if snapshot_anchor_for_tail is not None
            else interval_sec
        )
        base_meta: dict = {
            "interval_hours": interval_sec / 3600.0,
            "conversation_to_snapshot_gap_hours": max(0.0, gap_snap_sec / 3600.0),
        }
        if interval_sec <= 0:
            meta = {
                **base_meta,
                "n_full_intervals": 0,
                "remainder_hours": 0.0,
                "tail_appended": False,
                "equal_split_fallback": False,
                "note": "no advancement needed",
            }
            return [], meta

        n_full = int(interval_sec // min_sec)
        checkpoints: list[datetime] = [last + min_time_unit * k for k in range(1, n_full + 1)]
        if n_full >= 1:
            last_grid_end = last + min_time_unit * n_full
            remainder_sec = max(0.0, (now - last_grid_end).total_seconds())
        else:
            remainder_sec = max(0.0, interval_sec)
        tail_appended = allow_tail_checkpoint and remainder_sec > 1e-6
        if tail_appended:
            checkpoints.append(now)

        # For get_current_state tail-only refreshes, suppress very short gaps from the last snapshot.
        if (
            enforce_tail_min_gap_rule
            and n_full == 0
            and tail_appended
            and gap_snap_sec <= tail_min_gap_hours * 3600.0 + 1e-6
        ):
            checkpoints = []
            tail_appended = False
            meta = {
                **base_meta,
                "n_full_intervals": 0,
                "remainder_hours": 0.0,
                "tail_appended": False,
                "equal_split_fallback": False,
                "note": (
                    f"tail_only_suppressed: gap_from_snapshot {gap_snap_sec / 3600.0:.4f}h "
                    f"<= tail_min_gap {tail_min_gap_hours}h"
                ),
            }
            return checkpoints, meta

        meta = {
            **base_meta,
            "n_full_intervals": n_full,
            "remainder_hours": remainder_sec / 3600.0,
            "tail_allowed": allow_tail_checkpoint,
            "tail_appended": tail_appended,
            "equal_split_fallback": False,
            "note": "exact checkpoint schedule",
        }
        return checkpoints, meta

    def _build_checkpoint_recent_events(
        self,
        events: list[EventAnchor],
        checkpoint_time: datetime,
        limit: int,
    ) -> tuple[list[EventAnchor], str, dict]:
        visible_events = [
            event for event in events if self._event_is_visible_at_checkpoint(event, checkpoint_time)
        ]
        visible_events.sort(key=self._event_sort_key)
        selected_events = visible_events[-max(1, limit):]
        lines = [f"- [{e.date}] {e.description}" for e in selected_events]
        if lines:
            text = "\n".join(lines)
        else:
            selected_events = []
            text = "（无近期事件记录）"
        truncated = False
        return selected_events, text, {
            "visible_count": len(visible_events),
            "selected_count": len(selected_events),
            "chars": len(text),
            "truncated": truncated,
        }

    def _build_memory_context(self, memories) -> tuple[str, dict]:
        if not memories:
            text = "（无相关历史记忆）"
            return text, {"selected_count": 0, "chars": len(text), "truncated": False}
        lines: list[str] = []
        for memory in memories:
            label = "事件" if memory.source_type == "event" else "快照"
            lines.append(f"- [{label}] {memory.text}")
        text = "\n".join(lines) if lines else "（无相关历史记忆）"
        return text, {
            "selected_count": len(lines),
            "source_count": len(memories),
            "chars": len(text),
            "truncated": False,
        }

    @staticmethod
    def _event_sort_key(event: EventAnchor) -> tuple[str, str, int]:
        return (
            str(event.date or ""),
            str(event.created_at or ""),
            int(event.id or 0),
        )

    @staticmethod
    def _compose_event_description(
        objective: str,
        impression: str,
        detail_hooks: str = "",
        open_loop: str = "",
    ) -> str:
        objective_text = str(objective or "").strip()
        impression_text = str(impression or "").strip()
        detail_hooks_text = str(detail_hooks or "").strip()
        open_loop_text = str(open_loop or "").strip()
        lines: list[str] = []
        if objective_text:
            lines.append(f"客观记录：{objective_text}")
        if impression_text:
            lines.append(f"主观印象：{impression_text}")
        if detail_hooks_text:
            lines.append(f"细节钩子：{detail_hooks_text}")
        if open_loop_text:
            lines.append(f"未完成线索：{open_loop_text}")
        if lines:
            return "\n".join(lines)
        if objective_text:
            return f"客观记录：{objective_text}"
        if impression_text:
            return f"主观印象：{impression_text}"
        return ""

    def _event_is_visible_at_checkpoint(
        self,
        event: EventAnchor,
        checkpoint_time: datetime,
    ) -> bool:
        created_at = str(event.created_at or "").strip()
        if created_at:
            try:
                return self._parse_iso_datetime(created_at) <= checkpoint_time
            except Exception:
                pass
        event_date = str(event.date or "").strip()
        if event_date:
            return event_date <= checkpoint_time.date().isoformat()
        return True

    def _resolve_progress_baseline(
        self,
        latest_snapshot: StateSnapshot | None,
        requested_last: datetime | None,
    ) -> datetime:
        latest_time = self._snapshot_created_instant(latest_snapshot)
        if latest_time is not None:
            return latest_time
        if requested_last is not None:
            return requested_last
        return shanghai_now()

    @staticmethod
    def _snapshot_created_instant(latest_snapshot: StateSnapshot | None) -> datetime | None:
        if not latest_snapshot or not latest_snapshot.created_at:
            return None
        try:
            return StateMachine._parse_iso_datetime(latest_snapshot.created_at)
        except Exception:
            return None

    async def _get_snapshot_scheduler_night_pause_state(
        self,
        *,
        now: datetime | None = None,
    ) -> dict[str, str | int] | None:
        now_cst = now or shanghai_now()
        if now_cst.hour >= 8:
            return None
        # 当前没有显式“快照来源”字段，这里用非 conversation_end 快照近似代表自动推进基线。
        latest_auto_snapshot = await self.db.get_latest_non_conversation_snapshot()
        latest_auto_time = self._snapshot_created_instant(latest_auto_snapshot)
        if latest_auto_time is None:
            return None
        if latest_auto_time.date() >= now_cst.date():
            return None
        resume_at = now_cst.replace(hour=8, minute=0, second=0, microsecond=0)
        resume_in_seconds = max(0, int((resume_at - now_cst).total_seconds()))
        return {
            "reason": "night_pause_until_06",
            "now_cst": utc_naive_to_shanghai_iso(now_cst),
            "resume_at_cst": utc_naive_to_shanghai_iso(resume_at),
            "resume_in_seconds": resume_in_seconds,
            "last_auto_snapshot_cst": utc_naive_to_shanghai_iso(latest_auto_time),
        }

    def _resolve_get_current_state_baseline(
        self,
        _latest_snapshot: StateSnapshot | None,
        now: datetime,
        snapshot_instant: datetime | None,
    ) -> datetime:
        """get_current_state always advances from the latest snapshot in DB.

        ``last_interaction_time`` is kept for observability, but the actual
        checkpoint/tail-fill calculation should be based on the conversation
        start time versus the latest snapshot time.
        """
        if snapshot_instant is not None:
            return snapshot_instant
        return now

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
    def _environment_body_text(env: dict | None) -> str:
        if not env or not isinstance(env, dict):
            return ""
        return str(env.get("activity") or "").strip()

    @staticmethod
    def _environment_recent_disturbances_text(env: dict | None) -> str:
        if not env or not isinstance(env, dict):
            return ""
        return str(env.get("recent_disturbances") or env.get("disturbance_context") or "").strip()

    @staticmethod
    def _environment_disturbance_id(env: dict | None) -> int | None:
        if not env or not isinstance(env, dict):
            return None
        raw = str(env.get("disturbance_id") or "").strip()
        return int(raw) if raw.isdigit() and int(raw) > 0 else None

    @staticmethod
    def _extract_labeled_env_summary_value(summary_text: str, label: str) -> str:
        text = str(summary_text or "").strip()
        if not text:
            return ""
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.startswith(f"{label}:"):
                return line[len(label) + 1 :].strip()
        return ""

    def _environment_detail_hooks_text(self, env: dict | None) -> str:
        if not env or not isinstance(env, dict):
            return ""
        summary_text = str(env.get("summary") or "").strip()
        hooks = self._extract_labeled_env_summary_value(summary_text, "Key detail hooks")
        if hooks:
            return hooks[:180]
        body = self._environment_body_text(env)
        if not body:
            return ""
        quoted = re.findall(r"[“\"]([^”\"\n]{4,40})[”\"]", body)
        if quoted:
            deduped: list[str] = []
            for item in quoted:
                clean = item.strip()
                if clean and clean not in deduped:
                    deduped.append(clean)
                if len(deduped) >= 2:
                    break
            return "; ".join(deduped)[:180]
        sentences = [s.strip() for s in re.split(r"[。！？\n]", body) if s.strip()]
        picks: list[str] = []
        for sentence in sentences:
            if any(token in sentence for token in ("视线", "手指", "呼吸", "屏幕", "停顿", "腕", "肩", "消息", "终端", "脚步")):
                picks.append(sentence[:40])
            if len(picks) >= 2:
                break
        return "; ".join(picks)[:180]

    async def _persist_automation_report(
        self,
        report: dict | None,
        llm_usage: dict | None,
    ) -> None:
        if not isinstance(report, dict):
            return
        report["llm_usage"] = llm_usage or {}
        persist_method = getattr(self.automation_engine, "persist_run_report", None)
        if callable(persist_method):
            await persist_method(report)

    def _log_checkpoint_prompt_stats(
        self,
        *,
        checkpoint_time: datetime,
        trigger: str,
        previous_snapshot: str,
        recent_events_text: str,
        recent_events_meta: dict,
        memory_text: str,
        memory_meta: dict,
        environment_text: str,
        prompt_text: str,
    ) -> None:
        payload = {
            "trigger": trigger,
            "checkpoint_time_cst": utc_naive_to_shanghai_iso(checkpoint_time),
            "previous_snapshot_chars": len(previous_snapshot or ""),
            "recent_events_chars": len(recent_events_text or ""),
            "recent_events_count": int(recent_events_meta.get("selected_count", 0)),
            "recent_events_visible_count": int(recent_events_meta.get("visible_count", 0)),
            "recent_events_truncated": bool(recent_events_meta.get("truncated")),
            "memory_chars": len(memory_text or ""),
            "memory_count": int(memory_meta.get("selected_count", 0)),
            "memory_truncated": bool(memory_meta.get("truncated")),
            "environment_chars": len(environment_text or ""),
            "prompt_chars": len(prompt_text or ""),
        }
        logger.info(
            "snapshot checkpoint prompt stats: %s",
            json.dumps(payload, ensure_ascii=False),
        )

    async def _generate_event_anchor(
        self,
        snapshot_content: str,
        env: dict,
        memory_text: str,
        time_point: datetime,
        *,
        defer_vectorization: bool = False,
    ) -> int | None:
        system_prompt = await self.prompt_manager.get_system_prompt()
        prompt_template = await self.prompt_manager.get_prompt(KEY_PROMPT_EVENT_ANCHOR)
        prompt = prompt_template.format(
            current_snapshot=snapshot_content,
            environment=environment_text_for_prompt(env),
            memory_context=memory_text,
            system_layers=await self.prompt_manager.get_system_layers_text(),
        )

        response = await self.snapshot_llm.chat([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ])

        return await self._parse_and_save_event(
            response,
            source="generated",
            date_override=time_point.date().isoformat(),
            defer_vectorization=defer_vectorization,
        )

    async def _parse_and_save_event(
        self,
        response: str,
        source: str,
        date_override: str | None = None,
        *,
        defer_vectorization: bool = False,
    ) -> int | None:
        text = (response or "").strip()
        if not text:
            return None
        lowered = text.lower()
        if "无需记录" in text or "无须记录" in text or "no event" in lowered:
            logger.info("LLM determined no event anchor needed.")
            return None
        title = ""
        objective = ""
        impression = ""
        detail_hooks = ""
        open_loop = ""
        keywords: list[str] = []
        categories: list[str] = []

        title_match = re.search(r"(?:标题|title)\s*[:：]\s*(.+)", text, re.IGNORECASE)
        if title_match:
            title = title_match.group(1).strip()

        objective = self._extract_event_field_block(text, ["客观记录", "objective"])
        impression = self._extract_event_field_block(text, ["主观印象", "impression"])
        detail_hooks = self._extract_event_field_block(text, ["细节钩子", "detail hooks", "detail_hooks"])
        open_loop = self._extract_event_field_block(text, ["未完成线索", "open loop", "open_loop"])

        kw_match = re.search(r"(?:关键词|keywords?)\s*[:：]\s*\[?(.+?)\]?\s*$", text, re.IGNORECASE | re.MULTILINE)
        if kw_match:
            raw = kw_match.group(1)
            keywords = [k.strip().strip("\"'") for k in re.split(r"[,，、]", raw) if k.strip()]

        cat_match = re.search(r"(?:分类|categories?)\s*[:：]\s*\[?(.+?)\]?\s*$", text, re.IGNORECASE | re.MULTILINE)
        if cat_match:
            raw = cat_match.group(1)
            categories = [c.strip().strip("\"'") for c in re.split(r"[,，、]", raw) if c.strip()]

        if not objective and not impression:
            # Backward compatible: first non-empty line as description.
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            description = "\n".join(lines) if lines else ""
        else:
            description = self._compose_event_description(objective, impression, detail_hooks, open_loop)

        if not description:
            return None
        if not title:
            title = make_event_title(objective or description, keywords, categories)
        if not categories:
            categories = classify_event(description, keywords)

        now_shanghai = shanghai_now()
        event = EventAnchor(
            date=date_override or now_shanghai.date().isoformat(),
            title=title,
            description=description,
            source=source,
            created_at=format_utc_instant_z(shanghai_time_to_utc_naive(now_shanghai)),
            trigger_keywords=json.dumps(keywords, ensure_ascii=False),
            categories=json.dumps(categories, ensure_ascii=False),
        )
        event_id = await self.db.insert_event(event)
        logger.info("Saved event anchor #%d: %s", event_id, description[:50])
        if defer_vectorization:
            logger.info("Deferred event vector upsert for #%d.", event_id)
            return event_id
        upsert_event_vector = getattr(self.memory, "upsert_event_vector", None)
        if callable(upsert_event_vector):
            try:
                await upsert_event_vector(int(event_id))
            except Exception as exc:
                logger.warning("Event vector upsert skipped for #%d: %s", event_id, exc)
        return event_id

    def _parse_event_payload_for_update(
        self,
        response: str,
        *,
        source: str,
        date_override: str | None = None,
    ) -> dict | None:
        text = (response or "").strip()
        if not text:
            return None
        lowered = text.lower()
        if "无需记录" in text or "无须记录" in text or "no event" in lowered:
            return None

        title = ""
        objective = ""
        impression = ""
        detail_hooks = ""
        open_loop = ""
        keywords: list[str] = []
        categories: list[str] = []

        title_match = re.search(r"(?:标题|title)\s*[:：]\s*(.+)", text, re.IGNORECASE)
        if title_match:
            title = title_match.group(1).strip()

        objective = self._extract_event_field_block(text, ["客观记录", "objective"])
        impression = self._extract_event_field_block(text, ["主观印象", "impression"])
        detail_hooks = self._extract_event_field_block(text, ["细节钩子", "detail hooks", "detail_hooks"])
        open_loop = self._extract_event_field_block(text, ["未完成线索", "open loop", "open_loop"])

        kw_match = re.search(r"(?:关键词|keywords?)\s*[:：]\s*\[?(.+?)\]?\s*$", text, re.IGNORECASE | re.MULTILINE)
        if kw_match:
            raw = kw_match.group(1)
            keywords = [k.strip().strip("\"'") for k in re.split(r"[,，、]", raw) if k.strip()]

        cat_match = re.search(r"(?:分类|categories?)\s*[:：]\s*\[?(.+?)\]?\s*$", text, re.IGNORECASE | re.MULTILINE)
        if cat_match:
            raw = cat_match.group(1)
            categories = [c.strip().strip("\"'") for c in re.split(r"[,，、]", raw) if c.strip()]

        if not objective and not impression:
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            description = "\n".join(lines) if lines else ""
        else:
            description = self._compose_event_description(objective, impression, detail_hooks, open_loop)

        if not description:
            return None
        if not title:
            title = make_event_title(objective or description, keywords, categories)
        if not categories:
            categories = classify_event(description, keywords)

        return {
            "date": date_override or shanghai_now().date().isoformat(),
            "title": title,
            "description": description,
            "source": source,
            "trigger_keywords": json.dumps(keywords, ensure_ascii=False),
            "categories": json.dumps(categories, ensure_ascii=False),
        }

    async def _enforce_snapshot_limit(self):
        overflow = await self.db.get_oldest_snapshots_beyond_limit(self.max_snapshots)
        for snap in overflow:
            vector_id = await self.memory.store(
                f"snapshot_{snap.id}",
                snap.content,
                {
                    "type": snap.type,
                    "created_at": snap.created_at,
                    "source_type": "snapshot",
                    "source_id": snap.id,
                },
            )
            await self.db.mark_snapshot_vectorized(snap.id, vector_id or f"kw_{snap.id}")  # type: ignore
            logger.info("Archived snapshot #%d beyond retention limit.", snap.id)

    async def _sync_vector_candidates(self):
        sync_method = getattr(self.memory, "sync_eligible_vectors", None)
        if not callable(sync_method):
            return
        try:
            result = await sync_method()
            event_count = int(result.get("vectorized_events", 0))
            snapshot_count = int(result.get("vectorized_snapshots", 0))
            if event_count or snapshot_count:
                logger.info(
                    "Vector sync completed: %d events, %d snapshots.",
                    event_count,
                    snapshot_count,
                )
        except Exception as exc:
            logger.warning("Vector sync skipped due to error: %s", exc)

    async def _run_automation(self, trigger: str) -> dict | None:
        async with self._maintenance_lock:
            if self.automation_engine is None:
                await self._sync_vector_candidates()
                return None
            try:
                return await self.automation_engine.run(trigger)
            except Exception as exc:
                logger.warning("Automation run failed: %s", exc)
                return {"trigger": trigger, "errors": [str(exc)]}

    def _schedule_deferred_maintenance(
        self,
        *,
        trigger: str,
        llm_usage: dict | None,
    ) -> None:
        item = {
            "trigger": trigger,
            "llm_usage": llm_usage or {},
        }
        if self._deferred_maintenance_task and not self._deferred_maintenance_task.done():
            if self._deferred_maintenance_queue:
                self._deferred_maintenance_queue[-1] = item
            else:
                self._deferred_maintenance_queue.append(item)
            return
        self._deferred_maintenance_queue.append(item)
        self._deferred_maintenance_task = asyncio.create_task(
            self._drain_deferred_maintenance_queue()
        )

    async def _drain_deferred_maintenance_queue(self) -> None:
        while self._deferred_maintenance_queue:
            item = self._deferred_maintenance_queue.pop(0)
            trigger = str(item.get("trigger") or "deferred")
            llm_usage = item.get("llm_usage")
            try:
                event_task = self._deferred_event_task
                if event_task and not event_task.done():
                    await event_task
                env_retry_task = self._deferred_env_retry_task
                if env_retry_task and not env_retry_task.done():
                    await env_retry_task
                async with self._maintenance_lock:
                    await self._enforce_snapshot_limit()
                report = await self._run_automation(trigger)
                await self._persist_automation_report(report, llm_usage)
                logger.info("Deferred maintenance completed for trigger=%s", trigger)
            except Exception:
                logger.exception("Deferred maintenance failed for trigger=%s", trigger)

    def _schedule_deferred_event_generation(
        self,
        *,
        snapshot_id: int,
        snapshot_content: str,
        environment: dict | None = None,
        memory_text: str,
        checkpoint_time: datetime,
        defer_vectorization: bool,
        conversation_summary: str = "",
        conversation_started_at: str = "",
    ) -> None:
        if snapshot_id <= 0:
            return
        item = {
            "snapshot_id": snapshot_id,
            "snapshot_content": snapshot_content,
            "environment": environment or {},
            "memory_text": memory_text,
            "checkpoint_time": checkpoint_time,
            "defer_vectorization": defer_vectorization,
            "conversation_summary": conversation_summary,
            "conversation_started_at": conversation_started_at,
            "status": "queued",
        }
        replaced = False
        for idx, existing in enumerate(self._deferred_event_queue):
            if int(existing.get("snapshot_id") or 0) == snapshot_id:
                self._deferred_event_queue[idx] = item
                replaced = True
                break
        if not replaced:
            self._deferred_event_queue.append(item)
        self._deferred_event_snapshot_ids.add(snapshot_id)
        logger.info(
            "Deferred event job queued snapshot=%d status=queued replaced=%s",
            snapshot_id,
            replaced,
        )
        if self._deferred_event_task and not self._deferred_event_task.done():
            return
        self._deferred_event_task = asyncio.create_task(
            self._drain_deferred_event_queue()
        )

    async def _drain_deferred_event_queue(self) -> None:
        while self._deferred_event_queue:
            item = self._deferred_event_queue.pop(0)
            snapshot_id = int(item.get("snapshot_id") or 0)
            try:
                logger.info(
                    "Deferred event job processing snapshot=%d status=processing",
                    snapshot_id,
                )
                result = await self._process_deferred_event_job(item)
                logger.info(
                    "Deferred event job completed snapshot=%d status=%s route=%s event_id=%s",
                    snapshot_id,
                    str(result.get("status") or ""),
                    str(result.get("route") or ""),
                    result.get("event_id"),
                )
            except Exception:
                logger.exception(
                    "Deferred event job failed for snapshot=%s status=failed",
                    snapshot_id,
                )
            finally:
                self._deferred_event_snapshot_ids.discard(snapshot_id)

    async def _process_deferred_event_job(self, item: dict) -> dict:
        if not await self._get_snapshot_event_candidate_enabled():
            return {"status": "suppressed", "route": "disabled"}
        snapshot_id = int(item.get("snapshot_id") or 0)
        snapshot = await self.db.get_snapshot_by_id(snapshot_id)
        if snapshot is None:
            return {"status": "failed", "route": "missing_snapshot"}

        previous_snapshot = await self.db.get_previous_snapshot_before_id(snapshot_id)
        current_env = self._snapshot_environment_dict(snapshot) or {}
        previous_env = self._snapshot_environment_dict(previous_snapshot) or {}
        current_summary = self._environment_summary_text(current_env)
        previous_summary = self._environment_summary_text(previous_env)
        current_body = self._environment_body_text(current_env)
        current_detail_hooks = self._environment_detail_hooks_text(current_env)
        recent_disturbances = self._environment_recent_disturbances_text(current_env)
        snapshot_delta = self._build_delta_summary(
            previous_snapshot.content if previous_snapshot else "",
            snapshot.content,
        )
        environment_delta = self._build_delta_summary(previous_summary, current_summary)
        trace_summary = self._build_life_flow_trace_summary(
            environment_summary=current_summary,
            environment_delta=environment_delta,
            conversation_summary=str(item.get("conversation_summary") or ""),
        )
        trace_id: int | None = None
        if trace_summary:
            trace_theme = self._infer_life_flow_theme(
                trace_summary,
                {
                    "snapshot_delta": snapshot_delta,
                    "environment_delta": environment_delta,
                    "conversation_summary": str(item.get("conversation_summary") or ""),
                },
                source="conversation" if str(item.get("conversation_summary") or "").strip() else "environment",
            )
            trace_id = await self._append_life_flow_trace(
                trace_date=snapshot.created_at.split("T")[0] if snapshot.created_at else shanghai_now().date().isoformat(),
                source="conversation" if str(item.get("conversation_summary") or "").strip() else "environment",
                summary=trace_summary,
                details={
                    "snapshot_delta": snapshot_delta,
                    "environment_delta": environment_delta,
                    "conversation_summary": str(item.get("conversation_summary") or ""),
                    "summary": current_summary,
                    "plan_delta": str(current_env.get("plan_delta") or ""),
                    "life_theme": trace_theme,
                },
                schedule_alignment=str(current_env.get("schedule_alignment") or "on_track"),
                related_snapshot_id=int(snapshot.id or 0),
            )
        if self.memory_summary_engine is not None:
            try:
                target_date = snapshot.created_at.split("T")[0] if snapshot.created_at else shanghai_now().date().isoformat()
                active_plan = await self.db.get_latest_daily_plan_for_date(target_date)
                plan_items = await self.db.list_plan_items(int(active_plan.id or 0)) if active_plan and active_plan.id else []
                persisted_trace = await self.db.get_latest_life_flow_trace_for_date(target_date) if trace_id else None
                await self.memory_summary_engine.ingest_life_digest_from_materials(
                    target_date=target_date,
                    snapshot=snapshot,
                    trace=persisted_trace,
                    environment=current_env,
                    plan=active_plan,
                    plan_items=plan_items,
                )
            except Exception:
                logger.exception("Failed to ingest life digest nodes from deferred event job.")

        recent_events = await self.db.get_recent_events_before_date(
            snapshot.created_at.split("T")[0] if snapshot.created_at else shanghai_now().date().isoformat(),
            limit=5,
            include_archived=False,
        )
        recent_key_records = await self.db.get_recent_key_records(limit=5, include_archived=False)
        conversation_started_at = str(item.get("conversation_started_at") or "").strip()
        recent_manual_events: list[EventAnchor] = []
        recent_manual_key_records: list[KeyRecord] = []
        if conversation_started_at:
            recent_manual_events = await self.db.get_events_created_since(
                conversation_started_at,
                limit=12,
                include_archived=False,
                sources=["manual", "conversation"],
            )
            recent_manual_key_records = await self.db.get_key_records_updated_since(
                conversation_started_at,
                limit=12,
                include_archived=False,
                sources=["manual", "conversation"],
            )
        trigger_signals = self._extract_event_candidate_signals(
            snapshot_delta=snapshot_delta,
            environment_summary=current_summary,
            environment_delta=environment_delta,
            conversation_summary=str(item.get("conversation_summary") or ""),
            schedule_alignment=str(current_env.get("schedule_alignment") or ""),
            plan_delta=str(current_env.get("plan_delta") or ""),
            recent_events=recent_events,
            recent_key_records=recent_key_records,
            recent_manual_events=recent_manual_events,
            recent_manual_key_records=recent_manual_key_records,
        )
        dedup_context = self._build_event_dedup_context(
            recent_events,
            recent_key_records,
            recent_manual_events,
            recent_manual_key_records,
        )

        judgment = await self._judge_event_trigger(
            snapshot_delta=snapshot_delta,
            environment_summary=current_summary,
            environment_body=current_body,
            recent_disturbances=recent_disturbances,
            environment_delta=environment_delta,
            trigger_signals=trigger_signals,
            dedup_context=dedup_context,
        )
        route = str(judgment.get("route") or "suppress_to_snapshot_only")
        reason = str(judgment.get("reason") or "").strip()
        if route == "generate_event":
            event_id = await self._materialize_deferred_event(
                snapshot=snapshot,
                snapshot_delta=snapshot_delta,
                environment_summary=current_summary,
                environment_body=current_body,
                detail_hooks_text=current_detail_hooks,
                recent_disturbances=recent_disturbances,
                environment_delta=environment_delta,
                judgment=judgment,
                defer_vectorization=bool(item.get("defer_vectorization")),
            )
            disturbance_id = self._environment_disturbance_id(current_env)
            if disturbance_id and event_id:
                await self.db.update_disturbance_pulse(
                    int(disturbance_id),
                    linked_event_id=int(event_id),
                    linked_snapshot_id=int(snapshot.id or 0),
                    status="consumed",
                )
            return {
                "status": "generated_event" if event_id else "failed",
                "route": route,
                "event_id": event_id,
                "reason": reason,
            }
        if route == "convert_to_key_record_candidate":
            candidate = await self._route_key_record_candidate(
                snapshot_delta=snapshot_delta,
                environment_summary=current_summary,
                environment_delta=environment_delta,
                judgment=judgment,
            )
            logger.info(
                "Deferred event job routed_to_key_record_candidate snapshot=%d candidate=%s",
                snapshot_id,
                json.dumps(candidate, ensure_ascii=False),
            )
            return {
                "status": "routed_to_key_record_candidate",
                "route": route,
                "event_id": None,
                "reason": reason,
            }
        return {
            "status": "suppressed",
            "route": route,
            "event_id": None,
            "reason": reason,
        }

    @staticmethod
    def _environment_summary_text(env: dict | None) -> str:
        if not env or not isinstance(env, dict):
            return ""
        return (
            str(env.get("summary") or "").strip()
            or str(env.get("retrieval_summary") or "").strip()
            or str(env.get("activity") or "").strip()
        )

    def _build_life_flow_trace_summary(
        self,
        *,
        environment_summary: str,
        environment_delta: str,
        conversation_summary: str,
    ) -> str:
        if str(conversation_summary or "").strip():
            return self._build_trace_digest_from_conversation_summary(str(conversation_summary or "").strip())
        if str(environment_delta or "").strip() and "鏃犳樉钁楀彉鍖" not in str(environment_delta):
            return str(environment_delta or "").strip()
        return str(environment_summary or "").strip()

    @staticmethod
    def _build_delta_summary(previous_text: str, current_text: str, limit: int = 360) -> str:
        prev = re.sub(r"\s+", " ", str(previous_text or "").strip())
        curr = re.sub(r"\s+", " ", str(current_text or "").strip())
        if not curr:
            return "无新增内容"
        if not prev:
            return curr[:limit]
        if curr == prev:
            return "与上一条相比无显著变化"
        prev_slices = {
            prev[i : i + 16]
            for i in range(0, max(len(prev) - 15, 1), 8)
            if prev[i : i + 16].strip()
        }
        sentences = [s.strip() for s in re.split(r"[。！？；\n]", curr) if s.strip()]
        picked = [s for s in sentences if not any(chunk in s for chunk in prev_slices)]
        merged = "；".join(picked[:4]).strip() or curr[:limit]
        return merged[:limit]

    def _extract_event_candidate_signals(
        self,
        *,
        snapshot_delta: str,
        environment_summary: str,
        environment_delta: str,
        conversation_summary: str,
        schedule_alignment: str,
        plan_delta: str,
        recent_events: list[EventAnchor],
        recent_key_records: list[KeyRecord],
        recent_manual_events: list[EventAnchor],
        recent_manual_key_records: list[KeyRecord],
    ) -> dict:
        blob = "\n".join(
            [snapshot_delta, environment_summary, environment_delta, conversation_summary, plan_delta]
        )
        lower_blob = blob.lower()
        decision_words = (
            "决定",
            "改成",
            "调整为",
            "开始",
            "停止",
            "取消",
            "确认要",
            "决定先",
        )
        commitment_words = (
            "承诺",
            "约定",
            "共识",
            "原则",
            "答应",
            "以后会",
            "不会放弃",
        )
        emotion_words = (
            "害怕",
            "难过",
            "安心",
            "心疼",
            "想哭",
            "被触动",
            "终于说出来",
            "不想失去",
        )
        relation_words = (
            "关系",
            "边界",
            "信任",
            "靠近",
            "距离",
            "陪着",
            "不再",
            "理解了",
        )
        medical_words = (
            "复诊",
            "诊断",
            "用药",
            "药量",
            "剂量",
            "吸入",
            "症状",
            "炎症",
            "指标",
            "停药",
        )
        date_words = ("复诊", "截止", "到期", "纪念日", "明天", "今天", "deadline")
        external_words = ("搬家", "出行", "项目", "工作", "课程", "任务", "突发", "打断")
        user_marker_words = ("记住这一刻", "记下来", "值得被记住", "标记为事件", "记一笔")
        embodied_words = ("呼吸", "胸口", "身体", "发热", "冷", "疼", "喘", "心跳", "手在抖")
        routine_words = ("吃饭", "上课", "睡觉", "洗漱", "按时吃药", "例行", "打卡")
        administrative_words = ("填表", "排队", "挂号", "缴费", "行政", "手续", "提交材料")

        event_blob = "\n".join(f"{e.title}\n{e.description}" for e in recent_events).lower()
        key_record_blob = "\n".join(f"{r.title}\n{r.content_text}" for r in recent_key_records).lower()
        manual_event_blob = "\n".join(f"{e.title}\n{e.description}" for e in recent_manual_events).lower()
        manual_key_record_blob = "\n".join(f"{r.title}\n{r.content_text}" for r in recent_manual_key_records).lower()
        novelty_score = self._keyword_novelty_score(lower_blob, event_blob, key_record_blob)
        manual_event_overlap = self._keyword_overlap_score(lower_blob, manual_event_blob)
        manual_key_record_overlap = self._keyword_overlap_score(lower_blob, manual_key_record_blob)
        has_path_rewrite = self._detect_path_rewrite(
            blob,
            schedule_alignment=schedule_alignment,
            plan_delta=plan_delta,
        )
        return {
            "has_explicit_decision": any(w in blob for w in decision_words),
            "has_commitment_or_agreement": any(w in blob for w in commitment_words),
            "has_emotional_turn": any(w in blob for w in emotion_words),
            "has_relationship_shift": any(w in blob for w in relation_words),
            "has_medical_action": any(w in blob for w in medical_words),
            "has_dialogue_turning_point": bool(conversation_summary.strip()) and any(w in blob for w in commitment_words + decision_words + relation_words),
            "has_important_date_or_deadline": any(w in blob for w in date_words) or bool(re.search(r"\d{4}-\d{2}-\d{2}", blob)),
            "has_external_state_change": any(w in blob for w in external_words),
            "has_user_marker": any(w in blob for w in user_marker_words),
            "has_path_rewrite": has_path_rewrite,
            "has_embodied_signal": any(word in blob for word in embodied_words),
            "is_repetitive_daily_behavior": any(word in blob for word in routine_words),
            "is_administrative_only": any(word in blob for word in administrative_words),
            "is_pure_state_data": bool(
                re.search(r"\d+(?:\.\d+)?\s*(?:bpm|ml|mg|kg|次/分|次|℃|%|分钟|小时)", blob)
            ),
            "continuity_only": (
                ("无显著变化" in snapshot_delta or "延续" in snapshot_delta or "持续" in snapshot_delta)
                and ("无显著变化" in environment_delta or "延续" in environment_delta or "持续" in environment_delta)
            ),
            "novelty_score": novelty_score,
            "matched_recent_event": novelty_score < 0.35 and bool(event_blob),
            "matched_key_record": novelty_score < 0.28 and bool(key_record_blob),
            "manual_event_overlap": manual_event_overlap,
            "manual_key_record_overlap": manual_key_record_overlap,
            "has_manual_event": bool(recent_manual_events),
            "has_manual_key_record": bool(recent_manual_key_records),
        }

    @staticmethod
    def _keyword_novelty_score(blob: str, event_blob: str, key_record_blob: str) -> float:
        tokens = {
            token.strip().lower()
            for token in re.findall(r"[\u4e00-\u9fff]{2,8}|[A-Za-z][A-Za-z0-9_-]{2,20}", blob)
            if len(token.strip()) >= 2
        }
        if not tokens:
            return 0.2
        overlap = sum(1 for token in tokens if token in event_blob or token in key_record_blob)
        score = 1.0 - (overlap / max(len(tokens), 1))
        return round(max(0.0, min(score, 1.0)), 4)

    @staticmethod
    def _keyword_overlap_score(blob: str, reference_blob: str) -> float:
        if not reference_blob.strip():
            return 0.0
        tokens = {
            token.strip().lower()
            for token in re.findall(r"[\u4e00-\u9fff]{2,8}|[A-Za-z][A-Za-z0-9_-]{2,20}", blob)
            if len(token.strip()) >= 2
        }
        if not tokens:
            return 0.0
        overlap = sum(1 for token in tokens if token in reference_blob)
        return round(max(0.0, min(overlap / max(len(tokens), 1), 1.0)), 4)

    @staticmethod
    def _build_event_dedup_context(
        recent_events: list[EventAnchor],
        recent_key_records: list[KeyRecord],
        recent_manual_events: list[EventAnchor],
        recent_manual_key_records: list[KeyRecord],
    ) -> str:
        parts: list[str] = []
        if recent_manual_events:
            parts.append("【本轮对话中已手动记录的事件】")
            for event in recent_manual_events[:5]:
                title = (event.title or "").strip() or "未命名事件"
                parts.append(f"- [{event.date}] {title}：{(event.description or '').strip()[:120]}")
        if recent_manual_key_records:
            parts.append("【本轮对话中已手动记录的关键记录】")
            for record in recent_manual_key_records[:5]:
                parts.append(f"- [{record.type}] {(record.title or '').strip()}：{(record.content_text or '').strip()[:120]}")
        if recent_events:
            parts.append("【近期事件】")
            for event in recent_events[:5]:
                title = (event.title or "").strip() or "未命名事件"
                parts.append(f"- [{event.date}] {title}：{(event.description or '').strip()[:120]}")
        if recent_key_records:
            parts.append("【近期关键记录】")
            for record in recent_key_records[:5]:
                parts.append(f"- [{record.type}] {(record.title or '').strip()}：{(record.content_text or '').strip()[:120]}")
        return "\n".join(parts) if parts else "【暂无可用于去重的近期记录】"

    async def _judge_event_trigger(
        self,
        *,
        snapshot_delta: str,
        environment_summary: str,
        environment_body: str,
        recent_disturbances: str,
        environment_delta: str,
        trigger_signals: dict,
        dedup_context: str,
    ) -> dict:
        fallback = self._heuristic_event_judgment(trigger_signals)
        system_prompt = await self.prompt_manager.get_system_prompt()
        prompt_template = await self.prompt_manager.get_prompt(KEY_PROMPT_EVENT_TRIGGER_JUDGE)
        if not prompt_template.strip():
            return fallback
        prompt = prompt_template.format(
            snapshot_delta=snapshot_delta,
            environment_summary=environment_summary,
            environment_body=environment_body,
            recent_disturbances=recent_disturbances or "(no recent disturbances)",
            environment_delta=environment_delta,
            trigger_signals=json.dumps(trigger_signals, ensure_ascii=False),
            dedup_context=dedup_context,
        )
        try:
            response = await self.snapshot_llm.chat(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=400,
            )
            parsed = self._extract_json_object(response)
            route = str(parsed.get("route") or "").strip()
            if route not in {"generate_event", "convert_to_key_record_candidate", "suppress_to_snapshot_only"}:
                return fallback
            parsed["should_generate"] = route == "generate_event"
            parsed["trigger_types"] = self._parse_json_list(parsed.get("trigger_types"))
            parsed["reason"] = str(parsed.get("reason") or fallback.get("reason") or "").strip()
            parsed["novelty_level"] = str(parsed.get("novelty_level") or "medium").strip() or "medium"
            parsed = self._apply_event_policy_filter(parsed, trigger_signals)
            return parsed
        except Exception:
            return fallback

    @staticmethod
    def _heuristic_event_judgment(trigger_signals: dict) -> dict:
        high_value = any(
            bool(trigger_signals.get(key))
            for key in (
                "has_explicit_decision",
                "has_commitment_or_agreement",
                "has_dialogue_turning_point",
                "has_emotional_turn",
                "has_relationship_shift",
                "has_medical_action",
                "has_embodied_signal",
                "has_important_date_or_deadline",
                "has_user_marker",
            )
        )
        if float(trigger_signals.get("manual_key_record_overlap") or 0.0) >= 0.35:
            return {
                "should_generate": False,
                "route": "convert_to_key_record_candidate",
                "trigger_types": ["manual_key_record_overlap"],
                "reason": "本轮对话中已有人手动提取了相近关键记录，后台不重复归档",
                "novelty_level": "low",
            }
        if float(trigger_signals.get("manual_event_overlap") or 0.0) >= 0.4:
            return {
                "should_generate": False,
                "route": "suppress_to_snapshot_only",
                "trigger_types": ["manual_event_overlap"],
                "reason": "本轮对话中已有人手动记录了相近事件，后台仅补生活流痕迹",
                "novelty_level": "low",
            }
        if bool(trigger_signals.get("matched_key_record")) and not bool(trigger_signals.get("has_user_marker")):
            return {
                "should_generate": False,
                "route": "convert_to_key_record_candidate",
                "trigger_types": ["matched_key_record"],
                "reason": "与现有长期记录主题接近，更适合转为关键记录候选",
                "novelty_level": "low",
            }
        if bool(trigger_signals.get("continuity_only")) or not high_value:
            return {
                "should_generate": False,
                "route": "suppress_to_snapshot_only",
                "trigger_types": [],
                "reason": "主要是连续生活流或轻微波动，保留在快照中即可",
                "novelty_level": "low",
            }
        if bool(trigger_signals.get("is_repetitive_daily_behavior")) or bool(trigger_signals.get("is_administrative_only")):
            return {
                "should_generate": False,
                "route": "suppress_to_snapshot_only",
                "trigger_types": ["routine_or_administrative"],
                "reason": "重复性日常或纯行政处理仅保留为生活流痕迹，不自动升格为事件",
                "novelty_level": "low",
            }
        if bool(trigger_signals.get("is_pure_state_data")) and not (
            bool(trigger_signals.get("has_medical_action"))
            or bool(trigger_signals.get("has_embodied_signal"))
            or bool(trigger_signals.get("has_user_marker"))
        ):
            return {
                "should_generate": False,
                "route": "suppress_to_snapshot_only",
                "trigger_types": ["pure_state_data"],
                "reason": "纯状态监测数据保留在快照、生活流或健康监测中，不单独生成事件",
                "novelty_level": "low",
            }
        if bool(trigger_signals.get("has_external_state_change")) and not bool(trigger_signals.get("has_path_rewrite")):
            return {
                "should_generate": False,
                "route": "suppress_to_snapshot_only",
                "trigger_types": ["external_state_change"],
                "reason": "外部变化尚未改写后续生活路径，仅保留为生活流痕迹",
                "novelty_level": "low",
            }
        novelty = float(trigger_signals.get("novelty_score") or 0.0)
        if bool(trigger_signals.get("has_medical_action")) and novelty < 0.55:
            return {
                "should_generate": False,
                "route": "convert_to_key_record_candidate",
                "trigger_types": ["medical_action"],
                "reason": "内容更像长期医疗或监测信息，转为关键记录候选",
                "novelty_level": "medium",
            }
        trigger_types = [
            key.removeprefix("has_")
            for key, value in trigger_signals.items()
            if key.startswith("has_") and bool(value)
        ]
        return {
            "should_generate": True,
            "route": "generate_event",
            "trigger_types": trigger_types[:3],
            "reason": "检测到明确转折或可追踪变化，生成正式事件",
            "novelty_level": "high" if novelty >= 0.55 else "medium",
        }

    def _apply_event_policy_filter(self, judgment: dict, trigger_signals: dict) -> dict:
        route = str(judgment.get("route") or "").strip()
        if route != "generate_event":
            return judgment
        if bool(trigger_signals.get("has_external_state_change")) and not bool(trigger_signals.get("has_path_rewrite")):
            return {
                "should_generate": False,
                "route": "suppress_to_snapshot_only",
                "trigger_types": ["external_state_change"],
                "reason": "外部变化尚未改写后续生活路径，仅保留为生活流痕迹",
                "novelty_level": "low",
            }
        anchor_signals = any(
            bool(trigger_signals.get(key))
            for key in (
                "has_explicit_decision",
                "has_commitment_or_agreement",
                "has_dialogue_turning_point",
                "has_emotional_turn",
                "has_relationship_shift",
                "has_medical_action",
                "has_embodied_signal",
                "has_important_date_or_deadline",
                "has_user_marker",
            )
        )
        if not anchor_signals and not (
            bool(trigger_signals.get("has_external_state_change"))
            and bool(trigger_signals.get("has_path_rewrite"))
        ):
            return {
                "should_generate": False,
                "route": "suppress_to_snapshot_only",
                "trigger_types": [],
                "reason": "变化尚未形成独立记忆锚点，保留为生活流痕迹",
                "novelty_level": "low",
            }
        judgment["should_generate"] = True
        return judgment

    async def _materialize_deferred_event(
        self,
        *,
        snapshot: StateSnapshot,
        snapshot_delta: str,
        environment_summary: str,
        environment_body: str,
        detail_hooks_text: str,
        recent_disturbances: str,
        environment_delta: str,
        judgment: dict,
        defer_vectorization: bool,
    ) -> int | None:
        system_prompt = await self.prompt_manager.get_system_prompt()
        prompt_template = await self.prompt_manager.get_prompt(KEY_PROMPT_EVENT_MATERIALIZE)
        if prompt_template.strip():
            try:
                response = await self.snapshot_llm.chat(
                    [
                        {"role": "system", "content": system_prompt},
                        {
                            "role": "user",
                            "content": prompt_template.format(
                                snapshot_delta=snapshot_delta,
                                environment_summary=environment_summary,
                                environment_body=environment_body,
                                detail_hooks_text=detail_hooks_text,
                                recent_disturbances=recent_disturbances or "(no recent disturbances)",
                                environment_delta=environment_delta,
                                trigger_reason=str(judgment.get("reason") or ""),
                                trigger_types=", ".join(self._parse_json_list(judgment.get("trigger_types"))),
                            ),
                        },
                    ],
                    max_tokens=600,
                )
                event_id = await self._parse_and_save_event(
                    response,
                    source="generated",
                    date_override=snapshot.created_at.split("T")[0] if snapshot.created_at else None,
                    defer_vectorization=defer_vectorization,
                )
                if event_id is not None:
                    return event_id
            except Exception:
                logger.exception("Deferred event materialization prompt failed.")
        fallback_title = make_event_title(
            environment_summary or snapshot_delta,
            self._parse_json_list(judgment.get("trigger_types")),
            None,
        )
        description = self._compose_event_description(
            environment_delta or environment_summary or snapshot_delta,
            str(judgment.get("reason") or "").strip() or snapshot_delta,
            detail_hooks_text,
            self._extract_labeled_env_summary_value(environment_summary, "Open loop"),
        )
        event = EventAnchor(
            date=snapshot.created_at.split("T")[0] if snapshot.created_at else shanghai_now().date().isoformat(),
            title=fallback_title,
            description=description,
            source="generated",
            created_at=format_utc_instant_z(shanghai_time_to_utc_naive(shanghai_now())),
            trigger_keywords=json.dumps(self._parse_json_list(judgment.get("trigger_types")), ensure_ascii=False),
            categories=json.dumps(classify_event(description, self._parse_json_list(judgment.get("trigger_types"))), ensure_ascii=False),
        )
        event_id = await self.db.insert_event(event)
        if not defer_vectorization:
            upsert_event_vector = getattr(self.memory, "upsert_event_vector", None)
            if callable(upsert_event_vector):
                try:
                    await upsert_event_vector(int(event_id))
                except Exception:
                    logger.warning("Event vector upsert skipped for #%d", int(event_id))
        return int(event_id)

    async def _route_key_record_candidate(
        self,
        *,
        snapshot_delta: str,
        environment_summary: str,
        environment_delta: str,
        judgment: dict,
    ) -> dict:
        fallback = self._heuristic_key_record_candidate(
            snapshot_delta=snapshot_delta,
            environment_summary=environment_summary,
            environment_delta=environment_delta,
            judgment=judgment,
        )
        system_prompt = await self.prompt_manager.get_system_prompt()
        prompt_template = await self.prompt_manager.get_prompt(KEY_PROMPT_KEY_RECORD_CANDIDATE_ROUTE)
        if not prompt_template.strip():
            return fallback
        try:
            response = await self.snapshot_llm.chat(
                [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": prompt_template.format(
                            snapshot_delta=snapshot_delta,
                            environment_summary=environment_summary,
                            environment_delta=environment_delta,
                            trigger_reason=str(judgment.get("reason") or ""),
                        ),
                    },
                ],
                max_tokens=400,
            )
            parsed = self._extract_json_object(response)
            if not str(parsed.get("record_type") or "").strip():
                return fallback
            parsed["tags"] = self._parse_json_list(parsed.get("tags"))
            return parsed
        except Exception:
            return fallback

    def _heuristic_key_record_candidate(
        self,
        *,
        snapshot_delta: str,
        environment_summary: str,
        environment_delta: str,
        judgment: dict,
    ) -> dict:
        blob = "\n".join([snapshot_delta, environment_summary, environment_delta]).strip()
        record_type = "life_pattern"
        if any(word in blob for word in ("复诊", "日期", "截止", "之前")):
            record_type = "medical_review_date"
        elif any(word in blob for word in ("用药", "剂量", "吸入", "停药")):
            record_type = "medication_protocol"
        elif any(word in blob for word in ("指标", "波动", "症状", "炎症")):
            record_type = "health_monitoring"
        elif any(word in blob for word in ("承诺", "共识", "原则")):
            record_type = "commitment_agreement"
        title = (blob.split("。")[0].split("；")[0].strip() or "后台候选关键记录")[:48]
        return {
            "record_type": record_type,
            "title": title,
            "content_text": (environment_delta or environment_summary or snapshot_delta)[:180],
            "tags": self._parse_json_list(judgment.get("trigger_types")),
            "start_date": "",
            "end_date": "",
            "update_hint": "new_record",
        }

    @staticmethod
    def _extract_json_object(text: str) -> dict:
        raw = (text or "").strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            pass
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(raw[start:end + 1])
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}
        return {}

    @staticmethod
    def _parse_json_list(value) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(x).strip() for x in value if str(x).strip()]
        raw = str(value).strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:
            pass
        return [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]

    def _schedule_deferred_env_retry(
        self,
        *,
        snapshot_id: int,
        event_id: int | None,
        checkpoint_time: datetime,
        previous_snapshot_content: str,
        previous_env: dict | None,
        checkpoint_events: list[dict],
        snapshot_type: str,
        snapshot_created_at: str,
    ) -> None:
        self._deferred_env_retry_queue.append(
            {
                "snapshot_id": snapshot_id,
                "event_id": event_id,
                "checkpoint_time": checkpoint_time,
                "previous_snapshot_content": previous_snapshot_content,
                "previous_env": previous_env,
                "checkpoint_events": checkpoint_events,
                "snapshot_type": snapshot_type,
                "snapshot_created_at": snapshot_created_at,
            }
        )
        if self._deferred_env_retry_task and not self._deferred_env_retry_task.done():
            return
        self._deferred_env_retry_task = asyncio.create_task(
            self._drain_deferred_env_retry_queue()
        )

    async def _drain_deferred_env_retry_queue(self) -> None:
        while self._deferred_env_retry_queue:
            item = self._deferred_env_retry_queue.pop(0)
            try:
                await self._retry_stale_environment(item)
            except Exception:
                logger.exception(
                    "Deferred environment retry failed for snapshot=%s",
                    item.get("snapshot_id"),
                )

    async def _retry_stale_environment(self, item: dict) -> None:
        async with self._env_retry_lock:
            snapshot_id = int(item.get("snapshot_id") or 0)
            if snapshot_id <= 0:
                return
            snapshot = await self.db.get_snapshot_by_id(snapshot_id)
            if snapshot is None:
                return
            checkpoint_time = item.get("checkpoint_time")
            if not isinstance(checkpoint_time, datetime):
                return
            previous_snapshot_content = str(item.get("previous_snapshot_content") or "")
            previous_env = item.get("previous_env")
            checkpoint_events = item.get("checkpoint_events") or []
            if not isinstance(checkpoint_events, list):
                checkpoint_events = []

            events_text = self._format_checkpoint_event_dicts(checkpoint_events)
            world_books = await self.db.get_active_world_books()
            world_book_payload: list[dict] = [self._world_book_to_dict(wb) for wb in world_books]
            world_book_entries = await self._retrieve_world_book_entries(
                query=f"{previous_snapshot_content}\n{events_text}",
                entries=world_book_payload,
            )

            env = await self.env_gen.generate(
                time_point=checkpoint_time,
                previous_env=previous_env if isinstance(previous_env, dict) else None,
                context={
                    "latest_snapshot": previous_snapshot_content,
                    "time_delta_hours": 0.0,
                    "recent_events": checkpoint_events,
                    "world_book_entries": world_book_entries,
                    "current_plan_activity": (
                        await self.plan_engine.get_plan_activity_for_time(checkpoint_time)
                        if self.plan_engine is not None
                        else ""
                    ),
                },
                allow_retry_fallback=False,
            )
            environment_text = environment_text_for_prompt(env)
            retrieval_text = environment_text_for_retrieval(env)
            memory_results = await self.memory.search(
                retrieval_text or environment_text,
                top_k=self.DEFAULT_MEMORY_TOP_K,
            )
            memory_text, _memory_meta = self._build_memory_context(memory_results)

            system_prompt = await self.prompt_manager.get_system_prompt()
            prompt_template = await self.prompt_manager.get_prompt(
                KEY_PROMPT_SNAPSHOT_GENERATION
            )
            character_background = await self.prompt_manager.get_layer_content(KEY_L1_CHARACTER_BACKGROUND)
            character_personality = await self.prompt_manager.get_layer_content(KEY_L2_CHARACTER_PERSONALITY)
            relationship_dynamics = await self.prompt_manager.get_layer_content(KEY_L2_RELATIONSHIP_DYNAMICS)
            life_status = await self.prompt_manager.get_layer_content(KEY_L2_LIFE_STATUS)
            prompt = prompt_template.format(
                character_background=character_background,
                character_personality=character_personality,
                relationship_dynamics=relationship_dynamics,
                life_status=life_status,
                environment=environment_text,
                previous_snapshot=previous_snapshot_content,
                recent_events=events_text,
                memory_context=memory_text,
            )
            new_content = await self.snapshot_llm.chat(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=None,
            )

            remove_vector = getattr(self.memory, "remove_vector", None)
            if callable(remove_vector) and snapshot.embedding_vector_id:
                try:
                    await remove_vector(f"snapshot_{snapshot_id}")
                except Exception:
                    logger.warning("Snapshot vector cleanup skipped for #%d", snapshot_id)
            await self.db.update_snapshot(
                snapshot_id,
                content=new_content,
                environment=json.dumps(env, ensure_ascii=False),
            )

            self._schedule_deferred_event_generation(
                snapshot_id=snapshot_id,
                snapshot_content=new_content,
                environment=env,
                memory_text=memory_text,
                checkpoint_time=checkpoint_time,
                defer_vectorization=True,
            )

            logger.info("Deferred environment retry refreshed snapshot=%d", snapshot_id)

    @staticmethod
    def _format_checkpoint_event_dicts(events: list[dict]) -> str:
        if not events:
            return "（无近期事件记录）"
        lines: list[str] = []
        for event in events:
            date = str(event.get("date") or "").strip()
            description = str(event.get("description") or "").strip()
            title = str(event.get("title") or "").strip()
            label = description or title or "未命名事件"
            prefix = f"[{date}] " if date else ""
            lines.append(f"- {prefix}{label}")
        return "\n".join(lines)

    @staticmethod
    def _append_automation_report(content: str, report: dict | None) -> str:
        if not report:
            return content
        if not report.get("ran"):
            return content
        vector_sync = report.get("vector_sync") or {}
        evolution = report.get("evolution") or {}
        compaction = report.get("compaction") or {}
        llm_usage = report.get("llm_usage") or {}
        lines: list[str] = ["", "[自动记忆整理报告]"]
        if vector_sync:
            lines.append(
                f"- 向量同步：事件 {int(vector_sync.get('vectorized_events', 0))} 条，"
                f"快照 {int(vector_sync.get('vectorized_snapshots', 0))} 条。"
            )
        if evolution:
            status = evolution.get("status") or {}
            if evolution.get("pending_confirmation"):
                lines.append(
                    f"- 人格演化：已自动生成预览，待前往 Web 前端确认应用（新事件 {int(status.get('event_count', 0))} 条，"
                    f"候选 {int(evolution.get('candidate_count', 0))} 条）。"
                )
            else:
                lines.append(
                    f"- 人格演化：本次未触发（新事件 {int(status.get('event_count', 0))}/"
                    f"阈值 {int(status.get('threshold', 0))}）。"
                )
        if compaction:
            created = int(compaction.get("created_summaries", 0))
            deleted = int(compaction.get("deleted_originals", 0))
            if created or deleted:
                lines.append(f"- 冷记忆压缩：新增摘要 {created} 条，标记旧向量 {deleted} 条。")
            else:
                lines.append("- 冷记忆压缩：暂无可压缩候选。")
        errors = report.get("errors") or []
        if errors:
            lines.append(f"- 异常：{'; '.join(str(e) for e in errors[:2])}")
        if llm_usage:
            lines.append(
                f"- Token统计：输入 {int(llm_usage.get('prompt_tokens', 0))}，"
                f"输出 {int(llm_usage.get('completion_tokens', 0))}，"
                f"总计 {int(llm_usage.get('total_tokens', 0))}（请求 {int(llm_usage.get('requests', 0))} 次）。"
            )
        return content + "\n".join(lines)
    @staticmethod
    def _format_events(events: list[EventAnchor]) -> str:
        parts = []
        for e in events:
            parts.append(f"- [{e.date}] {e.description}")
        return "\n".join(parts)

    @staticmethod
    def _format_memories(memories) -> str:
        parts: list[str] = []
        for m in memories:
            label = "事件" if m.source_type == "event" else "快照"
            parts.append(f"- [{label}] {m.text}")
        return "\n".join(parts)

    @staticmethod
    def _format_periodic_events(events: list[EventAnchor]) -> str:
        if not events:
            return "（该时间段内无事件记录）"
        lines: list[str] = []
        for e in events:
            title = (e.title or "").strip() or "未命名事件"
            lines.append(f"- [{e.date}] {title}：{e.description[:160]}")
        return "\n".join(lines)

    @staticmethod
    def _format_periodic_snapshots(snapshots: list[StateSnapshot]) -> str:
        if not snapshots:
            return "（该时间段内无状态快照记录）"
        lines: list[str] = []
        for s in snapshots:
            created = s.created_at.split("T")[0] if s.created_at else "未知时间"
            lines.append(f"- [{created}] ({s.type}) {s.content[:180]}")
        return "\n".join(lines)

    @staticmethod
    def _build_periodic_stats(events: list[EventAnchor], snapshots: list[StateSnapshot]) -> str:
        category_count: dict[str, int] = {}
        source_count: dict[str, int] = {}
        for e in events:
            source_count[e.source] = source_count.get(e.source, 0) + 1
            try:
                categories = json.loads(e.categories or "[]")
            except Exception:
                categories = []
            for c in categories:
                if not c:
                    continue
                category_count[c] = category_count.get(c, 0) + 1

        category_text = "、".join(
            [f"{name}({count})" for name, count in sorted(category_count.items(), key=lambda x: (-x[1], x[0]))]
        ) or "无"
        source_text = "、".join(
            [f"{name}({count})" for name, count in sorted(source_count.items(), key=lambda x: (-x[1], x[0]))]
        ) or "无"
        return (
            f"事件总数：{len(events)}\n"
            f"快照总数：{len(snapshots)}\n"
            f"事件来源分布：{source_text}\n"
            f"事件分类分布：{category_text}"
        )

    @staticmethod
    def _parse_hours_setting(raw: str | None, default_hours: float) -> float:
        """将设定里的「小时」解析为浮点小时数。支持 8、0.5、8h、8 h、8小时 等；失败则用 default_hours。"""
        text = (raw or "").strip()
        if not text:
            return default_hours
        try:
            v = float(text.replace(",", "."))
            if v > 0 and v == v:
                return v
        except (TypeError, ValueError):
            pass
        m = re.search(r"[-+]?\d*\.?\d+", text.replace(",", "."))
        if m:
            try:
                v = float(m.group(0))
                if v > 0 and v == v:
                    return v
            except ValueError:
                pass
        return default_hours

    async def _get_min_time_unit_timedelta(self) -> timedelta:
        """解析 min_time_unit_hours 设定为时间间隔；支持小数与常见后缀（如 8h）。"""
        raw = await self.prompt_manager.get_config_value(KEY_MIN_TIME_UNIT_HOURS)
        default_h = float(self.config.environment.min_time_unit_hours)
        hours = self._parse_hours_setting(raw, default_h)
        td = timedelta(hours=hours)
        # 避免过小或浮点退化导致整除为 0、检查点异常
        min_sec = 1.0
        if td.total_seconds() < min_sec:
            td = timedelta(seconds=int(min_sec))
        return td

    async def _get_inject_hot_events_limit(self) -> int:
        raw = await self.prompt_manager.get_config_value(KEY_INJECT_HOT_EVENTS_LIMIT)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 5
        return max(1, min(value, 50))

    async def _get_inject_yesterday_events_limit(self) -> int:
        raw = await self.prompt_manager.get_config_value(KEY_INJECT_YESTERDAY_EVENTS_LIMIT)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 5
        return max(1, min(value, 50))

    @staticmethod
    def _parse_bool_setting(raw: str | None, default: bool) -> bool:
        if raw is None:
            return default
        text = str(raw).strip().lower()
        if not text:
            return default
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        return default

    @staticmethod
    def _parse_int_setting(raw: str | None, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
        try:
            value = int(str(raw).strip())
        except (AttributeError, TypeError, ValueError):
            value = default
        value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    async def _get_snapshot_scheduler_enabled(self) -> bool:
        raw = await self.prompt_manager.get_config_value(KEY_SNAPSHOT_SCHEDULER_ENABLED)
        return self._parse_bool_setting(raw, True)

    async def _get_snapshot_scheduler_interval_sec(self) -> int:
        raw = await self.prompt_manager.get_config_value(KEY_SNAPSHOT_SCHEDULER_INTERVAL_SEC)
        return self._parse_int_setting(
            raw,
            self.DEFAULT_SCHEDULER_INTERVAL_SEC,
            minimum=5,
            maximum=3600,
        )

    async def _get_snapshot_catchup_max_steps(self) -> int:
        raw = await self.prompt_manager.get_config_value(KEY_SNAPSHOT_CATCHUP_MAX_STEPS_PER_RUN)
        return self._parse_int_setting(
            raw,
            self.DEFAULT_CATCHUP_MAX_STEPS,
            minimum=1,
            maximum=24,
        )

    async def _get_snapshot_recent_events_limit(self) -> int:
        raw = await self.prompt_manager.get_config_value(KEY_SNAPSHOT_RECENT_EVENTS_LIMIT)
        return self._parse_int_setting(
            raw,
            self.DEFAULT_RECENT_EVENTS_LIMIT,
            minimum=1,
            maximum=20,
        )

    async def _get_snapshot_event_candidate_enabled(self) -> bool:
        raw = await self.prompt_manager.get_config_value(KEY_SNAPSHOT_EVENT_CANDIDATE_ENABLED)
        return self._parse_bool_setting(raw, True)

    @staticmethod
    def _world_book_to_dict(item) -> dict:
        keywords: list[str] = []
        tags: list[str] = []
        try:
            raw_keywords = json.loads(item.match_keywords or "[]")
            if isinstance(raw_keywords, list):
                keywords = [str(x).strip() for x in raw_keywords if str(x).strip()]
        except Exception:
            pass
        try:
            raw_tags = json.loads(item.tags or "[]")
            if isinstance(raw_tags, list):
                tags = [str(x).strip() for x in raw_tags if str(x).strip()]
        except Exception:
            pass
        return {
            "id": int(item.id or 0),
            "name": str(item.name or ""),
            "content": str(item.content or ""),
            "match_keywords": keywords,
            "tags": tags,
            "embedding_vector_id": item.embedding_vector_id,
        }

    @staticmethod
    def _extract_keywords_for_world_books(query: str) -> list[str]:
        text = (query or "").strip().lower()
        if not text:
            return []
        return list(
            dict.fromkeys(
                re.findall(r"[a-z0-9_\u4e00-\u9fff]{2,}", text)
            )
        )[:80]

    async def _retrieve_world_book_entries(self, query: str, entries: list[dict]) -> list[dict]:
        if not entries:
            return []
        keyword_scores = self._world_book_keyword_scores(query, entries)
        ranked_keywords = sorted(keyword_scores.items(), key=lambda x: x[1], reverse=True)[:6]
        selected_ids = [item_id for item_id, _ in ranked_keywords]

        search_world_books = getattr(self.memory, "search_world_books", None)
        if callable(search_world_books):
            try:
                vector_hits = await search_world_books(
                    query=query,
                    top_k=4,
                    candidate_ids=[int(e.get("id") or 0) for e in entries],
                )
                for hit in vector_hits:
                    hit_id = int(hit.get("id") or 0)
                    if hit_id > 0 and hit_id not in selected_ids:
                        selected_ids.append(hit_id)
            except Exception:
                pass

        if not selected_ids:
            selected_ids = [int(e.get("id") or 0) for e in entries[:3]]

        by_id = {int(e.get("id") or 0): e for e in entries}
        result: list[dict] = []
        for item_id in selected_ids:
            item = by_id.get(item_id)
            if not item:
                continue
            result.append(item)
            if len(result) >= 8:
                break
        return result

    @staticmethod
    def _parse_iso_datetime(value: str) -> datetime:
        return parse_db_instant_to_shanghai(value)
