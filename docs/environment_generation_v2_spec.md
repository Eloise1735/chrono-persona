# Environment Generation V2 Spec

This document captures the finalized phase-one contract for environment generation.
It is the stable companion spec for:

- `server/prompts.py` -> `ENVIRONMENT_GENERATION_PROMPT_V2`
- `server/environment.py` -> `_format_recent_events()`
- `server/environment.py` -> `_build_continuity()`

## Scope

The environment layer is responsible for:

- producing a high-density narrative slice of the current moment
- showing how the outside world affects the character
- showing how the character concretely responds
- preserving 1-3 memorable detail hooks for later event extraction
- leaving open loops for future continuity

The environment layer is not responsible for:

- final personality summarization
- final internal-state consolidation
- replacing the later state-snapshot layer

## Prompt Contract

The active phase-one prompt text is stored in `ENVIRONMENT_GENERATION_PROMPT_V2`.

Key decisions locked in by this version:

- style: narrative slice, not summary prose
- body length target: 900-1600 Chinese characters
- body hard cap: 2000 Chinese characters
- summary role: factual compression, not literary restatement
- retrieval role: searchable entity/action/state compression
- coincidence handling: the prompt should faithfully process externally supplied disruptions, but should not invent pseudo-random drama when no disruption exists

## `_format_recent_events()` Spec

### Goal

Provide expandable event material for environment generation:

- factual skeleton
- subjective meaning hook
- downstream impact or unresolved consequence
- 1-2 small detail hooks when available

It should not return JSON. It should return compact prompt-ready multiline text.

### Input

`list[dict]`

Each event may include:

- `date`
- `title`
- `description`
- `objective_record`
- `subjective_impression`
- `trigger_keywords`
- `categories`
- `participants`
- `location`
- `impact`
- `open_loop`
- optional future detail fields for hook extraction

### Output shape

Recommended shape:

```text
- [2026-05-05] 与亚叶复核矿石病抑制剂临床代谢数据
  Participants: 凯尔希, 亚叶
  Location: 医疗部办公室
  What happened: 两人复核临床代谢数据，并延伸讨论非感染性呼吸道炎症的预防性干预。
  Felt meaning: 凯尔希将这场常规医疗讨论额外转译为对 Eloise 今日呼吸道状态的储备性关注。
  Impact/Open loop: 已形成初步判断，但是否转化为明确介入仍未决定。
  Detail hooks: 亚叶翻页时停顿了一下; 屏幕右侧停着花粉热力图
  Keywords: 亚叶, 矿石病抑制剂, 临床代谢数据, 花粉热力图
```

### Rules

1. Keep at most `5` high-value events.
2. Prefer these output slots:
   - `What happened`
   - `Felt meaning`
   - `Impact/Open loop`
   - `Detail hooks`
   - `Keywords`
3. Field fallback order:
   - `What happened`: `objective_record` > `description` > `title`
   - `Felt meaning`: `subjective_impression`
   - `Impact/Open loop`: `impact` > `open_loop`
4. `Detail hooks` should target `1-2` short, memorable hooks.
5. `Keywords` should contain at most `4` concrete searchable terms.
6. A single event block should usually stay within `220-320` characters.
7. If there are no useful events, return exactly:

```text
(no recent events with actionable continuity)
```

## `_build_continuity()` Spec

### Goal

Provide a concise bridge from the previous environment into the current one.

It should not simply restate the previous environment. It should expose:

- what is carrying over
- what remains unfinished
- what direction the current moment is likely to continue in

### Input

`previous_env: dict | None`

Preferred fields:

- `time_period`
- `summary`
- `activity`
- `plan_delta`
- `schedule_alignment`
- `retrieval_summary`

### Output shape

Recommended shape:

```text
Previous period (morning):
- Scene carry-over: 上午主要停留在办公室内的高密度信息处理，焦点是花粉数据、学术阅读与医疗数据复核。
- Unfinished thread: 对 Eloise 呼吸道状态的预防性干预仍停留在内部准备阶段，尚未转化为明确行动。
- Motion into now: 当前时段更接近“是否采取下一步具体介入”的临界点。
- Previous plan delta: on_track
```

### Rules

1. Do not return a single `Previous period summary: ...` line.
2. Prefer three continuity slots:
   - `Scene carry-over`
   - `Unfinished thread`
   - `Motion into now`
3. Extraction intent:
   - `Scene carry-over`: the main scene or work focus of the previous period
   - `Unfinished thread`: unresolved action, judgment, feeling, or relationship line
   - `Motion into now`: the likely next directional push
4. If `plan_delta` exists, append `Previous plan delta: ...`
5. If information is sparse, returning `1-2` lines is acceptable.
6. If `previous_env` is empty, return an empty string.

## Downstream Chain Expectation

This environment version is designed for the following flow:

1. environment layer generates the narrative slice
2. `Summary` supplies the stable factual skeleton
3. later event extraction identifies candidate events from `Summary`
4. later event extraction revisits `Environment Body` to collect `1-3` detail hooks
5. later state snapshot decides whether the experience truly changed the character internally

This separation is intentional and should be preserved in later code changes.
