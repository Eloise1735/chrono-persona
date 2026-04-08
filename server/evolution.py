from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from server.database import Database
from server.llm_client import LLMClient, LLMTimeoutError
from server.prompts import (
    PromptManager,
    KEY_PROMPT_EVENT_SCORING,
    KEY_PROMPT_EVOLUTION_SUMMARY,
    EVOLUTION_SUMMARY_PROMPT,
    KEY_L1_CHARACTER_BACKGROUND,
    KEY_L2_CHARACTER_PERSONALITY,
    KEY_L2_LIFE_STATUS,
    KEY_L2_RELATIONSHIP_DYNAMICS,
    KEY_LAST_EVOLUTION_TIME,
    KEY_EVOLUTION_EVENT_THRESHOLD,
    KEY_ARCHIVE_IMPORTANCE_THRESHOLD,
    KEY_ARCHIVE_DEPTH_THRESHOLD,
    KEY_PENDING_EVOLUTION_PREVIEW_JSON,
    KEY_PENDING_EVOLUTION_PREVIEW_UPDATED_AT,
    KEY_EVOLUTION_PROMPT_IMPORTANCE_MIN,
    KEY_EVOLUTION_PROMPT_DEPTH_MIN,
    KEY_EVOLUTION_PROMPT_DROP_IMPORTANCE_BELOW,
    KEY_EVOLUTION_PROMPT_DROP_DEPTH_BELOW,
    KEY_EVOLUTION_PROMPT_MAX_EVENTS,
)

logger = logging.getLogger(__name__)


