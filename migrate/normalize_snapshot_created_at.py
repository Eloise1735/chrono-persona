from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.database import Database


async def _run(db_path: str, *, dry_run: bool) -> dict:
    db = Database(db_path)
    await db.initialize()
    try:
        return await db.repair_snapshot_timezones(dry_run=dry_run)
    finally:
        await db.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Normalize state_snapshots.created_at values to UTC Z format."
    )
    parser.add_argument(
        "--db",
        default="./data/kelsey.db",
        help="Path to the sqlite database. Defaults to ./data/kelsey.db.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report rows that would be updated without writing changes.",
    )
    args = parser.parse_args()
    result = asyncio.run(_run(args.db, dry_run=bool(args.dry_run)))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if int(result.get("error_count") or 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
