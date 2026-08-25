## Context

`v4_phase12_strict_runner.py` is the sole production strict-run driver (see `DECISIONS.md` 2026-08-01: draft runner archived, strict is production). Current `main` (whole-chapter v4.1 + v41-runtime-efficiency) fixes 5 HIGH correctness issues and leaves the runner at 4987 lines with interleaved concerns:

- Public entry points: `run_chapter_strict` (chunk path) and `_run_whole_chapter_strict` / `_run_whole_chapter_strict_impl` (whole-chapter path), selected in `run_chapter_strict` via `cfg.whole_chapter`.
- Journal/resume: append-only `journal.ndjson` (schema `pact-v4-strict-chapter-trial-journal/v2`) is the resume source of truth; `prior_entries = _load_journal` replayed at start, identity-checked (`chunk_plan_hash`, `config_identity`, `backend_identity_hashes`), then incremental `journal_file.write/flush` per chunk/whole-chapter entry.
- State reconstruction: `selected_text_by_chunk`, `selected_role_counts`, `final_text_by_pid`, `selection_records`, `generation_outcomes` reconstructed from journal + persisted sidecar `selection_meta.json` / `generation_outcomes.json` (merge helpers preserve `committed=True` vs `quarantine_reason` linkage).
- Chunk vs whole-chapter: `ChunkPlanArtifact.create(snapshot, plans)` → `chunk_plan_payload` annotated `mode=whole-chapter-derived` when `cfg.whole_chapter`; `WholeChapterPidMap.derive(chunk_plan, snapshot)` becomes PID order source of truth, but `chunk_plan.json` kept for ownership / audit slicing. Generation differs: per-chunk loop (`chunk_started/chunk_done` via `PhaseProgressWriter`, risk pre-screen `_risk_for_chunk`, `selection`, `left_ru_for_chunk`) vs single `generate_whole_chapter` call with `wc_generation_started/retry_attempt/generation_done/wc_validated` + `_validate_whole_chapter_generation_record`.
- Audit/repair/formatting: Steps 6/7/8 (`_run_step6_audit`, `_run_step7_repair`, `_run_quarantined_retry_cycle`, Phase 5 formatting) — chunked audit (`run_chapter_audit` iterates `chunk_plan.chunks` even in whole-chapter runs), `b3_audit_repair` injection, `repair_cache` / `formatting_report` / `quarantined_retry` paths, all gated by `cfg.run_audit` / `skip_audit` identity.
- Persistent artifacts inventory (all under `cfg.out_dir`): `chunk_plan.json`, `whole_chapter_pid_map.json`, `journal.ndjson`, `generation_outcomes.json`, `selection_meta.json`, `selection_results.json`, `translations_raw.json`, `translations.json`, `translations_repaired.json` / `translations_final.json`, `strict_chapter_trial_record.json`, `audit_cache_b3.json` / `audit_findings`, `b2_handoff.json`, `repair_cache.json` / `repair_report.json`, `formatting_report.json`, `quarantined_retry.json`, `phase_progress.ndjson`, `usage.ndjson` (via `UsageRecordWriter`), `whole_chapter_reasoning.txt` (+ `retry{N}` variants).
- Identity/determinism: `StrictRunConfig.to_config_artifact` builds the frozen config identity; `build_source_artifact` / `build_snapshot` + `ChunkPlan` hash + `config_identity` + `backend_identity_hash` + `candidate_ids` + `gate` trace form resume/cache foreign-identity rejections (`Foreign identity:` / `Data loss:` errors). `phase_progress.ndjson` is diagnostics-only, never read by pipeline.
- Callers: `pact_full_pipeline_runner_v1/v4_phase12_strict_run.py:main` → `run_chapter_strict`; `pact_full_pipeline_runner_v1/v4_book_run.py` (per-chapter promotion). Tests: `tests/pact_v4/pipeline/test_v4_phase12_strict_runner*.py` (main, b3, remote, repair, formatting, retry, whole_chapter, translations_final), `test_v4_phase_progress_*`, `test_v4_usage_record`, `_smoke_c3.py`, `audit/chunked_audit` unit tests.

Constraints: any modularization must preserve file formats, field names, flush semantics, identity hashes, and deterministic selection/retry; `phase_progress` stays write-only; resume must remain byte-compatible.

## Goals / Non-Goals

