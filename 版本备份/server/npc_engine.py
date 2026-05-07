from __future__ import annotations

import json
import re

from server.database import Database
from server.llm_client import LLMClient
from server.models import NPCEntity
from server.prompts import (
    KEY_L1_CHARACTER_BACKGROUND,
    KEY_PROMPT_NPC_AUTO_SPAWN,
    KEY_PROMPT_NPC_INTERACTION,
    PromptManager,
)


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
            data = json.loads(raw[start : end + 1])
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _safe_prompt_format(template: str, **kwargs) -> str:
    if not template:
        return ""
    escaped = template.replace("{", "{{").replace("}", "}}")
    for key in kwargs:
        escaped = escaped.replace("{{" + key + "}}", "{" + key + "}")
    for key in kwargs:
        escaped = re.sub(r"\{\{\s*" + re.escape(key) + r"\s*\}\}", "{" + key + "}", escaped)
    return escaped.format(**kwargs)


class NPCEngine:
    def __init__(self, db: Database, llm: LLMClient, prompt_manager: PromptManager):
        self.db = db
        self.llm = llm
        self.prompt_manager = prompt_manager

    async def get_npc(self, npc_id: int) -> NPCEntity | None:
        return await self.db.get_npc_entity_by_id(npc_id)

    async def list_active_npcs(self) -> list[NPCEntity]:
        return await self.db.list_npc_entities(status="active")

    async def resolve_interaction(
        self,
        npc: NPCEntity,
        activity_context: str,
        character_state: str,
        recent_events: str = "（暂无近期事件）",
    ) -> dict:
        template = await self.prompt_manager.get_prompt(KEY_PROMPT_NPC_INTERACTION)
        npc_profile = json.dumps(npc.model_dump(), ensure_ascii=False)
        prompt = _safe_prompt_format(
            template,
            latest_snapshot=character_state,
            activity_context=activity_context,
            npc_profile=npc_profile,
            recent_events=recent_events,
        )
        system_prompt = await self.prompt_manager.get_system_prompt()
        response = await self.llm.chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            temperature=0.6,
            max_tokens=1200,
        )
        return _extract_json_object(response)

    async def maybe_spawn_npc(self, activity_context: str) -> NPCEntity | None:
        existing = await self.list_active_npcs()
        template = await self.prompt_manager.get_prompt(KEY_PROMPT_NPC_AUTO_SPAWN)
        character_background = await self.prompt_manager.get_layer_content(
            KEY_L1_CHARACTER_BACKGROUND
        )
        prompt = _safe_prompt_format(
            template,
            activity_context=activity_context,
            existing_npcs=json.dumps([item.model_dump() for item in existing], ensure_ascii=False),
            character_background=character_background,
        )
        response = await self.llm.chat(
            [
                {"role": "system", "content": "你是世界实体生成助手。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.4,
            max_tokens=800,
        )
        payload = _extract_json_object(response)
        if not payload.get("should_spawn"):
            return None
        npc = NPCEntity(
            name=str(payload.get("name") or "").strip() or "未命名角色",
            role=str(payload.get("role") or "").strip(),
            background=str(payload.get("background") or "").strip(),
            relationship_to_character=str(payload.get("relationship_to_character") or "").strip(),
            personality_traits=json.dumps(payload.get("personality_traits") or [], ensure_ascii=False),
            spawn_source="auto_generated",
            spawn_context=activity_context,
        )
        npc.id = await self.db.insert_npc_entity(npc)
        return npc
