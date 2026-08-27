## Why

`book_memory.json` currently mixes durable story knowledge with chapter-local objects and generic role descriptions. B1.2 validates source evidence, but the current promotion path can persist entries such as `car`, `mirror`, and `old man`; the chapter index can then expose them to later translation prompts as characters. Glossary resolver mode is also incorrectly coupled to book-memory promotion, so glossary `off` suppresses otherwise valid B1.2 memory observations.

We need a versioned durable-memory contract, transactional migration, and independent mode policies before making glossary `promote` the default. Runs must remain inspectable while only durable, prompt-safe records are promoted.

## What Changes

- **BREAKING** Introduce versioned `book_memory` classification and per-field/per-alias provenance; old unclassified B1.2 caches are rejected and recomputed.
- **BREAKING** Introduce a versioned, type-aware `chapter_index` contract that separates characters, named entities, terms, facts, and address forms while preserving causal pre-chapter filtering.
- Add an exact durable-memory eligibility gate and stable rejection codes, independent from glossary worthiness.
- Migrate current state through the authoritative four-file Media candidate-revision transaction (`glossary.json`, `book_memory.json`, `chapter_index.json`, `observations.json`), with dry-run manifest, owner approval, CAS/parent validation, recovery, and rollback by publishing a new revision.
- Preserve chapter-local B1.2 evidence for B3 while preventing ordinary objects, generic roles, and unresolved descriptions from entering durable prompt memory.
- Add transaction staging/recovery for runtime promotions so glossary and book-memory mutations cannot leave category-partial local state.
- Decouple book-memory policy from `glossary_resolver_mode`.
- Change glossary resolver default to `promote`; preserve explicit `off` and `shadow` overrides.
- Record policy versions, effective modes, candidate decisions, identities, and hashes in versioned reports/provenance.
- Explicitly supersede `glossary-model-resolver/design.md` Decision D6 only with respect to the default rollout state: the prior default `off` becomes `promote`; all existing sidecar, model-budget, identity, quarantine, conflict, and fail-closed requirements remain authoritative.

## Capabilities

### New Capabilities

- `book-memory-hygiene`: Versioned durable-memory classification, transactional migration/promotion, prompt-safe causal indexing, and independent book-memory policy.
- `glossary-resolver-default-mode`: Default `promote` behavior, explicit `off`/`shadow` overrides, and independent glossary/book-memory policies; supersedes the earlier resolver rollout default.

### Modified Capabilities

<!-- The repository has no archived main capability specs yet. The new resolver-default capability explicitly supersedes D6 of the still-active glossary-model-resolver change. -->

## Impact

- Affected code: B1.2 schema/prompt/cache/versioning, entity-to-memory promotion, `MemoryManager` transaction handling, `v4_book_run`, Media snapshot candidate publication, `build_chapter_index`, `bible_renderer`, B3 context plumbing, CLI/config defaults, and reports.
- Affected persistent contracts: `book_memory.json` and `chapter_index.json` gain explicit schema/policy/provenance contracts; four canonical state files are staged and published together.
- Affected runtime state: production migration is a new approved Media revision, never an in-place historical snapshot edit.
- Affected prompts: translator BIBLE becomes narrower and type-aware; B3 retains current-chapter entity evidence.
- Risk: High—persistent state, causal prompt content, default glossary mutation, and recovery behavior change.
