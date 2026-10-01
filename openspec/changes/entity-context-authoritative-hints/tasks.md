## Approval gate

- [ ] 0.1 Owner reviews and approves proposal/spec/design, including the proposed 32-row/2048-character hint cap and source-only evidence boundary, **before** implementation.

## Implementation (after approval only)

- [ ] 1.1 Build a deterministic, bounded pre-chapter hint card from source-matched authoritative English names/aliases/terms; exclude unresolved conflicts, candidate-only/unmatched content; emit truncation/selection diagnostics. Do not pass Russian forms, unrelated facts or entire bible/glossary.
- [ ] 1.2 Pass the identical frozen card to `BackendEntityExtractor` for generation prepass and B3 audit replay; preserve empty-card source-only behavior and full PID grounding. Do not alter other prompts, hard filters, promotion gates or model settings.
- [ ] 1.3 Bind the rendered card and prompt variant to B1.2 cache and relevant stage/resume identity. Invalidate stale hinted cache on relevant changes, preserve valid empty-card behavior and fail-closed artifact checks; no state migration or bulk rebuild.

## Offline validation (no pipeline/model server)

- [ ] 2.1 Add negative/positive tests for matches, aliases, boundary words, conflicts/candidates, ordering/cap/truncation, empty-card path, source-only PID validation, known entity with new alias, contradictory prior hint, changed-hint cache invalidation and same-chapter 0-extra-call replay/resume.
- [ ] 2.2 Run focused entity/B3/strict runner tests through the worktree `.venv`, fidelity lint, `openspec validate entity-context-authoritative-hints --strict` and `git diff --check`; obtain independent pact-rev review. Report any pre-existing failing tests separately.
- [ ] 2.3 State explicitly that token/latency savings and quality parity are unproven until an owner-approved A/B trial on comparable chapters with per-call input/reasoning/output and entity-quality metrics. Do not run a pipeline, deploy, merge or archive without separate approval.
