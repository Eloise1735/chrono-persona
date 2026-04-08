from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

from server.diagnostics import TRACE_STORE
from server.llm_client import LLMTimeoutError, LLMTransportError, LLMUpstreamHTTPError
from server.models import (
    CreateSnapshotRequest,
    CreateEventRequest,
    UpdateEventRequest,
    CreateKeyRecordRequest,
    UpdateKeyRecordRequest,
    MemorySearchRequest,
    GetCurrentStateRequest,
    ReflectRequest,
    SummarizeConversationRequest,
    PeriodicReviewRequest,
    KeyRecordSearchRequest,
    UpdateSettingRequest,
    EvolutionApplyRequest,
    RecalculateArchiveRequest,
    EvolutionRescoreRequest,
    UpdateVectorSettingsRequest,
    VectorSyncRequest,
    VectorCompactRequest,
    VectorBatchDeleteRequest,
    UpdateRuntimeLLMRequest,
    WorldBookCreateRequest,
    WorldBookUpdateRequest,
    WorldBookAutoMetaRequest,
    WorldBookJsonImportRequest,
    UpsertModelPricingRequest,
    BulkImportRequest,
    SnapshotTimezoneRepairRequest,
    StateSnapshot,
    EventAnchor,
    KeyRecord,
    WorldBook,
    format_utc_instant_z,
)
from server.prompts import DEFAULT_SETTINGS, KEY_MODEL_PRICING_JSON
from server.time_display import (
    normalize_user_instant_to_utc_z,
    shanghai_now,
    shanghai_time_to_utc_naive,
    utc_naive_to_shanghai_iso,
)
from server.event_taxonomy import classify_event, make_event_title
from server.world_book_import import parse_world_book_import

router = APIRouter(prefix="/api")

_db = None
_state_machine = None
_memory_store = None
_prompt_manager = None
_evolution_engine = None
_llm_client = None
_env_llm_client = None
_snapshot_llm_client = None


def _require_state_machine():
    if _state_machine is None:
        raise HTTPException(503, "State machine is not initialized. Restart the server and wait for startup to complete.")
    return _state_machine


def _to_json_array_text(value) -> str:
    if value is None:
        return "[]"
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return "[]"
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return json.dumps(parsed, ensure_ascii=False)
        except Exception:
            pass
        # fallback: comma-separated string
        items = [x.strip() for x in text.replace("，", ",").split(",") if x.strip()]
        return json.dumps(items, ensure_ascii=False)
    return "[]"


