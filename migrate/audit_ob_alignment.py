from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.ob_client import OBClient


async def main() -> None:
    parser = argparse.ArgumentParser(description="Audit Ombre Brain alignment and optionally normalize safe metadata.")
    parser.add_argument("--buckets-dir", default="./data/ob_buckets")
    parser.add_argument("--apply", action="store_true", help="Apply safe metadata normalization.")
    args = parser.parse_args()

    client = OBClient(Path(args.buckets_dir))
    buckets = await client.list_buckets(include_archive=True)
    stats = await client.stats()
    diagnostics = await client.diagnostics()

    archive_renamed = []
    if args.apply:
        for bucket in buckets:
            if str(bucket.metadata.get("type") or "").lower() == "archive":
                ok = await client.update(bucket.id, type="archived")
                if ok:
                    archive_renamed.append(bucket.id)

    report = {
        "buckets_dir": str(Path(args.buckets_dir).resolve()),
        "apply": bool(args.apply),
        "stats": stats,
        "diagnostics": diagnostics,
        "applied": {
            "archive_type_renamed_to_archived": archive_renamed,
        },
        "notes": [
            "Historical feel buckets without source_bucket are reported only; this script never guesses their source.",
            "Low-score archive candidates are reported only unless normal decay is executed by the service/API.",
        ],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
