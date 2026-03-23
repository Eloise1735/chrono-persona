from __future__ import annotations

import re
from datetime import datetime

from server.database import Database
from server.llm_client import LLMClient
from server.prompts import (
    PromptManager,
    KEY_PROMPT_EVENT_SCORING,
    KEY_PROMPT_EVOLUTION_SUMMARY,
    KEY_L2_CHARACTER_PERSONALITY,
    KEY_L2_RELATIONSHIP_DYNAMICS,
    KEY_LAST_EVOLUTION_TIME,
    KEY_EVOLUTION_EVENT_THRESHOLD,
    KEY_ARCHIVE_IMPORTANCE_THRESHOLD,
)


class EvolutionEngine:
    def __init__(self, db: Database, llm: LLMClient, prompt_manager: PromptManager):
        self.db = db
        self.llm = llm
        self.prompt_manager = prompt_manager

    async def check_status(self) -> dict:
        last_time = await self._get_setting(KEY_LAST_EVOLUTION_TIME, "")
        threshold = int(await self._get_setting(KEY_EVOLUTION_EVENT_THRESHOLD, "10"))
        since = last_time or "1970-01-01T00:00:00"
        event_count = await self.db.count_events_since(since, include_archived=False)
        return {
            "should_evolve": event_count >= threshold,
            "event_count": event_count,
            "threshold": threshold,
            "last_time": last_time,
        }

    async def preview(self) -> dict:
        status = await self.check_status()
        since = status["last_time"] or "1970-01-01T00:00:00"
        events = await self.db.get_events_since(since, limit=200, include_archived=False)
        if not events:
            return {
                **status,
                "scored_events": [],
                "new_character_personality": await self.prompt_manager.get_layer_content(
                    KEY_L2_CHARACTER_PERSONALITY
                ),
                "new_relationship_dynamics": await self.prompt_manager.get_layer_content(
                    KEY_L2_RELATIONSHIP_DYNAMICS
                ),
                "change_summary": "没有新的活跃事件，无需演化。",
            }

        scored_events = await self._score_events(events)
        new_character, new_relationship, summary = await self._generate_updates(
            scored_events
        )
        return {
            **status,
            "scored_events": scored_events,
            "new_character_personality": new_character,
            "new_relationship_dynamics": new_relationship,
            "change_summary": summary,
        }

    async def apply(self, preview_data: dict) -> dict:
        scored_events = preview_data.get("scored_events", [])
        new_character = preview_data.get("new_character_personality", "").strip()
        new_relationship = preview_data.get("new_relationship_dynamics", "").strip()

        if new_character:
            await self.prompt_manager.set_layer_content(
                KEY_L2_CHARACTER_PERSONALITY, new_character
            )
        if new_relationship:
            await self.prompt_manager.set_layer_content(
                KEY_L2_RELATIONSHIP_DYNAMICS, new_relationship
            )

        archive_threshold = float(
            await self._get_setting(KEY_ARCHIVE_IMPORTANCE_THRESHOLD, "3.0")
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
            if importance < archive_threshold:
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
        return {
            "updated_keys": [
                KEY_L2_CHARACTER_PERSONALITY,
                KEY_L2_RELATIONSHIP_DYNAMICS,
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
        archive_threshold = float(
            await self._get_setting(KEY_ARCHIVE_IMPORTANCE_THRESHOLD, "3.0")
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
            new_archived = 1 if float(event.importance_score) < archive_threshold else 0
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

    async def _score_events(self, events: list) -> list[dict]:
        event_lines = "\n".join(
            f"- 事件ID:{e.id} | 日期:{e.date} | 描述:{e.description}" for e in events
        )
        template = await self.prompt_manager.get_prompt(KEY_PROMPT_EVENT_SCORING)
        prompt = template.format(events=event_lines)
        system_prompt = await self.prompt_manager.get_system_prompt()
        response = await self.llm.chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ]
        )

        scored: list[dict] = []
        for e in events:
            block = self._find_event_block(response, int(e.id or 0))
            importance = self._extract_score(block, "重要性")
            depth = self._extract_score(block, "印象深度")
            reason = self._extract_reason(block)
            scored.append(
                {
                    "id": int(e.id or 0),
                    "date": e.date,
                    "description": e.description,
                    "importance_score": importance,
                    "impression_depth": depth,
                    "reason": reason,
                }
            )
        scored.sort(
            key=lambda x: (x["importance_score"], x["impression_depth"]), reverse=True
        )
        return scored

    async def _generate_updates(self, scored_events: list[dict]) -> tuple[str, str, str]:
        character = await self.prompt_manager.get_layer_content(
            KEY_L2_CHARACTER_PERSONALITY
        )
        relationship = await self.prompt_manager.get_layer_content(
            KEY_L2_RELATIONSHIP_DYNAMICS
        )
        scored_text = "\n".join(
            (
                f"- 事件ID:{e['id']} | 重要性:{e['importance_score']:.1f} | "
                f"印象深度:{e['impression_depth']:.1f} | 内容:{e['description']}"
            )
            for e in scored_events[:30]
        )
        template = await self.prompt_manager.get_prompt(KEY_PROMPT_EVOLUTION_SUMMARY)
        prompt = template.format(
            character_personality=character,
            relationship_dynamics=relationship,
            scored_events=scored_text,
        )
        system_prompt = await self.prompt_manager.get_system_prompt()
        response = await self.llm.chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ]
        )
        new_character = self._extract_section(response, "角色人格更新") or character
        new_relationship = self._extract_section(response, "关系模式更新") or relationship
        summary = self._extract_section(response, "变更摘要") or "已完成人格层预览更新。"
        return new_character.strip(), new_relationship.strip(), summary.strip()

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
        m = re.search(r"理由\s*[：:]\s*(.+)", block)
        return m.group(1).strip() if m else ""

    @staticmethod
    def _extract_section(text: str, title: str) -> str:
        m = re.search(
            rf"{title}\s*[：:]\s*(.+?)(?=\n(?:角色人格更新|关系模式更新|变更摘要)\s*[：:]|\Z)",
            text,
            re.DOTALL,
        )
        return m.group(1).strip() if m else ""

    async def _get_setting(self, key: str, default: str) -> str:
        row = await self.db.get_setting(key)
        if row and row.get("value") is not None:
            return str(row["value"])
        return default
