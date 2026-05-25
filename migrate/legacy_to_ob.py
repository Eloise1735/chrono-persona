from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from urllib.parse import quote
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.config import load_config
from server.ob_client import OBClient


ANCHOR_TYPES = {"anniversary_date", "commitment_agreement", "emotional_anchor"}
PERMANENT_TYPES = {
    "medication_protocol",
    "health_monitoring",
    "dietary_intervention",
    "medical_review_date",
    "lifecycle_milestone",
    "key_collaboration",
    "life_pattern",
    "important_date",
    "important_item",
    "medical_advice",
}


def _json_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    try:
        data = json.loads(str(value))
        if isinstance(data, list):
            return [str(v) for v in data if str(v).strip()]
    except Exception:
        pass
    return [s.strip() for s in str(value).split(",") if s.strip()]


def _row_dict(cursor: sqlite3.Cursor, row: tuple[Any, ...]) -> dict[str, Any]:
    return {desc[0]: row[idx] for idx, desc in enumerate(cursor.description or [])}


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _importance(value: Any, default: int = 5) -> int:
    try:
        score = float(value)
        if 0 <= score <= 1:
            score *= 10
        return max(1, min(10, round(score)))
    except Exception:
        return default


def _arousal(value: Any, default: float = 0.3) -> float:
    try:
        score = float(value)
        if score > 1:
            score /= 10
        return max(0.0, min(1.0, score))
    except Exception:
        return default


def _event_domain(categories: list[str], source: str) -> list[str]:
    blob = " ".join(categories).lower()
    if "relationship" in blob or "conversation" in blob or source == "conversation":
        return ["relationship"]
    if any(token in blob for token in ("life", "daily", "environment", "plan", "npc")):
        return ["character_life"]
    return ["shared"]


def _event_valence(text: str) -> float:
    positive = ("感谢", "欣慰", "安心", "承诺", "靠近", "完成", "喜欢", "好")
    negative = ("害怕", "失落", "疼", "痛", "焦虑", "冲突", "担心", "难过")
    if any(word in text for word in positive) and not any(word in text for word in negative):
        return 0.68
    if any(word in text for word in negative):
        return 0.35
    return 0.5


async def migrate_key_records(conn: sqlite3.Connection, ob: OBClient, *, dry_run: bool) -> int:
    if not _table_exists(conn, "key_records"):
        return 0
    cursor = conn.execute("SELECT * FROM key_records ORDER BY id")
    count = 0
    rows = [_row_dict(cursor, row) for row in cursor.fetchall()]
    active_anchor_rows = [r for r in rows if str(r.get("status") or "active") == "active" and str(r.get("type")) in ANCHOR_TYPES]
    pinned_ids = {int(r["id"]) for r in active_anchor_rows[:24]}
    for row in rows:
        rid = int(row["id"])
        rtype = str(row.get("type") or "life_pattern")
        status = str(row.get("status") or "active")
        if status != "active":
            continue
        pinned = rid in pinned_ids
        bucket_type = "permanent" if rtype in ANCHOR_TYPES | PERMANENT_TYPES else "dynamic"
        domain = ["anchor"] if pinned else [rtype]
        body = "\n".join(
            part for part in [
                f"# {row.get('title') or rtype}",
                str(row.get("content_text") or "").strip(),
                f"生效：{row.get('start_date') or ''} ~ {row.get('end_date') or ''}".strip(),
                f"结构化内容：{row.get('content_json')}" if row.get("content_json") else "",
            ] if part
        )
        if not dry_run:
            await ob.hold(
                body,
                tags=_json_list(row.get("tags")) + _json_list(row.get("match_keywords")),
                importance=10 if pinned else 8,
                domain=domain,
                valence=0.55,
                arousal=0.45 if pinned else 0.3,
                bucket_type=bucket_type,
                name=str(row.get("title") or rtype),
                pinned=pinned,
                protected=pinned,
                created=row.get("created_at") or row.get("updated_at"),
                bucket_id=f"legacy_key_record_{rid}",
                extra_metadata={"legacy_table": "key_records", "legacy_id": rid, "record_type": rtype},
            )
        count += 1
    return count


async def migrate_events(conn: sqlite3.Connection, ob: OBClient, *, dry_run: bool) -> int:
    if not _table_exists(conn, "event_anchors"):
        return 0
    cursor = conn.execute("SELECT * FROM event_anchors ORDER BY id")
    count = 0
    for raw in cursor.fetchall():
        row = _row_dict(cursor, raw)
        eid = int(row["id"])
        categories = _json_list(row.get("categories"))
        keywords = _json_list(row.get("trigger_keywords"))
        description = str(row.get("description") or "").strip()
        title = str(row.get("title") or "").strip() or f"历史事件 {eid}"
        source = str(row.get("source") or "")
        if not description:
            continue
        if not dry_run:
            await ob.hold(
                f"# {title}\n{description}",
                tags=keywords + categories,
                importance=_importance(row.get("importance_score"), 5),
                domain=_event_domain(categories, source),
                valence=_event_valence(description),
                arousal=_arousal(row.get("impression_depth"), 0.45),
                bucket_type="dynamic",
                name=title,
                resolved=False,
                created=row.get("created_at") or row.get("date"),
                bucket_id=f"legacy_event_{eid}",
                extra_metadata={"legacy_table": "event_anchors", "legacy_id": eid, "event_date": row.get("date")},
            )
        count += 1
    return count


