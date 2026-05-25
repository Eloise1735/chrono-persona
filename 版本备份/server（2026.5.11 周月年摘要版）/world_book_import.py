"""Parse world book / lorebook JSON exports (e.g. SillyTavern) into local world_books rows."""

from __future__ import annotations

import re
from typing import Any


def _coerce_str_list(val: Any) -> list[str]:
    if val is None:
        return []
    if isinstance(val, str):
        parts = re.split(r"[,，、\n;；]", val)
        return [p.strip() for p in parts if p.strip()]
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    return [str(val).strip()] if str(val).strip() else []


def _truthy_enabled(val: Any) -> bool:
    if val is None:
        return True
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val != 0
    s = str(val).strip().lower()
    if s in ("false", "0", "no", "off", "disabled"):
        return False
    return True


def _normalize_one(
    raw: dict[str, Any],
    *,
    fallback_name: str,
    source_tag: str,
) -> dict[str, Any] | None:
    content = (
        raw.get("content")
        or raw.get("text")
        or raw.get("body")
        or raw.get("entry")
        or ""
    )
    if not isinstance(content, str):
        content = str(content)
    content = content.strip()
    if not content:
        return None

    name = (
        raw.get("name")
        or raw.get("title")
        or raw.get("comment")
        or raw.get("memo")
        or ""
    )
    if not isinstance(name, str):
        name = str(name)
    name = name.strip() or fallback_name

    keys = _coerce_str_list(
        raw.get("match_keywords")
        or raw.get("keywords")
        or raw.get("keys")
        or raw.get("key")
    )
    sec = _coerce_str_list(raw.get("secondary_keys") or raw.get("keysecondary"))
    match_keywords = list(dict.fromkeys(keys + sec))

    extra_tags = _coerce_str_list(raw.get("tags"))
    tags = list(dict.fromkeys(([source_tag] if source_tag else []) + extra_tags))

    is_active = _truthy_enabled(raw.get("is_active", raw.get("enabled", True)))

    return {
        "name": name[:500],
        "content": content,
        "tags": tags,
        "match_keywords": match_keywords,
        "is_active": is_active,
    }


def _iter_sillytavern_entries(entries_obj: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(entries_obj, dict):
        for uid, entry in entries_obj.items():
            if not isinstance(entry, dict):
                continue
            merged = dict(entry)
            if "comment" not in merged and merged.get("name"):
                merged.setdefault("comment", merged.get("name"))
            out.append(
                _normalize_one(
                    merged,
                    fallback_name=f"条目-{uid}",
                    source_tag="酒馆导入",
                )
            )
    elif isinstance(entries_obj, list):
        for i, entry in enumerate(entries_obj):
            if not isinstance(entry, dict):
                continue
            out.append(
                _normalize_one(
                    entry,
                    fallback_name=f"条目-{i}",
                    source_tag="酒馆导入",
                )
            )
    return [x for x in out if x]


def _unwrap_payload(root: Any) -> Any:
    if isinstance(root, dict):
        if "data" in root and len(root) <= 4:
            inner = root["data"]
            if isinstance(inner, (dict, list)):
                return inner
        if "world_book" in root and isinstance(root["world_book"], dict):
            return root["world_book"]
        if "lorebook" in root and isinstance(root["lorebook"], dict):
            return root["lorebook"]
    return root


def parse_world_book_import(
    root: Any,
    *,
    skip_disabled: bool = False,
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Returns (items_ready_for_db, warnings).
    Each item: name, content, tags (list[str]), match_keywords (list[str]), is_active (bool).
    """
    warnings: list[str] = []
    payload = _unwrap_payload(root)

    if isinstance(payload, list):
        items: list[dict[str, Any]] = []
        for i, el in enumerate(payload):
            if not isinstance(el, dict):
                warnings.append(f"跳过非对象项 index={i}")
                continue
            one = _normalize_one(
                el,
                fallback_name=f"导入-{i}",
                source_tag="JSON导入",
            )
            if one:
                items.append(one)
        return items, warnings

    if isinstance(payload, dict):
        entries = payload.get("entries")
        if isinstance(entries, (dict, list)):
            items = _iter_sillytavern_entries(entries)
            if not items and entries:
                warnings.append("entries 存在但未解析出有效条目")
            filtered: list[dict[str, Any]] = []
            for it in items:
                if skip_disabled and not it["is_active"]:
                    continue
                filtered.append(it)
            if skip_disabled and len(filtered) < len(items):
                warnings.append(f"已跳过 {len(items) - len(filtered)} 条禁用条目")
            return filtered, warnings

        wb_list = payload.get("world_books") or payload.get("items")
        if isinstance(wb_list, list):
            items = []
            for i, el in enumerate(wb_list):
                if not isinstance(el, dict):
                    continue
                one = _normalize_one(
                    el,
                    fallback_name=f"导入-{i}",
                    source_tag="JSON导入",
                )
                if one:
                    items.append(one)
            return items, warnings

        one = _normalize_one(
            payload,
            fallback_name="导入条目",
            source_tag="JSON导入",
        )
        if one:
            if skip_disabled and not one["is_active"]:
                return [], warnings + ["单条导入为禁用状态，已跳过"]
            return [one], warnings
        return [], warnings + ["无法从 JSON 中识别世界书结构（支持酒馆 entries、数组或单对象）"]

    warnings.append("根类型不支持，应为 JSON 对象或数组")
    return [], warnings
