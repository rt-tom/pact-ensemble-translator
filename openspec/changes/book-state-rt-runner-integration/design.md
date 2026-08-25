## Context

The media-side store is merged and seeded (`book-state-snapshot-handoff-impl`): immutable
revisions under `snapshots/rev-NNNN/`, atomic `CURRENT.json`, `manifest_sha256`, a
promote-time lease mutex (`os.open O_EXCL` CAS, no auto-TTL), and a strict exact-four-file
boundary that rejects symlinks, special files, and any non-regular or extra entry at every
layer. `promote` already returns `StaleParent` / `HashMismatch` / `LeaseHeld` rejections.

On RT, the v4 strict run is launched via PowerShell and uses `MemoryManager(base_dir)`
against `D:\pact\pact_chapters\`; at the end it calls `MemoryManager.promote('complete')`
which merges observations into `glossary.json` / `book_memory.json`. The RT side and the
media store are currently disconnected. This change wires them together for the
state-only boundary. See proposal.md — Why.

## Goals / Non-Goals

**Goals:**
- RT run starts from media-authoritative state (pull at start).
- RT run publishes its updated state as a new immutable media revision and obtains the
  promote verdict as confirmation (push + promote at end).
- Fail-closed: media unreachable or rejected is reported, never silently swallowed.
- Reuse the merged media store as-is (lease mutex, boundary validation, `StaleParent`).

**Non-Goals:**
- Translation-body collection / the 27-chapter mapping — explicitly out of scope; only
  the four canonical files move.
- Automated scheduling or any change to when the owner starts the run.
- Holding a run-time lease during the translation; the lease is acquired at promote time.
- Modifying media store internals.

## Decisions

- **RT client uses the system `ssh`/`scp`** (Windows OpenSSH, already on RT; RT already
  has interactive SSH access to media, e.g. `ssh media` from PowerShell). The client
  invokes `ssh media pact-snapshot <subcommand> ...`; the media facade is the restriction
  layer that limits what the dedicated key may do. The `pact_v4/snapshot` store package
  stays offline; the client is a separate RT module (`pact_v4/snapshot/remote_client.py`).
  Rationale: keeps the audited store free of network trust surface and matches the
  boundary-hardening standing rule (offline store).
- **Media facade = thin restricted wrapper** invoked via `authorized_keys command=`. It
  permits exactly four operations for one scoped `book-id`:
  - `fetch-current <book-id>` — streams the current state (CURRENT.json + manifest.json
    + the four canonical files) to the caller.
  - `receive-candidate <book-id> <candidate-id>` — reads a candidate archive from stdin
    and writes it under `incoming/<candidate-id>/`.
  - `promote <book-id> <candidate-id>` — runs the existing `promote`, returns JSON verdict.
  - `release-lease <book-id> --check-expired` — read-only expired report.
  The `authorized_keys` `command=` string itself (binding a dedicated RT key) is owner
  host-config, outside the repo; the repo ships the wrapper and the CLI surface it calls.
- **Run hooks** in the v4 run command:
  - Pre-init: call client `fetch-current`, validate the four files (regular, non-symlink,
    allowed names, valid JSON), then let `MemoryManager` init against the working dir.
  - Post-`promote`: build candidate (manifest + four files from the working dir after
    `MemoryManager.promote('complete')`), `receive-candidate` + `promote` via the client,
    capture the verdict.
- **Candidate = exact four canonical files** from the RT working `pact_chapters\` after the
  usual in-run promotion. The client packages only those four; media `promote` re-validates
  the boundary.
- **Stale-parent retry**: if media `promote` returns `STALE_PARENT`, the run re-pulls the
  current state and retries the push+promote a bounded number of times (default 1), then
  reports the rejection. This handles the case where media advanced between pull and push.
- **Fail-fast on unreachable media** at start (no silent stale fallback) and on push
  failure (local state retained, error reported).

## Risks / Trade-offs

- **Media unreachable blocks runs** → mitigation: clear error, RT local state preserved;
  owner can run without media only if they explicitly opt out (not in this change).
- **Concurrent RT runs / external media advances** → mitigation: `StaleParent` rejection +
  bounded retry, and the promote-time lease mutex prevents two promotions clobbering
  `CURRENT.json`.
- **Trust of fetched state** → mitigation: RT validates the four files (regular,
  non-symlink, JSON, allowed names) before the run uses them, so a misconfigured/compromised
  media cannot inject bad data silently.
- **Restricted-command escape** → mitigation: the facade parses and allow-lists subcommand +
  `book-id` and rejects everything else; the `command=` string permits only the wrapper.

## Migration Plan

- New capability; no migration of existing state. The media store already exists.
- Deployment of the facade = owner host-config (`authorized_keys command=` + RT key on
  media), separate from merging this change. Rollback: remove the `authorized_keys` entry
  and the facade returns to manual `bootstrap`/local operation.

## Open Questions

None blocking — the four design questions (state-only scope, RT→media restricted SSH,
promote-time lease as confirmation, v4 run as the execution point) were resolved with the
owner before planning.
