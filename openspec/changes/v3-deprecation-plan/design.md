## Context

`main` at `263398f` is whole-chapter v4.1 with `DECISIONS.md` 2026-08-02 recording the v3→v4 cutover: v3 line frozen at tag `archive/v3-main-20260802` (commit `4cbf958`, v3.1.3-hotfix.2) and WIP at `archive/v3-main-wip-20260802`. Yet the worktree still contains the full executable v3/v31 surface. Owner explicitly authorized **planning** a deprecation/retention plan, forbidding implementation (no moves/deletes, no pipeline runs, no model-settings edits) until gates are satisfied. Existing `v41-runtime-efficiency` (complete) does not cover legacy surface retention.

Current state constraints:
- `pact_translate_v3.py` (`VERSION 3.1.2d`) is importable and self-contained; last production artifact version is `3.1.3` (`v31_common.VERSION`) with temporary legacy compat `3.1.2j`.
- Two launchers coexist: `run_full_pipeline.ps1` (Runner `1.2.0`, v3 era) and `run_full_pipeline_v31.ps1` (Build `3.1.3-04`, v31 era with `AllowLegacyArtifactReuse`, `QwenContextSize` 32-65K, preflight policy).
- 20 `v31_*` modules implement atomic artifacts, DAG, stage protocol, audit/repair/finalize; they are the only holders of v3.1.3 contracts.
- Phase0C files (`v4_phase0c_baseline.py`, `v4_phase0c_gate_bench.py`, `self_test_v4_phase0c_*.py`) are read-only measurement harnesses that import `v4_measurement_harness.py` and never invoke models; they produced the frozen `phase0c_result.json` (`schema pact-v4-phase0c-result-record/v1`) now referenced by `docs/audits/pact_translation_benchmark_report_v4_1.md`.

## Goals / Non-Goals

**Goals:**
- Single inventory classifying every legacy file into one of five buckets: supported / historical / test / recovery / removable, with rationale and contract binding.
- Contract map: which v3/v31 contracts remain authoritative for forensics vs. superseded by v4.
- Explicit owner decision gates, migration/rollback plan, and evidence requirements that any future deprecation implementation must satisfy.
- Zero implementation in this change — planning artifacts only, validated by `openspec validate`.

**Non-Goals:**
- No file moves, deletes, renames, `git rm/mv`, or archive tag mutation.
- No pipeline execution on `RT` (`D:\pact\pact_translator_v4_1`) or `media`; no `self_test` runner invocation.
- No model routing, provider registry (`configs/providers.yaml`), budget, or reasoning-effort changes.
- No v4 runtime behavior change (`pact_v4/`, `v4_phase12_strict_run.py`, `v4_book_run.py`, `v4_phase_progress.py`).
- No new spec deltas in this planning change (`skip_specs: true`); deltas belong to a future implementation change after gate approval.

## Decisions

- **Bucket taxonomy — five mutually exclusive labels:**
  - `supported` — still required for current production operations (none of the legacy surface; v4 files are not in scope for this bucket).
  - `historical` — frozen, read-only, keep for audit/history but never executed in production; e.g., `pact_translate_v3.py`, `run_full_pipeline.ps1` (pre-v31), `v31_source_analysis.py` as historical reference for v3 prompt policy.
  - `test` — never production; offline contract/self-test harness only; e.g., `self_test_v4_phase0c_*.py`, `self_test_v4_v3_draft_compare.py`, `v4_phase0c_gate_bench.py`.
  - `recovery` — minimal set required to reproduce or roll back the last v3.1.3 release from `archive/v3-main-20260802` on `RT` if v4 rollback were ever ordered; e.g., `run_full_pipeline_v31.ps1` + `v31_common.py` + `v31_artifact_dag.py` + `v31_stage_protocol.py` + `v31_release_deploy.ps1` + `docs/releases/v3.1.3-rc1`.
  - `removable` — candidate for deletion/archival after recovery gate passes; not required for history or recovery; e.g., duplicate helpers already superseded by v4 equivalents or unreferenced after inventory triage. No file is marked removable for execution in this change — label is advisory for a future gated PR.

- **Inventory file = single source of truth:** `inventory.md` inside this change holds the full table (file, lines, role, bucket, contract, retention). Proposal/design/tasks reference it; no duplicate tables.

