## Purpose

Provides a media-side, offline, immutable book-state store that accepts validated terminal book-state snapshots from owner-started RT runs, enforces a single-writer promote-time lease mutex, quarantines invalid candidates, and allows owner-only lease recovery with an audit trail. Under Scope A the snapshot holds only the book *state* copied from the authoritative RT `pact_chapters` directory.

## ADDED Requirements

### Requirement: Media-side immutable store layout
The system SHALL provide a book-scoped media store under a configurable root (default `/home/rt/pact_runs/books/<book-id>/`) containing `CURRENT.json`, `locks/`, `incoming/`, `_bootstrap_inbox/`, `quarantine/`, and immutable `snapshots/<revision-id>/` directories. `pact-init-store` SHALL create this skeleton and initialize `CURRENT.json` with no current revision.

#### Scenario: Initialize a new book store
- **WHEN** an operator runs `pact-init-store <book-id>`
- **THEN** the system SHALL create the book directory, `locks/`, `incoming/`, `_bootstrap_inbox/`, `quarantine/`, and `snapshots/`, and write a `CURRENT.json` with `revision_id: null`, without contacting any network, model server, or pipeline.

#### Scenario: Isolated root for tests
- **WHEN** an operator passes `--root <dir>` to any store command
- **THEN** the system SHALL resolve all store paths under that root instead of the default media path.

### Requirement: Minimal state-only snapshot manifest
The system SHALL define a snapshot manifest with `schema_version`, `book_id`, `revision_id`, `parent_revision_id` (null for bootstrap), `created_at`, `published_at`, `terminal_status` ∈ {`bootstrap-seed`, `complete`, `accepted_degraded`}, `tool_version`, `source{path_on_rt, operator, host}`, `state_files[{rel_path, sha256, size}]`, `excludes[]`, and `code_commit`. The manifest SHALL NOT contain credentials, environment dumps, server state, or model caches.

#### Scenario: Manifest excludes secrets
- **WHEN** the system serializes a snapshot manifest
- **THEN** the manifest SHALL omit any credential values, environment dumps, server state, or model caches, and SHALL list every committed state file with its byte SHA-256 and size.

### Requirement: Bootstrap seeds first revision from copied state
`pact-bootstrap` SHALL read a quiescent owner-copied RT `pact_chapters` bundle from `_bootstrap_inbox/<ts>/`, select exactly the four canonical state files (`book_memory.json`, `glossary.json`, `chapter_index.json`, `observations.json`), validate each as well-formed JSON, build a manifest with `revision_id: rev-0001` and `parent_revision_id: null` and `terminal_status: bootstrap-seed`, write `snapshots/rev-0001/state/` plus `manifest.json`, and atomically write `CURRENT.json` pointing at `rev-0001`.

#### Scenario: Bootstrap from a valid inbox
- **WHEN** an owner copies `pact_chapters` into `_bootstrap_inbox/<ts>/` and runs `pact-bootstrap <book-id>`
- **THEN** the system SHALL create `snapshots/rev-0001/` with the four canonical state files and `manifest.json`, set `CURRENT.json` to `rev-0001`, and exclude candidates/backups/archives from the snapshot.

#### Scenario: Bootstrap rejects non-JSON state
- **WHEN** a canonical state file in the inbox is not valid JSON
- **THEN** `pact-bootstrap` SHALL fail closed and SHALL NOT create or advance `CURRENT.json`.

### Requirement: Promote validates and atomically advances CURRENT
`pact-promote` SHALL validate the candidate manifest schema and verify each `state_files` byte SHA-256 and size against the actual file, require an eligible `terminal_status`, require `manifest.parent_revision_id` to equal the current `CURRENT.json` revision (fail-closed on mismatch), acquire a promote-time lease mutex bound to that parent revision, atomically move the validated bundle to a new `snapshots/<revision-id>/`, write `CURRENT.json` via atomic rename only while the lease still references the same parent, then release the lease and report `ACCEPTED`.

