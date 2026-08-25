## Why

The planning change `book-state-snapshot-handoff` defines the host-handoff protocol but is explicitly implementation-free. Media must become the durable, immutable, single-writer authority for book state so that owner-started RT runs can publish validated terminal book-state snapshots without shared writable directories, two-way memory merges, cross-host resume, or pipeline execution from media. This implementation change delivers the first minimal, fully offline-testable slice: the media-side store, manifest schema, validation, atomic `CURRENT` promotion guarded by a promote-time lease mutex, quarantine on rejection, bootstrap from the existing authoritative RT `pact_chapters` state, and owner-only lease release with an audit trail.

## What Changes

- Implement a media-side book-state store under `/home/rt/pact_runs/books/<book-id>/` with `CURRENT.json`, `locks/`, `incoming/`, `_bootstrap_inbox/`, `quarantine/`, and immutable `snapshots/<revision-id>/`.
- Define and validate a minimal snapshot manifest (schema version, book id, revision id, parent revision id, timestamps, terminal status, source identity, `state_files` with byte SHA-256 + size, excludes, tool/code identity). The manifest MUST NOT contain credentials, environment dumps, server state, or model caches.
- Implement `pact-init-store` to create the store skeleton for a book.
- Implement `pact-bootstrap` to seed the first immutable revision from a quiescent owner-copied RT `pact_chapters` bundle placed in `_bootstrap_inbox/<ts>/`, validating the four canonical state files (`book_memory.json`, `glossary.json`, `chapter_index.json`, `observations.json`) and writing `snapshots/rev-0001/` plus `CURRENT.json`.
- Implement `pact-promote` to validate an incoming candidate bundle, enforce a promote-time lease mutex with fail-closed parent-revision comparison, atomically promote to a new immutable `snapshots/<revision-id>/`, advance `CURRENT.json` only while the lease still references the same parent, and quarantine the candidate on any rejection.
- Implement `pact-release-lease` as an owner-only recovery command that clears a held lease and appends a JSONL audit record; automatic TTL takeover is prohibited.
- Document the SSH/SFTP transport and a restricted `command=` snippet; actual key generation and `authorized_keys` edits are owner operations outside this repository. No SSH/SFTP client is implemented in this slice.
- Add offline tests on synthetic bundles that exercise init, bootstrap, accept, reject (bad hash, stale parent, held lease), lease release, and quarantine without network, model-server, or pipeline execution.

## Scope Decision (2026-08-26, owner-confirmed)

**Scope A — state-only snapshot.** The immutable snapshot contains the book *state* (glossary, book memory, chapter index, observations) copied directly from the authoritative RT `D:\pact\pact_chapters` directory. Per-chapter translated bodies (`v4_book_*/chapter_*/translations.json`) are NOT included; RT already holds them locally. The per-chapter 27-folder selection map is therefore out of scope for this change and reserved for a later `pact-collect-book` change if full-body reproducibility is required.

## Capabilities

### New Capabilities

- `book-state-snapshot`: A media-side, offline, immutable book-state store with manifest validation, atomic `CURRENT` promotion guarded by a promote-time lease mutex, quarantine on rejection, bootstrap seeding, and owner-only lease release with audit.

### Modified Capabilities

- (none)

## Impact

- New code in `pact_v4/snapshot/` (manifest, store, bootstrap, promote, lease, CLI) and `tests/pact_v4/snapshot/`. Likely a README/operator note documenting the `command=` snippet and bootstrap procedure.
- No prompts, translation/audit/formatting/tag semantics, model routing, runtime-profile semantics, or production pipeline run is changed.
- No automated RT→media transport is implemented in this slice; bootstrap requires a manual owner copy of `pact_chapters` into `_bootstrap_inbox/<ts>/`.
- Implementation remains subject to independent `pact-rev` review and separate owner approval for merge/deploy.