async def migrate_slowlines(conn: sqlite3.Connection, ob: OBClient, *, dry_run: bool) -> int:
    if not _table_exists(conn, "slowlines"):
        return 0
    cursor = conn.execute("SELECT * FROM slowlines WHERE status = 'active' ORDER BY id")
    count = 0
    for raw in cursor.fetchall():
        row = _row_dict(cursor, raw)
        sid = int(row["id"])
        theme = str(row.get("theme") or f"持续线索 {sid}")
        body = "\n".join(
            part for part in [
                f"# {theme}",
                str(row.get("stage_summary") or "").strip(),
                str(row.get("trajectory_summary") or "").strip(),
                str(row.get("current_tension") or "").strip(),
                "未解问题：" + "、".join(_json_list(row.get("open_questions"))) if _json_list(row.get("open_questions")) else "",
            ] if part
        )
        if not body.strip():
            continue
        if not dry_run:
            await ob.hold(
                body,
                tags=[str(row.get("source_family") or "daily_life"), str(row.get("scope") or "shared")],
                importance=_importance(row.get("salience"), 6),
                domain=[str(row.get("source_family") or "daily_life")],
                valence=0.48,
                arousal=0.72,
                bucket_type="dynamic",
                name=theme,
                resolved=False,
                created=row.get("created_at") or row.get("updated_at"),
                bucket_id=f"legacy_slowline_{sid}",
                extra_metadata={"legacy_table": "slowlines", "legacy_id": sid},
            )
        count += 1
    return count


async def migrate_relationship_summary(conn: sqlite3.Connection, ob: OBClient, *, dry_run: bool) -> int:
    if not _table_exists(conn, "relationship_states"):
        return 0
    cursor = conn.execute("SELECT * FROM relationship_states ORDER BY id DESC LIMIT 8")
    rows = [_row_dict(cursor, row) for row in cursor.fetchall()]
    if not rows:
        return 0
    parts = ["# 关系背景沉淀", "以下是迁移时从旧 RelationshipState 汇总出的过渡性关系感受材料："]
    for row in reversed(rows):
        summary = str(row.get("relationship_feeling_summary") or "").strip()
        if summary:
            parts.append(f"- {row.get('created_at')}: {summary}")
    content = "\n".join(parts)
    if not dry_run:
        latest = rows[0]
        await ob.hold(
            content,
            tags=["relationship", "legacy_relationship_state"],
            importance=8,
            domain=[],
            valence=float(latest.get("valence") or 0.5),
            arousal=float(latest.get("arousal") or 0.5),
            bucket_type="feel",
            name="关系背景沉淀",
            protected=True,
            created=latest.get("created_at"),
            bucket_id="legacy_relationship_summary",
            extra_metadata={"legacy_table": "relationship_states"},
        )
    return 1


async def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate legacy Kelsey memory tables into OB buckets.")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument("--db", default=None, help="Override SQLite database path")
    parser.add_argument("--buckets-dir", default=None, help="Override OB bucket directory")
    parser.add_argument("--dry-run", action="store_true", help="Count rows without writing buckets")
    args = parser.parse_args()

    cfg = load_config(args.config)
    db_path = Path(args.db or cfg.database.path)
    buckets_dir = Path(args.buckets_dir or cfg.ob.buckets_dir)
    db_uri = f"file:{quote(str(db_path.resolve()).replace(chr(92), '/'), safe=':/')}?mode=ro&immutable=1"
    conn = sqlite3.connect(db_uri, uri=True)
    try:
        ob = OBClient(buckets_dir)
        try:
            result = {
                "key_records": await migrate_key_records(conn, ob, dry_run=args.dry_run),
                "events": await migrate_events(conn, ob, dry_run=args.dry_run),
                "slowlines": await migrate_slowlines(conn, ob, dry_run=args.dry_run),
                "relationship_summary": await migrate_relationship_summary(conn, ob, dry_run=args.dry_run),
                "buckets_dir": str(buckets_dir),
                "dry_run": args.dry_run,
            }
        except sqlite3.DatabaseError as exc:
            raise SystemExit(
                "无法读取 legacy SQLite 数据库；请先停止正在运行的服务并确认数据库完整性，"
                f"然后重试迁移。底层错误：{exc}"
            ) from exc
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    asyncio.run(main())
