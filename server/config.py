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
class CharacterConfig:
    system_prompt: str | None = None
    system_prompt_file: str | None = None


@dataclass
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    environment: EnvironmentConfig = field(default_factory=EnvironmentConfig)
    memory_store: MemoryStoreConfig = field(default_factory=MemoryStoreConfig)
    character: CharacterConfig = field(default_factory=CharacterConfig)


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


def load_config(config_path: str | None = None) -> AppConfig:
    if config_path is None:
        config_path = os.environ.get("KELSEY_CONFIG", "config.yaml")

    path = Path(config_path)
    if not path.exists():
        return AppConfig()

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
    if "character" in raw:
        cfg.character = _dict_to_dataclass(CharacterConfig, raw["character"])

    return cfg
