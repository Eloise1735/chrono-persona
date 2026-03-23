from __future__ import annotations

from contextvars import ContextVar

import httpx

from server.config import LLMConfig
from server.database import Database

_TOKEN_USAGE_CTX: ContextVar[dict | None] = ContextVar("_token_usage_ctx", default=None)


class LLMClient:
    """OpenAI-compatible chat completion client with configurable endpoint."""

    def __init__(self, config: LLMConfig, db: Database | None = None):
        self.api_base = config.api_base.rstrip("/")
        self.api_key = config.api_key
        self.model = config.model
        self._db = db
        self._client = httpx.AsyncClient(timeout=120.0)

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> str:
        runtime = await self.get_runtime_config()
        api_base = runtime["api_base"]
        api_key = runtime["api_key"]
        model = runtime["model"]
        url = f"{api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }

        resp = await self._client.post(url, json=payload, headers=headers)
        if not resp.is_success:
            raise RuntimeError(
                f"LLM API returned {resp.status_code}: {resp.text[:500]}"
            )
        try:
            data = resp.json()
        except Exception:
            raise RuntimeError(
                f"LLM API returned non-JSON response (status {resp.status_code}). "
                f"Body preview: {resp.text[:300]!r}"
            )
        if "choices" not in data or not data["choices"]:
            raise RuntimeError(f"Unexpected LLM API response format: {data}")
        self._consume_usage(data.get("usage") or {}, model=model)
        return data["choices"][0]["message"]["content"]

    async def close(self):
        await self._client.aclose()

    def begin_usage_tracking(self):
        _TOKEN_USAGE_CTX.set(
            {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "requests": 0,
                "models": {},
            }
        )

    def end_usage_tracking(self) -> dict:
        usage = _TOKEN_USAGE_CTX.get() or {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "requests": 0,
            "models": {},
        }
        _TOKEN_USAGE_CTX.set(None)
        return usage

    async def get_runtime_config(self) -> dict[str, str]:
        if self._db is None:
            return {
                "api_base": self.api_base,
                "api_key": self.api_key,
                "model": self.model,
            }
        base = await self._get_setting("llm_api_base", self.api_base)
        key = await self._get_setting("llm_api_key", self.api_key)
        model = await self._get_setting("llm_model", self.model)
        return {"api_base": base.rstrip("/"), "api_key": key, "model": model}

    async def update_runtime_config(self, payload: dict):
        if self._db is None:
            if payload.get("llm_api_base"):
                self.api_base = str(payload["llm_api_base"]).rstrip("/")
            if payload.get("llm_api_key"):
                self.api_key = str(payload["llm_api_key"])
            if payload.get("llm_model"):
                self.model = str(payload["llm_model"])
            return
        mapping = {
            "llm_api_base": "运行时 LLM API Base（覆盖 config.yaml）",
            "llm_api_key": "运行时 LLM API Key（覆盖 config.yaml）",
            "llm_model": "运行时 LLM 模型名（覆盖 config.yaml）",
        }
        for key, desc in mapping.items():
            if key not in payload:
                continue
            await self._db.set_setting(
                key=key,
                value=str(payload.get(key) or ""),
                category="runtime",
                description=desc,
            )

    async def _get_setting(self, key: str, default: str) -> str:
        if self._db is None:
            return default
        row = await self._db.get_setting(key)
        if not row:
            return default
        value = str(row.get("value", "")).strip()
        return value if value else default

    def _consume_usage(self, usage: dict, model: str | None = None):
        tracker = _TOKEN_USAGE_CTX.get()
        if tracker is None:
            return
        prompt_tokens = int(
            usage.get("prompt_tokens")
            or usage.get("input_tokens")
            or 0
        )
        completion_tokens = int(
            usage.get("completion_tokens")
            or usage.get("output_tokens")
            or 0
        )
        total_tokens = int(
            usage.get("total_tokens")
            or (prompt_tokens + completion_tokens)
            or 0
        )
        tracker["prompt_tokens"] = int(tracker.get("prompt_tokens", 0)) + prompt_tokens
        tracker["completion_tokens"] = int(tracker.get("completion_tokens", 0)) + completion_tokens
        tracker["total_tokens"] = int(tracker.get("total_tokens", 0)) + total_tokens
        tracker["requests"] = int(tracker.get("requests", 0)) + 1
        model_key = str(model or "unknown").strip() or "unknown"
        per_model = tracker.get("models")
        if not isinstance(per_model, dict):
            per_model = {}
        bucket = per_model.get(model_key)
        if not isinstance(bucket, dict):
            bucket = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "requests": 0,
            }
        bucket["prompt_tokens"] = int(bucket.get("prompt_tokens", 0)) + prompt_tokens
        bucket["completion_tokens"] = int(bucket.get("completion_tokens", 0)) + completion_tokens
        bucket["total_tokens"] = int(bucket.get("total_tokens", 0)) + total_tokens
        bucket["requests"] = int(bucket.get("requests", 0)) + 1
        per_model[model_key] = bucket
        tracker["models"] = per_model
        _TOKEN_USAGE_CTX.set(tracker)
