## Purpose

Provide each real consumer in the default whole-chapter book run only the causal canonical memory needed to preserve translation consistency, while preserving source authority and avoiding new persistent state or model calls.

## ADDED Requirements

### Requirement: Default whole-chapter role routing
For the public simple `book --chapters … --local|--remote` whole-chapter path, the system SHALL derive model context from the same pre-chapter canonical state and SHALL route it as follows: the generator receives the Translator BIBLE; the enabled R-stage Russian editor receives a Russian editorial view; B3 audit and selective repair (including their re-audit) receive an audit/repair consistency view; and the enabled glossary resolver receives an established-term view. The entity prepass SHALL remain source-only, inline-formatting resolution SHALL remain memory-free, and shared-state promotion SHALL remain deterministic.

A disabled optional stage SHALL make no call and require no view. The implementation SHALL NOT add a new standalone model stage: role cards are attached to the existing generation, R-editor, B3 audit, selective repair/re-audit, and glossary-resolver calls.

#### Scenario: Default book chapter uses all enabled consistency views
- **WHEN** a simple whole-chapter book run processes a chapter with generation, R-editor, B3, repair, and glossary resolver enabled
- **THEN** each enabled consumer receives only its declared role view derived from the causal pre-chapter state

#### Scenario: No new model stage is introduced
- **WHEN** the whole-chapter book run executes with all enabled consumers
- **THEN** no additional standalone model stage is added beyond generation, R-editor, B3 audit, selective repair/re-audit, glossary resolver, and formatting; role cards are attached to those existing calls

#### Scenario: Source-only and formatting boundaries are preserved
- **WHEN** entity extraction or inline formatting runs in the same chapter
- **THEN** neither model receives book-memory-derived prompt text

### Requirement: Bounded role views, authoritative glossary, and source precedence
The Translator BIBLE SHALL compose two labelled sections: the causal durable role view (only facts whose provenance is strictly earlier than the target chapter) and a separate current-chapter verified B1.2 entity block permitted only for generation. The B1.2 block SHALL NOT be treated as a durable role view and SHALL carry its own identity.

The audit/repair view, Russian editorial view, and glossary view SHALL contain only canonical, source-relevant consistency constraints resolved from the authoritative glossary. Each established Russian form SHALL be resolved from the frozen authoritative glossary; when a glossary entry exists it SHALL override `book_memory.canonical_ru`; when the two disagree, the conflict SHALL be excluded from the prompt view and recorded as a diagnostic, and SHALL NOT be silently resolved in favor of either source. The rendered-view hash SHALL include the resolved glossary slice.

Every view SHALL exclude chapter-local observations, generic descriptions, unverified/candidate relations, and facts whose provenance is not strictly earlier than the target chapter. Prompts using audit/repair/glossary views SHALL state that current source evidence prevails over memory; a disagreement is a consistency issue to verify, not proof that the current source is wrong.

Each role view SHALL be bounded by a deterministic per-role token/card budget. When selected relevant records exceed the budget, the renderer SHALL apply a fixed field/record priority and overflow policy (retain the source-prevails instruction and highest-priority canonical constraints; drop lowest-priority extras) rather than growing unbounded. The audit/repair card SHALL be included in the existing audit input-budget accounting so it cannot silently increase audit chunks or retries; an over-budget card SHALL be trimmed, not used to add model calls.

#### Scenario: Local anaphora is not injected
- **WHEN** B1.2 has a candidate relation such as `the young lady → Paige`
- **THEN** that relation appears in no role view and cannot constrain a later translation, audit, or repair

#### Scenario: Established form protects a repair
- **WHEN** a source-relevant canonical entity has an established Russian form and selective repair is enabled
- **THEN** the repair view includes that form while instructing the repairer to follow the current source if it conflicts

#### Scenario: Glossary conflict is excluded and diagnosed
- **WHEN** a canonical entity has a glossary form that conflicts with its `book_memory.canonical_ru`
- **THEN** the view omits the conflicting form and the diagnostic records the conflict rather than injecting either value

