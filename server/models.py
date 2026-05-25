from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field

from server.time_display import iso_string_for_cst_display, shanghai_now


KEY_RECORD_TYPES = (
    "medication_protocol",
    "health_monitoring",
    "dietary_intervention",
    "anniversary_date",
    "medical_review_date",
    "lifecycle_milestone",
    "key_collaboration",
    "commitment_agreement",
    "emotional_anchor",
    "life_pattern",
    "medical_tracking",
    "appointment",
    "project",
    "academic",
)

LEGACY_KEY_RECORD_TYPE_MAP = {
    "important_date": "anniversary_date",
    "important_item": "life_pattern",
    "key_collaboration": "key_collaboration",
    "medical_advice": "health_monitoring",
}


def format_utc_instant_z(dt: datetime) -> str:
    """将 UTC 时刻写入 DB 时使用，带 Z 后缀，避免 naive iso 与 utcnow() 比较时出现时区歧义。"""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat() + "Z"


class StateSnapshot(BaseModel):
    id: int | None = None
    # 叙事/检查点时间（推进逻辑与「最新快照」排序均按此字段）
    created_at: str = Field(default_factory=lambda: format_utc_instant_z(datetime.utcnow()))
    # 行写入数据库时的 UTC 时刻（Z）；旧数据迁移前可能为空
    inserted_at: str | None = None
    type: Literal["daily", "conversation_end", "accumulated"] = "daily"
    content: str = ""
    environment: str = "{}"
    referenced_events: str = "[]"
    embedding_vector_id: str | None = None

    @computed_field
    @property
    def created_at_cst(self) -> str:
        return iso_string_for_cst_display(self.created_at)

    @computed_field
    @property
    def inserted_at_cst(self) -> str | None:
        if not self.inserted_at:
            return None
        return iso_string_for_cst_display(self.inserted_at)


class EventAnchor(BaseModel):
    id: int | None = None
    date: str = Field(default_factory=lambda: shanghai_now().date().isoformat())
    title: str = ""
    description: str = ""
    source: Literal["generated", "manual", "conversation"] = "generated"
    created_at: str = Field(default_factory=lambda: format_utc_instant_z(datetime.utcnow()))
    embedding_vector_id: str | None = None
    trigger_keywords: str = "[]"
    categories: str = "[]"
    meta_json: str | None = None
    archived: int = 0
    importance_score: float | None = None
    impression_depth: float | None = None


class KeyRecord(BaseModel):
    id: int | None = None
    type: Literal[
        "medication_protocol",
        "health_monitoring",
        "dietary_intervention",
        "anniversary_date",
        "medical_review_date",
        "lifecycle_milestone",
        "key_collaboration",
        "commitment_agreement",
        "emotional_anchor",
        "life_pattern",
        "medical_tracking",
        "appointment",
        "project",
        "academic",
        "important_date",
        "important_item",
        "medical_advice",
    ] = "life_pattern"
    title: str = ""
    content_text: str = ""
    content_json: str | None = None
    tags: str = "[]"
    match_keywords: str = "[]"
    start_date: str | None = None
    end_date: str | None = None
    status: Literal["active", "archived"] = "active"
    source: Literal["manual", "conversation", "generated"] = "manual"
    life_scope: Literal["user_life", "character_life", "shared_life"] = "user_life"
    linked_event_id: int | None = None
    embedding_vector_id: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class WorldBook(BaseModel):
    id: int | None = None
    name: str = ""
    content: str = ""
    tags: str = "[]"
    match_keywords: str = "[]"
    is_active: int = 1
    embedding_vector_id: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class DailyPlan(BaseModel):
    id: int | None = None
    plan_date: str = Field(default_factory=lambda: shanghai_now().date().isoformat())
    generated_at: str = Field(default_factory=lambda: format_utc_instant_z(datetime.utcnow()))
    raw_plan: str = ""
    status: Literal["active", "completed", "replanned"] = "active"
    replan_trigger: str | None = None
    replan_parent_id: int | None = None
    context_snapshot: str = "{}"
    created_at: str = Field(default_factory=lambda: format_utc_instant_z(datetime.utcnow()))


class PlanItem(BaseModel):
    id: int | None = None
    plan_id: int
    hour_start: int = 0
    hour_end: int = 1
    activity: str = ""
    action_type: Literal["internal", "web_search", "npc_interaction"] = "internal"
    action_payload: str = "{}"
    status: Literal["pending", "executing", "done", "skipped"] = "pending"
    outcome: str = ""
    outcome_event_id: int | None = None
    source_kind: Literal["generated", "routine", "carried_over", "thread", "spontaneous", "replan"] = "generated"
    source_ref_id: int | None = None
    created_at: str = Field(default_factory=lambda: format_utc_instant_z(datetime.utcnow()))
    executed_at: str | None = None


