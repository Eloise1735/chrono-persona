from __future__ import annotations

import os
from urllib.parse import urlparse

from starlette.requests import Request


MASKED_SECRET = "********"
ADMIN_TOKEN_QUERY_PARAM = "kelsey_token"
SECRET_ENV_VARS = {
    "llm_api_key": "KELSEY_LLM_API_KEY",
    "env_llm_api_key": "KELSEY_ENV_LLM_API_KEY",
    "snapshot_llm_api_key": "KELSEY_SNAPSHOT_LLM_API_KEY",
    "vector_embedding_api_key": "KELSEY_VECTOR_EMBEDDING_API_KEY",
    "plan_web_search_api_key": "KELSEY_PLAN_WEB_SEARCH_API_KEY",
}


def is_sensitive_setting_key(key: str) -> bool:
    normalized = str(key or "").strip().lower()
    return (
        normalized.endswith("_api_key")
        or normalized.endswith("_secret")
        or normalized.endswith("_token")
        or "api_key" in normalized
        or "secret" in normalized
        or "token" in normalized
    )


def mask_secret(value: object) -> str:
    return MASKED_SECRET if str(value or "").strip() else ""


def secret_env_var_for_setting(key: str) -> str:
    return SECRET_ENV_VARS.get(str(key or "").strip(), "")


def get_secret_from_env(key: str, default: str = "") -> str:
    env_name = secret_env_var_for_setting(key)
    if not env_name:
        return default
    value = str(os.environ.get(env_name) or "").strip()
    return value if value else default


def is_blank_or_masked_secret(value: object) -> bool:
    raw = str(value or "").strip()
    return not raw or raw == MASKED_SECRET


def validate_api_base(value: object, field_name: str = "api_base") -> str:
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field_name} must be an absolute http(s) URL")
    return raw


def get_admin_token() -> str:
    return str(os.environ.get("KELSEY_ADMIN_TOKEN") or "").strip()


def request_has_admin_token(request: Request, expected_token: str) -> bool:
    if not expected_token:
        return True
    query_token = str(request.query_params.get(ADMIN_TOKEN_QUERY_PARAM) or "").strip()
    if query_token == expected_token:
        return True
    header_token = str(request.headers.get("x-kelsey-admin-token") or "").strip()
    if header_token == expected_token:
        return True
    auth = str(request.headers.get("authorization") or "").strip()
    prefix = "Bearer "
    return auth.startswith(prefix) and auth[len(prefix) :].strip() == expected_token
