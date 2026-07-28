# Pact Ensemble Translator v3.1.3 (RC1) — Independent read-only audit

**Baseline:** `release/v3.1.3-rc1-pr21` @ `2dfede40…` (snapshot; **not** merged — current `main` is `b3c6add…` = v3.1.2j with v3.1.3 reverted).
**Scope:** code-first audit of the bundle. No patch written. Documents treated as advisory and verified against code.
**Offline checks run (all pass):** `self_test_v31.py`, `self_test_stage_protocol_v31.py`, `self_test_chapter_resolver.py`, `test_formatting_integrity.py` (6 tests). Python 3.10.

Status legend: **CONFIRMED** = proven in code; **LIKELY** = strong risk, needs a run to prove impact; **QUESTION** = needs real-run data or missing evidence.

---

## Findings

### F1 — Terminal-state monotonicity is implemented in dead code; the live gate can flip quarantined → complete
- **Severity:** MEDIUM
- **Status:** CONFIRMED (dead code) / LIKELY (live flip on redo/resume)
- **Affected files:** `v31_final_lifecycle.py` (`terminal_status`), `v31_finalize_quality.py` (`main`), `run_full_pipeline_v31.ps1` (quarantine check, lines 725–731)
- **Failure lifecycle:** The quality contract states "quarantine is monotonic … a later stale artifact can never turn it into complete." That rule lives only in `terminal_status()`, which is invoked **only** from `self_test_v31.py` — never from the pipeline. The live terminal decision is made by `v31_finalize_quality.main`, which recomputes status purely from current artifacts and overwrites `v31_quality_gate.json` each run. It never reads a prior `state.json`/gate. On the complete branch it writes `v31_quality_gate.json=complete` but does not even clear a stale `state.json=quarantined`. The runner's gate check reads only the freshly-overwritten `v31_quality_gate.json`. So a chapter quarantined on run N, then re-run (e.g. `--redo-quality`, or resume after interruption), is promoted to `complete` if the recompute looks clean — exactly the transition the contract forbids.
- **Evidence:** `grep terminal_status` → self-tests only; `finalize_quality` has no read of `state.json` (only a write at line 249); runner quarantine scan keys off `v31_quality_gate.json.status`.
- **Existing coverage:** `self_test_v31.py` asserts `terminal_status(..., prior_status="quarantined") == "quarantined"` — green, but on an unused function; gives false confidence.
- **Missing regression test:** integration test that a pre-existing quarantined terminal state survives a re-run of `finalize_quality`.
- **Disposition:** Wire `terminal_status` (or an equivalent prior-state read) into `finalize_quality`, **or** amend the contract to drop the monotonicity claim. Mandatory before the contract can be cited as satisfied. Low practical exposure on clean single runs.

