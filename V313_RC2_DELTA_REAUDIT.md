# Pact Ensemble Translator v3.1.3 RC2 — independent delta re-audit

**Baseline:** RC1 `2dfede40…` → **RC2 `22220298…`** on `origin/develop/v3.1.3`. Read-only; no patch/merge/deploy.
**Scope:** RC1→RC2 delta and its integrations only (F1, F2, F3, F7), plus regression scan. Code verified against docs, not the reverse.
**Offline tests executed here (Python; all PASS):** `self_test_v31.py`, `self_test_stage_protocol_v31.py`, `self_test_chapter_resolver.py`, `self_test_final_ledger_scope.py`, `self_test_glossary_candidate_ledger_v31.py`, `test_formatting_integrity.py` (6). PowerShell self-tests could not be executed (no `pwsh` in the audit sandbox) and were verified by reading.

## Verification of prior findings

**F1 — terminal state (RC1: MEDIUM) → RESOLVED (CONFIRMED).**
`state.json` is now authoritative and the quality gate is its derived projection. `v31_finalize_quality.main` reads `prior_terminal_status(work)` (which reads `state.json`) and passes it into `terminal_status`, which now returns `failed` for `prior_status=="failed"` and `quarantined` for `prior_status=="quarantined"` — monotonicity is enforced in the live path, not just in a unit-tested helper. Terminal writes go through `publish_terminal_pair` (generation-stamped marker → `state.json` → `v31_quality_gate.json`, marker unlinked), and `recover_terminal_pair` fails closed, reconstructing a stale/absent gate from `state.json` and ignoring an unpublished marker. Redo remains the explicit escape hatch (DAG invalidation deletes `state.json`). Files: `v31_final_lifecycle.py` (`prior_terminal_status`, `publish_terminal_pair`, `recover_terminal_pair`, `terminal_status`), `v31_finalize_quality.py:66,229–320`.

**F2 — version identity / cache / monitor (RC1: HIGH) → RESOLVED (CONFIRMED).**
`v31_common.VERSION = "3.1.3"` is now a single semantic identity (`ARTIFACT_VERSION` alias), explicitly separated from the runner build label (`3.1.3-05`). `valid_aggregate` enforces `compatible_artifact_version` (must equal `3.1.3`; legacy `3.1.2j` only under the explicit `--allow-legacy-artifact-version` opt-in), replacing the RC1 "is-a-string" check. Legacy reuse requires a provenance path and writes durable, hashed, de-duplicated records (`record_accepted_legacy_reuse`, schema `pact-v31-legacy-reuse-provenance/v1`). The monitor compares artifacts against the **semantic** `artifact_version` (state → `ensemble_v31.version` fallback), shows the build tag separately, and routes opt-in legacy artifacts to a "Legacy-compatible" line instead of "Mixed", so a clean 3.1.3 run reports `Resume: READY`. Files: `v31_common.py:27–43`, `v31_stage_protocol.py`, `monitor_pipeline_v31.ps1:36–39,78–137`.

**F3 — per-chapter final ledger (RC1: HIGH) → RESOLVED (CONFIRMED).**
New `v31_final_ledger_scope.py` builds a canonical per-chapter map `{chapter_id, work_stem, ledger_path}` from the chapter manifest (schema-checked, duplicate-stem-rejected, each ledger required to exist). `v31_audit` takes `--pids-map`; `scoped_ledger_path_for_work` resolves each chapter to **its own** `v31_final_changed_pid_ledger.json` and raises if the mapped path points outside that chapter (no foreign-ledger fallback), and `ledger_target_pids` verifies the ledger PIDs belong to that chapter's manifest. The runner resolves the scope map and passes `--pids-map` to both final audit passes. `self_test_final_ledger_scope.py` PASS. Multi-chapter single-invocation runs are now safe. Files: `v31_final_ledger_scope.py`, `v31_audit.py:385–422,430–440`, `run_full_pipeline_v31.ps1:729–738`.

**F7 — deploy / rollback (RC1: evidence gap) → IMPLEMENTED, but the tooling has one confirmed defect (see R1).**
`v31_release_deploy.ps1` implements annotated-tag-first resolution, active-worktree verification (`Get-ProjectRoot` rejects parent/nested paths), release-manifest hash+schema validation (`Assert-Manifest`, `Get-GitBlobSha256`), migration approval gating on schema change (`Assert-Migrations`), pre-deploy backup (`Save-Backup`), fast-forward-only deploy / ancestor-checked rollback, cache preservation (`Assert-CachePreserved` over `pipeline_runs`), and rejection of BOM (`Read-JsonFile`), hash tampering, false `schema_changes`, and non-active paths. The design meets the F7 requirements — subject to R1.

## New RC2-delta findings

