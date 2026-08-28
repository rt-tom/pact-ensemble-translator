## Context

See `proposal.md` and the `book-memory-hygiene` delta. The supported production workflow is the simple whole-chapter book run documented in `docs/architecture/V4_BOOK_PIPELINE_INVENTORY_RU.md`:

```text
B1.2 source prepass → whole-chapter generation → R editor
→ B3 chunked audit → selective repair → re-audit → formatting
→ glossary proposals → deterministic shared-state promotion
```

It is not the historical candidate-selection/cascade path. Current generation receives a rendered BIBLE; B3 audit/repair receive current-chapter entity context plus a narrow narrator fallback; the R editor and glossary-resolver model do not receive established book constraints. The existing v2 book-memory capability defines causal provenance and durable eligibility, but current writer behavior still permits cross-section duplicate keys and routes records by gender rather than class.

## Goals / Non-Goals

**Goals:**

- Make canonical memory improve names, gender, address, and term consistency in the actual default book path.
- Keep one authoritative durable state; role views are ephemeral deterministic renderings.
- Populate that state only from source-validated eligible identities and individually verified claims.
- Resolve established Russian forms against the authoritative glossary (glossary > `book_memory.canonical_ru`) so views never inject a conflicting or stale form.
- Keep role cards bounded by deterministic per-role token/card budgets that are accounted in existing audit input budgeting, so they cannot silently add audit chunks or retries.
- Preserve source authority, causal boundaries, cache correctness, and existing B3 current-chapter evidence.

**Non-Goals:**

- No persistent hypothesis ledger, knowledge graph, semantic alias inference, or model-based curator in this change.
- No new default model call, backend/provider/routing change, or model-server lifecycle action.
- No broad full-memory dump to every role, no source context to the R editor, and no memory context for extractor/formatting.
- No new audit issue category for memory/source disagreements; those are source-prevails diagnostics only.
- No separate relevance engine; the existing causal source-relevance selector is reused once per chapter.
- No production migration, Media publication, or pipeline run without separate owner approval.

## Decisions

### 1. One canonical state, one deterministic population reducer

The authoritative four-file state remains unchanged as the storage boundary. B1.2 remains the source-only chapter graph; it is not rewritten into durable memory wholesale. After a chapter has reached the existing promotion eligibility point, the reducer handles only records that passed the existing durable gate.

For each eligible identity it builds a normalized registry over *all* canonical scopes. The outcome is explicit:

```text
no match              → create in the memory_class scope
one match             → merge evidence/claims into that canonical record
multiple compatible matches
  (same normalized identity or policy-approved explicit alias,
   with non-contradictory verified attributes and Russian forms)
                      → deterministically merge into one canonical record,
                        preserving merged provenance and taking the scope
                        implied by memory_class
multiple incompatible matches
  (contradictory verified attributes or Russian forms)
                      → conflict; recorded, no prompt-visible mutation
same evidence/value   → no_op
```

`memory_class` selects the initial durable scope. Gender is an optional verified attribute, never a routing signal. The reducer admits aliases, facts, genders, canonical Russian forms, and relationships only when the particular claim and its relation are `verified`; a verified surface does not turn a candidate coreference into an alias. An agreeing later claim extends provenance; a disagreement is retained in diagnostics as conflict rather than overwriting or inventing a resolution.

Pre-existing cross-section duplicates from earlier state are not silently rewritten by this change. When a chapter confirms the same normalized identity, the reducer applies the same merge; any record still in an ambiguous conflict state is excluded by the view layer until an explicitly approved resolution, so it cannot appear as an ambiguous constraint in any role view.

This is deliberately not semantic merge. Exact normalized identity and policy-approved lexical variants are deterministic. Ambiguous semantics such as `the young lady → Paige` stay chapter-local. A future curator can be proposed only after evidence shows this conservative boundary materially harms quality.

### 2. Role-aware rendering is an ephemeral compiler

Add one deterministic, pure renderer pipeline. Relevance is computed exactly once per chapter by reusing the existing causal source-relevance logic in `build_chapter_index` as a single pure function over the canonical book_memory state plus the current source map:

