from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import yaml


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


STRUCTURED_CONTENT_RE = re.compile(
    r"(?m)^\s*结构化内容\s*[：:]\s*\{.*?\}\s*$"
)


@dataclass
class BucketFile:
    path: Path
    metadata: dict[str, Any]
    content: str

    @property
    def id(self) -> str:
        return str(self.metadata.get("id") or self.path.stem)

    @property
    def name(self) -> str:
        return str(self.metadata.get("name") or self.path.stem)

    @property
    def bucket_type(self) -> str:
        if self.metadata.get("type"):
            return str(self.metadata.get("type"))
        return str(self.path.parts[-3]) if len(self.path.parts) >= 3 else "dynamic"

    @property
    def domain_key(self) -> str:
        domains = self.metadata.get("domain") or []
        if isinstance(domains, list) and domains:
            return str(domains[0])
        return ""


def load_bucket(path: Path) -> BucketFile | None:
    try:
        raw = path.read_text(encoding="utf-8")
        if raw.startswith("---"):
            _, meta_raw, content = raw.split("---", 2)
            metadata = yaml.safe_load(meta_raw) or {}
        else:
            metadata = {"id": path.stem, "name": path.stem, "type": "dynamic", "domain": []}
            content = raw
        return BucketFile(path=path, metadata=metadata, content=content.strip())
    except Exception as exc:
        print(f"[skip] failed to read {path}: {exc}")
        return None


def dump_bucket(bucket: BucketFile, content: str) -> str:
    return "---\n" + yaml.safe_dump(bucket.metadata, allow_unicode=True, sort_keys=False) + "---\n" + content.strip() + "\n"


def normalize_title(value: str) -> str:
    text = str(value or "").lower()
    text = re.sub(r"legacy_(slowline|event|key_record)_\d+", " ", text)
    text = re.sub(r"[\s\"'“”‘’`《》〈〉「」『』【】\[\]（）()，,。.!！?？:：;；、\-—_·/\\]+", "", text)
    return text


def clean_content(content: str) -> str:
    cleaned = STRUCTURED_CONTENT_RE.sub("", str(content or ""))
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def content_signature(content: str) -> str:
    cleaned = clean_content(content).lower()
    cleaned = re.sub(r"\s+", "", cleaned)
    return cleaned[:1200]


def legacy_key(bucket: BucketFile) -> str:
    legacy_table = str(bucket.metadata.get("legacy_table") or "").strip()
    legacy_id = str(bucket.metadata.get("legacy_id") or "").strip()
    if legacy_table and legacy_id:
        return f"{legacy_table}:{legacy_id}"
    return ""


def quality(bucket: BucketFile) -> tuple[int, int, int, int]:
    cleaned_len = len(clean_content(bucket.content))
    importance = int(bucket.metadata.get("importance") or 5)
    type_rank = {"permanent": 3, "dynamic": 2, "feel": 1, "archive": 0}.get(bucket.bucket_type, 1)
    active_rank = 0 if bucket.bucket_type == "archive" else 1
    return (cleaned_len, importance, type_rank, active_rank)


def is_duplicate_group(group: list[BucketFile], *, fuzzy_title_threshold: float) -> bool:
    if len(group) < 2:
        return False
    ids = {bucket.id for bucket in group}
    legacy_keys = {legacy_key(bucket) for bucket in group if legacy_key(bucket)}
    if len(ids) < len(group) or (legacy_keys and len(legacy_keys) < len(group)):
        return True
    signatures = {content_signature(bucket.content) for bucket in group}
    if len(signatures) < len(group):
        return True
    if any(
        left and right and (left in right or right in left)
        for left in signatures
        for right in signatures
        if left != right
    ):
        return True
    titles = [normalize_title(bucket.name) for bucket in group]
    if len(titles) >= 2:
        best = max(
            SequenceMatcher(None, left, right).ratio()
            for idx, left in enumerate(titles)
            for right in titles[idx + 1 :]
            if left and right
        )
        if best >= fuzzy_title_threshold:
            return True
    return False


