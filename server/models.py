from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field

from server.time_display import iso_string_for_cst_display, shanghai_now


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
    include_world_books: bool = Field(
        default=True,
        description="为 True 时合并检索启用中的世界书（关键词 + 已向量化时的语义向量）",
    )


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
