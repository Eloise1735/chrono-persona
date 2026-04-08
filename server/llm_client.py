from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar

import httpx

from server.config import LLMConfig
from server.database import Database

_TOKEN_USAGE_CTX: ContextVar[dict | None] = ContextVar("_token_usage_ctx", default=None)
logger = logging.getLogger(__name__)


class LLMTimeoutError(RuntimeError):
    """Raised when the upstream LLM request exceeds the configured timeout."""


class LLMUpstreamHTTPError(RuntimeError):
    """Raised when the upstream LLM returns a non-success HTTP status."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = int(status_code)


class LLMTransportError(RuntimeError):
    """Raised when the upstream LLM connection fails before a valid HTTP response."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = int(status_code)


def _extract_chat_message_content(message: dict) -> str:
    """Normalize OpenAI-style message.content (str or list of parts) to a single string."""
    raw = message.get("content")
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        chunks: list[str] = []
        for part in raw:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    chunks.append(str(part.get("text") or ""))
                elif "text" in part:
                    chunks.append(str(part.get("text") or ""))
            elif isinstance(part, str):
                chunks.append(part)
        return "".join(chunks)
    return str(raw)


class LLMClient:
    """OpenAI-compatible chat completion client with configurable endpoint."""

    DEFAULT_TIMEOUT_SEC = 180.0
    TRANSIENT_STATUS_RETRYABLE = {429, 502, 503, 504}
    TRANSIENT_STATUS_MAX_RETRIES = 2

    def __init__(self, config: LLMConfig, db: Database | None = None):
        self.api_base = config.api_base.rstrip("/")
        self.api_key = config.api_key
        self.model = config.model
        self.timeout_sec = self.DEFAULT_TIMEOUT_SEC
        self._db = db
        self._client = httpx.AsyncClient(timeout=self.DEFAULT_TIMEOUT_SEC)

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = 2048,
        timeout_sec_override: float | None = None,
    ) -> str:
        runtime = await self.get_runtime_config()
        api_base = runtime["api_base"]
        api_key = runtime["api_key"]
        model = runtime["model"]
        timeout_sec = max(
            1.0,
            float(
                timeout_sec_override
                if timeout_sec_override is not None
                else (runtime.get("timeout_sec") or self.DEFAULT_TIMEOUT_SEC)
            ),
        )
        url = f"{api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        resp = None
        for attempt in range(self.TRANSIENT_STATUS_MAX_RETRIES + 1):
            try:
                resp = await self._client.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=timeout_sec,
                )
            except httpx.TimeoutException as exc:
                raise LLMTimeoutError(
                    f"LLM 请求超时：模型 {model} 在 {timeout_sec:.0f} 秒内未返回结果。"
                ) from exc
            except httpx.HTTPError as exc:
                if attempt < self.TRANSIENT_STATUS_MAX_RETRIES:
                    delay_sec = 1.0 * (attempt + 1)
                    logger.warning(
                        "Transient upstream LLM transport error; retrying. model=%s attempt=%s/%s delay=%.1fs error=%r",
                        model,
                        attempt + 1,
                        self.TRANSIENT_STATUS_MAX_RETRIES + 1,
                        delay_sec,
                        str(exc),
                    )
                    await asyncio.sleep(delay_sec)
                    continue
                raise LLMTransportError(
                    f"LLM 上游连接失败：模型 {model}，错误：{exc}"
                ) from exc
            if resp.is_success:
                break
            status_code = int(resp.status_code or 0)
            body_preview = resp.text[:500]
            if (
                status_code in self.TRANSIENT_STATUS_RETRYABLE
                and attempt < self.TRANSIENT_STATUS_MAX_RETRIES
            ):
                delay_sec = 1.0 * (attempt + 1)
                logger.warning(
                    "Transient upstream LLM HTTP error; retrying. model=%s status=%s attempt=%s/%s delay=%.1fs body=%r",
                    model,
                    status_code,
                    attempt + 1,
                    self.TRANSIENT_STATUS_MAX_RETRIES + 1,
                    delay_sec,
                    body_preview[:200],
                )
                await asyncio.sleep(delay_sec)
                continue
            raise LLMUpstreamHTTPError(
                status_code,
                f"LLM 上游服务返回 HTTP {status_code}：{body_preview}",
            )
        assert resp is not None
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
        choice0 = data["choices"][0]
        msg = choice0.get("message") or {}
        content = _extract_chat_message_content(msg)
        finish = str(choice0.get("finish_reason") or "").strip()
        if finish == "length":
            usage = data.get("usage") or {}
            logger.warning(
                "LLM hit completion length limit (finish_reason=length); output may be truncated. "
                "model=%s usage=%s",
                model,
                usage,
            )
        return content

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
                "timeout_sec": str(self.timeout_sec),
            }
        base = await self._get_setting("llm_api_base", self.api_base)
        key = await self._get_setting("llm_api_key", self.api_key)
        model = await self._get_setting("llm_model", self.model)
        timeout_sec = await self._get_setting("llm_timeout_sec", str(self.DEFAULT_TIMEOUT_SEC))
        return {
            "api_base": base.rstrip("/"),
            "api_key": key,
            "model": model,
            "timeout_sec": timeout_sec,
        }

    async def update_runtime_config(self, payload: dict):
        if self._db is None:
            if payload.get("llm_api_base"):
                self.api_base = str(payload["llm_api_base"]).rstrip("/")
            if payload.get("llm_api_key"):
                self.api_key = str(payload["llm_api_key"])
            if payload.get("llm_model"):
                self.model = str(payload["llm_model"])
            if payload.get("llm_timeout_sec") is not None:
                self.timeout_sec = max(1.0, float(payload["llm_timeout_sec"]))
            return
        mapping = {
            "llm_api_base": "运行时 LLM API Base（覆盖 config.yaml）",
            "llm_api_key": "运行时 LLM API Key（覆盖 config.yaml）",
            "llm_model": "运行时 LLM 模型名（覆盖 config.yaml）",
            "llm_timeout_sec": "运行时 LLM 请求超时秒数（覆盖 config.yaml）",
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


class EnvironmentLLMClient(LLMClient):
    async def get_runtime_config(self) -> dict[str, str]:
        if await self._get_setting("env_llm_enabled", "0") != "1":
            return await super().get_runtime_config()
        base = await self._get_setting("env_llm_api_base", self.api_base)
        key = await self._get_setting("env_llm_api_key", self.api_key)
        model = await self._get_setting("env_llm_model", self.model)
        timeout_sec = await self._get_setting("llm_timeout_sec", str(self.DEFAULT_TIMEOUT_SEC))
        return {
            "api_base": base.rstrip("/"),
            "api_key": key,
            "model": model,
            "timeout_sec": timeout_sec,
        }

    async def update_runtime_config(self, payload: dict):
        if self._db is None:
            return await super().update_runtime_config(payload)
        mapping = {
            "env_llm_enabled": "环境生成专用 LLM 开关（1=启用，0=禁用）",
            "env_llm_api_base": "环境生成专用 LLM API Base（未启用时回退主 LLM）",
            "env_llm_api_key": "环境生成专用 LLM API Key（未启用时回退主 LLM）",
            "env_llm_model": "环境生成专用 LLM 模型（未启用时回退主 LLM）",
        }
        for key, desc in mapping.items():
            if key not in payload:
                continue
            value = payload.get(key)
            if key == "env_llm_enabled":
                value = "1" if str(value).strip().lower() in {"1", "true", "yes", "on"} else "0"
            await self._db.set_setting(
                key=key,
                value=str(value or ""),
                category="runtime",
                description=desc,
            )


class SnapshotLLMClient(LLMClient):
    async def get_runtime_config(self) -> dict[str, str]:
        if await self._get_setting("snapshot_llm_enabled", "0") != "1":
            return await super().get_runtime_config()
        base = await self._get_setting("snapshot_llm_api_base", self.api_base)
        key = await self._get_setting("snapshot_llm_api_key", self.api_key)
        model = await self._get_setting("snapshot_llm_model", self.model)
        timeout_sec = await self._get_setting("llm_timeout_sec", str(self.DEFAULT_TIMEOUT_SEC))
        return {
            "api_base": base.rstrip("/"),
            "api_key": key,
            "model": model,
            "timeout_sec": timeout_sec,
        }

    async def update_runtime_config(self, payload: dict):
        if self._db is None:
            return await super().update_runtime_config(payload)
        mapping = {
            "snapshot_llm_enabled": "快照与评分专用 LLM 开关（1=启用，0=禁用）",
            "snapshot_llm_api_base": "快照与评分专用 LLM API Base（未启用时回退主 LLM）",
            "snapshot_llm_api_key": "快照与评分专用 LLM API Key（未启用时回退主 LLM）",
            "snapshot_llm_model": "快照与评分专用 LLM 模型（未启用时回退主 LLM）",
        }
        for key, desc in mapping.items():
            if key not in payload:
                continue
            value = payload.get(key)
            if key == "snapshot_llm_enabled":
                value = "1" if str(value).strip().lower() in {"1", "true", "yes", "on"} else "0"
            await self._db.set_setting(
                key=key,
                value=str(value or ""),
                category="runtime",
                description=desc,
            )
