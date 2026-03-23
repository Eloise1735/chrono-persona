from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class StateSnapshot(BaseModel):
    id: int | None = None
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    type: Literal["daily", "conversation_end", "accumulated"] = "daily"
    content: str = ""
    environment: str = "{}"
    referenced_events: str = "[]"
    embedding_vector_id: str | None = None


class EventAnchor(BaseModel):
    id: int | None = None
    date: str = Field(default_factory=lambda: datetime.utcnow().strftime("%Y-%m-%d"))
    title: str = ""
    description: str = ""
    source: Literal["generated", "manual", "conversation"] = "generated"
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    embedding_vector_id: str | None = None
    trigger_keywords: str = "[]"
    categories: str = "[]"
    archived: int = 0
    importance_score: float | None = None
    impression_depth: float | None = None


class KeyRecord(BaseModel):
    id: int | None = None
    type: Literal["important_date", "important_item", "key_collaboration", "medical_advice"] = "important_item"
    title: str = ""
    content_text: str = ""
    content_json: str | None = None
    tags: str = "[]"
    start_date: str | None = None
    end_date: str | None = None
    status: Literal["active", "archived"] = "active"
    source: Literal["manual", "conversation", "generated"] = "manual"
    linked_event_id: int | None = None
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


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


class UpdateEventRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    trigger_keywords: list[str] | None = None
    categories: list[str] | None = None
    archived: int | None = None
    importance_score: float | None = None
    impression_depth: float | None = None


class CreateKeyRecordRequest(BaseModel):
    type: Literal["important_date", "important_item", "key_collaboration", "medical_advice"]
    title: str
    content_text: str
    content_json: dict | None = None
    tags: list[str] = Field(default_factory=list)
    start_date: str | None = None
    end_date: str | None = None
    status: Literal["active", "archived"] = "active"
    source: Literal["manual", "conversation", "generated"] = "manual"
    linked_event_id: int | None = None


class UpdateKeyRecordRequest(BaseModel):
    type: Literal["important_date", "important_item", "key_collaboration", "medical_advice"] | None = None
    title: str | None = None
    content_text: str | None = None
    content_json: dict | None = None
    tags: list[str] | None = None
    start_date: str | None = None
    end_date: str | None = None
    status: Literal["active", "archived"] | None = None
    source: Literal["manual", "conversation", "generated"] | None = None
    linked_event_id: int | None = None


class MemorySearchRequest(BaseModel):
    query: str
    top_k: int = 5


class GetCurrentStateRequest(BaseModel):
    current_time: str
    last_interaction_time: str


class ReflectRequest(BaseModel):
    conversation_summary: str


class SummarizeConversationRequest(BaseModel):
    conversation_text: str


class PeriodicReviewRequest(BaseModel):
    start_date: str
    end_date: str
    include_archived: bool = False


class KeyRecordSearchRequest(BaseModel):
    query: str
    type: Literal["important_date", "important_item", "key_collaboration", "medical_advice"] | None = None
    top_k: int = 5
    include_archived: bool = False


class UpsertKeyRecordToolRequest(BaseModel):
    type: Literal["important_date", "important_item", "key_collaboration", "medical_advice"]
    title: str
    content_text: str
    content_json: dict | None = None
    tags: list[str] = Field(default_factory=list)
    start_date: str | None = None
    end_date: str | None = None
    status: Literal["active", "archived"] = "active"
    source: Literal["manual", "conversation", "generated"] = "conversation"
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


class UpdateRuntimeLLMRequest(BaseModel):
    llm_api_base: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None


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
