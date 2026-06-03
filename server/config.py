from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass, field

import yaml


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8000


@dataclass
class LLMConfig:
    api_base: str = "https://api.example.com/v1"
    api_key: str = ""
    model: str = "claude-3-sonnet-20240229"


@dataclass
class DatabaseConfig:
    path: str = "./data/kelsey.db"


@dataclass
class EnvironmentConfig:
    min_time_unit_hours: int = 24
    generator: str = "template"
    world_book_path: str | None = None
    prompt_template: str | None = None
    llm: LLMConfig | None = None


@dataclass
class MemoryStoreConfig:
    type: str = "keyword"
    max_snapshots: int = 7
    provider: str | None = None
    api_key: str | None = None
    index_name: str | None = None
    embedding_api_base: str | None = None
    embedding_model: str | None = None


@dataclass
class OBDecayEmotionWeightsConfig:
    base: float = 1.0
    arousal_boost: float = 0.8


@dataclass
class OBDecayConfig:
    lambda_: float = 0.05
    threshold: float = 0.3
    check_interval_hours: float = 24.0
    emotion_weights: OBDecayEmotionWeightsConfig = field(default_factory=OBDecayEmotionWeightsConfig)


@dataclass
class OBConfig:
    enabled: bool = True
    buckets_dir: str = "./data/ob_buckets"
    decay: OBDecayConfig = field(default_factory=OBDecayConfig)


@dataclass
class CharacterConfig:
    system_prompt: str | None = None
    system_prompt_file: str | None = None


@dataclass
class WeChatConfig:
    enabled: bool = True
    openclaw_state_dir: str | None = None
    sync_state_path: str = "./data/wechat_sync_state.json"
    session_state_path: str = "./data/wechat_sessions.json"
    max_history_turns: int = 8


@dataclass
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    environment: EnvironmentConfig = field(default_factory=EnvironmentConfig)
    memory_store: MemoryStoreConfig = field(default_factory=MemoryStoreConfig)
    ob: OBConfig = field(default_factory=OBConfig)
    character: CharacterConfig = field(default_factory=CharacterConfig)
    wechat: WeChatConfig = field(default_factory=WeChatConfig)


def _dict_to_dataclass(cls, data: dict):
    """Recursively convert a dict to a dataclass, ignoring unknown keys."""
    if data is None:
        return cls()
    filtered = {}
    for f in cls.__dataclass_fields__:
        if f in data and data[f] is not None:
            field_type = cls.__dataclass_fields__[f].type
            if isinstance(data[f], dict):
                inner_cls = cls.__dataclass_fields__[f].type
                if isinstance(inner_cls, str):
                    inner_cls = eval(inner_cls)
                if hasattr(inner_cls, "__dataclass_fields__"):
                    filtered[f] = _dict_to_dataclass(inner_cls, data[f])
                    continue
            filtered[f] = data[f]
    return cls(**filtered)


def _resolve_config_path(value: str | None, base_dir: Path) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return raw
    path = Path(raw)
    if path.is_absolute():
        return str(path)
    return str((base_dir / path).resolve())


def _normalize_config_paths(cfg: AppConfig, base_dir: Path) -> AppConfig:
    cfg.database.path = str(_resolve_config_path(cfg.database.path, base_dir) or cfg.database.path)
    cfg.ob.buckets_dir = str(_resolve_config_path(cfg.ob.buckets_dir, base_dir) or cfg.ob.buckets_dir)
    cfg.wechat.sync_state_path = str(
        _resolve_config_path(cfg.wechat.sync_state_path, base_dir) or cfg.wechat.sync_state_path
    )
    cfg.wechat.session_state_path = str(
        _resolve_config_path(cfg.wechat.session_state_path, base_dir) or cfg.wechat.session_state_path
    )
    cfg.environment.world_book_path = _resolve_config_path(cfg.environment.world_book_path, base_dir)
    cfg.environment.prompt_template = _resolve_config_path(cfg.environment.prompt_template, base_dir)
    cfg.character.system_prompt_file = _resolve_config_path(cfg.character.system_prompt_file, base_dir)
    return cfg


def load_config(config_path: str | None = None) -> AppConfig:
    if config_path is None:
        config_path = os.environ.get("KELSEY_CONFIG", "config.yaml")

    path = Path(config_path)
    if not path.exists():
        return _normalize_config_paths(AppConfig(), Path.cwd())
    base_dir = path.resolve().parent

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    cfg = AppConfig()
    if "server" in raw:
        cfg.server = _dict_to_dataclass(ServerConfig, raw["server"])
    if "llm" in raw:
        cfg.llm = _dict_to_dataclass(LLMConfig, raw["llm"])
    if "database" in raw:
        cfg.database = _dict_to_dataclass(DatabaseConfig, raw["database"])
    if "environment" in raw:
        env_data = raw["environment"]
        cfg.environment = _dict_to_dataclass(EnvironmentConfig, env_data)
        if "llm" in env_data and isinstance(env_data["llm"], dict):
            cfg.environment.llm = _dict_to_dataclass(LLMConfig, env_data["llm"])
    if "memory_store" in raw:
        cfg.memory_store = _dict_to_dataclass(MemoryStoreConfig, raw["memory_store"])
    if "ob" in raw:
        ob_data = dict(raw["ob"] or {})
        decay_data = dict(ob_data.get("decay") or {})
        if "lambda" in decay_data and "lambda_" not in decay_data:
            decay_data["lambda_"] = decay_data.pop("lambda")
        if decay_data:
            emotion_data = decay_data.get("emotion_weights")
            if isinstance(emotion_data, dict):
                decay_data["emotion_weights"] = _dict_to_dataclass(OBDecayEmotionWeightsConfig, emotion_data)
            ob_data["decay"] = _dict_to_dataclass(OBDecayConfig, decay_data)
        cfg.ob = _dict_to_dataclass(OBConfig, ob_data)
    if "character" in raw:
        cfg.character = _dict_to_dataclass(CharacterConfig, raw["character"])
    if "wechat" in raw:
        cfg.wechat = _dict_to_dataclass(WeChatConfig, raw["wechat"])

    return _normalize_config_paths(cfg, base_dir)
