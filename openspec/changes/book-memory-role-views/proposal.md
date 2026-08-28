## Why

The default `v4_run book --chapters … --local|--remote` whole-chapter path is meant to improve translation quality across chapters, but its durable memory is currently both unsafe to promote and unevenly distributed. The translator receives a chapter BIBLE, while the Russian editor, repairer, and glossary resolver lack the small established constraints that prevent inconsistent names, gender, address forms, and term translations. Conversely, the source-only extractor and formatting stage must not be contaminated by prior-book assertions.

The existing `book-memory-hygiene` capability supplies the v2 causal/provenance contract; this change makes its actual writer and its real default-book consumers serve the translation-consistency goal without creating a second knowledge store or unconditional new model calls.

## What Changes

- Correct durable-memory population from B1.2: normalized cross-section identity merge (including pre-existing compatible duplicates), class-based section routing, verified-claim-only promotion, provenance-preserving fact updates, and explicit no-op/conflict reporting.
- Introduce a role-aware, causal renderer over the existing canonical `book_memory` and `chapter_index`; it produces bounded prompt text, not new durable state.
- Resolve established Russian forms against the frozen authoritative glossary with glossary > `book_memory.canonical_ru`; exclude and diagnose glossary/memory conflicts instead of silently picking one.
- Compute one pure relevance selector once per chapter (reusing the existing causal source-relevance logic); every role view is a projection of it, and the glossary view additionally intersects it with resolver candidates.
- Bound each role view by a deterministic per-role token/card budget and include the audit/repair card in existing audit input-budget accounting so cards cannot silently add audit chunks or retries.
- Keep the current-chapter verified B1.2 block separate from the causal durable role view; generation composes both as labelled sections with distinct identities.
- Wire the resulting views into the actual whole-chapter book flow: generation, R-stage Russian editor, B3 audit, selective repair/re-audit, and glossary resolver.
- Keep B1.2 extraction source-only, keep formatting memory-free, and preserve deterministic promotion. Do not add a hypothesis ledger, semantic-merge curator, new standalone model stage, or new audit issue category.
- Make prompt-view versions, selected causal entry, resolved glossary slice, and rendered-view hashes identity-bearing for the model calls and caches they affect; produce inspectable context/provenance reports including conflict diagnostics.
- Validate the design against the representative failed book run and a representative quality-acceptance matrix: targeted name/gender/address/term inconsistencies removed, no new source-fidelity errors, no false constraints from candidate/local anaphora, and token/call budgets respected.

## Capabilities

### New Capabilities
- `book-memory-role-views`: Bounded, causal role-specific book-memory contexts for the actual default whole-chapter generation, editing, audit, repair, and glossary stages.

### Modified Capabilities
- `book-memory-hygiene`: Deterministic canonical writer behavior, verified-only claims, normalized cross-section merge, conflict/no-op outcomes, and provenance-rich reporting needed to populate safe role views.

## Impact

Affected code includes B1.2-to-memory observations, `MemoryManager` merge/index construction, BIBLE/context renderers, whole-chapter strict wiring, B3 editor/audit/repair plumbing, glossary-resolver prompt rendering, prompt/cache identities, and candidate/context reports. The work is high risk because it changes persistent canonical state and prompt inputs, but it does not change provider routing, start a model server, or require a production migration/pipeline run. Any migration or run remains separately owner-approved.

Deployment: developed on a branch off `main` and NOT merged to `main`; it ships as a new dev version **v4.2** alongside the unchanged production **v4.1**. A separate v4.2 runtime profile lets the owner test it without disturbing 4.1. Effectiveness is treated as unproven until the owner tests v4.2 on a representative run.
