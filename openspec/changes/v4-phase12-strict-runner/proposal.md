## Why

`pact_v4/pipeline/v4_phase12_strict_runner.py` (4987 lines, ~245k) is the production v4 chapter driver — journal/resume, chunk and whole-chapter generation, audit/repair/formatting, persistent artifacts, and identity/determinism all live in one module. Direct modularization without a frozen contract risks breaking resume, identity, model-routing, and fidelity invariants. Owner approved a **planning-only** change: prepare a dedicated OpenSpec proposal for v4 strict-runner modularization whose Phase 1 is characterization and contract-mapping only, with no behavioral or refactoring work under this change.

## What Changes

- Introduce change `v4-phase12-strict-runner` (module `v4_phase12_strict_runner`) as a planning artifact: proposal, design, tasks, and contract map.
- Contract map covers: journal/resume lifecycle, whole-chapter vs chunk flows, audit/repair/formatting stages, persistent artifact inventory, identity/determinism contracts, callers/tests.
- Phase 1 scope is **characterization only**: read-only inventory, contract mapping, and identification of candidate pure-internal extractions (helpers with no pipeline-visible effect) — **without** implementing extractions.
- Explicit prohibitions under this change (without later owner approval): pipeline behavioral changes, config/model-routing/fidelity changes, file moves, signature changes, and any refactoring.

## Capabilities

### New Capabilities
- (none — planning-only change, no runtime capability)

### Modified Capabilities
- (none — no requirement/spec changes; `skip_specs: true`)

## Impact

- Docs only: `openspec/changes/v4-phase12-strict-runner/{proposal,design,tasks,contract-map}.md` + `.openspec.yaml`.
- No code, config, model-routing, prompt, or artifact changes.
- No pipeline execution, deploy, or archive.
- Risk: Low (docs/planning only).