def find_duplicate_groups(buckets: list[BucketFile], *, fuzzy_title_threshold: float) -> list[list[BucketFile]]:
    index_by_path = {bucket.path: idx for idx, bucket in enumerate(buckets)}
    parent = list(range(len(buckets)))

    def find(idx: int) -> int:
        while parent[idx] != idx:
            parent[idx] = parent[parent[idx]]
            idx = parent[idx]
        return idx

    def union(left: int, right: int) -> None:
        root_left = find(left)
        root_right = find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    def connect(group: list[BucketFile]) -> None:
        if len(group) < 2:
            return
        first = index_by_path[group[0].path]
        for bucket in group[1:]:
            union(first, index_by_path[bucket.path])

    by_bucket_id: dict[str, list[BucketFile]] = {}
    for bucket in buckets:
        if bucket.id:
            by_bucket_id.setdefault(bucket.id, []).append(bucket)
    for group in by_bucket_id.values():
        if len(group) >= 2:
            connect(group)

    by_legacy: dict[str, list[BucketFile]] = {}
    for bucket in buckets:
        key = legacy_key(bucket)
        if key:
            by_legacy.setdefault(key, []).append(bucket)
    for group in by_legacy.values():
        if len(group) >= 2:
            connect(group)

    by_title: dict[str, list[BucketFile]] = {}
    for bucket in buckets:
        key = normalize_title(bucket.name)
        if key:
            by_title.setdefault(key, []).append(bucket)
    for group in by_title.values():
        if len(group) >= 2:
            connect(group)

    for idx, bucket in enumerate(buckets):
        title = normalize_title(bucket.name)
        if not title:
            continue
        for other in buckets[idx + 1 :]:
            other_title = normalize_title(other.name)
            if not other_title:
                continue
            if SequenceMatcher(None, title, other_title).ratio() >= fuzzy_title_threshold:
                union(idx, index_by_path[other.path])

    components: dict[int, list[BucketFile]] = {}
    for idx, bucket in enumerate(buckets):
        components.setdefault(find(idx), []).append(bucket)

    groups: list[list[BucketFile]] = []
    for group in components.values():
        if is_duplicate_group(group, fuzzy_title_threshold=fuzzy_title_threshold):
            groups.append(group)
    groups.sort(key=lambda group: normalize_title(group[0].name))
    return groups


def relative_to_cwd(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def move_to_quarantine(path: Path, buckets_dir: Path, quarantine_dir: Path) -> Path:
    rel = path.relative_to(buckets_dir)
    target = quarantine_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(target))
    return target