#### Scenario: Promote accepts a valid candidate
- **WHEN** RT uploads a valid candidate with `parent_revision_id` equal to the current revision and invokes `pact-promote`
- **THEN** the system SHALL create a new immutable `snapshots/<revision-id>/`, advance `CURRENT.json` to it, release the lease, and report `ACCEPTED` with revision/manifest/current evidence and exit code 0.

#### Scenario: Promote rejects stale parent revision
- **WHEN** a candidate's `parent_revision_id` does not equal the current `CURRENT.json` revision
- **THEN** `pact-promote` SHALL reject fail-closed (`StaleParent`), quarantine the candidate, preserve the prior `CURRENT.json`, and exit non-zero.

#### Scenario: Promote rejects hash mismatch
- **WHEN** a `state_files` entry's actual byte SHA-256 or size differs from the manifest
- **THEN** `pact-promote` SHALL reject, quarantine the candidate, preserve the prior `CURRENT.json`, and exit non-zero.

### Requirement: Promote-time lease mutex with no auto-takeover
The lease SHALL be acquired briefly at promote time via an atomic compare-and-swap write of `locks/<book-id>.lease.json` bound to the current parent revision. If a lease file already exists, `pact-promote` SHALL reject with `LEASE_HELD` and require owner release; a crashed run never holds a lease because no promote occurred. Lease expiry SHALL NOT trigger automatic takeover.

#### Scenario: Concurrent promote is rejected
- **WHEN** a promote finds an existing `locks/<book-id>.lease.json`
- **THEN** the system SHALL reject with `LEASE_HELD`, leave the prior `CURRENT.json` unchanged, and require `pact-release-lease` before retrying.

#### Scenario: Crashed run holds no lease
- **WHEN** an RT run crashes before invoking `pact-promote`
- **THEN** no `locks/<book-id>.lease.json` SHALL exist, so the next promote is not blocked by a dead lock.

### Requirement: Quarantine on rejection
On any validation or lease failure, `pact-promote` SHALL move the candidate to `quarantine/<candidate-id>/` and return a machine-readable rejection reason; the prior `CURRENT.json` SHALL remain authoritative.

#### Scenario: Rejected candidate is quarantined
- **WHEN** `pact-promote` rejects a candidate for any reason
- **THEN** the system SHALL move the candidate bundle to `quarantine/<candidate-id>/`, preserve the prior `CURRENT.json`, and print a `REJECTED` reason with non-zero exit.

### Requirement: Owner-only lease release with audit
`pact-release-lease <book-id> --operator <op> --reason <text>` SHALL clear `locks/<book-id>.lease.json` and append a JSONL record to `locks/<book-id>.lease_audit.jsonl` containing `book_id`, `lease_id`, `action`, `reason`, `operator`, `ts`, `prior_staging_reviewed`, and `recovery_decision`. It SHALL NOT be reachable through the restricted promote path.

#### Scenario: Release clears lease and audits
- **WHEN** an owner runs `pact-release-lease <book-id> --operator rt --reason "stale promote crashed"`
- **THEN** the system SHALL delete `locks/<book-id>.lease.json` and append a JSONL audit record, enabling a subsequent `pact-promote` to acquire the lease.

#### Scenario: Read-only expiry report
- **WHEN** an operator runs `pact-release-lease --check-expired`
- **THEN** the system SHALL report held leases and their informational expiry without releasing or modifying any lease.

### Requirement: Offline and side-effect-free validation
All store commands SHALL operate purely on local filesystem state with JSON manifests and SHALL NOT start a pipeline, contact a provider/remote endpoint, operate a model server, or edit any RT production path. Tests SHALL exercise init, bootstrap, accept, reject paths, lease release, and quarantine using synthetic bundles.

#### Scenario: Tests use synthetic bundles only
- **WHEN** the offline test suite runs
- **THEN** it SHALL create synthetic bundles in temporary directories, assert accept/reject/lease/quarantine behavior, and perform no network, model-server, or pipeline execution.
