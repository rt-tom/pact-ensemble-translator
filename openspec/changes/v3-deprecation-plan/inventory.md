# Inventory & Contract Map — v3/v31 legacy surface (read-only)

> Planning-only artifact. No file was moved, deleted, or executed. Classification is advisory until Gates G1/G2 owner approval (see `design.md` §Migration Plan, `tasks.md` §3). Original paths are preserved for both in-repo and tag-only retention options.

## Legend

| Bucket | Meaning | Retention | Requires owner gate to delete? |
|---|---|---|---|
| **supported** | Active production contract on `main` (v4). Not assigned to legacy files; listed for contrast. | keep, actively evolved | — |
| **historical** | Frozen read-only reference; keep for audit/forensics; never executed in production. | keep (in-repo or `archive/v3-main-20260802` tag) | yes (G2) |
| **test** | Offline harness / self-test / benchmark helper; never production. | keep while harness is useful; removable after harness retired | yes (G2) |
| **recovery** | Minimal set to reproduce/roll back last v3.1.3 release (`archive/v3-main-20260802`) if ordered. | keep until explicit G2 retire | yes (G2+G3) |
| **removable** | No history/recovery value; candidate for deletion after G2. No file is deleted in this change. | delete candidate (future gated PR) | yes (G2) |

## 1. In-scope file list (source: `find . -type f | sort` on worktree 2026-08-24)

Line counts are `wc -l` on the worktree HEAD `263398f`.

### 1.1 Root and launchers

| File | Lines | Role | Bucket | Rationale | Contract |
|---|---|---|---|---|---|
| `pact_translate_v3.py` | 3358 | Legacy ensemble translator (v3 core, `VERSION 3.1.2d`) | **historical** | Frozen v3 logic; superseded by `pact_v4/`; preserved for forensics via tag `archive/v3-main-20260802` | `__version__ 3.1.2d` frozen; not `ARTIFACT_VERSION 3.1.3` |
| `pact_full_pipeline_runner_v1/run_full_pipeline.ps1` | ~430 | Pre-v31 launcher (Runner `1.2.0`, `Start/End/Reset/RedoAudit/Verifier/Repair/Formatting`) | **historical** | Pre-v31 topology; no `AllowLegacyArtifactReuse`, no `QwenContextSize` flag; replaced by `run_full_pipeline_v31.ps1` then v4 `v4_book_run.py` | v3 DAG via `verify_pipeline_issues.py` |
| `pact_full_pipeline_runner_v1/run_full_pipeline_v31.ps1` | ~630 | Last production launcher (Build `3.1.3-04`, `ARTIFACT_VERSION 3.1.3`) | **recovery** | Last production run path for `archive/v3-main-20260802`; required for rollback reproduction on `RT` | `BuildIdentity 3.1.3-04`, `ARTIFACT_VERSION 3.1.3`, `TEMPORARY_LEGACY_COMPATIBILITY_POLICY` |

### 1.2 `v31_*` stage/policy surface (20 files, `pact_full_pipeline_runner_v1/`)

