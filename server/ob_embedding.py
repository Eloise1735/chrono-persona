from __future__ import annotations

import json
import logging
import math
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from server.database import Database
from server.vector_memory_store import (
    KEY_VECTOR_EMBEDDING_API_BASE,
    KEY_VECTOR_EMBEDDING_API_KEY,
    KEY_VECTOR_EMBEDDING_DIM,
    KEY_VECTOR_EMBEDDING_MODEL,
    KEY_VECTOR_EMBEDDING_TIMEOUT,
)

logger = logging.getLogger(__name__)


class OBEmbeddingStore:
    """Small embedding index for OB buckets.

    Settings are shared with VectorMemoryStore, while vectors are stored beside
    OB buckets in a separate sqlite file so OB can be backed up/moved as a unit.
    """

    def __init__(self, db: Database, buckets_dir: str | Path):
        self._db = db
        self.sqlite_path = Path(buckets_dir) / "embeddings.db"
        self._available = False
        try:
            self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_db()
            self._available = True
        except Exception:
            logger.exception("OB embedding sqlite initialization failed; embeddings disabled.")

    def _init_db(self) -> None:
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS embeddings (
                    bucket_id TEXT PRIMARY KEY,
                    embedding TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    async def _runtime_config(self) -> dict[str, Any]:
        return {
            "embedding_api_base": await self._get_setting(KEY_VECTOR_EMBEDDING_API_BASE, ""),
            "embedding_api_key": await self._get_setting(KEY_VECTOR_EMBEDDING_API_KEY, ""),
            "embedding_model": await self._get_setting(KEY_VECTOR_EMBEDDING_MODEL, "text-embedding-3-small"),
            "embedding_dim": int(await self._get_setting(KEY_VECTOR_EMBEDDING_DIM, "256")),
            "timeout_sec": float(await self._get_setting(KEY_VECTOR_EMBEDDING_TIMEOUT, "15")),
        }

    async def _get_setting(self, key: str, default: str) -> str:
        try:
            row = await self._db.get_setting(key)
            if not row:
                return default
            value = str(row.get("value", "")).strip()
            return value if value else default
        except Exception:
            logger.exception("Failed to read embedding setting %s", key)
            return default

    async def is_enabled(self) -> bool:
        if not self._available:
            return False
        cfg = await self._runtime_config()
        return bool(str(cfg["embedding_api_key"]).strip() and str(cfg["embedding_api_base"]).strip())

    async def embed_text(self, text: str) -> list[float]:
        text = str(text or "").strip()[:2000]
        if not text:
            return []
        cfg = await self._runtime_config()
        api_base = str(cfg.get("embedding_api_base") or "").strip()
        api_key = str(cfg.get("embedding_api_key") or "").strip()
        model = str(cfg.get("embedding_model") or "text-embedding-3-small")
        timeout_sec = max(3.0, float(cfg.get("timeout_sec") or 15))
        if not api_base or not api_key:
            return []
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
                    return self._normalize_vector([float(x) for x in emb])
        except Exception:
            logger.exception("OB embedding request failed")
        return []

    async def upsert(self, bucket_id: str, content: str) -> bool:
        if not str(bucket_id or "").strip() or not str(content or "").strip():
            return False
        if not await self.is_enabled():
            return False
        vector = await self.embed_text(str(content)[:2000])
        if not vector:
            return False
        self._store(bucket_id, vector)
        return True

    def _store(self, bucket_id: str, vector: list[float]) -> None:
        if not self._available:
            return
        try:
            with sqlite3.connect(self.sqlite_path) as conn:
                conn.execute(
                    """
                    INSERT INTO embeddings(bucket_id, embedding, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(bucket_id) DO UPDATE SET
                        embedding = excluded.embedding,
                        updated_at = excluded.updated_at
                    """,
                    (
                        str(bucket_id),
                        json.dumps(vector, ensure_ascii=False),
                        datetime.utcnow().isoformat(),
                    ),
                )
                conn.commit()
        except Exception:
            logger.exception("Failed to store OB embedding for %s", bucket_id)

    def get(self, bucket_id: str) -> list[float] | None:
        if not self._available:
            return None
        try:
            with sqlite3.connect(self.sqlite_path) as conn:
                row = conn.execute(
                    "SELECT embedding FROM embeddings WHERE bucket_id = ?",
                    (str(bucket_id),),
                ).fetchone()
            if not row:
                return None
            vec = self._loads_vector(row[0])
            return vec or None
        except Exception:
            logger.exception("Failed to read OB embedding for %s", bucket_id)
            return None

    def delete(self, bucket_id: str) -> None:
        if not self._available:
            return
        try:
            with sqlite3.connect(self.sqlite_path) as conn:
                conn.execute("DELETE FROM embeddings WHERE bucket_id = ?", (str(bucket_id),))
                conn.commit()
        except Exception:
            logger.exception("Failed to delete OB embedding for %s", bucket_id)

    async def search_similar(self, query: str, top_k: int = 80) -> list[tuple[str, float]]:
        if not self._available:
            return []
        if not await self.is_enabled():
            return []
        qvec = await self.embed_text(query)
        if not qvec:
            return []
        target_dim = len(qvec)
        try:
            with sqlite3.connect(self.sqlite_path) as conn:
                rows = conn.execute("SELECT bucket_id, embedding FROM embeddings").fetchall()
        except Exception:
            logger.exception("Failed to scan OB embeddings")
            return []
        scored: list[tuple[str, float]] = []
        for bucket_id, raw_vec in rows:
            vec = self._loads_vector(raw_vec)
            if len(vec) != target_dim:
                continue
            sim = self._cosine_similarity(qvec, vec)
            if sim > 0:
                scored.append((str(bucket_id), float(sim)))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[: max(1, int(top_k or 80))]

    def count(self) -> int:
        if not self._available:
            return 0
        try:
            with sqlite3.connect(self.sqlite_path) as conn:
                row = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()
            return int(row[0] if row else 0)
        except Exception:
            logger.exception("Failed to count OB embeddings")
            return 0

    @staticmethod
    def _normalize_vector(vec: list[float]) -> list[float]:
        if not vec:
            return []
        norm = math.sqrt(sum(v * v for v in vec))
        if norm <= 0:
            return []
        return [float(v) / norm for v in vec]

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