def delete_embeddings(buckets_dir: Path, bucket_ids: set[str]) -> int:
    if not bucket_ids:
        return 0
    db_path = buckets_dir / "embeddings.db"
    if not db_path.exists():
        return 0
    try:
        with sqlite3.connect(db_path) as conn:
            before = conn.total_changes
            conn.executemany("DELETE FROM embeddings WHERE bucket_id = ?", [(bucket_id,) for bucket_id in sorted(bucket_ids)])
            conn.commit()
            return conn.total_changes - before
    except Exception as exc:
        print(f"[warn] failed to update embeddings.db: {exc}")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Dry-run and optionally clean duplicated OB markdown buckets.")
    parser.add_argument("--buckets-dir", default="data/ob_buckets")
    parser.add_argument("--apply", action="store_true", help="Apply cleanup. Without this, only prints a report.")
    parser.add_argument("--clean-json", action="store_true", help="Remove legacy structured JSON lines from kept buckets.")
    parser.add_argument("--dedupe", action="store_true", help="Move lower-quality duplicate buckets to quarantine.")
    parser.add_argument(
        "--fuzzy-title-threshold",
        type=float,
        default=0.92,
        help="Title similarity threshold for relaxed duplicate detection. Default: 0.92",
    )
    parser.add_argument(
        "--protect-permanent",
        action="store_true",
        help="Never quarantine permanent buckets; useful after manual permanent curation.",
    )
    parser.add_argument("--report", default="", help="Optional JSON report path.")
    args = parser.parse_args()

    buckets_dir = Path(args.buckets_dir)
    buckets = [bucket for path in buckets_dir.rglob("*.md") if (bucket := load_bucket(path))]
    fuzzy_title_threshold = max(0.75, min(1.0, float(args.fuzzy_title_threshold or 0.92)))
    groups = find_duplicate_groups(buckets, fuzzy_title_threshold=fuzzy_title_threshold)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    quarantine_dir = Path("data/migration_logs") / f"ob_duplicate_quarantine_{timestamp}"

    report: dict[str, Any] = {
        "bucket_total": len(buckets),
        "duplicate_group_count": len(groups),
        "apply": bool(args.apply),
        "clean_json": bool(args.clean_json),
        "dedupe": bool(args.dedupe),
        "protect_permanent": bool(args.protect_permanent),
        "fuzzy_title_threshold": fuzzy_title_threshold,
        "groups": [],
        "cleaned_files": [],
        "quarantined_files": [],
        "embedding_deleted_ids": [],
        "embedding_deleted_count": 0,
    }
    embedding_delete_ids: set[str] = set()

    print(f"Buckets: {len(buckets)}")
    print(f"Duplicate groups: {len(groups)}")
    print(f"Mode: {'APPLY' if args.apply else 'DRY-RUN'}")

    for idx, group in enumerate(groups, start=1):
        ranked = sorted(group, key=quality, reverse=True)
        keep = ranked[0]
        remove = ranked[1:]
        if args.protect_permanent:
            remove = [bucket for bucket in remove if bucket.bucket_type != "permanent"]
            if not remove:
                continue
        print(f"\n[{idx}] keep: {keep.name} :: {keep.id} :: {keep.bucket_type}/{keep.domain_key} :: len={len(clean_content(keep.content))}")
        group_report = {
            "keep": relative_to_cwd(keep.path),
            "keep_id": keep.id,
            "keep_type": keep.bucket_type,
            "keep_domain": keep.domain_key,
            "remove": [],
        }
        for bucket in remove:
            print(f"    remove: {bucket.name} :: {bucket.id} :: {bucket.bucket_type}/{bucket.domain_key} :: len={len(clean_content(bucket.content))}")
            group_report["remove"].append(
                {
                    "path": relative_to_cwd(bucket.path),
                    "id": bucket.id,
                    "type": bucket.bucket_type,
                    "domain": bucket.domain_key,
                    "cleaned_len": len(clean_content(bucket.content)),
                }
            )
            if args.apply and args.dedupe:
                target = move_to_quarantine(bucket.path, buckets_dir, quarantine_dir)
                report["quarantined_files"].append({"from": relative_to_cwd(bucket.path), "to": relative_to_cwd(target)})
                embedding_delete_ids.add(bucket.id)

        if args.apply and args.clean_json:
            cleaned = clean_content(keep.content)
            if cleaned != keep.content.strip():
                keep.path.write_text(dump_bucket(keep, cleaned), encoding="utf-8")
                report["cleaned_files"].append(relative_to_cwd(keep.path))
                embedding_delete_ids.add(keep.id)

        report["groups"].append(group_report)

    if args.apply and args.clean_json:
        for bucket in buckets:
            if any(bucket.path == Path(group["keep"]) for group in report["groups"]):
                continue
            cleaned = clean_content(bucket.content)
            if cleaned != bucket.content.strip() and bucket.path.exists():
                bucket.path.write_text(dump_bucket(bucket, cleaned), encoding="utf-8")
                report["cleaned_files"].append(relative_to_cwd(bucket.path))
                embedding_delete_ids.add(bucket.id)

    if args.apply:
        report["embedding_deleted_ids"] = sorted(embedding_delete_ids)
        report["embedding_deleted_count"] = delete_embeddings(buckets_dir, embedding_delete_ids)

    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nReport written: {report_path}")

    if not args.apply:
        print("\nDry-run only. Add --apply with --dedupe and/or --clean-json to modify files.")
    elif args.dedupe:
        print(f"\nQuarantine: {quarantine_dir}")
    if args.apply and embedding_delete_ids:
        print("Embedding rows for changed buckets were deleted; run backfill_ob_embeddings.py --resume to regenerate them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
