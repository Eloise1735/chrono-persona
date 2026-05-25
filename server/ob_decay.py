from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from server.ob_client import OBClient

logger = logging.getLogger(__name__)


@dataclass
class OBDecaySettings:
    lambda_: float = 0.05
    threshold: float = 0.3
    check_interval_hours: float = 24.0
    emotion_base: float = 1.0
    arousal_boost: float = 0.8


class OBDecayEngine:
    """Background decay loop for Ombre Brain buckets."""

    def __init__(self, ob_client: OBClient, settings: OBDecaySettings | None = None):
        self.ob_client = ob_client
        self.settings = settings or OBDecaySettings()
        self.ob_client.decay_lambda = self.settings.lambda_
        self.ob_client.decay_emotion_base = self.settings.emotion_base
        self.ob_client.decay_arousal_boost = self.settings.arousal_boost
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self.last_result: dict[str, Any] | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def ensure_started(self) -> None:
        if not self.is_running:
            await self.start()

    async def start(self) -> None:
        if self.is_running:
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._background_loop(), name="ob-decay-loop")
        logger.info("OB decay engine started.")

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._stop_event = None
        logger.info("OB decay engine stopped.")

    async def run_decay_cycle(self, *, dry_run: bool = False) -> dict[str, Any]:
        buckets = await self.ob_client.list_buckets(include_archive=False)
        checked = 0
        archived = 0
        auto_resolved = 0
        lowest_score = float("inf")
        archived_ids: list[str] = []
        auto_resolved_ids: list[str] = []
        skipped = 0

        now = datetime.utcnow()
        for bucket in buckets:
            meta = bucket.metadata or {}
            bucket_type = self.ob_client._bucket_type(meta)
            if bucket_type in {"permanent", "feel", "archived"} or meta.get("pinned") or meta.get("protected"):
                skipped += 1
                continue

            checked += 1
            if not meta.get("resolved", False):
                importance = int(meta.get("importance", 5) or 5)
                days_since = self.ob_client._days_since(meta.get("last_active") or meta.get("created"), now)
                if importance <= 4 and days_since > 30:
                    auto_resolved += 1
                    auto_resolved_ids.append(bucket.id)
                    meta["resolved"] = True
                    if not dry_run:
                        await self.ob_client.update(bucket.id, resolved=True, auto_resolved_at=now.isoformat())

            score = self.ob_client.calculate_score(meta)
            lowest_score = min(lowest_score, score)
            if score < float(self.settings.threshold):
                archived += 1
                archived_ids.append(bucket.id)
                if not dry_run:
                    await self.ob_client.archive(bucket.id)

        result = {
            "checked": checked,
            "skipped": skipped,
            "archived": archived,
            "archived_ids": archived_ids,
            "auto_resolved": auto_resolved,
            "auto_resolved_ids": auto_resolved_ids,
            "lowest_score": 0 if checked == 0 else lowest_score,
            "threshold": float(self.settings.threshold),
            "dry_run": bool(dry_run),
            "ran_at": now.isoformat(),
        }
        if not dry_run:
            self.last_result = result
        return result

    async def _background_loop(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                self.last_result = await self.run_decay_cycle(dry_run=False)
                logger.info("OB decay cycle complete: %s", self.last_result)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("OB decay cycle failed.")

            interval = max(60.0, float(self.settings.check_interval_hours) * 3600.0)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue


def ob_decay_settings_from_config(config: Any) -> OBDecaySettings:
    decay = getattr(getattr(config, "ob", None), "decay", None)
    emotion = getattr(decay, "emotion_weights", None)
    return OBDecaySettings(
        lambda_=float(getattr(decay, "lambda_", 0.05) if decay is not None else 0.05),
        threshold=float(getattr(decay, "threshold", 0.3) if decay is not None else 0.3),
        check_interval_hours=float(getattr(decay, "check_interval_hours", 24) if decay is not None else 24),
        emotion_base=float(getattr(emotion, "base", 1.0) if emotion is not None else 1.0),
        arousal_boost=float(getattr(emotion, "arousal_boost", 0.8) if emotion is not None else 0.8),
    )
