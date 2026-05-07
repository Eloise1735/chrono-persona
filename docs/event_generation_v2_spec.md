# Event Generation V2 Spec

This document captures the event-layer contract after the environment and snapshot refactors.

It is the companion spec for:

- `server/state_machine.py`
- `server/prompts.py` -> `EVENT_TRIGGER_JUDGE_PROMPT_V2`
- `server/prompts.py` -> `EVENT_MATERIALIZE_PROMPT_V2`

## Core Chain

The target event chain is:

1. `Environment Summary` locks the factual skeleton.
2. `Environment Body` contributes a few memorable detail hooks.
3. `Snapshot delta` decides whether the change truly produced internal displacement.

Short version:

- summary = is there a real event-shaped unit here
- body = what makes it memorable
- snapshot = did it actually matter inside her

## Why This Split Exists

Environment and snapshot are now richer than before.
If event generation continues to treat all text as the same flat blob, it will regress into:

- low-density event descriptions
- overreliance on atmosphere
- false positives triggered by beautiful prose
- weak future recall value

This version aims to prevent that.

## Trigger Judgment Contract

The trigger judge should primarily answer:

- does the summary contain a discrete change unit
- does the snapshot delta indicate internal shift or path rewrite
- is this more than routine continuity

It should not promote an event just because:

- the body contains vivid details
- the prose sounds emotionally rich
- the environment slice is long

## Materialization Contract

Event materialization should:

- derive the objective backbone from summary
- derive subjective impression from snapshot delta + trigger reason
- extract 1-3 detail hooks from body
- preserve an unresolved thread when one exists

## Event Description Structure

The saved event description now supports:

- `客观记录`
- `主观印象`
- `细节钩子`
- `未完成线索`

This keeps event recall denser without requiring a schema migration.

## Current Feasibility Assessment

This chain is viable with the current storage model because:

- environment JSON is already stored on snapshots
- snapshot content is already stored
- events can keep richer structure inside `description`

No database schema change is required for this phase.

## Residual Limitation

The current event table still stores only:

- `title`
- `description`
- `trigger_keywords`
- `categories`

So event detail density is improved through structured text, not through fully normalized columns.

If later you want stronger querying or UI rendering, the next step would be promoting:

- detail hooks
- open loop
- objective record
- subjective impression

into separate event fields.
