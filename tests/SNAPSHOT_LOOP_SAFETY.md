# Snapshot loop safety — invariants registry

This file is the map of which test pins down which safety invariant from
[`docs/fix_plan_snapshot_loop.md`](../docs/fix_plan_snapshot_loop.md). If
any of these tests fail or get deleted, the original 21-hour token-burn
incident becomes possible again. Find the matching subsystem and fix the
underlying invariant before relaxing an assertion.

The full incident replay lives in
[`test_snapshot_loop_safety_incident_replay.py`](test_snapshot_loop_safety_incident_replay.py)
and exercises every layer end-to-end in a single test
(`test_incident_replay_all_defenses_hold`). It's the smoke test to run
when in doubt.

## Layered defenses and their tests

### A2 — LLM budget hard cap (last-line backstop)

**Invariant.** Every `LLMClient.chat()` call computes an estimated token
spend (prompt chars + `max_tokens` ceiling) and consults the
process-wide `_BudgetTracker` BEFORE any HTTP request. Hourly and daily
sliding windows; if the next call would exceed either, `BudgetExceeded`
is raised. **`BudgetExceeded` inherits from `BaseException`**, so
scattered `except Exception` blocks (in side-effect helpers, in
`_merge_environment_event_summary`, in legacy retry loops) do NOT
silently swallow it — it must propagate to the scheduler loop, where
C1's `pause_immediately_exception_types=(BudgetExceeded,)` clamps the
loop to paused. Limits are runtime-tunable via DB settings
`llm_budget_enabled`, `llm_hourly_token_limit`, `llm_daily_token_limit`.

- [`test_llm_budget_dedup.py`](test_llm_budget_dedup.py)
  - `test_budget_allows_calls_under_hourly_limit`
  - `test_budget_blocks_when_hourly_limit_would_be_exceeded`
  - `test_budget_blocks_when_daily_limit_would_be_exceeded`
  - `test_budget_window_eviction_lets_calls_through_after_an_hour`
  - `test_budget_can_be_disabled_via_configure`
  - `test_budget_snapshot_reports_used_remaining_and_rejections`
  - `test_budget_reset_clears_counters`
  - `test_budget_exceeded_is_not_caught_by_except_exception`
    *(the headline A2 invariant — locks the BaseException inheritance)*
  - `test_breaker_pauses_immediately_on_budget_exceeded`
    *(C1 integration)*

### A3 — Prompt-hash dedup at LLMClient layer

**Invariant.** Every `LLMClient.chat()` call computes a sha256 of
(model, messages, temperature) and consults the process-wide
`_PromptDedupTracker`. If the same hash was sent inside the cooldown
window (default 60s, configurable via `llm_dedup_window_sec`), it
raises `DuplicatePromptError` before any HTTP request. The dup
storm gets absorbed silently — C1 classifies `DuplicatePromptError`
as `non_failure_exception_types` so the breaker doesn't pause on
intentional skips, and `plan_engine.PLAN_TRANSIENT_GENERATION_ERRORS`
includes it so `ensure_today_plan` swallows it naturally for the next
tick to retry once the window expires. **This is the layer that catches
calls B2's per-call-site wraps miss**: plan_engine, npc_engine, evolution,
the merge helper, and any future caller — all protected without
per-site changes.

- [`test_llm_budget_dedup.py`](test_llm_budget_dedup.py)
  - `test_hash_is_stable_for_same_inputs`
  - `test_hash_changes_when_model_changes`
  - `test_hash_changes_when_messages_change`
  - `test_hash_changes_when_temperature_changes`
  - `test_dedup_blocks_identical_hash_inside_window`
  - `test_dedup_allows_after_window_expires`
  - `test_dedup_does_not_block_distinct_hashes`
  - `test_dedup_can_be_disabled_via_configure`
  - `test_dedup_rejection_counters_tick_up`
  - `test_dedup_reset_clears_state`
  - `test_dedup_lru_eviction_caps_memory`
    *(long-running process never accumulates unbounded state)*
  - `test_duplicate_prompt_error_IS_caught_by_except_exception`
    *(symmetric to A2 — dups are RuntimeError so legacy fallback
    paths absorb them)*
  - `test_breaker_classifies_duplicate_prompt_as_non_failure`
    *(C1 integration)*

Both A2 and A3 also surface state on `/api/admin/health` under
`llm_budget` and `llm_dedup`, and expose `POST
/api/admin/llm/budget/reset` and `POST /api/admin/llm/dedup/reset` for
one-click recovery after a top-up or tuning change:

- `test_admin_health_includes_llm_budget_and_dedup_blocks`
- `test_admin_reset_endpoints_clear_tracker_state`

### B1 — snapshot ordering by real UTC instant

**Invariant.** `state_snapshots.created_at` rows in either `...Z` or
`...+08:00` format must sort by their actual UTC instant, not by their
string representation. Without this the scheduler reads the wrong
baseline and re-builds the same prompt forever.

