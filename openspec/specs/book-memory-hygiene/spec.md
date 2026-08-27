## Purpose

Keep durable book memory small, causal, transactionally safe, and prompt-safe while retaining complete chapter-local entity evidence for audit and glossary resolution.

## ADDED Requirements

### Requirement: Versioned B1.2 memory classification
Each B1.2 entity response SHALL contain `memory_class` with exactly one value from `named_character`, `named_place`, `named_group`, `named_artifact`, `named_creature`, `world_term`, or `chapter_local`, plus boolean `memory_worthy`. The extractor prompt/version, response validator, validation report, cache schema/key, and run provenance SHALL identify this contract. Missing, unknown, or malformed classification SHALL fail validation; an older cache SHALL be rejected and recomputed rather than assigned a default.

#### Scenario: Current classified entity is accepted
- **WHEN** the current extractor returns a valid `memory_class` and boolean `memory_worthy` with otherwise valid source evidence
- **THEN** the entity SHALL continue to the deterministic durable-memory gate

#### Scenario: Old cache has no classification
- **WHEN** a cached B1.2 record predates the classification contract or omits either field
- **THEN** it SHALL NOT be used for memory promotion and SHALL be recomputed under the current extractor/cache version

### Requirement: Exact durable-memory eligibility
The durable-memory gate SHALL evaluate inputs in this precedence order: validated current chapter/source identity; explicit deny override; explicit allow/alias override; model veto; class-specific code checks; duplicate/conflict checks. `memory_worthy=false` SHALL veto promotion. `memory_worthy=true` SHALL not be sufficient by itself.

Named classes SHALL require a boundary-matched proper-name surface in source and SHALL reject generic-role/object patterns. `world_term` SHALL require exact membership in the approved-term policy. `chapter_local` SHALL always be rejected. A validated anchor is sufficient to establish an eligible named entity; only each additional alias, gender, or fact requires its own `verified` claim.

The versioned policy SHALL live in the canonical `book_memory.json` policy block and SHALL define explicit deny overrides, explicit allow/alias overrides, approved world terms, generic-role/object patterns, and `book_memory_policy_version`.

Stable rejection codes SHALL include at least `invalid_identity`, `explicit_deny`, `model_veto`, `chapter_local`, `generic_role`, `generic_object`, `term_not_approved`, `duplicate`, `conflict`, `candidate_claim`, and `quarantined_evidence`.

#### Scenario: Named character with no claims is eligible
- **WHEN** B1.2 validates a proper-name character anchor, classifies it as `named_character`, sets `memory_worthy=true`, and no higher-precedence rule rejects it
- **THEN** the identity SHALL be eligible even if it has no gender, alias, or other claim

#### Scenario: Generic scene object is rejected
- **WHEN** B1.2 extracts `car`, `mirror`, `coat`, or a matching generic object as `chapter_local` or a generic-object pattern matches
- **THEN** it SHALL remain in chapter entity context and SHALL be reported as rejected without durable promotion

#### Scenario: Approved world term is eligible
- **WHEN** a source-valid entity is classified `world_term`, sets `memory_worthy=true`, and its normalized surface is in the versioned approved-term policy
- **THEN** it SHALL be eligible for the durable term scope

#### Scenario: Candidate relation is withheld
- **WHEN** an alias, gender, or object-identity claim is marked `candidate`
- **THEN** that claim SHALL NOT become a durable alias, attribute, fact, or repair instruction even if the entity identity itself is eligible

### Requirement: Versioned durable-memory provenance
`book_memory.json` SHALL identify schema `pact-v4-book-memory/v2` and `book_memory_policy_version`. Every durable record SHALL carry its memory class and first-seen chapter. Each promoted alias and mutable attribute SHALL carry independent chapter and evidence-PID provenance; facts SHALL retain chapter, keys, status, and evidence PIDs. Existing canonical translations SHALL remain subordinate to the authoritative glossary.

#### Scenario: Alias is learned later
- **WHEN** an entity is first seen in chapter N and an alias is verified in chapter M where M>N
- **THEN** the alias provenance SHALL identify M and SHALL NOT be available to indexes for chapters at or before M

#### Scenario: Stale canonical translation exists
- **WHEN** memory `canonical_ru` conflicts with the authoritative glossary
- **THEN** migration/promotion SHALL reconcile memory to the glossary or record a conflict; memory SHALL NOT overwrite the glossary

### Requirement: Deterministic duplicate normalization
Equivalent durable records across sections SHALL be resolved using normalized Unicode, case, apostrophe, and punctuation forms plus explicit alias policy. The merge SHALL preserve per-field provenance and SHALL never infer semantic equivalence from equal Russian targets alone.

#### Scenario: Cross-section duplicate is merged
- **WHEN** the same normalized named identity exists in `characters` and `entities`
- **THEN** one canonical durable record SHALL remain with merged non-conflicting provenance

#### Scenario: Intentional source variants share translation
- **WHEN** policy explicitly relates `Dowght` and `Dowghty`
- **THEN** both source surfaces and their provenance SHALL be preserved while their approved Russian translation remains identical

