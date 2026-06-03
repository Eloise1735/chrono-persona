from __future__ import annotations

import json
import logging
import re
from difflib import SequenceMatcher
from datetime import datetime, timedelta

from fastapi import APIRouter, Body, HTTPException

logger = logging.getLogger(__name__)

from server.diagnostics import TRACE_STORE
from server.llm_client import LLMTimeoutError, LLMTransportError, LLMUpstreamHTTPError
from server.models import (
    CharacterNotification,
    CreateNPCRequest,
    CreateSnapshotRequest,
    CreateEventRequest,
    DeleteEventsByScoreRequest,
    UpdateEventRequest,
    DailyPlan,
    CreateKeyRecordRequest,
    GeneratePlanRequest,
    UpdateKeyRecordRequest,
    MemorySearchRequest,
    GetCurrentStateRequest,
    NPCEntity,
    PlanItem,
    ReplanRequest,
    ReflectRequest,
    SummarizeConversationRequest,
    PeriodicReviewRequest,
    PlanBatchDeleteRequest,
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
    WorldBookSearchRequest,
    KeyRecordBatchVectorizeRequest,
    WorldBookJsonImportRequest,
    UpsertModelPricingRequest,
    BulkImportRequest,
    SnapshotTimezoneRepairRequest,
    StateSnapshot,
    EventAnchor,
    KeyRecord,
    BulkPlanItemRequest,
    BulkUpdatePlanRequest,
    UpdatePlanItemRequest,
    UpdateNPCRequest,
    WorldBook,
    KEY_RECORD_TYPES,
    LEGACY_KEY_RECORD_TYPE_MAP,
    format_utc_instant_z,
)
from server.prompts import DEFAULT_SETTINGS, KEY_MODEL_PRICING_JSON
from server.security import (
    get_secret_from_env,
    is_blank_or_masked_secret,
    is_sensitive_setting_key,
    mask_secret,
    secret_env_var_for_setting,
    validate_api_base,
)
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
_ob_client = None
_ob_embedding_store = None
_ob_decay_engine = None

_OB_BUCKET_TYPES = {"dynamic", "permanent", "feel", "archive", "archived"}


def _redact_setting_item(item: dict | None) -> dict | None:
    if item is None:
        return None
    redacted = dict(item)
    if is_sensitive_setting_key(redacted.get("key", "")):
        env_value = get_secret_from_env(str(redacted.get("key") or ""), "")
        redacted["value"] = mask_secret(env_value)
        redacted["is_secret"] = True
        redacted["env_var"] = secret_env_var_for_setting(str(redacted.get("key") or ""))
    return redacted


def _redact_settings(items: list[dict]) -> list[dict]:
    return [item for item in (_redact_setting_item(item) for item in items) if item is not None]


def _public_llm_config(settings: dict) -> dict:
    public = dict(settings)
    public["api_key_set"] = bool(str(public.get("api_key") or "").strip())
    public["api_key"] = mask_secret(public.get("api_key", ""))
    public["api_key_source"] = "env" if public["api_key_set"] else "missing"
    return public


def _base_changes_without_new_key(current: dict, new_base: object, new_key: object) -> bool:
    if "api_base" not in current:
        return False
    normalized_new_base = validate_api_base(new_base, "api_base")
    normalized_current_base = str(current.get("api_base") or "").strip().rstrip("/")
    return (
        bool(normalized_new_base)
        and normalized_new_base != normalized_current_base
        and is_blank_or_masked_secret(new_key)
    )


def _ob_payload_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [part.strip() for part in value.replace("，", ",").split(",") if part.strip()]
    return []


def _ob_payload_float(value, default: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        num = default
    return max(minimum, min(maximum, num))


def _ob_payload_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        num = default
    return max(minimum, min(maximum, num))


def _ob_bucket_item(bucket) -> dict:
    formatted = _ob_client.format_buckets([bucket]) if _ob_client is not None and bucket is not None else []
    if not formatted:
        raise HTTPException(404, "OB bucket not found")
    return formatted[0]


def _normalize_plan_action_type(value: str | None) -> str:
    raw = str(value or "internal").strip()
    return raw if raw in {"internal", "web_search", "npc_interaction"} else "internal"


def _normalize_plan_source_kind(value: str | None) -> str:
    raw = str(value or "generated").strip()
    return raw if raw in {"generated", "routine", "carried_over", "thread", "spontaneous", "replan"} else "generated"


def _sanitize_plan_payload(payload: dict | None) -> dict:
    data = dict(payload or {})
    for key in ["content", "message", "message_text", "draft", "draft_message", "user_message", "sync_request"]:
        data.pop(key, None)
    outline = data.get("progress_outline")
    if isinstance(outline, dict):
        data["progress_outline"] = {
            "goal": str(outline.get("goal") or "").strip(),
            "done_so_far": str(outline.get("done_so_far") or "").strip(),
            "remaining": str(outline.get("remaining") or "").strip(),
            "watch_points": str(outline.get("watch_points") or "").strip(),
            "trigger_to_shift": str(outline.get("trigger_to_shift") or "").strip(),
        }
    if "thread_id" in data:
        data["thread_id"] = str(data.get("thread_id") or "").strip()
    if "progress_status" in data:
        data["progress_status"] = str(data.get("progress_status") or "").strip()
    if "closure_condition" in data:
        data["closure_condition"] = str(data.get("closure_condition") or "").strip()
    for numeric_key in ("expected_steps", "current_step"):
        if numeric_key in data:
            try:
                data[numeric_key] = max(1, int(data.get(numeric_key) or 1))
            except Exception:
                data.pop(numeric_key, None)
    return data


def _plan_items_to_raw_plan(items: list[PlanItem]) -> str:
    def _parse_action_payload(raw: str) -> dict:
        try:
            value = json.loads(raw or "{}")
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    payload = {
        "items": [
            {
                "hour_start": int(item.hour_start),
                "hour_end": int(item.hour_end),
                "activity": item.activity,
                "action_type": item.action_type,
                "action_payload": _parse_action_payload(item.action_payload),
                "status": item.status,
                "outcome": item.outcome,
                "source_kind": item.source_kind,
                "source_ref_id": item.source_ref_id,
                "executed_at": item.executed_at,
            }
            for item in items
        ]
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


_plan_engine = None
_npc_engine = None


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
    plan_engine=None,
    npc_engine=None,
    ob_client=None,
    ob_embedding_store=None,
    ob_decay_engine=None,
):
    global _db, _state_machine, _memory_store, _prompt_manager, _evolution_engine
    global _llm_client, _env_llm_client, _snapshot_llm_client, _plan_engine, _npc_engine, _ob_client, _ob_embedding_store, _ob_decay_engine
    _db = db
    _state_machine = state_machine
    _memory_store = memory_store
    _prompt_manager = prompt_manager
    _evolution_engine = evolution_engine
    _llm_client = llm_client
    _env_llm_client = env_llm_client
    _snapshot_llm_client = snapshot_llm_client
    _plan_engine = plan_engine
    _npc_engine = npc_engine
    _ob_client = ob_client
    _ob_embedding_store = ob_embedding_store
    _ob_decay_engine = ob_decay_engine


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


def _extract_feel_preview_object(text: str) -> dict:
    parsed = _extract_json_object(text)
    if parsed:
        return parsed
    raw = (text or "").strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start : end + 1]
    keys = ["source_summary", "principle_preview", "key_record_preview", "feel_preview"]
    result: dict[str, str] = {}
    for idx, key in enumerate(keys):
        next_key = keys[idx + 1] if idx + 1 < len(keys) else None
        if next_key:
            pattern = rf'"{key}"\s*:\s*"(.*?)"\s*,\s*"{next_key}"\s*:'
        else:
            pattern = rf'"{key}"\s*:\s*"(.*)"\s*\}}'
        match = re.search(pattern, raw, flags=re.S)
        if not match:
            return {}
        value = match.group(1)
        value = value.replace('\\"', '"').replace("\\n", "\n").replace("\\r", "\r").replace("\\t", "\t")
        result[key] = value.strip()
    return result


def _normalize_key_record_type_value(value) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw in KEY_RECORD_TYPES:
        return raw
    return str(LEGACY_KEY_RECORD_TYPE_MAP.get(raw) or "")


def _serialize_world_book(item: WorldBook) -> dict:
    data = item.model_dump()
    data["tags"] = _parse_json_list(item.tags)
    data["match_keywords"] = _parse_json_list(item.match_keywords)
    data["is_active"] = bool(int(item.is_active or 0))
    data["vectorized"] = bool(str(item.embedding_vector_id or "").strip())
    return data


def _serialize_key_record(item: KeyRecord) -> dict:
    data = item.model_dump()
    data["tags"] = _parse_json_list(item.tags)
    data["match_keywords"] = _parse_json_list(getattr(item, "match_keywords", "[]"))
    data["vectorized"] = bool(str(getattr(item, "embedding_vector_id", "") or "").strip())
    return data


