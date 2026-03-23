from __future__ import annotations

import json
from datetime import datetime, timedelta

from server.database import Database
from server.evolution import EvolutionEngine
from server.prompts import PromptManager


KEY_AUTOMATION_ENABLED = "automation_enabled"
KEY_AUTOMATION_VECTOR_SYNC = "automation_vector_sync"
KEY_AUTOMATION_AUTO_EVOLUTION = "automation_auto_evolution"
KEY_AUTOMATION_COLD_COMPACTION = "automation_cold_compaction"
KEY_AUTOMATION_COMPACTION_MIN_INTERVAL_HOURS = "automation_compaction_min_interval_hours"
KEY_AUTOMATION_LAST_COMPACTION_TIME = "automation_last_compaction_time"


class AutomationEngine:
    def __init__(
        self,
        db: Database,
        prompt_manager: PromptManager,
        memory_store,
        evolution_engine: EvolutionEngine | None = None,
    ):
        self._db = db
        self._prompt_manager = prompt_manager
        self._memory_store = memory_store
        self._evolution_engine = evolution_engine

    async def run(self, trigger: str) -> dict:
        report = {
            "trigger": trigger,
            "ran": False,
            "vector_sync": None,
            "evolution": None,
            "compaction": None,
            "errors": [],
        }
        if not await self._enabled(KEY_AUTOMATION_ENABLED, True):
            return report
        report["ran"] = True

        if await self._enabled(KEY_AUTOMATION_VECTOR_SYNC, True):
            sync = getattr(self._memory_store, "sync_eligible_vectors", None)
            if callable(sync):
                try:
                    report["vector_sync"] = await sync()
                except Exception as exc:
                    report["errors"].append(f"vector_sync: {exc}")

        if await self._enabled(KEY_AUTOMATION_AUTO_EVOLUTION, True):
            if self._evolution_engine is not None:
                try:
                    status = await self._evolution_engine.check_status()
                    if status.get("should_evolve"):
                        preview = await self._evolution_engine.preview()
                        applied = await self._evolution_engine.apply(preview)
                        report["evolution"] = {
                            "status": status,
                            "applied": True,
                            "archived_count": applied.get("archived_count", 0),
                        }
                    else:
                        report["evolution"] = {"status": status, "applied": False}
                except Exception as exc:
                    report["errors"].append(f"evolution: {exc}")

        if await self._enabled(KEY_AUTOMATION_COLD_COMPACTION, True):
            compact = getattr(self._memory_store, "compact_cold_memories", None)
            if callable(compact) and await self._should_run_compaction():
                try:
                    dry_run = await compact(dry_run=True)
                    if int(dry_run.get("would_compact_count", 0)) > 0:
                        real = await compact(dry_run=False)
                        report["compaction"] = real
                    else:
                        report["compaction"] = dry_run
                    await self._touch_compaction_time()
                except Exception as exc:
                    report["errors"].append(f"compaction: {exc}")
        return report

    async def _enabled(self, key: str, default: bool) -> bool:
        raw = await self._prompt_manager.get_config_value(key)
        text = str(raw).strip().lower()
        if not text:
            return default
        return text in {"1", "true", "yes", "on", "enabled"}

    async def _should_run_compaction(self) -> bool:
        raw_hours = await self._prompt_manager.get_config_value(
            KEY_AUTOMATION_COMPACTION_MIN_INTERVAL_HOURS
        )
        try:
            hours = max(1, int(raw_hours))
        except Exception:
            hours = 24
        last_raw = await self._prompt_manager.get_config_value(KEY_AUTOMATION_LAST_COMPACTION_TIME)
        if not str(last_raw).strip():
            return True
        try:
            last_time = datetime.fromisoformat(str(last_raw))
        except Exception:
            return True
        return datetime.utcnow() - last_time >= timedelta(hours=hours)

    async def _touch_compaction_time(self):
        now = datetime.utcnow().isoformat()
        await self._db.set_setting(
            key=KEY_AUTOMATION_LAST_COMPACTION_TIME,
            value=now,
            category="automation",
            description="自动压缩上次执行时间",
        )

    async def persist_run_report(self, report: dict):
        try:
            await self._db.insert_automation_run(
                trigger=str(report.get("trigger", "")),
                ran=bool(report.get("ran", False)),
                report_json=json.dumps(report, ensure_ascii=False),
            )
        except Exception:
            # Report persistence should never break main flow.
            return
