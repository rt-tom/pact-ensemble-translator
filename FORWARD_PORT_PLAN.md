# Forward-port plan: `main` (v3.1.3-hotfix.2) → `develop/v3.1.3`

Baseline: `git cherry -v develop/v3.1.3 main` lists 16 commits on `main` absent
from `develop/v3.1.3`.

Excluded no-ops (per task brief):
- `da5d09a` **release: v3.1.3-04 canonical chapter manifest** + `d2c6b36`
  **Revert "release: v3.1.3-04 canonical chapter manifest"** — self-cancelling
  pair.
- `2c44679` **Revert "release: v3.1.3 atomic artifacts and cache identity"** —
  reverts `b513767`, which is already present on `develop/v3.1.3` (via
  `a234ef3` upstream). The revert is a no-op vs the target branch and must
  not be forward-ported (would undo work already on develop).

Remaining candidates: **13 commits**. One of them (`e8efd19c`) turns out to be
functionally identical to a commit already on develop and is dropped; another
(`231af93f`) is a squash-merge whose only unique content is extracted rather
than picked whole. Net actions: **~15 focused ports** across 12 source commits.

The atomic-artifacts refactor on develop (`b513767` + follow-ups
`924f7f3`, `b991a14`, `719b4be`, `d19bf0c`, `1b42327`, `d826e9c`, `b06599d`,
`b12a9c1`) touched every v31 pipeline module: `v31_common.py`,
`v31_repair.py`, `v31_audit.py`, `v31_cross_verify.py`, `v31_postcheck.py`,
`v31_build_review.py`, `v31_source_analysis.py`, plus
`run_full_pipeline_v31.ps1` and `monitor_pipeline_v31.ps1`. Conflict analysis
below is against that post-refactor state.

---

## Per-commit analysis

### 1. `e8efd19c` — fix: recover replace_full repair candidates

- **(a) Present on develop?** **Yes, identical.** Develop's `d826e9c`
  ("fix: recover replace_full candidates in v3.1.3") is byte-for-byte the same
  hunk in `v31_repair.py::parse_candidates` (`elif text == current and new and
  new != current: after = new`). The two commits were the same fix carried on
  parallel branches.
- **(b) Files:** `v31_repair.py`, `self_test_v31.py`. Both refactored on
  develop, but the hunk itself is already there.
- **(c) Conflict risk:** n/a.
- **Action:** **SKIP.**

### 2. `b3c6add6` — release: v3.1.2j repair full new fallback

- **(a) Present on develop?** **No.** Extends the `replace_full` recovery
  above with a second, validation-gated branch that accepts `new` when `text`
  fails `runtime.validate_single_repair`, and adds `text_source` /
  `model_text` provenance fields to the candidate record.
- **(b) Files:** `v31_repair.py` (extends `parse_candidates`; ~18 lines),
  `self_test_v31.py` (+69 lines new fallback tests). Both files were
  refactored by the atomic-artifacts work, but this commit's target region
  in `parse_candidates` is stable.
- **(c) Conflict risk:** **mechanical.** Anchor lines (`elif text == current
  and new and new != current: after = new`, and the `candidates.append({...})`
  block) exist verbatim on develop. Tests append cleanly.
- **(d) Depends on:** `d826e9c` on develop (already there); no other
  prerequisites.

### 3. `2f6d1854` — release: v3.1.2k strict whole-PID repair

- **(a) Present on develop?** **No.** Normalizes `replace_span` to
  `replace_full` only when `raw_old == current` (byte-exact whole-PID),
  preserves `declared_action` / `operation_provenance`, tightens the span
  literal check (rejects fuzzy/absent old, adjusts the `changed_ratio`
  guard to exempt whole-PID spans).
