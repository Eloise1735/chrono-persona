from __future__ import annotations

import json
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException

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
    UpdateVectorSettingsRequest,
    VectorSyncRequest,
    VectorCompactRequest,
    VectorBatchDeleteRequest,
    UpdateRuntimeLLMRequest,
    UpsertModelPricingRequest,
    BulkImportRequest,
    StateSnapshot,
    EventAnchor,
    KeyRecord,
)
from server.prompts import DEFAULT_SETTINGS, KEY_MODEL_PRICING_JSON
from server.event_taxonomy import classify_event, make_event_title

router = APIRouter(prefix="/api")

_db = None
_state_machine = None
_memory_store = None
_prompt_manager = None
_evolution_engine = None
_llm_client = None


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


def set_dependencies(
    db,
    state_machine,
    memory_store,
    prompt_manager=None,
    evolution_engine=None,
    llm_client=None,
):
    global _db, _state_machine, _memory_store, _prompt_manager, _evolution_engine, _llm_client
    _db = db
    _state_machine = state_machine
    _memory_store = memory_store
    _prompt_manager = prompt_manager
    _evolution_engine = evolution_engine
    _llm_client = llm_client


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


# ── State Machine endpoints (mirror MCP tools for web testing) ──

@router.post("/state/current")
async def api_get_current_state(req: GetCurrentStateRequest):
    result = await _state_machine.get_current_state(
        req.current_time, req.last_interaction_time
    )
    return {"content": result}


@router.post("/state/reflect")
async def api_reflect(req: ReflectRequest):
    result = await _state_machine.reflect_on_conversation(req.conversation_summary)
    return {"content": result}


@router.post("/state/summarize")
async def api_summarize_conversation(req: SummarizeConversationRequest):
    result = await _state_machine.summarize_conversation(req.conversation_text)
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
        created_at=datetime.utcnow().isoformat(),
        type=req.type,
        content=req.content,
        environment=req.environment,
    )
    snap_id = await _db.insert_snapshot(snap)
    return {"id": snap_id, "message": "Snapshot created"}


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
    title = (req.title or "").strip() or make_event_title(req.description, req.trigger_keywords, categories)
    event = EventAnchor(
        date=req.date or datetime.utcnow().strftime("%Y-%m-%d"),
        title=title,
        description=req.description,
        source=req.source,
        created_at=datetime.utcnow().isoformat(),
        trigger_keywords=json.dumps(req.trigger_keywords, ensure_ascii=False),
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

    if req.categories is None and (req.description is not None or req.trigger_keywords is not None):
        fields["categories"] = json.dumps(classify_event(description, keywords), ensure_ascii=False)

    if req.title is None and (
        req.description is not None or req.trigger_keywords is not None or req.categories is not None
    ):
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
        "generated_at": now.isoformat(),
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

    now_iso = datetime.utcnow().isoformat()
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
                created_at=str(item.get("created_at") or now_iso),
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
    return await _evolution_engine.check_status()


@router.post("/evolution/preview")
async def evolution_preview():
    if _evolution_engine is None:
        raise HTTPException(500, "Evolution engine is not initialized")
    return await _evolution_engine.preview()


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