#### Scenario: Causal durable view excludes current-chapter B1.2
- **WHEN** the generation prompt is built
- **THEN** the causal durable view contains only pre-chapter facts and the current B1.2 block is a separate labelled section with its own identity

#### Scenario: Over-budget card is trimmed not expanded
- **WHEN** selected relevant records exceed the role budget
- **THEN** the renderer drops lowest-priority extras and the audit chunk count and retry budget are unchanged

#### Scenario: Russian editor receives grammar constraints only
- **WHEN** the R-stage editor processes a chapter containing a canonical character with verified gender and Russian form
- **THEN** its editorial view contains that name/form/gender information but no unrelated plot facts or chapter-local entity claims

### Requirement: Single relevance selector and identity-bound rendering
A single pure relevance selector SHALL be computed once per chapter from the frozen pre-chapter state and the current source map, reusing the existing causal source-relevance logic. Role views SHALL be projections of that selector result; the glossary view SHALL additionally intersect the selector result with the resolver's current entity candidates. The implementation SHALL NOT recompute relevance independently per consumer.

The role, view schema/version, selected causal entry, resolved glossary slice, and rendered-view hash SHALL participate in the identity of every prompt cache or resumable model artifact that consumes the view. A stale, unsupported, or mismatched view SHALL be recomputed from validated state or fail according to the existing consumer's safe policy; it SHALL not replay a model output made with different constraints.

#### Scenario: Later fact cannot leak into an earlier chapter view
- **WHEN** an alias or attribute is first verified in chapter M
- **THEN** no role view for chapter N where N is less than or equal to M contains that alias or attribute

#### Scenario: View change invalidates consumer replay
- **WHEN** the rendered repair view differs from the view bound to a cached repair artifact
- **THEN** the cached repair artifact is not replayed as valid for the current chapter

#### Scenario: Single selector prevents drift
- **WHEN** generation, audit, and glossary views are built for one chapter
- **THEN** they all derive from the same computed relevance result and cannot select different canonical subsets

### Requirement: Canonical consistency conflict is source-prevails only
When a view contains a known canonical memory/source disagreement, the system SHALL surface it only as a source-prevails instruction and bounded diagnostic. It SHALL NOT introduce a new audit issue category, and the implementation SHALL NOT extend the existing audit category set to resolve memory conflicts.

#### Scenario: Memory/source disagreement is not an audit category
- **WHEN** the audit card includes a known memory/source disagreement
- **THEN** the audit prompt treats source as prevailing and the disagreement appears only as a diagnostic, with no new audit issue type emitted

### Requirement: Inspectable context provenance
For every enabled role view, the chapter artifacts SHALL record the role, schema/version, causal state/index identity, resolved glossary slice identity, rendered-view hash, included canonical record identifiers, and any conflict diagnostics. Diagnostics SHALL distinguish no relevant canonical context from a disabled stage or an invalid/unsupported context. Reports SHALL not store an unbounded duplicate BIBLE when the existing prompt/raw-artifact retention policy does not require it.

#### Scenario: Empty relevant context is distinguishable
- **WHEN** an enabled audit has no source-relevant canonical facts
- **THEN** its provenance records an enabled audit view with zero included records rather than reporting the stage as disabled

#### Scenario: Glossary conflict is recorded in provenance
- **WHEN** a view excludes a conflicting glossary/memory form
- **THEN** its provenance records the conflict with both sources rather than silently omitting the record

### Requirement: Representative quality acceptance
For the representative default book run used to validate this change, verification SHALL demonstrate that the role views (a) eliminate targeted name, gender, address, and term inconsistencies across chapters, (b) introduce no new source-fidelity errors, (c) emit no false constraints derived from candidate or local anaphora, and (d) stay within the defined per-role token/call budgets. This SHALL be captured as a run-level acceptance check, not only unit wiring.

#### Scenario: Quality acceptance matrix passes
- **WHEN** the representative run is validated
- **THEN** the four acceptance criteria are recorded with evidence and any regression is reported as a finding