class EvolutionEngine:
    EVENT_SCORING_BATCH_SIZE = 8
    EVENT_SCORING_MAX_TOKENS = 6144
    EVOLUTION_SUMMARY_MAX_TOKENS = 9000
    EVENT_SCORING_TIMEOUT_SEC = 600.0
    EVOLUTION_SUMMARY_TIMEOUT_SEC = 600.0

    def __init__(
        self,
        db: Database,
        llm: LLMClient,
        prompt_manager: PromptManager,
        snapshot_llm: LLMClient | None = None,
    ):
        self.db = db
        self.llm = llm
        self.snapshot_llm = snapshot_llm or llm
        self.prompt_manager = prompt_manager

    async def check_status(self) -> dict:
        last_time = await self._get_setting(KEY_LAST_EVOLUTION_TIME, "")
        threshold = await self._get_int_setting(
            KEY_EVOLUTION_EVENT_THRESHOLD,
            10,
            minimum=1,
        )
        since = last_time or "1970-01-01T00:00:00"
        event_count = await self.db.count_events_since(since, include_archived=False)
        return {
            "should_evolve": event_count >= threshold,
            "event_count": event_count,
            "threshold": threshold,
            "last_time": last_time,
        }

    async def preview(
        self,
        *,
        store_pending: bool = False,
        source: str = "manual",
    ) -> dict:
        status = await self.check_status()
        since = status["last_time"] or "1970-01-01T00:00:00"
        events = await self.db.get_events_since(since, limit=200, include_archived=False)
        if not events:
            _selected_events, filter_meta = await self._select_events_for_evolution([])
            current_character, current_relationship, current_life_status = (
                await self._get_current_l2_layers()
            )
            result = {
                **status,
                "scored_events": [],
                "evolution_candidates": [],
                "evolution_prompt_event_count": 0,
                "evolution_prompt_event_ids": [],
                "evolution_filter_meta": filter_meta,
                "current_character_personality": current_character,
                "current_relationship_dynamics": current_relationship,
                "current_life_status": current_life_status,
                "new_character_personality": current_character,
                "new_relationship_dynamics": current_relationship,
                "new_life_status": current_life_status,
                "change_summary": "没有新的活跃事件，无需演化。",
            }
            if store_pending:
                await self.clear_pending_preview()
            return result

        scored_events = await self._score_events(events)
        await self._persist_event_scores(scored_events)
        selected_events, filter_meta = await self._select_events_for_evolution(scored_events)
        current_character, current_relationship, current_life_status = (
            await self._get_current_l2_layers()
        )
        if not selected_events:
            result = {
                **status,
                "scored_events": scored_events,
                "evolution_candidates": [],
                "evolution_prompt_event_count": 0,
                "evolution_prompt_event_ids": [],
                "evolution_filter_meta": filter_meta,
                "current_character_personality": current_character,
                "current_relationship_dynamics": current_relationship,
                "current_life_status": current_life_status,
                "new_character_personality": current_character,
                "new_relationship_dynamics": current_relationship,
                "new_life_status": current_life_status,
                "change_summary": "已完成事件评分，但暂无达到演化注入阈值的候选事件，L2 保持不变。",
            }
            if store_pending:
                await self.save_pending_preview(result, source=source)
            return result
        (
            _current_character,
            _current_relationship,
            _current_life_status,
            new_character,
            new_relationship,
            new_life_status,
            summary,
        ) = await self._generate_updates(
            selected_events
        )
        result = {
            **status,
            "scored_events": scored_events,
            "evolution_candidates": selected_events,
            "evolution_prompt_event_count": len(selected_events),
            "evolution_prompt_event_ids": [int(item.get("id") or 0) for item in selected_events],
            "evolution_filter_meta": filter_meta,
            "current_character_personality": current_character,
            "current_relationship_dynamics": current_relationship,
            "current_life_status": current_life_status,
            "new_character_personality": new_character,
            "new_relationship_dynamics": new_relationship,
            "new_life_status": new_life_status,
            "change_summary": summary,
        }
        if store_pending:
            await self.save_pending_preview(result, source=source)
        return result

    async def regenerate_preview_from_scored(
        self,
        *,
        store_pending: bool = False,
        source: str = "manual_regenerate",
    ) -> dict:
        status = await self.check_status()
        events = await self.db.get_events_for_archive_recalc()
        scored_events = [
            self._event_anchor_to_scored_item(event)
            for event in events
            if event.importance_score is not None or event.impression_depth is not None
        ]
        scored_events.sort(
            key=lambda x: (x["importance_score"], x["impression_depth"]), reverse=True
        )
        return await self._build_preview_from_scored_events(
            status=status,
            scored_events=scored_events,
            store_pending=store_pending,
            source=source,
            no_events_summary="全库中没有可复用的已评分事件，无法基于现有分数重建演化预览。",
            no_candidates_summary="已基于全库已评分事件重建预览，但暂无达到演化注入阈值的候选事件，L2 保持不变。",
        )

    async def apply(self, preview_data: dict) -> dict:
        scored_events = preview_data.get("scored_events", [])
        new_character = preview_data.get("new_character_personality", "").strip()
        new_relationship = preview_data.get("new_relationship_dynamics", "").strip()
        new_life_status = preview_data.get("new_life_status", "").strip()

        if new_character:
            await self.prompt_manager.set_layer_content(
                KEY_L2_CHARACTER_PERSONALITY, new_character
            )
        if new_relationship:
            await self.prompt_manager.set_layer_content(
                KEY_L2_RELATIONSHIP_DYNAMICS, new_relationship
            )
        if new_life_status:
            await self.prompt_manager.set_layer_content(KEY_L2_LIFE_STATUS, new_life_status)

        archive_threshold = await self._get_float_setting(
            KEY_ARCHIVE_IMPORTANCE_THRESHOLD,
            3.0,
            minimum=0.0,
        )
        archive_depth_threshold = await self._get_float_setting(
            KEY_ARCHIVE_DEPTH_THRESHOLD,
            5.0,
            minimum=0.0,
        )
        archived_ids: list[int] = []
        for item in scored_events:
            event_id = int(item.get("id", 0))
            importance = float(item.get("importance_score", 0))
            impression = float(item.get("impression_depth", 0))
            if event_id <= 0:
                continue
            fields = {
                "importance_score": importance,
                "impression_depth": impression,
            }
            if importance < archive_threshold and impression < archive_depth_threshold:
                fields["archived"] = 1
                archived_ids.append(event_id)
            await self.db.update_event(event_id, **fields)

        now = datetime.utcnow().isoformat()
        await self.db.set_setting(
            KEY_LAST_EVOLUTION_TIME,
            now,
            category="config",
            description="上次人格演化时间",
        )
        await self.clear_pending_preview()
        return {
            "updated_keys": [
                KEY_L2_CHARACTER_PERSONALITY,
                KEY_L2_RELATIONSHIP_DYNAMICS,
                KEY_L2_LIFE_STATUS,
                KEY_LAST_EVOLUTION_TIME,
            ],
            "archived_count": len(archived_ids),
            "archived_ids": archived_ids,
            "applied_at": now,
        }

    async def recalculate_archive_status(
        self,
        start_id: int | None = None,
        end_id: int | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict:
        archive_threshold = await self._get_float_setting(
            KEY_ARCHIVE_IMPORTANCE_THRESHOLD,
            3.0,
            minimum=0.0,
        )
        archive_depth_threshold = await self._get_float_setting(
            KEY_ARCHIVE_DEPTH_THRESHOLD,
            5.0,
            minimum=0.0,
        )
        events = await self.db.get_events_for_archive_recalc(
            start_id=start_id,
            end_id=end_id,
            start_date=start_date,
            end_date=end_date,
        )

        to_archive = 0
        to_unarchive = 0
        skipped_unscored = 0
        updates: list[tuple[int, int]] = []

        for event in events:
            old_archived = int(event.archived or 0)
            if event.importance_score is None:
                skipped_unscored += 1
                continue
            imp = float(event.importance_score)
            dep = float(event.impression_depth or 0)
            new_archived = 1 if (imp < archive_threshold and dep < archive_depth_threshold) else 0
            if new_archived == old_archived:
                continue
            if new_archived == 1:
                to_archive += 1
            else:
                to_unarchive += 1
            updates.append((new_archived, int(event.id or 0)))

        await self.db.update_event_archived_flags(updates)

        return {
            "threshold": archive_threshold,
            "depth_threshold": archive_depth_threshold,
            "scanned_count": len(events),
            "changed_count": len(updates),
            "archived_count": to_archive,
            "unarchived_count": to_unarchive,
            "skipped_unscored_count": skipped_unscored,
            "scope": {
                "start_id": start_id,
                "end_id": end_id,
                "start_date": start_date,
                "end_date": end_date,
            },
        }

    async def rescore_events(
        self,
        start_id: int | None = None,
        end_id: int | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        *,
        scored_only: bool = True,
    ) -> dict:
        events = await self.db.get_events_for_archive_recalc(
            start_id=start_id,
            end_id=end_id,
            start_date=start_date,
            end_date=end_date,
        )
        scanned_count = len(events)
        if scored_only:
            events = [
                e
                for e in events
                if e.importance_score is not None or e.impression_depth is not None
            ]
        skipped_unscored_count = scanned_count - len(events)
        if not events:
            return {
                "scanned_count": scanned_count,
                "rescored_count": 0,
                "skipped_unscored_count": skipped_unscored_count,
                "scored_only": scored_only,
                "top_events": [],
                "selected_count": 0,
                "selected_ids": [],
                "filter_meta": {},
                "archive_recalc": await self.recalculate_archive_status(
                    start_id=start_id,
                    end_id=end_id,
                    start_date=start_date,
                    end_date=end_date,
                ),
                "scope": {
                    "start_id": start_id,
                    "end_id": end_id,
                    "start_date": start_date,
                    "end_date": end_date,
                },
            }

        rescored_events = await self._score_events(events)
        await self._persist_event_scores(rescored_events)
        selected_events, filter_meta = await self._select_events_for_evolution(rescored_events)
        archive_recalc = await self.recalculate_archive_status(
            start_id=start_id,
            end_id=end_id,
            start_date=start_date,
            end_date=end_date,
        )
        return {
            "scanned_count": scanned_count,
            "rescored_count": len(rescored_events),
            "skipped_unscored_count": skipped_unscored_count,
            "scored_only": scored_only,
            "top_events": rescored_events[:10],
            "selected_count": len(selected_events),
            "selected_ids": [int(item.get("id") or 0) for item in selected_events],
            "filter_meta": filter_meta,
            "archive_recalc": archive_recalc,
            "scope": {
                "start_id": start_id,
                "end_id": end_id,
                "start_date": start_date,
                "end_date": end_date,
            },
        }

    async def _build_preview_from_scored_events(
        self,
        *,
        status: dict,
        scored_events: list[dict],
        store_pending: bool,
        source: str,
        no_events_summary: str,
        no_candidates_summary: str,
    ) -> dict:
        current_character, current_relationship, current_life_status = (
            await self._get_current_l2_layers()
        )
        if not scored_events:
            _selected_events, filter_meta = await self._select_events_for_evolution([])
            result = {
                **status,
                "scored_events": [],
                "evolution_candidates": [],
                "evolution_prompt_event_count": 0,
                "evolution_prompt_event_ids": [],
                "evolution_filter_meta": filter_meta,
                "current_character_personality": current_character,
                "current_relationship_dynamics": current_relationship,
                "current_life_status": current_life_status,
                "new_character_personality": current_character,
                "new_relationship_dynamics": current_relationship,
                "new_life_status": current_life_status,
                "change_summary": no_events_summary,
            }
            if store_pending:
                await self.clear_pending_preview()
            return result

        selected_events, filter_meta = await self._select_events_for_evolution(scored_events)
        if not selected_events:
            result = {
                **status,
                "scored_events": scored_events,
                "evolution_candidates": [],
                "evolution_prompt_event_count": 0,
                "evolution_prompt_event_ids": [],
                "evolution_filter_meta": filter_meta,
                "current_character_personality": current_character,
                "current_relationship_dynamics": current_relationship,
                "current_life_status": current_life_status,
                "new_character_personality": current_character,
                "new_relationship_dynamics": current_relationship,
                "new_life_status": current_life_status,
                "change_summary": no_candidates_summary,
            }
            if store_pending:
                await self.save_pending_preview(result, source=source)
            return result

        (
            _current_character,
            _current_relationship,
            _current_life_status,
            new_character,
            new_relationship,
            new_life_status,
            summary,
        ) = await self._generate_updates(selected_events)
        result = {
            **status,
            "scored_events": scored_events,
            "evolution_candidates": selected_events,
            "evolution_prompt_event_count": len(selected_events),
            "evolution_prompt_event_ids": [int(item.get("id") or 0) for item in selected_events],
            "evolution_filter_meta": filter_meta,
            "current_character_personality": current_character,
            "current_relationship_dynamics": current_relationship,
            "current_life_status": current_life_status,
            "new_character_personality": new_character,
            "new_relationship_dynamics": new_relationship,
            "new_life_status": new_life_status,
            "change_summary": summary,
        }
        if store_pending:
            await self.save_pending_preview(result, source=source)
        return result

    async def _score_events(self, events: list) -> list[dict]:
        template = await self.prompt_manager.get_prompt(KEY_PROMPT_EVENT_SCORING)
        l1_bg = await self.prompt_manager.get_layer_content(KEY_L1_CHARACTER_BACKGROUND)
        l2_char = await self.prompt_manager.get_layer_content(KEY_L2_CHARACTER_PERSONALITY)
        l2_life = await self.prompt_manager.get_layer_content(KEY_L2_LIFE_STATUS)
        l2_rel = await self.prompt_manager.get_layer_content(KEY_L2_RELATIONSHIP_DYNAMICS)
        system_prompt = await self.prompt_manager.get_system_prompt()

        scored: list[dict] = []
        for batch_index, batch in enumerate(
            self._chunk_items(events, self.EVENT_SCORING_BATCH_SIZE),
            start=1,
        ):
            logger.info(
                "Scoring evolution events batch %s size=%s",
                batch_index,
                len(batch),
            )
            scored.extend(
                await self._score_events_batch(
                    batch,
                    template=template,
                    system_prompt=system_prompt,
                    l1_bg=l1_bg,
                    l2_char=l2_char,
                    l2_life=l2_life,
                    l2_rel=l2_rel,
                )
            )
        scored.sort(
            key=lambda x: (x["importance_score"], x["impression_depth"]), reverse=True
        )
        return scored

    async def _select_events_for_evolution(
        self, scored_events: list[dict]
    ) -> tuple[list[dict], dict]:
        importance_min = await self._get_float_setting(
            KEY_EVOLUTION_PROMPT_IMPORTANCE_MIN,
            6.0,
            minimum=0.0,
        )
        depth_min = await self._get_float_setting(
            KEY_EVOLUTION_PROMPT_DEPTH_MIN,
            7.0,
            minimum=0.0,
        )
        drop_importance_below = await self._get_float_setting(
            KEY_EVOLUTION_PROMPT_DROP_IMPORTANCE_BELOW,
            4.0,
            minimum=0.0,
        )
        drop_depth_below = await self._get_float_setting(
            KEY_EVOLUTION_PROMPT_DROP_DEPTH_BELOW,
            5.0,
            minimum=0.0,
        )
        max_events = await self._get_int_setting(
            KEY_EVOLUTION_PROMPT_MAX_EVENTS,
            12,
            minimum=1,
        )
        dropped_low_ids: list[int] = []
        selected: list[dict] = []
        for item in scored_events:
            event_id = int(item.get("id") or 0)
            importance = float(item.get("importance_score") or 0)
            depth = float(item.get("impression_depth") or 0)
            if importance < drop_importance_below and depth < drop_depth_below:
                dropped_low_ids.append(event_id)
                continue
            if importance >= importance_min or depth >= depth_min:
                selected.append(item)
        selected = selected[:max_events]
        meta = {
            "importance_min": importance_min,
            "depth_min": depth_min,
            "drop_importance_below": drop_importance_below,
            "drop_depth_below": drop_depth_below,
            "max_events": max_events,
            "scored_count": len(scored_events),
            "dropped_low_count": len(dropped_low_ids),
            "dropped_low_ids": dropped_low_ids,
            "selected_count": len(selected),
            "selected_ids": [int(item.get("id") or 0) for item in selected],
        }
        logger.info(
            "Evolution prompt filter selected=%s/%s dropped_low=%s max_events=%s thresholds=(importance>=%.1f or depth>=%.1f)",
            meta["selected_count"],
            meta["scored_count"],
            meta["dropped_low_count"],
            max_events,
            importance_min,
            depth_min,
        )
        return selected, meta

    async def _persist_event_scores(self, scored_events: list[dict]) -> None:
        for item in scored_events:
            event_id = int(item.get("id") or 0)
            if event_id <= 0:
                continue
            await self.db.update_event(
                event_id,
                importance_score=float(item.get("importance_score") or 0),
                impression_depth=float(item.get("impression_depth") or 0),
            )

    async def _score_events_batch(
        self,
        events: list,
        *,
        template: str,
        system_prompt: str,
        l1_bg: str,
        l2_char: str,
        l2_life: str,
        l2_rel: str,
    ) -> list[dict]:
        event_blocks = [self._format_event_for_scoring(e) for e in events]
        event_lines = "\n\n".join(event_blocks)
        prompt = self._safe_format_template(
            template,
            L1_character_background=l1_bg,
            L2_character_personality=l2_char,
            L2_life_status=l2_life,
            L2_relationship_dynamics=l2_rel,
            events=event_lines,
        )
        try:
            response = await self.snapshot_llm.chat(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=self.EVENT_SCORING_MAX_TOKENS,
                timeout_sec_override=self.EVENT_SCORING_TIMEOUT_SEC,
            )
        except LLMTimeoutError:
            if len(events) > 1:
                mid = max(1, len(events) // 2)
                logger.warning(
                    "Scoring batch timed out (size=%s); splitting into %s + %s",
                    len(events),
                    mid,
                    len(events) - mid,
                )
                left = await self._score_events_batch(
                    events[:mid],
                    template=template,
                    system_prompt=system_prompt,
                    l1_bg=l1_bg,
                    l2_char=l2_char,
                    l2_life=l2_life,
                    l2_rel=l2_rel,
                )
                right = await self._score_events_batch(
                    events[mid:],
                    template=template,
                    system_prompt=system_prompt,
                    l1_bg=l1_bg,
                    l2_char=l2_char,
                    l2_life=l2_life,
                    l2_rel=l2_rel,
                )
                return left + right
            raise

        scored, matched_count = self._parse_scored_response(events, response)
        if matched_count < len(events) and len(events) > 1:
            mid = max(1, len(events) // 2)
            logger.warning(
                "Scoring batch incomplete (matched=%s total=%s); splitting into %s + %s",
                matched_count,
                len(events),
                mid,
                len(events) - mid,
            )
            left = await self._score_events_batch(
                events[:mid],
                template=template,
                system_prompt=system_prompt,
                l1_bg=l1_bg,
                l2_char=l2_char,
                l2_life=l2_life,
                l2_rel=l2_rel,
            )
            right = await self._score_events_batch(
                events[mid:],
                template=template,
                system_prompt=system_prompt,
                l1_bg=l1_bg,
                l2_char=l2_char,
                l2_life=l2_life,
                l2_rel=l2_rel,
            )
            return left + right
        return scored

    def _parse_scored_response(self, events: list, response: str) -> tuple[list[dict], int]:
        scored: list[dict] = []
        matched_count = 0
        for e in events:
            eid = int(e.id or 0)
            block = self._find_event_block(response, eid)
            if block:
                matched_count += 1
            importance = self._extract_score(block, "重要性")
            depth = self._extract_score(block, "印象深度")
            reason = self._extract_reason(block)
            rich_block = self._compose_scoring_block(
                event_id=eid,
                title=getattr(e, "title", "") or "",
                description=getattr(e, "description", "") or "",
                trigger_keywords=getattr(e, "trigger_keywords", "[]") or "[]",
                categories=getattr(e, "categories", "[]") or "[]",
                importance=importance,
                depth=depth,
                reason=reason,
            )
            scored.append(
                {
                    "id": eid,
                    "date": e.date,
                    "title": getattr(e, "title", "") or "",
                    "description": e.description,
                    "importance_score": importance,
                    "impression_depth": depth,
                    "reason": reason,
                    "trigger_keywords": getattr(e, "trigger_keywords", "[]") or "[]",
                    "categories": getattr(e, "categories", "[]") or "[]",
                    "rich_block": rich_block,
                }
            )
        return scored, matched_count

    async def _generate_updates(
        self, scored_events: list[dict]
    ) -> tuple[str, str, str, str, str, str, str]:
        character_bg = await self.prompt_manager.get_layer_content(
            KEY_L1_CHARACTER_BACKGROUND
        )
        character = await self.prompt_manager.get_layer_content(
            KEY_L2_CHARACTER_PERSONALITY
        )
        relationship = await self.prompt_manager.get_layer_content(
            KEY_L2_RELATIONSHIP_DYNAMICS
        )
        life_status = await self.prompt_manager.get_layer_content(KEY_L2_LIFE_STATUS)

        importance_min = await self._get_float_setting(
            KEY_EVOLUTION_PROMPT_IMPORTANCE_MIN,
            5.0,
            minimum=0.0,
        )
        core_events: list[dict] = []
        context_events: list[dict] = []
        for e in scored_events[:30]:
            imp = float(e.get("importance_score") or 0)
            if imp >= importance_min:
                core_events.append(e)
            else:
                context_events.append(e)

        def _format_event_blocks(events: list[dict]) -> list[str]:
            blocks: list[str] = []
            for e in events:
                b = (e.get("rich_block") or "").strip()
                if b:
                    blocks.append(b)
                else:
                    eid = int(e.get("id") or 0)
                    imp = float(e.get("importance_score") or 0)
                    dep = float(e.get("impression_depth") or 0)
                    r = (e.get("reason") or "").strip() or "（无）"
                    desc = (e.get("description") or "").strip() or "（无）"
                    blocks.append(
                        f"事件ID: {eid}\n客观记录/描述: {desc}\n---\n"
                        f"重要性: {imp:.1f}\n印象深度: {dep:.1f}\n理由: {r}"
                    )
            return blocks

        parts: list[str] = []
        core_blocks = _format_event_blocks(core_events)
        if core_blocks:
            parts.append(
                "▶ 核心事件（认知变化幅度较高，可作为 L2 更新的直接依据）\n\n"
                + "\n\n".join(core_blocks)
            )
        context_blocks = _format_event_blocks(context_events)
        if context_blocks:
            parts.append(
                "▷ 背景事件（记忆质感较高但认知变化幅度较低，仅供质感参考）\n\n"
                + "\n\n".join(context_blocks)
            )
        scored_text = "\n\n".join(parts) if parts else "（无符合条件的事件）"
        template = await self._get_effective_evolution_summary_template()
        prompt = self._safe_format_template(
            template,
            character_background=character_bg,
            character_personality=character,
            relationship_dynamics=relationship,
            life_status=life_status,
            scored_events=scored_text,
        )
        system_prompt = await self.prompt_manager.get_system_prompt()
        response = await self.llm.chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            max_tokens=self.EVOLUTION_SUMMARY_MAX_TOKENS,
            timeout_sec_override=self.EVOLUTION_SUMMARY_TIMEOUT_SEC,
        )
        new_character = self._extract_section(response, "角色人格更新") or character
        new_relationship = self._extract_section(response, "关系模式更新") or relationship
        new_life_status = self._extract_section(response, "生活状态更新") or life_status
        summary = self._extract_section(response, "变更摘要") or "已完成人格层预览更新。"
        return (
            character.strip(),
            relationship.strip(),
            life_status.strip(),
            new_character.strip(),
            new_relationship.strip(),
            new_life_status.strip(),
            summary.strip(),
        )

    @staticmethod
    def _split_objective_subjective(description: str) -> tuple[str, str]:
        if not (description or "").strip():
            return "", ""
        d = description.strip()
        mo = re.search(
            r"客观记录\s*[：:]\s*(.+?)(?=\n\s*主观印象\s*[：:]|\Z)", d, re.DOTALL
        )
        mi = re.search(r"主观印象\s*[：:]\s*(.+)", d, re.DOTALL)
        if mo and mi:
            return mo.group(1).strip(), mi.group(1).strip()
        if mo:
            return mo.group(1).strip(), ""
        return d, ""

    @staticmethod
    def _format_json_list_field(raw: str) -> str:
        try:
            data = json.loads(raw or "[]")
            if isinstance(data, list) and data:
                return "、".join(str(x) for x in data)
        except (json.JSONDecodeError, TypeError):
            pass
        return "（无）"

    @staticmethod
    def _format_event_for_scoring(e) -> str:
        obj, subj = EvolutionEngine._split_objective_subjective(
            getattr(e, "description", "") or ""
        )
        if not obj and not subj and (getattr(e, "description", "") or "").strip():
            obj = (e.description or "").strip()
        title = (getattr(e, "title", "") or "").strip() or "（无标题）"
        if not subj.strip():
            subj = "（无）"
        kw = EvolutionEngine._format_json_list_field(
            getattr(e, "trigger_keywords", "[]") or "[]"
        )
        cat = EvolutionEngine._format_json_list_field(
            getattr(e, "categories", "[]") or "[]"
        )
        eid = int(e.id or 0)
        lines = [
            f"事件ID: {eid}",
            f"标题: {title}",
            f"客观记录: {obj or '（无）'}",
            f"主观印象: {subj}",
            f"关键词: {kw}",
            f"分类: {cat}",
        ]
        return "\n".join(lines)

    @staticmethod
    def _fallback_scoring_block(e, importance: float, depth: float, reason: str) -> str:
        return EvolutionEngine._compose_scoring_block(
            event_id=int(getattr(e, "id", 0) or 0),
            title=getattr(e, "title", "") or "",
            description=getattr(e, "description", "") or "",
            trigger_keywords=getattr(e, "trigger_keywords", "[]") or "[]",
            categories=getattr(e, "categories", "[]") or "[]",
            importance=importance,
            depth=depth,
            reason=reason,
        )

    @staticmethod
    def _event_anchor_to_scored_item(event) -> dict:
        event_id = int(getattr(event, "id", 0) or 0)
        importance = float(getattr(event, "importance_score", 0) or 0)
        depth = float(getattr(event, "impression_depth", 0) or 0)
        reason = "（沿用已落库评分重建预览，原评分理由未持久化）"
        return {
            "id": event_id,
            "date": getattr(event, "date", "") or "",
            "title": getattr(event, "title", "") or "",
            "description": getattr(event, "description", "") or "",
            "importance_score": importance,
            "impression_depth": depth,
            "reason": reason,
            "trigger_keywords": getattr(event, "trigger_keywords", "[]") or "[]",
            "categories": getattr(event, "categories", "[]") or "[]",
            "rich_block": EvolutionEngine._compose_scoring_block(
                event_id=event_id,
                title=getattr(event, "title", "") or "",
                description=getattr(event, "description", "") or "",
                trigger_keywords=getattr(event, "trigger_keywords", "[]") or "[]",
                categories=getattr(event, "categories", "[]") or "[]",
                importance=importance,
                depth=depth,
                reason=reason,
            ),
        }

    @staticmethod
    def _compose_scoring_block(
        *,
        event_id: int,
        title: str,
        description: str,
        trigger_keywords,
        categories,
        importance: float,
        depth: float,
        reason: str,
    ) -> str:
        obj, subj = EvolutionEngine._split_objective_subjective(description or "")
        if not obj and not subj and (description or "").strip():
            obj = (description or "").strip()
        title_text = (title or "").strip() or "（无标题）"
        subj_text = subj.strip() if subj.strip() else "（无）"
        kw = EvolutionEngine._format_json_list_field(
            trigger_keywords if isinstance(trigger_keywords, str) else json.dumps(trigger_keywords or [], ensure_ascii=False)
        )
        cat = EvolutionEngine._format_json_list_field(
            categories if isinstance(categories, str) else json.dumps(categories or [], ensure_ascii=False)
        )
        r = (reason or "").strip() or "（模型未返回该事件评分理由）"
        return (
            f"事件ID: {int(event_id or 0)}\n"
            f"标题: {title_text}\n"
            f"客观记录: {obj or '（无）'}\n"
            f"主观印象: {subj_text}\n"
            f"关键词: {kw}\n"
            f"分类: {cat}\n"
            "---\n"
            f"重要性: {importance:.1f}\n"
            f"印象深度: {depth:.1f}\n"
            f"理由: {r}"
        )

    @staticmethod
    def _find_event_block(text: str, event_id: int) -> str:
        pattern = (
            rf"(事件ID\s*[：:]\s*{event_id}.*?)(?=\n\s*事件ID\s*[：:]|\Z)"
        )
        m = re.search(pattern, text, re.DOTALL)
        return m.group(1) if m else ""

    @staticmethod
    def _extract_score(block: str, label: str) -> float:
        if not block:
            return 0.0
        m = re.search(rf"{label}\s*[：:]\s*([0-9]+(?:\.[0-9]+)?)", block)
        if not m:
            return 0.0
        score = float(m.group(1))
        return max(0.0, min(10.0, score))

    @staticmethod
    def _extract_reason(block: str) -> str:
        if not block:
            return ""
        m = re.search(
            r"理由\s*[：:]\s*(.+?)(?=\n\s*事件ID\s*[：:]|\Z)",
            block,
            re.DOTALL,
        )
        return m.group(1).strip() if m else ""

    @staticmethod
    def _extract_section(text: str, title: str) -> str:
        m = re.search(
            rf"{title}\s*[：:]\s*(.+?)(?=\n(?:角色人格更新|关系模式更新|生活状态更新|变更摘要)\s*[：:]|\Z)",
            text,
            re.DOTALL,
        )
        return m.group(1).strip() if m else ""

    async def _get_setting(self, key: str, default: str) -> str:
        row = await self.db.get_setting(key)
        if row and row.get("value") is not None:
            return str(row["value"])
        return default

    async def _get_effective_evolution_summary_template(self) -> str:
        template = await self.prompt_manager.get_prompt(KEY_PROMPT_EVOLUTION_SUMMARY)
        if self._is_valid_evolution_summary_template(template):
            return template
        logger.warning(
            "Evolution summary template looks invalid or mismatched; falling back to built-in default."
        )
        return EVOLUTION_SUMMARY_PROMPT

    async def _get_current_l2_layers(self) -> tuple[str, str, str]:
        current_character = await self.prompt_manager.get_layer_content(
            KEY_L2_CHARACTER_PERSONALITY
        )
        current_relationship = await self.prompt_manager.get_layer_content(
            KEY_L2_RELATIONSHIP_DYNAMICS
        )
        current_life_status = await self.prompt_manager.get_layer_content(
            KEY_L2_LIFE_STATUS
        )
        return (
            current_character,
            current_relationship,
            current_life_status,
        )

    async def get_pending_preview(self) -> dict | None:
        row = await self.db.get_setting(KEY_PENDING_EVOLUTION_PREVIEW_JSON)
        if not row or not str(row.get("value") or "").strip():
            return None
        try:
            data = json.loads(str(row.get("value") or ""))
        except Exception:
            logger.warning("Pending evolution preview JSON is invalid; clearing stale data.")
            await self.clear_pending_preview()
            return None
        if not isinstance(data, dict):
            await self.clear_pending_preview()
            return None
        updated_row = await self.db.get_setting(KEY_PENDING_EVOLUTION_PREVIEW_UPDATED_AT)
        updated_at = str((updated_row or {}).get("value") or "").strip()
        if updated_at and not data.get("pending_preview_generated_at"):
            data["pending_preview_generated_at"] = updated_at
        data = await self._hydrate_preview_event_blocks(data)
        return data

    async def save_pending_preview(self, preview_data: dict, *, source: str) -> dict:
        now = datetime.utcnow().isoformat()
        payload = dict(preview_data)
        payload["pending_preview_generated_at"] = now
        payload["pending_preview_source"] = source
        await self.db.set_setting(
            KEY_PENDING_EVOLUTION_PREVIEW_JSON,
            json.dumps(payload, ensure_ascii=False),
            category="automation",
            description="待确认的人格演化预览 JSON（后台自动生成，前端确认后应用）",
        )
        await self.db.set_setting(
            KEY_PENDING_EVOLUTION_PREVIEW_UPDATED_AT,
            now,
            category="automation",
            description="待确认的人格演化预览生成时间",
        )
        return payload

    async def clear_pending_preview(self) -> None:
        await self.db.set_setting(
            KEY_PENDING_EVOLUTION_PREVIEW_JSON,
            "",
            category="automation",
            description="待确认的人格演化预览 JSON（后台自动生成，前端确认后应用）",
        )
        await self.db.set_setting(
            KEY_PENDING_EVOLUTION_PREVIEW_UPDATED_AT,
            "",
            category="automation",
            description="待确认的人格演化预览生成时间",
        )

    async def _hydrate_preview_event_blocks(self, preview_data: dict) -> dict:
        scored = list(preview_data.get("scored_events") or [])
        candidates = list(preview_data.get("evolution_candidates") or [])
        ids = []
        seen_ids = set()
        for item in scored + candidates:
            event_id = int(item.get("id") or 0)
            if event_id > 0 and event_id not in seen_ids:
                seen_ids.add(event_id)
                ids.append(event_id)
        if not ids:
            return preview_data
        events = await self.db.get_events_by_ids(ids)
        event_map = {int(e.id or 0): e for e in events}

        def enrich(items: list[dict]) -> list[dict]:
            out: list[dict] = []
            for item in items:
                event_id = int(item.get("id") or 0)
                ev = event_map.get(event_id)
                title = item.get("title") or (getattr(ev, "title", "") if ev else "") or ""
                description = item.get("description") or (getattr(ev, "description", "") if ev else "") or ""
                trigger_keywords = item.get("trigger_keywords")
                if trigger_keywords in (None, "") and ev is not None:
                    trigger_keywords = getattr(ev, "trigger_keywords", "[]") or "[]"
                categories = item.get("categories")
                if categories in (None, "") and ev is not None:
                    categories = getattr(ev, "categories", "[]") or "[]"
                enriched = dict(item)
                enriched["title"] = title
                enriched["description"] = description
                if "date" not in enriched and ev is not None:
                    enriched["date"] = getattr(ev, "date", "") or ""
                enriched["trigger_keywords"] = trigger_keywords or "[]"
                enriched["categories"] = categories or "[]"
                enriched["rich_block"] = self._compose_scoring_block(
                    event_id=event_id,
                    title=title,
                    description=description,
                    trigger_keywords=enriched["trigger_keywords"],
                    categories=enriched["categories"],
                    importance=float(enriched.get("importance_score") or 0),
                    depth=float(enriched.get("impression_depth") or 0),
                    reason=str(enriched.get("reason") or ""),
                )
                out.append(enriched)
            return out

        preview_data["scored_events"] = enrich(scored)
        preview_data["evolution_candidates"] = enrich(candidates)
        return preview_data

    async def _get_int_setting(
        self,
        key: str,
        default: int,
        *,
        minimum: int | None = None,
    ) -> int:
        raw = (await self._get_setting(key, str(default))).strip()
        try:
            value = int(raw)
        except (TypeError, ValueError):
            logger.warning(
                "Invalid integer setting for %s: %r; falling back to %s",
                key,
                raw,
                default,
            )
            value = default
        if minimum is not None:
            value = max(minimum, value)
        return value

    async def _get_float_setting(
        self,
        key: str,
        default: float,
        *,
        minimum: float | None = None,
    ) -> float:
        raw = (await self._get_setting(key, str(default))).strip()
        try:
            value = float(raw)
        except (TypeError, ValueError):
            logger.warning(
                "Invalid float setting for %s: %r; falling back to %s",
                key,
                raw,
                default,
            )
            value = default
        if minimum is not None:
            value = max(minimum, value)
        return value

    @staticmethod
    def _safe_format_template(template: str, **values: str) -> str:
        class _SafeFormatDict(dict):
            def __missing__(self, key):
                return "{" + str(key) + "}"

        aliases = {
            "L1_character_background": values.get("character_background", ""),
            "L2_character_personality": values.get("character_personality", ""),
            "L2_relationship_dynamics": values.get("relationship_dynamics", ""),
            "L2_life_status": values.get("life_status", ""),
            "events": values.get("events", values.get("scored_events", "")),
        }
        merged = _SafeFormatDict()
        merged.update(values)
        merged.update(aliases)
        try:
            return template.format_map(merged)
        except Exception:
            logger.exception("Failed to format evolution summary template; using default field mapping fallback.")
            return (
                f"【当前 L1 角色人格】\n{values.get('character_background', '')}\n\n"
                f"【当前 L2 角色人格】\n{values.get('character_personality', '')}\n\n"
                f"【当前 L2 生活状态】\n{values.get('life_status', '')}\n\n"
                f"【当前 L2 关系模式】\n{values.get('relationship_dynamics', '')}\n\n"
                f"【近期事件】\n{values.get('scored_events', '')}"
            )

    @staticmethod
    def _chunk_items(items: list, size: int) -> list[list]:
        chunk_size = max(1, int(size))
        return [items[i : i + chunk_size] for i in range(0, len(items), chunk_size)]

    @staticmethod
    def _is_valid_evolution_summary_template(template: str) -> bool:
        text = (template or "").strip()
        if not text:
            return False
        required_markers = (
            "角色人格更新",
            "关系模式更新",
            "生活状态更新",
            "变更摘要",
        )
        return all(marker in text for marker in required_markers)
