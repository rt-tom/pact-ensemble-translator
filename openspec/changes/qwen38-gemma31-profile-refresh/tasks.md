## Approval gate

- [x] 0.1 Owner approves proposal/spec/design before implementation; unchanged sampling/hybrid budgets and exact Q4_0 embedded-MTP B3 eligibility confirmed.
- [x] 0.2 Owner approves later addendum: ordinary qwen retains model/reasoning but drops speculative MTP; B3 audit remains active with a narrow no-draft contract. Implementation is still pending.

## Implementation (after approval only)

- [ ] 1.1 Refresh only `gemma31` and `qwen38` registry entries in `configs/providers.yaml` to the exact design §1 profiles. Do not duplicate lifecycle-injected `--device`; retain `gemma31 -fitt 512`, existing model-owned request sampling and role budgets.
- [ ] 1.2 Change the qwen38-specific B3 gate to require the exact approved embedded-MTP Q4_0 identity/config; maintain fail-closed checks for role-effective reasoning, context, wrong model, external draft and malformed flags.
- [ ] 1.3 Remove only `--spec-type draft-mtp` from ordinary `qwen` in `configs/providers.yaml`, `configs/runtime_local.example.yaml` and `QWEN_SERVER_ARGS`; keep the exact model path/name, `--reasoning-budget-enable`, budgets and other args. Update the ordinary `qwen` B3 branch to accept only this no-draft profile while keeping the audit and hard filters active.
- [ ] 1.4 Update only affected profile/matrix guidance and explicit historical spec references if required; do not change remote profiles, CLI semantics or unrelated model aliases.

## Verification (no pipeline/server execution)

- [ ] 2.1 Focused tests for Gemma31 and Qwen38 ordered arg lists, no duplicate `--device`, role budgets/sampling and B3 good/bad matrix (old build, lookalike paths, external/missing draft, missing/duplicate spec config, context and budget); ordinary Qwen3.6 parity across registry/example/CLI, B3 positive without draft and negatives for draft reintroduction, wrong path, weakened limits.
- [ ] 2.2 Focused run-identity/resume rejection regression for old vs new profiles including ordinary `qwen` using temporary fixtures; offline local preflight for both default and explicit pair where appropriate.
- [ ] 2.3 Run `openspec validate qwen38-gemma31-profile-refresh --strict`, focused tests via worktree `.venv`, `git diff --check`, independent pact-rev review. Report what was not verified on RT.
- [ ] 2.4 Before any separately approved production run, owner verifies on RT that the final combined Q4_0 server flags (including preserved budget/effort/enable and `--no-reasoning-preserve`) are supported and reviews target checkout/config/input/state/output. No agent-started server or pipeline and no reuse of old run directories.
