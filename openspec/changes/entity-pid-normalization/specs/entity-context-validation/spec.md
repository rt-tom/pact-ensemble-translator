## ADDED Requirements

### Requirement: PID-reference format tolerance in entity-context validation
The §8.3 entity-context validation SHALL accept a model-supplied PID reference in any of the equivalent forms `pNNNNN`, `NNNNN`, or `N` (the `p` prefix and leading zeros are optional) by canonicalizing it to the real chapter PID before any existence or verbatim check. Canonicalization SHALL be driven by the chapter's real PID set: a reference whose integer value matches a real source PID SHALL resolve to that PID's canonical key; a reference whose integer value does not match any real source PID SHALL remain unresolved and fail the existence check (dead PID). Stored `anchor`, `alias`, `evidence`, and `evidence_windows` PIDs SHALL be written in the canonical `pNNNNN` form.

#### Scenario: Bare-integer window endpoint resolves to canonical PID
- **WHEN** a claim's `evidence_windows` uses `[[86,86]]` and the chapter's real PIDs include `p00086`
- **THEN** the validation SHALL treat `86` as `p00086` and SHALL NOT drop the claim as a dead PID

#### Scenario: Zero-padded or p-prefixed variants resolve identically
- **WHEN** the model writes `p00086`, `00086`, `p86`, or `86` for the same real PID `p00086`
- **THEN** the validation SHALL canonicalize each to `p00086` before the existence and verbatim checks

#### Scenario: Genuinely unknown PID still fails
- **WHEN** a reference resolves to an integer absent from the chapter's real PID set (e.g. `999` in a chapter whose PIDs end at `279`)
- **THEN** the validation SHALL drop the claim as a dead PID and SHALL NOT invent or substitute a PID