- **Contract map — frozen vs. superseded:**
  - Frozen (v31 authoritative, do not re-interpret): `ARTIFACT_VERSION 3.1.3` / `CACHE_IDENTITY_SCHEMA pact-v31-cache-identity/v1` (`v31_common.py`), `pact-v31-stage-protocol` (`v31_stage_protocol.py`), `deployment_provenance.v31.json` archival provenance (DECISIONS.md 2026-08-02), `pact-v4-phase0c-result-record/v1` schema (`docs/schemas/v4_phase0c_result_record.schema.json`), `pact-v4-golden-record/v1` (`v4_phase0c_baseline.py` Track A).
  - Superseded (v4 is source of truth): all prompt/bible/glossary/runtime contracts (`pact_v4/phase2/prompts.py`, `bible_renderer.py`, `memory.py`), `v4_book_run` orchestration, `v4_phase_progress` monitoring — legacy callers must not be treated as v4 contract holders.
  - Alternatives considered — keep v31 stage protocol as fallback for v4: rejected (v4 has its own strict runner contracts; mixing would re-introduce v3 failure modes like `max_consecutive_terminal_nonselections` vs `max_consecutive_nonselections`).

- **Gates are owner-operated, not agent-operated:** every bucket transition from `historical`/`recovery` → `removable` requires explicit owner approval recorded in `DECISIONS.md` and a tag/provenance check.

## Risks / Trade-offs

- **Risk: inventory misclassifies a recovery-critical file as removable → loss of rollback capability.** Mitigation: recovery bucket defined as minimal reproducible set verified against `archive/v3-main-20260802` tag and `docs/releases/v3.1.3-rc1`; no deletion occurs in this planning change; future PR must include `git show archive/v3-main-20260802:pact_translate_v3.py` / `v31_common.py` reproducibility evidence.
- **Risk: self-tests assumed safe but actually import legacy translation logic.** Mitigation: `self_test_v4_phase0c_*.py` are `test`-bucket, flagged as `no pipeline run`; they import only measurement harnesses (`v4_measurement_harness`), never call `pact_translate_v3.py`; verified by static grep in inventory.
- **Risk: benchmark report continuity loss (`pact_translation_benchmark_report_v4_1.md`).** Mitigation: report + schema are `historical` bucket; retention is permanent; any archival PR must preserve `docs/audits/` and `docs/schemas/v4_phase0c_result_record.schema.json`.
- **Risk: launcher confusion (`run_full_pipeline.ps1` vs `run_full_pipeline_v31.ps1` vs `v4_book_run.py`).** Mitigation: inventory explicitly marks `run_full_pipeline.ps1` as `historical` (pre-v31, Runner 1.2.0, no `AllowLegacyArtifactReuse`) and `run_full_pipeline_v31.ps1` as `recovery` (last production launcher, Build 3.1.3-04); README navigation (branch `docs/readme-v4-navigation`) already points to `v4_book_run.py`.
- **Trade-off: comprehensive inventory vs. maintenance burden.** Chose single `inventory.md` table updated once; future file additions require inventory amendment and re-validation, not ad-hoc docs.

## Migration Plan

Planning-only change — no migration executes.

**Future implementation (gated, not in this change) — ordered steps:**
1. Gate G1 — Owner approves inventory/contract map (this change) and records decision in `DECISIONS.md`.
2. Gate G2 — Owner selects disposition per bucket: `historical` → keep in-repo read-only (optionally move to `archive/v3/` with `git mv` preserving history) or extract to `archive/v3-main-20260802` tag only; `recovery` → keep until G3; `removable` → delete.
3. Preparation — create `archive/v3/` mirror (if chosen) via `git mv` (not `rm`), verify `archive/v3-main-20260802` tag still resolves and `v31_common.VERSION` matches; run `openspec validate --strict` and `git diff --stat` hygiene.
4. Execution — gated PR deletes only `removable` bucket files; CI must show `pytest tests/pact_v4 -q` and `compileall` still green (v4 has no import of legacy surface — verified by grep).
5. Verification — `git log --follow -- <archived-path>` retains history; `docs/audits/pact_translation_benchmark_report_v4_1.md` still renders.

**Rollback:**
- Before G2, rollback is `git revert` of planning commit (no file moves to undo).
- After future archival: `git revert` of archival commit restores files; tag `archive/v3-main-20260802` remains immutable safety net; if `archive/v3/` was created, `git mv archive/v3/<file> <orig>` restores. No pipeline state to roll back because no pipeline was run.

## Open Questions

- Exact `archive/v3/` layout if owner prefers in-repo archival vs. tag-only retention — decision at G2 (inventory supports either; table records original path for both).
- Whether `v4_measurement_harness.py` (shared Phase0A helper) is considered legacy or v4 historical — currently classified `historical` (read-only, no v4 runtime import), but could be reclassified `supported` if v4 book-run measurement still needs it; pending owner confirmation at G1 review.