**Goals:**
- Freeze a planning contract for strict-runner modularization whose Phase 1 is read-only characterization (contract map) and candidate extraction identification, with no code change under this proposal.
- Enumerate every persistent artifact, journal/resume invariant, identity field, flow branch (chunk vs whole-chapter), and audit/repair/formatting hook so later splits can be checked mechanically.
- Define explicit extraction candidates that are pure internal helpers (no pipeline-observable behavior) and require separate approval before implementation.
- Keep all four artifacts (`proposal.md`, `design.md`, `tasks.md`, `contract-map.md`) passing `openspec validate --strict`.

**Non-Goals:**
- Implementing any extraction, file split, signature change, or pipeline behavior/config/model-routing/fidelity modification under this change — prohibited without later owner approval.
- Changing artifact schemas, journal format, identity composition, audit/repair logic, formatting rules, or determinism guarantees.
- Adding new capabilities, specs deltas, or runtime config.
- Archiving or merging; production pipeline stays manual on `RT`.

## Decisions

- **Planning-only change with `skip_specs: true`:** No spec deltas; the change is a doc contract. Alternative `specs/` delta rejected — there is no requirement change to specify, only to freeze.
- **One contract map file distinct from `investigation.md`:** `contract-map.md` is the deliverable enumerating surfaces; `investigation.md` (if present) is supporting grep. Keeps `openspec validate` focused. Rejected: embedding map in `design.md` — too large, harms reviewability.
- **Phase 1 = characterization + candidate identification, no implementation:** Prevents incremental refactor drift. Candidates are listed as `Not Implemented` with verification gates (`--strict` validate, diff empty, guard checks) rather than code.
- **Explicit prohibition list in proposal/design/tasks:** Pipeline behavioral, config, model-routing, fidelity, and refactoring are out of scope for this change and require a follow-up change with owner approval. Alternative "allow small safe extractions" rejected — even pure helpers need isolated review.
- **Module name `v4_phase12_strict_runner` preserved verbatim:** Contract map references `pact_v4/pipeline/v4_phase12_strict_runner.py` (underscores) while change slug is `v4-phase12-strict-runner` (hyphens, OpenSpec constraint). No rename.

## Risks / Trade-offs

- **Risk: Map drifts from code if not regenerated** → Mitigation: Tasks require grep counts, line references, and artifact path enumeration pinned to current `main`; future Phases must re-run map validation before split.
- **Risk: Candidate helpers look pure but have hidden identity coupling** → Mitigation: Candidates flagged as `pure-internal only if no config_identity/chunk_plan_hash/journal` touch; any candidate touching those is deferred to Phase 2 design review.
- **Risk: Scope creep into "quick refactor"** → Mitigation: Prohibitions stated in three places (proposal, design, tasks); `pact-workspace-guard` + `pact-git-hygiene` enforce empty diff for code files; CI `openspec validate` is gate.
- **Trade-off: `skip_specs:true` vs. desiring formal spec** → Accepted: A planning contract does not warrant capability spec; formal deltas will appear in Phase 2 implementation change.

## Migration Plan

- No migration. This change commits docs only. Later Phase 2 implementation change (still owner-approved) will propose actual splits, gated by re-validating this contract map and running `tests/pact_v4/pipeline/test_v4_phase12_strict_runner*` green.

## Owner-approved future sequencing (2026-08-24)

This planning change remains docs-only. The owner authorizes preparation of **separate, implementation-specific OpenSpec changes** for these bounded future stages, in order:

1. characterization baseline tests for the frozen contracts;
2. verbatim pure utility extraction (`_atomic_write_json`, `_left_context_hash`, `_pid_diffs`, simple artifact-path helpers);
3. pure validation/view helpers (`_gates_passed`, `_pick_best_variant`, candidate/audit mapping);
4. whole-chapter validation boundary (`_validate_whole_chapter_generation_record`, validation flags, reasoning persistence), preserving identical retry/event/error behavior.

Every stage still requires its own approved implementation scope, isolated review, and relevant strict-runner tests. **Phase 5** (resume sidecars, identity-bearing cache/merge helpers, atomic/flush ordering, audit/repair cache loading) is explicitly blocked pending a separate high-risk design review and owner approval. The chunk loop, whole-chapter orchestrator, B3 wiring, `StrictRunConfig`, and journal writer/loader remain outside the authorized stages.

## Open Questions

- Exact module boundaries and test fixtures for each owner-authorized future stage will be proposed in its own implementation OpenSpec; this planning change implements none.
- `MAX_TOKENS_CEILING` style follow-ups stay in `v41-runtime-efficiency`; no overlap.
