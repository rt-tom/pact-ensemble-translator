## 1. Characterization and contracts

- [ ] 1.1 Add isolated fixtures derived from the representative book run for cross-section duplicates, class routing, candidate anaphora, verified facts, canonical RU forms, glossary/memory conflicts, and causal chapter order; verify fixture hashes and no production state/artifact is modified
- [ ] 1.2 Inventory every default whole-chapter prompt/cache consumer and bind regression tests to the documented flow (prepass → generation → R-editor → B3 → repair/re-audit → formatting → glossary → promotion); verify candidate-selection paths are not asserted as default-book consumers

## 2. Canonical memory population

- [ ] 2.1 Implement normalized all-scope identity lookup before book-memory insertion; verify `Callan`, `Paige`, `Peter`, and `Stephanie`-style cross-section collisions merge or no-op without a duplicate durable key
- [ ] 2.2 Route created records by `memory_class`, retain verified gender only as an attribute, and preserve first-seen/evidence provenance; verify named characters, named places, terms, and claimless named identities land in their correct scopes
- [ ] 2.3 Enforce verified-claim and verified-relation promotion independently of a verified surface; verify candidate local coreferences never become aliases/facts while B3 retains them in chapter-local entity context
- [ ] 2.4 Merge multiple compatible matches (same normalized identity or policy-approved explicit alias, non-contradictory attributes/forms) into one canonical record with merged provenance and `memory_class` scope; treat only contradictory matches as conflict; add a fixture with two compatible duplicates existing before the current chapter and verify they are merged, not tripled
- [ ] 2.5 Exclude any record still in an ambiguous conflict state from role views; verify pre-existing unresolved duplicates never appear as an ambiguous constraint
- [ ] 2.6 Preserve established compatible facts by evidence append, record incompatible facts as non-prompt-visible conflicts, and emit create/merge/update/no-op/reject/conflict decisions; verify no incoming claim silently overwrites canonical data
- [ ] 2.7 Expand the versioned candidate report with operation, target, scope, class, reason, and evidence; verify an accepted merge/no-op is distinguishable from a newly created record

## 3. Role-aware context compiler

- [ ] 3.1 Add one pure `select_relevant(authoritative_state, source_map)` reusing the existing causal source-relevance logic; verify it is computed once per chapter and reused by every consumer rather than recomputed per stage
- [ ] 3.2 Implement `render_book_context(role, relevance, authoritative_glossary, current_b1_2, glossary_candidates)` with text, included canonical IDs, resolved term-map, schema/version, and canonical hash; verify identical input renders byte-identically
- [ ] 3.3 Resolve each established Russian form from the frozen authoritative glossary; when a glossary entry exists it overrides `book_memory.canonical_ru`; when they disagree, exclude the conflicting form and record a diagnostic; include the resolved glossary slice in the rendered hash; add a glossary/memory conflict test
- [ ] 3.4 Enforce causal source relevance using canonical names and verified lexical variants only, with explicit narrator/seed/global-voice exceptions; verify later facts, generic roles, relationship descriptions, candidate aliases, and shared-RU-target matches do not enter a view
- [ ] 3.5 Define concrete numeric per-role token/card budgets and fixed field/record priority as named constants in code (not prose), plus overflow behavior that trims lowest-priority extras; verify over-budget cards are trimmed, not used to add model calls
- [ ] 3.6 Include the audit/repair card in the existing audit input-budget accounting; add a regression test asserting audit chunk count and retry budget are unchanged by an enabled card
- [ ] 3.7 Compose the translator prompt from the causal durable view plus a separate labelled current-chapter verified B1.2 block with its own identity; verify the B1.2 block is not treated as a durable role view
- [ ] 3.8 Render bounded audit/repair, Russian-editor, and glossary cards according to their role allowlists, source-prevails policy, and glossary-wins resolution; verify prompt snapshots contain required name/gender/address/term constraints and omit unrelated plot/chapter-local facts
- [ ] 3.9 In `select_relevant`, explicitly document and implement narrator/seed/global-voice constraints as an exception always included from explicit policy independent of source presence; verify they appear in every role view and are not subject to relevance matching

## 4. Default whole-chapter wiring and identity

- [ ] 4.1 Wire the translator view into whole-chapter generation/retry and bind its rendered hash/version (including glossary slice) to generation identity; verify changed causal context invalidates stale generation replay
- [ ] 4.2 Wire the Russian editorial card into the enabled R-stage without exposing source text; bind card hash/version to R-editor artifacts and verify an established Russian form/gender remains stable through an edit
- [ ] 4.3 Wire the audit/repair card alongside the existing B1.2 entity context into B3 audit, selective repair, and re-audit; bind identities to all affected caches and verify source-prevails behavior on a memory/source disagreement without introducing a new audit issue category
- [ ] 4.4 Wire the candidate-limited established-term card (resolved from the authoritative glossary) into the enabled glossary resolver and its identity validation; verify only current candidates’ terms are supplied and a changed term card invalidates stale resolver replay
- [ ] 4.5 Assert that source entity prepass, formatting resolution, and deterministic promotion receive no rendered book-memory prompt card; verify disabled optional stages make no render/call

## 5. Provenance, verification, and review

- [ ] 5.1 Persist bounded per-role context provenance (role, schema/version, causal state/index identity, resolved glossary slice identity, rendered hash, included IDs/count, conflict diagnostics, and empty/disabled/invalid reason); verify diagnostics distinguish empty relevant context from a disabled stage without writing duplicate unbounded BIBLE artifacts
- [ ] 5.2 Run focused B1.2/memory/index/renderer/R-editor/B3/repair/glossary/whole-book tests and `pact-fidelity-lint`; verify no model server or pipeline is started
- [ ] 5.3 Run `openspec validate book-memory-role-views --strict`, applicable broader tests, and `pact-git-hygiene`; verify only scoped files changed and no secrets or production artifacts are present
- [ ] 5.4 Freeze the representative acceptance setup before validation: book = the book used in the defect analysis (book id `1`, “bonds” chapters such as `0001_bonds-1-1`), chapter range = the same chapters translated in the analyzed run `book_0001-0001_remote_worsebutfull`, baseline = the existing v4.1 run artifacts for those chapters (no new 4.1 run required). Then validate the matrix: targeted name/gender/address/term inconsistencies removed, no new source-fidelity errors, no false constraints from candidate/local anaphora, and per-role token/call budgets respected; record evidence and any regression as a finding. If the owner specifies a different book/chapter range, use that and record it.
- [ ] 5.5 Have `pact-dev` self-review and `pact-rev` independently review the implementation under the project review workflow; verify approval or return all actionable findings for a single same-worktree fix pass before owner review
