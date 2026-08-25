## Why

The media-side snapshot store (`book-state-snapshot-handoff-impl`, merged) provides an
immutable, auditable, lease-protected book-state authority, but RT still runs the
translation pipeline against its own local `D:\pact\pact_chapters\` without syncing
to/from media. The two sides are disconnected: media holds the durable history, RT
holds the live working memory, and nothing guarantees a run starts from
media-authoritative state or that its result becomes the next immutable revision.
This change closes that loop for the **state-only** boundary (the four canonical
`pact_chapters` files).

## What Changes

- **New RT-side media client** (SSH-based, uses the system `ssh`/`scp` on RT; does
  NOT add any SSH/network code into the offline `pact_v4/snapshot` store package)
  that talks to a media-restricted snapshot facade.
- **Run-hook integration into the v4 run command** (the run launched from RT via
  PowerShell):
  - **At start**, before `MemoryManager` initialization, fetch the current
    authoritative book-state from media and write it to the RT working
    `pact_chapters\` directory so the run operates on media-authoritative state.
  - **At end**, after the usual in-run `MemoryManager.promote('complete')`, build a
    candidate (manifest + the four canonical files), push it to media, trigger media
    `promote`, and treat the promote verdict as the confirmation received from media.
- **Media-side restricted facade** (a thin wrapper invoked through
  `authorized_keys command=`): exposes ONLY `fetch-current`, `receive-candidate`,
  `promote`, and `release-lease --check-expired` for a scoped `book-id`, rejecting
  anything else. The host `authorized_keys` entry itself is owner host-config,
  outside the repo.
- **State-only scope**: the four canonical files — `glossary.json`,
  `book_memory.json`, `chapter_index.json`, `observations.json`. No translation
  bodies are pulled or pushed.

## Capabilities

### New Capabilities
- `book-state-rt-integration`: RT↔media sync for the state-only book-state — pull
  authoritative state at run start, push updated state and obtain promote
  confirmation at run end, over a restricted SSH facade.

### Modified Capabilities
<!-- none — media store internals are reused as-is; this change adds the transport + run hooks -->

## Impact

- New RT module(s): `pact_v4/snapshot/remote_client.py` (RT side; system `ssh`/`scp`).
- New media facade: `pact_v4/snapshot/remote_facade.py` (or shell wrapper) invoked via
  `authorized_keys command=`; may add `fetch-current` / `receive-candidate` surface to
  the existing `pact_v4.snapshot` CLI.
- Integration point: the v4 strict run command (PowerShell-launched on RT) — pre-init
  pull hook and post-promote push+confirm hook.
- Depends on the merged `pact_v4/snapshot` store (lease mutex, `StaleParent`/
  `HashMismatch`/`LeaseHeld` rejection, exact-four-file boundary, `manifest_sha256`).
- Requires media `sshd` + a restricted RT key authorized on media (owner host-config,
  out of repo). Adds a system `ssh`/`scp` dependency on RT (Windows OpenSSH, already
  present).