### F2 — `VERSION = "3.1.2j"` in a v3.1.3 build breaks the monitor and voids "version identity" in the cache spec
- **Severity:** HIGH
- **Status:** CONFIRMED
- **Affected files:** `v31_common.py:26` (`VERSION="3.1.2j"`); every Python stage stamps `"version": VERSION` (`v31_audit.py`, `v31_source_analysis.py`, `v31_adjudicate.py`, `v31_finalize_*`, etc.); `monitor_pipeline_v31.ps1` (73, 90–91, 108, 120–121); `run_full_pipeline_v31.ps1` (`$RunnerVersion='3.1.3-03'`, `$ensemble.version='3.1.3'`); `v31_stage_protocol.valid_aggregate`
- **Failure lifecycle:** Real aggregates are stamped `"3.1.2j"`. The monitor derives run version from `monitor_state.runner_version` (`3.1.3-03`) or config `ensemble_v31.version` (`3.1.3`), then flags every artifact whose `version` ≠ run version as mixed-version (line 91). Because `3.1.2j` ≠ `3.1.3*` for **every** stamped artifact, `$mixed` is non-empty on every healthy run → `$blocked=$true` (108) → monitor prints "Mixed-version artifacts: …" and "Resume: BLOCKED" even for a perfectly healthy pipeline. The monitor therefore does not match reality (priority #9). Separately, `valid_aggregate` only checks `version` is a *string*, never a specific value, so the version stamp gives **no** real cache-identity protection — contradicting `CACHE_IDENTITY_SPEC`/`QUALITY_CONTRACT` claims that mixed-version artifacts are rejected.
- **Evidence:** `grep -n VERSION v31_common.py`; stage writers stamp `VERSION`; monitor comparison at line 91; `valid_aggregate` (stage_protocol lines 36–42).
- **Existing coverage:** `self_test_monitor_v31.ps1` hand-builds artifacts with `version='3.1.3'` and injects a synthetic `3.1.2` file to test the "mixed" path — so it validates the branch but **never exercises the real `v31_common.VERSION`**; the true mismatch is invisible to the suite.
- **Missing regression test:** monitor test that consumes artifacts stamped with the actual `v31_common.VERSION` and asserts a clean run reports `Resume: READY` / no mixed-version; a test asserting the runtime `VERSION` equals the release version.
- **Disposition:** Mandatory. Bump `VERSION` to a single shared `3.1.3` identity (decouple from the `3.1.3-NN` build tag used by monitor comparison), and decide whether `valid_aggregate` should enforce an expected version rather than "is a string."

### F3 — Multi-chapter final pass aborts: one chapter's ledger is reused for all selected chapters
- **Severity:** HIGH (directly affects the planned 2–3-chapter runs)
- **Status:** CONFIRMED
- **Affected files:** `run_full_pipeline_v31.ps1` (712–716, 722), `v31_audit.py` (410–419)
- **Failure lifecycle:** For a single invocation spanning multiple chapters (`Start ≠ End`), the runner picks the **first** `v31_final_changed_pid_ledger.json` (`Get-ChildItem … Select -First 1`, line 715) and passes that one file as `--pids-file` to `Run-AuditPass 'final'` for **every** selected chapter. In `v31_audit`, `requested_set` (chapter-1 PIDs) minus chapter-2's manifest PIDs is non-empty → `raise ValueError("Final ledger contains unknown PIDs …")` (line 418). The final pass hard-fails on the second chapter. (If PID namespaces ever overlapped, it would instead silently mis-scope final verification to the wrong PIDs — worse.) The runner comment concedes this: "runner expands one ledger only because selected chapter runs are currently one chapter."
- **Evidence:** runner lines 712–716; `v31_audit` lines 410–419.
- **Existing coverage:** none — no multi-chapter integration test.
- **Missing regression test:** two-chapter run asserting each chapter's final pass uses its own ledger.
- **Disposition:** For the planned test runs, invoke **one chapter per run** (`-Start N -End N`). Mandatory fix before any multi-chapter invocation: resolve the ledger per chapter inside the final pass.

### F4 — Cache identity has no input-content fingerprint; edited source/config is silently reused without `--redo`/`--reset`
- **Severity:** MEDIUM
- **Status:** LIKELY
- **Affected files:** `v31_stage_protocol.valid_aggregate`, per-stage `out.exists()` reuse (`v31_audit`, `v31_repair`, `v31_postcheck`, `v31_adjudicate`), `v31_artifact_dag` (invalidates only on explicit redo flags)
- **Failure lifecycle:** Reuse = file exists + parseable + `version` is a string + `expected==completed`. No stage hashes its actual inputs (source chapter text, translations, config). If a source chapter or config is edited but the PID count is unchanged and no `--redo*`/`--reset` is passed, every downstream aggregate is reused and the final text reflects stale inputs while the quality gate passes (its checks are coverage/count-based, not content-based). `sha256_json` exists in `v31_common` but is unused for cache identity.
- **Evidence:** `grep sha256_json` → defined, never used for caching; `valid_aggregate` logic; DAG invalidation gated solely on redo switches.
- **Existing coverage:** `self_test_stage_protocol` covers missing/partial/invalid aggregates, not stale-but-internally-consistent ones.
- **Missing regression test:** changing an upstream input hash must force `MODEL_REQUIRED`.
- **Disposition:** Acceptable v4 debt **only if** operators reliably use `--reset`/`--redo` on any input change; document loudly. Recommend an input fingerprint in cache identity for v4.

### F5 — changed-PID ledger ignores whitespace-only changes, narrowing final targeted re-verification
- **Severity:** LOW
- **Status:** LIKELY (low impact)
- **Affected files:** `v31_final_lifecycle.changed_pids` (whitespace-normalized diff), `v31_finalize_quality` (final semantic/Russian detectors scoped to `ledger.changed_pids`)
- **Failure lifecycle:** `changed_pids` compares whitespace-normalized text, so a repair that alters only whitespace/structure is not recorded, and the targeted final semantic/Russian re-verification (scoped to ledger PIDs) skips it. Mitigated by: (a) `load_translations` normalizes whitespace on read, so pure-whitespace edits mostly cannot persist to final text; (b) global smoke covers all PIDs and deterministic checks still run. Residual risk is small.
- **Existing coverage:** `self_test_v31` asserts unchanged PIDs aren't recomputed (the intended behavior), not the escape case.
- **Missing regression test:** none required given mitigations; document the assumption.
- **Disposition:** Accept; note in limitations.

### F6 — Runner operates over all `work/` chapters, not just the selected range
- **Severity:** LOW
- **Status:** CONFIRMED
- **Affected files:** `run_full_pipeline_v31.ps1` (717–719 pre-final copy, 725–727 quarantine scan), `v31_artifact_dag.apply` (iterates `work_dir.iterdir()`)
- **Failure lifecycle:** The pre-final copy, the quarantine scan, and DAG invalidation iterate every chapter directory under `work/`, not `$SelectedChapterStems`. RunRoot is namespaced per `chapter_<Start>_to_<End>_v31`, so contamination is limited to overlapping ranges reusing a RunRoot, but a stale quarantined gate from an unrelated chapter in the same RunRoot would abort finalization, and redo would delete unrelated chapters' artifacts.
- **Existing coverage:** none.
- **Missing regression test:** scope-limited apply/scan test.
- **Disposition:** Scope these operations to selected chapters; v4 debt.

### F7 — QUESTION: installation, active-path verification, and rollback are not in this bundle
- **Severity:** n/a (evidence gap)
- **Status:** QUESTION
- **Affected files:** none present; `collect_v31_bundle.ps1` is only a packager
- **Detail:** Priority #10 asks whether installation, active-path verification, and rollback are reliable. The bundle contains a pipeline **runner**, not an installer/deploy/rollback path. This cannot be audited from the snapshot. Baseline note already flags that GitHub merged-PR lineage is an explicit evidence gap and that `main` has reverted v3.1.3.
- **Disposition:** Supply the install/activation/rollback scripts (or confirm they are out of scope for this RC) before relying on priority #10.

---

## Cross-cutting observations (not defects)

- **Priority #1 (corrupt final → complete):** well defended for the covered failure classes. Final gate = deterministic fail-categories on final text + final semantic/Russian re-verification of changed PIDs + a single source-grounded Qwen global smoke over all PIDs + append-only changed-PID ledger integrity. A subtle, non-deterministic corruption in an **unchanged** PID that the full primary+residual ensemble missed and the single global-smoke pass also misses could in principle reach `complete` — inherent audit false-negative risk, not a code defect. Needs real-run data.
- **Priority #7 (re-check after every repair):** satisfied. After the final repair round the runner re-runs the full final audit pass (line 722) before `finalize_quality`; residual repair is followed by the final-pass re-verification over changed PIDs + global smoke.
- **Priority #5 (foreign server):** solid. `Start-LlamaServer` reuses only an owned+healthy+command-signature-matched process; refuses to stop or attach to an unowned endpoint on port 8080; `Stop-LlamaServer` kills only the owned PID and warns otherwise. `Test-V31OwnedServerIdentity` checks PID, profile, executable, command signature, live command line, health URI, and health status.
- **Priority #8 (glossary auto-mutation):** safe. `prepare_pipeline_context` snapshots `locked/established/provisional/conflicts.json` and restores them in a `finally`; the production glossary is copied into the run root and never written back. Chapter-bible glossary enforcement is deterministic (known targets override, Latin-only placeholders removed) and confined to run-scoped files.
- **Priority #6 (lineage):** the final changed-PID ledger is append-only and preserves earlier changed PIDs/reasons across residual→final stages (verified by `self_test_v31`). The only lineage weakness is the whitespace normalization of F5 and the multi-chapter ledger-resolution bug of F3.
- **`uncertain_policy='repair'` + `fail_on_uncertain=false` (runner config):** uncertain cross-verify verdicts are routed to repair at medium confidence rather than failing. Acceptable because candidates must still clear the strict independent post-gates (dual semantic + Russian + deterministic, all high-confidence-accept) before any text changes. Flag as a policy choice to confirm, not a bug.

---

## Test gaps (summary)

1. No integration test that a prior quarantined/failed terminal state is preserved across re-runs (F1) — the passing `terminal_status` unit tests exercise an unused function.
2. No monitor test using the real `v31_common.VERSION`; the suite hard-codes `3.1.3` artifacts and cannot see the `3.1.2j` mismatch (F2).
3. No multi-chapter integration test; the single-ledger final-pass bug is uncaught (F3).
4. No stale-input reuse test (edited input with matching PID count must invalidate) (F4).
5. No scope test proving apply/scan/pre-final-copy touch only selected chapters (F6).

---

## Verdict

| Severity | Count |
|---|---|
| BLOCKER | 0 |
| HIGH | 2 (F2, F3) |
| MEDIUM | 2 (F1, F4) |
| LOW | 2 (F5, F6) |
| QUESTION / evidence gap | 1 (F7) |

**Decision: APPROVE WITH FIXES.**

**Mandatory before merge to `main`:**
1. **F2** — unify the version identity (`VERSION` → `3.1.3`, decoupled from the build-tag the monitor compares against); confirm the monitor reports READY / no mixed-version on a clean run. Decide whether `valid_aggregate` should enforce an expected version.
2. **F3** — resolve the final ledger per chapter (or hard-gate the runner to single-chapter invocation) so multi-chapter runs don't abort or mis-scope.
3. **F1** — either wire `terminal_status`/prior-state read into `finalize_quality`, or amend the quality contract to stop asserting monotonic quarantine.

**Acceptable v4 debt (document, don't block):** F4 (input fingerprint — provided operators use `--reset`/`--redo` on input changes), F5 (whitespace ledger), F6 (selected-chapter scoping).

**Readiness:**
- **For 2–3 production-quality test runs:** **YES, with two constraints** — run **one chapter per invocation** (`-Start N -End N`) to avoid F3, and disregard the monitor's mixed-version/BLOCKED banner until F2 is fixed (it is a false alarm). The core pipeline (server ownership, glossary freeze, finalize/repair/re-verify chain, append-only lineage) is sound for single-chapter runs.
- **For release/merge:** NO until F2 and F3 are fixed (F1 fixed or contract amended).
- **Before v4 planning:** F1, F4, F6 are the architectural items to settle (terminal-state ownership, cache identity, chapter scoping). No BLOCKER stands in the way of starting v4 planning.

**Questions requiring real-run data:**
- Rate of global-smoke false-negatives on unchanged PIDs (priority #1 residual risk).
- Frequency with which `uncertain_policy='repair'` routes issues to repair and whether the strict post-gates hold the quality line.
- Real behavior of the monitor and stage-protocol reuse across an interrupted-then-resumed run (F1/F4 exposure in practice).
- Installation / active-path / rollback reliability (F7) — scripts not in the bundle.
