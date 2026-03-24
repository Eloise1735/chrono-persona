from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from server.config import AppConfig
from server.database import Database
from server.environment import EnvironmentGenerator
from server.llm_client import LLMClient
from server.memory_store import MemoryStore
from server.models import StateSnapshot, EventAnchor, KeyRecord
from server.event_taxonomy import classify_event, make_event_title
from server.prompts import (
    PromptManager,
    KEY_PROMPT_SNAPSHOT_GENERATION,
    KEY_PROMPT_EVENT_ANCHOR,
    KEY_PROMPT_REFLECT_SNAPSHOT,
    KEY_PROMPT_REFLECT_EVENT,
    KEY_PROMPT_CONVERSATION_SUMMARY,
    KEY_PROMPT_PERIODIC_REVIEW,
    KEY_MIN_TIME_UNIT_HOURS,
    KEY_INJECT_HOT_EVENTS_LIMIT,
    KEY_L1_CHARACTER_BACKGROUND,
    KEY_L1_USER_BACKGROUND,
    KEY_L2_CHARACTER_PERSONALITY,
    KEY_L2_RELATIONSHIP_DYNAMICS,
)
from server.automation_engine import AutomationEngine

logger = logging.getLogger(__name__)


class StateMachine:
    def __init__(
        self,
        config: AppConfig,
        db: Database,
        llm: LLMClient,
        env_gen: EnvironmentGenerator,
        memory: MemoryStore,
        prompt_manager: PromptManager,
        automation_engine: AutomationEngine | None = None,
    ):
        self.config = config
        self.db = db
        self.llm = llm
        self.env_gen = env_gen
        self.memory = memory
        self.prompt_manager = prompt_manager
        self.automation_engine = automation_engine
        self.max_snapshots = config.memory_store.max_snapshots

    async def get_current_state(
        self, current_time: str, last_interaction_time: str
    ) -> str:
        self.llm.begin_usage_tracking()
        now = self._parse_iso_datetime(current_time)
        last = self._parse_iso_datetime(last_interaction_time)
        interval = now - last
        min_unit_hours = await self._get_min_time_unit_hours()
        min_time_unit = timedelta(hours=min_unit_hours)

        latest_snapshot = await self.db.get_latest_snapshot()
        previous_content = latest_snapshot.content if latest_snapshot else "（尚无历史状态记录）"

        n_checkpoints = max(1, int(interval.total_seconds() / min_time_unit.total_seconds()))
        n_checkpoints = min(n_checkpoints, 30)  # cap to avoid runaway generation

        time_step = interval / n_checkpoints
        previous_env = None
        current_content = previous_content

        start_date = last.strftime("%Y-%m-%d")
        end_date = now.strftime("%Y-%m-%d")
        recent_events = await self.db.get_events_in_range(start_date, end_date)
        events_text = self._format_events(recent_events) if recent_events else "（无近期事件记录）"

        for i in range(n_checkpoints):
            checkpoint_time = last + time_step * (i + 1)

            env = await self.env_gen.generate(
                time_point=checkpoint_time,
                previous_env=previous_env,
                context={"latest_snapshot": current_content},
            )

            memory_results = await self.memory.search(
                env.get("summary", ""),
                top_k=3,
            )
            memory_text = self._format_memories(memory_results) if memory_results else "（无相关历史记忆）"

            system_prompt = await self.prompt_manager.get_system_prompt()
            prompt_template = await self.prompt_manager.get_prompt(
                KEY_PROMPT_SNAPSHOT_GENERATION
            )
            prompt = prompt_template.format(
                environment=env.get("summary", ""),
                previous_snapshot=current_content,
                recent_events=events_text,
                memory_context=memory_text,
            )

            current_content = await self.llm.chat([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ])

            snap = StateSnapshot(
                created_at=checkpoint_time.isoformat(),
                type="daily" if i < n_checkpoints - 1 else "accumulated",
                content=current_content,
                environment=json.dumps(env, ensure_ascii=False),
                referenced_events=json.dumps([e.id for e in recent_events]),
            )
            await self.db.insert_snapshot(snap)

            await self._generate_event_anchor(
                current_content, env, memory_text, checkpoint_time
            )

            await self._enforce_snapshot_limit()
            previous_env = env

        report = await self._run_automation(trigger="get_current_state")
        llm_usage = self.llm.end_usage_tracking()
        if isinstance(report, dict):
            report["llm_usage"] = llm_usage
            persist_method = getattr(self.automation_engine, "persist_run_report", None)
            if callable(persist_method):
                await persist_method(report)
        snapshot_with_report = self._append_automation_report(current_content, report)
        return await self._build_injectable_context(snapshot_with_report)

    async def reflect_on_conversation(self, conversation_summary: str) -> str:
        self.llm.begin_usage_tracking()
        latest_snapshot = await self.db.get_latest_snapshot()
        previous_content = latest_snapshot.content if latest_snapshot else "（尚无历史状态记录）"

        memory_results = await self.memory.search(conversation_summary, top_k=3)
        memory_text = self._format_memories(memory_results) if memory_results else "（无相关历史记忆）"

        system_prompt = await self.prompt_manager.get_system_prompt()
        reflect_template = await self.prompt_manager.get_prompt(
            KEY_PROMPT_REFLECT_SNAPSHOT
        )
        reflect_prompt = reflect_template.format(
            previous_snapshot=previous_content,
            conversation_summary=conversation_summary,
            memory_context=memory_text,
        )

        new_content = await self.llm.chat([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": reflect_prompt},
        ])

        snap = StateSnapshot(
            created_at=datetime.utcnow().isoformat(),
            type="conversation_end",
            content=new_content,
            environment="{}",
            referenced_events="[]",
        )
        await self.db.insert_snapshot(snap)

        event_template = await self.prompt_manager.get_prompt(KEY_PROMPT_REFLECT_EVENT)
        event_prompt = event_template.format(
            current_snapshot=new_content,
            conversation_summary=conversation_summary,
            memory_context=memory_text,
            system_layers=await self.prompt_manager.get_system_layers_text(),
        )

        event_response = await self.llm.chat([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": event_prompt},
        ])

        await self._parse_and_save_event(event_response, source="conversation")
        await self._enforce_snapshot_limit()
        report = await self._run_automation(trigger="reflect_on_conversation")
        llm_usage = self.llm.end_usage_tracking()
        if isinstance(report, dict):
            report["llm_usage"] = llm_usage
            persist_method = getattr(self.automation_engine, "persist_run_report", None)
            if callable(persist_method):
                await persist_method(report)
        return self._append_automation_report(new_content, report)

    async def summarize_conversation(self, conversation_text: str) -> str:
        latest_snapshot = await self.db.get_latest_snapshot()
        previous_content = latest_snapshot.content if latest_snapshot else "（尚无历史状态记录）"
        memory_results = await self.memory.search(conversation_text, top_k=3)
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
        hot_limit = await self._get_inject_hot_events_limit()
        recent_events_text = await self._build_recent_events_text(limit=hot_limit)
        return (
            "【L1 稳定底层】\n"
            f"角色背景：{l1_char}\n\n"
            f"用户背景：{l1_user}\n\n"
            "【L2 动态层】\n"
            f"角色人格：{l2_char}\n\n"
            f"关系模式：{l2_rel}\n\n"
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

    async def recall_key_records(
        self,
        query: str,
        top_k: int = 5,
        record_type: str | None = None,
        include_archived: bool = False,
    ) -> list[dict]:
        rows = await self.db.search_key_records(
            query=query,
            top_k=top_k,
            record_type=record_type,
            include_archived=include_archived,
        )
        return [r.model_dump() for r in rows]

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

    # ── Internal helpers ──

    async def _generate_event_anchor(
        self,
        snapshot_content: str,
        env: dict,
        memory_text: str,
        time_point: datetime,
    ):
        system_prompt = await self.prompt_manager.get_system_prompt()
        prompt_template = await self.prompt_manager.get_prompt(KEY_PROMPT_EVENT_ANCHOR)
        prompt = prompt_template.format(
            current_snapshot=snapshot_content,
            environment=(
                "【客观事件来源（优先据此抽取事实）】\n"
                f"{env.get('summary', '')}"
            ),
            memory_context=memory_text,
            system_layers=await self.prompt_manager.get_system_layers_text(),
        )

        response = await self.llm.chat([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ])

        await self._parse_and_save_event(
            response, source="generated", date_override=time_point.strftime("%Y-%m-%d")
        )

    async def _parse_and_save_event(
        self, response: str, source: str, date_override: str | None = None
    ):
        if "无需记录" in response:
            logger.info("LLM determined no event anchor needed.")
            return

        description = ""
        title = ""
        keywords: list[str] = []
        categories: list[str] = []

        objective_text = ""
        impression_text = ""
        objective_match = re.search(
            r"客观记录[：:]\s*(.+?)(?:\n(?:主观印象|关键词|分类)[：:]|$)",
            response,
            re.DOTALL,
        )
        if objective_match:
            objective_text = objective_match.group(1).strip()

        impression_match = re.search(
            r"主观印象[：:]\s*(.+?)(?:\n(?:关键词|分类)[：:]|$)",
            response,
            re.DOTALL,
        )
        if impression_match:
            impression_text = impression_match.group(1).strip()

        # Backward compatible with old prompt format using "事件描述".
        if not objective_text and not impression_text:
            desc_match = re.search(r"事件描述[：:]\s*(.+?)(?:\n|关键词|分类|$)", response, re.DOTALL)
            if desc_match:
                description = desc_match.group(1).strip()
            else:
                lines = [l.strip() for l in response.strip().split("\n") if l.strip()]
                description = lines[0] if lines else response.strip()
        elif objective_text and impression_text:
            description = f"客观记录：{objective_text}\n主观印象：{impression_text}"
        elif objective_text:
            description = f"客观记录：{objective_text}"
        else:
            description = f"主观印象：{impression_text}"

        title_match = re.search(
            r"标题[：:]\s*(.+?)(?:\n|客观记录|主观印象|事件描述|关键词|分类|$)",
            response,
            re.DOTALL,
        )
        if title_match:
            title = title_match.group(1).strip()

        kw_match = re.search(r"关键词[：:]\s*\[?(.+?)\]?\s*$", response, re.MULTILINE)
        if kw_match:
            raw = kw_match.group(1)
            keywords = [k.strip().strip("\"'") for k in re.split(r"[,，、]", raw) if k.strip()]

        cat_match = re.search(r"分类[：:]\s*\[?(.+?)\]?\s*$", response, re.MULTILINE)
        if cat_match:
            raw = cat_match.group(1)
            categories = [c.strip().strip("\"'") for c in re.split(r"[,，、]", raw) if c.strip()]

        if not description:
            return
        if not title:
            title = make_event_title(objective_text or description, keywords, categories)
        if not categories:
            categories = classify_event(description, keywords)

        event = EventAnchor(
            date=date_override or datetime.utcnow().strftime("%Y-%m-%d"),
            title=title,
            description=description,
            source=source,
            created_at=datetime.utcnow().isoformat(),
            trigger_keywords=json.dumps(keywords, ensure_ascii=False),
            categories=json.dumps(categories, ensure_ascii=False),
        )
        event_id = await self.db.insert_event(event)
        logger.info("Saved event anchor #%d: %s", event_id, description[:50])
        upsert_event_vector = getattr(self.memory, "upsert_event_vector", None)
        if callable(upsert_event_vector):
            try:
                await upsert_event_vector(int(event_id))
            except Exception as exc:
                logger.warning("Event vector upsert skipped for #%d: %s", event_id, exc)

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
        if self.automation_engine is None:
            await self._sync_vector_candidates()
            return None
        try:
            return await self.automation_engine.run(trigger)
        except Exception as exc:
            logger.warning("Automation run failed: %s", exc)
            return {"trigger": trigger, "errors": [str(exc)]}

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
            if evolution.get("applied"):
                lines.append(
                    f"- 人格演化：已自动执行（新事件 {int(status.get('event_count', 0))} 条，"
                    f"归档 {int(evolution.get('archived_count', 0))} 条）。"
                )
            else:
                lines.append(
                    f"- 人格演化：本次未触发（新事件 {int(status.get('event_count', 0))}/阈值 {int(status.get('threshold', 0))}）。"
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
        parts = []
        for m in memories:
            label = "事件" if m.source_type == "event" else "快照"
            parts.append(f"- [{label}] {m.text[:200]}")
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

    async def _get_min_time_unit_hours(self) -> int:
        raw = await self.prompt_manager.get_config_value(KEY_MIN_TIME_UNIT_HOURS)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = int(self.config.environment.min_time_unit_hours)
        return max(1, value)

    async def _get_inject_hot_events_limit(self) -> int:
        raw = await self.prompt_manager.get_config_value(KEY_INJECT_HOT_EVENTS_LIMIT)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 3
        return max(1, min(value, 50))

    @staticmethod
    def _parse_iso_datetime(value: str) -> datetime:
        text = (value or "").strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        # Normalize to naive UTC to avoid aware/naive subtraction errors.
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
