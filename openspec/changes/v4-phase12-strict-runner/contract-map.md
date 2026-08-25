# Contract Map — `v4_phase12_strict_runner` (pact_v4/pipeline/v4_phase12_strict_runner.py)

**Change:** `v4-phase12-strict-runner` (module `v4_phase12_strict_runner`, change slug `v4-phase12-strict-runner`)
**File:** `pact_v4/pipeline/v4_phase12_strict_runner.py` — 4987 lines, ~245k (commit `af120ae` baseline; `HEAD` 263398f adds only README nav)
**Scope:** Phase 1 characterization only — read-only inventory. No behavioral, config, model-routing, fidelity, or refactoring changes under this change without later approval.
**Date:** 2026-08-24
**Guard:** `skip_specs: true` (planning-only); `pact-workspace-guard` / `pact-risk-test` Low / `pact-git-hygiene` enforce docs-only diff.

---

## 1. Journal / Resume

| Concern | Location | Contract |
|---------|----------|----------|
| **Journal schema** | `v4_phase12_strict_runner.py:226` `JOURNAL_SCHEMA = "pact-v4-strict-chapter-trial-journal/v2"`; `JournalEntry:692` | One JSON line per chunk (chunked) or single whole-chapter entry. Fields: `chunk_id`, `chunk_plan_hash`, `config_identity`, `candidate_ids`, gate `decision_trace`, `selected_candidate_id/selected_role` or `quarantine/needs_synthesis`. Schema v2 carries `quarantine_reason`/`synthesis_reason`; v1 omits (`:1202` sidecar merge). |
| **Append-only flush** | `:2845`, `:2932`, `:3005`, `:3164` `journal_file.write(json.dumps(entry.to_json()) + "\n"); journal_file.flush()` | Per-entry flush; crash leaves clean prefix, ` _load_journal` skips empty/malformed trailing line. Writer opened `open(journal_path, "a")` at `:2845`. |
| **Load** | `:_load_journal:754` | `if not exists: []`; `read_text.splitlines()` → `json.loads` per line; used only for resume. |
| **Resume — chunk path** | `:2690` `journal_path = cfg.out_dir / "journal.ndjson"` → `prior_entries = _load_journal` → `:2706` identity check `entry.get("chunk_plan_hash") != chunk_plan.plan_hash or config_identity !=` → raise `Foreign identity: journal entry for ... against a stale journal.`; then `:2717` `LOG.info("Resuming ... from chunk index %d")`; reconstruction `:2728-2855` replays `selected_text_by_chunk`, `selection_records`, `generation_outcomes` via `_merge_selection_meta` / `_merge_generation_outcomes`. |
| **Resume — whole-chapter** | `:4159` `journal_path`; `:4170` `prior_entries`; `:4186-4264` single-entry validation: must be single `whole_chapter` entry, `chunk_id == WHOLE_CHAPTER_CHUNK_ID`, `config_identity` / `chunk_plan_hash` match else `Foreign identity:` / `Data loss: malformed whole-chapter journal entry`. |
| **Whole-chapter chunk id** | `:3696` `WHOLE_CHAPTER_CHUNK_ID = "whole_chapter"` | Single journal entry invariant; `:4215` comment "exactly ONE whole_chapter entry". Duplicate → `Data loss: whole-chapter resume journal must contain ...`. |
| **Result bookkeeping** | `StrictChapterRunResult:842-856` | `out_dir`, `translations_path`, `journal_path`, `record_path`, `resumed_from_index`, `counts`; `processed_count = len(_load_journal)` at `:3474`. |
| **Selection sidecar** | `:_merge_selection_meta:1190`, `:_merge_generation_outcomes:1323`, `:_generation_outcomes_path:1182` | Resume-safe merge: prior `selection_meta.json` / `generation_outcomes.json` merged with current-session stubs; `committed=True` vs `quarantine_reason` preserved without re-budgeting (`:780`). `chunk_plan_hash` / `config_identity` identity-checked in both loaders (`:1227`, `:1353`). |
| **Error taxonomy** | Throughout | `Foreign identity: selection_meta was written under a ...`, `Foreign identity: generation_outcomes ...`, `Data loss: journal says whole_chapter generation was ...` — all fatal, refuse to mix identities. |

