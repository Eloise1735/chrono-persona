from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.config import load_config
from server.database import Database
from server.ob_embedding import OBEmbeddingStore


def _mask_secret(value: str) -> str:
    value = str(value or "")
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def _looks_like_base_url(value: str) -> bool:
    parsed = urlparse(str(value or ""))
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check OB embedding runtime settings and optionally probe the embeddings endpoint."
    )
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument("--probe", action="store_true", help="Send one test embedding request")
    args = parser.parse_args()

    cfg = load_config(args.config)
    db = Database(cfg.database.path)
    await db.initialize()
    try:
        store = OBEmbeddingStore(db, cfg.ob.buckets_dir)
        runtime = await store._runtime_config()
        api_base = str(runtime.get("embedding_api_base") or "").strip()
        api_key = str(runtime.get("embedding_api_key") or "").strip()
        model = str(runtime.get("embedding_model") or "").strip()
        enabled = await store.is_enabled()
        endpoint = f"{api_base.rstrip('/')}/embeddings" if api_base else ""

        result: dict[str, object] = {
            "embedding_enabled": bool(enabled),
            "api_base": api_base,
            "api_base_valid_shape": _looks_like_base_url(api_base),
            "embeddings_endpoint": endpoint,
            "api_key_masked": _mask_secret(api_key),
            "api_key_present": bool(api_key),
            "model": model,
            "dim": runtime.get("embedding_dim"),
            "timeout_sec": runtime.get("timeout_sec"),
            "embeddings_db": str(store.sqlite_path),
            "embedding_total": store.count(),
        }

        if args.probe:
            vector = await store.embed_text("OB embedding connectivity probe")
            result["probe_ok"] = bool(vector)
            result["probe_dim"] = len(vector)
            if vector:
                result["probe_first_values"] = [round(float(v), 6) for v in vector[:3]]

        print(json.dumps(result, ensure_ascii=False, indent=2))

        if not enabled:
            raise SystemExit(1)
        if args.probe and not result.get("probe_ok"):
            raise SystemExit(2)
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
