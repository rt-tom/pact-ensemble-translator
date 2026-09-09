## ADDED Requirements

### Requirement: Canonical population, merge, and update outcomes
Before promotion, each eligible B1.2 record SHALL be resolved against all durable identity sections using normalized Unicode, case, apostrophe, and punctuation matching. A normalized existing identity SHALL produce one of `merge`, `no_op`, or `conflict`; it SHALL NOT create a duplicate record in a different section. The writer SHALL route a newly created durable record by `memory_class`, not by availability of gender: `named_character` records enter the character scope; other eligible named classes and approved terms enter their corresponding canonical scope.

When an eligible record matches multiple existing records, the system SHALL deterministically reconcile them. Multiple compatible matches (the same normalized identity or a policy-approved explicit alias, with non-contradictory verified attributes and Russian forms) SHALL be merged into one canonical record with merged provenance and the scope implied by `memory_class`. Only incompatible matches (contradictory verified attributes or Russian forms) SHALL be treated as `conflict`. A record left in an ambiguous `conflict` state SHALL be excluded from role views until an explicitly approved resolution, so it cannot appear as an ambiguous constraint.

The writer SHALL promote only individually `verified` aliases, attributes, facts, and relations permitted by the existing eligibility policy. A verbatim alias surface with a `candidate` relation SHALL remain withheld. A later agreeing verified claim SHALL append evidence/provenance without destructively replacing an established value. A contradictory or ambiguous verified claim SHALL create a conflict outcome and SHALL not overwrite the established canonical field or become prompt-visible until resolved through an explicitly approved policy.

#### Scenario: Cross-section identity is merged
- **WHEN** a newly eligible `named_character` normalizes to an existing entity record in another durable section
- **THEN** the promotion merges compatible provenance into one canonical record and does not create a second key

#### Scenario: Pre-existing cross-section duplicates are merged
- **WHEN** a current chapter confirms the same normalized identity that already exists as two compatible records in different sections before this chapter
- **THEN** promotion merges them into one canonical record with merged provenance rather than creating a third duplicate

#### Scenario: Ambiguous conflict is excluded from views
- **WHEN** two existing records are incompatible and remain unresolved
- **THEN** neither is used as an ambiguous constraint in a role view

#### Scenario: Gender does not determine section
- **WHEN** an eligible named place has no gender claim
- **THEN** it is routed to the named-entity scope rather than being classified as a character or an undifferentiated fallback entity

#### Scenario: Candidate coreference cannot become an alias
- **WHEN** a source surface is verified verbatim but its connection to a canonical entity is only `candidate`
- **THEN** the surface-to-entity relation is not persisted as a canonical variant and appears as withheld in diagnostics

#### Scenario: New contradictory fact does not overwrite history
- **WHEN** a verified incoming attribute conflicts with an established canonical attribute
- **THEN** the established field remains unchanged, the incoming evidence is reported as a conflict, and neither conflicting value is silently promoted as a new constraint

### Requirement: Population decision provenance
The versioned book-memory candidate report SHALL record, for every considered source identity and claim, its `memory_class`, evidence PIDs, selected operation (`create`, `merge`, `update`, `no_op`, `reject`, or `conflict`), canonical collision target when applicable, resulting scope, and stable reason code. A report SHALL distinguish an entity that was accepted but merged/no-op from one that created a new durable record.

#### Scenario: Existing entity is accepted without a new record
- **WHEN** an accepted current-chapter identity exactly or normally matches an existing canonical record
- **THEN** the report identifies the collision target and records `merge` or `no_op` instead of counting it as an independent new entity
