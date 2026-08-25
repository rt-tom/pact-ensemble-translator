## Why

Before any strict-runner extraction, the production contracts in the 4,987-line `v4_phase12_strict_runner.py` need executable characterization coverage. Existing coverage is broad, but the planned refactor sequence needs a small, explicit baseline for the invariants most likely to regress: resume identity, whole-chapter journal shape, chunked audit under whole-chapter generation, and terminal artifact contracts.

## What Changes

- Add focused, offline characterization tests around existing strict-runner behavior only.
- Pin journal/resume identity rejection, the one-entry whole-chapter journal invariant, whole-chapter generation with chunked audit, and terminal artifact/provenance behavior.
- Document the baseline test command for later extraction changes.
- Do not move production code or alter pipeline behavior, artifact schemas, prompts, model routing, fidelity policy, or persistent data.

## Capabilities

### New Capabilities

- (none — test-only characterization; no behavior or requirement change)

### Modified Capabilities

- (none)

## Impact

- `tests/pact_v4/pipeline/test_v4_phase12_strict_runner*.py` and, only if a gap is demonstrated, adjacent test fixtures/helpers.
- No production code, configuration, runtime routing, prompts, or pipeline execution.
- This is Stage 1 of the owner-approved strict-runner sequence; later extraction stages require separate approved OpenSpec changes.