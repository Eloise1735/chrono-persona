"""C1 scheduler circuit breaker.

Before C1, both _snapshot_scheduler_loop and _life_scheduler_loop in
server/main.py looked like:

    while True:
        try: await state_machine.run_snapshot_scheduler_tick()
        except Exception: logger.exception(...)
        await asyncio.sleep(interval_sec)

A persistently-failing tick would just keep ticking at the regular cadence,
swallowing the same exception each time. During the original 21-hour
token-burn incident this meant the same broken prompt was retried hundreds
of times. The B2 in-flight barrier now prevents the LLM re-spend at the
prompt-hash level, but C1 closes the broader hole: after N consecutive
failures (any kind) the loop pauses entirely until a human resumes it,
and between attempts an exponential backoff slows the churn.

Design notes:
- Single asyncio task per loop, so no locks needed.
- Designed to be queryable for /admin/health (C3) and resumable via an
  admin endpoint (C3) without restarting the server.
- A2/A3 land later. To avoid having to re-touch the loop when they add
  BudgetExceeded / DuplicatePromptError exception classes, the breaker
  accepts `non_failure_exception_types` (don't count toward
  consecutive_failures) and `pause_immediately_exception_types` (skip
  the counter, go straight to paused) at construction time.

See docs/fix_plan_snapshot_loop.md C1.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Sequence

from server.models import format_utc_instant_z

logger = logging.getLogger(__name__)


DEFAULT_BACKOFF_STEPS_SEC: tuple[int, ...] = (30, 120, 300, 900, 1800)  # 30s, 2m, 5m, 15m, 30m
DEFAULT_MAX_CONSECUTIVE_FAILURES: int = 10


def _now_z() -> str:
    return format_utc_instant_z(datetime.utcnow())


class SchedulerCircuitBreaker:
    """Per-loop state tracker. Not thread-safe (single asyncio task owns it).

    Lifecycle:
      record_tick_start() → tick runs → record_success() | record_failure(exc)

    When record_failure() pushes consecutive_failures over the max, the
    breaker flips to paused=True; subsequent is_paused() checks let the
    loop short-circuit until resume() is called.
    """

    def __init__(
        self,
        *,
        name: str,
        max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
        backoff_steps_sec: Sequence[int] = DEFAULT_BACKOFF_STEPS_SEC,
        non_failure_exception_types: tuple[type[BaseException], ...] = (),
        pause_immediately_exception_types: tuple[type[BaseException], ...] = (),
    ) -> None:
        if max_consecutive_failures < 1:
            raise ValueError("max_consecutive_failures must be >= 1")
        if not backoff_steps_sec:
            raise ValueError("backoff_steps_sec must not be empty")
        self.name = name
        self.max_consecutive_failures = int(max_consecutive_failures)
        self.backoff_steps_sec: tuple[int, ...] = tuple(int(x) for x in backoff_steps_sec)
        self.non_failure_exception_types = tuple(non_failure_exception_types)
        self.pause_immediately_exception_types = tuple(pause_immediately_exception_types)
        # Runtime state.
        self.consecutive_failures: int = 0
        self.paused: bool = False
        self.paused_reason: str | None = None
        self.paused_at: str | None = None
        self.last_tick_at: str | None = None
        self.last_success_at: str | None = None
        self.last_failure_at: str | None = None
        self.last_error_summary: str | None = None

    # ── status / control ──────────────────────────────────────────────

    def is_paused(self) -> bool:
        return self.paused

    def pause(self, reason: str) -> None:
        if self.paused:
            return
        self.paused = True
        self.paused_reason = reason
        self.paused_at = _now_z()
        logger.warning(
            "[scheduler:%s] PAUSED reason=%s; manual reset required.",
            self.name,
            reason,
        )

    def resume(self) -> bool:
        """Clear paused state and reset the failure counter. Returns True
        if the breaker was previously paused."""
        was_paused = self.paused
        self.paused = False
        self.paused_reason = None
        self.paused_at = None
        self.consecutive_failures = 0
        if was_paused:
            logger.info("[scheduler:%s] RESUMED.", self.name)
        return was_paused

    def snapshot(self) -> dict:
        """JSON-serializable view, for /admin/health and tests."""
        return {
            "name": self.name,
            "paused": self.paused,
            "paused_reason": self.paused_reason,
            "paused_at": self.paused_at,
            "consecutive_failures": self.consecutive_failures,
            "last_tick_at": self.last_tick_at,
            "last_success_at": self.last_success_at,
            "last_failure_at": self.last_failure_at,
            "last_error_summary": self.last_error_summary,
        }

    # ── per-tick recording ────────────────────────────────────────────

    def record_tick_start(self) -> None:
        self.last_tick_at = _now_z()

    def record_success(self) -> None:
        """A tick completed without raising. Resets the failure counter
        but does NOT auto-resume a paused breaker — a human must call
        resume() explicitly, otherwise a transient noisy period could
        silently re-enable a loop that was deliberately paused."""
        self.consecutive_failures = 0
        self.last_success_at = _now_z()
        self.last_error_summary = None

    def record_failure(self, exc: BaseException) -> int:
        """Classify the exception and update state. Returns the number of
        seconds the loop should sleep before its next tick (on top of the
        regular interval). The returned value is 0 when:
          - the exception was classified as non_failure (counter untouched)
          - the breaker is now paused (the loop will short-circuit via
            is_paused() instead of sleeping for backoff)
        """
        # Don't reclassify these — caller is responsible for re-raising
        # asyncio.CancelledError before reaching us.
        self.last_failure_at = _now_z()
        self.last_error_summary = _summarize_exception(exc)

        if isinstance(exc, self.pause_immediately_exception_types):
            self.pause(reason=type(exc).__name__)
            return 0
        if isinstance(exc, self.non_failure_exception_types):
            # Don't bump consecutive_failures or apply backoff: this is a
            # benign skip (e.g. DuplicatePromptError from A3).
            return 0

        self.consecutive_failures += 1
        if self.consecutive_failures >= self.max_consecutive_failures:
            self.pause(
                reason=f"consecutive_failures>={self.max_consecutive_failures}"
            )
            return 0

        # Exponential backoff: clamp at the last step so a long failure
        # streak doesn't push sleeps into hours.
        idx = min(self.consecutive_failures - 1, len(self.backoff_steps_sec) - 1)
        return int(self.backoff_steps_sec[idx])


def _summarize_exception(exc: BaseException) -> str:
    msg = f"{type(exc).__name__}: {exc}"
    if len(msg) > 240:
        msg = msg[:240] + "…"
    return msg
