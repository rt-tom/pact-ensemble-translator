## Context

See `v4-phase12-strict-runner` contract map. The strict runner supports both chunked and whole-chapter generation while preserving chunked audit, append-only journal/resume, identity rejection, and terminal artifact provenance. This change creates only characterization tests before any extraction proposal.

## Goals / Non-Goals

**Goals:**
- Make the highest-risk contracts explicit, offline, deterministic test cases.
- Reuse existing fixtures and test patterns where possible; add no broad integration harness.
- Establish one documented narrow baseline command for future refactor gates.

**Non-Goals:**
- No production-module move, rename, signature change, or refactor.
- No new behavior, artifact fields/schema, runtime configuration, model routing, prompts, fidelity policy, or pipeline execution.
- No Stage 2 utility extraction or later stages.

## Decisions

- **Tests must observe existing external contracts, not implementation helpers.** Assert persisted journal/artifact content, public result fields, and failure prefixes rather than private call sequence; this keeps tests useful during later refactors. Alternative: snapshot private helper calls — rejected because it freezes structure rather than behavior.
- **Use synthetic/offline fixtures only.** Fake backends and temporary output directories avoid source text, paid calls, and persistent artifacts. Alternative: replay real chapter outputs — rejected for data-boundary and determinism risk.
- **One invariant per test group.** Cover (a) resume/foreign identity, (b) one-entry whole-chapter journal plus PID order, (c) whole-chapter generation with chunked audit evidence, and (d) terminal output/strict record provenance. Avoid a monolithic golden-output test.
- **No test is added solely to raise coverage.** Each test must map to a contract-map entry and a future extraction risk.

## Risks / Trade-offs

- [Tests duplicate existing assertions] → First map existing tests; add only uncovered boundary assertions or factor existing tests into named characterization coverage without behavioral changes.
- [Fixtures accidentally depend on private structure] → Review assertions for public artifacts/errors/results only.
- [Characterization exposes existing behavior ambiguity] → Stop and return it to the owner; do not normalize behavior in this change.

## Migration Plan

No deployment or migration. Rollback is reverting test-only commits. Later extraction changes must run this baseline plus the affected strict-runner suite.

## Open Questions

None. The scope is constrained to existing contracts and test-only work.