- **(b) Files:** `v31_repair.py` (same `parse_candidates` region as #2),
  `self_test_v31.py` (+55 lines).
- **(c) Conflict risk:** **mechanical**, but must be applied **after** #2 —
  `2f6d1854`'s upstream parent is `b3c6add6`, so the hunks assume the
  `text_source` bookkeeping introduced there.
- **(d) Depends on:** #2 (`b3c6add6`).

### 4. `231af93f` — release: Pact Ensemble Translator v3.1.3  *(SQUASH BUNDLE)*

- **(a) Present on develop?** **Mostly yes, in original decomposed form.**
  This is a squash merge of the same feature series that landed on develop as
  individual commits (`924f7f3` DAG, `b991a14` stage protocol, `719b4be`
  chapter manifest, `d19bf0c` monitor, `1b42327` formatting, `d826e9c`
  replace_full, `b06599d` final PID lineage, plus review-bundle docs). Do
  **not** cherry-pick as a single commit — it will conflict massively against
  its own already-landed decomposition on develop.
- **(b) Net-new content (not on develop, in any form):**
  - `v31_common.py`: constants `ARTIFACT_VERSION`, `LEGACY_COMPATIBLE_ARTIFACT_VERSIONS`
    (`{"3.1.2j"}`), `TEMPORARY_LEGACY_COMPATIBILITY_POLICY`, helper
    `compatible_artifact_version(...)`, and the `"final"` value added to the
    `--pass-name` choices. **Load-bearing** — referenced by #5, #7, #10, #14.
  - New files: `v31_release_deploy.ps1`, `self_test_release_deploy_v31.ps1`,
    `v31_final_ledger_scope.py`, `self_test_final_ledger_scope.py`,
    `self_test_glossary_candidate_ledger_v31.py`.
  - Expanded modules (superset of develop): `v31_final_lifecycle.py`
    (Qwen global smoke + final PID scoping), `v31_audit.py` (+58 lines vs
    develop), `v31_stage_protocol.py` (+102 vs develop),
    `v31_finalize_quality.py` (+37 vs develop), `v31_merge_issues.py`
    (small tweak), `pact_translate_v3.py` (Qwen final smoke integration),
    `v31_artifact_dag.py` (+4).
  - Docs / infra: `docs/releases/v3.1.3-rc1/MANIFEST.md`,
    `docs/reviews/v3.1.3-rc1/CHATGPT_REVIEW_BUNDLE_RU.md`,
    `docs/reviews/v3.1.3-rc1/CLAUDE_REVIEW_BUNDLE_RU.md`,
    `pact_full_pipeline_runner_v1/README.md`, `.gitignore` additions
    (`release_manifest.v31.json`, `deployment_provenance.v31.json`).
- **(c) Conflict risk:** **semantic** if picked whole; **mechanical** when
  extracted as focused slices below.
- **Action:** **EXTRACT INTO ~4 FOCUSED PORTS** and drop the squash itself:
  - **4a.** Add `ARTIFACT_VERSION` / `LEGACY_COMPATIBLE_ARTIFACT_VERSIONS` /
    `compatible_artifact_version` / `TEMPORARY_LEGACY_COMPATIBILITY_POLICY`
    constants and the `"final"` `--pass-name` value to `v31_common.py`.
    *Load-bearing prerequisite for #5, #7, #10, #14.*
  - **4b.** Add release-deploy files: `v31_release_deploy.ps1` +
    `self_test_release_deploy_v31.ps1`. *Prerequisite for #7.*
  - **4c.** Add ledger/finalization additions on top of develop's versions:
    `v31_final_ledger_scope.py`, `self_test_final_ledger_scope.py`,
    `self_test_glossary_candidate_ledger_v31.py`, and merge the Qwen
    final-smoke deltas into `v31_final_lifecycle.py`,
    `v31_finalize_quality.py`, `v31_stage_protocol.py`, `v31_audit.py`,
    `pact_translate_v3.py`. **Semantic risk here** — develop's variants
    diverge; do this as a manual 3-way merge, not a cherry-pick.
  - **4d.** Add rc1 review-bundle docs, README updates, `.gitignore`
    entries. Trivial.

### 5. `c16653aa` — fix: support new release schema introductions

- **(a) Present on develop?** **No.** Adds `schema_introduction` record type
  to `Assert-Migrations` in `v31_release_deploy.ps1`, extends the self-test,
  and adds `docs/releases/v3.1.3/MIGRATION_PLAN.md` +
  `migration_plan.json`.
- **(b) Files:** `v31_release_deploy.ps1` and `self_test_release_deploy_v31.ps1`
  **do not exist on develop** until slice **4b** lands.
- **(c) Conflict risk:** **none** once #4b is in place; would fail to apply
  otherwise (missing target file).
- **(d) Depends on:** #4b.

### 6. `7d311343` — fix: start Gemma before chapter bible preparation

- **(a) Present on develop?** **No.** Adds `Test-PrepareContextModelRequired`
  helper and a conditional `Start-LlamaServer GemmaTranslate` call before
  the `Prepare manifest` stage in `run_full_pipeline_v31.ps1`; adds
  `self_test_prepare_context_v31.py` (new file); minor tweak to
  `self_test_runner_model_policy_v31.ps1`.
- **(b) Files:** `run_full_pipeline_v31.ps1` was rewritten by the develop-side
  DAG + stage-protocol + monitor commits. The insertion point (before
  `Invoke-PythonStage -Label '1/11 Prepare manifest, ...'`) exists on develop
  (line ~680), and `Start-LlamaServer GemmaTranslate` / `$SelectedChapterStems`
  are defined identically. `self_test_prepare_context_v31.py` is net-new
  (also referenced by #7).
- **(c) Conflict risk:** **semantic** — needs to re-anchor around develop's
  line numbering, but the helper is self-contained and the call site is
  unambiguous. No dependence on the atomic-artifacts refactor beyond the
  presence of `Start-LlamaServer`.
- **(d) Depends on:** nothing else in this plan (net-new file has no develop
  precursor).

### 7. `d0b6a134` — fix: enforce v31 work manifest version (#34)

- **(a) Present on develop?** **No.** Adds a `manifest_version` kwarg to
  `Runner.prepare_chapter` in `pact_translate_v3.py` that rejects work
  manifests whose `version` does not match the required semantic version;
  `prepare_pipeline_context.py` imports `ARTIFACT_VERSION` from `v31_common`
  and passes it in.
- **(b) Files:** `pact_translate_v3.py` (diverges heavily overall but the
  `prepare_chapter` region is intact on develop);
  `prepare_pipeline_context.py` (line 217 target present verbatim on
  develop); `self_test_prepare_context_v31.py` (only exists after #6).
- **(c) Conflict risk:** **mechanical**, but with **two hard preconditions**.
- **(d) Depends on:** **#4a** (needs `ARTIFACT_VERSION` in `v31_common.py`)
  and **#6** (needs `self_test_prepare_context_v31.py` to exist for its test
  additions).

### 8. `2bbc7deb` — feat: restore v31 monitor diagnostics (#35)

- **(a) Present on develop?** **No.** Adds `Get-LiveDiagnostics` helper to
  `monitor_pipeline_v31.ps1`, wraps the summary in `AUTHORITATIVE STATE` /
  `LIVE DIAGNOSTICS` labels, and appends fixture setup to
  `self_test_monitor_v31.ps1`.
- **(b) Files:** `monitor_pipeline_v31.ps1` was rewritten on develop by
  `d19bf0c` ("fix: report live v3.1 monitor state") — the resulting file is
  the exact ancestor of this commit, so the anchors (`Test-OwnedProcess`,
  `Show-Monitor`, the final `Resume: ...` line) all match verbatim.
- **(c) Conflict risk:** **mechanical.**
- **(d) Depends on:** nothing.

### 9. `85cf9f1c` — fix: fall back when Gemma rejects JSON grammar (#38)

- **(a) Present on develop?** **No.** Adds `_json_response_format_supported`
  flag and `_rejects_json_response_format` helper to `ApiClient`; catches
  llama-server's `does not match the expected peg-gemma4 format` error and
  retries once without `response_format`. Adds new
  `test_api_client_json_fallback.py`.
- **(b) Files:** `pact_translate_v3.py` diverges overall between main and
  develop (~172 lines), but the specific ApiClient region (line ~363 /
  ~435, `payload["response_format"] = {"type": "json_object"}`) is
  unchanged on develop.
- **(c) Conflict risk:** **mechanical.**
- **(d) Depends on:** nothing.

### 10. `2ff37797` — docs: simplify agent workflow guidance (#39)

- **(a) Present on develop?** **No.** Cuts `AGENTS.md` from ~529 → ~103 lines
  (net −426). Develop still carries the older verbose version essentially
  unchanged since `3dba81f docs: align agent workflow guidance (#10)`.
- **(b) Files:** `AGENTS.md` only. Not affected by atomic-artifacts.
- **(c) Conflict risk:** **semantic-low.** Effectively a whole-file
  replacement. Do a diff of develop's `AGENTS.md` against `3dba81f` first —
  if develop hasn't touched it since, apply as verbatim overwrite; otherwise
  fold in any develop-side additions.
- **(d) Depends on:** nothing.

### 11. `ebc8442d` — docs(v4): v4.0 literature review, MVP spec, plan (ed.2) (#40)

- **(a) Present on develop?** **No.** Three new files under
  `docs/architecture/`.
- **(b) Files:** all new, no overlap.
- **(c) Conflict risk:** **none.**
- **(d) Depends on:** nothing.

### 12. `8d92bca1` — fix: guide high-ratio repair retries

- **(a) Present on develop?** **No.** Adds `REPAIR_RETRY_GUIDANCE` string
  constant and passes it as `retry_guidance=` to the `complete_json`
  invocation in `v31_repair.py::main`.
- **(b) Files:** `v31_repair.py`, `self_test_v31.py`.
- **(c) Conflict risk:** **mechanical**, but the call-site anchor was
  established by #3 (`2f6d1854`) chain. Should land last in the repair
  series so the constant sits above the region touched by #2/#3.
- **(d) Depends on:** #3 (repair-file structure).

### 13. `5ccb21c0` — baseline: audit findings, pipeline runner scripts

- **(a) Present on develop?** **No.** Adds legacy generic runner scripts
  (`apply_project_fixes.py`, `collect_pipeline_result.ps1`,
  `monitor_pipeline.ps1`, `retry_rejected_repairs.py`, `run_full_pipeline.ps1`,
  `verify_pipeline_issues.py`, `verify_repair_results.py`), two audit MDs
  (`V313_INDEPENDENT_AUDIT_FINDINGS.md`, `V313_RC2_DELTA_REAUDIT.md`), and
  `.gitignore` entries (`deployment_backups/`, `*_before_*_2026*.ps1`).
- **(b) Files:** every added file is net-new (no `_v31` suffix — these are
  the pre-v31 generic runners). No overlap with atomic-artifacts.
- **(c) Conflict risk:** **mechanical** (only `.gitignore` is edited; the
  rest are creations). `.gitignore` needs both the release-manifest entries
  from **4d** and these entries to end up ordered together — merge order
  matters, not content.
- **(d) Depends on:** #4d (both edit `.gitignore`; interleave to avoid a
  cosmetic re-order patch).

---

## Recommended port order

Ports are grouped by dependency. Each numbered item is one commit on the
port branch; nothing here should be squashed further.

**Phase A — Prerequisite scaffolding from `231af93f`** *(extract, do not
cherry-pick the squash)*

1. **4a.** `v31_common.py`: add `ARTIFACT_VERSION`,
   `LEGACY_COMPATIBLE_ARTIFACT_VERSIONS`,
   `TEMPORARY_LEGACY_COMPATIBILITY_POLICY`, `compatible_artifact_version`,
   and the `"final"` `--pass-name` choice.  *(Unblocks 6, 8, 12.)*
2. **4b.** Add `v31_release_deploy.ps1` + `self_test_release_deploy_v31.ps1`
   as new files.  *(Unblocks 5.)*
3. **4c.** Merge Qwen final-smoke + ledger scoping additions into develop's
   `v31_final_lifecycle.py`, `v31_finalize_quality.py`,
   `v31_stage_protocol.py`, `v31_audit.py`, `pact_translate_v3.py`; add new
   `v31_final_ledger_scope.py`, `self_test_final_ledger_scope.py`,
   `self_test_glossary_candidate_ledger_v31.py`. **Manual 3-way merge; do
   not cherry-pick.** Split into per-module commits if the diff is too large
   to review in one.
4. **4d.** Add `docs/releases/v3.1.3-rc1/MANIFEST.md`,
   `docs/reviews/v3.1.3-rc1/CHATGPT_REVIEW_BUNDLE_RU.md`,
   `docs/reviews/v3.1.3-rc1/CLAUDE_REVIEW_BUNDLE_RU.md`, README updates,
   `.gitignore` entries (`release_manifest.v31.json`,
   `deployment_provenance.v31.json`).

**Phase B — Repair-lifecycle chain** *(strictly ordered)*

5. **`b3c6add6`** — v3.1.2j: `new`-field validation-gated fallback +
   `text_source` / `model_text` provenance.
6. **`2f6d1854`** — v3.1.2k: strict whole-PID normalization,
   `declared_action` / `operation_provenance`, tightened span-literal check.
7. **`8d92bca1`** — high-ratio repair retry guidance (`REPAIR_RETRY_GUIDANCE`
   → `complete_json(retry_guidance=...)`).

**Phase C — Runtime fixes with prerequisites**

8. **`c16653aa`** — release schema-introduction support. *Requires 4b.*
9. **`7d311343`** — start Gemma before chapter-bible preparation
   (`Test-PrepareContextModelRequired`, `Start-LlamaServer GemmaTranslate`
   pre-stage-1). *Also adds `self_test_prepare_context_v31.py`; required
   by #10.*
10. **`d0b6a134`** — enforce v31 work-manifest version
    (`Runner.prepare_chapter(manifest_version=...)`,
    `prepare_pipeline_context.py` passes `ARTIFACT_VERSION`).
    *Requires 4a **and** 9.*

**Phase D — Independent fixes** *(any order)*

11. **`2bbc7deb`** — restore monitor diagnostics
    (`Get-LiveDiagnostics`, `AUTHORITATIVE STATE` / `LIVE DIAGNOSTICS`
    sections).
12. **`85cf9f1c`** — Gemma JSON grammar fallback in `ApiClient` + regression
    test.

**Phase E — Docs and infra baseline**

13. **`ebc8442d`** — v4.0 literature review / MVP spec / implementation
    plan (three new files under `docs/architecture/`).
14. **`2ff37797`** — simplified `AGENTS.md`. Diff-check against `3dba81f`
    first; overwrite if develop hasn't edited since.
15. **`5ccb21c0`** — legacy runner scripts + audit findings + `.gitignore`
    additions. Land after **4d** so the `.gitignore` interleave is one
    ordered edit rather than a re-order patch.

**Dropped**

- **`e8efd19c`** — already present on develop as `d826e9c` (identical
  hunk).
- **`da5d09a`**, **`d2c6b36`**, **`2c44679`** — no-op revert pairs
  (excluded per task brief).