**Readers outside runner:** None. `journal.ndjson` is never read by callers except for resume and tests. `StrictChapterRunResult` returned to CLI `v4_phase12_strict_run.py` for exit code.

---

## 2. Whole-Chapter vs Chunk Flows

### 2.1 Branching point

```python
# run_chapter_strict:2615-2670
source = build_source_artifact(...)
config = cfg.to_config_artifact(...)
chunk_plan = ChunkPlanArtifact.create(snapshot, tuple(plans))
chunk_plan_payload["mode"] = CHUNK_PLAN_MODE_WHOLE_CHAPTER  # if cfg.whole_chapter
whole_chapter_pid_map = WholeChapterPidMap.derive(chunk_plan, snapshot)  # §2.2
if cfg.whole_chapter:
    return _run_whole_chapter_strict(cfg, source, snapshot, chunk_plan, ...)
# else chunked loop:2680-3668
```

`StrictRunConfig.whole_chapter:343` (bool, default False) — part of identity (`to_config_artifact:500 whole_chapter`).

### 2.2 Whole-Chapter PID Map

- `from pact_v4.phase1.models: WholeChapterPidMap` imported `:106`.
- `WholeChapterPidMap.derive(chunk_plan, snapshot)` — ordered PID source of truth when whole-chapter; `chunk_plan.json` kept with `mode=whole-chapter-derived` annotation (`:2639`), `whole_chapter_pid_map.json` written at `:2638` (schema `pact-v4-whole-chapter-pid-map/v1` per DECISIONS.md 2026-08-10 W).
- `ChunkPlan` still persisted for `WholeChapterPidMap` ownership and audit slicing — cannot be deleted (see `v41-runtime-efficiency` investigation.md).

### 2.3 Chunk flow (per-chunk generation & selection)

- Loop: `for index, plan_chunk in enumerate(chunk_plan.chunks):` `:2846` — `PhaseProgressWriter.chunk_started(chunk_id)` → `left_context = _shared_runner_helpers._left_ru_for_chunk` (`:766`, left_context = previous selected translation or `()` if quarantined / first chunk) → `glossary_entries = _shared_runner_helpers._glossary_entries` / `_glossary_entries_for_chunk` filtered → `_risk_for_chunk` → cascade `select_candidate` → `record_selection` / `_serialize_generation_outcome` → `journal_file.write` + `selection_meta`/`generation_outcomes` incremental.
- Resources: `build_strict_lifecycle:635` yields `runtime`, `progress_writer`, `usage_writer`; closed via `_close_run_resources:2464`.

### 2.4 Whole-Chapter flow (single call, bounded retry)

- Impl: `_run_whole_chapter_strict_impl:4115` → `generate_whole_chapter` (one call per chapter, `cfg.whole_chapter` guard) with `max_attempts` / `reasoning_budget` (`runtime_config` backed). Progress: `PhaseProgressWriter.wc_generation_started(pid_count, reasoning_budget, model, max_attempts)` → `wc_retry_attempt(attempt, reason malformed|missing_pid|truncated|abort)` per retry → `wc_generation_done` → `wc_validated(json_ok,pids_ok,order_ok)`.
- Validation: `_validate_whole_chapter_generation_record:3802` (RV3 t_27de970d) enforces `chunk_id == whole_chapter`, `status`, `candidates` dict shape, `expected_roles`, linkage to journal `selected_candidate_id/selected_role`; raises `Data loss:` on mismatch.
- Reasoning: `_persist_whole_chapter_reasoning:3758` writes `whole_chapter_reasoning.txt` (attempt 0) or `whole_chapter_retry{N}_reasoning.txt`; does not affect `whole_chapter_pid_map`/`wc_validated`/cache/resume.

### 2.5 Audit still chunked even in WC generation