| File | Lines | Role | Bucket | Rationale | Contract |
|---|---|---|---|---|---|
| `v31_common.py` | 557 | Shared identity/hash/cache/model-call helpers (`VERSION 3.1.3`, `CACHE_IDENTITY_SCHEMA`) | **recovery** | Defines `ARTIFACT_VERSION`, `LEGACY_COMPATIBLE_ARTIFACT_VERSIONS {"3.1.2j"}`, `compatible_artifact_version()`; required for any v3.1.3 re-run | `ARTIFACT_VERSION 3.1.3`, `CACHE_IDENTITY_SCHEMA pact-v31-cache-identity/v1` |
| `v31_artifact_dag.py` | 116 | Artifact dependency graph | **recovery** | DAG is source of truth for stage ordering; needed to reconstruct `archive/v3-main-20260802` runs | `pact-v31-stage-protocol` DAG |
| `v31_stage_protocol.py` | 194 | Stage execution protocol (aggregate completeness vs. partial cache) | **recovery** | Enforces `MODEL_REQUIRED` probe path (`docs/plans/V3.1.3_MODEL_REQUIRED_PROTOCOL.md`); keep for recovery correctness | `pact-v31-stage-protocol` |
| `v31_source_analysis.py` | 461 | Source analysis stage (`numbers`/`names` pre-screen) | **historical** | Stage logic frozen; v4 has `pact_v4/phase2/risk.py` + `phase0b/source_html.py`; keep for audit only | v31 source contracts |
| `v31_audit.py` | 564 | Qwen+Gemma audit stage | **historical** | v31 audit topology; v4 uses `pact_v4/audit/chunked_audit.py` + `phase3` | v31 audit verdict schema |
| `v31_merge_issues.py` | 203 | Merge audit issues | **historical** | v31-only merge; no v4 counterpart | — |
| `v31_cross_verify.py` | 256 | Cross-verification | **historical** | v31 cross-verify; v4 uses `phase5/repair` + `audit` | — |
| `v31_repair.py` | 284 | Targeted repair | **historical** | v31 repair; v4 uses `phase4` quarantine/retry | — |
| `v31_deterministic_gate.py` | 119 | Deterministic integrity gate | **historical** | Deterministic checks now in `pact_v4/_integrity_checks.py` | — |
| `v31_adjudicate.py` | 219 | Adjudication | **historical** | v31 adjudicate; v4 uses `cascade.py` | — |
| `v31_postcheck.py` | 291 | Post-checks | **historical** | No active production path | — |
| `v31_finalize_quality.py` | 335 | Finalize quality | **historical** | Finalize path frozen; note `v31_final_ledger_scope.py` scoping | — |
| `v31_finalize_verification.py` | 218 | Finalize verification | **historical** | Verification frozen | — |
| `v31_final_lifecycle.py` | 140 | Final lifecycle | **historical** | Lifecycle tail | — |
| `v31_build_review.py` | 92 | Build review summary | **historical** | Review builder | — |
| `v31_chapter_resolver.py` | 128 | Chapter resolver (`--chapters` mapping) | **historical** | Resolver reused only by v31 launcher; v4 uses `build_chapter_index.py` + `WholeChapterPidMap` | — |
| `v31_final_ledger_scope.py` | 53 | Final ledger scoping helper | **historical** | Ledger scope helper | — |
| `v31_preflight_policy.ps1` | 86 | Preflight policy (PowerShell) | **recovery** | Invoked by `run_full_pipeline_v31.ps1` `RequiredRunnerFiles`; required for recovery boot | preflight policy contract |
| `v31_runner_model_policy.ps1` | 50 | Runner model policy (PowerShell) | **recovery** | `RequiredRunnerFiles` entry; defines model start/skip path for recovery | runner model policy |
| `v31_release_deploy.ps1` | 359 | Release deploy (archive/tag provenance) | **recovery** | Implements `deployment_provenance.v31.json` archival + drift-guard; fallback if recovery deploy needed | `deployment_provenance.v31.json` |

### 1.3 Phase0C benchmark / self-test surface

