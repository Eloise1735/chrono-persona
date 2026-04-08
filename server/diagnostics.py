from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

_CST = timezone(timedelta(hours=8))


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_cst_iso() -> str:
    return _now_utc().astimezone(_CST).isoformat(timespec="seconds")


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if len(text) <= 240:
            return text
        return f"{text[:240]}...(+{len(text) - 240} chars)"
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value[:20]]
    if isinstance(value, dict):
        return {str(k): _safe_value(v) for k, v in list(value.items())[:40]}
    return str(value)


def _safe_meta(meta: dict[str, Any] | None) -> dict[str, Any]:
    if not meta:
        return {}
    return {str(key): _safe_value(value) for key, value in meta.items()}


class OperationTraceStore:
    def __init__(self, max_items: int = 80) -> None:
        self._max_items = max(10, int(max_items))
        self._lock = threading.Lock()
        self._items: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def upsert(self, trace: dict[str, Any]) -> None:
        trace_id = str(trace.get("trace_id") or "").strip()
        if not trace_id:
            return
        snapshot = deepcopy(trace)
        with self._lock:
            self._items[trace_id] = snapshot
            self._items.move_to_end(trace_id)
            while len(self._items) > self._max_items:
                self._items.popitem(last=False)

    def list_recent(
        self,
        *,
        limit: int = 20,
        operation: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        op = str(operation or "").strip()
        wanted_status = str(status or "").strip()
        with self._lock:
            values = list(self._items.values())
        out: list[dict[str, Any]] = []
        for item in reversed(values):
            if op and str(item.get("operation") or "") != op:
                continue
            if wanted_status and str(item.get("status") or "") != wanted_status:
                continue
            out.append(deepcopy(item))
            if len(out) >= max(1, int(limit)):
                break
        return out


TRACE_STORE = OperationTraceStore()


class OperationTracer:
    def __init__(
        self,
        logger: logging.Logger,
        operation: str,
        *,
        meta: dict[str, Any] | None = None,
    ) -> None:
        self.logger = logger
        self.operation = operation
        self.trace_id = uuid.uuid4().hex[:10]
        self._started_perf = time.perf_counter()
        self._stages: list[dict[str, Any]] = []
        self._trace: dict[str, Any] = {
            "trace_id": self.trace_id,
            "operation": operation,
            "status": "running",
            "started_at_cst": _now_cst_iso(),
            "finished_at_cst": None,
            "total_ms": None,
            "meta": _safe_meta(meta),
            "current_stage": None,
            "stages": self._stages,
            "error": None,
        }
        TRACE_STORE.upsert(self._trace)
        self.logger.info(
            "[diag][%s][%s] start %s",
            self.operation,
            self.trace_id,
            json.dumps(self._trace["meta"], ensure_ascii=False),
        )

    def add_meta(self, **meta: Any) -> None:
        if not meta:
            return
        base = self._trace.get("meta")
        if not isinstance(base, dict):
            base = {}
        base.update(_safe_meta(meta))
        self._trace["meta"] = base
        TRACE_STORE.upsert(self._trace)

    @contextmanager
    def stage(self, name: str, **meta: Any):
        stage = {
            "name": str(name),
            "status": "running",
            "started_at_cst": _now_cst_iso(),
            "finished_at_cst": None,
            "duration_ms": None,
            "meta": _safe_meta(meta),
            "error": None,
        }
        self._stages.append(stage)
        self._trace["current_stage"] = {
            "name": stage["name"],
            "started_at_cst": stage["started_at_cst"],
            "meta": stage["meta"],
        }
        TRACE_STORE.upsert(self._trace)
        self.logger.info(
            "[diag][%s][%s] stage_start %s %s",
            self.operation,
            self.trace_id,
            stage["name"],
            json.dumps(stage["meta"], ensure_ascii=False),
        )
        started = time.perf_counter()
        try:
            yield stage
        except Exception as exc:
            stage["status"] = "error"
            stage["finished_at_cst"] = _now_cst_iso()
            stage["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
            stage["error"] = {
                "type": exc.__class__.__name__,
                "message": str(exc),
            }
            self._trace["current_stage"] = None
            TRACE_STORE.upsert(self._trace)
            self.logger.exception(
                "[diag][%s][%s] stage_error %s %sms",
                self.operation,
                self.trace_id,
                stage["name"],
                stage["duration_ms"],
            )
            raise
        else:
            stage["status"] = "ok"
            stage["finished_at_cst"] = _now_cst_iso()
            stage["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
            self._trace["current_stage"] = None
            TRACE_STORE.upsert(self._trace)
            self.logger.info(
                "[diag][%s][%s] stage_done %s %sms",
                self.operation,
                self.trace_id,
                stage["name"],
                stage["duration_ms"],
            )

    async def run(self, name: str, awaitable, **meta: Any):
        with self.stage(name, **meta):
            return await awaitable

    def finish_ok(self, **meta: Any) -> None:
        self.add_meta(**meta)
        total_ms = round((time.perf_counter() - self._started_perf) * 1000, 1)
        self._trace["status"] = "ok"
        self._trace["finished_at_cst"] = _now_cst_iso()
        self._trace["total_ms"] = total_ms
        self._trace["current_stage"] = None
        TRACE_STORE.upsert(self._trace)
        self.logger.info(
            "[diag][%s][%s] finish_ok total=%sms stages=%s",
            self.operation,
            self.trace_id,
            total_ms,
            json.dumps(self._stage_summary(), ensure_ascii=False),
        )

    def finish_error(self, exc: BaseException, **meta: Any) -> None:
        self.add_meta(**meta)
        total_ms = round((time.perf_counter() - self._started_perf) * 1000, 1)
        self._trace["status"] = "error"
        self._trace["finished_at_cst"] = _now_cst_iso()
        self._trace["total_ms"] = total_ms
        self._trace["error"] = {
            "type": exc.__class__.__name__,
            "message": str(exc),
        }
        TRACE_STORE.upsert(self._trace)
        self.logger.exception(
            "[diag][%s][%s] finish_error total=%sms stages=%s",
            self.operation,
            self.trace_id,
            total_ms,
            json.dumps(self._stage_summary(), ensure_ascii=False),
        )

    def _stage_summary(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for stage in self._stages:
            out.append(
                {
                    "name": stage.get("name"),
                    "status": stage.get("status"),
                    "duration_ms": stage.get("duration_ms"),
                }
            )
        return out
