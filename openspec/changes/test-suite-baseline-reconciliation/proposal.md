## Why

The current `tests/pact_v4` suite reports 138 failures (2714 passed, 11 skipped). A full run at the pre-limit baseline `ca03f19` reports 136 failures (2674 passed, 11 skipped). The failure-ID comparison shows two additional failures after the output-budget updates; the other 136 predate them. Many existing failures cluster around test doubles missing explicit `repair` bindings, obsolete provider fixtures, and promotion fixtures that do not satisfy the canonical artifact contract. A smaller number assert outdated budget/output behavior.

These failures make the suite a weak regression signal, especially for B3 repair, resume, cache, and promotion paths. The goal is to restore meaningful tests—not to make failures disappear by weakening safety checks.

## What Changes

- Repair test doubles and fixtures to satisfy the existing fail-closed role-binding, provider-registry, and canonical-promotion contracts, so tests reach the behavior they claim to cover.
- Reconcile output-budget assertions with the approved reasoning-headroom policy. Preserve content headroom, per-item slopes, configured ceilings, and deterministic chunking; do not blindly replace numeric constants.
- For every remaining baseline failure, inspect the representative failure and its shared cluster. Update a fixture/assertion only when it conflicts with the existing contract. Fix production code only if a reproducible defect violates an already-approved requirement; do not relax fail-closed behavior or invent new policy.
- No pipeline/model-server runs, production data changes, model-setting changes, deployment, or data migration.

## Risk and Boundaries

High test-risk area: B3 repair, re-audit, cache/resume, kill-safe savepoints, promotion, and canonical artifact boundaries. Test-only fixture corrections are preferred. Any proposed production behavior change must be narrow, contract-backed, and independently reviewed; if it would alter a product requirement or persistent-data contract, stop for a separate owner decision.

## Acceptance Criteria

1. Every currently failing `tests/pact_v4` case is assigned to a root-cause cluster and either fixed or explicitly escalated; no blanket skips/xfails or weakened safety assertions.
2. The full `tests/pact_v4` suite passes on the final branch, or any unresolved test is reported with evidence and owner decision before merge.
3. B3 tests exercise their intended repair/re-audit/resume/promotion paths with explicit role bindings and canonical fixtures, rather than failing early in setup.
4. Budget tests separately protect content capacity, reasoning headroom, per-item/per-span increments, and ceilings.
5. OpenSpec strict validation, focused tests followed by the full suite, and `git diff --check` pass; `pact-rev` independently approves the final diff.

## Impact

Expected primary changes are test fixtures and assertions under `tests/pact_v4/{pipeline,repair,runtime,...}`. Production files are out of scope unless a demonstrated defect conflicts with an existing approved contract. No new runtime feature or data format is intended.