### Requirement: Versioned causal chapter index
`chapter_index.json` SHALL use schema `pact-v4-chapter-index/v2` with top-level schema and policy-version metadata and per-chapter entries containing exactly the prompt scopes `characters`, `named_entities`, `terms`, `facts`, and `address`. Items that carry attributes SHALL include only fields whose provenance is strictly earlier than the target chapter. Alias matching SHALL use only aliases learned strictly before the target chapter. Missing/unknown v2 metadata SHALL fail soft to narrator plus seed facts; it SHALL NOT trigger a full-memory fallback.

The index hash and generation/cache identity SHALL bind to the selected v2 chapter entry and policy version. Older readers SHALL either ignore reserved top-level metadata and consume compatible fields or reject the v2 index explicitly; they SHALL NOT silently flatten named entities/terms into characters.

#### Scenario: Later alias cannot leak backward
- **WHEN** an alias or attribute was first verified in chapter M
- **THEN** rebuilt indexes for chapters N≤M SHALL omit that alias or attribute

#### Scenario: Prompt-safe scopes render
- **WHEN** a chapter source contains a previously known durable character, named entity, or approved term
- **THEN** the v2 index SHALL place it in the corresponding scope and the BIBLE SHALL render it under that scope rather than as an undifferentiated character

#### Scenario: Invalid index version is encountered
- **WHEN** a chapter run receives a missing, foreign, or unsupported index schema/policy version
- **THEN** it SHALL render only narrator plus seed facts and SHALL record the fail-soft reason

### Requirement: Versioned candidate report
Each chapter SHALL write `book_memory_candidates_report.json` using a versioned schema. It SHALL include chapter ID, source/snapshot/config hashes, extractor/cache/policy versions, effective glossary and book-memory modes, terminal status, B1.2 entity count, and accepted/rejected/duplicate/conflict entries with class, evidence PIDs, and stable reason codes. Missing or foreign reports SHALL never be interpreted as a successful zero-candidate run.

#### Scenario: All candidates are rejected
- **WHEN** B1.2 returns entities but none pass the durable gate
- **THEN** the report SHALL show a nonzero entity count, zero eligible/promoted count, and a rejection entry for each entity

#### Scenario: Entity artifact is foreign
- **WHEN** B1.2 identity does not match the current chapter/source/config
- **THEN** the report SHALL record `invalid_identity`, durable state SHALL remain unchanged, and the run SHALL not report a normal empty extraction

### Requirement: Transactional canonical state update
Any runtime promotion or migration that can change canonical state SHALL stage and validate all four files—`glossary.json`, `book_memory.json`, `chapter_index.json`, and `observations.json`—as one versioned candidate bundle. Local replacement SHALL use a transaction marker containing pre/post hashes, recoverable backups, deterministic replacement order, post-replacement verification, and startup recovery that restores the complete pre-transaction bundle after any interrupted step. No category-partial state SHALL be accepted or published.

#### Scenario: Crash occurs during local replacement
- **WHEN** execution stops after replacing any strict subset of the four canonical files
- **THEN** startup recovery SHALL detect the marker and restore all four pre-transaction files before another run or publish proceeds

#### Scenario: Combined glossary and memory promotion succeeds
- **WHEN** both categories have valid observations
- **THEN** the staged four-file bundle SHALL be validated and committed as one transaction, and observations SHALL be cleared only within that committed bundle

### Requirement: Authoritative Media migration
Production migration SHALL construct all four canonical files in an isolated candidate directory, validate exact file set/types/JSON/hashes and parent revision, and publish through the existing Media lease/parent/CAS gate as one new revision. Historical snapshots SHALL remain immutable. Rollback SHALL publish a new revision built from the retained pre-migration snapshot; it SHALL NOT directly restore tracked live files or rewrite history.

For a migration candidate built from the accepted parent revision (owner clarification 2026-08-27): `glossary.json` is copied byte-for-byte from the accepted parent unless a separately approved glossary change is explicitly part of the same candidate (`book_memory` `canonical_ru` reconciled *to* that parent glossary, never vice versa); `chapter_index.json` MUST be rebuilt from the migrated v2 book_memory/policy (copying retains contaminated v1 flattened-character content and stale hashes); `observations.json` is preserved byte-for-byte from the accepted parent — if nonempty/pending/incompatible with migrated state, migration fails closed and requires explicit owner-approved reconciliation, not silent clearing; the four files remain the exact publishable bundle.

#### Scenario: Owner has not approved migration manifest
- **WHEN** the complete retain/merge/move/reject manifest lacks explicit owner approval
- **THEN** no production candidate revision SHALL be published

#### Scenario: Candidate parent is stale
- **WHEN** Media current revision differs from the migration candidate's expected parent
- **THEN** publication SHALL fail closed without changing current state

#### Scenario: Post-publication verification fails
- **WHEN** current revision hashes or exact canonical file set differ from the accepted candidate
- **THEN** further pipeline execution SHALL stop and rollback SHALL require an explicitly approved new revision from the retained snapshot