- [`test_database_snapshot_ordering.py`](test_database_snapshot_ordering.py)
  - `test_snapshot_ordering_uses_actual_instant_for_mixed_timezone_strings`
  - `test_insert_snapshot_normalizes_created_at_to_utc_z`
  - `test_snapshot_keyword_search_uses_actual_instant_order_for_mixed_timezones`
  - `test_normalize_snapshot_created_at_script_is_idempotent`

### B2 — in-flight idempotency barrier

**Invariant.** A placeholder row (`status='in_flight'`, recorded
`prompt_hash`) is committed BEFORE the LLM is invoked. If anything fails
between placeholder commit and finalize, the row stays `in_flight` or
flips to `failed`. The next tick consults `find_done_snapshot_by_prompt_hash`
/ `find_in_flight_snapshot_by_prompt_hash` /
`count_failed_snapshot_attempts` and short-circuits — the same prompt is
NEVER re-sent to the LLM after a successful run, and is dead-lettered
after 3 failed attempts.

- [`test_snapshot_in_flight_helpers.py`](test_snapshot_in_flight_helpers.py)
  - `test_insert_placeholder_creates_in_flight_row`
  - `test_finalize_snapshot_marks_done_and_writes_content`
  - `test_in_flight_placeholder_is_not_returned_as_latest` *(critical: an
    unfinished placeholder must NOT shift `get_latest_snapshot`)*
  - `test_find_done_skips_in_flight_and_failed`
  - `test_count_failed_attempts_gates_dead_letter`
  - `test_reset_stale_in_flight_flips_old_rows_to_failed`
  - `test_mark_failed_transitions_status`
- [`test_snapshot_in_flight_state_machine.py`](test_snapshot_in_flight_state_machine.py)
  - `test_compute_prompt_hash_*` (4 tests, hash stability / sensitivity / part-boundary / None tolerance)
  - `test_cache_hit_after_successful_run_skips_llm` *(the headline B2 invariant)*
  - `test_in_flight_placeholder_blocks_duplicate_llm_call`
  - `test_dead_letter_after_three_failures_refuses_llm`
  - `test_first_two_failures_still_allow_retry`
  - `test_llm_failure_marks_placeholder_failed_and_does_not_advance_done_count`

### B3 — post-finalize side-effect isolation

**Invariant.** Once `finalize_snapshot` commits `status='done'`, the LLM
spend is sunk. Subsequent OB-hold / relationship-thought / life-flow /
slowlines / disturbance-pulse writes are wrapped by
`StateMachine._run_side_effect`; their failures are logged into the
snapshot's `side_effects_status` JSON column but never propagate out of
the tick.

- [`test_snapshot_side_effect_isolation.py`](test_snapshot_side_effect_isolation.py)
  - `test_run_side_effect_records_ok_on_success`
  - `test_run_side_effect_records_failure_and_returns_default`
  - `test_run_side_effect_swallows_unrelated_failures_independently`
  - `test_run_side_effect_truncates_overlong_error_messages`
  - `test_run_side_effect_default_is_none_when_unspecified`
  - `test_update_snapshot_side_effects_status_writes_json_payload`
  - `test_update_snapshot_side_effects_status_overwrites_previous_payload`
  - `test_update_snapshot_side_effects_status_empty_payload_is_safe`
  - `test_chained_side_effects_continue_after_a_failure_and_persist_status`

### C1 — scheduler circuit breaker

**Invariant.** Each scheduler loop runs through a
`SchedulerCircuitBreaker`. Consecutive failures bump
`consecutive_failures`; exponential backoff (30 s → 2 m → 5 m → 15 m →
30 m) inserts between retries; at 10 failures the loop pauses entirely
until `resume()` is called. Success resets the counter but does NOT
auto-resume a deliberately paused breaker. Forward-compat hooks
(`pause_immediately_exception_types`, `non_failure_exception_types`)
let A2/A3 plug in their classes without re-touching the loop.

- [`test_scheduler_circuit_breaker.py`](test_scheduler_circuit_breaker.py)
  - construction / invariants (2 tests)
  - timestamp recording (2 tests)
  - backoff progression + default sequence (2 tests)
  - success resets counter, success does NOT auto-resume (2 tests)
  - pause-after-N + paused_reason / paused_at frozen (2 tests)
  - exception-classifier hooks (3 tests)
  - resume returns was-paused and preserves history (3 tests)
  - error message truncation (1 test)
  - snapshot field-shape lock (1 test)
  - StateMachine attaches both breakers (1 test)

### C3 — admin observability and one-click resume

**Invariant.** `GET /api/admin/health` returns a consistent JSON shape
exposing both breakers' state, the in-flight placeholder list (oldest
first), the last `done` snapshot/reflect timestamps, and the seconds
since each. `POST /api/admin/scheduler/{name}/resume` is idempotent and
404s on unknown names. The web page at `/admin/health` polls this
endpoint every 10 s and shows a one-click resume button when paused.
This is the property that would have flagged the original 21-hour
incident within minutes.

