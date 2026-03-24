from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime

import httpx

from server.database import Database
from server.memory_store import MemoryEntry, MemoryStore


KEY_VECTOR_EMBEDDING_API_BASE = "vector_embedding_api_base"
KEY_VECTOR_EMBEDDING_API_KEY = "vector_embedding_api_key"
KEY_VECTOR_EMBEDDING_MODEL = "vector_embedding_model"
KEY_VECTOR_EMBEDDING_DIM = "vector_embedding_dim"
KEY_VECTOR_EMBEDDING_TIMEOUT = "vector_embedding_timeout_sec"
KEY_VECTOR_SYNC_BATCH = "vector_sync_batch_size"
KEY_VECTOR_SNAPSHOT_DAYS = "vector_snapshot_days_threshold"
KEY_VECTOR_TOP_K = "vector_search_top_k"
KEY_VECTOR_COLD_DAYS = "vector_cold_days_threshold"
KEY_VECTOR_COMPACTION_GROUP = "vector_compaction_group_size"
KEY_VECTOR_COMPACTION_MAX_GROUPS = "vector_compaction_max_groups"


class VectorMemoryStore(MemoryStore):
    """Vector-based memory store with deterministic local fallback embedding."""

    def __init__(self, db: Database):
        self._db = db

    async def store(self, entry_id: str, text: str, metadata: dict) -> str:
        source_type = str(metadata.get("source_type") or "")
        source_id = int(metadata.get("source_id") or 0)
        if not source_type or not source_id:
            if entry_id.startswith("event_"):
                source_type = "event"
                source_id = int(entry_id.split("_", 1)[1])
            elif entry_id.startswith("snapshot_"):
                source_type = "snapshot"
                source_id = int(entry_id.split("_", 1)[1])
            else:
                return ""

        cfg = await self.get_runtime_config()
        vector, provider = await self._embed_text(text, cfg)
        await self._db.upsert_memory_vector(
            entry_id=entry_id,
            source_type=source_type,
            source_id=source_id,
            text_content=text,
            vector_json=json.dumps(vector, ensure_ascii=False),
            vector_dim=len(vector),
            vector_model=cfg["embedding_model"],
            vector_provider=provider,
            tier="warm",
            status="active",
        )
        return entry_id

    async def search(self, query: str, top_k: int = 5) -> list[MemoryEntry]:
        query = (query or "").strip()
        if not query:
            return []

        cfg = await self.get_runtime_config()
        actual_top_k = max(1, int(top_k or cfg["search_top_k"]))
        qvec, _provider = await self._embed_text(query, cfg)
        qvec = self._normalize_vector(qvec)

        scored: list[tuple[float, dict]] = []
        if qvec:
            rows = await self._db.get_active_memory_vectors(limit=5000)
            now = datetime.utcnow()
            event_cache: dict[int, object] = {}
            snapshot_cache: dict[int, object] = {}
            for row in rows:
                vec = self._normalize_vector(self._loads_vector(row.get("vector_json")))
                if len(vec) != len(qvec):
                    continue
                cosine = self._cosine_similarity(qvec, vec)
                if cosine <= 0:
                    continue
                recency = self._recency_boost(row.get("updated_at"), now)
                importance = await self._importance_boost(
                    row=row,
                    event_cache=event_cache,
                    snapshot_cache=snapshot_cache,
                )
                final_score = 0.72 * cosine + 0.18 * recency + 0.10 * importance
                scored.append((final_score, row))

            scored.sort(key=lambda x: x[0], reverse=True)
        selected = scored[:actual_top_k]
        result: list[MemoryEntry] = []
        for score, row in selected:
            entry = await self._entry_from_vector_row(row, score)
            if entry:
                result.append(entry)

        if len(result) < actual_top_k:
            extras = await self._keyword_fallback_entries(
                query=query,
                limit=actual_top_k - len(result),
                excluded_ids={entry.id for entry in result},
            )
            result.extend(extras)

        await self._db.record_memory_recalls([e.id for e in result])
        return result

    async def delete(self, entry_id: str) -> None:
        await self._db.mark_memory_vector_deleted(entry_id)

    async def get_runtime_config(self) -> dict:
        return {
            "embedding_api_base": await self._get_setting(KEY_VECTOR_EMBEDDING_API_BASE, ""),
            "embedding_api_key": await self._get_setting(KEY_VECTOR_EMBEDDING_API_KEY, ""),
            "embedding_model": await self._get_setting(KEY_VECTOR_EMBEDDING_MODEL, "text-embedding-3-small"),
            "embedding_dim": int(await self._get_setting(KEY_VECTOR_EMBEDDING_DIM, "256")),
            "timeout_sec": float(await self._get_setting(KEY_VECTOR_EMBEDDING_TIMEOUT, "15")),
            "sync_batch_size": int(await self._get_setting(KEY_VECTOR_SYNC_BATCH, "200")),
            "snapshot_days_threshold": int(await self._get_setting(KEY_VECTOR_SNAPSHOT_DAYS, "14")),
            "search_top_k": int(await self._get_setting(KEY_VECTOR_TOP_K, "5")),
            "cold_days_threshold": int(await self._get_setting(KEY_VECTOR_COLD_DAYS, "180")),
            "compaction_group_size": int(await self._get_setting(KEY_VECTOR_COMPACTION_GROUP, "8")),
            "compaction_max_groups": int(await self._get_setting(KEY_VECTOR_COMPACTION_MAX_GROUPS, "20")),
        }

    async def update_runtime_config(self, payload: dict):
        allowed = {
            KEY_VECTOR_EMBEDDING_API_BASE,
            KEY_VECTOR_EMBEDDING_API_KEY,
            KEY_VECTOR_EMBEDDING_MODEL,
            KEY_VECTOR_EMBEDDING_DIM,
            KEY_VECTOR_EMBEDDING_TIMEOUT,
            KEY_VECTOR_SYNC_BATCH,
            KEY_VECTOR_SNAPSHOT_DAYS,
            KEY_VECTOR_TOP_K,
            KEY_VECTOR_COLD_DAYS,
            KEY_VECTOR_COMPACTION_GROUP,
            KEY_VECTOR_COMPACTION_MAX_GROUPS,
        }
        for key, value in payload.items():
            if key not in allowed or value is None:
                continue
            await self._db.set_setting(
                key=key,
                value=str(value),
                category="vector",
                description=f"Vector runtime setting: {key}",
            )

    async def sync_eligible_vectors(self) -> dict:
        cfg = await self.get_runtime_config()
        batch_size = max(1, cfg["sync_batch_size"])
        snapshot_days = max(1, cfg["snapshot_days_threshold"])

        pending_events = await self._db.get_events_without_vector(
            limit=batch_size,
            include_archived=True,
        )
        old_snapshots = await self._db.get_snapshots_older_than_days_without_vector(
            days=snapshot_days,
            limit=batch_size,
        )
        event_count = 0
        snapshot_count = 0
        for ev in pending_events:
            if await self.upsert_event_vector(int(ev.id or 0)):
                event_count += 1
        for snap in old_snapshots:
            entry_id = f"snapshot_{snap.id}"
            await self.store(
                entry_id,
                snap.content,
                {
                    "source_type": "snapshot",
                    "source_id": snap.id,
                    "created_at": snap.created_at,
                    "type": snap.type,
                },
            )
            await self._db.mark_snapshot_vectorized(int(snap.id), entry_id)
            snapshot_count += 1
        return {
            "vectorized_events": event_count,
            "vectorized_snapshots": snapshot_count,
            "batch_size": batch_size,
            "snapshot_days_threshold": snapshot_days,
        }

    async def upsert_event_vector(self, event_id: int) -> bool:
        if event_id <= 0:
            return False
        event = await self._db.get_event_by_id(event_id)
        if not event:
            return False
        entry_id = f"event_{event_id}"
        vector_text = self._build_event_vector_text(event)
        await self.store(
            entry_id,
            vector_text,
            {
                "source_type": "event",
                "source_id": event_id,
                "date": event.date,
            },
        )
        await self._db.mark_event_vectorized(event_id, entry_id)
        return True

    async def get_vector_stats(self) -> dict:
        total = await self._db.count_memory_vectors()
        active = await self._db.count_memory_vectors(status="active")
        by_source = await self._db.count_memory_vectors_by_source()
        return {
            "total": total,
            "active": active,
            "deleted": max(total - active, 0),
            "by_source": by_source,
        }

    async def list_vectors(
        self,
        offset: int = 0,
        limit: int = 50,
        source_type: str | None = None,
        status: str | None = "active",
        tier: str | None = None,
    ) -> list[dict]:
        return await self._db.list_memory_vectors(
            offset=offset,
            limit=limit,
            source_type=source_type,
            status=status,
            tier=tier,
        )

    async def remove_vector(self, entry_id: str) -> bool:
        row = await self._db.get_memory_vector(entry_id)
        if not row:
            return False
        await self._db.mark_memory_vector_deleted(entry_id)
        if entry_id.startswith("event_"):
            await self._db.clear_event_vectorized(int(entry_id.split("_", 1)[1]))
        elif entry_id.startswith("snapshot_"):
            await self._db.clear_snapshot_vectorized(int(entry_id.split("_", 1)[1]))
        return True

    async def reindex_all_vectors(self) -> dict:
        await self._db.conn.execute("DELETE FROM memory_vectors")
        await self._db.conn.commit()
        await self._db.conn.execute("UPDATE event_anchors SET embedding_vector_id = NULL")
        await self._db.conn.execute("UPDATE state_snapshots SET embedding_vector_id = NULL")
        await self._db.conn.commit()
        return await self.sync_eligible_vectors()

    async def compact_cold_memories(self, dry_run: bool = False) -> dict:
        cfg = await self.get_runtime_config()
        cold_days = max(30, cfg["cold_days_threshold"])
        group_size = max(4, cfg["compaction_group_size"])
        max_groups = max(1, cfg["compaction_max_groups"])
        candidates = await self._db.get_active_memory_vectors_older_than_days(
            days=cold_days,
            limit=max_groups * group_size * 3,
        )
        grouped = self._group_old_vectors(candidates, group_size=group_size, max_groups=max_groups)
        if dry_run:
            return {
                "dry_run": True,
                "cold_days_threshold": cold_days,
                "group_size": group_size,
                "group_count": len(grouped),
                "candidate_count": len(candidates),
                "would_compact_count": sum(len(g["items"]) for g in grouped),
            }

        created = 0
        deleted = 0
        for group in grouped:
            items = group["items"]
            summary_text = self._build_group_summary_text(group["key"], items)
            if not summary_text:
                continue
            summary_entry_id = self._make_summary_entry_id(group["key"], items)
            cfg_now = await self.get_runtime_config()
            vec, provider = await self._embed_text(summary_text, cfg_now)
            await self._db.upsert_memory_vector(
                entry_id=summary_entry_id,
                source_type="summary",
                source_id=int(datetime.utcnow().timestamp()),
                text_content=summary_text,
                vector_json=json.dumps(vec, ensure_ascii=False),
                vector_dim=len(vec),
                vector_model=cfg_now["embedding_model"],
                vector_provider=provider,
                tier="cold",
                status="active",
            )
            created += 1
            deleted += await self._db.mark_memory_vectors_deleted([str(i["entry_id"]) for i in items])

        return {
            "dry_run": False,
            "cold_days_threshold": cold_days,
            "group_size": group_size,
            "group_count": len(grouped),
            "candidate_count": len(candidates),
            "created_summaries": created,
            "deleted_originals": deleted,
        }

    async def _get_setting(self, key: str, default: str) -> str:
        row = await self._db.get_setting(key)
        if not row:
            return default
        value = str(row.get("value", "")).strip()
        return value if value else default

    async def _embed_text(self, text: str, cfg: dict) -> tuple[list[float], str]:
        api_base = str(cfg.get("embedding_api_base") or "").strip()
        api_key = str(cfg.get("embedding_api_key") or "").strip()
        model = str(cfg.get("embedding_model") or "text-embedding-3-small")
        dim = max(32, int(cfg.get("embedding_dim") or 256))
        timeout_sec = max(3.0, float(cfg.get("timeout_sec") or 15))
        if api_base and api_key:
            try:
                async with httpx.AsyncClient(timeout=timeout_sec) as client:
                    resp = await client.post(
                        f"{api_base.rstrip('/')}/embeddings",
                        headers={"Authorization": f"Bearer {api_key}"},
                        json={"model": model, "input": text},
                    )
                resp.raise_for_status()
                payload = resp.json()
                data = payload.get("data") or []
                if data and isinstance(data, list):
                    emb = data[0].get("embedding")
                    if isinstance(emb, list) and emb:
                        return self._normalize_vector([float(x) for x in emb]), "api"
            except Exception:
                pass
        return self._local_embedding(text, dim), "local"

    @staticmethod
    def _local_embedding(text: str, dim: int) -> list[float]:
        buckets = [0.0] * dim
        tokens = VectorMemoryStore._tokenize_local_text(text)
        if not tokens:
            return []
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
            idx = int(digest[:8], 16) % dim
            sign = -1.0 if int(digest[8:10], 16) % 2 else 1.0
            buckets[idx] += sign
        return VectorMemoryStore._normalize_vector(buckets)

    @staticmethod
    def _normalize_vector(vec: list[float]) -> list[float]:
        if not vec:
            return []
        norm = math.sqrt(sum(v * v for v in vec))
        if norm <= 0:
            return []
        return [float(v) / norm for v in vec]

    @staticmethod
    def _tokenize_local_text(text: str) -> list[str]:
        raw = (text or "").lower().strip()
        if not raw:
            return []
        chunks = re.findall(r"[\u4e00-\u9fff]+|[a-z0-9_]+", raw)
        tokens: list[str] = []
        for chunk in chunks:
            if re.fullmatch(r"[\u4e00-\u9fff]+", chunk):
                # Keep original chunk and add short n-grams to improve CJK recall.
                tokens.append(chunk)
                if len(chunk) >= 2:
                    tokens.extend(chunk[i : i + 2] for i in range(len(chunk) - 1))
                if len(chunk) >= 3:
                    tokens.extend(chunk[i : i + 3] for i in range(len(chunk) - 2))
            else:
                tokens.append(chunk)
        return tokens

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b))

    @staticmethod
    def _loads_vector(raw: object) -> list[float]:
        if isinstance(raw, list):
            return [float(x) for x in raw]
        if not raw:
            return []
        try:
            data = json.loads(str(raw))
            if isinstance(data, list):
                return [float(x) for x in data]
        except Exception:
            return []
        return []

    @staticmethod
    def _recency_boost(ts: object, now: datetime) -> float:
        if not ts:
            return 0.4
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00").split("+")[0])
        except Exception:
            return 0.4
        days = max((now - dt).total_seconds() / 86400.0, 0.0)
        return 0.4 + 0.6 * math.exp(-math.log(2) * days / 60.0)

    async def _entry_from_vector_row(self, row: dict, score: float) -> MemoryEntry | None:
        entry_id = str(row.get("entry_id") or "")
        source_type = str(row.get("source_type") or "")
        source_id = int(row.get("source_id") or 0)
        if not entry_id or source_id <= 0:
            return None

        if source_type == "event":
            ev = await self._db.get_event_by_id(source_id)
            if not ev:
                return None
            try:
                categories = json.loads(ev.categories or "[]")
            except Exception:
                categories = []
            return MemoryEntry(
                id=entry_id,
                text=ev.description,
                source_type="event",
                source_id=source_id,
                metadata={
                    "date": ev.date,
                    "source": ev.source,
                    "title": ev.title,
                    "categories": categories,
                    "score": round(score, 4),
                },
                score=score,
            )

        if source_type == "summary":
            return MemoryEntry(
                id=entry_id,
                text=str(row.get("text_content") or ""),
                source_type="summary",
                source_id=source_id,
                metadata={
                    "created_at": row.get("created_at"),
                    "tier": row.get("tier"),
                    "score": round(score, 4),
                },
                score=score,
            )

        snap = await self._db.get_snapshot_by_id(source_id)
        if not snap:
            return None
        return MemoryEntry(
            id=entry_id,
            text=snap.content,
            source_type="snapshot",
            source_id=source_id,
            metadata={
                "created_at": snap.created_at,
                "type": snap.type,
                "score": round(score, 4),
            },
            score=score,
        )

    async def _importance_boost(
        self,
        row: dict,
        event_cache: dict[int, object],
        snapshot_cache: dict[int, object],
    ) -> float:
        source_type = str(row.get("source_type") or "")
        source_id = int(row.get("source_id") or 0)
        if source_type == "event" and source_id > 0:
            if source_id not in event_cache:
                event_cache[source_id] = await self._db.get_event_by_id(source_id)
            ev = event_cache.get(source_id)
            if ev is None:
                return 0.45
            importance = float(getattr(ev, "importance_score", 5.0) or 5.0)
            depth = float(getattr(ev, "impression_depth", 5.0) or 5.0)
            blended = 0.7 * importance + 0.3 * depth
            return max(0.1, min(1.0, blended / 10.0))
        if source_type == "snapshot" and source_id > 0:
            if source_id not in snapshot_cache:
                snapshot_cache[source_id] = await self._db.get_snapshot_by_id(source_id)
            snap = snapshot_cache.get(source_id)
            if snap is None:
                return 0.5
            snap_type = str(getattr(snap, "type", "daily") or "daily")
            if snap_type == "conversation_end":
                return 0.7
            if snap_type == "accumulated":
                return 0.62
            return 0.5
        if source_type == "summary":
            return 0.68
        return 0.45

    @staticmethod
    def _build_event_vector_text(event: object) -> str:
        title = str(getattr(event, "title", "") or "").strip()
        description = str(getattr(event, "description", "") or "").strip()
        keywords = VectorMemoryStore._parse_json_list(getattr(event, "trigger_keywords", "[]"))
        categories = VectorMemoryStore._parse_json_list(getattr(event, "categories", "[]"))
        parts = [description]
        if title:
            parts.append(f"title: {title}")
        if keywords:
            parts.append("keywords: " + ", ".join(keywords))
        if categories:
            parts.append("categories: " + ", ".join(categories))
        return "\n".join([p for p in parts if p])

    async def _keyword_fallback_entries(
        self,
        query: str,
        limit: int,
        excluded_ids: set[str],
    ) -> list[MemoryEntry]:
        if limit <= 0:
            return []
        keywords = self._extract_keywords(query)
        if not keywords:
            return []

        candidate_limit = max(limit * 6, 30)
        now = datetime.utcnow()
        entries: list[MemoryEntry] = []

        events = await self._db.search_events_by_keywords(
            keywords,
            limit=candidate_limit,
            include_archived=False,
        )
        for ev in events:
            source_id = int(ev.id or 0)
            if source_id <= 0:
                continue
            entry_id = f"event_{source_id}"
            if entry_id in excluded_ids:
                continue
            kw_list = self._parse_json_list(ev.trigger_keywords)
            categories = self._parse_json_list(ev.categories)
            relevance = self._keyword_relevance(
                query_keywords=keywords,
                text_parts=[str(ev.title or ""), str(ev.description or "")],
                trigger_keywords=kw_list,
            )
            if relevance <= 0:
                continue
            recency = self._recency_boost(ev.date or ev.created_at, now)
            importance = self._event_importance(ev)
            final_score = 0.72 * relevance + 0.18 * recency + 0.10 * importance
            entries.append(
                MemoryEntry(
                    id=entry_id,
                    text=str(ev.description or ""),
                    source_type="event",
                    source_id=source_id,
                    metadata={
                        "date": ev.date,
                        "source": ev.source,
                        "title": ev.title,
                        "categories": categories,
                        "keywords": kw_list,
                        "score": round(final_score, 4),
                        "retrieval_mode": "keyword_fallback",
                    },
                    score=final_score,
                )
            )

        snapshots = await self._db.search_snapshots_by_keywords(keywords, limit=candidate_limit)
        for snap in snapshots:
            source_id = int(snap.id or 0)
            if source_id <= 0:
                continue
            entry_id = f"snapshot_{source_id}"
            if entry_id in excluded_ids:
                continue
            relevance = self._keyword_relevance(
                query_keywords=keywords,
                text_parts=[str(snap.content or "")],
                trigger_keywords=[],
            )
            if relevance <= 0:
                continue
            recency = self._recency_boost(snap.created_at, now)
            final_score = 0.82 * relevance + 0.18 * recency
            entries.append(
                MemoryEntry(
                    id=entry_id,
                    text=str(snap.content or ""),
                    source_type="snapshot",
                    source_id=source_id,
                    metadata={
                        "created_at": snap.created_at,
                        "type": snap.type,
                        "score": round(final_score, 4),
                        "retrieval_mode": "keyword_fallback",
                    },
                    score=final_score,
                )
            )

        entries.sort(key=lambda item: item.score, reverse=True)
        if len(entries) <= limit:
            return entries
        return entries[:limit]

    @staticmethod
    def _extract_keywords(query: str) -> list[str]:
        raw = (query or "").strip()
        if not raw:
            return []
        base_tokens = [
            t.strip()
            for t in re.split(r"[\s,\uFF0C\u3002;\uFF1B\u3001/|]+", raw)
            if t and t.strip()
        ]
        expanded: list[str] = []
        for token in base_tokens:
            expanded.append(token)
            if re.fullmatch(r"[\u4e00-\u9fff]+", token):
                # Support loose Chinese phrase matching, e.g. "博士的日常" -> "博士".
                parts = [p.strip() for p in token.split("的") if p.strip()]
                for part in parts:
                    if len(part) >= 2:
                        expanded.append(part)
        # Keep order while deduplicating and cap size to avoid SQL bloat.
        deduped = list(dict.fromkeys(expanded))
        return deduped[:12]

    @staticmethod
    def _parse_json_list(raw: object) -> list[str]:
        if not raw:
            return []
        try:
            data = json.loads(str(raw))
            if isinstance(data, list):
                return [str(v).strip() for v in data if str(v).strip()]
        except Exception:
            pass
        return []

    @staticmethod
    def _keyword_relevance(
        query_keywords: list[str],
        text_parts: list[str],
        trigger_keywords: list[str],
    ) -> float:
        if not query_keywords:
            return 0.0
        texts = " ".join(text_parts).lower()
        trigger = {str(k).lower() for k in trigger_keywords if str(k).strip()}
        hit = 0.0
        for kw in query_keywords:
            kw_lower = kw.lower()
            if not kw_lower:
                continue
            if kw_lower in trigger:
                hit += 1.0
            elif kw_lower in texts:
                hit += 0.72
        return hit / max(len(query_keywords), 1)

    @staticmethod
    def _event_importance(event: object) -> float:
        importance = float(getattr(event, "importance_score", 5.0) or 5.0)
        depth = float(getattr(event, "impression_depth", 5.0) or 5.0)
        blended = 0.7 * importance + 0.3 * depth
        return max(0.1, min(1.0, blended / 10.0))

    @staticmethod
    def _group_old_vectors(candidates: list[dict], group_size: int, max_groups: int) -> list[dict]:
        bucket_map: dict[str, list[dict]] = {}
        for row in candidates:
            source_type = str(row.get("source_type") or "unknown")
            ts = str(row.get("updated_at") or "")
            month = ts[:7] if len(ts) >= 7 else "unknown-month"
            key = f"{source_type}:{month}"
            bucket_map.setdefault(key, []).append(row)

        grouped: list[dict] = []
        for key, rows in bucket_map.items():
            if len(rows) < group_size:
                continue
            rows_sorted = sorted(rows, key=lambda r: str(r.get("updated_at") or ""))
            chunk = rows_sorted[: group_size * 2]
            grouped.append({"key": key, "items": chunk})
            if len(grouped) >= max_groups:
                break
        return grouped

    @staticmethod
    def _build_group_summary_text(group_key: str, items: list[dict]) -> str:
        lines = [f"冷记忆压缩摘要（{group_key}）"]
        for row in items[:16]:
            txt = str(row.get("text_content") or "").strip()
            if not txt:
                continue
            clean = " ".join(txt.replace("\n", " ").split())
            lines.append(f"- {clean[:120]}")
        if len(lines) <= 1:
            return ""
        return "\n".join(lines)

    @staticmethod
    def _make_summary_entry_id(group_key: str, items: list[dict]) -> str:
        joined = group_key + "|" + "|".join(str(i.get("entry_id")) for i in items)
        digest = hashlib.sha1(joined.encode("utf-8")).hexdigest()[:12]
        return f"summary_{digest}"
