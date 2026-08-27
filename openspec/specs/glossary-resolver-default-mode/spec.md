## Purpose

Make gated glossary promotion active by default while retaining explicit disabled and observation-only modes and keeping glossary behavior independent from durable book-memory policy.

## ADDED Requirements

### Requirement: Promote supersedes the prior default
This change SHALL supersede `glossary-model-resolver` design Decision D6 only for the effective default mode. When no explicit glossary resolver mode is supplied, the system SHALL use `promote`, generate and validate the resolver sidecar, and submit valid proposals to the existing promotion gate. All other approved resolver safety and transport/configuration requirements SHALL remain unchanged.

#### Scenario: No mode is supplied
- **WHEN** a chapter run starts without a glossary resolver mode
- **THEN** effective mode SHALL be `promote` and valid proposals SHALL pass through the existing fail-closed gate

#### Scenario: Explicit promote is supplied
- **WHEN** the operator supplies `promote`
- **THEN** behavior SHALL equal the default mode and provenance SHALL record that the mode was explicit

### Requirement: Explicit off and shadow remain available
The system SHALL support explicit `off` and `shadow`. `off` SHALL make no glossary resolver model call, SHALL create no `glossary_proposals.json`, and SHALL make no new glossary observation or legacy deterministic promotion; it SHALL write only a versioned status artifact identifying `disabled` with zero resolver candidates. `shadow` SHALL generate and validate `glossary_proposals.json` and report proposals but SHALL not mutate the durable glossary.

#### Scenario: Resolver is off
- **WHEN** the operator supplies `off`
- **THEN** no resolver call or proposal sidecar SHALL occur, the glossary hash SHALL remain unchanged, and the status artifact SHALL distinguish disabled mode from an empty extraction

#### Scenario: Resolver is shadowed
- **WHEN** the operator supplies `shadow`
- **THEN** valid/rejected proposals SHALL be inspectable while the glossary hash remains unchanged

### Requirement: Glossary and book-memory mode matrix is independent
Glossary mode (`promote`, `shadow`, `off`) and book-memory policy (`promote_verified`, `observe`, `off`) SHALL be evaluated independently. Each policy SHALL control only its own durable resource; chapter-level B1.2 evidence may still be produced whenever audit/entity context is enabled.

#### Scenario: Glossary promote with memory off
- **WHEN** glossary mode is `promote` and book-memory policy is `off`
- **THEN** valid glossary proposals MAY mutate only the glossary while book-memory hash remains unchanged

#### Scenario: Glossary off with memory promotion enabled
- **WHEN** glossary mode is `off` and book-memory policy is `promote_verified`
- **THEN** eligible verified memory observations MAY mutate only book memory while glossary hash remains unchanged

#### Scenario: Both policies are observation-only
- **WHEN** glossary mode is `shadow` and book-memory policy is `observe`
- **THEN** proposal and candidate reports SHALL be written while all four canonical state file hashes remain unchanged

#### Scenario: Both policies are off
- **WHEN** glossary mode and book-memory policy are both `off`
- **THEN** only disabled/status reporting and any audit-required chapter entity artifact SHALL be produced; no durable mutation SHALL occur

### Requirement: Promotion remains fail-closed and transactional
Default `promote` SHALL preserve sidecar schema, snapshot/config identity, candidate-input and semantic-translation hashes, model/backend identity, evidence allowlist, quarantine exclusion, duplicate-target, and existing-conflict validation. Accepted glossary and book-memory observations SHALL be staged into one four-file canonical state transaction; invalid proposals SHALL not cause partial mutation.

#### Scenario: Proposal identity is stale
- **WHEN** any required sidecar identity or hash differs from the current run
- **THEN** the sidecar SHALL be rejected and the glossary SHALL remain unchanged

#### Scenario: Evidence is quarantined
- **WHEN** a proposal cites a quarantined PID
- **THEN** that proposal SHALL be excluded while independent valid proposals may proceed in the same staged transaction

#### Scenario: Transaction is interrupted
- **WHEN** a crash occurs after one canonical file replacement
- **THEN** startup recovery SHALL restore the complete pre-transaction four-file bundle before processing another run

### Requirement: Modes and policies are identity-bearing
Run provenance and versioned status/candidate reports SHALL record requested and effective glossary mode, whether the default was used, effective book-memory policy, resolver/policy versions, and relevant state hashes. Changes to approved terms, rejects, aliases, or exceptions SHALL change `book_memory_policy_version`, invalidate affected index/cache identities, and require deterministic index rebuild.

#### Scenario: Policy changes
- **WHEN** any durable-memory policy input changes
- **THEN** the policy version and dependent identities SHALL change, stale reports/indexes SHALL not be reused, and the causal index SHALL be rebuilt
