## 1. Pre-flight and scope lock

- [ ] 1.1 Verify isolated worktree `openspec/v3-deprecation-plan` on `media` (`git branch --show-current != main`, `pwd == /home/rt/projects/pact-worktrees/v3-deprecation-plan`), confirm no `D:\pact\pact_translator_v4_1` edits staged, no `reset --hard`/`force push` — verify: `git status --porcelain` clean + `git diff --stat` shows only `openspec/changes/v3-deprecation-plan/` planning files.
- [ ] 1.2 Classify risk Low (planning/docs only, no runtime). Run narrowest checks: `openspec validate --change v3-deprecation-plan --strict` + `git diff --check`. No pipeline, no model-server ops — verify: no `llama-server` / `opencode` invocations in diff.
- [ ] 1.3 Lock scope: no moves/deletes/renames, no `configs/*` or `pact_v4/` edits, no pipeline runs, no model-settings changes. Any out-of-scope discovery (e.g., v3 import found in `pact_v4/`) is reported, not fixed — verify: scope checklist in PR description.

## 2. Inventory and contract map (read-only, no code changes)

- [ ] 2.1 Enumerate full legacy surface via `find`/`ls` over `pact_translate_v3.py`, `pact_full_pipeline_runner_v1/run_full_pipeline*.ps1`, `pact_full_pipeline_runner_v1/v31_*`, Phase0C `v4_phase0c_baseline.py`/`v4_phase0c_gate_bench.py`/`self_test_v4_*.py`/`v4_measurement_harness.py`/`v4_v3_draft_compare.py` + related `docs/releases/*`/`docs/plans/V3*`/`docs/schemas/v4_phase0c_result_record.schema.json`/`docs/audits/pact_translation_benchmark_report_v4_1.md`. Verify: file list in `inventory.md` matches `find` output, with line counts.
- [ ] 2.2 Classify each file into exactly one bucket `supported / historical / test / recovery / removable` with one-line rationale and contract binding (e.g., `v31_common.py` → `recovery` — holds `ARTIFACT_VERSION 3.1.3` + `CACHE_IDENTITY_SCHEMA`; `pact_translate_v3.py` → `historical` — frozen translator, tag `archive/v3-main-20260802`; `self_test_v4_phase0c_baseline.py` → `test` — harness imports `v4_measurement_harness` only). Verify: `inventory.md` table has no unclassified row, no file appears in two buckets, grep confirms `test` files never import `pact_translate_v3`.
- [ ] 2.3 Build contract map section: frozen v31 contracts (`ARTIFACT_VERSION`, `CACHE_IDENTITY_SCHEMA`, stage protocol, `deployment_provenance.v31.json`, Phase0C result schema `pact-v4-phase0c-result-record/v1`) vs. superseded v4 contracts (prompt/bible/glossary/runtime). Verify: map lists source file + schema/version per contract, no claim that v31 prompt policy still governs v4.
- [ ] 2.4 Confirm no executable dependency from v4 to legacy surface: `grep -R "pact_translate_v3\|v31_" pact_v4/ --include="*.py"` shows zero production imports (self-tests excluded). Verify: grep output pasted in `inventory.md` evidence appendix.

## 3. Decision gates (owner-operated)

- [ ] 3.1 Gate G1 — Owner approves `proposal.md`/`design.md`/`inventory.md` as correct classification and contract map. Requires: owner comment on PR + `DECISIONS.md` entry (date, decision, rationale). Block until G1 passes; no implementation PR may be opened — verify: PR checklist cites G1 status.
- [ ] 3.2 Gate G2 — Owner selects disposition per bucket: `historical` keep read-only (in-repo vs. tag-only), `recovery` retain until explicit retire, `removable` approve deletion. For `removable` → requires second explicit approval plus tag check `git show archive/v3-main-20260802:<path>` still resolves. Verify: gate records per-bucket decision table.
- [ ] 3.3 Gate G3 — Owner approves future implementation PR (not this planning change) only after G1+G2, with migration/rollback evidence attached. Verify: gate checklist requires `openspec validate --strict`, `git diff --stat` focused, `pytest tests/pact_v4 -q` green.

## 4. Migration / rollback (planning, not executed)

- [ ] 4.1 Document ordered future migration steps: G1→G2→(optional `archive/v3/` mirror via `git mv`)→gated `removable`-only deletion PR→history preservation check (`git log --follow`). Verify: steps in `design.md` §Migration Plan match `tasks.md` gates, no step claims immediate file move.
- [ ] 4.2 Document rollback: planning phase `git revert <planning-commit>`; post-archival `git revert <archival-commit>` + `git show archive/v3-main-20260802` as immutable fallback; no pipeline state rollback needed because no pipeline was run. Verify: rollback paragraph reviewed against `AGENTS.md` merge/deploy separation.
- [ ] 4.3 State forbidden actions in this change: `git rm`, `git mv`, `rm -rf archive/`, pipeline launch on `RT`, `configs/providers.yaml` edit. Verify: `git diff --stat` shows zero deletions/moves, `git log --stat HEAD` shows only `openspec/changes/v3-deprecation-plan/*`.

## 5. Evidence requirements (acceptance contract)

- [ ] 5.1 Planning evidence: `inventory.md` classification table (file, lines, bucket, rationale, contract), contract map, and grep evidence (`grep -R v31 pact_v4/` no prod import). Verify: table covers all files from task 2.1, each row has bucket + contract column.
- [ ] 5.2 Validation evidence: `openspec validate --change v3-deprecation-plan --strict` output (zero errors), `git status --porcelain` clean except planning files, `git diff --stat` focused. Verify: validation log pasted in PR body or `openspec status --change v3-deprecation-plan --json`.
- [ ] 5.3 Acceptance report evidence (required for review gate): `changed-files`, `tests-added` (empty for planning), `commands-run`, `validationOutput`, `residual-risks`, `no-staged-files`, `reviewFindings`. Verify: report shape matches `acceptance-report` JSON contract (empty arrays `[]` not `[""]`, `criteriaSatisfied[].status` in `satisfied|not-satisfied|not-applicable`).

## 6. Verification and commit hygiene

- [ ] 6.1 Run `openspec validate --change v3-deprecation-plan --strict` and `openspec status --change v3-deprecation-plan --json`; confirm `proposal`/`design`/`tasks` done, `specs` skipped (expected for `skip_specs: true`). Verify: validate returns `pass`, status shows `isPlanningComplete`.
- [ ] 6.2 Run `git diff --stat` / `git diff --check` clean, `git status --porcelain` shows only `openspec/changes/v3-deprecation-plan/` additions. No unrelated formatting churn, no secrets. Verify: diff is ~4 files, no `pact_v4/`/`configs/` delta.
- [ ] 6.3 Commit planning artifacts only: `git add openspec/changes/v3-deprecation-plan/proposal.md design.md tasks.md inventory.md` (plus `.openspec.yaml`/`README.md` if needed), commit on `openspec/v3-deprecation-plan`, push to origin. Verify: `git show --stat HEAD` lists only planning artifacts, no file moves/deletes.
