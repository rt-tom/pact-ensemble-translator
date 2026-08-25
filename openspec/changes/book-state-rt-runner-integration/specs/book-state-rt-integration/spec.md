## Purpose

RT↔media synchronisation for the state-only book-state: the RT run pulls authoritative
state from media at start and pushes the updated state back (with promote confirmation)
at end, over a restricted SSH facade.

## ADDED Requirements

### Requirement: Pull authoritative state at run start
The RT run SHALL fetch the current media book-state (the `CURRENT.json` pointer, the
referenced `manifest.json`, and the four canonical files `glossary.json`,
`book_memory.json`, `chapter_index.json`, `observations.json`) and write it to the RT
working `pact_chapters\` directory BEFORE `MemoryManager` initialization, so the run
operates on media-authoritative state.

#### Scenario: Run starts from media-authoritative state
- **WHEN** the v4 run command begins and media is reachable
- **THEN** the four canonical files are written to the RT working directory and the run's `MemoryManager` is initialised against them

#### Scenario: Fetched state is validated before use
- **WHEN** the state is fetched from media
- **THEN** each of the four files is validated as a regular, non-symlink JSON file with an allowed name, and the run refuses to proceed if validation fails

#### Scenario: Media unreachable fails fast
- **WHEN** media cannot be reached at run start
- **THEN** the run fails with a clear error and does NOT silently fall back to stale local state

### Requirement: Push updated state and obtain confirmation at run end
After the usual in-run `MemoryManager.promote('complete')`, the RT run SHALL build a
candidate (a `manifest.json` plus the four canonical files), push it to media, trigger
media `promote`, and treat the promote verdict as the confirmation received from media.

#### Scenario: Promotion accepted with confirmation
- **WHEN** media `promote` returns ACCEPTED with a `revision_id`
- **THEN** the run records that `revision_id` as the confirmation of the published state

#### Scenario: Stale parent triggers bounded retry
- **WHEN** media `promote` returns REJECTED with reason `STALE_PARENT` (the run's base revision is no longer current)
- **THEN** the run re-pulls the current state and retries the push+promote a bounded number of times, and otherwise reports the rejection

#### Scenario: Rejection or transport failure is reported, local state preserved
- **WHEN** media `promote` returns REJECTED for validation/lease reasons, or the push cannot reach media
- **THEN** the run reports the reason/error and retains the local RT state; it does NOT silently drop the result

### Requirement: Media restricted facade allow-list
Media SHALL expose the snapshot operations to RT only through a restricted facade that
permits exactly `fetch-current`, `receive-candidate`, `promote`, and
`release-lease --check-expired` for a single scoped `book-id`, and MUST reject any other
subcommand, argument, or `book-id`.

#### Scenario: Allowed subcommand executes
- **WHEN** RT invokes an allowed subcommand for the scoped `book-id`
- **THEN** the facade executes it and returns the result

#### Scenario: Disallowed request is rejected
- **WHEN** RT (or any caller) invokes a subcommand outside the allow-list, or a different `book-id`
- **THEN** the facade rejects the request without performing the operation

### Requirement: Transport separation keeps the store package offline
The `pact_v4/snapshot` store package MUST remain offline (no SSH/network client code).
The RT↔media transport SHALL be implemented by a separate RT-side client that uses the
system `ssh`/`scp` executable.

#### Scenario: Store package has no network code
- **WHEN** the `pact_v4/snapshot` store modules are inspected
- **THEN** none of them import or shell out to `ssh`/`scp`/any network client

#### Scenario: Client uses system ssh only
- **WHEN** the RT client performs a transport operation
- **THEN** it invokes the system `ssh`/`scp` executable and does not embed its own transport

### Requirement: State-only boundary is preserved end to end
The RT client SHALL package and push exactly the four canonical files; it MUST NOT pull
or push translation bodies or any file outside the canonical set.

#### Scenario: Candidate contains only canonical files
- **WHEN** the RT client builds the candidate to push
- **THEN** it contains exactly `manifest.json` plus the four canonical files and no extra or translation-body files

#### Scenario: Media enforces the boundary on receipt
- **WHEN** a candidate arrives at media `incoming/<candidate-id>/`
- **THEN** media `promote` validates the exact-four-file boundary (rejecting extra files, symlinks, special files, and non-regular entries) exactly as for any other candidate
