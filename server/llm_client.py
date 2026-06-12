from __future__ import annotations

import asyncio
import hashlib
import json as _json
import logging
import time
from collections import OrderedDict, deque
from contextvars import ContextVar

import httpx

from server.config import LLMConfig
from server.database import Database
from server.security import get_secret_from_env, validate_api_base

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


# ── A3: prompt-hash dedup ────────────────────────────────────────────


class DuplicatePromptError(RuntimeError):
    """Raised when an identical prompt was sent within the dedup window.

    The original 21-hour incident burned the same prompt every ~90 seconds.
    A3 catches that at the LLMClient layer: every chat() entry hashes
    (model, messages, temperature) and refuses to send if the same hash
    was sent inside `llm_dedup_window_sec` (default 60).

    Inherits from RuntimeError (so legacy `except Exception` blocks catch
    it and use their fallback paths). C1's circuit breaker treats it as
    non_failure so a dup storm doesn't pause the loop — the dedup window
    naturally expires.
    """

    def __init__(self, prompt_hash: str, last_sent_at: float, window_sec: float):
        self.prompt_hash = prompt_hash
        self.last_sent_at = last_sent_at
        self.window_sec = float(window_sec)
        super().__init__(
            f"identical prompt sent {time.time() - last_sent_at:.1f}s ago "
            f"(window {window_sec:.0f}s); refusing duplicate call. hash={prompt_hash[:12]}"
        )


# ── A2: hourly + daily token budget cap ──────────────────────────────


class BudgetExceeded(BaseException):
    """Raised when the projected token spend would exceed the hourly or
    daily budget.

    **Inherits from BaseException, not RuntimeError**, so `except Exception`
    blocks scattered around the codebase do NOT swallow it. Budget
    exhaustion is like asyncio.CancelledError: it must propagate to the
    scheduler loop where C1 catches it via pause_immediately_exception_types
    and stops the loop entirely. That's the last-line backstop — if any
    future bug introduces a new silent loop, BudgetExceeded short-circuits
    it before the proxy balance is drained.
    """

    def __init__(
        self,
        *,
        window: str,
        used_tokens: int,
        limit_tokens: int,
        est_tokens: int,
    ):
        self.window = window
        self.used_tokens = int(used_tokens)
        self.limit_tokens = int(limit_tokens)
        self.est_tokens = int(est_tokens)
        super().__init__(
            f"LLM {window} budget would be exceeded "
            f"(used={used_tokens}, est_new={est_tokens}, limit={limit_tokens}); "
            f"refusing call until window resets."
        )


