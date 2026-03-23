from __future__ import annotations

import json
import math
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from server.database import Database


@dataclass
class MemoryEntry:
    id: str
    text: str
    source_type: str  # "event" or "snapshot"
    source_id: int
    metadata: dict
    score: float = 0.0


class MemoryStore(ABC):
    """Abstract interface for memory storage & retrieval.
    MVP: KeywordMemoryStore (SQLite LIKE matching)
    Future: VectorMemoryStore (cloud embedding + semantic search)
    """

    @abstractmethod
    async def store(self, entry_id: str, text: str, metadata: dict) -> str:
        ...

    @abstractmethod
    async def search(self, query: str, top_k: int = 5) -> list[MemoryEntry]:
        ...

    @abstractmethod
    async def delete(self, entry_id: str) -> None:
        ...


# Half-life in days: after this many days, the recency boost decays to 50%.
_RECENCY_HALF_LIFE_DAYS = 30.0
_ASSOCIATIVE_ENABLED = True
_ASSOCIATIVE_REPLACE_PROB = 0.2
_ASSOCIATIVE_MAX_REPLACEMENTS = 2
_ASSOCIATIVE_MIN_SCORE = 0.08
_ASSOCIATIVE_BETA = 1.2
_DIVERSITY_SAME_DAY_PENALTY = 0.4
_DIVERSITY_CATEGORY_OVERLAP_PENALTY = 0.75
_COLD_BONUS_MAX = 1.9


