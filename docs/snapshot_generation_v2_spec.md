# Snapshot Generation V2 Spec

This document captures the phase-two prompt contract for state snapshot generation.

It is the companion spec for:

- `server/prompts.py` -> `SNAPSHOT_GENERATION_PROMPT_V2`

## Scope

The snapshot layer is responsible for:

- recording what has already settled inside the character
- turning scene experience into an inheritable internal-state trace
- preserving nuanced differences in emotional tone, attention, and judgment
- helping later event routing decide whether something truly changed the character

The snapshot layer is not responsible for:

- re-narrating the environment scene
- summarizing all external events again
- replacing the environment layer's concrete scene work

## Core Separation From Environment Layer

Environment layer:

- writes the external-world slice
- emphasizes scene progression, interaction, action, pressure, and open loops

Snapshot layer:

- writes the internal residue after that slice has entered the character
- emphasizes attention shift, bodily load, judgment drift, emotional weather, and unresolved internal weight

Short version:

- environment = what is happening around and through her
- snapshot = what has already become true inside her

## Main Problems This Version Targets

This version is explicitly designed to reduce:

- repetitive intentional phrasing
- templated reuse of special terms
- abstract, smooth, undifferentiated first-person narration
- emotionally flat output where every state sounds like the same restrained voice

## Prompt Contract

Key decisions locked in by this version:

1. The snapshot is an internal sediment layer, not a scene rewrite.
2. Emotional distinction is required even under a restrained voice.
3. Concrete mental and bodily micro-signals are preferred over free-floating abstract terminology.
4. Abstract, philosophical, or conceptual thought is allowed, but only when it grows from a concrete present trigger and returns to the current judgment.
5. Memory and association are allowed only when they affect the present inner state.
6. The output should remain inheritable by later generations.

## Emotional Differentiation Requirement

The prompt now explicitly asks the model to distinguish low-intensity but meaningfully different internal weathers, such as:

- controlled tension
- tired focus
- post-touch recoil
- rational suppression over mild irritation
- low-grade concern
- temporary loosening or warmth
- unnamed hesitation

These are examples, not a closed label set. The model should not treat them as a fixed menu.

The point is not to dramatize the character, but to avoid flattening every snapshot into the same calm register.
Whenever possible, emotional difference should be shown through:

- where attention keeps returning
- breath, muscle, and bodily load
- speed or drag in the thinking process
- reaction style toward other people
- the way unfinished concerns remain active

This is preferred over simply naming an emotion directly.

## Output Contract

- first person only
- no title
- target length: 350-700 Chinese characters
- no list structure
- no scene retelling
- no free-floating philosophical ornament
- last sentence should land on a real internal stopping point

## Downstream Expectation

This snapshot version should help later systems answer:

- what she currently cares about most
- where her internal load is leaning
- whether a judgment has shifted
- which emotional or relational line is quietly gaining weight

That is the main reason this layer remains necessary even after the environment layer becomes more detailed.
