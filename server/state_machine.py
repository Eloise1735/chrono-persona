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
from server.models import StateSnapshot, EventAnchor, KeyRecord, format_utc_instant_z
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
    KEY_PROMPT_REFLECT_SNAPSHOT,
    KEY_PROMPT_CONVERSATION_SUMMARY,
    KEY_PROMPT_PERIODIC_REVIEW,
    KEY_MIN_TIME_UNIT_HOURS,
    KEY_INJECT_HOT_EVENTS_LIMIT,
    KEY_SNAPSHOT_CATCHUP_MAX_STEPS_PER_RUN,
    KEY_SNAPSHOT_RECENT_EVENTS_LIMIT,
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
        self._env_retry_lock = asyncio.Lock()
        self._deferred_env_retry_queue: list[dict] = []
        self._deferred_env_retry_task: asyncio.Task | None = None

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
        return await self._get_snapshot_scheduler_interval_sec()

    async def get_snapshot_scheduler_public_info(self) -> dict[str, bool | int]:
        return {
            "enabled": await self._get_snapshot_scheduler_enabled(),
            "interval_sec": await self._get_snapshot_scheduler_interval_sec(),
        }

    async def run_snapshot_scheduler_tick(self) -> dict:
        async with self._advance_lock:
            enabled = await self._get_snapshot_scheduler_enabled()
            interval_sec = await self._get_snapshot_scheduler_interval_sec()
            if not enabled:
                return {
                    "status": "disabled",
                    "interval_sec": interval_sec,
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
                await self._trace_await(
                    tracer,
                    "db.insert_conversation_end_snapshot",
                    self.db.insert_snapshot(snap),
                    snapshot_chars=len(new_content or ""),
                )
            finally:
                llm_usage = self.snapshot_llm.end_usage_tracking()

            self._schedule_deferred_maintenance(
                trigger="reflect_on_conversation",
                llm_usage=llm_usage,
            )
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
        hot_limit = await self._get_inject_hot_events_limit()
        recent_events_text = await self._build_recent_events_text(limit=hot_limit)
        return (
            "【L1 稳定层】\n"
            f"角色背景：{l1_char}\n\n"
            f"用户背景：{l1_user}\n\n"
            "【L2 动态层】\n"
            f"角色人格：{l2_char}\n\n"
            f"关系模式：{l2_rel}\n\n"
            f"生活状态：{l2_life}\n\n"
            "【近期事件（热记忆）】\n"
            f"{recent_events_text}\n\n"
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
        record_type: str,
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
        tags = tags or []
        existing = await self.db.get_key_record_by_type_title(record_type, title)
        if existing and update_if_exists:
            fields = {
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
            type=record_type,  # type: ignore[arg-type]
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
            "record": created.model_dump() if created else {"id": record_id, "title": title},
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
            if defer_maintenance:
                if not env.get("stale"):
                    self._schedule_deferred_event_generation(
                        snapshot_id=snap_id,
                        snapshot_content=current_content,
                        environment=env,
                        memory_text=memory_text,
                        checkpoint_time=checkpoint_time,
                        defer_vectorization=True,
                    )
            else:
                generated_event_id = await self._trace_await(
                    diagnostic,
                    f"{trigger}.checkpoint_{checkpoint_index}.generate_event_anchor",
                    self._generate_event_anchor(
                        current_content,
                        env,
                        memory_text,
                        checkpoint_time,
                        defer_vectorization=False,
                    ),
                    checkpoint_time_cst=checkpoint_cst,
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
    ) -> None:
        self._deferred_event_queue.append(
            {
                "snapshot_id": snapshot_id,
                "snapshot_content": snapshot_content,
                "environment": environment or {},
                "memory_text": memory_text,
                "checkpoint_time": checkpoint_time,
                "defer_vectorization": defer_vectorization,
            }
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
                event_id = await self._generate_event_anchor(
                    str(item.get("snapshot_content") or ""),
                    item.get("environment") if isinstance(item.get("environment"), dict) else {},
                    str(item.get("memory_text") or ""),
                    item.get("checkpoint_time") if isinstance(item.get("checkpoint_time"), datetime) else shanghai_now(),
                    defer_vectorization=bool(item.get("defer_vectorization")),
                )
                logger.info(
                    "Deferred event generation completed for snapshot=%d event_id=%s",
                    snapshot_id,
                    event_id,
                )
            except Exception:
                logger.exception(
                    "Deferred event generation failed for snapshot=%s",
                    snapshot_id,
                )

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

            event_prompt_template = await self.prompt_manager.get_prompt(KEY_PROMPT_EVENT_ANCHOR)
            event_prompt = event_prompt_template.format(
                current_snapshot=new_content,
                environment=environment_text,
                memory_context=memory_text,
                system_layers=await self.prompt_manager.get_system_layers_text(),
            )
            event_response = await self.snapshot_llm.chat(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": event_prompt},
                ]
            )
            payload = self._parse_event_payload_for_update(
                event_response,
                source="generated",
                date_override=checkpoint_time.date().isoformat(),
            )
            event_id = item.get("event_id")
            event_id_int = int(event_id or 0)
            if payload is None:
                if event_id_int > 0:
                    if callable(remove_vector):
                        try:
                            await remove_vector(f"event_{event_id_int}")
                        except Exception:
                            logger.warning("Event vector cleanup skipped for #%d", event_id_int)
                    await self.db.delete_event(event_id_int)
            elif event_id_int > 0 and await self.db.get_event_by_id(event_id_int):
                if callable(remove_vector):
                    try:
                        await remove_vector(f"event_{event_id_int}")
                    except Exception:
                        logger.warning("Event vector cleanup skipped for #%d", event_id_int)
                await self.db.update_event(event_id_int, **payload)
            else:
                new_event = EventAnchor(
                    date=str(payload["date"]),
                    title=str(payload["title"]),
                    description=str(payload["description"]),
                    source=str(payload["source"]),
                    created_at=format_utc_instant_z(shanghai_time_to_utc_naive(shanghai_now())),
                    trigger_keywords=str(payload["trigger_keywords"]),
                    categories=str(payload["categories"]),
                )
                await self.db.insert_event(new_event)

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
            value = 3
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
