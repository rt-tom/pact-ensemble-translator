# Design: test-suite baseline reconciliation

## Approach

Treat the red suite as a set of failure clusters, not 138 independent defects. Preserve the current `main` behavior and contracts as the comparison target.

1. Capture current and pre-limit failure IDs and group by exception/assertion plus relevant traceback. Current comparison establishes 136 persistent failures and two newly red qwen-budget tests.
2. Work through high-risk groups first: explicit `repair` role binding and B3 downstream behavior; then kill-safe/resume/cache; then promotion/canonical fixtures; then provider-schema and numeric budget assertions.
3. For each group, select one representative test, identify whether it fails during setup or reaches the intended behavior, and validate the proposed fixture/expectation change against its contract. Update sibling tests only when they share the same verified cause.
4. Keep the strict runtime behavior intact. A fake backend that lacks a required role must be corrected in the test, not given a production fallback. Canonical promotion fixtures must contain the required canonical artifacts, not weaken the boundary.
5. Budget expectations must distinguish request token ceilings from content allowance after reasoning headroom. Check deterministic chunk sizes and formula slopes separately.
6. If investigation demonstrates an application bug, fix only that bug under an existing approved requirement. Any scope that changes product policy, state format, or safety gate is escalated before implementation.

## Verification

- Run one representative test per cluster after its fixture/assertion change.
- Run focused B3/repair/resume/runtime groups.
- Run full `tests/pact_v4` through the worktree `.venv`.
- Run `openspec validate test-suite-baseline-reconciliation --strict` and `git diff --check`.
- Obtain fresh independent `pact-rev` review before delivery.

No real pipeline, model server, or production artifact is used.