### R1 — Deployment tooling is broken on Windows PowerShell 5.1 (`ProcessStartInfo.ArgumentList` under StrictMode)
- **Severity:** HIGH · **Status:** CONFIRMED (also self-declared in `FINDING_MAPPING.md`)
- **Affected files/functions:** `v31_release_deploy.ps1` → `Get-GitBlobSha256` (lines 145–153); test `self_test_release_deploy_v31.ps1`
- **Failure lifecycle:** `Get-GitBlobSha256` constructs `[Diagnostics.ProcessStartInfo]::new()` and calls `$psi.ArgumentList.Add(...)`. `ProcessStartInfo.ArgumentList` exists only on .NET Core / PowerShell 7+; on Windows PowerShell 5.1 the property is absent, and `Set-StrictMode -Version Latest` (line 16) turns the missing-property access into a terminating error. Every path — `New-Manifest`, `Assert-Manifest` (verify), `Deploy`, `Rollback` — routes through `Get-GitBlobSha256`, so the entire manifest/verify/deploy/rollback tool is non-functional under PS 5.1. It works only under pwsh 7+. The rest of the script correctly uses array-argument `& git` via `Invoke-Git`, so this is an isolated inconsistency.
- **Evidence:** code lines above; `FINDING_MAPPING.md` records the self-test failure "missing `ProcessStartInfo.ArgumentList` property under strict mode," left unrepaired in this package.
- **Existing coverage:** `self_test_release_deploy_v31.ps1` (fails at this point).
- **Missing regression test:** run the deploy self-test under the **minimum supported** PowerShell edition; assert `Get-GitBlobSha256` succeeds there.
- **Disposition:** Mandatory before relying on automated deployment. Fix by hashing via array-arg `& git cat-file blob …` (as `Invoke-Git` already does) or `ProcessStartInfo.Arguments`, **or** formally require and gate on PowerShell 7+. Does **not** affect chapter translation runs (the pipeline runner does not call this script).

### R2 — PowerShell self-tests unverified by execution; PS edition unpinned
- **Severity:** LOW · **Status:** QUESTION
- **Detail:** The monitor, model-policy, preflight, startup-args, and release-deploy self-tests are PowerShell and could not be executed in this sandbox; they were reviewed statically. R1 shows the codebase is PowerShell-edition-sensitive, yet no minimum edition is pinned/asserted. The pipeline runner itself uses only PS-5.1-safe constructs (`Start-Process -ArgumentList`, `Get-CimInstance`); only the new deploy script is affected.
- **Disposition:** Pin and document the supported PowerShell edition; run the PS suite in CI on that edition. Low risk for chapter runs.

### Regression scan — none found
- **Glossary freeze preserved (improved).** RC2 removed the RC1 snapshot/restore in `prepare_pipeline_context`, but the auto-mutating writer was renamed to `Glossary.legacy_update` (**no active callers**), and the live `Glossary.update` now delegates to `GlossaryCandidateLedger.observe_chapter`, which writes only the run/book candidate-ledger paths — never `locked/established/provisional`. `self_test_glossary_candidate_ledger_v31.py` asserts the glossary dir is byte-identical after `update()` and PASSES. The freeze is now structural rather than dependent on snapshot/restore. (Cleanup: `legacy_update` is dead code — LOW.)
- **Atomic durability hardened.** New `write_text_atomic` / `write_json_atomic` use temp-file + `fsync` + read-back (+ optional validator) before `os.replace`, so an interrupted write can't replace a good artifact.
- **Acknowledged carry-over debt (not RC2 regressions):** F4 (no input-content fingerprint in cache identity) and F6 (some runner/DAG operations still iterate all `work/` chapters, not just the selected range) remain, documented in `REMAINING_DEBT_F4_F6.md`.

## Verdict

| Severity | Count (RC2 delta) |
|---|---|
| BLOCKER | 0 |
| HIGH | 1 (R1) |
| MEDIUM | 0 |
| LOW | 1 (R2 + `legacy_update` dead code) |

**Decision: APPROVE WITH FIXES.** F1, F2, and F3 are resolved and correctly integrated into the active path; no regressions were introduced. R1 is the only mandatory fix and it gates only the deployment tooling, not the translation pipeline.

**Mandatory before using the automated deploy path to main:** fix R1 (array-arg git hashing, or require/gate PowerShell 7+).

**Readiness:**
- **2–3 real chapter runs:** **YES**, and — unlike RC1 — multi-chapter single-invocation runs are now safe (F3). The monitor now reports version/resume state correctly (F2), and terminal state is authoritative/monotonic (F1). No operational caveats remain for the pipeline itself.
- **Release / merge to main:** the pipeline is ready. If the intended installation path is the new `v31_release_deploy.ps1` tooling, R1 must be fixed first (or the deploy run under PowerShell 7+); manual/existing deployment is unaffected.
- **v4 planning:** unblocked; the remaining architectural items are the acknowledged F4 (cache input fingerprint) and F6 (selected-chapter scoping).

**Questions requiring real-run / environment data:**
- Target PowerShell edition for the runner and deploy tooling (R1/R2).
- Execution of the PowerShell self-test suite on the supported edition.
- Rate of global-smoke false-negatives on unchanged PIDs and how often `uncertain_policy='repair'` routes to repair (carried over from RC1; needs a real run).