```text
select_relevant(authoritative_state, source_map) -> RelevanceResult   # once per chapter
render_book_context(role, relevance, authoritative_glossary,
                    current_b1_2, glossary_candidates) -> RenderedContext
```

Every role view is a projection of the single `RelevanceResult`; the glossary view additionally intersects it with the resolver's current entity candidates. The renderer never recomputes relevance per consumer, so generation, audit, repair, R-editor, and glossary views cannot drift apart.

`render_book_context` resolves each established Russian form from the frozen authoritative glossary. When a glossary entry exists it overrides `book_memory.canonical_ru`; when the two disagree, the conflict is excluded from the prompt text and recorded as a diagnostic rather than silently resolved. The rendered hash includes the resolved glossary slice so a glossary change invalidates replay.

`RenderedContext` contains text, schema/version, included canonical IDs, resolved term-map, and canonical hash. Each role has a deterministic token/card budget. When the selected relevant records exceed the budget, the renderer applies a fixed field/record priority and overflow policy (retain the source-prevails instruction and highest-priority canonical constraints; drop lowest-priority extras) instead of growing unbounded. The audit/repair card is included in the existing audit input-budget accounting, so it cannot silently increase audit chunks or retries; an over-budget card is trimmed, not used to add model calls.

The translator prompt composes two clearly labelled sections: the causal durable role view (drawn from the full accumulated book_memory, selected by presence in the chapter source) and a separate current-chapter verified B1.2 block (permitted only for generation). The B1.2 block is not a durable role view and carries its own identity. The other role cards are small projections:

| Role | Contents | Consumers |
|---|---|---|
| `translator` | Causal durable BIBLE (canonical translation constraints, global voice/address facts) **plus** a separate current-chapter verified B1.2 block | whole-chapter generator/retry |
| `audit_repair` | Source-relevant canonical names, approved lexical variants, established RU forms, verified gender/address/facts; explicit source-prevails instruction | B3 audit, repair, re-audit |
| `russian_editor` | Source-selected established RU names/terms plus grammar-relevant verified gender/address/register; no source text and no plot summary | R-stage Russian editor |
| `glossary` | Established EN→RU form for current resolver candidates only (authoritative glossary wins) | glossary resolver |

A source relevance predicate uses canonical name plus verified *lexical* variants only; it excludes relation labels, generic descriptions, Russian-target equality, and unverified aliases. Narrator/seed/global voice constraints are an explicit exception handled inside `select_relevant` (not by relevance matching): they are always included in every role view from the explicit existing policy, independent of source presence. The concrete per-role token/card budgets and field/record priority from Decision 2 are defined as named constants in code, not prose, so “bounded” is reproducible.

### 3. Wire only the real whole-chapter consumers

The strict/book runner computes `select_relevant` once and uses that single result as the basis for every consumer; it does not recompute relevance per stage. The R editor gets its card even though its prompt remains source-free; the compiler, not the editor, uses source solely to select relevant canonical constraints. The translator prompt composes the causal durable BIBLE with the separate current-chapter verified B1.2 block as two labelled sections. B3 receives the existing complete B1.2 current-chapter entity context unchanged plus the audit/repair card, replacing the insufficient narrator-only fallback. Selective repair receives the same bounded card and current entity/glossary context; re-audit receives the same policy rather than an inconsistent second view. The glossary resolver gets its candidate-limited card, resolved from the authoritative glossary, in addition to its existing source/translation/candidate input.

Entity extraction and formatting receive none. Promotion makes no model call. Optional disabled stages neither render nor require a role card.

### 4. Source wins; memory is a consistency constraint

Audit, repair, and glossary prompts explicitly state that memory is historical context, not source evidence. A known canonical memory/source disagreement is surfaced only as a source-prevails instruction plus a bounded diagnostic in the renderer/provenance output. It does **not** introduce a new audit issue category, and the implementation must not extend the existing audit category set to resolve memory conflicts. This avoids reinforcing an earlier bad memory fact and keeps the audit contract closed.

