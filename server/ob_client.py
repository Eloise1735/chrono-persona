from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import random
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass
class OBBucket:
    id: str
    content: str
    metadata: dict[str, Any]
    path: str
    score: float = 0.0


class OBClient:
    """Reference-aligned Ombre Brain bucket store.

    This keeps the current project's embedded OB entrypoint, but follows the
    lifecycle from the reference project:
    - natural breath surfaces without touch
    - query breath touches recall hits
    - feel is an independent channel
    - dream reads candidates only; it never writes
    - decay/archive are explicit lifecycle operations
    """

    ARCHIVE_TYPES = {"archive", "archived"}

    # ── stable-layer role model (spec v2) ──────────────────────────────
    # permanent buckets are "exempt from forgetting"; within permanent, a
    # single `role` axis decides behaviour. See
    # docs/ob_consolidation_primitive_spec.md §2.
    ROLE_EVOLVING = "evolving_principle"   # 关系整体感受/相处模式；注入按新近 N
    ROLE_STANDING = "standing_invariant"   # 边界/稳定偏好/重大共识；始终注入
    ROLE_ANCHOR = "anchor"                 # 原汁原味关键事件；仅 recall，不自动注入
    PERMANENT_ROLES = {ROLE_EVOLVING, ROLE_STANDING, ROLE_ANCHOR}
    EVOLVING_PRINCIPLE_INJECT_LIMIT = 5    # evolving 注入窗（新近）

    def __init__(self, buckets_dir: str | Path, *, embedding_store: Any | None = None):
        self.base_dir = Path(buckets_dir)
        self.embedding_store = embedding_store
        self.decay_lambda = 0.05
        self.decay_emotion_base = 1.0
        self.decay_arousal_boost = 0.8
        # feel aging: feel used to be a flat 50 plateau that never decayed nor
        # archived (a pure write-only leak). It now decays gently from 50 so
        # that genuinely old, un-recalled feel eventually drops below the decay
        # archive threshold. Half-life ≈ 17 days; an uncrystallized feel reaches
        # the 0.3 archive line around ~128 days, a crystallized one (×0.1) around
        # ~70 days — both well past the 7-day crystallized protection window, so
        # surfacing is unaffected (breath sets feel score to 50 at selection).
        self.feel_decay_lambda = 0.04
        # Merge SUGGESTIONS on hold(): the backend never merges on its own — it
        # only surfaces recent similar same-type buckets as candidates and lets
        # the frontend model decide (and call merge_buckets explicitly). The
        # thresholds are deliberately a little permissive so the model gets to
        # judge borderline cases rather than the backend silently dropping them.
        self.merge_suggest_embedding_min_similarity = 0.82
        self.merge_suggest_text_min_ratio = 0.72
        self.merge_suggest_recency_days = 14.0
        self.merge_suggest_top_k = 3
        self.permanent_dir = self.base_dir / "permanent"
        self.dynamic_dir = self.base_dir / "dynamic"
        self.archive_dir = self.base_dir / "archive"
        self.feel_dir = self.base_dir / "feel"
        for path in (self.permanent_dir, self.dynamic_dir, self.archive_dir, self.feel_dir):
            path.mkdir(parents=True, exist_ok=True)

    def set_embedding_store(self, embedding_store: Any | None) -> None:
        self.embedding_store = embedding_store

    async def hold(
        self,
        content: str,
        *,
        tags: list[str] | None = None,
        importance: int = 5,
        domain: list[str] | None = None,
        valence: float = 0.5,
        arousal: float = 0.3,
        bucket_type: str = "dynamic",
        name: str | None = None,
        pinned: bool = False,
        protected: bool = False,
        resolved: bool = False,
        created: str | None = None,
        bucket_id: str | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> str:
        content = str(content or "").strip()
        if not content:
            return ""
        bucket_id = self._safe_id(bucket_id) if bucket_id else uuid.uuid4().hex[:12]
        bucket_type = "permanent" if pinned else self._normalize_bucket_type(bucket_type)
        if bucket_type == "feel":
            domain = domain if domain is not None else []
        else:
            domain = [d for d in (domain or ["未分类"]) if str(d).strip()] or ["未分类"]

        now = datetime.utcnow().isoformat()
        metadata: dict[str, Any] = {
            "id": bucket_id,
            "name": name or bucket_id,
            "tags": tags or [],
            "domain": domain,
            "valence": self._clamp_float(valence, 0.0, 1.0, 0.5),
            "arousal": self._clamp_float(arousal, 0.0, 1.0, 0.3),
            "importance": max(1, min(10, int(importance or 5))),
            "type": bucket_type,
            "created": created or now,
            "last_active": created or now,
            "activation_count": 0,
            "resolved": bool(resolved),
        }
        if pinned:
            metadata["pinned"] = True
            metadata["importance"] = 10
            metadata["type"] = "permanent"
        if protected:
            metadata["protected"] = True
            metadata["importance"] = 10
        if extra_metadata:
            metadata.update(extra_metadata)

        path = self._path_for(metadata)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._dump_bucket(metadata, content), encoding="utf-8")
        self._schedule_embedding_upsert(bucket_id, content)
        return bucket_id

    @staticmethod
    def _normalize_for_dedupe(text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "")).strip().lower()

    def _merge_candidate_ok(
        self, bucket: OBBucket, want_type: str, now: datetime, *, exclude_id: str | None
    ) -> bool:
        meta = bucket.metadata or {}
        if exclude_id is not None and bucket.id == exclude_id:
            return False
        if self._bucket_type(meta) != want_type:
            return False
        if meta.get("pinned") or meta.get("protected"):
            return False
        if meta.get("resolved"):
            return False
        if want_type == "feel" and meta.get("crystallized"):
            return False
        # Never suggest folding a relational note into the character-life stream.
        if self._is_character_life_bucket(bucket):
            return False
        age_days = self._bucket_age_days(meta, prefer_keys=("last_active", "created"))
        return age_days <= float(self.merge_suggest_recency_days)

    async def find_merge_candidates(
        self,
        content: str,
        *,
        bucket_type: str,
        exclude_id: str | None = None,
        top_k: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return recent same-type buckets that look like merge candidates.

        The backend NEVER merges on its own — this only proposes. The frontend
        model inspects the candidates and decides whether to call merge_buckets.
        Each entry: {id, name, similarity, created, excerpt}. Sorted desc by
        similarity. Embedding similarity when available, else difflib text ratio.
        """
        norm_new = self._normalize_for_dedupe(content)
        if not norm_new:
            return []
        limit = max(1, int(top_k if top_k is not None else self.merge_suggest_top_k))
        now = datetime.utcnow()
        scored: list[tuple[float, OBBucket]] = []

        store = self.embedding_store
        store_enabled = False
        if store is not None:
            try:
                store_enabled = await store.is_enabled()
            except Exception:
                store_enabled = False
        if store_enabled:
            try:
                pairs = await store.search_similar(content, top_k=max(10, limit * 4))
            except Exception:
                pairs = []
            for bid, sim in pairs:
                if float(sim) < float(self.merge_suggest_embedding_min_similarity):
                    break  # search_similar is sorted desc
                bucket = await self.get(str(bid))
                if bucket is not None and self._merge_candidate_ok(bucket, bucket_type, now, exclude_id=exclude_id):
                    scored.append((float(sim), bucket))
        else:
            import difflib

            for bucket in await self.list_buckets(include_archive=False):
                if not self._merge_candidate_ok(bucket, bucket_type, now, exclude_id=exclude_id):
                    continue
                ratio = difflib.SequenceMatcher(
                    None, norm_new, self._normalize_for_dedupe(bucket.content)
                ).ratio()
                if ratio >= float(self.merge_suggest_text_min_ratio):
                    scored.append((float(ratio), bucket))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        out: list[dict[str, Any]] = []
        for sim, bucket in scored[:limit]:
            meta = bucket.metadata or {}
            out.append({
                "id": bucket.id,
                "name": meta.get("name") or bucket.id,
                "similarity": round(sim, 4),
                "created": meta.get("created", ""),
                "excerpt": self._strip_wikilinks(bucket.content)[:240],
            })
        return out

    async def merge_content_into(
        self,
        target_id: str,
        content: str,
        *,
        bucket_type: str | None = None,
        importance: int = 5,
        valence: float = 0.5,
        arousal: float = 0.3,
    ) -> dict[str, Any]:
        """Merge raw new content into an existing bucket (the write-time merge
        path used by hold's two-phase flow). Validates type + that target is not
        a stable principle. Returns {ok, target_id} or {ok: False, error}."""
        target = await self.get(str(target_id))
        if target is None:
            return {"ok": False, "error": f"未找到 target bucket: {target_id}"}
        t_type = self._bucket_type(target.metadata)
        if bucket_type and t_type != str(bucket_type):
            return {"ok": False, "error": f"类型不一致，拒绝合并：target={t_type} 期望={bucket_type}"}
        if target.metadata.get("pinned") or target.metadata.get("protected") or t_type == "permanent":
            return {"ok": False, "error": "target 是 pinned/protected/permanent，不能作为合并落点"}
        await self._merge_into_bucket(
            target, content, importance=importance, valence=valence, arousal=arousal
        )
        return {"ok": True, "target_id": str(target_id)}

    async def merge_buckets(self, source_id: str, target_id: str) -> dict[str, Any]:
        """Merge source bucket into target (model-directed), then delete source.

        Validates that both exist and are the same type; refuses to merge into a
        pinned/protected/permanent target (those are stable principles, not a
        dynamic/feel sink). Returns {ok, target_id, ...} or {ok: False, error}.
        """
        if str(source_id) == str(target_id):
            return {"ok": False, "error": "source 与 target 不能相同"}
        source = await self.get(str(source_id))
        target = await self.get(str(target_id))
        if source is None:
            return {"ok": False, "error": f"未找到 source bucket: {source_id}"}
        if target is None:
            return {"ok": False, "error": f"未找到 target bucket: {target_id}"}
        s_type = self._bucket_type(source.metadata)
        t_type = self._bucket_type(target.metadata)
        if t_type != s_type:
            return {"ok": False, "error": f"类型不一致，拒绝合并：source={s_type} target={t_type}"}
        if target.metadata.get("pinned") or target.metadata.get("protected") or t_type == "permanent":
            return {"ok": False, "error": "target 是 pinned/protected/permanent，不能作为合并落点"}
        await self._merge_into_bucket(
            target,
            source.content,
            importance=int(source.metadata.get("importance", 5) or 5),
            valence=self._clamp_float(source.metadata.get("valence"), 0.0, 1.0, 0.5),
            arousal=self._clamp_float(source.metadata.get("arousal"), 0.0, 1.0, 0.3),
        )
        await self.delete(str(source_id))
        return {"ok": True, "target_id": str(target_id), "merged_source_id": str(source_id)}

    async def _merge_into_bucket(
        self,
        bucket: OBBucket,
        content: str,
        *,
        importance: int = 5,
        valence: float = 0.5,
        arousal: float = 0.3,
    ) -> str:
        """Append new content into an existing bucket, blending affect/importance."""
        merged = f"{bucket.content.rstrip()}\n\n---\n{str(content or '').strip()}"
        updates: dict[str, Any] = {
            "content": merged,
            "last_active": datetime.utcnow().isoformat(),
        }
        cur_importance = int(bucket.metadata.get("importance", 5) or 5)
        updates["importance"] = max(cur_importance, int(importance or 5))
        old_v = self._clamp_float(bucket.metadata.get("valence"), 0.0, 1.0, 0.5)
        updates["valence"] = round((old_v + self._clamp_float(valence, 0.0, 1.0, 0.5)) / 2, 2)
        old_a = self._clamp_float(bucket.metadata.get("arousal"), 0.0, 1.0, 0.3)
        updates["arousal"] = round((old_a + self._clamp_float(arousal, 0.0, 1.0, 0.3)) / 2, 2)
        await self.update(bucket.id, **updates)
        return bucket.id

    async def breath(
        self,
        query: str = "",
        *,
        limit: int = 0,
        domain: str | list[str] | None = None,
        valence: float | None = None,
        arousal: float | None = None,
        include_archive: bool = False,
        importance_min: int = 0,
        include_core: bool = True,
        date_from: str | None = None,
        date_to: str | None = None,
        include_character_life: bool = False,
    ) -> list[OBBucket]:
        """OB breath: query / surface / feel routing.

        ``date_from`` / ``date_to`` (optional YYYY-MM-DD or full ISO) filter candidates by
        ``metadata.created`` (the WRITE date — touch/last_active does NOT shift this).

        ``include_character_life`` 默认 False —— breath 默认**排除** character_life buckets。
        理由：character_life 内容会大量浮现并掩盖关系侧/用户侧的回忆，造成"角色侧噪音"。
        当调用方需要 character_life 内容时：
          - 传 ``domain="character_life"`` （会自动包含，因为这是显式请求该 domain）
          - 或传 ``include_character_life=True`` （混合返回）
        breath_bundle 内部的 personal 三槽通过 `_character_life_pins` 直接走，不受此默认影响。
        """
        query = str(query or "").strip()
        domains = self._domain_filter(domain)
        limit = max(1, min(50, int(limit or (3 if domains == {"feel"} else 8))))
        date_window = self._normalize_date_window(date_from, date_to)
        # 当 caller 显式传 character_life domain，等同于"请专门给我 character_life"
        # → 自然应该包含 character_life；否则默认排除。
        include_char_life = bool(include_character_life) or "character_life" in domains

        if domains == {"feel"}:
            return await self._feel_breath(
                limit=limit,
                date_window=date_window,
                # 不显式包含 char_life 时，feel 池也禁止 char_life feel 渗透
                max_character_life=1 if include_char_life else 0,
            )

        if importance_min and not query:
            buckets = [
                b for b in await self.list_buckets(include_archive=include_archive)
                if self._bucket_type(b.metadata) != "feel"
                and int(b.metadata.get("importance", 0) or 0) >= int(importance_min)
                and self._bucket_in_date_window(b, date_window)
                and (include_char_life or not self._is_character_life_bucket(b))
            ]
            buckets.sort(key=lambda b: int(b.metadata.get("importance", 0) or 0), reverse=True)
            for bucket in buckets:
                bucket.score = self.calculate_score(bucket.metadata)
            return buckets[:limit]

        if query:
            selected = await self._query_breath(
                query,
                limit=limit,
                domains=domains,
                valence=valence,
                arousal=arousal,
                include_archive=include_archive,
                date_window=date_window,
                include_character_life=include_char_life,
            )
            for bucket in selected:
                await self.touch(bucket.id)
            return selected

        return await self._surface_breath(
            limit=limit,
            domains=domains,
            include_core=include_core,
            date_window=date_window,
            include_character_life=include_char_life,
        )

    def _normalize_date_window(
        self, date_from: str | None, date_to: str | None
    ) -> tuple[datetime | None, datetime | None]:
        """Parse YYYY-MM-DD or ISO datetime strings into a UTC-naive window tuple.

        Returns (start_inclusive, end_inclusive). Either or both can be None — meaning open.
        End-of-day semantics: when only a date string is given for date_to, extend to the
        end of that day (so date_to=2026-05-27 includes everything up to 2026-05-27 23:59:59).
        """
        def _parse(value: str | None, *, end_of_day: bool) -> datetime | None:
            if not value:
                return None
            raw = str(value).strip()
            if not raw:
                return None
            parsed = self._parse_dt(raw)
            if parsed is None:
                # Accept bare YYYY-MM-DD even if _parse_dt is strict.
                try:
                    parsed = datetime.strptime(raw[:10], "%Y-%m-%d")
                except Exception:
                    return None
            # If user passed only YYYY-MM-DD and end_of_day, push to 23:59:59.
            is_date_only = len(raw) == 10 and raw.count("-") == 2
            if end_of_day and is_date_only:
                parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
            return parsed

        return _parse(date_from, end_of_day=False), _parse(date_to, end_of_day=True)

    def _bucket_in_date_window(
        self,
        bucket: OBBucket,
        date_window: tuple[datetime | None, datetime | None],
    ) -> bool:
        start, end = date_window
        if start is None and end is None:
            return True
        # 显式使用 `created` 而不是 `last_active`：date_from/date_to 语义是"按写入日期检索"——
        # last_active 会被 breath/touch 反复刷新（每次浮现都改），用它做窗口会让同一条记忆
        # 不断"漂移"出窗口。created 在写入后基本冻结（touch 不改、grow merge 也不改），
        # 是稳定的写入时间锚点。
        ts_raw = str((bucket.metadata or {}).get("created") or "").strip()
        if not ts_raw:
            # buckets without a created timestamp are not date-filterable; keep them inclusive.
            return True
        ts = self._parse_dt(ts_raw)
        if ts is None:
            return True
        if start is not None and ts < start:
            return False
        if end is not None and ts > end:
            return False
        return True

    # 一周保护窗：刚 crystallize 的 feel 仍然有现实连续性价值（角色对话中
    # 可能会自然引用昨天的感受）。dream 每天晚上跑会把 feel 标 crystallized，
    # 若立即从浮现池剔除，角色会觉得"昨天的事已经忘了"。允许 crystallized
    # AND 距 crystallized_at（或 fallback 时间戳）≤7 天的 feel 继续浮现。
    FEEL_CRYSTALLIZED_PROTECTION_DAYS = 7.0

    def _feel_is_eligible(self, meta: dict) -> bool:
        """Filter feel for breath: keep non-crystallized; keep crystallized within protection window."""
        if not meta.get("crystallized"):
            return True
        age_days = self._bucket_age_days(
            meta,
            prefer_keys=("crystallized_at", "last_active", "created"),
        )
        return age_days <= self.FEEL_CRYSTALLIZED_PROTECTION_DAYS

    def _bucket_age_days(self, meta: dict, *, prefer_keys: tuple[str, ...]) -> float:
        """Return age in days using the first available timestamp key. 0.0 on parse failure."""
        for key in prefer_keys:
            ts_raw = str(meta.get(key) or "").strip()
            if not ts_raw:
                continue
            try:
                ts = self._parse_dt(ts_raw)
            except Exception:
                continue
            if ts is None:
                continue
            try:
                delta = datetime.utcnow() - ts
            except Exception:
                continue
            return max(0.0, delta.total_seconds() / 86400.0)
        return 0.0

    async def _feel_breath(
        self,
        *,
        limit: int,
        max_character_life: int = 1,
        date_window: tuple[datetime | None, datetime | None] | None = None,
    ) -> list[OBBucket]:
        window = date_window or (None, None)
        feels = [
            bucket for bucket in await self.list_buckets(include_archive=False)
            if self._bucket_type(bucket.metadata) == "feel"
            and self._feel_is_eligible(bucket.metadata)
            and self._bucket_in_date_window(bucket, window)
        ]
        feels.sort(key=lambda b: str(b.metadata.get("created") or ""), reverse=True)
        char_limit = max(0, min(int(max_character_life or 0), int(limit or 0)))
        char_items: list[OBBucket] = []
        other_items: list[OBBucket] = []
        for bucket in feels:
            if self._is_character_life_bucket(bucket):
                if len(char_items) < char_limit:
                    char_items.append(bucket)
                continue
            other_items.append(bucket)
        selected = char_items + other_items[: max(0, int(limit or 0) - len(char_items))]
        # Note: do NOT re-sort by created here; char_items must stay in front. They are
        # already created-desc within each group because `feels` was sorted above.
        for bucket in selected:
            bucket.score = 50.0
        return selected[:limit]

    async def _surface_breath(
        self,
        *,
        limit: int,
        domains: set[str],
        include_core: bool = True,
        date_window: tuple[datetime | None, datetime | None] | None = None,
        include_character_life: bool = True,
    ) -> list[OBBucket]:
        window = date_window or (None, None)
        buckets = await self.list_buckets(include_archive=False)
        if domains:
            buckets = [b for b in buckets if self._matches_domains(b, domains)]
        if window != (None, None):
            buckets = [b for b in buckets if self._bucket_in_date_window(b, window)]
        if not include_character_life:
            buckets = [b for b in buckets if not self._is_character_life_bucket(b)]

        core_quota = min(2, limit)
        pinned_core: list[OBBucket] = []
        protected_core: list[OBBucket] = []
        if include_core:
            # anchor-role permanent is recall-only: it must never take an
            # auto-surfacing core slot (spec §2.2). evolving/standing still may.
            pinned_core = [
                b for b in buckets
                if b.metadata.get("pinned")
                and self.effective_role(b.metadata) != self.ROLE_ANCHOR
            ]
            protected_core = [
                b for b in buckets
                if b.metadata.get("protected")
                and not b.metadata.get("pinned")
                and self.effective_role(b.metadata) != self.ROLE_ANCHOR
                and (
                    self._bucket_type(b.metadata) == "permanent"
                    or "core" in {str(d).strip() for d in b.metadata.get("domain", [])}
                )
            ]
        for bucket in pinned_core + protected_core:
            bucket.score = self.calculate_score(bucket.metadata)
        pinned_core.sort(key=lambda b: str(b.metadata.get("created") or ""), reverse=True)
        protected_core.sort(key=lambda b: str(b.metadata.get("created") or ""), reverse=True)
        core = (pinned_core + protected_core)[:core_quota]

        dynamic = [
            b for b in buckets
            if self._bucket_type(b.metadata) == "dynamic"
            and not b.metadata.get("pinned")
            and not b.metadata.get("protected")
            and not b.metadata.get("resolved")
        ]
        for bucket in dynamic:
            bucket.score = self.calculate_score(bucket.metadata)
        dynamic.sort(key=lambda b: b.score, reverse=True)

        selected: list[OBBucket] = []
        selected_ids: set[str] = set()
        character_dynamic_count = 0
        character_dynamic_limit = 2

        def can_add(bucket: OBBucket) -> bool:
            if bucket.id in selected_ids:
                return False
            if (
                self._bucket_type(bucket.metadata) == "dynamic"
                and self._is_character_life_bucket(bucket)
                and character_dynamic_count >= character_dynamic_limit
            ):
                return False
            return True

        def add(bucket: OBBucket) -> bool:
            nonlocal character_dynamic_count
            if len(selected) >= limit or not can_add(bucket):
                return False
            selected.append(bucket)
            selected_ids.add(bucket.id)
            if self._bucket_type(bucket.metadata) == "dynamic" and self._is_character_life_bucket(bucket):
                character_dynamic_count += 1
            return True

        for bucket in core:
            add(bucket)

        cold = [
            b for b in dynamic
            if int(float(b.metadata.get("activation_count", 0) or 0)) == 0
            and int(b.metadata.get("importance", 0) or 0) >= 7
        ]
        cold_ids = {b.id for b in cold}
        for bucket in cold[:1]:
            add(bucket)

        rest = [b for b in dynamic if b.id not in cold_ids]
        high_added = 0
        for bucket in rest:
            if high_added >= 4:
                break
            if add(bucket):
                high_added += 1

        pool = [b for b in rest if b.id not in selected_ids][:24]
        random.shuffle(pool)
        for bucket in pool:
            if len(selected) >= limit:
                break
            add(bucket)

        for bucket in dynamic:
            if len(selected) >= limit:
                break
            add(bucket)
        return selected[:limit]

    async def breath_personal(self) -> dict[str, Any]:
        """Lightweight 子功能：只返回 breath_bundle 的 personal 三槽。

        同一对话内多次需要刷新"我最近做了什么 / 心境如何"时调用此工具，
        节省 token——相当于把 breath_bundle 的第一层单独拆出来。

        返回：
          { "personal": [...至多 3 条 buckets..] }
        三槽含义与 breath_bundle 的 personal 完全一致：
          当下进行 / 近期总结 / 当下感受。
        自然浮现不会 touch。
        """
        pin_dyn_active, pin_dyn_rollup, pin_feel = await self._character_life_pins()
        personal: list[OBBucket] = []
        used_ids: set[str] = set()
        if pin_dyn_active is not None:
            personal.append(
                self._with_fixed_slot_marker(pin_dyn_active, "personal_event_active", "【个人事件·当下进行】")
            )
            used_ids.add(pin_dyn_active.id)
        if pin_dyn_rollup is not None and pin_dyn_rollup.id not in used_ids:
            personal.append(
                self._with_fixed_slot_marker(pin_dyn_rollup, "personal_event_rollup", "【个人事件·近期总结】")
            )
            used_ids.add(pin_dyn_rollup.id)
        if pin_feel is not None and pin_feel.id not in used_ids:
            personal.append(self._with_fixed_slot_marker(pin_feel, "current_feeling", "【当下感受】"))
            used_ids.add(pin_feel.id)
        return {"personal": self.format_buckets(personal)}

    async def breath_bundle(self, *, top_k: int = 8, feel_top_k: int = 3) -> dict[str, Any]:
        """Return the 13-slot startup breath bundle as three grouped lists.

        Layout (固定 13 槽，三组结构)：
          personal   (3)：
            ① 1 条 character_life dynamic·当下进行（source_kind=environment_event_summary
               的固定 bucket character_life_latest_event；缺则取其他 character_life dynamic）
            ② 1 条 character_life dynamic·近期总结（source_kind=environment_life_rollup
               的最近一条聚合事件叙述）
            ③ 1 条 character_life feel·当下感受
          relational (8)：4 条非 character_life dynamic + 4 条 feel（允许至多 2 条 character_life feel 渗透关系侧）
          free       (2)：不限标签/类型的自然浮现；显式排除 pinned/protected/permanent；
                          按 score 取 top 5 后随机 shuffle 取 2，避免高分 bucket 永久霸榜。

        pinned/protected principles 由 get_current_state 注入，这里不重复出现。
        ``top_k`` / ``feel_top_k`` 保留为兼容参数但不再生效（固定 3+8+2=13）。
        """
        pin_dyn_active, pin_dyn_rollup, pin_feel = await self._character_life_pins()
        used_ids: set[str] = set()

        personal: list[OBBucket] = []
        if pin_dyn_active is not None:
            personal.append(
                self._with_fixed_slot_marker(pin_dyn_active, "personal_event_active", "【个人事件·当下进行】")
            )
            used_ids.add(pin_dyn_active.id)
        if pin_dyn_rollup is not None and pin_dyn_rollup.id not in used_ids:
            personal.append(
                self._with_fixed_slot_marker(pin_dyn_rollup, "personal_event_rollup", "【个人事件·近期总结】")
            )
            used_ids.add(pin_dyn_rollup.id)
        if pin_feel is not None and pin_feel.id not in used_ids:
            personal.append(self._with_fixed_slot_marker(pin_feel, "current_feeling", "【当下感受】"))
            used_ids.add(pin_feel.id)

        # Relational dynamics: 4 条非 character_life dynamic。
        # 直接走 _surface_breath 拉混合候选池（含 character_life），绕过 breath() 的
        # 新默认行为（默认排除 character_life）——breath() 默认排除只针对外部关键词检索场景，
        # breath_bundle 是内部启动包装，需要混合候选池供 free 池"自由联想"。
        # relational_dyn 内循环自己会过滤 character_life，不受影响。
        dyn_candidates = await self._surface_breath(
            limit=30, domains=set(), include_core=False
        )
        relational_dyn: list[OBBucket] = []
        for bucket in dyn_candidates:
            if len(relational_dyn) >= 4:
                break
            if bucket.id in used_ids:
                continue
            if self._is_character_life_bucket(bucket):
                continue
            if self._is_raw_environment_life_fragment(bucket):
                continue
            relational_dyn.append(bucket)
            used_ids.add(bucket.id)

        # Relational feel: 4 条 feel。允许至多 2 条 character_life feel 渗透关系侧——
        # personal 槽已经占了最新那条，再让 2 条更早的 character_life feel 进入关系侧浮现，
        # 配合 _feel_breath 的一周保护窗，对 dream 已 crystallize 的近期感受也能再上来。
        feel_pool = await self._feel_breath(limit=20, max_character_life=2)
        relational_feel: list[OBBucket] = []
        for bucket in feel_pool:
            if len(relational_feel) >= 4:
                break
            if bucket.id in used_ids:
                continue
            relational_feel.append(bucket)
            used_ids.add(bucket.id)

        relational = relational_dyn + relational_feel

        # Free: 2 条自由浮现；防御 pinned/protected/permanent；top 5 score 后 shuffle 取 2。
        free_pool: list[OBBucket] = []
        seen_pool_ids: set[str] = set()
        for bucket in dyn_candidates:
            if bucket.id in used_ids or bucket.id in seen_pool_ids:
                continue
            if self._is_raw_environment_life_fragment(bucket):
                continue
            if self._is_protected_or_permanent(bucket):
                continue
            free_pool.append(bucket)
            seen_pool_ids.add(bucket.id)
        feel_extra = await self._feel_breath(limit=12, max_character_life=1)
        for bucket in feel_extra:
            if bucket.id in used_ids or bucket.id in seen_pool_ids:
                continue
            if self._is_protected_or_permanent(bucket):
                continue
            free_pool.append(bucket)
            seen_pool_ids.add(bucket.id)
        free_pool.sort(key=lambda b: float(getattr(b, "score", 0.0) or 0.0), reverse=True)
        top_pool = free_pool[:5]
        random.shuffle(top_pool)
        free: list[OBBucket] = []
        for bucket in top_pool:
            if len(free) >= 2:
                break
            free.append(bucket)
            used_ids.add(bucket.id)

        return {
            "personal":   self.format_buckets(personal),
            "relational": self.format_buckets(relational),
            "free":       self.format_buckets(free),
        }

    def _is_protected_or_permanent(self, bucket: OBBucket) -> bool:
        """Defense for breath_bundle free pool: never let pinned/protected/permanent buckets
        compete in free's score-sort. They get 999 score and would霸榜——defeating the
        "自由联想" purpose of the free slots. They belong to get_current_state, not breath.
        """
        meta = bucket.metadata or {}
        if meta.get("pinned") or meta.get("protected"):
            return True
        return self._bucket_type(meta) == "permanent"

    async def _character_life_pins(
        self,
    ) -> tuple[OBBucket | None, OBBucket | None, OBBucket | None]:
        """Pick three personal-group pins for breath_bundle.

        Returns (pin_dyn_active, pin_dyn_rollup, pin_feel)：
          - pin_dyn_active: 当下进行 — 优先 source_kind=environment_event_summary
            （即固定 bucket character_life_latest_event），缺失则回退到其他
            character_life dynamic（排除 rollup 与 raw fragment）。
          - pin_dyn_rollup: 近期总结 — source_kind=environment_life_rollup
            的最新一条聚合事件叙述。
          - pin_feel: 当下感受 — 最新一条 character_life feel（未 crystallized）。

        pinned/protected 跳过——它们由 get_current_state 注入，非短期记忆。
        raw fragment 始终排除——它们是线索碎片，不算"事件"。
        """
        buckets = await self.list_buckets(include_archive=False)
        active_summary: list[OBBucket] = []   # source_kind=environment_event_summary
        active_other: list[OBBucket] = []     # 其他 character_life dynamic（非 rollup、非 fragment）
        rollup_candidates: list[OBBucket] = []
        feel_candidates: list[OBBucket] = []
        for bucket in buckets:
            if not self._is_character_life_bucket(bucket):
                continue
            meta = bucket.metadata or {}
            if meta.get("pinned") or meta.get("protected"):
                continue
            if self._is_raw_environment_life_fragment(bucket):
                continue
            btype = self._bucket_type(meta)
            source_kind = str(meta.get("source_kind") or "").strip().lower()
            if btype == "dynamic" and not meta.get("resolved"):
                if source_kind == "environment_life_rollup":
                    rollup_candidates.append(bucket)
                elif source_kind == "environment_event_summary":
                    active_summary.append(bucket)
                else:
                    active_other.append(bucket)
            elif btype == "feel" and not meta.get("crystallized"):
                feel_candidates.append(bucket)

        def _latest(items: list[OBBucket]) -> OBBucket | None:
            if not items:
                return None
            items.sort(key=lambda b: str((b.metadata or {}).get("created") or ""), reverse=True)
            return items[0]

        pin_dyn_active = _latest(active_summary) or _latest(active_other)
        pin_dyn_rollup = _latest(rollup_candidates)
        pin_feel = _latest(feel_candidates)
        return pin_dyn_active, pin_dyn_rollup, pin_feel

    @staticmethod
    def _is_raw_environment_life_fragment(bucket: OBBucket) -> bool:
        meta = bucket.metadata or {}
        return str(meta.get("source_kind") or "").strip().lower() == "environment_life_fragment"

    def _with_fixed_slot_marker(self, bucket: OBBucket, slot_role: str, marker: str) -> OBBucket:
        """Tag a pinned-group bucket with a header marker.

        Score is recomputed via calculate_score (i.e. natural decay score) — we do NOT
        force 999. The fixed-slot positioning is provided by breath_bundle's list order,
        not by score. This keeps the bucket's normal decay/activation semantics intact.

        Speculative-about-user 警示：若该 bucket 的 metadata 标了
        ``speculative_about_user=True``（说明 env 生成时 LLM 自评为 speculative，
        含有对用户行为的虚构描写），在 marker 后追加一行 ⚠ 提示，让前台模型
        在对话中主动与用户确认，不要把这段内容当作"已发生事实"使用。
        """
        meta = dict(bucket.metadata or {})
        meta["slot_role"] = slot_role
        display_marker = marker
        if meta.get("speculative_about_user"):
            display_marker = f"{marker} ⚠ 含关于泳琳的推测内容，请在对话中确认后再当作事实使用"
        content = str(bucket.content or "").strip()
        if content and not content.startswith(display_marker):
            content = f"{display_marker}\n{content}"
        natural_score = self.calculate_score(meta)
        return OBBucket(
            id=bucket.id,
            content=content,
            metadata=meta,
            path=bucket.path,
            score=round(float(natural_score or 0.0), 2),
        )

    async def recent_pinned(self, *, limit: int = 5, include_archive: bool = False) -> list[OBBucket]:
        buckets = [
            bucket for bucket in await self.list_buckets(include_archive=include_archive)
            if bucket.metadata.get("pinned")
        ]
        buckets.sort(key=lambda b: str(b.metadata.get("created") or ""), reverse=True)
        for bucket in buckets:
            bucket.score = self.calculate_score(bucket.metadata)
        return buckets[: max(1, min(50, int(limit or 5)))]

    def effective_role(self, meta: dict) -> str | None:
        """Resolve the stable-layer role of a permanent bucket.

        Returns None for non-permanent buckets. For permanent buckets, an
        explicit valid `role` wins; otherwise the legacy fallback applies
        (spec §2.4): pinned/protected → evolving_principle; bare permanent →
        anchor. `pinned`/`protected` are kept for compatibility and never
        removed by this resolution.
        """
        if not isinstance(meta, dict):
            return None
        if self._bucket_type(meta) != "permanent":
            return None
        explicit = str(meta.get("role") or "").strip()
        if explicit in self.PERMANENT_ROLES:
            return explicit
        if meta.get("pinned") or meta.get("protected"):
            return self.ROLE_EVOLVING
        return self.ROLE_ANCHOR

    async def list_injectable_principles(self) -> dict[str, list[OBBucket]]:
        """Group permanent buckets for get_current_state injection by role.

        Returns {"standing": [...all standing_invariant...],
                 "evolving": [...newest EVOLVING_PRINCIPLE_INJECT_LIMIT...]}.
        anchor-role buckets are intentionally excluded (recall-only).
        """
        standing: list[OBBucket] = []
        evolving: list[OBBucket] = []
        for bucket in await self.list_buckets(include_archive=False):
            role = self.effective_role(bucket.metadata)
            if role == self.ROLE_STANDING:
                standing.append(bucket)
            elif role == self.ROLE_EVOLVING:
                evolving.append(bucket)
        key_created = lambda b: str((getattr(b, "metadata", {}) or {}).get("created") or "")
        standing.sort(key=key_created, reverse=True)
        evolving.sort(key=key_created, reverse=True)
        evolving = evolving[: max(1, int(self.EVOLVING_PRINCIPLE_INJECT_LIMIT))]
        for bucket in standing + evolving:
            bucket.score = self.calculate_score(bucket.metadata)
        return {"standing": standing, "evolving": evolving}

    async def _query_breath(
        self,
        query: str,
        *,
        limit: int,
        domains: set[str],
        valence: float | None,
        arousal: float | None,
        include_archive: bool,
        date_window: tuple[datetime | None, datetime | None] | None = None,
        include_character_life: bool = True,
    ) -> list[OBBucket]:
        window = date_window or (None, None)
        buckets = [
            b for b in await self.list_buckets(include_archive=include_archive)
            if self._bucket_type(b.metadata) != "feel"
            and not b.metadata.get("pinned")
            and not b.metadata.get("protected")
        ]
        if domains:
            buckets = [b for b in buckets if self._matches_domains(b, domains)]
        if window != (None, None):
            buckets = [b for b in buckets if self._bucket_in_date_window(b, window)]
        if not include_character_life:
            buckets = [b for b in buckets if not self._is_character_life_bucket(b)]

        vector_scores: dict[str, float] = {}
        if self.embedding_store is not None:
            try:
                hits = await self.embedding_store.search_similar(query, top_k=max(50, limit * 4))
                vector_scores = {str(bucket_id): float(sim) for bucket_id, sim in hits}
            except Exception:
                logger.exception("OB embedding search failed; falling back to lexical search.")

        scored: list[OBBucket] = []
        now = datetime.utcnow()
        for bucket in buckets:
            score = self._search_score(bucket, query, valence, arousal, now)
            if bucket.id in vector_scores and vector_scores[bucket.id] > 0.5:
                score = max(score, vector_scores[bucket.id] * 100.0)
            if score < 18:
                continue
            bucket.score = round(score, 2)
            scored.append(bucket)

        scored.sort(key=lambda b: b.score, reverse=True)
        return scored[:limit]

    async def grow(
        self,
        content: str,
        *,
        query: str = "",
        domain: str | list[str] | None = None,
        importance: int | None = None,
        valence: float | None = None,
        arousal: float | None = None,
    ) -> str:
        matches = await self.breath(
            query or content,
            limit=1,
            domain=domain,
            valence=valence,
            arousal=arousal,
        )
        if not matches or matches[0].score < 75:
            return await self.hold(
                content,
                domain=self._domain_list(domain) or None,
                importance=importance or 5,
                valence=valence if valence is not None else 0.5,
                arousal=arousal if arousal is not None else 0.3,
            )
        bucket = matches[0]
        if bucket.metadata.get("pinned") or bucket.metadata.get("protected") or self._bucket_type(bucket.metadata) == "feel":
            return await self.hold(
                content,
                domain=self._domain_list(domain) or None,
                importance=importance or 5,
                valence=valence if valence is not None else 0.5,
                arousal=arousal if arousal is not None else 0.3,
            )
        merged = f"{bucket.content.rstrip()}\n\n---\n{str(content or '').strip()}"
        updates: dict[str, Any] = {"content": merged}
        if importance is not None:
            updates["importance"] = max(int(bucket.metadata.get("importance", 5)), int(importance))
        if valence is not None:
            old = self._clamp_float(bucket.metadata.get("valence"), 0, 1, 0.5)
            updates["valence"] = round((old + self._clamp_float(valence, 0, 1, 0.5)) / 2, 2)
        if arousal is not None:
            old = self._clamp_float(bucket.metadata.get("arousal"), 0, 1, 0.3)
            updates["arousal"] = round((old + self._clamp_float(arousal, 0, 1, 0.3)) / 2, 2)
        await self.update(bucket.id, **updates)
        return bucket.id

    async def dream(self, *, limit: int = 10, scope: str = "relational") -> dict[str, Any]:
        normalized_scope = self._normalize_scope(scope)
        buckets = await self.list_buckets(include_archive=False)
        scoped_buckets = self._filter_by_scope(buckets, normalized_scope)
        candidates = [bucket for bucket in scoped_buckets if self._is_dream_candidate(bucket)]
        candidates.sort(key=lambda b: str(b.metadata.get("created") or ""), reverse=True)
        recent = candidates[: max(1, min(30, int(limit or 10)))]
        connection_hint = await self._dream_connection_hint(recent)
        crystal_hint = await self._dream_crystal_hint(scoped_buckets, scope=normalized_scope)
        return {
            "text": self._dream_text(recent, connection_hint=connection_hint, crystal_hint=crystal_hint),
            "items": self.format_buckets(recent),
            "connection_hint": connection_hint,
            "crystal_hint": crystal_hint,
            "scope": normalized_scope,
        }

    async def feel_crystals(
        self,
        *,
        limit: int = 3,
        max_items_per_cluster: int = 5,
        min_cluster_size: int = 3,
        min_similarity: float = 0.7,
        cursor: str = "",
        include_crystallized: bool = False,
        scope: str = "relational",
    ) -> dict[str, Any]:
        limit = max(1, min(20, int(limit or 3)))
        max_items = max(1, min(20, int(max_items_per_cluster or 5)))
        min_size = max(2, min(20, int(min_cluster_size or 3)))
        threshold = max(0.0, min(1.0, float(min_similarity or 0.7)))
        normalized_scope = self._normalize_scope(scope)
        offset = self._cursor_offset(cursor)
        clusters = await self._feel_clusters(
            min_cluster_size=min_size,
            min_similarity=threshold,
            include_crystallized=include_crystallized,
            scope=normalized_scope,
        )
        page = clusters[offset : offset + limit]
        formatted = [
            self._format_feel_cluster(cluster, max_items_per_cluster=max_items)
            for cluster in page
        ]
        for item in formatted:
            item["cursor_snapshot"] = str(offset) if offset else ""
        next_offset = offset + len(page)
        has_more = next_offset < len(clusters)
        return {
            "clusters": formatted,
            "limit": limit,
            "max_items_per_cluster": max_items,
            "min_cluster_size": min_size,
            "min_similarity": threshold,
            "include_crystallized": bool(include_crystallized),
            "scope": normalized_scope,
            "cursor": str(offset) if offset else "",
            "cursor_snapshot": str(offset) if offset else "",
            "next_cursor": str(next_offset) if has_more else "",
            "has_more": has_more,
            "total_clusters": len(clusters),
            "diagnostics": {
                "embedding_enabled": self.embedding_store is not None,
                "message": "" if self.embedding_store is not None else "OB embedding store is not initialized; cannot cluster feels.",
            },
        }

    async def crystallize_feel(
        self,
        *,
        mode: str,
        principle_content: str = "",
        principle_title: str = "",
        principle_card: dict[str, Any] | None = None,
        principle_injection: str = "",
        feel_content: str = "",
        domain: list[str] | None = None,
        feel_ids: list[str] | None = None,
        cluster_id: str = "",
        include_all: bool = False,
        extra_targets: list[str] | None = None,
        min_cluster_size: int = 3,
        min_similarity: float = 0.7,
        scope: str = "relational",
    ) -> dict[str, Any]:
        normalized_mode = str(mode or "").strip().lower()
        if normalized_mode not in {"principle", "thread", "both", "feel"}:
            raise ValueError("mode must be principle, thread, both, or feel")
        normalized_scope = self._normalize_scope(scope)
        source_ids = [self._safe_id(item) for item in (feel_ids or []) if str(item or "").strip()]
        if cluster_id and include_all:
            cluster = await self._get_feel_cluster_by_id(
                cluster_id,
                min_cluster_size=min_cluster_size,
                min_similarity=min_similarity,
                scope=normalized_scope,
            )
            if cluster:
                source_ids = list(cluster["ids"])
        source_ids = list(dict.fromkeys(source_ids))
        sources = [bucket for bucket in [await self.get(bid) for bid in source_ids] if bucket]
        sources = [bucket for bucket in sources if self._bucket_type(bucket.metadata) == "feel"]
        now = datetime.utcnow().isoformat()
        created_ids: dict[str, str] = {}
        signature = self._crystal_signature(
            mode=normalized_mode,
            source_ids=[bucket.id for bucket in sources],
            principle_content=principle_content,
            principle_title=principle_title,
            principle_card=principle_card or {},
            principle_injection=principle_injection,
            feel_content=feel_content,
            domain=domain or [],
        )
        existing_by_kind = await self._find_existing_crystal_outputs(signature)

        if normalized_mode in {"principle", "both"}:
            existing_id = existing_by_kind.get("principle_bucket_id")
            if existing_id:
                created_ids["principle_bucket_id"] = existing_id
            else:
                content = self._format_principle_card_content(
                    principle_content=principle_content,
                    principle_title=principle_title,
                    principle_card=principle_card,
                    principle_injection=principle_injection,
                )
                if not content:
                    content = self._default_crystal_content(sources)
                if content:
                    created_ids["principle_bucket_id"] = await self.hold(
                        content,
                        tags=["feel_crystal", "principle"],
                        importance=10,
                        domain=domain or ["core"],
                        bucket_type="permanent",
                        pinned=True,
                        protected=False,
                        name=str(principle_title or "").strip() or "feel crystal principle",
                        extra_metadata={
                            "source_kind": "feel_crystal",
                            "crystal_signature": signature,
                            "crystal_output": "principle",
                            "principle_title": str(principle_title or "").strip(),
                            "principle_card": principle_card or {},
                            "principle_injection": str(principle_injection or "").strip(),
                        },
                    )

        if normalized_mode == "feel":
            existing_id = existing_by_kind.get("feel_bucket_id")
            if existing_id:
                created_ids["feel_bucket_id"] = existing_id
            else:
                content = str(feel_content or "").strip() or self._default_crystal_content(sources)
                if content:
                    created_ids["feel_bucket_id"] = await self.hold(
                        content,
                        tags=["feel_crystal", "condensed"],
                        importance=6,
                        domain=[],
                        bucket_type="feel",
                        name="condensed feel",
                        extra_metadata={
                            "source_kind": "feel_crystal",
                            "crystal_signature": signature,
                            "crystal_output": "feel",
                        },
                    )

        targets = [value for value in created_ids.values() if value]
        targets.extend(str(value) for value in (extra_targets or []) if str(value or "").strip())
        targets = list(dict.fromkeys(targets))
        if normalized_mode == "thread" and not targets:
            targets.append("key_record:pending")
        for bucket in sources:
            await self.update(
                bucket.id,
                crystallized=True,
                digested=True,
                crystallized_mode=normalized_mode,
                crystallized_into=targets,
                crystallized_at=now,
                crystal_signature=signature,
            )
        return {
            "mode": normalized_mode,
            "source_feel_ids": [bucket.id for bucket in sources],
            **created_ids,
            "marked_count": len(sources),
            "deduped": bool(existing_by_kind),
        }

    async def feel_cluster_sources(
        self,
        *,
        cluster_id: str = "",
        feel_ids: list[str] | None = None,
        include_all: bool = False,
        min_cluster_size: int = 3,
        min_similarity: float = 0.7,
        include_crystallized: bool = False,
    ) -> list[OBBucket]:
        source_ids = [self._safe_id(item) for item in (feel_ids or []) if str(item or "").strip()]
        if cluster_id and include_all:
            cluster = await self._get_feel_cluster_by_id(
                cluster_id,
                min_cluster_size=min_cluster_size,
                min_similarity=min_similarity,
                include_crystallized=include_crystallized,
            )
            if cluster:
                source_ids = list(cluster["ids"])
        source_ids = list(dict.fromkeys(source_ids))
        sources = [bucket for bucket in [await self.get(bid) for bid in source_ids] if bucket]
        return [bucket for bucket in sources if self._bucket_type(bucket.metadata) == "feel"]

    async def uncrystallize_feel(
        self,
        *,
        cluster_id: str = "",
        feel_ids: list[str] | None = None,
        include_all: bool = False,
        min_cluster_size: int = 3,
        min_similarity: float = 0.7,
    ) -> dict[str, Any]:
        sources = await self.feel_cluster_sources(
            cluster_id=cluster_id,
            feel_ids=feel_ids,
            include_all=include_all,
            min_cluster_size=min_cluster_size,
            min_similarity=min_similarity,
            include_crystallized=True,
        )
        restored: list[str] = []
        for bucket in sources:
            await self.update(
                bucket.id,
                crystallized=False,
                digested=False,
                crystallized_mode="",
                crystallized_into=[],
                crystallized_at="",
                crystal_signature="",
            )
            restored.append(bucket.id)
        return {"restored_count": len(restored), "source_feel_ids": restored}

    async def resolve(self, bucket_id: str, *, reason: str = "") -> bool:
        updates: dict[str, Any] = {
            "resolved": True,
            "resolved_at": datetime.utcnow().isoformat(),
        }
        if str(reason or "").strip():
            updates["resolved_reason"] = str(reason).strip()
        return await self.update(bucket_id, **updates)

    async def trace(self, bucket_id: str, **updates: Any) -> OBBucket | None:
        if updates.pop("delete", False):
            ok = await self.delete(bucket_id)
            return None if ok else await self.get(bucket_id)
        clean = {k: v for k, v in updates.items() if v is not None}
        if clean:
            ok = await self.update(bucket_id, **clean)
            if not ok:
                return None
        return await self.get(bucket_id)

    async def get(self, bucket_id: str) -> OBBucket | None:
        path = self._find_bucket_file(bucket_id)
        return self._load_bucket(path) if path else None

    async def touch(self, bucket_id: str) -> bool:
        bucket = await self.get(bucket_id)
        if not bucket:
            return False
        count = float(bucket.metadata.get("activation_count", 0) or 0)
        ok = await self.update(
            bucket_id,
            last_active=datetime.utcnow().isoformat(),
            activation_count=round(count + 1, 1),
        )
        if ok:
            await self._time_ripple(bucket)
        return ok

    async def _time_ripple(self, source: OBBucket, *, hours: float = 48.0) -> None:
        reference_time = self._parse_dt(source.metadata.get("last_active") or source.metadata.get("created"))
        if reference_time is None:
            return
        candidates = []
        for bucket in await self.list_buckets(include_archive=False):
            if bucket.id == source.id:
                continue
            meta = bucket.metadata
            if meta.get("pinned") or meta.get("protected") or self._bucket_type(meta) in {"permanent", "feel"}:
                continue
            other_time = self._parse_dt(meta.get("last_active") or meta.get("created"))
            if other_time is None:
                continue
            delta_hours = abs((reference_time - other_time).total_seconds()) / 3600.0
            if delta_hours <= hours:
                candidates.append((delta_hours, bucket))
        candidates.sort(key=lambda item: item[0])
        for _, bucket in candidates[:5]:
            count = float(bucket.metadata.get("activation_count", 0) or 0)
            await self.update(bucket.id, activation_count=round(count + 0.3, 1))

    async def update(self, bucket_id: str, **updates: Any) -> bool:
        bucket = await self.get(bucket_id)
        if not bucket:
            return False
        content_changed = "content" in updates
        content = str(updates.pop("content", bucket.content))
        metadata = dict(bucket.metadata)
        metadata.update({k: v for k, v in updates.items() if v is not None})
        metadata["id"] = self._safe_id(str(metadata.get("id") or bucket_id))
        metadata["type"] = self._normalize_bucket_type(str(metadata.get("type") or "dynamic"))

        if metadata.get("pinned"):
            metadata["type"] = "permanent"
            metadata["importance"] = 10
        if metadata.get("protected"):
            metadata["importance"] = 10
        if metadata["type"] == "feel":
            metadata["domain"] = metadata.get("domain") if metadata.get("domain") is not None else []
        elif not metadata.get("domain"):
            metadata["domain"] = ["未分类"]

        old_path = Path(bucket.path)
        new_path = self._path_for(metadata)
        new_path.parent.mkdir(parents=True, exist_ok=True)
        new_path.write_text(self._dump_bucket(metadata, content), encoding="utf-8")
        if old_path != new_path and old_path.exists():
            try:
                old_path.unlink()
            except PermissionError:
                # Some Windows/sandboxed environments deny deletion. Keep the
                # stale path semantically consistent so readers do not see an
                # active duplicate; diagnostics can clean duplicates later.
                old_path.write_text(self._dump_bucket(metadata, content), encoding="utf-8")
        if content_changed:
            self._schedule_embedding_upsert(bucket_id, content)
        return True

    async def archive(self, bucket_id: str) -> bool:
        return await self.update(bucket_id, type="archived", pinned=False)

    async def restore(self, bucket_id: str, *, target_type: str = "dynamic") -> bool:
        target = self._normalize_bucket_type(target_type)
        if target == "archived":
            target = "dynamic"
        return await self.update(bucket_id, type=target, pinned=False)

    async def delete(self, bucket_id: str) -> bool:
        path = self._find_bucket_file(bucket_id)
        if not path:
            return False
        path.unlink()
        self.delete_embedding(bucket_id)
        return True

    def delete_embedding(self, bucket_id: str) -> None:
        if self.embedding_store is None:
            return
        try:
            self.embedding_store.delete(bucket_id)
        except Exception:
            logger.exception("OB embedding delete failed for %s", bucket_id)

    async def list_buckets(self, *, include_archive: bool = False) -> list[OBBucket]:
        roots = [self.permanent_dir, self.dynamic_dir, self.feel_dir]
        if include_archive:
            roots.append(self.archive_dir)
        buckets: list[OBBucket] = []
        for root in roots:
            if not root.exists():
                continue
            for path in root.rglob("*.md"):
                bucket = self._load_bucket(path)
                if bucket:
                    if not include_archive and self._bucket_type(bucket.metadata) == "archived":
                        continue
                    buckets.append(bucket)
        return buckets

    async def stats(self) -> dict[str, Any]:
        buckets = await self.list_buckets(include_archive=True)
        by_type: dict[str, int] = {}
        by_domain: dict[str, int] = {}
        for bucket in buckets:
            btype = self._bucket_type(bucket.metadata)
            by_type[btype] = by_type.get(btype, 0) + 1
            domains = bucket.metadata.get("domain", [])
            if not domains:
                domains = ["沉淀物"] if btype == "feel" else ["未分类"]
            for domain in domains:
                key = str(domain)
                by_domain[key] = by_domain.get(key, 0) + 1
        return {"total": len(buckets), "by_type": by_type, "by_domain": by_domain}

    async def pulse(self, *, include_archive: bool = False) -> dict[str, Any]:
        buckets = await self.list_buckets(include_archive=include_archive)
        items = []
        for bucket in buckets:
            bucket.score = self.calculate_score(bucket.metadata)
            items.append(self.format_buckets([bucket])[0])
        items.sort(key=lambda item: float(item.get("score") or 0), reverse=True)
        return {"stats": await self.stats(), "items": items}

    async def diagnostics(self) -> dict[str, Any]:
        buckets = await self.list_buckets(include_archive=True)
        feels = [b for b in buckets if self._bucket_type(b.metadata) == "feel"]
        active = [b for b in buckets if self._bucket_type(b.metadata) not in {"archived", "feel", "permanent"}]
        dream_candidates = [b for b in buckets if self._is_dream_candidate(b)]
        low_score = [
            {"id": b.id, "score": self.calculate_score(b.metadata), "name": b.metadata.get("name")}
            for b in active
            if self.calculate_score(b.metadata) < 0.3
        ]
        old_archive_type = [b.id for b in buckets if str(b.metadata.get("type") or "").lower() == "archive"]
        missing_source = [b.id for b in feels if not str(b.metadata.get("source_bucket") or "").strip()]
        mojibake = [
            b.id for b in buckets
            if any(token in (b.content + " " + str(b.metadata)) for token in ("锛", "鈥", "鏈", "娌"))
        ]
        return {
            "stats": await self.stats(),
            "dream_candidates": len(dream_candidates),
            "feel_total": len(feels),
            "feel_without_source": len(missing_source),
            "feel_without_source_ids": missing_source[:50],
            "legacy_archive_type_ids": old_archive_type[:50],
            "low_score_archive_candidates": low_score[:50],
            "mojibake_suspect_ids": mojibake[:50],
        }

    def format_buckets(self, buckets: list[OBBucket]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for bucket in buckets:
            meta = dict(bucket.metadata)
            out.append(
                {
                    "id": bucket.id,
                    "score": bucket.score,
                    "content": bucket.content,
                    "metadata": meta,
                    "path": bucket.path,
                }
            )
        return out

    def calculate_score(self, metadata: dict[str, Any]) -> float:
        if not isinstance(metadata, dict):
            return 0.0
        if metadata.get("pinned") or metadata.get("protected"):
            return 999.0
        bucket_type = self._bucket_type(metadata)
        if bucket_type == "permanent":
            return 999.0
        if bucket_type == "feel":
            # Gentle aging from the historic flat 50. NOTE: breath surfacing
            # (_feel_breath) overrides bucket.score back to 50.0 at selection
            # time and orders feel by recency, so this score only governs the
            # background decay/archive lifecycle — it does NOT change which feel
            # surfaces. Crystallized feel sinks faster (its essence has been
            # captured into a principle/key_record).
            days_since = self._days_since(metadata.get("last_active") or metadata.get("created"), datetime.utcnow())
            feel_score = 50.0 * math.exp(-float(self.feel_decay_lambda) * days_since)
            if metadata.get("crystallized"):
                feel_score *= 0.1
            return round(feel_score, 4)
        if bucket_type == "archived":
            return 0.0

        importance = max(1.0, min(10.0, float(metadata.get("importance", 5) or 5)))
        activation_count = max(1.0, float(metadata.get("activation_count", 1) or 1))
        arousal = self._clamp_float(metadata.get("arousal"), 0.0, 1.0, 0.3)
        days_since = self._days_since(metadata.get("last_active") or metadata.get("created"), datetime.utcnow())
        time_weight = self._calc_time_weight(days_since)
        emotion_weight = float(self.decay_emotion_base) + float(self.decay_arousal_boost) * arousal
        combined_weight = (time_weight * 0.7 + emotion_weight * 0.3) if days_since <= 3 else emotion_weight
        base_score = importance * (activation_count ** 0.3) * math.exp(-float(self.decay_lambda) * days_since) * combined_weight
        if metadata.get("resolved") and metadata.get("digested"):
            base_score *= 0.02
        elif metadata.get("resolved"):
            base_score *= 0.05
        if arousal > 0.7 and not metadata.get("resolved"):
            base_score *= 1.5
        return round(base_score, 4)

    @staticmethod
    def _calc_time_weight(days_since: float) -> float:
        return 1.0 + math.exp(-(max(days_since, 0.0) * 24.0) / 36.0)

    async def breath_debug(
        self,
        *,
        query: str = "",
        domain: str | list[str] | None = None,
        valence: float | None = None,
        arousal: float | None = None,
        include_archive: bool = False,
    ) -> dict[str, Any]:
        buckets = await self.list_buckets(include_archive=include_archive)
        domains = self._domain_filter(domain)
        now = datetime.utcnow()
        rows = []
        for bucket in buckets:
            if domains and not self._matches_domains(bucket, domains):
                continue
            meta = bucket.metadata
            topic = self._topic_score(query, bucket) if query else 0.0
            emotion = self._emotion_score(valence, arousal, meta)
            time_score = self._time_score(meta, now)
            importance = max(1, min(10, int(meta.get("importance", 5) or 5))) / 10.0
            search_score = self._search_score(bucket, query, valence, arousal, now) if query else 0.0
            decay_score = self.calculate_score(meta)
            rows.append({
                "id": bucket.id,
                "name": meta.get("name") or bucket.id,
                "type": self._bucket_type(meta),
                "domain": meta.get("domain", []),
                "resolved": bool(meta.get("resolved")),
                "digested": bool(meta.get("digested")),
                "pinned": bool(meta.get("pinned")),
                "protected": bool(meta.get("protected")),
                "topic": round(topic, 4),
                "emotion": round(emotion, 4),
                "time": round(time_score, 4),
                "importance": round(importance, 4),
                "search_score": round(search_score, 2),
                "decay_score": decay_score,
                "passed_threshold": search_score >= 18 if query else decay_score > 0,
            })
        rows.sort(key=lambda item: float(item["search_score"] if query else item["decay_score"]), reverse=True)
        return {
            "query": query,
            "weights": {"topic": 4.0, "emotion": 2.0, "time": 1.5, "importance": 1.0},
            "threshold": 18 if query else 0,
            "items": rows,
        }

    def _schedule_embedding_upsert(self, bucket_id: str, content: str) -> None:
        if self.embedding_store is None:
            return

        async def _run() -> None:
            try:
                await self.embedding_store.upsert(bucket_id, content)
            except Exception:
                logger.exception("OB embedding upsert failed for %s", bucket_id)

        try:
            asyncio.get_running_loop().create_task(_run())
        except RuntimeError:
            logger.debug("Skipping async OB embedding upsert outside a running loop.")

    def _is_dream_candidate(self, bucket: OBBucket) -> bool:
        meta = bucket.metadata or {}
        if self._bucket_type(meta) != "dynamic":
            return False
        if meta.get("pinned") or meta.get("protected"):
            return False
        if meta.get("resolved") or meta.get("digested"):
            return False
        return True

    def _dream_text(self, recent: list[OBBucket], *, connection_hint: str = "", crystal_hint: str = "") -> str:
        if not recent:
            return "没有需要消化的新记忆。"
        header = (
            "=== Dreaming ===\n"
            "以下是最近还没有消化的动态记忆。请用第一人称想一想：\n"
            "- 哪些仍然在我这里留下了重量？\n"
            "- 哪些还没想清楚？\n"
            "- 哪些可以放下？\n"
            "想完之后：值得放下的，用 resolve_bucket(bucket_id, reason=\"...\")；"
            "有沉淀的，用 hold_feel(content=\"...\", source_bucket=\"bucket_id\", valence=你的感受)；"
            "如果末尾出现结晶提示，调用 feel_crystals(...) / crystallize_feel(...)。"
            "resolve_bucket 是放下已经被沉淀、理解或转写的 dynamic 源事件，不是删除或归档。\n\n"
        )
        parts: list[str] = []
        for bucket in recent:
            meta = bucket.metadata or {}
            domains = ", ".join(str(d) for d in meta.get("domain", []) if str(d).strip()) or "无"
            valence = self._clamp_float(meta.get("valence"), 0.0, 1.0, 0.5)
            arousal = self._clamp_float(meta.get("arousal"), 0.0, 1.0, 0.3)
            content = self._strip_wikilinks(str(bucket.content or ""))[:1000]
            parts.append(
                f"[{meta.get('name') or bucket.id}] [未解决] "
                f"主题:{domains} V{valence:.1f}/A{arousal:.1f} 创建:{meta.get('created', '')}\n"
                f"ID: {bucket.id}\n{content}"
            )
        hints = ""
        if connection_hint:
            hints += f"\n\n{connection_hint}"
        if crystal_hint:
            hints += f"\n\n{crystal_hint}"
        return header + "\n---\n".join(parts) + hints

    async def _dream_connection_hint(self, recent: list[OBBucket]) -> str:
        if self.embedding_store is None or len(recent) < 2:
            return ""
        try:
            embeddings = {bucket.id: self.embedding_store.get(bucket.id) for bucket in recent}
            embeddings = {bid: emb for bid, emb in embeddings.items() if emb}
            best_pair: tuple[str, str] | None = None
            best_sim = 0.0
            ids = list(embeddings)
            for idx, id_a in enumerate(ids):
                for id_b in ids[idx + 1:]:
                    sim = self._cosine_similarity(embeddings[id_a], embeddings[id_b])
                    if sim > best_sim:
                        best_sim = sim
                        best_pair = (id_a, id_b)
            if not best_pair or best_sim <= 0.5:
                return ""
            names = {bucket.id: str(bucket.metadata.get("name") or bucket.id) for bucket in recent}
            return (
                f"连接提示：[{names.get(best_pair[0], best_pair[0])}] 和 "
                f"[{names.get(best_pair[1], best_pair[1])}] 似乎有关联"
                f"（相似度 {best_sim:.2f}）。这不是结论，只是提醒我自己想一想。"
            )
        except Exception:
            logger.exception("OB dream connection hint failed")
            return ""

    async def _dream_crystal_hint(self, buckets: list[OBBucket], *, scope: str = "relational") -> str:
        if self.embedding_store is None:
            return ""
        feels = [
            bucket for bucket in buckets
            if self._bucket_type(bucket.metadata) == "feel"
            and not bucket.metadata.get("pinned")
        ]
        feels = self._filter_by_scope(feels, scope)
        if len(feels) < 3:
            return ""
        try:
            embeddings = {bucket.id: self.embedding_store.get(bucket.id) for bucket in feels}
            embeddings = {bid: emb for bid, emb in embeddings.items() if emb}
            for bucket in feels:
                emb = embeddings.get(bucket.id)
                if not emb:
                    continue
                similar = [
                    other_id for other_id, other_emb in embeddings.items()
                    if other_id != bucket.id and self._cosine_similarity(emb, other_emb) > 0.7
                ]
                if len(similar) >= 2:
                    preview = self._strip_wikilinks(bucket.content)[:90]
                    return (
                        f"结晶提示：我已经写过 {len(similar) + 1} 条相似的 feel"
                        f"（围绕“{preview}...”）。如果它已经从感受变成稳定认知，"
                        "请先调用 feel_crystals(...) 查看相似 feel，再用 crystallize_feel(...) "
                        "结晶为 OB principle、key_record、both 或浓缩为普通 feel。"
                    )
        except Exception:
            logger.exception("OB dream crystal hint failed")
        return ""

    async def _feel_clusters(
        self,
        *,
        min_cluster_size: int,
        min_similarity: float,
        include_crystallized: bool = False,
        scope: str = "relational",
    ) -> list[dict[str, Any]]:
        if self.embedding_store is None:
            return []
        feels = [
            bucket for bucket in await self.list_buckets(include_archive=False)
            if self._bucket_type(bucket.metadata) == "feel"
            and (include_crystallized or not bucket.metadata.get("crystallized"))
        ]
        feels = self._filter_by_scope(feels, scope)
        embeddings = {bucket.id: self.embedding_store.get(bucket.id) for bucket in feels}
        embeddings = {bid: emb for bid, emb in embeddings.items() if emb}
        by_id = {bucket.id: bucket for bucket in feels if bucket.id in embeddings}
        ids = sorted(embeddings)
        adjacency: dict[str, set[str]] = {bid: set() for bid in ids}
        edge_scores: dict[tuple[str, str], float] = {}
        for idx, left in enumerate(ids):
            for right in ids[idx + 1:]:
                sim = self._cosine_similarity(embeddings[left], embeddings[right])
                if sim >= min_similarity:
                    adjacency[left].add(right)
                    adjacency[right].add(left)
                    edge_scores[(left, right)] = sim
        seen: set[str] = set()
        clusters: list[dict[str, Any]] = []
        for start in ids:
            if start in seen:
                continue
            stack = [start]
            component: set[str] = set()
            while stack:
                current = stack.pop()
                if current in component:
                    continue
                component.add(current)
                stack.extend(sorted(adjacency.get(current, set()) - component))
            seen |= component
            if len(component) < min_cluster_size:
                continue
            ordered = sorted(
                component,
                key=lambda bid: str(by_id[bid].metadata.get("created") or ""),
                reverse=True,
            )
            sims = [
                score for (a, b), score in edge_scores.items()
                if a in component and b in component
            ]
            avg = sum(sims) / len(sims) if sims else 0.0
            clusters.append({
                "id": self._cluster_id(ordered),
                "ids": ordered,
                "avg_similarity": round(avg, 4),
                "latest_created": str(by_id[ordered[0]].metadata.get("created") or "") if ordered else "",
                "buckets": [by_id[bid] for bid in ordered],
            })
        clusters.sort(key=lambda item: (len(item["ids"]), item["latest_created"]), reverse=True)
        return clusters

    async def _get_feel_cluster_by_id(
        self,
        cluster_id: str,
        *,
        min_cluster_size: int,
        min_similarity: float,
        include_crystallized: bool = False,
        scope: str = "relational",
    ) -> dict[str, Any] | None:
        for cluster in await self._feel_clusters(
            min_cluster_size=min_cluster_size,
            min_similarity=min_similarity,
            include_crystallized=include_crystallized,
            scope=scope,
        ):
            if cluster.get("id") == cluster_id:
                return cluster
        return None

    def _format_feel_cluster(self, cluster: dict[str, Any], *, max_items_per_cluster: int) -> dict[str, Any]:
        buckets: list[OBBucket] = list(cluster.get("buckets") or [])
        shown = buckets[:max_items_per_cluster]
        excerpts = []
        for bucket in shown:
            meta = bucket.metadata or {}
            excerpts.append({
                "id": bucket.id,
                "created": meta.get("created", ""),
                "domain": meta.get("domain", []),
                "name": meta.get("name") or bucket.id,
                "excerpt": self._strip_wikilinks(bucket.content)[:360],
            })
        preview = self._default_crystal_content(buckets)
        return {
            "cluster_id": cluster.get("id"),
            "feel_ids": list(cluster.get("ids") or []),
            "shown_ids": [bucket.id for bucket in shown],
            "hidden_count": max(0, len(buckets) - len(shown)),
            "has_more": len(buckets) > len(shown),
            "avg_similarity": cluster.get("avg_similarity", 0.0),
            "excerpts": excerpts,
            "items": excerpts,
            "suggested_core": preview,
            "suggested_content": preview,
        }

    def _default_crystal_content(self, buckets: list[OBBucket]) -> str:
        snippets = [
            self._strip_wikilinks(bucket.content).strip()
            for bucket in buckets[:3]
            if str(bucket.content or "").strip()
        ]
        if not snippets:
            return ""
        joined = " / ".join(snippet[:120] for snippet in snippets)
        return f"我反复留下的感受指向同一个核心：{joined}"

    def _crystal_signature(
        self,
        *,
        mode: str,
        source_ids: list[str],
        principle_content: str = "",
        principle_title: str = "",
        principle_card: dict[str, Any] | None = None,
        principle_injection: str = "",
        feel_content: str = "",
        domain: list[str] | None = None,
    ) -> str:
        payload = {
            "mode": str(mode or "").strip().lower(),
            "source_ids": sorted(str(item) for item in source_ids if str(item or "").strip()),
            "principle_content": str(principle_content or "").strip(),
            "feel_content": str(feel_content or "").strip(),
            "domain": sorted(str(item) for item in (domain or []) if str(item or "").strip()),
        }
        if str(principle_title or "").strip():
            payload["principle_title"] = str(principle_title or "").strip()
        if principle_card:
            payload["principle_card"] = principle_card
        if str(principle_injection or "").strip():
            payload["principle_injection"] = str(principle_injection or "").strip()
        raw = yaml.safe_dump(payload, allow_unicode=True, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _format_principle_card_content(
        *,
        principle_content: str = "",
        principle_title: str = "",
        principle_card: dict[str, Any] | None = None,
        principle_injection: str = "",
    ) -> str:
        title = str(principle_title or "").strip()
        card = principle_card if isinstance(principle_card, dict) else {}
        principle = str(card.get("principle") or "").strip()
        response_rule = str(card.get("response_rule") or "").strip()
        avoid = str(card.get("avoid") or "").strip()
        anchors = card.get("anchors") or []
        if not isinstance(anchors, list):
            anchors = []
        anchors = [str(item).strip() for item in anchors if str(item).strip()]
        injection = str(principle_injection or "").strip()
        legacy = str(principle_content or "").strip()
        if not any([title, principle, response_rule, avoid, anchors, injection]):
            return legacy
        lines: list[str] = []
        if title:
            lines.append(f"标题：{title}")
            lines.append("")
        if principle:
            lines.append(f"核心原则：{principle}")
        if response_rule:
            lines.append(f"回应规则：{response_rule}")
        if anchors:
            lines.append("锚点：")
            lines.extend(f"- {item}" for item in anchors[:5])
        if avoid:
            lines.append(f"避免：{avoid}")
        if injection:
            lines.append(f"注入摘要：{injection}")
        if legacy and legacy not in "\n".join(lines):
            lines.append("")
            lines.append(f"原始沉淀：{legacy}")
        return "\n".join(lines).strip()

    async def _find_existing_crystal_outputs(self, signature: str) -> dict[str, str]:
        if not signature:
            return {}
        outputs: dict[str, str] = {}
        for bucket in await self.list_buckets(include_archive=False):
            meta = bucket.metadata or {}
            if meta.get("crystal_signature") != signature:
                continue
            output = str(meta.get("crystal_output") or "").strip()
            if output == "principle" and self._bucket_type(meta) == "permanent":
                outputs.setdefault("principle_bucket_id", bucket.id)
            elif output == "feel" and self._bucket_type(meta) == "feel":
                outputs.setdefault("feel_bucket_id", bucket.id)
        return outputs

    @staticmethod
    def _cluster_id(ids: list[str]) -> str:
        raw = "|".join(sorted(ids))
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    @staticmethod
    def _cursor_offset(cursor: str) -> int:
        try:
            return max(0, int(str(cursor or "0").strip() or "0"))
        except Exception:
            return 0

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(float(x) * float(y) for x, y in zip(a, b))
        norm_a = math.sqrt(sum(float(x) * float(x) for x in a))
        norm_b = math.sqrt(sum(float(y) * float(y) for y in b))
        if norm_a <= 0 or norm_b <= 0:
            return 0.0
        return dot / (norm_a * norm_b)

    @staticmethod
    def _strip_wikilinks(text: str) -> str:
        return re.sub(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", r"\1", str(text or ""))

    def _path_for(self, metadata: dict[str, Any]) -> Path:
        bucket_type = self._normalize_bucket_type(str(metadata.get("type") or "dynamic"))
        if metadata.get("pinned"):
            bucket_type = "permanent"
        root = {
            "permanent": self.permanent_dir,
            "archived": self.archive_dir,
            "feel": self.feel_dir,
        }.get(bucket_type, self.dynamic_dir)
        domains = metadata.get("domain") or []
        primary_domain = "沉淀物" if bucket_type == "feel" else (str(domains[0]) if domains else "未分类")
        dirname = self._sanitize_filename(primary_domain)
        name = self._sanitize_filename(str(metadata.get("name") or metadata.get("id") or "bucket"))
        bid = self._safe_id(str(metadata.get("id") or uuid.uuid4().hex[:12]))
        return root / dirname / f"{name}_{bid}.md"

    def _find_bucket_file(self, bucket_id: str) -> Path | None:
        safe_id = self._safe_id(bucket_id)
        for root in (self.permanent_dir, self.dynamic_dir, self.feel_dir, self.archive_dir):
            if not root.exists():
                continue
            for path in root.rglob(f"*{safe_id}.md"):
                bucket = self._load_bucket(path)
                if bucket and bucket.id == safe_id:
                    return path
        return None

    @staticmethod
    def _dump_bucket(metadata: dict[str, Any], content: str) -> str:
        return "---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False) + "---\n" + content.strip() + "\n"

    def _load_bucket(self, path: Path) -> OBBucket | None:
        try:
            raw = path.read_text(encoding="utf-8")
            if raw.startswith("---"):
                _, meta_raw, content = raw.split("---", 2)
                metadata = yaml.safe_load(meta_raw) or {}
            else:
                metadata = {"id": path.stem, "name": path.stem, "domain": ["未分类"], "type": "dynamic"}
                content = raw
            bid = self._safe_id(str(metadata.get("id") or path.stem))
            metadata["id"] = bid
            metadata["type"] = self._normalize_bucket_type(str(metadata.get("type") or "dynamic"))
            return OBBucket(id=bid, content=content.strip(), metadata=metadata, path=str(path))
        except Exception:
            logger.exception("Failed to load OB bucket %s", path)
            return None

    def _search_score(
        self,
        bucket: OBBucket,
        query: str,
        valence: float | None,
        arousal: float | None,
        now: datetime,
    ) -> float:
        meta = bucket.metadata
        topic = self._topic_score(query, bucket)
        emotion = self._emotion_score(valence, arousal, meta)
        time_score = self._time_score(meta, now)
        importance = max(1, min(10, int(meta.get("importance", 5) or 5))) / 10.0
        total = topic * 4.0 + emotion * 2.0 + time_score * 1.5 + importance
        score = total / 8.5 * 100
        if meta.get("resolved"):
            score *= 0.3
        return score

    def _topic_score(self, query: str, bucket: OBBucket) -> float:
        meta = bucket.metadata
        chunks = [
            (str(meta.get("name", "")), 3.0),
            (" ".join(str(d) for d in meta.get("domain", [])), 2.5),
            (" ".join(str(t) for t in meta.get("tags", [])), 2.0),
            (bucket.content[:1200], 1.0),
        ]
        total_weight = sum(weight for _, weight in chunks)
        score = 0.0
        q = query.lower()
        tokens = self._tokens(q)
        for text, weight in chunks:
            t = text.lower()
            if not t:
                continue
            direct = 1.0 if q and q in t else 0.0
            ratio = SequenceMatcher(None, q, t[: max(len(q) * 4, 120)]).ratio() if q else 0.0
            token_hits = sum(1 for token in tokens if token in t)
            token_score = token_hits / max(1, len(tokens))
            score += max(direct, ratio, token_score) * weight
        return score / total_weight if total_weight else 0.0

    @staticmethod
    def _emotion_score(valence: float | None, arousal: float | None, meta: dict[str, Any]) -> float:
        if valence is None or arousal is None:
            return 0.5
        bv = OBClient._clamp_float(meta.get("valence"), 0.0, 1.0, 0.5)
        ba = OBClient._clamp_float(meta.get("arousal"), 0.0, 1.0, 0.3)
        dist = math.sqrt((float(valence) - bv) ** 2 + (float(arousal) - ba) ** 2)
        return max(0.0, 1.0 - dist / math.sqrt(2))

    @staticmethod
    def _time_score(meta: dict[str, Any], now: datetime) -> float:
        days = OBClient._days_since(meta.get("last_active") or meta.get("created"), now)
        return math.exp(-0.02 * min(days, 365))

    @staticmethod
    def _days_since(value: Any, now: datetime) -> float:
        dt = OBClient._parse_dt(value)
        if dt is None:
            return 30.0
        return max((now - dt).total_seconds() / 86400.0, 0.0)

    @staticmethod
    def _parse_dt(value: Any) -> datetime | None:
        try:
            text = str(value or "").replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is not None:
                dt = dt.astimezone().replace(tzinfo=None)
            return dt
        except Exception:
            return None

    @staticmethod
    def _clamp_float(value: Any, low: float, high: float, default: float) -> float:
        try:
            return max(low, min(high, float(value)))
        except Exception:
            return default

    @classmethod
    def _normalize_bucket_type(cls, value: str) -> str:
        raw = str(value or "dynamic").lower()
        if raw in cls.ARCHIVE_TYPES:
            return "archived"
        return raw if raw in {"dynamic", "permanent", "feel"} else "dynamic"

    @classmethod
    def _bucket_type(cls, metadata: dict[str, Any]) -> str:
        return cls._normalize_bucket_type(str((metadata or {}).get("type") or "dynamic"))

    def _matches_domains(self, bucket: OBBucket, domains: set[str]) -> bool:
        meta_domains = {str(d).lower() for d in bucket.metadata.get("domain", [])}
        return bool(meta_domains & domains) or self._bucket_type(bucket.metadata) in domains

    def _is_character_life_bucket(self, bucket: OBBucket) -> bool:
        meta_domains = {str(d).strip().lower() for d in bucket.metadata.get("domain", []) if str(d).strip()}
        return "character_life" in meta_domains

    def _normalize_scope(self, scope: str | None) -> str:
        """Normalize scope into one of {relational, character, all}; default relational.

        relational — 仅关系侧（排除 character_life buckets）
        character  — 仅角色侧（只 character_life buckets）
        all        — 全量（旧行为，兜底）
        """
        value = str(scope or "").strip().lower()
        if value in {"relational", "character", "all"}:
            return value
        return "relational"

    def _filter_by_scope(self, buckets: list[OBBucket], scope: str) -> list[OBBucket]:
        """Filter a bucket list by relational / character / all scope.

        Centralized so dream / feel_crystals / crystallize_feel all behave consistently.
        """
        normalized = self._normalize_scope(scope)
        if normalized == "all":
            return list(buckets)
        if normalized == "character":
            return [b for b in buckets if self._is_character_life_bucket(b)]
        return [b for b in buckets if not self._is_character_life_bucket(b)]

    @staticmethod
    def _sanitize_filename(value: str) -> str:
        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value or "bucket")).strip(" .")
        return cleaned[:80] or "bucket"

    @staticmethod
    def _safe_id(value: str | None) -> str:
        return re.sub(r"[^A-Za-z0-9_-]", "_", str(value or "")).strip("_")[:80] or uuid.uuid4().hex[:12]

    @staticmethod
    def _tokens(value: str) -> list[str]:
        return [t for t in re.split(r"[\s,，。；;、|]+", value.lower()) if t]

    @staticmethod
    def _domain_filter(domain: str | list[str] | None) -> set[str]:
        return {d.lower() for d in OBClient._domain_list(domain)}

    @staticmethod
    def _domain_list(domain: str | list[str] | None) -> list[str]:
        if domain is None:
            return []
        if isinstance(domain, str):
            return [d.strip() for d in re.split(r"[,，|]+", domain) if d.strip()]
        return [str(d).strip() for d in domain if str(d).strip()]