class LifeFlowTrace(BaseModel):
    id: int | None = None
    trace_date: str = Field(default_factory=lambda: shanghai_now().date().isoformat())
    source: Literal["environment", "conversation", "manual"] = "environment"
    summary: str = ""
    details_json: str = "{}"
    schedule_alignment: Literal[
        "on_track",
        "delayed",
        "interrupted",
        "replaced_by_conversation",
        "cancelled",
        "unexpected_inserted",
    ] = "on_track"
    related_snapshot_id: int | None = None
    related_event_ids: str = "[]"
    created_at: str = Field(default_factory=lambda: format_utc_instant_z(datetime.utcnow()))
    updated_at: str = Field(default_factory=lambda: format_utc_instant_z(datetime.utcnow()))


class DisturbancePulse(BaseModel):
    id: int | None = None
    occur_at: str = Field(default_factory=lambda: format_utc_instant_z(datetime.utcnow()))
    reveal_at: str = Field(default_factory=lambda: format_utc_instant_z(datetime.utcnow()))
    status: Literal["pending", "injected", "consumed", "suppressed"] = "pending"
    channel_type: Literal["endogenous_reveal", "external_incident"] = "endogenous_reveal"
    source_family: Literal[
        "relationship",
        "body",
        "task",
        "npc",
        "environment",
        "device",
        "public_world",
    ] = "task"
    seed_kind: Literal[
        "event",
        "key_record",
        "plan_item",
        "life_flow",
        "npc",
        "conversation_claim",
        "world_state",
    ] = "event"
    seed_ref_id: int | None = None
    blind_spot_reason: str = ""
    reveal_channel: str = ""
    title: str = ""
    factual_payload_json: str = "{}"
    impact_hint: str = ""
    salience: float = 0.5
    novelty_score: float = 0.5
    cooldown_until: str | None = None
    fingerprint: str = ""
    linked_snapshot_id: int | None = None
    linked_event_id: int | None = None
    created_at: str = Field(default_factory=lambda: format_utc_instant_z(datetime.utcnow()))
    updated_at: str = Field(default_factory=lambda: format_utc_instant_z(datetime.utcnow()))


class ConversationTimeClaim(BaseModel):
    id: int | None = None
    status: Literal["active", "closed"] = "active"
    started_at: str = Field(default_factory=lambda: format_utc_instant_z(datetime.utcnow()))
    ended_at: str | None = None
    source: Literal["get_current_state", "manual"] = "get_current_state"
    context_summary: str = ""
    latest_snapshot_id: int | None = None
    closing_snapshot_id: int | None = None
    created_at: str = Field(default_factory=lambda: format_utc_instant_z(datetime.utcnow()))
    updated_at: str = Field(default_factory=lambda: format_utc_instant_z(datetime.utcnow()))


class RelationshipState(BaseModel):
    id: int | None = None
    last_meaningful_contact_at: str | None = None
    hours_since_meaningful_contact: float = 0.0
    days_since_meaningful_contact: int = 0
    contact_recency_bucket: Literal["active", "cooling", "distant", "stale"] = "active"
    connection_need: float = 0.5
    pride_or_distance: float = 0.5
    valence: float = 0.5
    arousal: float = 0.5
    life_immersion: float = 0.5
    relationship_feeling_summary: str = ""
    space_need_level: float = 0.5
    concern_level: float = 0.5
    proactive_topics: str = "[]"
    plan_bias_hint: str = ""
    created_at: str = Field(default_factory=lambda: format_utc_instant_z(datetime.utcnow()))
    updated_at: str = Field(default_factory=lambda: format_utc_instant_z(datetime.utcnow()))


class RelationshipThought(BaseModel):
    id: int | None = None
    thought_date: str = Field(default_factory=lambda: shanghai_now().date().isoformat())
    source_snapshot_id: int | None = None
    source_env_id: str | None = None
    topic_line: str = ""
    thought_type: Literal[
        "say",
        "ask",
        "do_if_present",
        "wait",
        "doubt",
        "reconsider",
        "timing_sensitive",
        "mood_dependent",
    ] = "reconsider"
    content: str = ""
    salience: float = 0.5
    dedupe_fingerprint: str = ""
    resolution_status: Literal["open", "cooled", "superseded"] = "open"
    created_at: str = Field(default_factory=lambda: format_utc_instant_z(datetime.utcnow()))
    updated_at: str = Field(default_factory=lambda: format_utc_instant_z(datetime.utcnow()))


