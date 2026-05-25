from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.config import load_config
from server.database import Database
from server.ob_client import OBClient
from server.ob_embedding import OBEmbeddingStore


def _dry_run_embedding_count(buckets_dir: str) -> int:
    path = Path(buckets_dir) / "embeddings.db"
    if not path.exists():
        return 0
    try:
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as conn:
            row = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()
        return int(row[0] if row else 0)
    except Exception:
        return 0


async def _upsert_with_retry(
    store: OBEmbeddingStore,
    bucket_id: str,
    content: str,
    *,
    retry: int,
) -> bool:
    attempts = max(1, int(retry or 1))
    for idx in range(attempts):
        ok = await store.upsert(bucket_id, content)
        if ok:
            return True
        if idx < attempts - 1:
            await asyncio.sleep(2 ** idx)
    return False


async def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill embeddings for OB buckets.")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--resume", action="store_true", help="Skip buckets that already have embeddings")
    parser.add_argument("--dry-run", action="store_true", help="Only print how many buckets would be processed")
    parser.add_argument("--retry", type=int, default=3)
    parser.add_argument("--include-archive", action="store_true")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.dry_run:
        ob = OBClient(cfg.ob.buckets_dir)
        buckets = await ob.list_buckets(include_archive=args.include_archive)
        existing_count = _dry_run_embedding_count(cfg.ob.buckets_dir)
        skipped = min(existing_count, len(buckets)) if args.resume else 0
        print(
            json.dumps(
                {
                    "bucket_total": len(buckets),
                    "would_process": max(0, len(buckets) - skipped),
                    "would_skip": skipped,
                    "embedding_enabled": "not_checked_in_dry_run",
                    "embedding_total": existing_count,
                    "embeddings_db": str(Path(cfg.ob.buckets_dir) / "embeddings.db"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    db = Database(cfg.database.path)
    await db.initialize()
    try:
        store = OBEmbeddingStore(db, cfg.ob.buckets_dir)
        ob = OBClient(cfg.ob.buckets_dir, embedding_store=store)
        buckets = await ob.list_buckets(include_archive=args.include_archive)
        to_process = []
        skipped = 0
        for bucket in buckets:
            if args.resume and store.get(bucket.id):
                skipped += 1
                continue
            to_process.append(bucket)

        ok_count = 0
        fail_ids: list[str] = []
        batch_size = max(1, int(args.batch_size or 20))
        for start in range(0, len(to_process), batch_size):
            batch = to_process[start : start + batch_size]
            results = await asyncio.gather(
                *[
                    _upsert_with_retry(store, bucket.id, bucket.content, retry=args.retry)
                    for bucket in batch
                ]
            )
            for bucket, ok in zip(batch, results):
                if ok:
                    ok_count += 1
                else:
                    fail_ids.append(bucket.id)
                    print(f"failed: {bucket.id}", file=sys.stderr)
            print(f"processed {min(start + batch_size, len(to_process))}/{len(to_process)}")

        print(
            json.dumps(
                {
                    "success": ok_count,
                    "failed": len(fail_ids),
                    "skipped": skipped,
                    "failed_ids": fail_ids,
                    "embedding_total": store.count(),
                    "embeddings_db": str(store.sqlite_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if fail_ids:
            raise SystemExit(1)
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