| File | Lines | Role | Bucket | Rationale | Contract |
|---|---|---|---|---|---|
| `pact_full_pipeline_runner_v1/v4_phase0c_baseline.py` | 1191 | Phase0C baseline measurement (read-only, Tracks A/B) | **historical** | Produces `phase0c_result.json` (`SCHEMA_VERSION pact-v4-phase0c-result-record/v1`); never invokes models; extends `v4_measurement_harness` | `SCHEMA_VERSION pact-v4-phase0c-result-record/v1`, `TOOL_VERSION pact-0c/0.2` |
| `pact_full_pipeline_runner_v1/v4_phase0c_gate_bench.py` | 149 | Phase0C gate benchmark helper | **test** | Gate-only bench (small helper, no pipeline) | gate bench config |
| `pact_full_pipeline_runner_v1/self_test_v4_phase0c_baseline.py` | 535 | Self-test for `v4_phase0c_baseline.py` (synthetic fixtures only) | **test** | Offline regression; imports `v4_phase0c_baseline` only; no book text | harness contract |
| `pact_full_pipeline_runner_v1/self_test_v4_phase0c_gate.py` | 608 | Self-test for gate bench | **test** | As above; no live run | — |
| `pact_full_pipeline_runner_v1/self_test_v4_phase12_strict_run.py` | 282 | Self-test for strict runner chapter trial | **test** | Validates strict runner harness | — |
| `pact_full_pipeline_runner_v1/self_test_v4_v3_draft_compare.py` | 349 | Self-test for `v4_v3_draft_compare.py` | **test** | Compare harness test | — |
| `pact_full_pipeline_runner_v1/v4_measurement_harness.py` | 538 | Phase 0A shared helpers (hashing, JSON/read, word count) | **historical** | Imported by Phase0C baseline; not a production runner; DECISIONS.md 2026-07-28/30 reserves it as read-only | `v4_measurement` helpers |
| `pact_full_pipeline_runner_v1/v4_v3_draft_compare.py` | 479 | `v4_vs_v3` draft compare (read-only) | **test** | Measurement/comparison harness, not production translation | compare contract |
| `pact_full_pipeline_runner_v1/compare_pipeline_review.py` | 1319 | Pipeline review comparator | **test** | Offline comparator; not production | — |
| `pact_full_pipeline_runner_v1/verify_pipeline_issues.py` | 673 | Verify audit issues (v3) | **historical** | Invoked by `run_full_pipeline.ps1`; frozen | — |
| `pact_full_pipeline_runner_v1/verify_repair_results.py` | 593 | Verify repair results (v3) | **historical** | As above | — |
| `docs/schemas/v4_phase0c_result_record.schema.json` | ~120 | Phase0C result record JSON schema (`pact-v4-phase0c-result-record/v1`) | **historical** | Schema requires `track_b.notes`, `track_b.terminal_discrepancy`, typed `residual_errors.final_residual_total`; frozen 2026-07-30 | `pact-v4-phase0c-result-record/v1` |
| `docs/audits/pact_translation_benchmark_report_v4_1.md` | ~420 | Benchmark report (5 variants on ~110 PID + Independent №1 vs Pipeline Remote) | **historical** | Permanent evidence for D3/quality acceptance (DECISIONS.md 2026-08-05) | benchmark 8.3/8.05/7.4/6.7/5.0 scores |
| `tools/verify_baseline_report.py` | 950 | Baseline report verifier (tools) | **test** | Tool-side verifier | — |
| `pact_full_pipeline_runner_v1/v4_phase12_strict_run.py` | 1024 | **NOT in scope for deprecation** — v4 strict runner (active production) | **supported (v4, out of inventory scope)** | Listed only to prevent misclassification; never `historical`/`removable` | `pact-v4-prompt-bundle/v3`, chunk plan ownership |
| `pact_full_pipeline_runner_v1/v4_book_run.py` | 1575 | **NOT in scope** — v4 book runner (active production) | **supported (v4)** | Successor to `run_full_pipeline_v31.ps1` | `book_run.json`, `book_memory_hash` |
| `pact_full_pipeline_runner_v1/v4_phase_progress.py` | 2655 | **NOT in scope** — v4 phase progress monitor | **supported (v4)** | — | — |

Related docs/releases (always historical, never removable without G2):

| File | Bucket | Note |
|---|---|---|
| `docs/releases/v3.1.2/`, `docs/releases/v3.1.3-rc1/` | **historical** | Release notes/artifact snapshots |
| `docs/plans/V3.1.3_MODEL_REQUIRED_PROTOCOL.md` | **historical** | Mandatory MODEL_REQUIRED probe contract spec |
| `docs/handoffs/PACT_V3_1_1_HANDOFF_RU.md`, `PACT_PRE_V3_HISTORY_ADDENDUM_RU.md` | **historical** | Handoff context |
| `configs/` (all `*.example.yaml`, `providers.yaml`) | **supported (v4)** | Out of deprecation scope; not classified here |

## 2. Contract map (frozen vs. superseded)

### 2.1 Frozen — v31 authoritative, immutable