class SlowLine(BaseModel):
    id: int | None = None
    thread_key: str = ""
    theme: str = ""
    scope: Literal["user_side", "character_side", "shared"] = "shared"
    source_family: Literal["health", "study", "work", "relationship", "logistics", "daily_life"] = "daily_life"
    memory_role: Literal["bridge_core", "active_thread_detail", "trigger_only", "archive_reference"] = "active_thread_detail"
    progress_status: Literal["open", "advancing", "paused", "ready_to_close", "completed", "dropped"] = "open"
    tension_level: Literal["low", "medium", "high"] = "medium"
    unresolved_level: Literal["low", "medium", "high"] = "medium"
    preload_priority: float = 0.5
    stage_summary: str = ""
    trajectory_summary: str = ""
    current_tension: str = ""
    recent_shift_summary: str = ""
    recent_movement_summary: str = ""
    last_meaningful_shift_at: str | None = None
    emotional_tension: Literal["stable", "strained", "brittle", "tender", "suspended", "unresolved"] = "stable"
    affective_direction: Literal["approach", "avoidance", "ambivalence", "endurance", "repair"] = "endurance"
    open_questions: str = "[]"
    salience: float = 0.5
    last_touched_at: str | None = None
    linked_key_record_ids: str = "[]"
    linked_event_ids: str = "[]"
    status: Literal["active", "archived"] = "active"
    created_at: str = Field(default_factory=lambda: format_utc_instant_z(datetime.utcnow()))
    updated_at: str = Field(default_factory=lambda: format_utc_instant_z(datetime.utcnow()))


class NPCEntity(BaseModel):
    id: int | None = None
    name: str
    role: str = ""
    background: str = ""
    relationship_to_character: str = ""
    personality_traits: str = "[]"
    status: Literal["active", "inactive", "departed"] = "active"
    spawn_source: Literal["manual", "auto_generated"] = "manual"
    spawn_context: str = ""
    last_interaction_at: str | None = None
    interaction_count: int = 0
    notes: str = ""
    created_at: str = Field(default_factory=lambda: format_utc_instant_z(datetime.utcnow()))
    updated_at: str = Field(default_factory=lambda: format_utc_instant_z(datetime.utcnow()))


class CharacterNotification(BaseModel):
    id: int | None = None
    trigger_type: Literal["plan_item", "replan", "npc_event", "spontaneous"] = "plan_item"
    trigger_item_id: int | None = None
    message_text: str = ""
    tone: Literal["neutral", "warm", "urgent", "playful"] = "neutral"
    status: Literal["pending", "delivered", "read", "expired"] = "pending"
    created_at: str = Field(default_factory=lambda: format_utc_instant_z(datetime.utcnow()))
    delivered_at: str | None = None
    expires_at: str | None = None


# --- API request/response models ---

class CreateSnapshotRequest(BaseModel):
    content: str
    type: Literal["daily", "conversation_end", "accumulated"] = "accumulated"
    environment: str = "{}"


class CreateEventRequest(BaseModel):
    date: str | None = None
    title: str | None = None
    description: str
    source: Literal["generated", "manual", "conversation"] = "manual"
    trigger_keywords: list[str] = Field(default_factory=list)
    categories: list[str] | None = None
    meta_json: dict | None = None


class UpdateEventRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    trigger_keywords: list[str] | None = None
    categories: list[str] | None = None
    meta_json: dict | None = None
    archived: int | None = None
    importance_score: float | None = None
    impression_depth: float | None = None


class DeleteEventsByScoreRequest(BaseModel):
    include_archived: bool = False
    categories: list[str] = Field(default_factory=list)
    sources: list[Literal["generated", "manual", "conversation"]] = Field(default_factory=list)
    scored_only: bool = True
    min_importance_score: float | None = None
    max_importance_score: float | None = None
    min_impression_depth: float | None = None
    max_impression_depth: float | None = None