- [`test_admin_health_endpoint.py`](test_admin_health_endpoint.py)
  - `test_admin_health_returns_both_breakers_with_documented_shape`
    *(locks the JSON shape that web/admin-health.html depends on)*
  - `test_admin_health_surfaces_in_flight_placeholders_oldest_first`
  - `test_admin_health_reports_last_snapshot_and_reflect_timestamps`
    *(in_flight rows must NOT shift `last_snapshot_at`)*
  - `test_admin_health_handles_empty_database`
  - `test_admin_health_reflects_paused_breaker_state`
  - `test_resume_clears_paused_breaker_via_endpoint`
  - `test_resume_on_not_paused_breaker_is_idempotent`
  - `test_resume_unknown_scheduler_returns_404`
  - `test_health_reflects_resume_after_full_round_trip`

### C4 — end-to-end regression replay

**Invariant.** Every defense above, exercised as a single sequence,
matches the production scheduler tick's behavior. This test is the
smoke alarm for the whole subsystem.

- [`test_snapshot_loop_safety_incident_replay.py`](test_snapshot_loop_safety_incident_replay.py)
  - `test_incident_replay_all_defenses_hold` *(B1 → B2 → B3 → C1 → C3
    in a single walkthrough mirroring the 21-hour incident)*
  - `test_stale_in_flight_recovery_unblocks_a_stuck_prompt` *(an
    11-minute-old placeholder is flipped to failed so future ticks
    are not permanently blocked)*
  - `test_admin_health_renders_during_an_active_in_flight_window`
    *(mid-flight tick is observable on the dashboard but does not
    corrupt last_snapshot_at)*

### D1 — non-UTF-8 TEXT resilience (daily-plan corruption incident)

**Invariant.** A TEXT column holding non-UTF-8 bytes must never 500 a
read. SQLite's default text_factory strict-decodes TEXT and raises
`OperationalError: Could not decode to UTF-8 column ...`; a single such
row (written from outside the app — a non-UTF-8/GBK upload, a manual SQL
edit, or a truncated multibyte paste) would take down an entire endpoint
(`GET /api/plans/history` → `list_daily_plans` → `fetchall()`).
`_lenient_text_factory` decodes with replacement so reads survive;
`Database.repair_non_utf8_text()` rewrites the offending rows to valid
UTF-8 permanently (detected precisely via `CAST(col AS BLOB)` +
strict-decode, so it's idempotent). Exposed as `POST
/api/admin/db/repair-text` and `migrate/repair_non_utf8_text.py`.

The app's own write paths bind Python `str` (always valid UTF-8), so this
corruption can only originate outside the app — but the read path must be
defensive regardless.

- [`test_db_non_utf8_text.py`](test_db_non_utf8_text.py)
  - `test_lenient_factory_passes_valid_utf8_unchanged`
  - `test_lenient_factory_replaces_invalid_bytes_without_raising`
  - `test_list_daily_plans_survives_corrupt_row`
    *(reproduces the exact /api/plans/history 500)*
  - `test_default_factory_would_have_crashed`
    *(guards the premise — strict factory DOES raise on the same row)*
  - `test_repair_rewrites_corrupt_row_to_valid_utf8`
    *(post-repair, a STRICT-factory connection reads it cleanly)*
  - `test_repair_is_idempotent_and_clean_db_is_noop`
  - `test_repair_dry_run_reports_without_writing`
  - `test_repair_leaves_clean_rows_untouched`
  - `test_admin_repair_text_endpoint`

## Deferred phases

These phases of [`docs/fix_plan_snapshot_loop.md`](../docs/fix_plan_snapshot_loop.md)
were NOT implemented (the team opted to ship the highest-value layers
first) and have no tests yet. If you implement them later, add the
corresponding tests here so the registry stays current.

- **B4** — `idempotency_key` on `reflect_on_conversation` for client-side
  retry de-duplication. Skipped because the current production deployment
  generates reflects in the front-end model directly and does not call
  the backend reflect endpoint.
- **C2** — Hourly/daily budget threshold alerts at 80% / 95%. A2's
  tracker is already in place; C2 would add an event-stream alert when
  `hourly_used / hourly_limit` crosses 0.80 and again at 0.95 so the
  admin sees the warning BEFORE the tracker actually raises.

## Running just the safety suite

```bash
pytest tests/test_database_snapshot_ordering.py \
       tests/test_snapshot_in_flight_helpers.py \
       tests/test_snapshot_in_flight_state_machine.py \
       tests/test_snapshot_side_effect_isolation.py \
       tests/test_scheduler_circuit_breaker.py \
       tests/test_admin_health_endpoint.py \
       tests/test_llm_budget_dedup.py \
       tests/test_db_non_utf8_text.py \
       tests/test_snapshot_loop_safety_incident_replay.py -v
```

As of the D1 commit this suite is 97 tests; the full repo suite is 183.
Either should be green before merging anything that touches
`server/database.py`, `server/state_machine.py`,
`server/scheduler_breaker.py`, `server/llm_client.py`,
`server/main.py` scheduler loops, or `server/api_routes.py` admin
endpoints.