def _to_int_flag(value, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return 1 if int(value) else 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return 1
    if text in {"0", "false", "no", "n", "off"}:
        return 0
    return default


def _normalize_optional_instant_to_utc_z(value, fallback: str) -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    return normalize_user_instant_to_utc_z(text)

def _parse_pricing_table(json_str: str) -> dict[str, dict[str, float]]:
    """Parse a JSON pricing string into a normalized {model: {prompt, completion}} table."""
    try:
        raw = json.loads(json_str or "{}")
    except Exception:
        raw = {}
    result: dict[str, dict[str, float]] = {}
    for model, prices in raw.items():
        if not isinstance(prices, dict):
            continue
        normalized = str(model).strip().lower().replace("_", "-")
        if normalized:
            result[normalized] = {
                "prompt": float(prices.get("prompt") or 0),
                "completion": float(prices.get("completion") or 0),
            }
    return result


def _resolve_model_pricing(
    model_name: str,
    pricing_table: dict[str, dict[str, float]],
) -> dict[str, float] | None:
    normalized = str(model_name or "").strip().lower().replace("_", "-")
    if not normalized:
        return None
    if normalized in pricing_table:
        return pricing_table[normalized]
    for key, price in pricing_table.items():
        if normalized.startswith(key):
            return price
    return None


def _estimate_cost_usd(prompt_tokens: int, completion_tokens: int, pricing: dict[str, float]) -> float:
    return (
        (float(prompt_tokens) / 1_000_000.0) * float(pricing.get("prompt", 0.0))
        + (float(completion_tokens) / 1_000_000.0) * float(pricing.get("completion", 0.0))
    )


def _pricing_table_to_json(pricing_table: dict[str, dict[str, float]]) -> str:
    payload = {
        str(model): {
            "prompt": float(prices.get("prompt") or 0),
            "completion": float(prices.get("completion") or 0),
        }
        for model, prices in pricing_table.items()
    }
    return json.dumps(payload, ensure_ascii=False)


async def _get_model_pricing_table() -> dict[str, dict[str, float]]:
    pricing_json = ""
    if _prompt_manager is not None:
        try:
            pricing_json = await _prompt_manager.get_config_value(KEY_MODEL_PRICING_JSON)
        except Exception:
            pricing_json = ""
    if not str(pricing_json).strip():
        pricing_json = DEFAULT_SETTINGS.get(KEY_MODEL_PRICING_JSON, {}).get("value", "{}")
    return _parse_pricing_table(pricing_json)


async def _generate_event_meta_by_summary_llm(
    description: str,
    categories: list[str] | None = None,
) -> dict:
    if _llm_client is None:
        return {}
    desc = (description or "").strip()
    if not desc:
        return {}
    cat_text = ", ".join([c for c in (categories or []) if str(c).strip()]) or "无"
    prompt = (
        "你是事件元信息提取助手。请只输出 JSON，不要输出其他文本。"
        "JSON格式：{\"title\": string, \"keywords\": string[]}。"
        "要求：title 8-24 字且具体；keywords 4-8 个，可检索、尽量实体化。"
        "不要空数组。\n\n"
        f"事件描述：{desc}\n"
        f"事件分类：{cat_text}"
    )
    try:
        response = await _llm_client.chat(
            [
                {"role": "system", "content": "你是严谨的结构化信息提取助手。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=220,
        )
        parsed = _extract_json_object(response)
        title = str(parsed.get("title") or "").strip()
        keywords = _parse_json_list(parsed.get("keywords"))
        return {"title": title, "keywords": keywords}
    except Exception:
        return {}


def set_dependencies(
    db,
    state_machine,
    memory_store,
    prompt_manager=None,
    evolution_engine=None,
    llm_client=None,
    env_llm_client=None,
    snapshot_llm_client=None,
):
    global _db, _state_machine, _memory_store, _prompt_manager, _evolution_engine
    global _llm_client, _env_llm_client, _snapshot_llm_client
    _db = db
    _state_machine = state_machine
    _memory_store = memory_store
    _prompt_manager = prompt_manager
    _evolution_engine = evolution_engine
    _llm_client = llm_client
    _env_llm_client = env_llm_client
    _snapshot_llm_client = snapshot_llm_client


async def _ensure_event_meta(event: EventAnchor) -> EventAnchor:
    fields = {}
    title = (event.title or "").strip()
    if not title:
        title = ""
    try:
        categories = json.loads(event.categories or "[]")
    except Exception:
        categories = []
    try:
        keywords = json.loads(event.trigger_keywords or "[]")
    except Exception:
        keywords = []
    if not categories:
        categories = classify_event(event.description, keywords)
        fields["categories"] = json.dumps(categories, ensure_ascii=False)
    if not title:
        title = make_event_title(event.description, keywords, categories)
        fields["title"] = title
    if fields and event.id is not None:
        await _db.update_event(int(event.id), **fields)
    event.title = title
    event.categories = json.dumps(categories, ensure_ascii=False)
    return event


def _ensure_vector_store():
    if _memory_store is None:
        raise HTTPException(500, "Memory store is not initialized")
    required_methods = [
        "get_runtime_config",
        "update_runtime_config",
        "sync_eligible_vectors",
        "get_vector_stats",
        "list_vectors",
        "remove_vector",
        "compact_cold_memories",
    ]
    missing = [name for name in required_methods if not hasattr(_memory_store, name)]
    if missing:
        raise HTTPException(
            400,
            "Current memory_store does not support vector management. "
            "Set memory_store.type = 'vector' in config.yaml and restart.",
        )
    return _memory_store


def _parse_json_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    raw = str(value).strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(x).strip() for x in parsed if str(x).strip()]
    except Exception:
        pass
    return [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]


def _extract_json_object(text: str) -> dict:
    raw = (text or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        snippet = raw[start : end + 1]
        try:
            data = json.loads(snippet)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _serialize_world_book(item: WorldBook) -> dict:
    data = item.model_dump()
    data["tags"] = _parse_json_list(item.tags)
    data["match_keywords"] = _parse_json_list(item.match_keywords)
    data["is_active"] = bool(int(item.is_active or 0))
    data["vectorized"] = bool(str(item.embedding_vector_id or "").strip())
    return data


async def _get_llm_config(prefix: str) -> dict:
    enabled = await _db.get_setting(f"{prefix}_enabled")
    api_base = await _db.get_setting(f"{prefix}_api_base")
    api_key = await _db.get_setting(f"{prefix}_api_key")
    model = await _db.get_setting(f"{prefix}_model")
    return {
        "enabled": str((enabled or {}).get("value", "0")) == "1",
        "api_base": str((api_base or {}).get("value", "")),
        "api_key": str((api_key or {}).get("value", "")),
        "model": str((model or {}).get("value", "")),
    }


async def _save_llm_config(prefix: str, payload: dict):
    meta_map = {
        "enabled": f"{prefix}_enabled",
        "api_base": f"{prefix}_api_base",
        "api_key": f"{prefix}_api_key",
        "model": f"{prefix}_model",
    }
    defaults = {
        "enabled": "0",
        "api_base": "",
        "api_key": "",
        "model": "",
    }
    for key, setting_key in meta_map.items():
        if key not in payload:
            continue
        value = payload.get(key, defaults[key])
        if key == "enabled":
            value = "1" if _to_int_flag(value, 0) == 1 else "0"
        await _db.set_setting(
            key=setting_key,
            value=str(value or ""),
            category="runtime",
            description=DEFAULT_SETTINGS.get(setting_key, {}).get("description", ""),
        )


# ── State Machine endpoints (mirror MCP tools for web testing) ──

@router.post("/state/current")
async def api_get_current_state(req: GetCurrentStateRequest):
    sm = _require_state_machine()
    try:
        out = await sm.get_current_state(
            req.current_time,
            req.last_interaction_time,
            return_schedule=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        # LLMClient.chat 在网关错误/限流/非 JSON 响应时抛出，原先会变成笼统的 500
        logger.warning("get_current_state LLM/runtime error: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if isinstance(out, tuple):
        content, schedule = out
        pending = await _evolution_engine.get_pending_preview() if _evolution_engine is not None else None
        if pending:
            content = (
                f"{content}\n\n"
                f"[系统提示：后台已生成一份待确认的人格演化预览（新事件 {pending.get('event_count')} 条，"
                f"候选 {pending.get('evolution_prompt_event_count', 0)} 条）。"
                "请提醒用户前往 Web 前端的“人格演化”页面查看预览并手动确认应用。]"
            )
        generated_count = len(schedule.get("generated_snapshots") or [])
        payload = {
            "content": content,
            "generated_snapshot_count": generated_count,
            "input_current_time_cst": schedule.get("input_current_time_cst"),
            "input_last_interaction_cst": schedule.get("input_last_interaction_cst"),
        }
        if req.include_checkpoint_schedule:
            payload["checkpoint_schedule"] = schedule
        return payload
    return {"content": out, "generated_snapshot_count": 0}


@router.post("/state/reflect")
async def api_reflect(req: ReflectRequest):
    sm = _require_state_machine()
    try:
        result = await sm.reflect_on_conversation(req.conversation_summary)
    except RuntimeError as exc:
        logger.warning("reflect_on_conversation LLM/runtime error: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"content": result}


@router.get("/debug/operation-traces")
async def api_operation_traces(limit: int = 20, operation: str | None = None, status: str | None = None):
    capped_limit = max(1, min(int(limit or 20), 100))
    return {
        "items": TRACE_STORE.list_recent(
            limit=capped_limit,
            operation=operation,
            status=status,
        )
    }


@router.post("/state/summarize")
async def api_summarize_conversation(req: SummarizeConversationRequest):
    sm = _require_state_machine()
    result = await sm.summarize_conversation(req.conversation_text)
    return {"summary": result}


@router.post("/memories/search")
async def api_search_memories(req: MemorySearchRequest):
    results = await _state_machine.recall_memories(req.query, top_k=req.top_k)
    return {"results": results}


@router.post("/review/periodic")
async def api_periodic_review(req: PeriodicReviewRequest):
    try:
        start = datetime.fromisoformat(req.start_date).date()
        end = datetime.fromisoformat(req.end_date).date()
    except ValueError as exc:
        raise HTTPException(400, f"Invalid date format: {exc}") from exc
    if start > end:
        raise HTTPException(400, "Invalid date range: start_date must be <= end_date")
    result = await _state_machine.generate_periodic_review(
        start.strftime("%Y-%m-%d"),
        end.strftime("%Y-%m-%d"),
        include_archived=req.include_archived,
    )
    return result


# ── Key Records ──

@router.get("/key-records")
async def list_key_records(
    offset: int = 0,
    limit: int = 50,
    record_type: str | None = None,
    status: str | None = None,
    include_archived: bool = False,
):
    items = await _db.get_all_key_records(
        offset=offset,
        limit=limit,
        record_type=record_type,
        status=status,
        include_archived=include_archived,
    )
    return {"items": [i.model_dump() for i in items]}


@router.post("/key-records/search")
async def search_key_records(req: KeyRecordSearchRequest):
    items = await _state_machine.recall_key_records(
        query=req.query,
        top_k=req.top_k,
        record_type=req.type,
        include_archived=req.include_archived,
        include_world_books=req.include_world_books,
    )
    return {"items": items}


@router.get("/key-records/{record_id}")
async def get_key_record(record_id: int):
    item = await _db.get_key_record_by_id(record_id)
    if not item:
        raise HTTPException(404, "Key record not found")
    return item.model_dump()


@router.post("/key-records")
async def create_key_record(req: CreateKeyRecordRequest):
    now = datetime.utcnow().isoformat()
    item = KeyRecord(
        type=req.type,
        title=req.title.strip(),
        content_text=req.content_text.strip(),
        content_json=json.dumps(req.content_json, ensure_ascii=False) if req.content_json is not None else None,
        tags=json.dumps(req.tags, ensure_ascii=False),
        start_date=req.start_date,
        end_date=req.end_date,
        status=req.status,
        source=req.source,
        linked_event_id=req.linked_event_id,
        created_at=now,
        updated_at=now,
    )
    record_id = await _db.insert_key_record(item)
    return {"id": record_id, "message": "Key record created"}


@router.put("/key-records/{record_id}")
async def update_key_record(record_id: int, req: UpdateKeyRecordRequest):
    item = await _db.get_key_record_by_id(record_id)
    if not item:
        raise HTTPException(404, "Key record not found")
    fields = {}
    if req.type is not None:
        fields["type"] = req.type
    if req.title is not None:
        fields["title"] = req.title.strip()
    if req.content_text is not None:
        fields["content_text"] = req.content_text.strip()
    if req.content_json is not None:
        fields["content_json"] = json.dumps(req.content_json, ensure_ascii=False)
    if req.tags is not None:
        fields["tags"] = json.dumps(req.tags, ensure_ascii=False)
    if req.start_date is not None:
        fields["start_date"] = req.start_date
    if req.end_date is not None:
        fields["end_date"] = req.end_date
    if req.status is not None:
        fields["status"] = req.status
    if req.source is not None:
        fields["source"] = req.source
    if req.linked_event_id is not None:
        fields["linked_event_id"] = req.linked_event_id
    if fields:
        await _db.update_key_record(record_id, **fields)
    return {"message": "Key record updated"}


@router.delete("/key-records/{record_id}")
async def delete_key_record(record_id: int):
    item = await _db.get_key_record_by_id(record_id)
    if not item:
        raise HTTPException(404, "Key record not found")
    await _db.delete_key_record(record_id)
    return {"message": "Key record deleted"}


@router.get("/world-books")
async def list_world_books(offset: int = 0, limit: int = 100):
    items = await _db.list_world_books(offset=offset, limit=limit)
    return {"items": [_serialize_world_book(i) for i in items]}


@router.get("/world-books/{item_id}")
async def get_world_book(item_id: int):
    item = await _db.get_world_book_by_id(item_id)
    if not item:
        raise HTTPException(404, "World book item not found")
    return _serialize_world_book(item)


@router.post("/world-books")
async def create_world_book(req: WorldBookCreateRequest):
    now = datetime.utcnow().isoformat()
    item = WorldBook(
        name=req.name.strip(),
        content=req.content.strip(),
        tags=json.dumps(req.tags, ensure_ascii=False),
        match_keywords=json.dumps(req.match_keywords, ensure_ascii=False),
        is_active=1 if req.is_active else 0,
        created_at=now,
        updated_at=now,
    )
    item_id = await _db.insert_world_book(item)
    return {"id": item_id, "message": "World book created"}


@router.post("/world-books/import-json")
async def import_world_books_json(req: WorldBookJsonImportRequest):
    items, warnings = parse_world_book_import(
        req.data, skip_disabled=req.skip_disabled
    )
    if not items:
        detail = "; ".join(warnings) if warnings else "未能解析出任何有效条目（内容为空或格式不匹配）"
        raise HTTPException(400, detail)
    now = datetime.utcnow().isoformat()
    ids: list[int] = []
    for it in items:
        wb = WorldBook(
            name=str(it["name"] or "未命名")[:500],
            content=str(it["content"] or "").strip(),
            tags=json.dumps(it.get("tags") or [], ensure_ascii=False),
            match_keywords=json.dumps(it.get("match_keywords") or [], ensure_ascii=False),
            is_active=1 if it.get("is_active", True) else 0,
            created_at=now,
            updated_at=now,
        )
        ids.append(await _db.insert_world_book(wb))
    return {
        "created": len(ids),
        "ids": ids,
        "warnings": warnings,
    }


@router.put("/world-books/{item_id}")
async def update_world_book(item_id: int, req: WorldBookUpdateRequest):
    item = await _db.get_world_book_by_id(item_id)
    if not item:
        raise HTTPException(404, "World book item not found")
    fields = {}
    if req.name is not None:
        fields["name"] = req.name.strip()
    if req.content is not None:
        fields["content"] = req.content.strip()
    if req.tags is not None:
        fields["tags"] = json.dumps(req.tags, ensure_ascii=False)
    if req.match_keywords is not None:
        fields["match_keywords"] = json.dumps(req.match_keywords, ensure_ascii=False)
    if req.is_active is not None:
        fields["is_active"] = 1 if req.is_active else 0
    should_revectorize = False
    if fields:
        if any(k in fields for k in ("name", "content", "tags", "match_keywords")):
            should_revectorize = True
        await _db.update_world_book(item_id, **fields)
    if should_revectorize:
        upsert_method = getattr(_memory_store, "upsert_world_book_vector", None)
        if callable(upsert_method) and str(item.embedding_vector_id or "").strip():
            await upsert_method(item_id)
    return {"message": "World book updated"}


@router.delete("/world-books/{item_id}")
async def delete_world_book(item_id: int):
    item = await _db.get_world_book_by_id(item_id)
    if not item:
        raise HTTPException(404, "World book item not found")
    delete_method = getattr(_memory_store, "delete_world_book_vector", None)
    if callable(delete_method):
        await delete_method(item_id)
    await _db.delete_world_book(item_id)
    return {"message": "World book deleted"}


@router.post("/world-books/{item_id}/vectorize")
async def vectorize_world_book(item_id: int):
    item = await _db.get_world_book_by_id(item_id)
    if not item:
        raise HTTPException(404, "World book item not found")
    upsert_method = getattr(_memory_store, "upsert_world_book_vector", None)
    if not callable(upsert_method):
        raise HTTPException(400, "Current memory store does not support world book vectorization")
    ok = await upsert_method(item_id)
    if not ok:
        raise HTTPException(500, "World book vectorization failed")
    return {"message": "World book vectorized"}


@router.delete("/world-books/{item_id}/vector")
async def delete_world_book_vector(item_id: int):
    item = await _db.get_world_book_by_id(item_id)
    if not item:
        raise HTTPException(404, "World book item not found")
    delete_method = getattr(_memory_store, "delete_world_book_vector", None)
    if callable(delete_method):
        await delete_method(item_id)
    else:
        await _db.clear_world_book_vectorized(item_id)
    return {"message": "World book vector removed"}


@router.post("/world-books/vector-sync")
async def sync_world_book_vectors(limit: int = 200):
    sync_method = getattr(_memory_store, "sync_world_book_vectors", None)
    if not callable(sync_method):
        raise HTTPException(400, "Current memory store does not support world book vectorization")
    result = await sync_method(limit=max(1, limit))
    return {"message": "World book vector sync completed", "result": result}


@router.post("/world-books/auto-meta")
async def auto_fill_world_book_meta(req: WorldBookAutoMetaRequest):
    if _llm_client is None:
        raise HTTPException(500, "LLM client is not initialized")
    if _db is None:
        raise HTTPException(500, "Database is not initialized")

    target_ids = [int(x) for x in req.item_ids if int(x) > 0]
    if target_ids:
        items = await _db.get_world_books_by_ids(target_ids)
    else:
        items = await _db.list_world_books(offset=0, limit=500)

    processed = 0
    updated = 0
    failed = 0
    details: list[dict] = []

    for item in items:
        processed += 1
        name = str(item.name or "").strip()
        content = str(item.content or "").strip()
        if not content:
            failed += 1
            details.append({"id": item.id, "status": "failed", "reason": "empty content"})
            continue
        need_title = req.overwrite_title or (not name)
        existing_keywords = _parse_json_list(item.match_keywords)
        need_keywords = req.overwrite_keywords or (not existing_keywords)
        if not need_title and not need_keywords:
            details.append({"id": item.id, "status": "skipped", "reason": "already has meta"})
            continue

        prompt = (
            "你是世界书整理助手。请只输出 JSON，不要输出额外文本。"
            "JSON 结构为：{\"title\": string, \"keywords\": string[]}。"
            "要求：title 8-30 字，简洁具体；keywords 6-12 个，偏可检索实体词/术语词。"
            "禁止空数组。\n\n"
            f"世界书内容：\n{content}"
        )
        try:
            response = await _llm_client.chat(
                [
                    {"role": "system", "content": "你是严谨的信息抽取助手。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                max_tokens=300,
            )
            parsed = _extract_json_object(response)
            suggested_title = str(parsed.get("title") or "").strip()
            suggested_keywords = _parse_json_list(parsed.get("keywords"))

            if not suggested_title:
                suggested_title = (content.split("。")[0].strip() or content[:24].strip())[:30]
            if not suggested_keywords:
                tokens = _parse_json_list(item.tags)
                suggested_keywords = tokens[:8]
                if not suggested_keywords:
                    suggested_keywords = [w for w in content.replace("，", ",").replace("。", ",").split(",") if w.strip()][:8]

            fields = {}
            if need_title and suggested_title:
                fields["name"] = suggested_title
            if need_keywords and suggested_keywords:
                fields["match_keywords"] = json.dumps(suggested_keywords, ensure_ascii=False)

            if fields:
                await _db.update_world_book(int(item.id or 0), **fields)
                upsert_method = getattr(_memory_store, "upsert_world_book_vector", None)
                if callable(upsert_method) and str(item.embedding_vector_id or "").strip():
                    await upsert_method(int(item.id or 0))
                updated += 1
                details.append(
                    {
                        "id": item.id,
                        "status": "updated",
                        "name": fields.get("name", item.name),
                        "match_keywords": suggested_keywords if "match_keywords" in fields else existing_keywords,
                    }
                )
            else:
                details.append({"id": item.id, "status": "skipped", "reason": "no generated fields"})
        except Exception as exc:
            failed += 1
            details.append({"id": item.id, "status": "failed", "reason": str(exc)})

    return {
        "message": "World book auto meta completed",
        "processed": processed,
        "updated": updated,
        "failed": failed,
        "details": details,
        "model_source": "runtime_llm",
    }


@router.get("/environment/history")
async def list_environment_history(offset: int = 0, limit: int = 50, include_empty: bool = False):
    snapshots = await _db.get_all_snapshots(offset=offset, limit=limit)
    items: list[dict] = []
    for snap in snapshots:
        env_raw = str(snap.environment or "{}").strip() or "{}"
        try:
            env_obj = json.loads(env_raw)
            if not isinstance(env_obj, dict):
                env_obj = {}
        except Exception:
            env_obj = {}
        summary = str(env_obj.get("summary") or "")
        activity = str(env_obj.get("activity") or "")
        if not include_empty and not (summary or activity):
            continue
        items.append(
            {
                "snapshot_id": snap.id,
                "created_at": snap.created_at,
                "type": snap.type,
                "summary": summary,
                "activity": activity,
                "continuity": str(env_obj.get("continuity") or ""),
                "environment": env_obj,
            }
        )
    return {"items": items}


@router.get("/dashboard/idle-snapshot-summary")
async def get_idle_snapshot_summary():
    """仪表盘：距最新快照时间、自上次「对话结束」快照以来的增量统计、后台调度器开关状态。"""
    if _db is None:
        raise HTTPException(500, "Database is not initialized")
    sm = _require_state_machine()
    latest = await _db.get_latest_snapshot()
    conv = await _db.get_latest_snapshot_by_type("conversation_end")
    since = str(conv.created_at).strip() if conv and conv.created_at else ""
    snap_n: int | None = None
    evt_n: int | None = None
    if since:
        snap_n = await _db.count_snapshots_since(since)
        evt_n = await _db.count_events_since(since)
    sched = await sm.get_snapshot_scheduler_public_info()
    now_u = datetime.utcnow()
    latest_d = latest.model_dump() if latest else None
    last_conv = None
    if conv:
        lc = conv.model_dump()
        last_conv = {
            "id": lc["id"],
            "type": lc["type"],
            "created_at": lc["created_at"],
            "inserted_at": lc.get("inserted_at"),
            "created_at_cst": lc["created_at_cst"],
            "inserted_at_cst": lc.get("inserted_at_cst"),
        }
    return {
        "server_now_cst": utc_naive_to_shanghai_iso(now_u),
        "latest_snapshot": latest_d,
        "last_conversation_end": last_conv,
        "snapshots_since_conversation_end": snap_n,
        "events_since_conversation_end": evt_n,
        "snapshot_scheduler": sched,
    }


# ── Snapshots CRUD ──

@router.get("/snapshots")
async def list_snapshots(offset: int = 0, limit: int = 50):
    snapshots = await _db.get_all_snapshots(offset=offset, limit=limit)
    total = await _db.count_snapshots()
    return {"items": [s.model_dump() for s in snapshots], "total": total}


@router.get("/snapshots/latest")
async def get_latest_snapshot():
    snap = await _db.get_latest_snapshot()
    if not snap:
        return {"snapshot": None}
    return {"snapshot": snap.model_dump()}


@router.get("/snapshots/{snap_id}")
async def get_snapshot(snap_id: int):
    snap = await _db.get_snapshot_by_id(snap_id)
    if not snap:
        raise HTTPException(404, "Snapshot not found")
    return snap.model_dump()


@router.post("/snapshots")
async def create_snapshot(req: CreateSnapshotRequest):
    snap = StateSnapshot(
        created_at=format_utc_instant_z(datetime.utcnow()),
        type=req.type,
        content=req.content,
        environment=req.environment,
    )
    snap_id = await _db.insert_snapshot(snap)
    return {"id": snap_id, "message": "Snapshot created"}


@router.post("/snapshots/repair-timezone")
async def repair_snapshot_timezone(req: SnapshotTimezoneRepairRequest):
    if _db is None:
        raise HTTPException(500, "Database is not initialized")
    return await _db.repair_snapshot_timezones(dry_run=req.dry_run)


@router.delete("/snapshots/{snap_id}")
async def delete_snapshot(snap_id: int):
    snap = await _db.get_snapshot_by_id(snap_id)
    if not snap:
        raise HTTPException(404, "Snapshot not found")
    await _db.delete_snapshot(snap_id)
    return {"message": "Snapshot deleted"}


# ── Events CRUD ──

@router.get("/events")
async def list_events(
    offset: int = 0,
    limit: int = 50,
    include_archived: bool = False,
    categories: str | None = None,
):
    category_list = [c.strip() for c in (categories or "").split(",") if c.strip()]
    events = await _db.get_all_events(
        offset=offset,
        limit=limit,
        include_archived=include_archived,
        categories=category_list,
    )
    normalized = [await _ensure_event_meta(e) for e in events]
    return {"items": [e.model_dump() for e in normalized]}


@router.get("/events/{event_id}")
async def get_event(event_id: int):
    event = await _db.get_event_by_id(event_id)
    if not event:
        raise HTTPException(404, "Event not found")
    return event.model_dump()


@router.post("/events")
async def create_event(req: CreateEventRequest):
    categories = req.categories if req.categories is not None else classify_event(req.description, req.trigger_keywords)
    title = (req.title or "").strip()
    keywords = list(req.trigger_keywords or [])

    if not title or not keywords:
        meta = await _generate_event_meta_by_summary_llm(
            description=req.description,
            categories=categories,
        )
        if not title:
            title = str(meta.get("title") or "").strip()
        if not keywords:
            generated_keywords = _parse_json_list(meta.get("keywords"))
            if generated_keywords:
                keywords = generated_keywords

    title = title or make_event_title(req.description, keywords, categories)
    now_shanghai = shanghai_now()
    event = EventAnchor(
        date=req.date or now_shanghai.date().isoformat(),
        title=title,
        description=req.description,
        source=req.source,
        created_at=format_utc_instant_z(shanghai_time_to_utc_naive(now_shanghai)),
        trigger_keywords=json.dumps(keywords, ensure_ascii=False),
        categories=json.dumps(categories, ensure_ascii=False),
    )
    event_id = await _db.insert_event(event)
    upsert_event_vector = getattr(_memory_store, "upsert_event_vector", None)
    if callable(upsert_event_vector):
        await upsert_event_vector(int(event_id))
    else:
        sync_method = getattr(_memory_store, "sync_eligible_vectors", None)
        if callable(sync_method):
            await sync_method()
    return {"id": event_id, "message": "Event created"}


@router.put("/events/{event_id}")
async def update_event(event_id: int, req: UpdateEventRequest):
    event = await _db.get_event_by_id(event_id)
    if not event:
        raise HTTPException(404, "Event not found")

    fields = {}
    if req.title is not None:
        fields["title"] = req.title
    if req.description is not None:
        fields["description"] = req.description
    if req.trigger_keywords is not None:
        fields["trigger_keywords"] = json.dumps(req.trigger_keywords, ensure_ascii=False)
    if req.categories is not None:
        fields["categories"] = json.dumps(req.categories, ensure_ascii=False)
    if req.archived is not None:
        fields["archived"] = req.archived
    if req.importance_score is not None:
        fields["importance_score"] = req.importance_score
    if req.impression_depth is not None:
        fields["impression_depth"] = req.impression_depth

    description = req.description if req.description is not None else event.description
    try:
        existing_keywords = json.loads(event.trigger_keywords or "[]")
    except Exception:
        existing_keywords = []
    keywords = req.trigger_keywords if req.trigger_keywords is not None else existing_keywords
    current_title = (req.title if req.title is not None else event.title) or ""

    if req.categories is None and (req.description is not None or req.trigger_keywords is not None):
        fields["categories"] = json.dumps(classify_event(description, keywords), ensure_ascii=False)

    # Auto-generate event title/keywords via runtime summary-LLM when missing.
    if (
        req.description is not None or req.trigger_keywords is not None or req.categories is not None
    ) and (not str(current_title).strip() or not keywords):
        try:
            category_for_meta = (
                req.categories
                if req.categories is not None
                else json.loads(fields.get("categories", event.categories or "[]"))
            )
        except Exception:
            category_for_meta = []
        meta = await _generate_event_meta_by_summary_llm(description, category_for_meta)
        if not str(current_title).strip():
            generated_title = str(meta.get("title") or "").strip()
            if generated_title:
                fields["title"] = generated_title
                current_title = generated_title
        if not keywords:
            generated_keywords = _parse_json_list(meta.get("keywords"))
            if generated_keywords:
                keywords = generated_keywords
                fields["trigger_keywords"] = json.dumps(generated_keywords, ensure_ascii=False)

    if not str(current_title).strip():
        try:
            category_for_title = (
                req.categories
                if req.categories is not None
                else json.loads(fields.get("categories", event.categories or "[]"))
            )
        except Exception:
            category_for_title = []
        fields["title"] = make_event_title(description, keywords, category_for_title)

    if fields:
        await _db.update_event(event_id, **fields)
        upsert_event_vector = getattr(_memory_store, "upsert_event_vector", None)
        if callable(upsert_event_vector):
            await upsert_event_vector(event_id)
        elif req.archived == 1:
            sync_method = getattr(_memory_store, "sync_eligible_vectors", None)
            if callable(sync_method):
                await sync_method()
    return {"message": "Event updated"}


@router.delete("/events/{event_id}")
async def delete_event(event_id: int):
    event = await _db.get_event_by_id(event_id)
    if not event:
        raise HTTPException(404, "Event not found")
    await _db.delete_event(event_id)
    await _memory_store.delete(f"event_{event_id}")
    return {"message": "Event deleted"}


# ── Keyword search ──

@router.get("/search")
async def search(q: str, limit: int = 10, include_archived: bool = False):
    events = await _db.search_events_by_keyword(
        q, limit=limit, include_archived=include_archived
    )
    snapshots = await _db.search_snapshots_by_keyword(q, limit=limit)
    return {
        "events": [(await _ensure_event_meta(e)).model_dump() for e in events],
        "snapshots": [s.model_dump() for s in snapshots],
    }


# ── Vector Memory Management ──

@router.get("/vectors/stats")
async def vector_stats():
    store = _ensure_vector_store()
    stats = await store.get_vector_stats()
    return {"stats": stats}


@router.get("/vectors/entries")
async def list_vector_entries(
    offset: int = 0,
    limit: int = 50,
    source_type: str | None = None,
    status: str | None = "active",
    tier: str | None = None,
):
    store = _ensure_vector_store()
    items = await store.list_vectors(
        offset=offset,
        limit=limit,
        source_type=source_type,
        status=status,
        tier=tier,
    )
    return {"items": items}


@router.post("/vectors/sync")
async def vector_sync(req: VectorSyncRequest):
    store = _ensure_vector_store()
    if req.reindex:
        result = await store.reindex_all_vectors()
        return {"message": "Vector reindex completed", "result": result}
    result = await store.sync_eligible_vectors()
    return {"message": "Vector sync completed", "result": result}


@router.post("/vectors/compact")
async def vector_compact(req: VectorCompactRequest):
    store = _ensure_vector_store()
    result = await store.compact_cold_memories(dry_run=req.dry_run)
    return {"message": "Vector compaction completed", "result": result}


@router.delete("/vectors/entries/{entry_id}")
async def delete_vector_entry(entry_id: str):
    store = _ensure_vector_store()
    ok = await store.remove_vector(entry_id)
    if not ok:
        raise HTTPException(404, "Vector entry not found")
    return {"message": "Vector entry deleted"}


@router.post("/vectors/entries/batch-delete")
async def batch_delete_vector_entries(req: VectorBatchDeleteRequest):
    store = _ensure_vector_store()
    deleted = 0
    failed = 0
    processed_ids: list[str] = []
    if req.entry_ids:
        seen = set()
        entry_ids = []
        for raw in req.entry_ids:
            entry_id = str(raw or "").strip()
            if not entry_id or entry_id in seen:
                continue
            seen.add(entry_id)
            entry_ids.append(entry_id)
        if req.limit > 0:
            entry_ids = entry_ids[: req.limit]
        for entry_id in entry_ids:
            if await store.remove_vector(entry_id):
                deleted += 1
                processed_ids.append(entry_id)
            else:
                failed += 1
        return {
            "message": "Vector batch delete completed",
            "deleted": deleted,
            "failed": failed,
            "processed_entry_ids": processed_ids,
        }

    items = await store.list_vectors(
        offset=0,
        limit=max(1, int(req.limit)),
        source_type=req.source_type,
        status=req.status,
        tier=req.tier,
    )
    for item in items:
        entry_id = str(item.get("entry_id") or "").strip()
        if not entry_id:
            continue
        if await store.remove_vector(entry_id):
            deleted += 1
            processed_ids.append(entry_id)
        else:
            failed += 1
    return {
        "message": "Vector batch delete completed",
        "deleted": deleted,
        "failed": failed,
        "processed_entry_ids": processed_ids,
    }


@router.get("/vectors/settings")
async def get_vector_settings():
    store = _ensure_vector_store()
    settings = await store.get_runtime_config()
    return {"settings": settings}


@router.put("/vectors/settings")
async def update_vector_settings(req: UpdateVectorSettingsRequest):
    store = _ensure_vector_store()
    payload = req.model_dump(exclude_none=True)
    await store.update_runtime_config(payload)
    return {"message": "Vector settings updated"}


# ── Runtime LLM API config ──

@router.get("/environment/llm-config")
async def get_environment_llm_config():
    settings = await _get_llm_config("env_llm")
    return {"settings": settings}


@router.post("/environment/llm-config")
async def update_environment_llm_config(payload: dict):
    if _env_llm_client is not None:
        await _env_llm_client.update_runtime_config(
            {
                "env_llm_enabled": "1" if _to_int_flag(payload.get("enabled"), 0) == 1 else "0",
                "env_llm_api_base": payload.get("api_base", ""),
                "env_llm_api_key": payload.get("api_key", ""),
                "env_llm_model": payload.get("model", ""),
            }
        )
    else:
        await _save_llm_config("env_llm", payload)
    return {"message": "Environment LLM settings updated"}


@router.get("/snapshot/llm-config")
async def get_snapshot_llm_config():
    settings = await _get_llm_config("snapshot_llm")
    return {"settings": settings}


@router.post("/snapshot/llm-config")
async def update_snapshot_llm_config(payload: dict):
    if _snapshot_llm_client is not None:
        await _snapshot_llm_client.update_runtime_config(
            {
                "snapshot_llm_enabled": "1" if _to_int_flag(payload.get("enabled"), 0) == 1 else "0",
                "snapshot_llm_api_base": payload.get("api_base", ""),
                "snapshot_llm_api_key": payload.get("api_key", ""),
                "snapshot_llm_model": payload.get("model", ""),
            }
        )
    else:
        await _save_llm_config("snapshot_llm", payload)
    return {"message": "Snapshot LLM settings updated"}


@router.get("/runtime/llm")
async def get_runtime_llm():
    if _llm_client is None:
        raise HTTPException(500, "LLM client is not initialized")
    return {"settings": await _llm_client.get_runtime_config()}


@router.put("/runtime/llm")
async def update_runtime_llm(req: UpdateRuntimeLLMRequest):
    if _llm_client is None:
        raise HTTPException(500, "LLM client is not initialized")
    payload = req.model_dump(exclude_none=True)
    await _llm_client.update_runtime_config(payload)
    return {"message": "Runtime LLM settings updated"}


# ── Automation reports ──

@router.get("/automation/latest")
async def get_latest_automation_report():
    row = await _db.get_latest_automation_run()
    if not row:
        return {"item": None}
    try:
        report = json.loads(row.get("report_json") or "{}")
    except Exception:
        report = {}
    row["report"] = report
    return {"item": row}


@router.get("/automation/runs")
async def list_automation_reports(offset: int = 0, limit: int = 20):
    rows = await _db.get_automation_runs(offset=offset, limit=limit)
    items = []
    for row in rows:
        try:
            report = json.loads(row.get("report_json") or "{}")
        except Exception:
            report = {}
        row["report"] = report
        items.append(row)
    return {"items": items}


@router.get("/automation/model-pricing")
async def get_automation_model_pricing():
    pricing_table = await _get_model_pricing_table()
    items = [
        {
            "model": model,
            "prompt_price": float(prices.get("prompt") or 0),
            "completion_price": float(prices.get("completion") or 0),
        }
        for model, prices in pricing_table.items()
    ]
    items.sort(key=lambda x: x["model"])
    return {
        "items": items,
        "pricing_unit": "USD / 1M tokens",
    }


@router.post("/automation/model-pricing")
async def upsert_automation_model_pricing(req: UpsertModelPricingRequest):
    model = str(req.model or "").strip().lower().replace("_", "-")
    if not model:
        raise HTTPException(400, "Model name is required")
    pricing_table = await _get_model_pricing_table()
    pricing_table[model] = {
        "prompt": float(req.prompt_price),
        "completion": float(req.completion_price),
    }
    if _db is None:
        raise HTTPException(500, "Database is not initialized")
    meta = DEFAULT_SETTINGS.get(KEY_MODEL_PRICING_JSON, {})
    await _db.set_setting(
        key=KEY_MODEL_PRICING_JSON,
        value=_pricing_table_to_json(pricing_table),
        category=meta.get("category", "runtime"),
        description=meta.get("description", ""),
    )
    return {"message": "Model pricing updated", "model": model}


@router.delete("/automation/model-pricing")
async def delete_automation_model_pricing(model: str):
    model_key = str(model or "").strip().lower().replace("_", "-")
    if not model_key:
        raise HTTPException(400, "Model name is required")
    pricing_table = await _get_model_pricing_table()
    if model_key not in pricing_table:
        raise HTTPException(404, "Model pricing not found")
    pricing_table.pop(model_key, None)
    if _db is None:
        raise HTTPException(500, "Database is not initialized")
    meta = DEFAULT_SETTINGS.get(KEY_MODEL_PRICING_JSON, {})
    await _db.set_setting(
        key=KEY_MODEL_PRICING_JSON,
        value=_pricing_table_to_json(pricing_table),
        category=meta.get("category", "runtime"),
        description=meta.get("description", ""),
    )
    return {"message": "Model pricing removed", "model": model_key}


@router.get("/automation/token-summary")
async def automation_token_summary():
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now.weekday())

    today_rows = await _db.get_automation_runs_since(today_start.isoformat())
    week_rows = await _db.get_automation_runs_since(week_start.isoformat())
    all_rows = await _db.get_automation_runs(offset=0, limit=5000)

    pricing_table = await _get_model_pricing_table()

    async def _sum_usage(rows: list[dict]) -> dict:
        prompt = 0
        completion = 0
        total = 0
        requests = 0
        counted_runs = 0
        estimated_cost_usd = 0.0
        unknown_priced_tokens = 0
        by_model: dict[str, dict] = {}
        fallback_model = ""
        if _llm_client is not None:
            try:
                runtime = await _llm_client.get_runtime_config()
                fallback_model = str(runtime.get("model") or "").strip()
            except Exception:
                fallback_model = ""

        def _upsert_model_usage(model_name: str, usage: dict):
            nonlocal estimated_cost_usd, unknown_priced_tokens
            normalized_name = str(model_name or "unknown").strip() or "unknown"
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
            total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens) or 0)
            req_count = int(usage.get("requests") or 0)
            bucket = by_model.get(normalized_name)
            if not bucket:
                bucket = {
                    "model": normalized_name,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "requests": 0,
                    "estimated_cost_usd": 0.0,
                    "has_pricing": False,
                }
            bucket["prompt_tokens"] = int(bucket.get("prompt_tokens", 0)) + prompt_tokens
            bucket["completion_tokens"] = int(bucket.get("completion_tokens", 0)) + completion_tokens
            bucket["total_tokens"] = int(bucket.get("total_tokens", 0)) + total_tokens
            bucket["requests"] = int(bucket.get("requests", 0)) + req_count

            pricing = _resolve_model_pricing(normalized_name, pricing_table)
            if pricing:
                cost = _estimate_cost_usd(prompt_tokens, completion_tokens, pricing)
                bucket["estimated_cost_usd"] = float(bucket.get("estimated_cost_usd", 0.0)) + cost
                bucket["has_pricing"] = True
                estimated_cost_usd += cost
            else:
                unknown_priced_tokens += total_tokens
            by_model[normalized_name] = bucket

        for row in rows:
            try:
                report = json.loads(row.get("report_json") or "{}")
            except Exception:
                report = {}
            usage = report.get("llm_usage") or {}
            if not usage:
                continue
            counted_runs += 1
            prompt += int(usage.get("prompt_tokens") or 0)
            completion += int(usage.get("completion_tokens") or 0)
            total += int(usage.get("total_tokens") or 0)
            requests += int(usage.get("requests") or 0)
            usage_models = usage.get("models")
            if isinstance(usage_models, dict) and usage_models:
                for model_name, model_usage in usage_models.items():
                    if not isinstance(model_usage, dict):
                        continue
                    _upsert_model_usage(str(model_name), model_usage)
                continue

            legacy_model_usage = {
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
                "requests": int(usage.get("requests") or 0),
            }
            _upsert_model_usage(fallback_model or "unknown", legacy_model_usage)

        model_breakdown = sorted(
            by_model.values(),
            key=lambda item: float(item.get("estimated_cost_usd", 0.0)),
            reverse=True,
        )
        return {
            "runs": counted_runs,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
            "requests": requests,
            "estimated_cost_usd": round(estimated_cost_usd, 6),
            "unknown_priced_tokens": unknown_priced_tokens,
            "model_breakdown": model_breakdown,
        }

    return {
        "today": await _sum_usage(today_rows),
        "week": await _sum_usage(week_rows),
        "all": await _sum_usage(all_rows),
        "pricing_unit": "USD / 1M tokens",
        "generated_at": utc_naive_to_shanghai_iso(now),
    }


# ── Settings ──

@router.get("/settings")
async def list_settings():
    items = await _db.get_all_settings()
    grouped: dict[str, list[dict]] = {}
    for item in items:
        category = item.get("category", "system")
        grouped.setdefault(category, []).append(item)
    defaults = {
        key: meta.get("value", "")
        for key, meta in DEFAULT_SETTINGS.items()
    }
    return {"items": items, "grouped": grouped, "defaults": defaults}


@router.get("/settings/{key}")
async def get_setting(key: str):
    item = await _db.get_setting(key)
    if not item:
        raise HTTPException(404, "Setting not found")
    return item


@router.put("/settings/{key}")
async def update_setting(key: str, req: UpdateSettingRequest):
    if key not in DEFAULT_SETTINGS:
        raise HTTPException(400, "Unsupported setting key")
    meta = DEFAULT_SETTINGS[key]
    await _db.set_setting(
        key=key,
        value=req.value,
        category=meta.get("category", "system"),
        description=meta.get("description", ""),
    )
    return {"message": "Setting updated"}


@router.post("/settings/reset/{key}")
async def reset_setting(key: str):
    if key not in DEFAULT_SETTINGS:
        raise HTTPException(400, "Unsupported setting key")
    if _prompt_manager is None:
        raise HTTPException(500, "Prompt manager is not initialized")
    ok = await _prompt_manager.reset_setting(key)
    if not ok:
        raise HTTPException(404, "Setting default not found")
    return {"message": "Setting reset to default"}


# ── Bulk Import ──

@router.post("/import/bulk")
async def bulk_import(req: BulkImportRequest):
    if _db is None:
        raise HTTPException(500, "Database is not initialized")

    now_iso = format_utc_instant_z(datetime.utcnow())
    result: dict = {
        "settings": {"imported": 0, "skipped": 0, "errors": []},
        "snapshots": {"imported": 0, "skipped": 0, "errors": []},
        "events": {"imported": 0, "skipped": 0, "errors": []},
        "key_records": {"created": 0, "updated": 0, "skipped": 0, "errors": []},
        "vector_sync": None,
    }

    # 1) Settings
    for key, value in (req.settings or {}).items():
        if key not in DEFAULT_SETTINGS:
            result["settings"]["skipped"] += 1
            continue
        try:
            if not req.overwrite_settings:
                existing = await _db.get_setting(key)
                if existing and str(existing.get("value", "")).strip():
                    result["settings"]["skipped"] += 1
                    continue
            meta = DEFAULT_SETTINGS[key]
            await _db.set_setting(
                key=key,
                value=str(value or ""),
                category=meta.get("category", "system"),
                description=meta.get("description", ""),
            )
            result["settings"]["imported"] += 1
        except Exception as exc:
            result["settings"]["errors"].append(f"{key}: {exc}")

    # 2) Snapshots
    for idx, item in enumerate(req.snapshots or []):
        try:
            content = str(item.get("content") or "").strip()
            if not content:
                result["snapshots"]["skipped"] += 1
                continue
            snap_type = str(item.get("type") or "accumulated")
            if snap_type not in {"daily", "conversation_end", "accumulated"}:
                snap_type = "accumulated"
            environment = item.get("environment")
            if isinstance(environment, (dict, list)):
                environment_text = json.dumps(environment, ensure_ascii=False)
            else:
                environment_text = str(environment or "{}")
            referenced_events_text = _to_json_array_text(item.get("referenced_events"))
            snapshot = StateSnapshot(
                created_at=_normalize_optional_instant_to_utc_z(
                    item.get("created_at"),
                    now_iso,
                ),
                type=snap_type,  # type: ignore[arg-type]
                content=content,
                environment=environment_text,
                referenced_events=referenced_events_text,
                embedding_vector_id=item.get("embedding_vector_id"),
            )
            await _db.insert_snapshot(snapshot)
            result["snapshots"]["imported"] += 1
        except Exception as exc:
            result["snapshots"]["errors"].append(f"index={idx}: {exc}")

    # 3) Events
    for idx, item in enumerate(req.events or []):
        try:
            description = str(item.get("description") or "").strip()
            if not description:
                result["events"]["skipped"] += 1
                continue
            keywords_text = _to_json_array_text(item.get("trigger_keywords"))
            categories_text = _to_json_array_text(item.get("categories"))
            try:
                keywords = json.loads(keywords_text)
                if not isinstance(keywords, list):
                    keywords = []
            except Exception:
                keywords = []
            try:
                categories = json.loads(categories_text)
                if not isinstance(categories, list):
                    categories = []
            except Exception:
                categories = []
            if not categories:
                categories = classify_event(description, keywords)
            title = str(item.get("title") or "").strip() or make_event_title(description, keywords, categories)
            source = str(item.get("source") or "manual")
            if source not in {"generated", "manual", "conversation"}:
                source = "manual"
            event = EventAnchor(
                date=str(item.get("date") or datetime.utcnow().strftime("%Y-%m-%d")),
                title=title,
                description=description,
                source=source,  # type: ignore[arg-type]
                created_at=str(item.get("created_at") or now_iso),
                embedding_vector_id=item.get("embedding_vector_id"),
                trigger_keywords=json.dumps(keywords, ensure_ascii=False),
                categories=json.dumps(categories, ensure_ascii=False),
                archived=_to_int_flag(item.get("archived"), 0),
                importance_score=item.get("importance_score"),
                impression_depth=item.get("impression_depth"),
            )
            await _db.insert_event(event)
            result["events"]["imported"] += 1
        except Exception as exc:
            result["events"]["errors"].append(f"index={idx}: {exc}")

    # 4) Key Records
    for idx, item in enumerate(req.key_records or []):
        try:
            record_type = str(item.get("type") or "").strip()
            title = str(item.get("title") or "").strip()
            content_text = str(item.get("content_text") or "").strip()
            if record_type not in {"important_date", "important_item", "key_collaboration", "medical_advice"}:
                result["key_records"]["skipped"] += 1
                continue
            if not title or not content_text:
                result["key_records"]["skipped"] += 1
                continue
            tags_text = _to_json_array_text(item.get("tags"))
            content_json = item.get("content_json")
            if isinstance(content_json, str):
                content_json_text = content_json
            elif content_json is None:
                content_json_text = None
            else:
                content_json_text = json.dumps(content_json, ensure_ascii=False)
            status = str(item.get("status") or "active")
            if status not in {"active", "archived"}:
                status = "active"
            source = str(item.get("source") or "manual")
            if source not in {"manual", "conversation", "generated"}:
                source = "manual"

            if req.upsert_key_records:
                existing = await _db.get_key_record_by_type_title(record_type, title)
                if existing:
                    await _db.update_key_record(
                        int(existing.id),
                        content_text=content_text,
                        content_json=content_json_text,
                        tags=tags_text,
                        start_date=item.get("start_date"),
                        end_date=item.get("end_date"),
                        status=status,
                        source=source,
                        linked_event_id=item.get("linked_event_id"),
                    )
                    result["key_records"]["updated"] += 1
                    continue

            record = KeyRecord(
                type=record_type,  # type: ignore[arg-type]
                title=title,
                content_text=content_text,
                content_json=content_json_text,
                tags=tags_text,
                start_date=item.get("start_date"),
                end_date=item.get("end_date"),
                status=status,  # type: ignore[arg-type]
                source=source,  # type: ignore[arg-type]
                linked_event_id=item.get("linked_event_id"),
                created_at=str(item.get("created_at") or now_iso),
                updated_at=str(item.get("updated_at") or now_iso),
            )
            await _db.insert_key_record(record)
            result["key_records"]["created"] += 1
        except Exception as exc:
            result["key_records"]["errors"].append(f"index={idx}: {exc}")

    # 5) Optional vector sync
    if req.sync_vectors_after_import:
        sync_method = getattr(_memory_store, "sync_eligible_vectors", None)
        if callable(sync_method):
            try:
                result["vector_sync"] = await sync_method()
            except Exception as exc:
                result["vector_sync"] = {"error": str(exc)}

    return result


# ── Evolution ──

@router.get("/evolution/status")
async def evolution_status():
    if _evolution_engine is None:
        raise HTTPException(500, "Evolution engine is not initialized")
    status = await _evolution_engine.check_status()
    pending = await _evolution_engine.get_pending_preview()
    status["has_pending_preview"] = bool(pending)
    status["pending_preview_generated_at"] = (
        pending.get("pending_preview_generated_at") if pending else None
    )
    status["pending_preview_event_count"] = int(pending.get("event_count") or 0) if pending else 0
    status["pending_preview_candidate_count"] = (
        int(pending.get("evolution_prompt_event_count") or 0) if pending else 0
    )
    return status


@router.post("/evolution/preview")
async def evolution_preview():
    if _evolution_engine is None:
        raise HTTPException(500, "Evolution engine is not initialized")
    try:
        return await _evolution_engine.preview(store_pending=True, source="manual")
    except LLMTimeoutError as exc:
        raise HTTPException(504, str(exc)) from exc
    except LLMTransportError as exc:
        status_code = exc.status_code if 400 <= int(exc.status_code) <= 599 else 502
        raise HTTPException(status_code, str(exc)) from exc
    except LLMUpstreamHTTPError as exc:
        status_code = exc.status_code if 400 <= int(exc.status_code) <= 599 else 502
        raise HTTPException(status_code, str(exc)) from exc


@router.post("/evolution/regenerate-preview")
async def evolution_regenerate_preview():
    if _evolution_engine is None:
        raise HTTPException(500, "Evolution engine is not initialized")
    try:
        return await _evolution_engine.regenerate_preview_from_scored(
            store_pending=True,
            source="manual_regenerate",
        )
    except LLMTimeoutError as exc:
        raise HTTPException(504, str(exc)) from exc
    except LLMTransportError as exc:
        status_code = exc.status_code if 400 <= int(exc.status_code) <= 599 else 502
        raise HTTPException(status_code, str(exc)) from exc
    except LLMUpstreamHTTPError as exc:
        status_code = exc.status_code if 400 <= int(exc.status_code) <= 599 else 502
        raise HTTPException(status_code, str(exc)) from exc


@router.get("/evolution/pending-preview")
async def evolution_pending_preview():
    if _evolution_engine is None:
        raise HTTPException(500, "Evolution engine is not initialized")
    data = await _evolution_engine.get_pending_preview()
    if not data:
        raise HTTPException(404, "No pending evolution preview")
    return data


@router.put("/evolution/pending-preview")
async def evolution_update_pending_preview(req: EvolutionApplyRequest):
    if _evolution_engine is None:
        raise HTTPException(500, "Evolution engine is not initialized")
    return await _evolution_engine.save_pending_preview(req.preview, source="manual_edit")


@router.post("/evolution/apply")
async def evolution_apply(req: EvolutionApplyRequest):
    if _evolution_engine is None:
        raise HTTPException(500, "Evolution engine is not initialized")
    result = await _evolution_engine.apply(req.preview)
    sync_method = getattr(_memory_store, "sync_eligible_vectors", None)
    if callable(sync_method):
        await sync_method()
    return result


@router.post("/evolution/recalculate-archive")
async def evolution_recalculate_archive(req: RecalculateArchiveRequest):
    if _evolution_engine is None:
        raise HTTPException(500, "Evolution engine is not initialized")
    result = await _evolution_engine.recalculate_archive_status(
        start_id=req.start_id,
        end_id=req.end_id,
        start_date=req.start_date,
        end_date=req.end_date,
    )
    sync_method = getattr(_memory_store, "sync_eligible_vectors", None)
    if callable(sync_method):
        await sync_method()
    return result


@router.post("/evolution/rescore")
async def evolution_rescore(req: EvolutionRescoreRequest):
    if _evolution_engine is None:
        raise HTTPException(500, "Evolution engine is not initialized")
    try:
        result = await _evolution_engine.rescore_events(
            start_id=req.start_id,
            end_id=req.end_id,
            start_date=req.start_date,
            end_date=req.end_date,
            scored_only=req.scored_only,
        )
    except LLMTimeoutError as exc:
        raise HTTPException(504, str(exc)) from exc
    except LLMTransportError as exc:
        raise HTTPException(502, str(exc)) from exc
    except LLMUpstreamHTTPError as exc:
        status_code = 502 if exc.status_code < 400 else exc.status_code
        raise HTTPException(status_code, str(exc)) from exc
    sync_method = getattr(_memory_store, "sync_eligible_vectors", None)
    if callable(sync_method):
        await sync_method()
    return result
