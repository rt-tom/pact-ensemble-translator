## 1. Contracts and fixtures

- [ ] 1.1 Freeze representative four-file state fixtures from accepted `rev-0011`, record exact hashes/file types, and verify historical snapshots remain untouched
- [x] 1.2 Add B1.2 `memory_class` enum and required `memory_worthy` field to prompt/schema/validator; bump extractor, cache, validation-report, and identity versions and verify old/missing/unknown fields force recompute
- [ ] 1.3 Define `pact-v4-book-memory/v2`, policy block, per-field/per-alias provenance, stable rejection codes, and tolerant legacy reader behavior; verify schema fixtures and glossary authority conflicts
- [ ] 1.4 Define `pact-v4-chapter-index/v2` reserved metadata and exact per-chapter scopes; inventory every reader/hash consumer and verify unsupported schema fails soft only to narrator+seed

## 2. Durable-memory gate

- [ ] 2.1 Implement the ordered identity→deny→allow/alias→model-veto→class-check→duplicate/conflict/quarantine gate using the canonical policy block; verify precedence tests
- [ ] 2.2 Implement class-specific outcomes for named identities, approved world terms, chapter-local records, generic roles/objects, possessive props, and anonymous descriptions; verify required negative/positive matrix and stable rejection codes
- [ ] 2.3 Promote validated named anchors independently of claims while allowing only individually `verified` alias/gender/fact claims; verify claimless named characters pass and `candidate` claims stay audit-only
- [ ] 2.4 Merge normalized duplicates without using Russian-target equality as identity, preserve explicit `Dowght`/`Dowghty` policy, and reconcile stale `canonical_ru` only from glossary authority; verify conflict and provenance fixtures

## 3. Migration and production transaction

- [ ] 3.1 Implement deterministic dry-run classification of every current input record as retain/merge/move-to-term/reject; verify every source key is accounted for exactly once and second-run output is canonically identical
- [ ] 3.2 Build a publishable candidate directory containing exactly the four canonical files (migrated v2 memory, rebuilt v2 index, glossary, and observations), plus a separate non-publishable transaction envelope containing manifest, expected parent revision, approval identity, and hashes; verify the candidate exact-four-file boundary and negative matrix for extra files/dirs, symlinks, special files, tampering, and stale parent
- [ ] 3.3 Implement Media-authoritative migration publication through existing lease/parent/CAS and post-publication current/hash verification; verify no direct historical/live-file mutation path exists
- [ ] 3.4 Implement rollback candidate generation from the retained pre-migration snapshot and verify rollback publishes only as a new revision, never by rewriting history
- [ ] 3.5 Produce the complete dry-run decision manifest and candidate hashes, then stop at an explicit owner-approval gate; verify no production publication can occur without approval of that exact manifest/hash set

## 4. Transactional runtime promotion

- [ ] 4.1 Stage glossary, book memory, chapter index, and observations as one same-filesystem candidate bundle with exact-set/schema/hash validation; verify observations clear only in the committed bundle
- [ ] 4.2 Add durable transaction marker, pre-state backups, deterministic replacement progress, fsync, post-hash verification, and startup/pre-publish recovery; fault-inject before/after each of four replacements and each verification boundary
- [ ] 4.3 Replace sequential category mutation in `MemoryManager.promote()` with the complete-state transaction or guaranteed full restoration; verify combined glossary+memory promotion cannot leave category-partial state

## 5. Independent modes and defaults

- [ ] 5.1 Add independent book-memory policies `promote_verified` (default), `observe`, and `off`, removing `glossary_sidecar_handled` as a memory gate; verify each policy controls only book-memory mutation
- [x] 5.2 Change glossary resolver default to `promote` while preserving explicit `shadow` and `off`; verify D6 supersession, CLI/config precedence, requested/effective provenance, and unchanged reviewer transport/budgets
- [ ] 5.3 Implement glossary `off` status-only behavior (zero calls, no proposal sidecar) and `shadow` proposal-without-mutation behavior; verify artifacts and glossary hashes
- [ ] 5.4 Add the full 3×3 glossary/book-memory mode matrix, including glossary-promote+memory-off and glossary-off+memory-promote; verify only the policy-owned resource mutates and both observation-only modes preserve all four canonical hashes
- [ ] 5.5 Preserve all existing resolver identity/hash/model/backend/evidence/quarantine/duplicate/conflict gates under default promote; verify stale, tampered, missing, quarantined, and mixed valid/invalid proposal cases

## 6. Causal index and prompt views

- [ ] 6.1 Rebuild v2 indexes from pre-chapter memory using only fields and aliases with provenance strictly earlier than the target chapter; verify later-learned alias/attribute backward-leakage tests
- [ ] 6.2 Render separate Characters, Named entities, Terms, Facts, and Address scopes while excluding chapter-local objects/generic descriptions; verify prompt snapshots for chapters 0001, 0029, and 0033
- [ ] 6.3 Preserve full current-chapter B1.2 entity context for B3, including object-identity candidate evidence, while keeping translator BIBLE narrow; verify translator and auditor receive intentionally different views
- [ ] 6.4 Bind index schema/policy/selected-entry hashes to run/cache identity and invalidate/rebuild on policy change; verify stale v1/foreign v2 indexes fail soft without full-memory fallback

## 7. Reports and integration

- [ ] 7.1 Implement versioned `book_memory_candidates_report.json` with all required identities, versions, effective modes, terminal status, counts, decisions, evidence, and rejection codes; verify disabled, empty, all-rejected, invalid-identity, and promoted cases are distinguishable
- [ ] 7.2 Add public book-run integration tests from B1.2 through candidate reporting, transaction staging, promotion, and index rebuild for named entities, generic objects, verified/candidate claims, quarantine, and foreign caches
- [ ] 7.3 Add crash/recovery integration tests proving no run or Media publish proceeds while an incomplete transaction marker exists
- [ ] 7.4 Add compatibility tests for snapshot factory, renderer, index builder, mixed-script allowlist, B3 narrator/entity contexts, and existing four-file snapshot clients

## 8. Verification and rollout readiness

- [ ] 8.1 Run focused memory/index/B1.2/B3/mode/transaction tests, then the applicable full pytest suite; verify no production pipeline or model-server lifecycle action is launched on media
- [ ] 8.2 Run fidelity lint, git hygiene, and `openspec validate --all --strict`; verify focused diff, no secrets, and no untracked production artifacts
- [ ] 8.3 Independently review the implementation, complete migration manifest, candidate hashes, recovery tests, and rollback procedure; verify `pact-rev` approval before requesting production migration approval
- [ ] 8.4 After separately approved publication, verify Media current revision and exact four-file hashes, and stop before pipeline execution unless the owner separately approves the controlled run