def _hash_messages(model: str, messages: list[dict], temperature: float) -> str:
    """Stable sha256 across the prompt payload. NUL-separated parts so
    `"a"+"b"` and `"ab"+""` don't collide."""
    h = hashlib.sha256()
    h.update((model or "").encode("utf-8", errors="replace"))
    h.update(b"\x00")
    h.update(f"t={float(temperature):.4f}".encode("utf-8"))
    h.update(b"\x00")
    # Sort keys so dict ordering differences don't fragment the hash.
    h.update(_json.dumps(messages, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return h.hexdigest()


def _estimate_prompt_tokens(messages: list[dict]) -> int:
    """Rough pre-flight estimate. Tokens ≈ chars / 3 for mixed CJK/English,
    plus a 500-token safety margin so we don't ship a request that would
    push us over the limit only to refund the difference afterwards."""
    total_chars = 0
    for m in messages:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, str):
            total_chars += len(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict):
                    total_chars += len(str(part.get("text") or ""))
                elif isinstance(part, str):
                    total_chars += len(part)
    return max(1, total_chars // 3) + 500


class _PromptDedupTracker:
    """Single process-wide instance shared by every LLMClient (proxy
    balance is per-account, so dedup must be global). Settings are loaded
    lazily from the first DB-bound client; until then, defaults apply."""

    _DEFAULT_WINDOW_SEC = 60.0
    _MAX_ENTRIES = 256

    def __init__(self) -> None:
        self._entries: OrderedDict[str, float] = OrderedDict()
        self._enabled = True
        self._window_sec = self._DEFAULT_WINDOW_SEC
        self._rejected_count = 0
        self._last_rejected_hash: str | None = None
        self._last_rejected_at: float | None = None

    def configure(self, *, enabled: bool, window_sec: float) -> None:
        self._enabled = bool(enabled)
        self._window_sec = max(1.0, float(window_sec))

    def check_or_raise(self, prompt_hash: str, *, now: float | None = None) -> None:
        if not self._enabled:
            return
        ts = now if now is not None else time.time()
        # Lazy eviction of expired entries (cheap: at most _MAX_ENTRIES).
        cutoff = ts - self._window_sec
        while self._entries:
            oldest_hash, oldest_ts = next(iter(self._entries.items()))
            if oldest_ts < cutoff:
                self._entries.popitem(last=False)
            else:
                break
        last_sent = self._entries.get(prompt_hash)
        if last_sent is not None and (ts - last_sent) < self._window_sec:
            self._rejected_count += 1
            self._last_rejected_hash = prompt_hash
            self._last_rejected_at = ts
            raise DuplicatePromptError(prompt_hash, last_sent, self._window_sec)

    def record(self, prompt_hash: str, *, now: float | None = None) -> None:
        if not self._enabled:
            return
        ts = now if now is not None else time.time()
        # Move-to-end so LRU eviction makes sense if we hit _MAX_ENTRIES.
        if prompt_hash in self._entries:
            self._entries.move_to_end(prompt_hash)
        self._entries[prompt_hash] = ts
        while len(self._entries) > self._MAX_ENTRIES:
            self._entries.popitem(last=False)

    def snapshot(self) -> dict:
        return {
            "enabled": self._enabled,
            "window_sec": self._window_sec,
            "tracked_prompts": len(self._entries),
            "rejected_count": self._rejected_count,
            "last_rejected_hash_short": (self._last_rejected_hash or "")[:12] or None,
            "last_rejected_at": self._last_rejected_at,
        }

    def reset(self) -> None:
        """Admin reset — clears history and rejection counters."""
        self._entries.clear()
        self._rejected_count = 0
        self._last_rejected_hash = None
        self._last_rejected_at = None


class _BudgetTracker:
    """Sliding hourly + daily token windows. Process-wide singleton.

    Pre-flight check uses estimated prompt tokens (`_estimate_prompt_tokens`)
    plus a `max_tokens` ceiling for completion (or a 2000-token default if
    the caller passed None). After the upstream returns real usage, the
    actual numbers replace the estimate via `record_actual`.
    """

    _HOUR_SEC = 3600
    _DAY_SEC = 86400
    _DEFAULT_HOURLY = 30000
    _DEFAULT_DAILY = 200000

    def __init__(self) -> None:
        self._enabled = True
        self._hourly_limit = self._DEFAULT_HOURLY
        self._daily_limit = self._DEFAULT_DAILY
        # Each deque entry is (timestamp, tokens).
        self._hourly: deque[tuple[float, int]] = deque()
        self._daily: deque[tuple[float, int]] = deque()
        self._hourly_used = 0
        self._daily_used = 0
        self._rejected_count = 0
        self._last_rejected_at: float | None = None
        self._last_rejected_reason: str | None = None

    def configure(
        self,
        *,
        enabled: bool,
        hourly_limit: int,
        daily_limit: int,
    ) -> None:
        self._enabled = bool(enabled)
        self._hourly_limit = max(1, int(hourly_limit))
        self._daily_limit = max(1, int(daily_limit))

    def _evict_expired(self, now: float) -> None:
        cutoff_hour = now - self._HOUR_SEC
        while self._hourly and self._hourly[0][0] < cutoff_hour:
            _, n = self._hourly.popleft()
            self._hourly_used -= n
        cutoff_day = now - self._DAY_SEC
        while self._daily and self._daily[0][0] < cutoff_day:
            _, n = self._daily.popleft()
            self._daily_used -= n

    def check_or_raise(self, est_tokens: int, *, now: float | None = None) -> None:
        if not self._enabled:
            return
        ts = now if now is not None else time.time()
        self._evict_expired(ts)
        if self._hourly_used + est_tokens > self._hourly_limit:
            self._record_rejection(ts, "hourly")
            raise BudgetExceeded(
                window="hourly",
                used_tokens=self._hourly_used,
                limit_tokens=self._hourly_limit,
                est_tokens=est_tokens,
            )
        if self._daily_used + est_tokens > self._daily_limit:
            self._record_rejection(ts, "daily")
            raise BudgetExceeded(
                window="daily",
                used_tokens=self._daily_used,
                limit_tokens=self._daily_limit,
                est_tokens=est_tokens,
            )

    def _record_rejection(self, ts: float, window: str) -> None:
        self._rejected_count += 1
        self._last_rejected_at = ts
        self._last_rejected_reason = window

    def record_actual(self, tokens: int, *, now: float | None = None) -> None:
        if not self._enabled or tokens <= 0:
            return
        ts = now if now is not None else time.time()
        self._evict_expired(ts)
        self._hourly.append((ts, int(tokens)))
        self._daily.append((ts, int(tokens)))
        self._hourly_used += int(tokens)
        self._daily_used += int(tokens)

    def snapshot(self) -> dict:
        # Don't mutate state during a read; report what's currently in the
        # windows (may include some technically-expired entries but the
        # caller doesn't care about <1s precision).
        return {
            "enabled": self._enabled,
            "hourly_limit": self._hourly_limit,
            "hourly_used": self._hourly_used,
            "hourly_remaining": max(0, self._hourly_limit - self._hourly_used),
            "daily_limit": self._daily_limit,
            "daily_used": self._daily_used,
            "daily_remaining": max(0, self._daily_limit - self._daily_used),
            "rejected_count": self._rejected_count,
            "last_rejected_at": self._last_rejected_at,
            "last_rejected_reason": self._last_rejected_reason,
        }

    def reset(self) -> None:
        """Admin reset — wipes counters. Use after raising hourly/daily
        limits or after balance top-up."""
        self._hourly.clear()
        self._daily.clear()
        self._hourly_used = 0
        self._daily_used = 0
        self._rejected_count = 0
        self._last_rejected_at = None
        self._last_rejected_reason = None


# Module-level singletons. Shared by every LLMClient instance because the
# proxy account balance and the duplicate-prompt risk are global, not
# per-client.
_prompt_dedup_tracker = _PromptDedupTracker()
_budget_tracker = _BudgetTracker()


def get_prompt_dedup_tracker() -> _PromptDedupTracker:
    return _prompt_dedup_tracker


def get_budget_tracker() -> _BudgetTracker:
    return _budget_tracker


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
        runtime_override: dict[str, str] | None = None,
    ) -> str:
        runtime = runtime_override or await self.get_runtime_config()
        api_base = runtime["api_base"]
        api_key = runtime["api_key"]
        model = runtime["model"]
        if not str(api_key or "").strip():
            raise RuntimeError(
                "LLM API key is not configured. Set KELSEY_LLM_API_KEY or the dedicated task key in the server environment."
            )
        timeout_sec = max(
            1.0,
            float(
                timeout_sec_override
                if timeout_sec_override is not None
                else (runtime.get("timeout_sec") or self.DEFAULT_TIMEOUT_SEC)
            ),
        )

        # ── A3 + A2 last-line defenses (see docs/fix_plan_snapshot_loop.md) ──
        # Refresh tracker config from runtime settings (cheap: cached at the
        # tracker, only the DB lookup costs anything). Then:
        #   1. Check prompt-hash dedup: same prompt within window → refuse.
        #   2. Estimate prompt + max_tokens for budget pre-flight.
        #   3. Check hourly/daily budget. Exhausted → raise BudgetExceeded
        #      (BaseException → propagates through `except Exception`).
        # On a successful upstream response we record the hash AND the real
        # token usage (see _consume_usage). Failed calls record neither —
        # caller can adjust the prompt and retry immediately.
        await self._refresh_safety_trackers_from_settings()
        prompt_hash = _hash_messages(model, messages, temperature)
        _prompt_dedup_tracker.check_or_raise(prompt_hash)
        est_tokens = _estimate_prompt_tokens(messages) + int(
            max_tokens if max_tokens is not None else 2000
        )
        _budget_tracker.check_or_raise(est_tokens)

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
        usage_dict = data.get("usage") or {}
        self._consume_usage(usage_dict, model=model)
        # A3: only record the hash after a successful response. If the
        # upstream rejected the call (e.g. 4xx malformed prompt) the
        # caller wants to fix and retry immediately, not wait out a 60s
        # cooldown. A2: same logic — only count real token spend.
        _prompt_dedup_tracker.record(prompt_hash)
        actual_total = int(
            usage_dict.get("total_tokens")
            or (int(usage_dict.get("prompt_tokens") or 0) + int(usage_dict.get("completion_tokens") or 0))
            or 0
        )
        if actual_total > 0:
            _budget_tracker.record_actual(actual_total)
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

    # ── A2/A3 lazy settings refresh ──────────────────────────────────
    #
    # The trackers are process-wide singletons but their thresholds come
    # from runtime settings (so they can be tuned without a redeploy).
    # We refresh at most once per 30 seconds — well under any meaningful
    # tick interval, and cheap enough that bursts of LLM calls don't
    # hammer the DB.
    _SAFETY_REFRESH_INTERVAL_SEC = 30.0
    _safety_last_refresh_at: float = 0.0

    async def _refresh_safety_trackers_from_settings(self) -> None:
        if self._db is None:
            return
        now = time.time()
        # Class-level last-refresh timestamp — shared by every LLMClient.
        if now - LLMClient._safety_last_refresh_at < self._SAFETY_REFRESH_INTERVAL_SEC:
            return
        LLMClient._safety_last_refresh_at = now
        try:
            dedup_enabled = (await self._get_setting("llm_dedup_enabled", "1")).strip() == "1"
            dedup_window = float(await self._get_setting("llm_dedup_window_sec", "60") or 60.0)
            _prompt_dedup_tracker.configure(
                enabled=dedup_enabled, window_sec=dedup_window
            )
        except Exception:
            logger.exception("Failed to refresh prompt-dedup settings; keeping current values.")
        try:
            budget_enabled = (await self._get_setting("llm_budget_enabled", "1")).strip() == "1"
            hourly = int(await self._get_setting("llm_hourly_token_limit", "30000") or 30000)
            daily = int(await self._get_setting("llm_daily_token_limit", "200000") or 200000)
            _budget_tracker.configure(
                enabled=budget_enabled,
                hourly_limit=hourly,
                daily_limit=daily,
            )
        except Exception:
            logger.exception("Failed to refresh budget settings; keeping current values.")

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
        env_key = get_secret_from_env("llm_api_key", "")
        if self._db is None:
            return {
                "api_base": self.api_base,
                "api_key": env_key,
                "model": self.model,
                "timeout_sec": str(self.timeout_sec),
            }
        base = await self._get_setting("llm_api_base", self.api_base)
        model = await self._get_setting("llm_model", self.model)
        timeout_sec = await self._get_setting("llm_timeout_sec", str(self.DEFAULT_TIMEOUT_SEC))
        return {
            "api_base": base.rstrip("/"),
            "api_key": env_key,
            "model": model,
            "timeout_sec": timeout_sec,
        }

    async def update_runtime_config(self, payload: dict):
        if self._db is None:
            if payload.get("llm_api_base"):
                self.api_base = validate_api_base(payload["llm_api_base"], "llm_api_base")
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
            value = payload.get(key)
            if key == "llm_api_base":
                value = validate_api_base(value, "llm_api_base")
            if key == "llm_api_key":
                continue
            await self._db.set_setting(
                key=key,
                value=str(value or ""),
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
        key = get_secret_from_env("env_llm_api_key", get_secret_from_env("llm_api_key", ""))
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
            elif key == "env_llm_api_base":
                value = validate_api_base(value, "env_llm_api_base")
            elif key == "env_llm_api_key":
                continue
            await self._db.set_setting(
                key=key,
                value=str(value or ""),
                category="runtime",
                description=desc,
            )


class SnapshotLLMClient(LLMClient):
    async def get_dedicated_runtime_config(self) -> dict[str, str]:
        env_key = get_secret_from_env("snapshot_llm_api_key", "")
        if self._db is None:
            return {
                "api_base": self.api_base.rstrip("/"),
                "api_key": env_key or get_secret_from_env("llm_api_key", ""),
                "model": self.model,
                "timeout_sec": str(self.timeout_sec),
            }
        if await self._get_setting("snapshot_llm_enabled", "0") != "1":
            raise RuntimeError("snapshot_llm_enabled is not 1")
        base = await self._get_setting("snapshot_llm_api_base", "")
        model = await self._get_setting("snapshot_llm_model", "")
        missing = [
            name for name, value in (
                ("snapshot_llm_api_base", base),
                ("KELSEY_SNAPSHOT_LLM_API_KEY", env_key),
                ("snapshot_llm_model", model),
            )
            if not str(value or "").strip()
        ]
        if missing:
            raise RuntimeError(f"Missing snapshot LLM settings: {', '.join(missing)}")
        timeout_sec = await self._get_setting("llm_timeout_sec", str(self.DEFAULT_TIMEOUT_SEC))
        return {
            "api_base": base.rstrip("/"),
            "api_key": env_key,
            "model": model,
            "timeout_sec": timeout_sec,
        }

    async def chat_dedicated(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = 2048,
        timeout_sec_override: float | None = None,
    ) -> str:
        runtime = await self.get_dedicated_runtime_config()
        return await super().chat(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_sec_override=timeout_sec_override,
            runtime_override=runtime,
        )

    async def get_runtime_config(self) -> dict[str, str]:
        if await self._get_setting("snapshot_llm_enabled", "0") != "1":
            return await super().get_runtime_config()
        base = await self._get_setting("snapshot_llm_api_base", self.api_base)
        key = get_secret_from_env("snapshot_llm_api_key", get_secret_from_env("llm_api_key", ""))
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
            elif key == "snapshot_llm_api_base":
                value = validate_api_base(value, "snapshot_llm_api_base")
            elif key == "snapshot_llm_api_key":
                continue
            await self._db.set_setting(
                key=key,
                value=str(value or ""),
                category="runtime",
                description=desc,
            )