class CreateKeyRecordRequest(BaseModel):
    type: Literal[
        "medication_protocol",
        "health_monitoring",
        "dietary_intervention",
        "anniversary_date",
        "medical_review_date",
        "lifecycle_milestone",
        "key_collaboration",
        "commitment_agreement",
        "emotional_anchor",
        "life_pattern",
        "medical_tracking",
        "appointment",
        "project",
        "academic",
    ] | None = None
    title: str
    content_text: str
    content_json: dict | None = None
    tags: list[str] = Field(default_factory=list)
    match_keywords: list[str] = Field(default_factory=list)
    start_date: str | None = None
    end_date: str | None = None
    status: Literal["active", "archived"] = "active"
    source: Literal["manual", "conversation", "generated"] = "manual"
    life_scope: Literal["user_life", "character_life", "shared_life"] = "user_life"
    linked_event_id: int | None = None


class UpdateKeyRecordRequest(BaseModel):
    type: Literal[
        "medication_protocol",
        "health_monitoring",
        "dietary_intervention",
        "anniversary_date",
        "medical_review_date",
        "lifecycle_milestone",
        "key_collaboration",
        "commitment_agreement",
        "emotional_anchor",
        "life_pattern",
        "medical_tracking",
        "appointment",
        "project",
        "academic",
    ] | None = None
    title: str | None = None
    content_text: str | None = None
    content_json: dict | None = None
    tags: list[str] | None = None
    match_keywords: list[str] | None = None
    start_date: str | None = None
    end_date: str | None = None
    status: Literal["active", "archived"] | None = None
    source: Literal["manual", "conversation", "generated"] | None = None
    life_scope: Literal["user_life", "character_life", "shared_life"] | None = None
    linked_event_id: int | None = None


class WorldBookCreateRequest(BaseModel):
    name: str = ""
    content: str
    tags: list[str] = Field(default_factory=list)
    match_keywords: list[str] = Field(default_factory=list)
    is_active: bool = True


class WorldBookUpdateRequest(BaseModel):
    name: str | None = None
    content: str | None = None
    tags: list[str] | None = None
    match_keywords: list[str] | None = None
    is_active: bool | None = None


class WorldBookAutoMetaRequest(BaseModel):
    item_ids: list[int] = Field(default_factory=list)
    overwrite_title: bool = False
    overwrite_keywords: bool = False


class WorldBookSearchRequest(BaseModel):
    query: str
    top_k: int = 5
    include_inactive: bool = False


class KeyRecordBatchVectorizeRequest(BaseModel):
    item_ids: list[int] = Field(default_factory=list)
    include_archived: bool = False


class WorldBookJsonImportRequest(BaseModel):
    """Body: `{ "data": <酒馆/世界书导出 JSON 根对象>, "skip_disabled": false }`。"""

    data: Any
    skip_disabled: bool = False


class MemorySearchRequest(BaseModel):
    query: str
    top_k: int = 5


class GetCurrentStateRequest(BaseModel):
    current_time: str
    # 兼容旧调用保留，可不传；实际 last_interaction 检查点来自 DB 的 conversation_end
    last_interaction_time: str | None = None
    # 为 True 时在响应中附带 checkpoint_schedule，便于核对整格/尾部逻辑
    include_checkpoint_schedule: bool = False


class ReflectRequest(BaseModel):
    conversation_summary: str


class GeneratePlanRequest(BaseModel):
    plan_date: str | None = None


class ReplanRequest(BaseModel):
    trigger: str = "manual"
    context: str = ""


class UpdatePlanItemRequest(BaseModel):
    hour_start: int | None = None
    hour_end: int | None = None
    activity: str | None = None
    action_type: Literal["internal", "web_search", "npc_interaction"] | None = None
    action_payload: dict | None = None
    status: Literal["pending", "executing", "done", "skipped"] | None = None
    outcome: str | None = None
    source_kind: Literal["generated", "routine", "carried_over", "thread", "spontaneous", "replan"] | None = None
    source_ref_id: int | None = None


class BulkPlanItemRequest(BaseModel):
    hour_start: int
    hour_end: int
    activity: str
    action_type: Literal["internal", "web_search", "npc_interaction"] = "internal"
    action_payload: dict = Field(default_factory=dict)
    status: Literal["pending", "executing", "done", "skipped"] = "pending"
    outcome: str = ""
    source_kind: Literal["generated", "routine", "carried_over", "thread", "spontaneous", "replan"] = "generated"
    source_ref_id: int | None = None
    executed_at: str | None = None


class BulkUpdatePlanRequest(BaseModel):
    items: list[BulkPlanItemRequest] = Field(default_factory=list)


class CreateNPCRequest(BaseModel):
    name: str
    role: str = ""
    background: str = ""
    relationship_to_character: str = ""
    personality_traits: list[str] = Field(default_factory=list)
    status: Literal["active", "inactive", "departed"] = "active"
    spawn_source: Literal["manual", "auto_generated"] = "manual"
    spawn_context: str = ""
    notes: str = ""


