## Why

Production `main` has been v4 since 2026-08-02 (DECISIONS.md: archive tags `archive/v3-main-20260802`, `archive/v3-main-wip-20260802`; production checkout `D:\pact\pact_translator_v4_1` tracks `origin/main`). The repository still carries the full legacy v3/v31 operational surface at top-level and in `pact_full_pipeline_runner_v1/` — `pact_translate_v3.py`, both `run_full_pipeline*.ps1` launchers, 20 `v31_*` stage/policy scripts, and Phase0C benchmark/self-test files. This surface is unowned: no active bugfix, no model-settings ownership, yet it remains executable and confusable with v4. Owner approved **preparing — not implementing** — a dedicated OpenSpec deprecation/retention plan that classifies every legacy file, maps contracts, and defines decision gates, migration/rollback, and evidence before any move/delete.

Without this plan, ad-hoc deletion risks losing recovery paths (last v3.1.3 release), breaking forensic reproducibility (benchmark `phase0c_result.json`), or re-introducing v3 prompt/model coupling into v4 operations.

## What Changes

This change is **planning-only**. No code/script moves, deletes, renames, pipeline runs, or model-settings changes.

- Creates `openspec/changes/v3-deprecation-plan/` planning artifacts: `proposal.md`, `design.md`, `tasks.md`, `inventory.md` (+ `contract-map` section).
- `inventory.md` enumerates every in-scope file (`pact_translate_v3.py`, `run_full_pipeline.ps1`, `run_full_pipeline_v31.ps1`, all `v31_*`, Phase0C `v4_phase0c_baseline.py`, `v4_phase0c_gate_bench.py`, `self_test_v4_phase0c_*.py`, `self_test_v4_phase12_strict_run.py`, `self_test_v4_v3_draft_compare.py`, plus related docs/schemas) and classifies each into exactly one bucket: **supported / historical / test / recovery / removable**.
- Defines the contract map: which v3/v31 contracts are frozen vs. superseded by v4 (artifact versions, cache identity, stage protocol, deployment provenance, Phase0C result schema).
- Defines owner decision gates, migration/rollback, and evidence requirements that any future implementation change must satisfy.
- Explicit Non-Goals enforced in this change: no file moves/deletes, no `git rm`/`mv`, no pipeline execution on `RT` or `media`, no `configs/*` or model routing edits, no `v4_phase12_strict_run.py` behavior change.

## Capabilities

### New Capabilities
- (none — planning-only; no runtime capability added)

### Modified Capabilities
- (none — no requirement change; this change is governed by `skip_specs: true`. Future implementation will delta `legacy-surface-retention` or equivalent spec)

## Impact

- **In scope (inventory only, read-only):** `pact_translate_v3.py`; `pact_full_pipeline_runner_v1/run_full_pipeline.ps1`; `pact_full_pipeline_runner_v1/run_full_pipeline_v31.ps1`; `pact_full_pipeline_runner_v1/v31_*.py` (17 files) + `v31_*.ps1` (3 files); Phase0C `v4_phase0c_baseline.py`, `v4_phase0c_gate_bench.py`, `self_test_v4_phase0c_baseline.py`, `self_test_v4_phase0c_gate.py`, `self_test_v4_phase12_strict_run.py`, `self_test_v4_v3_draft_compare.py`, `v4_measurement_harness.py`, `v4_v3_draft_compare.py`, `v4_phase0c_result_record.schema.json`, `pact_translation_benchmark_report_v4_1.md`; related `docs/releases/v3.1.2*`, `v3.1.3-rc1`, `docs/plans/V3.1.3_MODEL_REQUIRED_PROTOCOL.md`, `docs/handoffs/PACT_*`.
- **Out of scope (not touched):** `pact_v4/`, `pact_full_pipeline_runner_v1/v4_*` strict runners (`v4_phase12_strict_run.py`, `v4_book_run.py`, `v4_phase_progress.py`), `configs/`, model provider registries, runtime policies.
- **Risks if not planned:** accidental loss of `archive/v3-main-20260802` recovery path; confusion between `run_full_pipeline.ps1` (legacy v3) and v4 book runner; benchmark reproducibility loss.
- **Validation:** `openspec validate --change v3-deprecation-plan --strict` passes. No tests added (planning change). No pipeline run.
