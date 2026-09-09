## MODIFIED Requirements

### Requirement: Versioned durable-memory provenance
`book_memory.json` SHALL identify schema `pact-v4-book-memory/v2` and `book_memory_policy_version`. Every durable record SHALL carry its memory class and first-seen chapter. Each promoted alias and mutable attribute SHALL carry independent chapter and evidence-PID provenance; facts SHALL retain chapter, keys, status, and evidence PIDs. Existing canonical translations SHALL remain subordinate to the authoritative glossary. Provenance SHALL remain audit metadata and SHALL NOT by itself make a durable record, attribute, fact, or verified alias ineligible for a chapter index or role view.

#### Scenario: Alias is learned later
- **WHEN** an entity is first seen in chapter N and an alias is verified in chapter M where M>N
- **THEN** the alias provenance SHALL identify M and the alias MAY participate in selection for any chapter whose current source satisfies the applicable presence rule, regardless of the ordering of that chapter and M

#### Scenario: Stale canonical translation exists
- **WHEN** memory `canonical_ru` conflicts with the authoritative glossary
- **THEN** migration/promotion SHALL reconcile memory to the glossary or record a conflict; memory SHALL NOT overwrite the glossary

### Requirement: Versioned causal chapter index
`chapter_index.json` SHALL use schema `pact-v4-chapter-index/v2` with top-level schema and policy-version metadata and per-chapter entries containing exactly the prompt scopes `characters`, `named_entities`, `terms`, `facts`, and `address`. Selection SHALL use the full accumulated durable `book_memory`, with no eligibility restriction based solely on whether a record, field, fact, or alias has provenance strictly before the target chapter. Characters, named entities, and durable terms SHALL require a canonical name or verified alias surface in the current chapter source; facts SHALL require a relevant explicit or deterministically-derived key in that source; and address forms SHALL require a relevant non-narrator participant in that source. Alias matching SHALL use all verified durable aliases. Existing narrator and glossary-conflict locks, plus existing seed/global-voice behavior in their render/role-view consumers, SHALL remain explicit exceptions; normal source-presence selection SHALL NOT become a full-memory fallback. Missing/unknown v2 metadata SHALL fail soft to narrator plus seed facts; it SHALL NOT trigger a full-memory fallback.

The index hash and generation/cache identity SHALL bind to the selected v2 chapter entry and policy version. Older readers SHALL either ignore reserved top-level metadata and consume compatible fields or reject the v2 index explicitly; they SHALL NOT silently flatten named entities/terms into characters.

#### Scenario: Later-derived record remains bounded by source presence
- **WHEN** a durable record or verified alias has provenance at or after the target chapter but its applicable source surface is absent and it is not an explicit existing exception
- **THEN** the chapter index SHALL omit it

#### Scenario: Prompt-safe scopes render
- **WHEN** a chapter source contains a previously known durable character, named entity, or approved term
- **THEN** the v2 index SHALL place it in the corresponding scope and the BIBLE SHALL render it under that scope rather than as an undifferentiated character

#### Scenario: Invalid index version is encountered
- **WHEN** a chapter run receives a missing, foreign, or unsupported index schema/policy version
- **THEN** it SHALL render only narrator plus seed facts and SHALL record the fail-soft reason
