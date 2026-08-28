# Design: Remove pre-chapter provenance filter from chapter bible selection

## Current behavior to remove

SAFE-MEMORY applies Rule 2 (provenance must be strictly earlier than chapter N) at three functional points:

- `pre_chapter_book_memory(bm, chapter_id)` rebuilds `facts`, `characters`, and `entities`, dropping facts from N/later and records without an earlier `chapters` provenance.
- `_variants_with_provenance(bm, section, name, chapter_id)` suppresses aliases whose own provenance chapter is N/later, so source presence ignores later-learned aliases.
- The `world_term` branch in `build_chapter_index` retains a term only if at least one record chapter is strictly before N.

`phase1/memory.py` invokes the same helper when it rebuilds current/next index entries during promotion. `select_relevant` uses the helper before calling `build_chapter_index`, so v4.2 inherits the same filter.

`bible_renderer.py` does not itself apply a provenance filter: it renders the selected index entry and already fails soft rather than dumping full memory for a missing/invalid entry. Its causal `< N` documentation becomes false after this change and must be revised without weakening its index-only/fail-soft behavior.

## New selection contract

For chapter N, select from the full accumulated durable `book_memory` with no provenance-before-N eligibility gate:

- characters, named entities, and durable terms require a canonical name or verified variant present in the current source;
- facts require a relevant explicit key, or the existing deterministic key derivation, present in the current source;
- address forms require a relevant non-narrator participant present in the current source.

This is Rule 1 and remains the normal boundary. Existing narrator and glossary-conflict locks in the chapter index, seed/global-voice exceptions in their existing render/role-view consumers, and v4.2 durable-conflict exclusion remain unchanged. Thus a rerun may use its own already-promoted source-derived memory and an early chapter may use a later-promoted source-derived record, but unrelated memory still does not enter merely because it exists.

## Implementation sites

1. `pact_full_pipeline_runner_v1/build_chapter_index.py`
   - Make `pre_chapter_book_memory(book_memory, chapter_id)` a non-filtering **top-level shallow copy**: `dict(book_memory)`. Keep its signature, preserve every section and provenance field, and do not mutate the caller's mapping.
   - Make `_variants_with_provenance(...)` delegate to `_variants_for(...)`; retain its public signature while making `chapter_id` non-gating.
   - Remove the `_chapter_before` condition from the `world_term` branch. A stored `world_term` uses the same source-presence rule as other durable terms but remains gated by `policy.approved_terms` — only an approved world_term whose canonical name or verified variant is present in the source is selected (existing approved-terms-only legacy path retained).
   - Keep the schema/policy fail-soft and the existing narrator/glossary-conflict behavior exactly as-is.
   - Update function/module/doc comments that promise pre-N filtering; remove obsolete `_field_provenance_before` only if it remains unused after the change.
2. `pact_v4/runtime/book_memory_role_views.py`
   - `select_relevant` receives the full shallow copy through the existing helper and retains its one selector / `_term_present` path. No independent selection algorithm is added.
   - Update `AuthoritativeState`, `_project_records`, and selector docstrings/variable wording from “pre-chapter” to canonical full-state selection.
   - Preserve `_is_excluded` / `_excluded_conflict` handling: conflict exclusion is independent of provenance and must continue to exclude ambiguous records from role views.
3. `pact_v4/phase1/memory.py`
   - Promotion rebuilds current/next entries through the unchanged helper call; revise comments that call these pre-N entries. No transaction or persistent-data behavior changes.
4. `pact_v4/runtime/bible_renderer.py`
   - Revise only obsolete causal `< N` documentation/comments. Preserve index-only rendering, no-full-memory fallback, and the existing seed-fact render behavior.

## Contract reconciliation

- Modify `book-memory-hygiene` » `Versioned durable-memory provenance`: provenance remains mandatory audit metadata; an alias learned in M is no longer barred from a chapter N index solely because N≤M.
- Modify `book-memory-hygiene` » `Versioned causal chapter index`: replace the provenance gate with the scope-specific source-presence contract above.
- The pending `book-memory-role-views` change has already been reconciled in its own spec so it no longer requires a pre-chapter state or the removed backward-leak guarantee. The separate artifacts avoid a cross-change MODIFIED delta against a capability not yet in the base spec.

## Tests

- Add synthetic, versioned unit fixtures that model the representative out-of-order state; do not read, copy, or mutate the production book-state store during tests.
- Verify `pre_chapter_book_memory` preserves the complete mapping semantically while returning a distinct top-level mapping.
- Verify a later-learned alias, a fact attributed to N, and a `world_term` first recorded in N are selected when their applicable Rule 1 source surface is present; verify an unrelated absent record is not selected.
- Update the existing causal/backward-leak tests in `test_v2_index_scope_causal.py` and affected `test_a2_chapter_index.py` cases to assert the new behavior.
- Verify v4.2 `select_relevant` includes multiple source-relevant records from full memory while `_excluded_conflict` still suppresses a conflicted record.
- Verify missing/foreign schema or policy still fails soft, and `render_bible_section` still never falls back to a full-memory dump.
- The owner may separately perform a read-only representative check against rev-0012 / chapter 1; it is not a test fixture or a pipeline launch.

## Risks accepted by owner

- Self-reference on rerun and future-leakage are intentional, limited by Rule 1 and source-prevails instructions.
- Broader relevant context can change translation/audit/repair/glossary prompts; this is the intended quality intervention and requires representative owner evaluation.
- No provider routing, model-server operation, persistent-data migration, or production pipeline execution is in scope.
