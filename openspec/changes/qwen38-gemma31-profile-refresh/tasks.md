## Approval gate

- [x] 0.1 Owner approves proposal/spec/design before implementation; unchanged sampling/hybrid budgets and exact Q4_0 embedded-MTP B3 eligibility confirmed.
- [x] 0.2 Owner approves later addendum: ordinary qwen retains model/reasoning but drops speculative MTP; B3 audit remains active with a narrow no-draft contract. Qwen implementation completed at the baseline (present in `origin/main`; see historical section below) — only Gemma31 remains pending.
- [x] 0.3 Owner approves addendum B: new Gemma31 QAT-MTP profile update (keep `--reasoning-budget 2000`, apply every other change from the prior comparison table, no `--alias gemma-qat`, external MTP draft config only). Qwen38 / ordinary-Qwen requirements unchanged. No PR/commit/push/merge/deploy authorization given.

## Historical / completed at baseline — Qwen38 and ordinary-Qwen (protected, do not edit)

The Qwen38 embedded-MTP Q4_0 profile, the ordinary-`qwen` no-draft profile, both B3 branches, and their focused tests are already implemented in `origin/main`. Qwen requirements stay in the spec as the unchanged protected baseline. No edits to `qwen38`, ordinary `qwen`, aliases, routing, role budgets, or B3 code are authorized by this change.

- [x] H.1 (was 1.1) `qwen38` registry entry at the exact spec / design §1 profile — done at baseline (`origin/main:configs/providers.yaml` qwen38 block: Q4_0 path/name, embedded `draft-mtp` n-max 2 / p-min 0.5, no external draft, no lifecycle-duplicated `--device`; sampling 0.2/0.95/20/0.0/0.0).
- [x] H.2 (was 1.2) qwen38-specific B3 gate requiring the exact approved embedded-MTP Q4_0 identity/config with fail-closed checks — done at baseline (`_validate_b3_qwen_profile` qwen38 branch in `origin/main:pact_full_pipeline_runner_v1/v4_phase12_strict_run.py`).
- [x] H.3 (was 1.3) ordinary-`qwen` no-draft profile (only `--spec-type draft-mtp` removed; exact Qwen3.6 path/name, `--reasoning-budget-enable`, budgets/args kept) plus no-draft B3 branch — done at baseline (`origin/main` registry, `configs/runtime_local.example.yaml` with no `spec-type`, `QWEN_SERVER_ARGS` with no draft selector).
- [x] H.4 (was 2.1, Qwen scope) focused Qwen38 / ordinary-Qwen tests (ordered args, no duplicate `--device`, role budgets/sampling, B3 good/bad matrix, Qwen3.6 parity) — done at baseline (`origin/main:tests/pact_v4/runtime/test_local_matrix_v2.py`; Qwen/B3-selected 22 passed, full file 98 passed in worktree `.venv`).

## Implementation (after approval only) — Gemma31 QAT-MTP only

- [ ] 1.1b (Owner-approved addendum B, Gemma31 QAT-MTP only) Refresh the `gemma31` registry entry in `configs/providers.yaml` to the exact spec (design §1 mapping): QAT `model_path`/`model_name`, `-fitt 1280`, `-b/-ub 2048`, `-ctv q8_0`, `-t 6`, reasoning base 2000/sampling/role deltas, exact external-MTP draft args. No `--alias`, no `-m`/main `--device`/host/port in `server_args`. Leave `qwen38`, ordinary `qwen`, aliases, routing, role budgets, providers and runtime policy untouched; B3 code unchanged.
- [ ] 1.4 Update only Gemma31 profile/matrix guidance and explicit historical spec references if required; do not change `qwen38`, ordinary `qwen`, B3 code, remote profiles, CLI semantics or unrelated model aliases.

## Verification (no pipeline/server execution) — Gemma31 only

- [ ] 2.1b (Addendum B) Focused Gemma31 QAT tests: exact new profile shape (identity, changed values, exact draft args), no server alias/device duplication (`-m`, `--device`, `-dev`, host, port, `--alias` absent), unchanged reasoning base 2000/sampling/role-effective values (generator 4000, repair/Gemma-audit 2000), and historical old-Q5-profile rejection.
- [ ] 2.2 Focused run-identity/resume rejection regression for old Q5 vs new QAT `gemma31` profiles using temporary fixtures; offline local preflight for the Gemma31 pair where appropriate. (Ordinary-`qwen` identity coverage is baseline-complete; see H.4.)
- [ ] 2.3 Run `openspec validate qwen38-gemma31-profile-refresh --strict`, focused tests via worktree `.venv`, `git diff --check`, independent pact-rev review. Report what was not verified on RT.
- [ ] 2.4 Before any separately approved production run, owner verifies on RT that the final combined Gemma31 QAT server flags (including external draft args and preserved reasoning-budget-enable/budget flags) are supported and reviews target checkout/config/input/state/output. No agent-started server or pipeline and no reuse of old run directories. (Qwen Q4_0 manual launch was already established at the baseline per the proposal.)