The R editor receives only material that can safely constrain Russian realization without access to source: established Russian spelling, gender agreement, address/register, and approved terms. It never gets relationship hypotheses or broad narrative facts.

### 5. Context identity and diagnostics are mandatory

Every model/cache identity that currently depends on prompt/config material is extended with its role-view version and rendered-view hash. The rendered-view hash includes the selected causal entry, the resolved glossary slice, and the role schema/version. This includes whole-generation retry/resume, R-editor artifacts, B3 audit/repair/re-audit cache paths, and glossary sidecar validation/input identity as appropriate. A card change cannot silently replay an output produced without it.

Per-chapter provenance records role, state/index hashes, rendered hash, included canonical IDs/count, and empty/disabled/invalid reason. Store hashes/IDs by default rather than duplicating unbounded prompt text; use existing raw-prompt retention where it already exists for diagnosis.

### 6. Rollout is code-and-fixture first

No automatic persistent-state migration is part of this change. Tests use the existing v2/legacy-compatible fixture strategy, and unsupported/malformed state retains the existing fail-soft/fail-closed behavior of the affected consumer. Production use follows normal merge, RT fast-forward synchronization, and a separately approved small manual book run. Rollback is a code revert; no canonical state is altered by merely deploying the feature.

## Risks / Trade-offs

- [Risk] A small relevance filter omits a needed constraint. → Single `select_relevant` reused by every consumer; include exact canonical name/verified lexical aliases plus explicit global constraints; test source forms from the representative run and expose included IDs in diagnostics.
- [Risk] A large card biases audits or wastes context. → Per-role allowlists, source relevance, no candidate relations, and bounded prompt snapshots.
- [Risk] A larger or mis-budgeted card indirectly increases audit chunks/retries. → Fixed per-role token/card budgets included in the existing audit input-budget accounting; over-budget cards are trimmed deterministically rather than expanding calls.
- [Risk] A wrong historical fact causes a wrong repair. → Source-prevails prompt rule, conflict reporting, and no automatic overwrite.
- [Risk] Old unresolved cross-section duplicates leak ambiguous constraints. → The reducer merges compatible duplicates and the view layer excludes any record still in a conflict state.
- [Risk] Cache reuse ignores new context. → Role-view hash/version is identity-bearing for each consuming artifact and includes the resolved glossary slice.
- [Risk] Writer merge regresses durable state. → Exact normalized registry, compatible-merge/no-op/conflict outcomes, provenance-preserving transaction, and current-run regression fixtures including pre-existing duplicates.

## Migration Plan

1. Add reducer and renderer tests against isolated fixtures, including the observed `Callan`, `Paige`, `Peter`, `Stephanie`, `Aunt Irene`, and candidate-anaphora cases.
2. Implement and independently review in an isolated worktree; run focused memory, prompt, B3, R-editor, glossary, and whole-book integration tests.
3. Validate the OpenSpec and obtain owner approval before merge. Deployment only updates code; it does not migrate or publish book state.
4. After separately approved RT sync and a small manual book run, inspect candidate/context reports, final translations, and canonical state delta. Revert code if role cards harm quality or cache behavior.

## Deployment (owner decision)

The owner has decided this enhancement is developed on a branch off `main` and is **NOT** merged to `main`. It ships as a new dev version **v4.2** alongside the unchanged production **v4.1**. Implementation SHALL:

- create/use a branch such as `dev/v4.2-book-memory-role-views` off `main`;
- avoid altering 4.1 behavior on `main`; provide a separate v4.2 runtime profile/config so the owner can run v4.2 without disturbing 4.1;
- keep all changes reviewable on the branch; no `main` merge until the owner separately approves after testing.

Effectiveness is treated as unproven until the owner tests v4.2 on a representative run.

## Open Questions

A semantic-curator model call is intentionally deferred rather than left as an implementation choice in this change. If a representative pilot shows deterministic exact/lexical matching systematically misses important cross-chapter alias/merge/conflict links, a separate OpenSpec change may propose a shadow semantic alias/coreference curator that emits proposals only (no automatic durable promotion) and runs as one batch after B1.2.
