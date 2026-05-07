from __future__ import annotations

import asyncio
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
    StateSnapshot,
    EventAnchor,
    KeyRecord,
    LifeFlowTrace,
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

    def set_plan_engine(self, plan_engine) -> None:
        self.plan_engine = plan_engine

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
        now = format_utc_instant_z(datetime.utcnow())
        trace = LifeFlowTrace(
            trace_date=trace_date,
            source=source,  # type: ignore[arg-type]
            summary=text,
            details_json=json.dumps(details or {}, ensure_ascii=False),
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
                trace_summary = conversation_summary.strip()
                if impacted_summary:
                    trace_summary = (
                        f"对话占用了原日程：{impacted_summary}。"
                        f"{trace_summary[:220]}"
                    ).strip()
                await self._trace_await(
                    tracer,
                    "append_conversation_life_flow_trace",
                    self._append_life_flow_trace(
                        trace_date=shanghai_now().date().isoformat(),
                        source="conversation",
                        summary=trace_summary[:320],
                        details=trace_details,
                        schedule_alignment=schedule_alignment,
                        related_snapshot_id=int(snap_id or 0),
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
        today_events_text = await self._build_today_events_text(today_str, limit=today_limit)
        yesterday_events_text = await self._build_yesterday_events_text(
            yesterday_str, limit=yesterday_limit
        )
        plan_summary = "（今日尚无计划）"
        if self.plan_engine is not None:
            try:
                plan_summary = await self.plan_engine.get_plan_summary_text()
            except Exception:
                logger.exception("Failed to load plan summary for injectable context.")
        recent_trace_text = await self._build_recent_life_flow_trace_text()
        latest_claims = await self.db.list_conversation_time_claims(status="closed", limit=1)
        latest_claim_summary = "（最近没有对话占时改写记录）"
        if latest_claims:
            latest_claim_summary = (
                str(latest_claims[0].context_summary or "").strip()[:220]
                or latest_claim_summary
            )
        recent_trace_block = recent_trace_text or "（今日/昨日暂无生活流痕迹）"
        return (
            "【L1 稳定层】\n"
            f"角色背景：{l1_char}\n\n"
            f"用户背景：{l1_user}\n\n"
            "【L2 动态层】\n"
            f"角色人格：{l2_char}\n\n"
            f"关系模式：{l2_rel}\n\n"
            f"生活状态：{l2_life}\n\n"
            f"【昨日事件（{yesterday_str}，按重要性）】\n"
            f"{yesterday_events_text}\n\n"
            f"【今日事件（{today_str}）】\n"
            f"{today_events_text}\n\n"
            "【当前日程】\n"
            f"{plan_summary}\n\n"
            "【今日/昨日生活流痕迹】\n"
            f"{recent_trace_block}\n\n"
            "【最近一次对话占时改写】\n"
            f"{latest_claim_summary}\n\n"
            "【当前状态快照】\n"
            f"{snapshot_text}"
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
        existing = await self.db.get_key_record_by_type_title(normalized_type, title)
        if existing and update_if_exists:
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
            updated = await self.db.get_key_record_by_id(existing.id)  # type: ignore[arg-type]
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
        created = await self.db.get_key_record_by_id(record_id)
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

        existing = await self.db.get_event_by_date_title(event_date, normalized_title)
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
        rows = await self.db.search_key_records(
            query=query,
            top_k=cap,
            record_type=record_type,
            include_archived=include_archived,
        )

        kr_hint = (
            "【关键记录】用于承载对话中沉淀下来的结构化事实，例如约定、医嘱、计划、日期等，"
            "优先于下方设定条目采信。"
        )
        kr_list: list[dict] = []
        for r in rows:
            s = self._key_record_query_strength(query, r)
            d = r.model_dump()
            d["_result_kind"] = "key_record"
            d["_memory_tier"] = "primary"
            d["_relevance_score"] = round(s, 4)
            d["_usage_hint"] = kr_hint
            title = str(d.get("title") or "").strip() or "（未命名）"
            body = str(d.get("content_text") or "").strip()
            d["_content_for_prompt"] = f"【关键记录·优先采信】\n{title}\n{body}"
            d["_sort_recency"] = (r.updated_at or r.created_at or "").strip()
            kr_list.append(d)
        kr_list.sort(
            key=lambda d: (d.get("_sort_recency") or "", d.get("_relevance_score") or 0),
            reverse=True,
        )
        for d in kr_list:
            d.pop("_sort_recency", None)

        wb_max = min(3, tk)
        kr_slots = max(0, tk - wb_max)
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
    def _key_record_query_strength(query: str, record: KeyRecord) -> float:
        raw = (query or "").strip()
        if not raw:
            return 0.5
        kws = [k.strip() for k in re.split(r"[\s,，。;；、|/]+", raw) if k.strip()]
        if not kws:
            kws = [raw]
        parts = [
            record.title or "",
            record.content_text or "",
            record.tags or "",
            record.content_json or "",
        ]
        blob = " ".join(parts).lower()
        hit = sum(1 for kw in kws if kw.lower() in blob)
        return max(0.25, hit / max(len(kws), 1))

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
                        "schedule_alignment": environment_context_details.get("schedule_alignment", ""),
                        "plan_delta": environment_context_details.get("plan_delta", ""),
                    },
                ),
                checkpoint_time_cst=checkpoint_cst,
                time_delta_hours=round(time_delta_hours, 4),
                world_book_count=len(world_book_entries),
            )
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
    def _compose_event_description(objective: str, impression: str) -> str:
        objective_text = str(objective or "").strip()
        impression_text = str(impression or "").strip()
        if objective_text and impression_text:
            return f"客观记录：{objective_text}\n主观印象：{impression_text}"
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
        keywords: list[str] = []
        categories: list[str] = []

        title_match = re.search(r"(?:标题|title)\s*[:：]\s*(.+)", text, re.IGNORECASE)
        if title_match:
            title = title_match.group(1).strip()

        objective = self._extract_event_field_block(text, ["客观记录", "objective"])
        impression = self._extract_event_field_block(text, ["主观印象", "impression"])

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
            description = self._compose_event_description(objective, impression)

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
        keywords: list[str] = []
        categories: list[str] = []

        title_match = re.search(r"(?:标题|title)\s*[:：]\s*(.+)", text, re.IGNORECASE)
        if title_match:
            title = title_match.group(1).strip()

        objective = self._extract_event_field_block(text, ["客观记录", "objective"])
        impression = self._extract_event_field_block(text, ["主观印象", "impression"])

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
            description = self._compose_event_description(objective, impression)

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
        if trace_summary:
            await self._append_life_flow_trace(
                trace_date=snapshot.created_at.split("T")[0] if snapshot.created_at else shanghai_now().date().isoformat(),
                source="conversation" if str(item.get("conversation_summary") or "").strip() else "environment",
                summary=trace_summary[:320],
                details={
                    "snapshot_delta": snapshot_delta,
                    "environment_delta": environment_delta,
                    "conversation_summary": str(item.get("conversation_summary") or ""),
                    "summary": current_summary,
                },
                schedule_alignment=str(current_env.get("schedule_alignment") or "on_track"),
                related_snapshot_id=int(snapshot.id or 0),
            )

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
                environment_delta=environment_delta,
                judgment=judgment,
                defer_vectorization=bool(item.get("defer_vectorization")),
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

    @staticmethod
    def _build_life_flow_trace_summary(
        *,
        environment_summary: str,
        environment_delta: str,
        conversation_summary: str,
    ) -> str:
        if str(conversation_summary or "").strip():
            return str(conversation_summary or "").strip()
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
        recent_events: list[EventAnchor],
        recent_key_records: list[KeyRecord],
        recent_manual_events: list[EventAnchor],
        recent_manual_key_records: list[KeyRecord],
    ) -> dict:
        blob = "\n".join([snapshot_delta, environment_summary, environment_delta, conversation_summary])
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
                "has_external_state_change",
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

    async def _materialize_deferred_event(
        self,
        *,
        snapshot: StateSnapshot,
        snapshot_delta: str,
        environment_summary: str,
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
