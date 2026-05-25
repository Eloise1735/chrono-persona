from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.config import load_config
from server.database import Database
from server.ob_client import OBClient


def _looks_like_snapshot_bucket(metadata: dict) -> bool:
    tags = {str(tag).strip().lower() for tag in metadata.get("tags", []) if str(tag).strip()}
    source = str(metadata.get("source") or "").strip().lower()
    return (
        bool(metadata.get("snapshot_id"))
        or source in {"snapshot_scheduler", "conversation_reflection"}
        or ("snapshot" in tags and "environment" in tags)
    )


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Move old snapshot-generated OB buckets to feel and restore first-person snapshot text."
    )
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument("--apply", action="store_true", help="Apply changes. Default is dry-run.")
    parser.add_argument("--include-archive", action="store_true")
    args = parser.parse_args()

    cfg = load_config(args.config)
    db = Database(cfg.database.path)
    await db.initialize()
    client = OBClient(cfg.ob.buckets_dir)

    report: dict = {
        "apply": bool(args.apply),
        "buckets_dir": str(Path(cfg.ob.buckets_dir).resolve()),
        "database": str(Path(cfg.database.path).resolve()),
        "candidates": [],
        "updated": [],
        "skipped": [],
    }

    try:
        buckets = await client.list_buckets(include_archive=bool(args.include_archive))
        for bucket in buckets:
            meta = dict(bucket.metadata or {})
            if str(meta.get("type") or "").strip().lower() == "feel":
                continue
            if not _looks_like_snapshot_bucket(meta):
                continue
            snapshot_id = int(meta.get("snapshot_id") or 0)
            if snapshot_id <= 0:
                report["skipped"].append({"id": bucket.id, "reason": "missing_snapshot_id"})
                continue
            snapshot = await db.get_snapshot_by_id(snapshot_id)
            if snapshot is None:
                report["skipped"].append({"id": bucket.id, "snapshot_id": snapshot_id, "reason": "snapshot_not_found"})
                continue
            content = str(snapshot.content or "").strip()
            if not content:
                report["skipped"].append({"id": bucket.id, "snapshot_id": snapshot_id, "reason": "empty_snapshot_content"})
                continue
            item = {
                "id": bucket.id,
                "snapshot_id": snapshot_id,
                "old_type": meta.get("type"),
                "old_content_preview": str(bucket.content or "")[:90],
                "new_content_preview": content[:90],
            }
            report["candidates"].append(item)
            if args.apply:
                tags = list(dict.fromkeys([*(meta.get("tags") or []), "snapshot", "feel"]))
                ok = await client.update(
                    bucket.id,
                    content=content,
                    type="feel",
                    domain=["character_life"],
                    tags=tags,
                    source_kind="state_snapshot",
                )
                (report["updated"] if ok else report["skipped"]).append(
                    item if ok else item | {"reason": "update_failed"}
                )
    finally:
        if db.conn is not None:
            await db.conn.close()

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