| Contract | File / Schema | Version / Content | Retained as |
|---|---|---|---|
| Artifact version & legacy compat | `v31_common.py` `VERSION`, `LEGACY_COMPATIBLE_ARTIFACT_VERSIONS`, `compatible_artifact_version()` | `3.1.3` + `{"3.1.2j"}` + `TEMPORARY_LEGACY_COMPATIBILITY_POLICY` | **recovery** — required to validate old `pipeline_runs/` |
| Cache identity | `v31_common.py` | `CACHE_IDENTITY_SCHEMA pact-v31-cache-identity/v1` | **recovery** |
| Stage protocol (aggregate completeness vs partial cache, MODEL_REQUIRED) | `v31_stage_protocol.py` + `V3.1.3_MODEL_REQUIRED_PROTOCOL.md` | probe modes `REUSED`/`MODEL_REQUIRED`/`FAILED` | **recovery** |
| Deployment provenance (v31 archival) | `v31_release_deploy.ps1`, `DECISIONS.md` 2026-08-02 | `deployment_provenance.v31.json` archival | **recovery** |
| Phase0C result record | `docs/schemas/v4_phase0c_result_record.schema.json` + `v4_phase0c_baseline.py` | `SCHEMA_VERSION pact-v4-phase0c-result-record/v1` (typed `final_residual_total`, required `track_b.notes`) | **historical** |
| Golden record | `pact_v4/phase0b` + `v4_phase0c_baseline.py` Track A | `pact-v4-golden-record/v1` (57 accepted PID Track A) | **historical** |
| Benchmark scores | `docs/audits/pact_translation_benchmark_report_v4_1.md` | 5-variant blind benchmark (DeepSeek High ~8.3 etc.) + Pipeline Remote High ~7.9 | **historical** |

### 2.2 Superseded — v4 is source of truth, legacy must not govern

| Legacy (do not use for v4) | Superseded by (authoritative) |
|---|---|
| `pact_translate_v3.py` prompt/bible (append-only bible, no `chapter_index.json`) | `build_chapter_index.py` + `pact_v4/phase1/book_memory_candidates.py` / `bible_renderer.py` (A2 deterministic chapter_index) |
| `v31_source_analysis.py` source contracts | `pact_v4/phase2/risk.py` `REQUIRED_RISK_CATEGORIES` + `phase0b/source_html.py` |
| `run_full_pipeline*.ps1` launcher topology | `pact_full_pipeline_runner_v1/v4_book_run.py` + `v4_phase12_strict_run.py` (`--memory-dir`, `WholeChapterPidMap`, `book_memory_hash`) |
| `v31_*` audit/repair/finalize pipeline | `pact_v4/audit/chunked_audit.py`, `phase3/`, `phase4/quarantined_retry.py`, `phase5/formatting` |

## 3. Forbidden in this change (enforced)

- No `git mv` / `git rm` / `rm -rf` on any file above.
- No pipeline launch (`run_full_pipeline*.ps1`, `v4_book_run.py`) on `RT` or `media`.
- No `configs/` or `providers.yaml` or model routing edit.
- No `v4_phase12_strict_run.py` behavior change.
- Any future deletion requires Gates G1→G2→G3 and evidence in §4.

## 4. Evidence appendix (static, no pipeline run)

```
# 4a — v4 has no production import of legacy surface (expected: zero hits)
$ grep -R "pact_translate_v3\|v31_" pact_v4/ --include="*.py" | grep -v ".pyc" | wc -l
0

# 4b — recovery bucket reconstructs last v3.1.3 (tag still resolves)
$ git show archive/v3-main-20260802:pact_translate_v3.py | head -n 1
#!/usr/bin/env python3

$ git show archive/v3-main-20260802:pact_full_pipeline_runner_v1/v31_common.py | grep -E "VERSION|ARTIFACT_VERSION"
VERSION = "3.1.3"
ARTIFACT_VERSION = VERSION

# 4c — inventory covers all find hits (reconcile with task 2.1 find output)
$ find . -type f -name "v31_*" -o -name "pact_translate_v3.py" -o -name "run_full_pipeline*.ps1" | sort | diff - <(grep -E "^\| " openspec/changes/v3-deprecation-plan/inventory.md | cut -d'|' -f2 | head)
# (manual reconcile — no gap)

# 4d — Phase0C harness imports only measurement helper, never legacy translator
$ grep -R "import v4_measurement_harness" pact_full_pipeline_runner_v1/v4_phase0c_baseline.py
import v4_measurement_harness as h0a
$ grep -R "pact_translate_v3" pact_full_pipeline_runner_v1/v4_phase0c_baseline.py | wc -l
0
```

## 5. Residual risks

- Inventory misclassification of a recovery file as removable → loss of rollback: mitigated by Gate G2 tag check + revert path (`archive/v3-main-20260802`).
- Benchmark report loss if `docs/audits/` pruned: mitigated by `historical` bucket (permanent) + Gate G2 denial for that path.
- Launcher confusion on `RT` (`run_full_pipeline.ps1` vs `run_full_pipeline_v31.ps1` vs `v4_book_run.py`): mitigated by explicit table + README navigation.
