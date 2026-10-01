## Why

The B1.2 `entity_extractor` currently sees the full English chapter source but no previous book-memory or glossary entries. The owner observes that it spends substantial reasoning/output tokens rediscovering names and terms already present in authoritative state. Passing the full rendered bible or glossary verbatim would add input tokens and risk treating stale memory as source evidence; any optimization must preserve source-only verification and audit safety.

## What Changes

- Before the B1.2 prepass, construct a compact, deterministic hint card from the **pre-chapter authoritative** `book_memory` and glossary: only approved names/aliases/terms with a word-boundary match in the current English source, excluding unresolved conflicts and candidate-only observations. Include only source surfaces and canonical English identity; Russian translations remain in downstream glossary/bible stages, not the English-source extractor. Never send the full bible, all facts or unrelated entries.
- Send the card to the `entity_extractor` as labeled **prior context, never evidence**. The model must still ground every anchor, alias, gender or relation it emits in verbatim PIDs of this chapter. Known terms must not be blindly skipped: newly appearing aliases, senses and relationships remain discoverable, and existing schema/validators and hard audit filters remain unchanged.
- Bind the exact rendered card (including empty-card behavior) and extractor prompt version to the extraction cache identity, so a changed glossary/memory hint cannot reuse stale source-only output. Preserve the original source-only behavior when there are no relevant hints, without relaxing foreign/tampered-cache checks or changing persistent memory formats.
- Measure per-call input/reasoning/output tokens and verified/candidate entity quality against a comparable source-only baseline before claiming speedup. No pipeline run by agents; any live A/B run requires a separate owner decision and isolated output/state.

## Capabilities

### New Capabilities
- `entity-context-authoritative-hints`: bounded source-matched prior hints for source-verified B1.2 extraction, cache-safe and measurable.

## Impact and approval boundary

High risk: B1.2 prompt and cache identity, whole-chapter prepass/B3 replay, glossary and book-memory fidelity. Expected code seams are `pact_v4/audit/entity_extractor.py`, `pact_v4/pipeline/b3_audit_repair.py`, `pact_v4/pipeline/v4_phase12_strict_runner.py`, deterministic relevance helpers and focused tests. This proposal does **not** approve implementation, memory migration, RT edits, model-server operations, pipeline execution or deployment. Owner approval of proposal/spec/design is required before code changes.
