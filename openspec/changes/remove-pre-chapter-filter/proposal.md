## Why

The default whole-chapter book flow builds a per-chapter bible / role-view from durable `book_memory` via `build_chapter_index` (and `select_relevant` for v4.2). Today SAFE-MEMORY provenance gates remove facts and durable records before selection when their provenance is not strictly earlier than chapter N; a separate variant gate rejects aliases learned in N or later. This was meant to prevent a rerun from seeing its own promoted facts and to prevent later-chapter knowledge from appearing in an earlier chapter.

For out-of-order book builds and re-translation this restriction defeats the cumulative-bible goal. The owner already translated chapters 1–30, then reset the run pointer to chapter 1 while keeping the valuable bible records accumulated in `book_memory`. Because many records first carry provenance at or after the current chapter, the pre-chapter filter can leave early chapters with no non-exception context — chapter 1 reproduced this as narrator-only context. The owner wants each chapter to draw relevant context from the **full** accumulated `book_memory`, while keeping the source-presence boundary that prevents a full-memory dump.

Owner decisions (2026-08-28):
1. Remove the provenance-before-N restriction **globally** (shared `main` code; affects 4.1 and the v4.2 consumer when enabled).
2. Accept that a rerun may re-inject the chapter's own previously-promoted facts (self-reference) and that earlier chapters may see later-chapter facts (future-leakage). Both are acceptable because durable memory is source-derived and remains source-prevails/verifiable.
3. Make this the permanent 4.2-era / production behavior.

## What Changes

- Remove Rule 2, the provenance-before-N gate, from `pre_chapter_book_memory`, `_variants_with_provenance`, and the `world_term` branch in `build_chapter_index`.
- **Keep Rule 1, source-presence selection:** characters, named entities, and durable terms are selected by a canonical name or verified alias surface in the current chapter source; facts by a relevant explicit/deterministically-derived key in that source; and address forms by a relevant non-narrator participant in that source. The bible remains a bounded chapter-relevant slice, not a full `book_memory` dump.
- Preserve existing explicit exceptions without broadening them: narrator and glossary-conflict locks in `chapter_index`; seed/global-voice handling in the consumers that already render them; and durable-conflict exclusion in v4.2 role views.
- `pre_chapter_book_memory` becomes a non-filtering top-level shallow copy of the full `book_memory`, so stored `chapter_index.json`, v4.2 `select_relevant`, and promotion index rebuilds all receive the complete durable state.
- Keep unknown schema/policy fail-soft behavior (narrator + seed facts only); it remains distinct from ordinary full-memory selection.
- Update stale causal `< N` comments/docstrings and replace the backward-leakage tests. Modify both relevant `book-memory-hygiene` contract requirements. The already-pending `book-memory-role-views` change is reconciled in its own artifact, rather than making this change depend on an invalid cross-change delta.

## Capabilities

### Modified Capabilities
- `book-memory-hygiene`: durable provenance remains recorded for auditability, but it no longer gates chapter-index eligibility. Chapter-index selection is presence-based over the full accumulated `book_memory`.

## Impact

High-risk shared selection contract: `pact_full_pipeline_runner_v1/build_chapter_index.py` and the promotion index rebuild in `pact_v4/phase1/memory.py`; v4.2 role views consume the same selection path when their existing feature gate is enabled. `book_memory_role_views.py` and `bible_renderer.py` require stale-contract documentation/test reconciliation but no new model call, provider routing, persistent format, migration, or pipeline run. Any production state change, migration, or run remains separately owner-approved. This change is separate from the v4.2 role-view implementation and is intended for global, permanent rollout only after review and owner approval.