class KeywordMemoryStore(MemoryStore):
    """MVP implementation: keyword search with relevance + recency scoring."""

    def __init__(self, db: Database):
        self._db = db

    async def store(self, entry_id: str, text: str, metadata: dict) -> str:
        return entry_id

    async def search(self, query: str, top_k: int = 5) -> list[MemoryEntry]:
        keywords = self._extract_keywords(query)
        if not keywords:
            keywords = [query.strip()]
        keywords = [kw for kw in keywords if kw]
        if not keywords:
            return []

        candidate_limit = max(top_k * 5, 30)
        events = await self._db.search_events_by_keywords(keywords, limit=candidate_limit)
        snapshots = await self._db.search_snapshots_by_keywords(keywords, limit=candidate_limit)

        seen: dict[str, MemoryEntry] = {}
        now = datetime.utcnow()

        for ev in events:
            eid = f"event_{ev.id}"
            if eid in seen:
                continue
            kw_list = self._parse_trigger_keywords(ev.trigger_keywords)
            categories = self._parse_trigger_keywords(ev.categories)
            entry = MemoryEntry(
                id=eid,
                text=ev.description,
                source_type="event",
                source_id=ev.id,  # type: ignore
                metadata={
                    "date": ev.date,
                    "source": ev.source,
                    "keywords": kw_list,
                    "categories": categories,
                },
            )
            entry.score = self._compute_score(
                keywords, ev.description, kw_list, ev.date, now
            )
            seen[eid] = entry

        for sn in snapshots:
            sid = f"snapshot_{sn.id}"
            if sid in seen:
                continue
            entry = MemoryEntry(
                id=sid,
                text=sn.content,
                source_type="snapshot",
                source_id=sn.id,  # type: ignore
                metadata={
                    "type": sn.type,
                    "created_at": sn.created_at,
                },
            )
            entry.score = self._compute_score(
                keywords, sn.content, [], sn.created_at, now
            )
            seen[sid] = entry

        ranked = sorted(seen.values(), key=lambda e: e.score, reverse=True)
        selected = await self._select_with_association(ranked, top_k, now)
        await self._db.record_memory_recalls([e.id for e in selected])
        return selected

    async def delete(self, entry_id: str) -> None:
        pass

    # ── Scoring helpers ──

    @staticmethod
    def _compute_score(
        query_keywords: list[str],
        text: str,
        trigger_keywords: list[str],
        timestamp_str: str | None,
        now: datetime,
    ) -> float:
        """Combined score = relevance (0-1) * recency_boost (0.5-1.0)."""
        text_lower = text.lower()
        kw_lower = [k.lower() for k in trigger_keywords]
        total_kw = len(query_keywords)
        if total_kw == 0:
            return 0.0

        relevance = 0.0
        for qk in query_keywords:
            qk_l = qk.lower()
            if qk_l in kw_lower:
                relevance += 1.0
            elif qk_l in text_lower:
                relevance += 0.6
        relevance /= total_kw

        recency = 0.5
        if timestamp_str:
            try:
                ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00").split("+")[0])
                days_ago = max((now - ts).total_seconds() / 86400, 0)
                recency = 0.5 + 0.5 * math.exp(
                    -math.log(2) * days_ago / _RECENCY_HALF_LIFE_DAYS
                )
            except (ValueError, TypeError):
                pass

        return relevance * recency

    @staticmethod
    def _parse_trigger_keywords(raw: str | None) -> list[str]:
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(k) for k in parsed]
        except (json.JSONDecodeError, TypeError):
            pass
        return []

    @staticmethod
    def _extract_keywords(query: str) -> list[str]:
        """Split query into individual search terms."""
        separators = [",", "，", " ", "、"]
        parts = [query]
        for sep in separators:
            new_parts = []
            for p in parts:
                new_parts.extend(p.split(sep))
            parts = new_parts
        return [p.strip() for p in parts if p.strip()]

    async def _select_with_association(
        self,
        ranked: list[MemoryEntry],
        top_k: int,
        now: datetime,
    ) -> list[MemoryEntry]:
        if top_k <= 0:
            return []
        baseline = ranked[:top_k]
        if not _ASSOCIATIVE_ENABLED or len(baseline) < 2:
            return baseline

        replace_indices = self._pick_replacement_indices(len(baseline))
        if not replace_indices:
            return baseline

        kept_ids = {
            item.id for idx, item in enumerate(baseline)
            if idx not in replace_indices
        }
        pool = [
            e for e in ranked[top_k:]
            if e.source_type == "event"
            and e.id not in kept_ids
            and e.score >= _ASSOCIATIVE_MIN_SCORE
        ]
        if not pool:
            return baseline

        recall_stats = await self._db.get_memory_recall_stats([e.id for e in pool])
        kept_context = [
            item for idx, item in enumerate(baseline)
            if idx not in replace_indices
        ]
        assoc_picks = self._weighted_diverse_sample(
            entries=pool,
            k=len(replace_indices),
            beta=_ASSOCIATIVE_BETA,
            recall_stats=recall_stats,
            context_entries=kept_context,
            now=now,
        )
        if not assoc_picks:
            return baseline

        result = list(baseline)
        for idx, pick in zip(replace_indices, assoc_picks):
            pick.metadata = dict(pick.metadata)
            pick.metadata["selection_reason"] = "associative_random"
            result[idx] = pick
        return result

    @staticmethod
    def _pick_replacement_indices(result_len: int) -> list[int]:
        if result_len < 2:
            return []
        tail_size = min(_ASSOCIATIVE_MAX_REPLACEMENTS, result_len - 1)
        start = result_len - tail_size
        picked: list[int] = []
        for idx in range(start, result_len):
            if random.random() < _ASSOCIATIVE_REPLACE_PROB:
                picked.append(idx)
        return picked

    def _weighted_diverse_sample(
        self,
        entries: list[MemoryEntry],
        k: int,
        beta: float,
        recall_stats: dict[str, dict],
        context_entries: list[MemoryEntry],
        now: datetime,
    ) -> list[MemoryEntry]:
        if k <= 0 or not entries:
            return []
        candidates = list(entries)
        picks: list[MemoryEntry] = []
        used_dates = {
            d for d in (self._entry_day_key(e) for e in context_entries)
            if d
        }
        used_categories = {
            c for e in context_entries for c in self._entry_categories(e)
        }

        for _ in range(min(k, len(candidates))):
            weights = []
            for e in candidates:
                base = max(e.score, 1e-6) ** beta
                diversity = self._diversity_multiplier(e, used_dates, used_categories)
                cold = self._cold_bonus_multiplier(e.id, recall_stats, now)
                weights.append(base * diversity * cold)
            total = sum(weights)
            if total <= 0:
                idx = random.randrange(len(candidates))
            else:
                pivot = random.uniform(0, total)
                acc = 0.0
                idx = len(candidates) - 1
                for i, w in enumerate(weights):
                    acc += w
                    if pivot <= acc:
                        idx = i
                        break
            chosen = candidates.pop(idx)
            picks.append(chosen)
            day_key = self._entry_day_key(chosen)
            if day_key:
                used_dates.add(day_key)
            used_categories.update(self._entry_categories(chosen))
        return picks

    def _diversity_multiplier(
        self,
        entry: MemoryEntry,
        used_dates: set[str],
        used_categories: set[str],
    ) -> float:
        weight = 1.0
        day_key = self._entry_day_key(entry)
        if day_key and day_key in used_dates:
            weight *= _DIVERSITY_SAME_DAY_PENALTY
        categories = self._entry_categories(entry)
        overlap = len([c for c in categories if c in used_categories])
        if overlap > 0:
            weight *= (_DIVERSITY_CATEGORY_OVERLAP_PENALTY ** overlap)
        return max(weight, 1e-4)

    def _cold_bonus_multiplier(
        self,
        entry_id: str,
        recall_stats: dict[str, dict],
        now: datetime,
    ) -> float:
        stat = recall_stats.get(entry_id, {})
        recall_count = int(stat.get("recall_count") or 0)
        last_recalled_at = stat.get("last_recalled_at")
        # Never-recalled entries get a modest base bonus.
        if not last_recalled_at:
            days_since = 180.0
        else:
            try:
                ts = datetime.fromisoformat(str(last_recalled_at).replace("Z", "+00:00").split("+")[0])
                days_since = max((now - ts).total_seconds() / 86400.0, 0.0)
            except (ValueError, TypeError):
                days_since = 30.0

        count_bonus = 1.0 / (1.0 + 0.3 * recall_count)
        staleness_bonus = min(days_since / 60.0, 1.5)
        bonus = 1.0 + 0.45 * count_bonus + 0.45 * staleness_bonus
        return min(bonus, _COLD_BONUS_MAX)

    @staticmethod
    def _entry_day_key(entry: MemoryEntry) -> str:
        day = entry.metadata.get("date") or entry.metadata.get("created_at")
        if not day:
            return ""
        text = str(day)
        return text.split("T")[0]

    @staticmethod
    def _entry_categories(entry: MemoryEntry) -> list[str]:
        raw = entry.metadata.get("categories")
        if isinstance(raw, list):
            return [str(c) for c in raw if str(c).strip()]
        if isinstance(raw, str):
            return [s.strip() for s in raw.split(",") if s.strip()]
        return []

    @staticmethod
    def _weighted_sample_without_replacement(
        entries: list[MemoryEntry],
        k: int,
        beta: float,
    ) -> list[MemoryEntry]:
        # Deprecated helper: kept for backward compatibility and future fallback.
        if k <= 0 or not entries:
            return []

        candidates = list(entries)
        picks: list[MemoryEntry] = []
        for _ in range(min(k, len(candidates))):
            weights = [max(e.score, 1e-6) ** beta for e in candidates]
            total = sum(weights)
            if total <= 0:
                idx = random.randrange(len(candidates))
            else:
                pivot = random.uniform(0, total)
                acc = 0.0
                idx = len(candidates) - 1
                for i, w in enumerate(weights):
                    acc += w
                    if pivot <= acc:
                        idx = i
                        break
            picks.append(candidates.pop(idx))
        return picks
