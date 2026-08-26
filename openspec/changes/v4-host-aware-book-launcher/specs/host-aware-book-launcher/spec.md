## Purpose

Provides a deterministic and host-safe book launch contract for the canonical media source and the separate RT/media mutable state and output locations.

## ADDED Requirements

### Requirement: Host-aware source, state, and output separation
The launcher SHALL select a declared execution-host layout before resolving a book run. The RT layout SHALL use `D:\pact\pact_chapters` as read-only source root, `D:\pact\book_state` as mutable state root, and `D:\pact\gate_bench_runs` as default output root. The media layout SHALL use `/home/rt/pact_chapters` as read-only canonical source root and SHALL use worker-local mutable state/staging and outputs outside that source root and outside canonical snapshot directories. Source roots SHALL NOT be used as mutable state roots.

#### Scenario: RT run isolates mutable state
- **WHEN** an operator starts a book run on RT without overriding host paths
- **THEN** the resolved source, mutable state, and output roots SHALL be the declared RT roots and no state file SHALL be written under `D:\pact\pact_chapters`

#### Scenario: Media run has Linux-local roots
- **WHEN** an operator starts a book run on media
- **THEN** the launcher SHALL resolve Linux-local source, worker state/staging, and output roots and SHALL NOT use Windows path defaults

### Requirement: Numeric range resolves canonical chapter files
The launcher SHALL resolve each numeric chapter in a requested closed range against its selected source root using `NNNN_*.html`, where `NNNN` is the zero-padded numeric chapter number. Each requested number SHALL resolve to exactly one regular, non-symlink HTML file. A zero-match, multiple-match, unreadable root, or non-regular match SHALL fail before pipeline startup.

#### Scenario: Variable chapter suffix is resolved
- **WHEN** chapter `149` is requested from a source root containing `0149_judgment-16-13.html`
- **THEN** the launcher SHALL select that full file name rather than require a fictional `0149.html`

#### Scenario: Ambiguous source name is rejected
- **WHEN** a requested number matches two or more `NNNN_*.html` files
- **THEN** the launcher SHALL reject the run before creating output or invoking the pipeline

### Requirement: Book preflight validates resolved local prerequisites
The book preflight SHALL validate the entire resolved requested source range and report the selected source/state/output roots, resolved chapter file names, and sanitized runtime identity. It SHALL verify source readability and local state/output directory readiness without contacting a provider, starting a model server, or creating a run artifact.

#### Scenario: Missing source fails in preflight
- **WHEN** a requested chapter has no matching source file
- **THEN** preflight SHALL return non-zero with the missing chapter number and source root before pipeline startup

### Requirement: State-only media synchronization is host-safe
Every simple book mode, local or remote, SHALL default to media book id `1`, media target `media-snap`, and media root `/home/rt/pact_runs`, with an explicit book-id override for another book. Before mutable book state initialization, it SHALL fetch the current media state; after an accepted local promotion, it SHALL use the existing fail-closed state-only synchronization contract and report a terminal media acceptance or rejection verdict. On media, it SHALL use an equivalent local restricted facade path rather than require a self-SSH connection. Fetching state SHALL materialize the four canonical mutable JSON files only at the selected mutable state root; it SHALL NOT create a duplicate `state/` mirror there.

#### Scenario: RT local publication is accepted
- **WHEN** an RT simple local run promotes a chapter and the media facade accepts the state candidate
- **THEN** the command SHALL print `MEDIA PUBLISH: ACCEPTED` with the returned revision evidence

#### Scenario: RT remote publication is accepted
- **WHEN** an RT simple remote run promotes a chapter and the media facade accepts the state candidate
- **THEN** the command SHALL print `MEDIA PUBLISH: ACCEPTED` with the returned revision evidence

#### Scenario: Media rejection fails the command
- **WHEN** state synchronization rejects a promoted candidate or cannot confirm publication
- **THEN** the command SHALL preserve local diagnostics, print `MEDIA PUBLISH: REJECTED` with the sanitized reason, and exit non-zero

#### Scenario: Media host avoids self-SSH
- **WHEN** simple remote mode is run on media
- **THEN** state synchronization SHALL not open an SSH connection from media to its own `media-snap` alias