async def _get_llm_config(prefix: str) -> dict:
    enabled = await _db.get_setting(f"{prefix}_enabled")
    api_base = await _db.get_setting(f"{prefix}_api_base")
    model = await _db.get_setting(f"{prefix}_model")
    return {
        "enabled": str((enabled or {}).get("value", "0")) == "1",
        "api_base": str((api_base or {}).get("value", "")),
        "api_key": get_secret_from_env(f"{prefix}_api_key", ""),
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
        elif key == "api_base":
            try:
                value = validate_api_base(value, "api_base")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            current = await _get_llm_config(prefix)
            if _base_changes_without_new_key(current, value, current.get("api_key")):
                raise HTTPException(
                    status_code=400,
                    detail="Changing API Base requires the matching API key to be set in the server environment.",
                )
        elif key == "api_key":
            continue
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


@router.get("/ob/stats")
async def get_ob_stats():
    if _ob_client is None:
        raise HTTPException(400, "OB memory spine is not initialized")
    stats = await _ob_client.stats()
    total = int(stats.get("total") or 0)
    embedding_total = _ob_embedding_store.count() if _ob_embedding_store is not None else 0
    embedding_enabled = (
        await _ob_embedding_store.is_enabled()
        if _ob_embedding_store is not None
        else False
    )
    stats.update(
        {
            "embedding_enabled": bool(embedding_enabled),
            "embedding_total": int(embedding_total),
            "embedding_covered_pct": round((embedding_total / total * 100.0), 2) if total else 0.0,
            "decay_engine": {
                "running": bool(_ob_decay_engine.is_running) if _ob_decay_engine is not None else False,
                "last_result": _ob_decay_engine.last_result if _ob_decay_engine is not None else None,
            },
        }
    )
    return stats


@router.get("/ob/pulse")
async def ob_pulse(include_archive: bool = False):
    if _ob_client is None:
        raise HTTPException(400, "OB memory spine is not initialized")
    result = await _ob_client.pulse(include_archive=include_archive)
    result["decay_engine"] = {
        "running": bool(_ob_decay_engine.is_running) if _ob_decay_engine is not None else False,
        "last_result": _ob_decay_engine.last_result if _ob_decay_engine is not None else None,
    }
    return result


@router.get("/ob/diagnostics")
async def ob_diagnostics():
    if _ob_client is None:
        raise HTTPException(400, "OB memory spine is not initialized")
    result = await _ob_client.diagnostics()
    total = int((result.get("stats") or {}).get("total") or 0)
    embedding_total = _ob_embedding_store.count() if _ob_embedding_store is not None else 0
    result["embedding"] = {
        "enabled": await _ob_embedding_store.is_enabled() if _ob_embedding_store is not None else False,
        "total": embedding_total,
        "covered_pct": round((embedding_total / total * 100.0), 2) if total else 0.0,
    }
    result["decay_engine"] = {
        "running": bool(_ob_decay_engine.is_running) if _ob_decay_engine is not None else False,
        "last_result": _ob_decay_engine.last_result if _ob_decay_engine is not None else None,
    }
    return result


@router.get("/ob/breath-debug")
async def ob_breath_debug(
    q: str = "",
    domain: str = "",
    valence: float | None = None,
    arousal: float | None = None,
    include_archive: bool = False,
):
    if _ob_client is None:
        raise HTTPException(400, "OB memory spine is not initialized")
    return await _ob_client.breath_debug(
        query=q,
        domain=domain or None,
        valence=valence,
        arousal=arousal,
        include_archive=include_archive,
    )


@router.post("/ob/decay/run")
async def ob_decay_run(payload: dict | None = Body(default=None)):
    if _ob_decay_engine is None:
        raise HTTPException(400, "OB decay engine is not initialized")
    payload = payload or {}
    return await _ob_decay_engine.run_decay_cycle(dry_run=bool(payload.get("dry_run", True)))


@router.post("/ob/embeddings/backfill")
async def ob_embedding_backfill(payload: dict | None = Body(default=None)):
    if _ob_client is None or _ob_embedding_store is None:
        raise HTTPException(400, "OB embedding store is not initialized")
    payload = payload or {}
    dry_run = bool(payload.get("dry_run", True))
    limit = _ob_payload_int(payload.get("limit"), 50, 1, 500)
    buckets = await _ob_client.list_buckets(include_archive=bool(payload.get("include_archive", True)))
    missing = [b for b in buckets if _ob_embedding_store.get(b.id) is None and str(b.content or "").strip()]
    selected = missing[:limit]
    result = {"dry_run": dry_run, "missing": len(missing), "attempted": len(selected), "success": 0, "failed": 0, "items": []}
    if dry_run:
        result["items"] = [{"id": b.id, "name": b.metadata.get("name") or b.id} for b in selected]
        return result
    for bucket in selected:
        ok = await _ob_embedding_store.upsert(bucket.id, bucket.content)
        result["success" if ok else "failed"] += 1
        result["items"].append({"id": bucket.id, "ok": bool(ok)})
    return result


@router.get("/ob/network")
async def ob_network(limit: int = 200, min_similarity: float = 0.5):
    if _ob_client is None:
        raise HTTPException(400, "OB memory spine is not initialized")
    capped = max(1, min(int(limit or 200), 500))
    buckets = await _ob_client.list_buckets(include_archive=False)
    buckets = buckets[:capped]
    nodes = []
    embeddings = {}
    for bucket in buckets:
        meta = bucket.metadata or {}
        nodes.append({
            "id": bucket.id,
            "name": meta.get("name") or bucket.id,
            "type": _ob_client._bucket_type(meta),
            "domain": meta.get("domain", []),
            "score": _ob_client.calculate_score(meta),
            "pinned": bool(meta.get("pinned")),
            "digested": bool(meta.get("digested")),
        })
        if _ob_embedding_store is not None:
            emb = _ob_embedding_store.get(bucket.id)
            if emb:
                embeddings[bucket.id] = emb
    edges = []
    ids = list(embeddings)
    threshold = max(0.0, min(float(min_similarity), 1.0))
    for i, left in enumerate(ids):
        for right in ids[i + 1:]:
            sim = _ob_client._cosine_similarity(embeddings[left], embeddings[right])
            if sim >= threshold:
                edges.append({"source": left, "target": right, "similarity": round(sim, 4)})
    return {"nodes": nodes, "edges": edges}


@router.get("/ob/buckets")
async def list_ob_buckets(
    include_archive: bool = False,
    limit: int = 100,
    life_scope: str = "user",
):
    """List OB buckets, sorted by last_active desc.

    Query parameter ``life_scope`` controls character_life filtering for the management UI:
      - ``"user"``      (default): exclude character_life buckets — what the user normally
                        wants to manage (relational / cross-domain memories)
      - ``"character"`` : only character_life buckets
      - ``"all"``       : everything

    Default is ``user`` because the frontend bucket browser is for managing the user's own
    memory入库，角色侧的自动 fragments / rollups / 当下事件总结混在里面会让管理困难。
    """
    if _ob_client is None:
        raise HTTPException(400, "OB memory spine is not initialized")
    scope = str(life_scope or "user").strip().lower()
    if scope not in {"user", "character", "all"}:
        scope = "user"
    buckets = await _ob_client.list_buckets(include_archive=include_archive)
    if scope == "user":
        buckets = [b for b in buckets if not _ob_client._is_character_life_bucket(b)]
    elif scope == "character":
        buckets = [b for b in buckets if _ob_client._is_character_life_bucket(b)]
    buckets.sort(
        key=lambda item: str((getattr(item, "metadata", {}) or {}).get("last_active") or ""),
        reverse=True,
    )
    capped = max(1, min(500, int(limit or 100)))
    return {"items": _ob_client.format_buckets(buckets[:capped]), "life_scope": scope}


@router.post("/ob/breath")
async def ob_breath(payload: dict | None = Body(default=None)):
    if _ob_client is None:
        raise HTTPException(400, "OB memory spine is not initialized")
    payload = payload or {}
    domain = payload.get("domain") or None
    domain_text = ",".join(_ob_payload_list(domain)).lower() if domain is not None else ""
    default_limit = 3 if domain_text == "feel" else 8
    buckets = await _ob_client.breath(
        query=str(payload.get("query") or ""),
        limit=int(payload.get("top_k") or payload.get("limit") or default_limit),
        domain=domain,
        valence=payload.get("valence"),
        arousal=payload.get("arousal"),
        include_archive=bool(payload.get("include_archive") or False),
        date_from=str(payload.get("date_from") or "") or None,
        date_to=str(payload.get("date_to") or "") or None,
        include_character_life=bool(payload.get("include_character_life") or False),
    )
    return {"items": _ob_client.format_buckets(buckets)}


@router.post("/ob/breath-bundle")
async def ob_breath_bundle(payload: dict | None = Body(default=None)):
    if _ob_client is None:
        raise HTTPException(400, "OB memory spine is not initialized")
    payload = payload or {}
    return await _ob_client.breath_bundle(
        top_k=_ob_payload_int(payload.get("top_k"), 8, 1, 50),
        feel_top_k=_ob_payload_int(payload.get("feel_top_k"), 3, 1, 20),
    )


@router.post("/ob/breath-personal")
async def ob_breath_personal():
    if _ob_client is None:
        raise HTTPException(400, "OB memory spine is not initialized")
    return await _ob_client.breath_personal()


@router.post("/ob/feel-crystals")
async def ob_feel_crystals(payload: dict | None = Body(default=None)):
    if _ob_client is None:
        raise HTTPException(400, "OB memory spine is not initialized")
    payload = payload or {}
    return await _ob_client.feel_crystals(
        limit=_ob_payload_int(payload.get("limit"), 3, 1, 20),
        max_items_per_cluster=_ob_payload_int(payload.get("max_items_per_cluster"), 5, 1, 20),
        min_cluster_size=_ob_payload_int(payload.get("min_cluster_size"), 3, 2, 20),
        min_similarity=_ob_payload_float(payload.get("min_similarity"), 0.7),
        cursor=str(payload.get("cursor") or ""),
        include_crystallized=bool(payload.get("include_crystallized") or False),
    )


def _fallback_feel_crystal_preview(sources: list, *, reason: str = "") -> dict:
    message = (
        "LLM 没有生成有效的有机结晶预览。请检查 LLM 配置或点击重新扫描。"
        "为避免把空泛模板误写入记忆，下面不自动填充结晶正文。"
    )
    if not sources:
        message = "没有可用于结晶的源 feel。"
    return {
        "source_summary": message,
        "principle_title": "",
        "principle_card": {},
        "principle_injection": "",
        "principle_preview": "",
        "key_record_preview": "",
        "feel_preview": "",
        "generated_by": "fallback",
        "error": reason,
    }


class FeelCrystalPreviewError(RuntimeError):
    pass


def _feel_preview_copy_score(text: str, sources: list) -> float:
    candidate = re.sub(r"\s+", "", str(text or ""))
    if not candidate:
        return 0.0
    score = 0.0
    for bucket in sources:
        source = re.sub(r"\s+", "", str(getattr(bucket, "content", "") or ""))
        if not source:
            continue
        score = max(score, SequenceMatcher(None, candidate[:800], source[:1600]).ratio())
        for start in range(0, max(0, len(source) - 28), 28):
            snippet = source[start:start + 36]
            if len(snippet) >= 24 and snippet in candidate:
                score = max(score, 0.95)
                break
    return round(score, 4)


async def _generate_feel_crystal_preview(sources: list) -> dict:
    if not sources:
        raise FeelCrystalPreviewError("没有可用于结晶的源 feel。")
    llm = _snapshot_llm_client
    if llm is None:
        raise FeelCrystalPreviewError("Snapshot LLM client is not initialized")
    sample_sources = sources
    if len(sources) > 24:
        step = (len(sources) - 1) / 23
        sample_sources = [sources[round(i * step)] for i in range(24)]
    items = []
    for idx, bucket in enumerate(sample_sources, 1):
        meta = getattr(bucket, "metadata", {}) or {}
        created = str(meta.get("created") or "")
        domains = ", ".join(str(d) for d in meta.get("domain", []) if str(d).strip()) or "none"
        content = str(getattr(bucket, "content", "") or "").strip()
        if len(content) > 900:
            content = content[:900] + "\n[truncated]"
        items.append(f"[{idx}] id={bucket.id} created={created} domain={domains}\n{content}")
    def build_prompt(strict_retry: bool = False) -> str:
        retry_rule = (
            "\n额外强约束：上一次输出太像原文。现在必须重新写，不能保留任何连续 12 个字以上的原文片段；"
            "不要出现列表感，不要用斜杠、分号或逐条并列来压缩。"
            "JSON 字符串内部禁止使用英文双引号；如需引用，请用中文书名号或单引号。"
            if strict_retry else ""
        )
        return (
        f"你是 OB feel 结晶助手。下面是一组相似的第一人称 feel；总数 {len(sources)} 条，抽样 {len(sample_sources)} 条。\n"
        "任务不是摘要，也不是提取关键词，而是把这些反复出现的感受整理成可复用的人格原则卡，"
        "同时保留一份新的第一人称 feel 沉淀。\n"
        "principle 不是事件记录，也不是鸡汤；它必须说明这些感受以后如何约束判断、回应和行动。"
        "必须保留原文本的核心张力，但不能引用、复述、拼接原句。"
        "不要写“这组/这些 feel/源文本显示/共同主题/内在姿态/后续需要观察”等分析腔或占位话。"
        "不要提到 feel 数量。不要分条。不要流水账。"
        f"{retry_rule}\n"
        "只输出 JSON，不要输出 markdown。输出必须能被 json.loads 直接解析。\n"
        "JSON 字符串内部不要使用英文双引号；如需引用原话风格，使用「」或单引号。"
        "JSON 结构必须严格类似："
        "{\"source_summary\":\"...\",\"principle_title\":\"...\",\"principle_card\":{\"principle\":\"...\",\"response_rule\":\"...\",\"anchors\":[\"...\"],\"avoid\":\"...\"},\"principle_injection\":\"...\",\"key_record_preview\":\"...\",\"feel_preview\":\"...\"}\n\n"
        "字段：\n"
        "- source_summary: 120-220字，给管理者看的共同主题说明，可以第三人称，但必须具体说出情绪结构，不要空泛。\n"
        "- principle_title: 8-28字，像一张原则卡标题，必须具体。\n"
        "- principle_card.principle: 80-180字，写稳定原则；保留核心张力，不写成口号。\n"
        "- principle_card.response_rule: 60-160字，写以后遇到相似情境时应如何回应/行动。\n"
        "- principle_card.anchors: 2-4个不可替代的画面或细节钩子，每个不超过40字。\n"
        "- principle_card.avoid: 30-100字，写需要避免的旧冲动、误判或过度补偿。\n"
        "- principle_injection: 120-220字，可直接注入 get_current_state；压缩原则+回应规则+避免点，不要列 JSON。\n"
        "- key_record_preview: 220-460字，中长期主线记录。可以稍中性，但要说清楚这条主线如何影响关系、选择、自我定位或生活流，不能只是“需要观察”。\n"
        "- feel_preview: 280-560字，新的第一人称浓缩 feel。它应该像角色此刻真的把这些余波重新感受了一遍后写下来的话，保留风格、呼吸和身体感。\n\n"
        "反例：把多条原文摘句用逗号、顿号、斜杠拼起来。反例：逐条说我经历了A/B/C。"
        "反例：我需要承认这些感受指向一种姿态。反例：这组 feel 表明某种模式。"
        "正例：写出这些碎片背后共同的温度、迟疑、确认、疼痛、靠近或松动，并让它自然变成一段新的第一人称文字。\n\n"
        "feel 原文：\n"
        + "\n\n---\n\n".join(items)
        )
    try:
        best = None
        last_error = ""
        last_response_preview = ""
        for attempt in range(2):
            response = await llm.chat_dedicated(
                [
                    {"role": "system", "content": "Return ONLY one valid JSON object. No markdown, no prose outside JSON. Do not copy source sentences."},
                    {"role": "user", "content": build_prompt(strict_retry=attempt > 0)},
                ],
                temperature=0.55 if attempt == 0 else 0.7,
                max_tokens=2200,
            )
            parsed = _extract_feel_preview_object(response)
            if not parsed:
                last_response_preview = str(response or "")[:500]
                last_error = "LLM returned no JSON object"
                continue
            candidate = {
                "source_summary": str(parsed.get("source_summary") or "").strip(),
                "principle_title": str(parsed.get("principle_title") or "").strip(),
                "principle_card": parsed.get("principle_card") if isinstance(parsed.get("principle_card"), dict) else {},
                "principle_injection": str(parsed.get("principle_injection") or "").strip(),
                "principle_preview": str(parsed.get("principle_preview") or "").strip(),
                "key_record_preview": str(parsed.get("key_record_preview") or "").strip(),
                "feel_preview": str(parsed.get("feel_preview") or "").strip(),
                "generated_by": "llm",
            }
            card = candidate["principle_card"] if isinstance(candidate["principle_card"], dict) else {}
            if candidate["principle_preview"] and not card:
                card = {
                    "principle": candidate["principle_preview"],
                    "response_rule": candidate["principle_preview"][:160],
                    "anchors": [],
                    "avoid": "",
                }
                candidate["principle_card"] = card
            if candidate["principle_preview"] and not candidate["principle_title"]:
                candidate["principle_title"] = candidate["principle_preview"][:24]
            if candidate["principle_preview"] and not candidate["principle_injection"]:
                candidate["principle_injection"] = candidate["principle_preview"][:220]
            principle_text = str(card.get("principle") or candidate["principle_preview"] or "").strip()
            response_rule = str(card.get("response_rule") or "").strip()
            missing = [
                key for key in ("source_summary", "key_record_preview", "feel_preview")
                if not candidate[key]
            ]
            if not candidate["principle_title"]:
                missing.append("principle_title")
            if not principle_text:
                missing.append("principle_card.principle")
            if not response_rule:
                missing.append("principle_card.response_rule")
            if not candidate["principle_injection"]:
                missing.append("principle_injection")
            if missing:
                last_error = f"LLM preview missing fields: {', '.join(missing)}"
                continue
            copy_score = max(
                _feel_preview_copy_score(principle_text, sources),
                _feel_preview_copy_score(response_rule, sources),
                _feel_preview_copy_score(candidate["principle_injection"], sources),
                _feel_preview_copy_score(candidate["key_record_preview"], sources),
                _feel_preview_copy_score(candidate["feel_preview"], sources),
            )
            candidate["copy_score"] = copy_score
            generic_text = "\n".join(
                [
                    candidate["principle_preview"],
                    candidate["principle_injection"],
                    candidate["key_record_preview"],
                    candidate["feel_preview"],
                ]
            )
            generic_bad = any(
                token in generic_text
                for token in ("这组", "这些 feel", "源文本", "共同主题", "内在姿态", "后续需要观察", "feel 指向")
            )
            too_short = (
                len(principle_text) < 60
                or len(response_rule) < 40
                or len(candidate["principle_injection"]) < 80
                or len(candidate["key_record_preview"]) < 160
                or len(candidate["feel_preview"]) < 200
            )
            best = candidate
            if copy_score < 0.62 and not generic_bad and not too_short:
                break
        if not best:
            detail = last_error or "LLM did not return a usable preview"
            if last_response_preview:
                detail = f"{detail}; response preview={last_response_preview!r}"
            raise ValueError(detail)
        if best.get("copy_score", 1.0) >= 0.62:
            raise ValueError(f"LLM preview is too close to source text: copy_score={best.get('copy_score')}")
        best_card = best.get("principle_card") if isinstance(best.get("principle_card"), dict) else {}
        best_principle = str(best_card.get("principle") or best.get("principle_preview") or "").strip()
        best_response_rule = str(best_card.get("response_rule") or "").strip()
        generic_text = "\n".join([best_principle, best_response_rule, best["principle_injection"], best["key_record_preview"], best["feel_preview"]])
        if any(token in generic_text for token in ("这组", "这些 feel", "源文本", "共同主题", "内在姿态", "后续需要观察", "feel 指向")):
            raise ValueError("LLM preview is too generic")
        if len(best_principle) < 60 or len(best_response_rule) < 40 or len(best["principle_injection"]) < 80 or len(best["key_record_preview"]) < 160 or len(best["feel_preview"]) < 200:
            raise ValueError("LLM preview is too short")
        return best
    except Exception as exc:
        logger.exception("Failed to generate feel crystal preview")
        raise FeelCrystalPreviewError(str(exc)) from exc


@router.post("/ob/feel-crystals/preview")
async def ob_feel_crystal_preview(payload: dict | None = Body(default=None)):
    if _ob_client is None:
        raise HTTPException(400, "OB memory spine is not initialized")
    payload = payload or {}
    sources = await _ob_client.feel_cluster_sources(
        cluster_id=str(payload.get("cluster_id") or ""),
        feel_ids=_ob_payload_list(payload.get("feel_ids")),
        include_all=bool(payload.get("include_all", True)),
        min_cluster_size=_ob_payload_int(payload.get("min_cluster_size"), 3, 2, 20),
        min_similarity=_ob_payload_float(payload.get("min_similarity"), 0.7),
        include_crystallized=bool(payload.get("include_crystallized") or False),
    )
    try:
        preview = await _generate_feel_crystal_preview(sources)
    except FeelCrystalPreviewError as exc:
        raise HTTPException(502, f"Feel crystal preview failed: {exc}") from exc
    return {
        "preview": preview,
        "source_feel_ids": [bucket.id for bucket in sources],
        "source_count": len(sources),
    }


@router.post("/ob/uncrystallize-feel")
async def ob_uncrystallize_feel(payload: dict | None = Body(default=None)):
    if _ob_client is None:
        raise HTTPException(400, "OB memory spine is not initialized")
    payload = payload or {}
    return await _ob_client.uncrystallize_feel(
        cluster_id=str(payload.get("cluster_id") or ""),
        feel_ids=_ob_payload_list(payload.get("feel_ids")),
        include_all=bool(payload.get("include_all") or False),
        min_cluster_size=_ob_payload_int(payload.get("min_cluster_size"), 3, 2, 20),
        min_similarity=_ob_payload_float(payload.get("min_similarity"), 0.7),
    )


async def _ob_create_feel_crystal_key_record(payload: dict, *, content: str, mode: str) -> dict | None:
    if _state_machine is None:
        raise HTTPException(400, "State machine is not initialized")
    body = str(content or "").strip()
    if not body:
        raise HTTPException(400, "key_record_content is required for key_record crystallization")
    record_type = str(payload.get("key_record_type") or "").strip()
    result = await _state_machine.upsert_key_record(
        record_type=record_type if record_type and record_type.lower() != "auto" else None,
        title=str(payload.get("key_record_title") or "Feel crystal").strip() or "Feel crystal",
        content_text=body,
        tags=["feel_crystal", "ob"] + _ob_payload_list(payload.get("key_record_tags")),
        content_json={
            "source": "ob_feel_crystal",
            "mode": mode,
            "cluster_id": str(payload.get("cluster_id") or ""),
            "feel_ids": _ob_payload_list(payload.get("feel_ids")),
            "include_all": bool(payload.get("include_all") or False),
        },
        source="ob_feel_crystal",
        life_scope=str(payload.get("life_scope") or "character_life").strip() or "character_life",
        update_if_exists=bool(payload.get("update_if_exists", True)),
    )
    return result


@router.post("/ob/crystallize-feel")
async def ob_crystallize_feel(payload: dict | None = Body(default=None)):
    if _ob_client is None:
        raise HTTPException(400, "OB memory spine is not initialized")
    payload = payload or {}
    mode = str(payload.get("mode") or "").strip().lower()
    if mode not in {"principle", "thread", "both", "feel"}:
        raise HTTPException(400, "mode must be principle, thread, both, or feel")

    key_record_result = None
    extra_targets: list[str] = []
    if mode in {"thread", "both"}:
        key_content = str(
            payload.get("key_record_content")
            or payload.get("principle_injection")
            or payload.get("principle_content")
            or payload.get("feel_content")
            or ""
        ).strip()
        key_record_result = await _ob_create_feel_crystal_key_record(payload, content=key_content, mode=mode)
        record = (key_record_result or {}).get("record") or {}
        record_id = record.get("id")
        if record_id is not None:
            extra_targets.append(f"key_record:{record_id}")

    try:
        ob_result = await _ob_client.crystallize_feel(
            mode=mode,
            principle_content=str(payload.get("principle_content") or ""),
            principle_title=str(payload.get("principle_title") or ""),
            principle_card=payload.get("principle_card") if isinstance(payload.get("principle_card"), dict) else None,
            principle_injection=str(payload.get("principle_injection") or ""),
            feel_content=str(payload.get("feel_content") or ""),
            domain=_ob_payload_list(payload.get("domain")) or None,
            feel_ids=_ob_payload_list(payload.get("feel_ids")),
            cluster_id=str(payload.get("cluster_id") or ""),
            include_all=bool(payload.get("include_all") or False),
            extra_targets=extra_targets,
            min_cluster_size=_ob_payload_int(payload.get("min_cluster_size"), 3, 2, 20),
            min_similarity=_ob_payload_float(payload.get("min_similarity"), 0.7),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ob": ob_result, "key_record": key_record_result}


@router.post("/ob/dream")
async def ob_dream(payload: dict | None = Body(default=None)):
    if _ob_client is None:
        raise HTTPException(400, "OB memory spine is not initialized")
    payload = payload or {}
    limit = _ob_payload_int(payload.get("limit"), 10, 1, 30)
    return await _ob_client.dream(limit=limit)


@router.post("/ob/hold")
async def ob_hold(payload: dict | None = Body(default=None)):
    if _ob_client is None:
        raise HTTPException(400, "OB memory spine is not initialized")
    payload = payload or {}
    content = str(payload.get("content") or "").strip()
    if not content:
        raise HTTPException(400, "content is required")
    bucket_type = str(payload.get("type") or payload.get("bucket_type") or "dynamic").strip()
    if bucket_type not in {"dynamic", "permanent", "feel"}:
        bucket_type = "dynamic"
    source_bucket = str(payload.get("source_bucket") or "").strip()
    extra_metadata = {"source_bucket": source_bucket} if bucket_type == "feel" and source_bucket else None
    bucket_id = await _ob_client.hold(
        content,
        name=str(payload.get("name") or "").strip() or None,
        tags=_ob_payload_list(payload.get("tags")),
        domain=_ob_payload_list(payload.get("domain")),
        importance=_ob_payload_int(payload.get("importance"), 5, 1, 10),
        valence=_ob_payload_float(payload.get("valence"), 0.5),
        arousal=_ob_payload_float(payload.get("arousal"), 0.3),
        bucket_type=bucket_type,
        pinned=bool(payload.get("pinned") or False),
        protected=bool(payload.get("protected") or False),
        resolved=bool(payload.get("resolved") or False),
        extra_metadata=extra_metadata,
    )
    if bucket_type == "feel" and source_bucket:
        await _ob_client.update(
            source_bucket,
            digested=True,
            digested_at=datetime.utcnow().isoformat(),
            model_valence=_ob_payload_float(payload.get("valence"), 0.5),
            model_arousal=_ob_payload_float(payload.get("arousal"), 0.3),
        )
    bucket = await _ob_client.get(bucket_id)
    return {"item": _ob_bucket_item(bucket)}


@router.post("/ob/grow")
async def ob_grow(payload: dict | None = Body(default=None)):
    if _ob_client is None:
        raise HTTPException(400, "OB memory spine is not initialized")
    payload = payload or {}
    content = str(payload.get("content") or "").strip()
    if not content:
        raise HTTPException(400, "content is required")
    bucket_id = await _ob_client.grow(
        content,
        query=str(payload.get("query") or ""),
        domain=payload.get("domain") or None,
        importance=payload.get("importance"),
        valence=payload.get("valence"),
        arousal=payload.get("arousal"),
    )
    bucket = await _ob_client.get(bucket_id)
    return {"item": _ob_bucket_item(bucket)}


@router.get("/ob/buckets/{bucket_id}")
async def get_ob_bucket(bucket_id: str):
    if _ob_client is None:
        raise HTTPException(400, "OB memory spine is not initialized")
    bucket = await _ob_client.get(bucket_id)
    return {"item": _ob_bucket_item(bucket)}


@router.patch("/ob/buckets/{bucket_id}")
async def update_ob_bucket(bucket_id: str, payload: dict | None = Body(default=None)):
    if _ob_client is None:
        raise HTTPException(400, "OB memory spine is not initialized")
    payload = payload or {}
    updates: dict = {}
    if "content" in payload:
        content = str(payload.get("content") or "").strip()
        if not content:
            raise HTTPException(400, "content cannot be empty")
        updates["content"] = content
    if "name" in payload:
        updates["name"] = str(payload.get("name") or "").strip() or bucket_id
    if "domain" in payload:
        updates["domain"] = _ob_payload_list(payload.get("domain"))
    if "tags" in payload:
        updates["tags"] = _ob_payload_list(payload.get("tags"))
    if "type" in payload:
        bucket_type = str(payload.get("type") or "dynamic").strip()
        if bucket_type not in _OB_BUCKET_TYPES:
            raise HTTPException(400, "Unsupported OB bucket type")
        updates["type"] = bucket_type
    for key in ("pinned", "protected", "resolved", "crystallized", "digested"):
        if key in payload:
            updates[key] = bool(payload.get(key))
    if "importance" in payload:
        updates["importance"] = _ob_payload_int(payload.get("importance"), 5, 1, 10)
    if "valence" in payload:
        updates["valence"] = _ob_payload_float(payload.get("valence"), 0.5)
    if "arousal" in payload:
        updates["arousal"] = _ob_payload_float(payload.get("arousal"), 0.3)
    if not updates:
        raise HTTPException(400, "No OB bucket updates supplied")
    ok = await _ob_client.update(bucket_id, **updates)
    if not ok:
        raise HTTPException(404, "OB bucket not found")
    bucket = await _ob_client.get(bucket_id)
    return {"item": _ob_bucket_item(bucket)}


@router.post("/ob/buckets/{bucket_id}/archive")
async def archive_ob_bucket(bucket_id: str):
    if _ob_client is None:
        raise HTTPException(400, "OB memory spine is not initialized")
    ok = await _ob_client.archive(bucket_id)
    if not ok:
        raise HTTPException(404, "OB bucket not found")
    bucket = await _ob_client.get(bucket_id)
    return {"ok": True, "item": _ob_bucket_item(bucket)}


@router.post("/ob/buckets/{bucket_id}/restore")
async def restore_ob_bucket(bucket_id: str, payload: dict | None = Body(default=None)):
    if _ob_client is None:
        raise HTTPException(400, "OB memory spine is not initialized")
    payload = payload or {}
    target_type = str(payload.get("target_type") or "dynamic").strip()
    if target_type not in {"dynamic", "permanent", "feel"}:
        raise HTTPException(400, "target_type must be dynamic, permanent, or feel")
    ok = await _ob_client.restore(bucket_id, target_type=target_type)
    if not ok:
        raise HTTPException(404, "OB bucket not found")
    bucket = await _ob_client.get(bucket_id)
    return {"ok": True, "item": _ob_bucket_item(bucket)}


@router.post("/ob/buckets/{bucket_id}/resolve")
async def resolve_ob_bucket(bucket_id: str, payload: dict | None = Body(default=None)):
    if _ob_client is None:
        raise HTTPException(400, "OB memory spine is not initialized")
    payload = payload or {}
    ok = await _ob_client.resolve(bucket_id, reason=str(payload.get("reason") or ""))
    if not ok:
        raise HTTPException(404, "OB bucket not found")
    bucket = await _ob_client.get(bucket_id)
    return {"ok": True, "item": _ob_bucket_item(bucket)}


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


# ── Plans / NPCs / Notifications ──

@router.get("/plans/today")
async def api_get_today_plan():
    if _plan_engine is None:
        raise HTTPException(503, "Plan engine is not initialized")
    plan = await _plan_engine.get_current_plan()
    baseline_plan_id = await _plan_engine.get_baseline_plan_id()
    if plan is None or plan.id is None:
        return {"plan": None, "items": [], "baseline_plan_id": baseline_plan_id}
    items = await _plan_engine.get_effective_plan_items(plan)
    return {"plan": plan.model_dump(), "items": [item.model_dump() for item in items], "baseline_plan_id": baseline_plan_id}


@router.get("/plans/history")
async def api_list_plan_history(offset: int = 0, limit: int = 30, status: str | None = None):
    items = await _db.list_daily_plans(offset=offset, limit=limit, status=status)
    return {"items": [item.model_dump() for item in items]}


@router.get("/plans/{plan_id}")
async def api_get_plan(plan_id: int):
    plan = await _db.get_daily_plan_by_id(plan_id)
    if plan is None:
        raise HTTPException(404, "Plan not found")
    if _plan_engine is not None:
        items = await _plan_engine.get_effective_plan_items(plan)
    else:
        items = await _db.list_plan_items(plan_id)
    return {"plan": plan.model_dump(), "items": [item.model_dump() for item in items]}


@router.delete("/plans/{plan_id}")
async def api_delete_plan(plan_id: int):
    plan = await _db.get_daily_plan_by_id(plan_id)
    if plan is None:
        raise HTTPException(404, "Plan not found")
    deleted_items = await _db.delete_plan_items_for_plan(plan_id)
    await _db.delete_daily_plan(plan_id)
    return {
        "message": "Plan deleted",
        "plan_id": plan_id,
        "deleted_item_count": deleted_items,
    }


@router.post("/plans/batch-delete")
async def api_batch_delete_plans(req: PlanBatchDeleteRequest):
    unique_ids: list[int] = []
    seen: set[int] = set()
    for raw in req.plan_ids:
        plan_id = int(raw or 0)
        if plan_id <= 0 or plan_id in seen:
            continue
        seen.add(plan_id)
        unique_ids.append(plan_id)
    if not unique_ids:
        raise HTTPException(400, "No valid plan ids provided")

    deleted = 0
    failed = 0
    deleted_item_count = 0
    missing_ids: list[int] = []
    for plan_id in unique_ids:
        plan = await _db.get_daily_plan_by_id(plan_id)
        if plan is None:
            failed += 1
            missing_ids.append(plan_id)
            continue
        deleted_item_count += await _db.delete_plan_items_for_plan(plan_id)
        await _db.delete_daily_plan(plan_id)
        deleted += 1
    return {
        "message": "Plan batch delete completed",
        "deleted": deleted,
        "failed": failed,
        "deleted_item_count": deleted_item_count,
        "missing_ids": missing_ids,
    }


@router.put("/plans/items/{item_id}")
async def api_update_plan_item(item_id: int, req: UpdatePlanItemRequest):
    item = await _db.get_plan_item_by_id(item_id)
    if item is None:
        raise HTTPException(404, "Plan item not found")
    fields = {}
    if req.hour_start is not None:
        fields["hour_start"] = max(0, int(req.hour_start))
    if req.hour_end is not None:
        fields["hour_end"] = max(1, int(req.hour_end))
    if req.activity is not None:
        fields["activity"] = req.activity.strip()
    if req.action_type is not None:
        fields["action_type"] = _normalize_plan_action_type(req.action_type)
    if req.action_payload is not None:
        fields["action_payload"] = json.dumps(_sanitize_plan_payload(req.action_payload), ensure_ascii=False)
    if req.status is not None:
        fields["status"] = req.status
    if req.outcome is not None:
        fields["outcome"] = req.outcome.strip()
    if req.source_kind is not None:
        fields["source_kind"] = req.source_kind
    if req.source_ref_id is not None:
        fields["source_ref_id"] = req.source_ref_id
    if fields:
        await _db.update_plan_item(item_id, **fields)
        updated_item = await _db.get_plan_item_by_id(item_id)
        if updated_item is not None:
            plan_items = await _db.list_plan_items(int(updated_item.plan_id))
            await _db.update_daily_plan(int(updated_item.plan_id), raw_plan=_plan_items_to_raw_plan(plan_items))
    return {"message": "Plan item updated"}


@router.put("/plans/{plan_id}/bulk-edit")
async def api_bulk_update_plan(plan_id: int, req: BulkUpdatePlanRequest):
    plan = await _db.get_daily_plan_by_id(plan_id)
    if plan is None:
        raise HTTPException(404, "Plan not found")
    normalized_items: list[PlanItem] = []
    for raw in req.items:
        hs = max(0, int(raw.hour_start))
        he = max(1, int(raw.hour_end))
        if he <= hs:
            raise HTTPException(400, f"Invalid plan item range: {hs}-{he}")
        normalized_items.append(
            PlanItem(
                plan_id=plan_id,
                hour_start=hs,
                hour_end=he,
                activity=str(raw.activity or "").strip(),
                action_type=_normalize_plan_action_type(raw.action_type),  # type: ignore[arg-type]
                action_payload=json.dumps(_sanitize_plan_payload(raw.action_payload), ensure_ascii=False),
                status=raw.status,  # type: ignore[arg-type]
                outcome=str(raw.outcome or "").strip(),
                source_kind=_normalize_plan_source_kind(raw.source_kind),  # type: ignore[arg-type]
                source_ref_id=raw.source_ref_id,
                executed_at=raw.executed_at,
            )
        )
    normalized_items.sort(key=lambda item: (item.hour_start, item.hour_end, item.activity))
    await _db.delete_plan_items_for_plan(plan_id)
    for item in normalized_items:
        await _db.insert_plan_item(item)
    fresh_items = await _db.list_plan_items(plan_id)
    await _db.update_daily_plan(plan_id, raw_plan=_plan_items_to_raw_plan(fresh_items))
    fresh_plan = await _db.get_daily_plan_by_id(plan_id)
    return {
        "message": "Plan updated",
        "plan": fresh_plan.model_dump() if fresh_plan else None,
        "items": [item.model_dump() for item in fresh_items],
    }


@router.post("/plans/generate")
async def api_generate_plan(req: GeneratePlanRequest):
    if _plan_engine is None:
        raise HTTPException(503, "Plan engine is not initialized")
    plan = await _plan_engine.generate_daily_plan(req.plan_date or shanghai_now().date().isoformat())
    items = await _db.list_plan_items(int(plan.id or 0))
    return {"plan": plan.model_dump(), "items": [item.model_dump() for item in items]}


@router.post("/plans/replan")
async def api_replan(req: ReplanRequest):
    if _plan_engine is None:
        raise HTTPException(503, "Plan engine is not initialized")
    plan = await _plan_engine.maybe_replan(trigger=req.trigger, context=req.context)
    if plan is None:
        return {"plan": None, "message": "No replan needed"}
    items = await _db.list_plan_items(int(plan.id or 0))
    return {"plan": plan.model_dump(), "items": [item.model_dump() for item in items]}


@router.post("/plans/{plan_id}/use-as-baseline")
async def api_use_plan_as_baseline(plan_id: int):
    if _plan_engine is None:
        raise HTTPException(503, "Plan engine is not initialized")
    try:
        plan = await _plan_engine.set_baseline_plan(plan_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    items = await _db.list_plan_items(int(plan.id or 0))
    return {
        "message": "Baseline plan updated",
        "baseline_plan_id": int(plan.id or 0),
        "plan": plan.model_dump(),
        "items": [item.model_dump() for item in items],
    }


@router.get("/npcs")
async def api_list_npcs(offset: int = 0, limit: int = 100, status: str | None = None):
    items = await _db.list_npc_entities(offset=offset, limit=limit, status=status)
    return {"items": [item.model_dump() for item in items]}


@router.get("/npcs/{npc_id}")
async def api_get_npc(npc_id: int):
    item = await _db.get_npc_entity_by_id(npc_id)
    if item is None:
        raise HTTPException(404, "NPC not found")
    return item.model_dump()


@router.post("/npcs")
async def api_create_npc(req: CreateNPCRequest):
    item = NPCEntity(
        name=req.name.strip(),
        role=req.role.strip(),
        background=req.background.strip(),
        relationship_to_character=req.relationship_to_character.strip(),
        personality_traits=json.dumps(req.personality_traits, ensure_ascii=False),
        status=req.status,
        spawn_source=req.spawn_source,
        spawn_context=req.spawn_context.strip(),
        notes=req.notes.strip(),
    )
    item.id = await _db.insert_npc_entity(item)
    return {"id": item.id, "message": "NPC created"}


@router.put("/npcs/{npc_id}")
async def api_update_npc(npc_id: int, req: UpdateNPCRequest):
    item = await _db.get_npc_entity_by_id(npc_id)
    if item is None:
        raise HTTPException(404, "NPC not found")
    fields = {}
    if req.name is not None:
        fields["name"] = req.name.strip()
    if req.role is not None:
        fields["role"] = req.role.strip()
    if req.background is not None:
        fields["background"] = req.background.strip()
    if req.relationship_to_character is not None:
        fields["relationship_to_character"] = req.relationship_to_character.strip()
    if req.personality_traits is not None:
        fields["personality_traits"] = json.dumps(req.personality_traits, ensure_ascii=False)
    if req.status is not None:
        fields["status"] = req.status
    if req.spawn_source is not None:
        fields["spawn_source"] = req.spawn_source
    if req.spawn_context is not None:
        fields["spawn_context"] = req.spawn_context.strip()
    if req.notes is not None:
        fields["notes"] = req.notes.strip()
    await _db.update_npc_entity(npc_id, **fields)
    return {"message": "NPC updated"}


@router.get("/notifications")
async def api_list_notifications(status: str | None = "pending", offset: int = 0, limit: int = 100):
    items = await _db.list_character_notifications(status=status, offset=offset, limit=limit)
    return {"items": [item.model_dump() for item in items]}


@router.post("/notifications/{notification_id}/read")
async def api_mark_notification_read(notification_id: int):
    item = await _db.get_character_notification_by_id(notification_id)
    if item is None:
        raise HTTPException(404, "Notification not found")
    await _db.update_character_notification(notification_id, status="read")
    return {"message": "Notification marked as read"}


@router.get("/notifications/history")
async def api_list_notification_history(offset: int = 0, limit: int = 100):
    items = await _db.list_character_notifications(offset=offset, limit=limit)
    return {"items": [item.model_dump() for item in items]}


# ── Key Records ──

@router.get("/key-records")
async def list_key_records(
    offset: int = 0,
    limit: int = 50,
    record_type: str | None = None,
    status: str | None = None,
    life_scope: str | None = None,
    include_archived: bool = False,
):
    if life_scope not in {None, "", "user_life", "character_life", "shared_life"}:
        raise HTTPException(400, "Unsupported key record life_scope")
    items = await _db.get_all_key_records(
        offset=offset,
        limit=limit,
        record_type=record_type,
        status=status,
        life_scope=life_scope or None,
        include_archived=include_archived,
    )
    return {"items": [_serialize_key_record(i) for i in items]}


@router.post("/key-records/search")
async def search_key_records(req: KeyRecordSearchRequest):
    items = await _state_machine.recall_key_records(
        query=req.query,
        top_k=req.top_k,
        record_type=req.type,
        life_scope=req.life_scope,
        include_archived=req.include_archived,
        include_world_books=False,
    )
    return {"items": items, "include_world_books": False}


@router.get("/key-records/{record_id}")
async def get_key_record(record_id: int):
    item = await _db.get_key_record_by_id(record_id)
    if not item:
        raise HTTPException(404, "Key record not found")
    return _serialize_key_record(item)


@router.post("/key-records")
async def create_key_record(req: CreateKeyRecordRequest):
    now = datetime.utcnow().isoformat()
    normalized_type = _normalize_key_record_type_value(req.type)
    item = KeyRecord(
        type=(normalized_type or _state_machine._classify_key_record_type(
            title=req.title.strip(),
            content_text=req.content_text.strip(),
            tags=req.tags,
            content_json=req.content_json,
            start_date=req.start_date,
            end_date=req.end_date,
        )),
        title=req.title.strip(),
        content_text=req.content_text.strip(),
        content_json=json.dumps(req.content_json, ensure_ascii=False) if req.content_json is not None else None,
        tags=json.dumps(req.tags, ensure_ascii=False),
        match_keywords=json.dumps(req.match_keywords, ensure_ascii=False),
        start_date=req.start_date,
        end_date=req.end_date,
        status=req.status,
        source=req.source,
        life_scope=req.life_scope,
        linked_event_id=req.linked_event_id,
        created_at=now,
        updated_at=now,
    )
    record_id = await _db.insert_key_record(item)
    upsert_method = getattr(_memory_store, "upsert_key_record_vector", None)
    if callable(upsert_method):
        await upsert_method(record_id)
    try:
        await _state_machine.refresh_related_slowline_from_key_record_id(int(record_id))
    except Exception:
        logger.exception("Failed to refresh slowline after API key record create: %s", record_id)
    return {"id": record_id, "message": "Key record created"}


@router.put("/key-records/{record_id}")
async def update_key_record(record_id: int, req: UpdateKeyRecordRequest):
    item = await _db.get_key_record_by_id(record_id)
    if not item:
        raise HTTPException(404, "Key record not found")
    fields = {}
    if req.type is not None:
        normalized_type = _normalize_key_record_type_value(req.type)
        if normalized_type:
            fields["type"] = normalized_type
    if req.title is not None:
        fields["title"] = req.title.strip()
    if req.content_text is not None:
        fields["content_text"] = req.content_text.strip()
    if req.content_json is not None:
        fields["content_json"] = json.dumps(req.content_json, ensure_ascii=False)
    if req.tags is not None:
        fields["tags"] = json.dumps(req.tags, ensure_ascii=False)
    if req.match_keywords is not None:
        fields["match_keywords"] = json.dumps(req.match_keywords, ensure_ascii=False)
    if req.start_date is not None:
        fields["start_date"] = req.start_date
    if req.end_date is not None:
        fields["end_date"] = req.end_date
    if req.status is not None:
        fields["status"] = req.status
    if req.source is not None:
        fields["source"] = req.source
    if req.life_scope is not None:
        fields["life_scope"] = req.life_scope
    if req.linked_event_id is not None:
        fields["linked_event_id"] = req.linked_event_id
    if fields:
        await _db.update_key_record(record_id, **fields)
        upsert_method = getattr(_memory_store, "upsert_key_record_vector", None)
        if callable(upsert_method):
            await upsert_method(record_id)
        try:
            await _state_machine.refresh_related_slowline_from_key_record_id(int(record_id), previous_record=item)
        except Exception:
            logger.exception("Failed to refresh slowline after API key record update: %s", record_id)
    return {"message": "Key record updated"}


@router.delete("/key-records/{record_id}")
async def delete_key_record(record_id: int):
    item = await _db.get_key_record_by_id(record_id)
    if not item:
        raise HTTPException(404, "Key record not found")
    delete_method = getattr(_memory_store, "delete_key_record_vector", None)
    if callable(delete_method):
        await delete_method(record_id)
    await _db.delete_key_record(record_id)
    return {"message": "Key record deleted"}


@router.get("/key-records/relabel-preview")
async def key_record_relabel_preview(
    limit: int = 200,
    include_migrated: bool = False,
):
    items = await _db.get_all_key_records(offset=0, limit=max(1, min(limit, 500)), include_archived=True)
    out: list[dict] = []
    for item in items:
        current_type = str(item.type or "").strip()
        is_legacy = current_type in LEGACY_KEY_RECORD_TYPE_MAP
        if not include_migrated and not is_legacy:
            continue
        suggested = _state_machine._classify_key_record_type(
            title=str(item.title or ""),
            content_text=str(item.content_text or ""),
            tags=_parse_json_list(item.tags),
            content_json=_extract_json_object(str(item.content_json or "")) if item.content_json else None,
            start_date=item.start_date,
            end_date=item.end_date,
        )
        payload = item.model_dump()
        payload["legacy_type"] = current_type if is_legacy else None
        payload["suggested_type"] = suggested
        payload["needs_relabel"] = is_legacy or current_type != suggested
        out.append(payload)
    return {"items": out, "total": len(out)}


@router.post("/key-records/relabel-apply")
async def key_record_relabel_apply(payload: dict):
    record_ids = payload.get("record_ids") or []
    apply_all_legacy = bool(payload.get("apply_all_legacy"))
    items = await _db.get_all_key_records(offset=0, limit=1000, include_archived=True)
    selected: list[KeyRecord] = []
    selected_ids = {int(x) for x in record_ids if str(x).isdigit()}
    for item in items:
        item_id = int(item.id or 0)
        if apply_all_legacy and str(item.type or "").strip() in LEGACY_KEY_RECORD_TYPE_MAP:
            selected.append(item)
        elif item_id in selected_ids:
            selected.append(item)
    updated = 0
    skipped = 0
    for item in selected:
        suggested = _state_machine._classify_key_record_type(
            title=str(item.title or ""),
            content_text=str(item.content_text or ""),
            tags=_parse_json_list(item.tags),
            content_json=_extract_json_object(str(item.content_json or "")) if item.content_json else None,
            start_date=item.start_date,
            end_date=item.end_date,
        )
        if not suggested or str(item.type or "").strip() == suggested:
            skipped += 1
            continue
        await _db.update_key_record(int(item.id or 0), type=suggested)
        updated += 1
    return {"updated": updated, "skipped": skipped, "selected": len(selected)}


@router.post("/key-records/vectorize-batch")
async def batch_vectorize_key_records(req: KeyRecordBatchVectorizeRequest):
    upsert_method = getattr(_memory_store, "upsert_key_record_vector", None)
    if not callable(upsert_method):
        raise HTTPException(400, "Current memory store does not support key record vectorization")

    target_ids = [int(x) for x in req.item_ids if int(x) > 0]
    if target_ids:
        items = await _db.get_key_records_by_ids(target_ids)
    else:
        items = await _db.get_all_key_records(
            offset=0,
            limit=1000,
            include_archived=req.include_archived,
        )

    processed = 0
    vectorized = 0
    failed = 0
    details: list[dict] = []
    for item in items:
        processed += 1
        try:
            ok = await upsert_method(int(item.id or 0))
            if ok:
                vectorized += 1
                details.append({"id": item.id, "status": "vectorized"})
            else:
                failed += 1
                details.append({"id": item.id, "status": "failed", "reason": "upsert returned false"})
        except Exception as exc:
            failed += 1
            details.append({"id": item.id, "status": "failed", "reason": str(exc)})

    return {
        "message": "Key record batch vectorization completed",
        "processed": processed,
        "vectorized": vectorized,
        "failed": failed,
        "details": details,
    }


@router.get("/world-books")
async def list_world_books(offset: int = 0, limit: int = 100):
    items = await _db.list_world_books(offset=offset, limit=limit)
    return {"items": [_serialize_world_book(i) for i in items]}


@router.post("/world-books/search")
async def search_world_books(req: WorldBookSearchRequest):
    items = await _state_machine.recall_world_books(
        query=req.query,
        top_k=req.top_k,
        include_inactive=req.include_inactive,
    )
    return {"items": items}


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
    if req.type == "conversation_end" and _state_machine is not None:
        try:
            await _state_machine._close_active_conversation_time_claim(
                ended_at=shanghai_now(),
                closing_snapshot_id=int(snap_id or 0),
                context_summary=req.content,
            )
        except Exception:
            logger.exception("Failed to close active conversation claim after manual conversation_end snapshot.")
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
    sources: str | None = None,
    scored_only: bool = False,
    min_importance_score: float | None = None,
    max_importance_score: float | None = None,
    min_impression_depth: float | None = None,
    max_impression_depth: float | None = None,
):
    category_list = [c.strip() for c in (categories or "").split(",") if c.strip()]
    source_list = [s.strip() for s in (sources or "").split(",") if s.strip()]
    events = await _db.get_all_events(
        offset=offset,
        limit=limit,
        include_archived=include_archived,
        categories=category_list,
        sources=source_list,
        scored_only=scored_only,
        min_importance_score=min_importance_score,
        max_importance_score=max_importance_score,
        min_impression_depth=min_impression_depth,
        max_impression_depth=max_impression_depth,
    )
    normalized = [await _ensure_event_meta(e) for e in events]
    total = await _db.count_events(
        include_archived=include_archived,
        categories=category_list,
        sources=source_list,
        scored_only=scored_only,
        min_importance_score=min_importance_score,
        max_importance_score=max_importance_score,
        min_impression_depth=min_impression_depth,
        max_impression_depth=max_impression_depth,
    )
    return {
        "items": [e.model_dump() for e in normalized],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


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
        meta_json=json.dumps(req.meta_json, ensure_ascii=False) if req.meta_json is not None else None,
    )
    event_id = await _db.insert_event(event)
    upsert_event_vector = getattr(_memory_store, "upsert_event_vector", None)
    if callable(upsert_event_vector):
        await upsert_event_vector(int(event_id))
    else:
        sync_method = getattr(_memory_store, "sync_eligible_vectors", None)
        if callable(sync_method):
            await sync_method()
    try:
        await _state_machine.refresh_related_slowline_from_event_id(int(event_id))
    except Exception:
        logger.exception("Failed to refresh slowline after API event create: %s", event_id)
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
    if req.meta_json is not None:
        fields["meta_json"] = json.dumps(req.meta_json, ensure_ascii=False)
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
        try:
            await _state_machine.refresh_related_slowline_from_event_id(int(event_id))
        except Exception:
            logger.exception("Failed to refresh slowline after API event update: %s", event_id)
    return {"message": "Event updated"}


@router.delete("/events/{event_id}")
async def delete_event(event_id: int):
    event = await _db.get_event_by_id(event_id)
    if not event:
        raise HTTPException(404, "Event not found")
    await _db.delete_event(event_id)
    await _memory_store.delete(f"event_{event_id}")
    return {"message": "Event deleted"}


@router.post("/events/delete-by-score")
async def delete_events_by_score(req: DeleteEventsByScoreRequest = Body(...)):
    total = await _db.count_events(
        include_archived=req.include_archived,
        categories=req.categories,
        sources=req.sources,
        scored_only=req.scored_only,
        min_importance_score=req.min_importance_score,
        max_importance_score=req.max_importance_score,
        min_impression_depth=req.min_impression_depth,
        max_impression_depth=req.max_impression_depth,
    )
    events = await _db.get_all_events(
        offset=0,
        limit=max(total, 1),
        include_archived=req.include_archived,
        categories=req.categories,
        sources=req.sources,
        scored_only=req.scored_only,
        min_importance_score=req.min_importance_score,
        max_importance_score=req.max_importance_score,
        min_impression_depth=req.min_impression_depth,
        max_impression_depth=req.max_impression_depth,
    )
    deleted = await _db.delete_events_by_filters(
        include_archived=req.include_archived,
        categories=req.categories,
        sources=req.sources,
        scored_only=req.scored_only,
        min_importance_score=req.min_importance_score,
        max_importance_score=req.max_importance_score,
        min_impression_depth=req.min_impression_depth,
        max_impression_depth=req.max_impression_depth,
    )
    for event in events:
        try:
            await _memory_store.delete(f"event_{int(event.id)}")
        except Exception:
            continue
    return {"deleted": deleted, "message": f"Deleted {deleted} events"}


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
    settings = dict(settings)
    settings["embedding_api_key_set"] = bool(str(settings.get("embedding_api_key") or "").strip())
    settings["embedding_api_key"] = mask_secret(settings.get("embedding_api_key", ""))
    settings["embedding_api_key_source"] = "env" if settings["embedding_api_key_set"] else "missing"
    return {"settings": settings}


@router.put("/vectors/settings")
async def update_vector_settings(req: UpdateVectorSettingsRequest):
    store = _ensure_vector_store()
    payload = req.model_dump(exclude_none=True)
    try:
        if payload.get("vector_embedding_api_base") is not None:
            current = await store.get_runtime_config()
            new_base = validate_api_base(payload.get("vector_embedding_api_base"), "vector_embedding_api_base")
            current_base = validate_api_base(current.get("embedding_api_base", ""), "vector_embedding_api_base")
            if (
                new_base
                and new_base != current_base
                and not get_secret_from_env("vector_embedding_api_key", "")
            ):
                raise HTTPException(
                    status_code=400,
                    detail="Changing vector API Base requires KELSEY_VECTOR_EMBEDDING_API_KEY in the server environment.",
                )
            payload["vector_embedding_api_base"] = new_base
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await store.update_runtime_config(payload)
    return {"message": "Vector settings updated"}


# ── Runtime LLM API config ──

@router.get("/environment/llm-config")
async def get_environment_llm_config():
    settings = await _get_llm_config("env_llm")
    return {"settings": _public_llm_config(settings)}


@router.post("/environment/llm-config")
async def update_environment_llm_config(payload: dict):
    current = await _get_llm_config("env_llm")
    try:
        if _base_changes_without_new_key(current, payload.get("api_base"), current.get("api_key")):
            raise HTTPException(
                status_code=400,
                detail="Changing API Base requires KELSEY_ENV_LLM_API_KEY in the server environment.",
            )
        api_base = validate_api_base(payload.get("api_base", ""), "api_base")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if _env_llm_client is not None:
        await _env_llm_client.update_runtime_config(
            {
                "env_llm_enabled": "1" if _to_int_flag(payload.get("enabled"), 0) == 1 else "0",
                "env_llm_api_base": api_base,
                "env_llm_model": payload.get("model", ""),
            }
        )
    else:
        await _save_llm_config("env_llm", payload)
    return {"message": "Environment LLM settings updated"}


@router.get("/snapshot/llm-config")
async def get_snapshot_llm_config():
    settings = await _get_llm_config("snapshot_llm")
    return {"settings": _public_llm_config(settings)}


@router.post("/snapshot/llm-config")
async def update_snapshot_llm_config(payload: dict):
    current = await _get_llm_config("snapshot_llm")
    try:
        if _base_changes_without_new_key(current, payload.get("api_base"), current.get("api_key")):
            raise HTTPException(
                status_code=400,
                detail="Changing API Base requires KELSEY_SNAPSHOT_LLM_API_KEY in the server environment.",
            )
        api_base = validate_api_base(payload.get("api_base", ""), "api_base")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if _snapshot_llm_client is not None:
        await _snapshot_llm_client.update_runtime_config(
            {
                "snapshot_llm_enabled": "1" if _to_int_flag(payload.get("enabled"), 0) == 1 else "0",
                "snapshot_llm_api_base": api_base,
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
    return {"settings": _public_llm_config(await _llm_client.get_runtime_config())}


@router.put("/runtime/llm")
async def update_runtime_llm(req: UpdateRuntimeLLMRequest):
    if _llm_client is None:
        raise HTTPException(500, "LLM client is not initialized")
    payload = req.model_dump(exclude_none=True)
    try:
        if payload.get("llm_api_base") is not None:
            current = await _llm_client.get_runtime_config()
            new_base = validate_api_base(payload.get("llm_api_base"), "llm_api_base")
            current_base = validate_api_base(current.get("api_base", ""), "llm_api_base")
            if (
                new_base
                and new_base != current_base
                and not get_secret_from_env("llm_api_key", "")
            ):
                raise HTTPException(
                    status_code=400,
                    detail="Changing API Base requires KELSEY_LLM_API_KEY in the server environment.",
                )
            payload["llm_api_base"] = new_base
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
    items = _redact_settings(await _db.get_all_settings())
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
    return _redact_setting_item(item)


@router.put("/settings/{key}")
async def update_setting(key: str, req: UpdateSettingRequest):
    if key not in DEFAULT_SETTINGS:
        raise HTTPException(400, "Unsupported setting key")
    if is_sensitive_setting_key(key):
        return {"message": "Secret settings are managed by server environment variables"}
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
        if is_sensitive_setting_key(key):
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
                meta_json=json.dumps(item.get("meta_json"), ensure_ascii=False) if isinstance(item.get("meta_json"), dict) else item.get("meta_json"),
                archived=_to_int_flag(item.get("archived"), 0),
                importance_score=item.get("importance_score"),
                impression_depth=item.get("impression_depth"),
            )
            event_id = await _db.insert_event(event)
            try:
                await _state_machine.refresh_related_slowline_from_event_id(int(event_id))
            except Exception:
                logger.exception("Failed to refresh slowline after imported event: %s", event_id)
            result["events"]["imported"] += 1
        except Exception as exc:
            result["events"]["errors"].append(f"index={idx}: {exc}")

    # 4) Key Records
    for idx, item in enumerate(req.key_records or []):
        try:
            record_type = _normalize_key_record_type_value(item.get("type"))
            title = str(item.get("title") or "").strip()
            content_text = str(item.get("content_text") or "").strip()
            if not record_type:
                record_type = _state_machine._classify_key_record_type(
                    title=title,
                    content_text=content_text,
                    tags=_parse_json_list(item.get("tags")),
                    content_json=item.get("content_json") if isinstance(item.get("content_json"), dict) else None,
                    start_date=item.get("start_date"),
                    end_date=item.get("end_date"),
                )
            if record_type not in KEY_RECORD_TYPES:
                result["key_records"]["skipped"] += 1
                continue
            if not title or not content_text:
                result["key_records"]["skipped"] += 1
                continue
            tags_text = _to_json_array_text(item.get("tags"))
            match_keywords_raw = _parse_json_list(item.get("match_keywords"))
            content_json = item.get("content_json")
            if isinstance(content_json, str):
                content_json_text = content_json
            elif content_json is None:
                content_json_text = None
            else:
                content_json_text = json.dumps(content_json, ensure_ascii=False)
            match_keywords = match_keywords_raw
            status = str(item.get("status") or "active")
            if status not in {"active", "archived"}:
                status = "active"
            source = str(item.get("source") or "manual")
            if source not in {"manual", "conversation", "generated"}:
                source = "manual"
            life_scope = str(item.get("life_scope") or "user_life")
            if life_scope not in {"user_life", "character_life", "shared_life"}:
                life_scope = "user_life"

            if req.upsert_key_records:
                existing = await _db.get_key_record_by_type_title(record_type, title, life_scope=life_scope)
                if existing:
                    await _db.update_key_record(
                        int(existing.id),
                        content_text=content_text,
                        content_json=content_json_text,
                        tags=tags_text,
                        match_keywords=json.dumps(match_keywords, ensure_ascii=False),
                        start_date=item.get("start_date"),
                        end_date=item.get("end_date"),
                        status=status,
                        source=source,
                        life_scope=life_scope,
                        linked_event_id=item.get("linked_event_id"),
                    )
                    upsert_method = getattr(_memory_store, "upsert_key_record_vector", None)
                    if callable(upsert_method):
                        await upsert_method(int(existing.id))
                    try:
                        await _state_machine.refresh_related_slowline_from_key_record_id(int(existing.id), previous_record=existing)
                    except Exception:
                        logger.exception("Failed to refresh slowline after imported key record update: %s", existing.id)
                    result["key_records"]["updated"] += 1
                    continue

            record = KeyRecord(
                type=record_type,  # type: ignore[arg-type]
                title=title,
                content_text=content_text,
                content_json=content_json_text,
                tags=tags_text,
                match_keywords=json.dumps(match_keywords, ensure_ascii=False),
                start_date=item.get("start_date"),
                end_date=item.get("end_date"),
                status=status,  # type: ignore[arg-type]
                source=source,  # type: ignore[arg-type]
                life_scope=life_scope,  # type: ignore[arg-type]
                linked_event_id=item.get("linked_event_id"),
                created_at=str(item.get("created_at") or now_iso),
                updated_at=str(item.get("updated_at") or now_iso),
            )
            record_id = await _db.insert_key_record(record)
            try:
                await _state_machine.refresh_related_slowline_from_key_record_id(int(record_id))
            except Exception:
                logger.exception("Failed to refresh slowline after imported key record create: %s", record_id)
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
