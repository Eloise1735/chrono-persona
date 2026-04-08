from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

DISPLAY_TZ = timezone(timedelta(hours=8))


def shanghai_now() -> datetime:
    return datetime.now(DISPLAY_TZ)


def _normalize_iso_text(value: str) -> str:
    text = (value or "").strip()
    text = re.sub(r"[Tt]\s+", "T", text, count=1)
    if re.match(r"^\d{4}-\d{2}-\d{2} \d", text):
        text = text.replace(" ", "T", 1)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return re.sub(r"([Tt])(\d)(?=:)", r"\g<1>0\2", text, count=1)


def _to_shanghai_time(dt: datetime, *, naive_tz: timezone) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=naive_tz)
    return dt.astimezone(DISPLAY_TZ)


def utc_naive_to_shanghai_iso(dt: datetime, *, timespec: str = "seconds") -> str:
    return _to_shanghai_time(dt, naive_tz=timezone.utc).isoformat(timespec=timespec)


def shanghai_time_to_utc_naive(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=DISPLAY_TZ)
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _format_utc_instant_z(dt: datetime) -> str:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat() + "Z"


def parse_user_instant_to_shanghai(value: str) -> datetime:
    text = _normalize_iso_text(value)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Invalid ISO datetime: {value!r}") from exc
    return _to_shanghai_time(dt, naive_tz=DISPLAY_TZ)


def parse_db_instant_to_shanghai(value: str) -> datetime:
    text = _normalize_iso_text(value)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Invalid ISO datetime: {value!r}") from exc
    # Legacy DB rows were often stored with datetime.utcnow().isoformat().
    return _to_shanghai_time(dt, naive_tz=timezone.utc)


def parse_user_instant_to_utc_naive(value: str) -> datetime:
    return shanghai_time_to_utc_naive(parse_user_instant_to_shanghai(value))


def normalize_user_instant_to_utc_z(value: str) -> str:
    return _format_utc_instant_z(parse_user_instant_to_utc_naive(value))


def iso_string_for_cst_display(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    try:
        dt = parse_db_instant_to_shanghai(text)
    except ValueError:
        return value
    return dt.isoformat(timespec="milliseconds")
