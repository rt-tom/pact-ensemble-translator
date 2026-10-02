## Approval Gate

- [x] 0.1 Owner approves this test-suite reconciliation scope before implementation. Production behavior stays unchanged unless a narrow defect against an existing approved contract is confirmed; material behavior/policy changes require a separate owner decision.

## Inventory and Fixes

- [x] 1.1 Preserve baseline/current failure lists and a compact root-cause cluster report.
- [x] 1.2 Fix explicit model-role bindings in test doubles and repair/re-audit fixtures without weakening no-fallback behavior.
- [x] 1.3 Update provider-registry test fixtures to satisfy the required role-budget schema where that schema is the contract under test.
- [x] 1.4 Repair canonical promotion fixtures and expected state shape while retaining exact-file/fail-closed assertions.
- [x] 1.5 Reconcile budget tests with reasoning headroom, content allowance, per-item/per-span increments, and configured ceilings; inspect the already-red region-gate expectations rather than changing constants blindly.
- [x] 1.6 Investigate remaining clusters; make only contract-backed minimal fixes, escalating any product-policy ambiguity.

## Verification and Review

- [x] 2.1 Run representative tests after each cluster fix, then focused B3/repair/resume/runtime suites.
- [x] 2.2 Run the full `tests/pact_v4` suite with the verified worktree `.venv`.
- [x] 2.3 Run OpenSpec strict validation and `git diff --check`.
- [x] 2.4 Complete independent `pact-rev` review and report any unresolved failures; no commit/push/merge/deploy without separate owner approval.
