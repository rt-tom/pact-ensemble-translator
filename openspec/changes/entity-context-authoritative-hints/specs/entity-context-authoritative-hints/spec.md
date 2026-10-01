## Purpose

Reduce repeated B1.2 analysis of already-known entities without turning glossary/book-memory content into evidence or hiding new chapter-local facts.

## ADDED Requirements

### Requirement: Only source-matched authoritative hints reach the extractor

Before B1.2, the system SHALL deterministically select a bounded card from the frozen **pre-chapter** glossary and durable book memory. Only English names, aliases or terms matched by word boundary in the current chapter's source SHALL be eligible. Unrelated bible facts, current-chapter observations, candidate-only records, excluded/conflicting entries and entries without a source match SHALL NOT appear. For an eligible record the card MAY contain canonical English identity and matched English source surface only; Russian translations and prose facts SHALL remain in downstream stages. The card SHALL label entries as prior hints, never as source evidence. Full `bible_text`/`book_memory.json`/`glossary.json` SHALL NOT be appended. Selection/rendering SHALL perform no model calls, use a deterministic order and hard size/entry cap, and report truncation rather than silently claiming full coverage.

#### Scenario: Known and unknown names in the same chapter
- **WHEN** the source includes one previously known name and one newly introduced entity
- **THEN** only the source-matched known record SHALL enter the prior-hint card and the new entity SHALL remain eligible for normal extraction

#### Scenario: Conflict or absent source match
- **WHEN** a glossary term conflicts with a memory canonical form, is candidate-only, or has no word-boundary match in the chapter
- **THEN** it SHALL be excluded from hints; a conflict SHALL be diagnosable and no old fact SHALL be presented as chapter evidence

### Requirement: Source-only entity evidence remains mandatory

The extractor SHALL receive the full current-chapter English PID map and MAY use matched prior hints to orient attention. It SHALL NOT treat a hint as an anchor, alias, gender or relation witness; every `verified` claim still requires current-chapter verbatim PID evidence and the existing validators. Existing entities SHALL NOT be skipped wholesale: new aliases, contradictions, meanings or chapter-local relations SHALL remain discoverable. The JSON schema, `verified`/`candidate` distinction, Tier-B treatment of semantic coreference, hard filters and promotion gates SHALL remain intact. With an empty card the extractor SHALL retain its source-only behavior.

#### Scenario: A known entity gains a new alias
- **WHEN** a known identity is hinted but the chapter supplies a new alias with verbatim PID evidence
- **THEN** extraction SHALL still report the new alias with only the status the current source justifies

#### Scenario: Prior memory contradicts chapter source
- **WHEN** a hint conflicts with the current source or supplies a fact not evidenced there
- **THEN** it SHALL NOT yield a `verified` claim or automatic promotion on the strength of the hint

### Requirement: Hint-dependent cache identity and same-chapter replay

A non-empty rendered hint card and its prompt variant/version SHALL be bound to the B1.2 extraction cache key/identity. A changed relevant glossary or memory hint SHALL cause recomputation for the same source; an irrelevant state change that leaves the rendered card identical SHALL not. Generation prepass and B3 audit replay of one chapter SHALL use the SAME frozen card, preserving a zero-extra-model-call cache hit. Empty-card source-only output MAY retain the existing source-only cache key only when its actual prompt and validation contract remain byte-for-byte equivalent. Foreign/tampered cache entries SHALL still fail closed; no persistent memory/glossary schema migration is allowed.

#### Scenario: Relevant prior form changes
- **WHEN** the source is unchanged but the card's canonical identity or source-matched alias changes
- **THEN** a previous extraction result SHALL not be reused under the new card identity

#### Scenario: Same chapter prepass and B3
- **WHEN** B3 audits after the generation prepass with unchanged frozen hints
- **THEN** it SHALL reuse the validated cached extraction and issue no second entity model call

### Requirement: Efficiency is measured rather than assumed

Tests SHALL characterize deterministic selection, cap/truncation, source-only evidence, cache identity and resume, no-hint fallback, contradictory state and glossary conflicts. Production speedup SHALL not be claimed from unit tests: any owner-approved A/B trial SHALL compare per-call input/reasoning/output tokens and extraction quality (new aliases, verified/candidate status, hard-audit outcomes) on comparable chapters with separate output/state; agents SHALL NOT launch a pipeline as part of this change.

#### Scenario: Card adds input but does not save reasoning
- **WHEN** an A/B trial shows more total tokens or reduced source-grounded quality
- **THEN** the optimization SHALL not be described as faster or higher-fidelity without another owner decision
