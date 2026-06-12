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
        return await db.repair_non_utf8_text(dry_run=dry_run)
    finally:
        await db.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Repair TEXT columns holding non-UTF-8 bytes (e.g. a daily plan "
            "uploaded in a non-UTF-8 encoding such as GBK). Rewrites the "
            "offending rows so their stored bytes become valid UTF-8."
        )
    )
    parser.add_argument(
        "--db",
        default="./data/kelsey.db",
        help="Path to the sqlite database. Defaults to ./data/kelsey.db.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report rows that would be repaired without writing changes.",
    )
    args = parser.parse_args()
    result = asyncio.run(_run(args.db, dry_run=bool(args.dry_run)))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