`run_chapter_audit` iterates `for chunk in chunk_plan.chunks: for detector in (qwen, gemma)` even when generation was whole-chapter (B3 reads `chunk_plan.chunks` to split chapter into audit chunks; `audit_journal.ndjson` not used). The per-chunk audit tables in `v4_phase_progress.py` are **not legacy** — see `v41-runtime-efficiency` investigation.md.

---

## 3. Audit / Repair / Formatting (B3 / Phase 4 / quarantined retry)

### 3.1 Step 6 — Audit (B3)

- `_run_step6_audit:1433` signature `(out_dir, chunk_plan, source, snapshot, config, model_router, ...)` → calls `run_chapter_audit` (phase3) + `render_entity_context_block` (`b3_audit_repair.py`).
- Reads: `chunk_plan`, `source`, `snapshot`, `config`, candidates (selected map). Writes: `audit_cache_b3.json` (`AUDIT_CACHE_SCHEMA v1`, `_audit_cache_path:1170`), `audit_findings` (`_audit_findings_path:1174`, via `FindingStore`), `b2_handoff.json` (`_b2_handoff_path:1178` — Step 6's B2 handoff sidecar, `v1` journal does not persist it `:2767`), emits `audit_unit_started/done` + `audit_done` via `PhaseProgressWriter`.
- Identity: `_load_audit_cache:1377` checks `chunk_plan_hash`, `config_identity`, `backend_identity_hashes`, `candidate_ids` gate tuple (`:1379-1420`); foreign identity → refuse. Coverage check: `uncovered_chunks = len(chunk_plan.chunks) - len(candidates)` `:1574`.
- Gating: `cfg.run_audit:344`, `entity_context_enabled`, audit budgets (`audit_max_input_tokens/max_tokens/overlap_tokens`, `audit_reasoning_budget`, `audit_transport_*`), prompt/extractor versions (`audit_prompt_version`, `HARNESS_VERSION`, `EXTRACTOR_VERSION`) all participate in `to_config_artifact` identity (`:540-560`).

### 3.2 Step 7 — Repair + Re-audit (Phase 4)

- `_run_step7_repair:1753` → selective repair (`repair/selective_repair.py`, `pact_v4/repair/*`) with repair budgets (`audit_repair_*` fields: `repair_findings_cap`, `microbatch_trigger`, `context_window`, `reaudit_neighbour_window`, `reaudit_max_input_tokens`, `repair_max_tokens`, `repair_reasoning`, etc. — all identity-bearing `:391-442`).
- Reads: `chunk_plan`, `source`, `snapshot`, candidates, `AuditCache`. Writes: `repair_cache.json` (`_repair_cache_path:1596`), `repair_report.json` (`_repair_report_path:1600`, final translation normalized via `_normalize_final_markup:1632` clean tags), `phase4` provenance (`_build_phase4_provenance:1721` → artifact `pact-v4-phase4/v1`), `translations_repaired.json` (+ delta chain), re-audit units (`reaudit_unit_started/done`).
- Merge: `_load_repair_cache:1653` identity checks `chunk_plan_hash` etc. (`:1708`); `_build_phase4_provenance` input includes `chunk_plan_hash` / `config_identity`.
- Events: `repair_round_started(round_number)`, `region_started/done(chunk_id, repair_id, target_pids, action, committed, reason)`, `reaudit_unit_started/done`, `repair_done(rounds)`.

### 3.3 Quarantined retry cycle

- `_run_quarantined_retry_cycle:1992` + `_load_prior_quarantined_retries:1914`, `_quarantined_retry_path:1910` → reads `chunk_plan`, retries quarantined chunks with repair context windows; `chunk_plan_hash` identity-checked (`:1954`).
- Outcome recorded in `quarantined_retry.json` identity-checked; failure path feeds `translations_final.json` fallback.

### 3.4 Phase 5 / Formatting terminal

- `_load_repair_cache` + formatting (`pact_v4/phase5/formatting.py:150`) → `formatting_report.json` (`_formatting_report_path:1649`) → terminal artifacts `translations_final.json` / `translations.json` (`translations_path_exists:3673`), `strict_chapter_trial_record.json` (`:3479-3633` strict trial record build with `chunk_plan_hash`, `config_identity`, `gate` provenance), `formatting_done(incidents, blocking)`, `terminal(status complete|accepted_degraded|failed)`.

---

## 4. Persistent Artifacts (all under `cfg.out_dir`)

| Artifact | Writer | Reader on resume | Schema / version | Call site |
|----------|--------|------------------|------------------|-----------|
| `chunk_plan.json` | `ChunkPlanArtifact.create → to_payload` (`:2630`) | `_load_journal` identity check, `WholeChapterPidMap.derive` | `ChunkPlan` payload + `mode=whole-chapter-derived` when WC | `:2641-2643` |
| `whole_chapter_pid_map.json` | `WholeChapterPidMap.derive` | WC resume order truth | `pact-v4-whole-chapter-pid-map/v1` | `:2638` |
| `journal.ndjson` | Per-entry `journal_file.write` | `_load_journal:754` → resume state | `pact-v4-strict-chapter-trial-journal/v2` (`:226`) | `:2691, :4159` |
| `generation_outcomes.json` | `_merge_generation_outcomes:1323` + `_generation_outcomes_path:1182` | `_coalesce_generation_outcome_records:1250` etc. | `chunk_plan_hash` + `config_identity` identities | `:1324` |
| `selection_meta.json` | `_merge_selection_meta:1190` | `_merge_selection_meta` (sidecar for `committed/quarantine_reason`) | `schema selection_meta` + chunk_plan_hash | `:1186` |
| `selection_results.json` | Selection loop | Offline validation (`test_b9_offline_validation.py`) | v1 `not_applicable` for WC | `:2665` comment |
| `translations_raw.json` | Per-candidate generation | None (raw dump) | — | — |
| `translations.json` | Terminal merge (Phase 5) | Resume reads back prior `translations.json` for `final_text_by_pid` reconstruction (`:2728`) | Atomic `_atomic_write_json:721` | `:2692→:3633` |
| `translations_repaired.json` | Repair cache final | Re-audit input | `_normalize_final_markup` normalized | `:1604` |
| `translations_final.json` | Formatting | Final CLI output | Terminal | — |
| `strict_chapter_trial_record.json` | Record builder `:3479` | `v4_book_run` promotion | Includes `chunk_plan_hash`, `config_identity`, `chunks_total`, gate trace | `:3633` |
| `audit_cache_b3.json` | `_run_step6_audit` | `_load_audit_cache:1377` | `pact-v4-strict-audit-cache/v1` | `:1170` |
| `audit_findings` | `FindingStore` | `AuditCache` | `pact-v4-strict-audit-findings/v1` | `:1174` |
| `b2_handoff.json` | Step 6 sidecar | Not in journal v1 (`:2767`) | — | `:1178` |
| `repair_cache.json` | `_run_step7_repair` | `_load_repair_cache:1653` | includes `chunk_plan_hash` | `:1596` |
| `repair_report.json` | Repair | `_load_repair_report_final_translation:1604` | — | `:1600` |
| `formatting_report.json` | Phase 5 | `_formatting_report_path:1649` | `incidents`, `blocking` | `:1649` |
| `quarantined_retry.json` | `_run_quarantined_retry_cycle` | `_load_prior_quarantined_retries:1914` | `chunk_plan_hash` checked | `:1910` |
| `phase_progress.ndjson` | `PhaseProgressWriter` (`phase_progress.py`) | **Never read by pipeline** (diagnostics-only `:6` doc) | `pact-v4-phase-progress/ndjson/v1` | — |
| `usage.ndjson` | `UsageRecordWriter` (`usage_record.py`) | Never read by pipeline; read by monitor `v4_usage.py` | — | — |
| `whole_chapter_reasoning.txt` (+ `retry{N}`) | `_persist_whole_chapter_reasoning:3758` | None | Reasoning dump, no identity effect | `:3784` |

All `_*.json` writes via `_atomic_write_json:721` where terminal (rename+fsync contract elsewhere).

---

## 5. Identity / Determinism

### 5.1 Config identity source

- `StrictRunConfig:269` fields (`whole_chapter`, `run_audit`, `glossary_budget_policy_version`, `audit_*`, `audit_repair_*`, `generation_*`, `model_profile` via `StrictBackendConfig`) all participate in `to_config_artifact:483 → build_config_artifact:484` and thus in `config.config_identity` (`:515-545` audit block, `:540 audit.run`, `:541 base_delay`, `:551 repair_findings_cap`, `:558 repair_context_window`, `:565 repair_context_window_by_category`, etc.). Any flip invalidates `journal`, `selection_meta`, `generation_outcomes`, `audit_cache`, `repair_cache` on resume (foreign identity).
- Comments explicitly: `MUST be part of the config identity` (`:519`), `part of to_config_artifact, so flipping any ... invalidates` (`:463`).

### 5.2 Source / snapshot / chunk plan / backend identity

- `build_source_artifact:211`, `build_snapshot:207`, `ChunkPlanArtifact.create:2630` → `chunk_plan.plan_hash` / `snapshot.snapshot_hash`; `build_strict_lifecycle` → `runtime` `backend_identity_hash` / `backend.config_profile_name()`. All carried in every journal entry (`:37` list) and checked on resume (`:2706`, `:4186`).

### 5.3 Determinism

- `select_candidate` cascade (`phase2/cascade.py`) deterministic per chunk given same candidates/decision_trace; `left_context_hash:715` hashes `left_context`; `phase_progress` explicitly write-only; resume reconstructs `final_text_by_pid` from prior `translations.json` + journal without re-budgeting (`:779-783`).
- Gate trace: `JournalEntry` + `generation_outcomes.json` `decision_trace` gate names (`:904 _gates_passed`, `:908 _pick_best_variant`) logged verbatim for provenance.

### 5.4 Foreign identity / Data loss taxonomy

| Prefix | Meaning | Example |
|--------|---------|---------|
| `Foreign identity:` | Persisted artifact/journal written under different `schema/chapter/snapshot/chunk_plan_hash/config_identity` — mixing identities refused | `:2710`, `:4186`, `selection_meta schema ... was written under a ...`, `generation_outcomes ... was written under ...`, `audit_cache ... written under ...` |
| `Data loss:` | Journal/candidate mismatch, truncated/corrupt file, tampered translations (`:883`), single-entry WC invariant violation | `:3996 journal says whole_chapter generation was ...`, `:3854 generation record is not an object`, whole-chapter validation `:3802-4050` |

---

## 6. Callers / Tests

### 6.1 Production callers

| Caller | Path | Usage |
|--------|------|-------|
| CLI strict runner | `pact_full_pipeline_runner_v1/v4_phase12_strict_run.py:46 import run_chapter_strict` → `:934/:988 run_chapter_strict(cfg=..., out_dir)` | Wires CLI args → `StrictRunConfig` → `run_chapter_strict` → exit code; reasoning writer `open_reasoning_writer` injected. |
| Book run orchestrator | `pact_full_pipeline_runner_v1/v4_book_run.py:5,165` `from v4_phase12_strict_run import main as strict_main` | Per-chapter loop, `MemoryManager.promote` after each chapter; does not call `run_chapter_strict` directly but via CLI shim. |
| Monitor — phase progress | `pact_full_pipeline_runner_v1/v4_phase_progress.py:6` reads `out_dir/phase_progress.ndjson` (diagnostics) + `journal.ndjson`/`chunk_plan.json`/`usage.ndjson` snapshot (v41-runtime-efficiency) | Never writes, never gates. |
| Monitor — usage | `pact_full_pipeline_runner_v1/v4_usage.py:6` reads `out_dir/usage.ndjson` written by `UsageRecordWriter` in runner | Cost/usage reporting. |
| Smoke | `_smoke_c3.py:43` + `pact_full_pipeline_runner_v1/self_test_v4_phase12_strict_run.py` | Manual smoke of `run_chapter_strict` with real chapter gate bench. |

### 6.2 Test suites (all under `tests/pact_v4/pipeline/`)

| Suite | Count hint | Focus |
|-------|------------|-------|
| `test_v4_phase12_strict_runner.py` (88029 lines) | ~50 tests | Core chunked flow, resume, foreign-identity, selection, gate trace, lifecycle usage_writer, quarantine, `_last_worker_run*` edge, `run_chapter_strict` close semantics. |
| `test_v4_phase12_strict_runner_b3.py` (184227) | ~40 tests | B3 audit/repair integration, `run_audit` gating, chunked audit slicing, `b2_handoff`, repair cache identity, `v4_phase12_strict_run` CLI wiring (imports `strict_run_mod`). |
| `test_v4_phase12_strict_runner_remote.py` (20782) | ~8 | Remote `HttpQwenEvaluator` / `BackendRoleAdapters` vs local `ModelRouter` parity. |
| `test_v4_phase12_strict_runner_repair.py` (19554) | ~10 | Selective repair + formatting terminal, translation rewrites. |
| `test_v4_phase12_strict_runner_retry.py` (22215) | ~10 | Quarantined retry cycle, `quarantined_retry.json` handling. |
| `test_v4_phase12_strict_runner_formatting.py` (16162) | ~8 | Phase 5 formatting (`formatting_report.json` → terminal). |
| `test_v4_phase12_strict_runner_whole_chapter.py` (84527) | ~30 | WC single-call retry, `_validate_whole_chapter_generation_record`, WC resume (single-entry invariant), WC audit still chunked, `WholeChapterPidMap` ownership. |
| `test_v4_phase12_strict_runner_translations_final.py` (18633) | ~8 | Final `translations.json` / `translations_final.json` contracts. |
| `test_v4_phase_progress_monitor_v2.py` (82550) + `monitor_whole_chapter` | — | `PhaseProgressWriter` event vocabulary (`run_started`, `chunk_started/done`, `wc_*`, `audit_unit_*`, `repair_*`, `terminal`) and snapshot rendering. |
| `test_v4_phase_progress_writer.py` (12637), `test_v4_phase_progress_tracker.py` (16672), `test_v4_usage_record.py` (35873), `test_b3_kill_safe_*` (63215+21268), `test_glossary_budgeter` (13380) | — | Writer resilience (disabled on write failure), progress diagnostics, usage writer flush, glossary budgeter filtering. |
| `test_v4_phase12_strict_run_reasoning.py` (33324) | — | Reasoning/backend boundary (`whole_chapter_reasoning.txt` writer). |
| Cross-module | `test_b9_book_run_integration`, `test_bm_book_memory_integration`, `test_a2_prompt_and_snapshots` | Book-run promotion reads `strict_chapter_trial_record.json` with `chunk_plan_hash`/`config_identity`; prompt snapshot identity. |

`grep -rn "run_chapter_strict" --include="*.py" | wc -l` ≈ 90 hits (pact_v4 + tests + smoke). The inventory above captures all **production** sites; remaining hits are test assertions.

---

## 7. Candidate Pure-Internal Extractions (Phase 1 — Not Implemented)

> **Prohibition:** Under this change (`v4-phase12-strict-runner`) **no** extraction, file move, signature change, or refactoring of the below is implemented. Listing is planning-only; each requires a follow-up owner-approved change with isolated diff, `pact-risk-test`, and full green `tests/pact_v4/pipeline/test_v4_phase12_strict_runner*`. Pipeline behavioral, config, model-routing, or fidelity changes are **forbidden** even as "incidental" to extraction.

| # | Candidate | Lines | Purity rationale (no identity or pipeline effect) | Status |
|---|-----------|-------|---------------------------------------------------|--------|
| C1 | `_atomic_write_json(path, payload)` | `:721` | Pure `json.dumps` + atomic rename; called only for terminal artifacts, no identity hash input. Verbatim move safe. | Not Implemented |
| C2 | `_left_context_hash(left_context)` | `:715` | Hashes `Tuple[Tuple[str,str]]` for cache key; deterministic, no I/O, not identity-bearing beyond dedup. | Not Implemented |
| C3 | `_pid_diffs(...)` | `:735` | PID-level diff helper for reporting; diagnostics only. | Not Implemented |
| C4 | Helpers `_gates_passed` / `_pick_best_variant` / `_candidate_from_generation_record` / `_audit_candidate_map` / `_fill_audit_status` | `:904`, `:908`, `:930`, `:970`, `:1134` | Pure selection/audit view helpers; no artifact write. | Not Implemented |
| C5 | Path helpers `_audit_cache_path`, `_audit_findings_path`, `_b2_handoff_path`, `_generation_outcomes_path`, `_selection_meta_path`, `_repair_cache_path`, `_repair_report_path`, `_formatting_report_path`, `_quarantined_retry_path` | `:1170-1910` | `out_dir / "name.json"` only; trivial constants. | Not Implemented |
| C6 | Merge helpers `_merge_selection_meta`, `_coalesce_generation_outcome_records`, `_coalesce_lazy_record_into_primary`, `_merge_generation_outcomes`, `_load_audit_cache` / `_load_repair_cache` / `_load_prior_quarantined_retries` | `:1190-1954` | Currently identity-checked; moving the **merge** combinator alone is safe *only* if the `chunk_plan_hash/config_identity` guard stays at call site — deferred until Phase 2 HHI review. | Not Implemented — requires per-function HHI |
| C7 | Whole-chapter helpers `_wc_generation_model`, `_wc_validation_flags`, `_persist_whole_chapter_reasoning:3758`, `_validate_whole_chapter_generation_record:3802`, `_normalize_final_markup:1632`, `_build_phase4_provenance:1721` | `:3705-3802` | Pure validation/markup/provenance assembly; `_validate_*` is gate (must keep Data-loss throws identical). | Not Implemented |
| C8 | Resource closer `_close_run_resources:2464` | `:2464` | `try close` of `runtime/progress_writer/usage_writer`; no logic. | Not Implemented |
| C9 | Shared helper `_left_ru_for_chunk`, `_risk_for_chunk`, `_serialize_generation_outcome`, `_record_selection`, `_glossary_entries*` | `pact_v4/pipeline/_shared_runner_helpers.py` | Already extracted; referenced as pattern for pure boundary. | Already separate |

**Deferred / Not candidates in Phase 1:** Any function that touches `chunk_plan_hash`, `config_identity`, `backend_identity_hash`, `journal.ndjson` flush ordering, `StrictRunConfig.to_config_artifact` composition, or `b3_audit_repair` wiring — e.g., the `run_chapter_strict` loop body or `_run_whole_chapter_strict_impl` — is **not** a pure-internal candidate until contract tests pin it.

---

## 8. Verification (Phase 1 planning-only)

- `openspec validate --changes --json` → `v4-phase12-strict-runner` PASS (`skip_specs` info), `v41-runtime-efficiency` PASS.
- `bash .pi/skills/pact-workspace-guard/scripts/check.sh` PASS (isolated worktree `.../strict-runner-contract-map`, branch `openspec/strict-runner-contract-map`).
- `bash .pi/skills/pact-risk-test/scripts/check.sh` Low (no code diff).
- `bash .pi/skills/pact-git-hygiene/scripts/check.sh` PASS (focused diff: only `openspec/changes/v4-phase12-strict-runner/**`).
- `git diff --name-only origin/main...HEAD` == `openspec/changes/v4-phase12-strict-runner/proposal.md`, `design.md`, `tasks.md`, `contract-map.md`, `.openspec.yaml` (no `pact_v4/**`, no `pact_full_pipeline_runner_v1/**`).

---

## 9. Out of Scope (explicit)

- Implementing any candidate (Table 7) or splitting `v4_phase12_strict_runner.py`.
- Changing journal, artifact schemas, config identity, model routing, selection gates, prompts, glossary, audit/repair/formatting logic, or determinism.
- Archiving, merging, or running production pipeline.