class UpdateNPCRequest(BaseModel):
    name: str | None = None
    role: str | None = None
    background: str | None = None
    relationship_to_character: str | None = None
    personality_traits: list[str] | None = None
    status: Literal["active", "inactive", "departed"] | None = None
    spawn_source: Literal["manual", "auto_generated"] | None = None
    spawn_context: str | None = None
    notes: str | None = None


class SummarizeConversationRequest(BaseModel):
    conversation_text: str


class PeriodicReviewRequest(BaseModel):
    start_date: str
    end_date: str
    include_archived: bool = False


class KeyRecordSearchRequest(BaseModel):
    query: str
    type: Literal[
        "medication_protocol",
        "health_monitoring",
        "dietary_intervention",
        "anniversary_date",
        "medical_review_date",
        "lifecycle_milestone",
        "key_collaboration",
        "commitment_agreement",
        "emotional_anchor",
        "life_pattern",
        "medical_tracking",
        "appointment",
        "project",
        "academic",
    ] | None = None
    top_k: int = 5
    include_archived: bool = False
    life_scope: Literal["user_life", "character_life", "shared_life"] | None = None
    include_world_books: bool = Field(
        default=False,
        description="兼容旧前端字段；key_records 搜索不再混入 world_books，请改用 /world-books/search。",
    )


class UpsertKeyRecordToolRequest(BaseModel):
    type: Literal[
        "medication_protocol",
        "health_monitoring",
        "dietary_intervention",
        "anniversary_date",
        "medical_review_date",
        "lifecycle_milestone",
        "key_collaboration",
        "commitment_agreement",
        "emotional_anchor",
        "life_pattern",
        "medical_tracking",
        "appointment",
        "project",
        "academic",
    ] | None = None
    title: str
    content_text: str
    content_json: dict | None = None
    tags: list[str] = Field(default_factory=list)
    match_keywords: list[str] = Field(default_factory=list)
    start_date: str | None = None
    end_date: str | None = None
    status: Literal["active", "archived"] = "active"
    source: Literal["manual", "conversation", "generated"] = "conversation"
    life_scope: Literal["user_life", "character_life", "shared_life"] = "user_life"
    linked_event_id: int | None = None
    update_if_exists: bool = True


class UpdateSettingRequest(BaseModel):
    value: str


class EvolutionApplyRequest(BaseModel):
    preview: dict


class RecalculateArchiveRequest(BaseModel):
    start_id: int | None = None
    end_id: int | None = None
    start_date: str | None = None
    end_date: str | None = None


class EvolutionRescoreRequest(BaseModel):
    start_id: int | None = None
    end_id: int | None = None
    start_date: str | None = None
    end_date: str | None = None
    scored_only: bool = True


class UpdateVectorSettingsRequest(BaseModel):
    vector_embedding_api_base: str | None = None
    vector_embedding_api_key: str | None = None
    vector_embedding_model: str | None = None
    vector_embedding_dim: int | None = None
    vector_embedding_timeout_sec: float | None = None
    vector_sync_batch_size: int | None = None
    vector_snapshot_days_threshold: int | None = None
    vector_search_top_k: int | None = None


class VectorSyncRequest(BaseModel):
    reindex: bool = False


class VectorCompactRequest(BaseModel):
    dry_run: bool = False


class VectorBatchDeleteRequest(BaseModel):
    entry_ids: list[str] = Field(default_factory=list)
    source_type: str | None = None
    status: str | None = "active"
    tier: str | None = None
    limit: int = Field(default=500, ge=1, le=5000)


class PlanBatchDeleteRequest(BaseModel):
    plan_ids: list[int] = Field(default_factory=list)


class UpdateRuntimeLLMRequest(BaseModel):
    llm_api_base: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_timeout_sec: float | None = None


class UpsertModelPricingRequest(BaseModel):
    model: str
    prompt_price: float = Field(ge=0)
    completion_price: float = Field(ge=0)


class BulkImportRequest(BaseModel):
    settings: dict[str, str] = Field(default_factory=dict)
    snapshots: list[dict] = Field(default_factory=list)
    events: list[dict] = Field(default_factory=list)
    key_records: list[dict] = Field(default_factory=list)
    overwrite_settings: bool = True
    upsert_key_records: bool = True
    sync_vectors_after_import: bool = True


class SnapshotTimezoneRepairRequest(BaseModel):
    dry_run: bool = False
