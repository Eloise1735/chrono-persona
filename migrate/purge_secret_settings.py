from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

SECRET_SETTING_KEYS = (
    "llm_api_key",
    "env_llm_api_key",
    "snapshot_llm_api_key",
    "vector_embedding_api_key",
    "plan_web_search_api_key",
)


def main() -> int:
    db_path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/kelsey.db")
    if not db_path.exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 2

    with sqlite3.connect(db_path) as conn:
        before = conn.execute(
            f"""
            SELECT key FROM system_settings
            WHERE key IN ({",".join("?" for _ in SECRET_SETTING_KEYS)})
              AND COALESCE(value, '') <> ''
            """,
            SECRET_SETTING_KEYS,
        ).fetchall()
        conn.execute(
            f"""
            UPDATE system_settings
            SET value = '', updated_at = datetime('now')
            WHERE key IN ({",".join("?" for _ in SECRET_SETTING_KEYS)})
            """,
            SECRET_SETTING_KEYS,
        )
        conn.commit()

    purged = [row[0] for row in before]
    print(f"Purged {len(purged)} secret setting(s): {', '.join(purged) if purged else '(none)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
