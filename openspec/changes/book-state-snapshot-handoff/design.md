## Context

`v4_book_run` currently executes chapters and promotes mutable book memory in the same host-local flow. `MemoryManager.promote` updates glossary/book memory and chapter index in causal chapter order, but has no inter-host lock, transaction, or merge protocol. Strict-run artifacts are identity-bound to source, snapshot, config, chunk plan, and backend descriptors.

The current intended target is a durable media artifact authority with owner-started **remote** execution on media, while RT remains the execution host for local-model runs (and may also run remote profiles). A later, separate change may add media orchestration of RT-only llama workers. This change defines the protocol only; it must not assume that SMB, SSHFS, rsync, SSH, TLS, or a particular host path is already available.

## Goals / Non-Goals

**Goals:**
- Define a single-writer, immutable-snapshot handoff protocol that preserves book memory causality and run provenance.
- Specify required manifest/identity/integrity checks before a candidate becomes the canonical media snapshot.
- Define safe failure behavior: media remains on its prior accepted snapshot after worker crash, partial upload, validation failure, or lock conflict.
- Establish a transport-neutral design so the later implementation can choose an owner-approved host transport without changing pipeline semantics.

**Non-Goals:**
- No live remote/media/RT run, SSH/Tailscale/SMB setup, artifact copying, server operation, credential handling, or policy/deployment change.
- No alteration of prompts, translation/audit/formatting behavior, model routing, artifact formats, resume identity, or MemoryManager promotion rules.
- No shared writable network directory, two-way sync, merge of mutable memory JSON, or cross-host resume.

## Decisions

- **Canonical state is versioned snapshots, never a shared live work directory.** Media stores a named immutable input/output bundle and one atomically promoted current pointer per book. Alternative: RT/media write the same directory — rejected because journal/sidecar writes and mutable memory do not have distributed locking semantics.
- **Workers stage and execute locally.** Media executes approved remote runs in local media staging; RT executes local-model runs in local RT staging. Each writes only its own staging during a book run and publishes after terminal validation. Alternative: stream per-file changes during a run — rejected because it exposes partial state and breaks causal promotion.
- **RT llama as a media-controlled worker is deferred.** The future `media controller → RT llama worker` mode requires a distinct high-risk design/implementation change for remote command execution, worker authentication, staged handoff, crash recovery, and host policy. It is not implemented or implied by the current media remote-run capability.
- **One writer per book through a lease.** A lease identifies book, snapshot revision, worker host, run ID, owner, start/expiry; publication must match the lease input revision. Alternative: rely on atomic JSON writes — rejected because atomic replacement does not prevent lost updates from two hosts.
- **Publish is bundle-first, pointer-last.** Upload into a new immutable candidate directory, validate manifest/hash/identity/terminal status/PID coverage, then atomically advance `CURRENT`. Alternative: overwrite current files — rejected because a crash can leave mixed generations.
- **No cross-host resume.** Transferred terminal bundles are forensic/release data; resume remains local to the worker and exact identity. Alternative: resume after copy — rejected because endpoint/profile/source/snapshot identity intentionally fail closed.
- **SSH/SFTP plus restricted media promotion command is the approved handoff transport (owner decision 2026-08-25).** RT uploads a whole candidate bundle via SFTP, then invokes a restricted `pact-promote` command over SSH on media. That command validates and atomically promotes or rejects the candidate, returning the verdict to RT. RT's key is limited to bundle upload and this command, not an interactive shell. Direct writable network shares, two-way sync, and a persistent watcher daemon are not permitted.

## Proposed logical layout

```text
<media-store>/books/<book-id>/
  CURRENT.json                         # atomically updated pointer + manifest digest
  locks/<book-id>.lease.json           # active lease; never a live run directory
  incoming/<candidate-id>/              # SFTP upload staging; never CURRENT
  quarantine/<candidate-id>/            # failed/invalid candidate; auto-expire after 30 days
  snapshots/<revision-id>/
    manifest.json
    inputs/                             # originals, memory snapshot, approved config inputs
    runs/<run-id>/                      # immutable terminal chapter/book bundle
    state/                              # glossary, book_memory, chapter_index, ledgers, book_run
```

`manifest.json` contains relative paths, byte SHA-256, sizes, schema/version, book id, chapter order/range, source/snapshot/chunk-plan/config/backend identities, code commit, dependency/profile/registry fingerprints, parent revision, terminal status, and creation/publish timestamps. It MUST exclude credential values, environment dumps, server state, and model caches.

## Owner decisions (2026-08-25)

- **Host roles:** media is the canonical artifact/state authority and is approved for owner-started **remote** runs only. RT remains the owner-started host for local-model runs and may also run remote profiles. A book nevertheless has exactly one active writer/lease at a time. Media-controlled execution against RT-only llama servers is explicitly deferred to a separate future change.
- **Store layout:** use the proposed book-scoped layout under `/home/rt/pact_runs/books/<book-id>/` with `CURRENT.json`, `locks/`, and immutable `snapshots/`.
- **Terminal eligibility:** both `complete` and `accepted_degraded` candidate bundles automatically advance `CURRENT` after all manifest/identity/PID validation succeeds. A terminal status does not waive hard audit/quarantine consistency validation.
- **Retention:** retain immutable accepted revision history and terminal bundles; failed/invalid candidates go to quarantine for 30 days, then are automatically cleaned.
- **Automatic promotion:** RT SFTP-uploads the complete terminal candidate, then invokes a restricted media `pact-promote` SSH command. Media returns `ACCEPTED` with revision/manifest/current evidence or `REJECTED` with reason/quarantine location; RT console reports that cross-host verdict and treats rejection as non-zero exit.
- **Lease recovery:** leases have a TTL, but expiry never triggers automatic takeover. The owner manually releases/replaces a lease after reviewing the prior worker staging/recovery state.
- **Policy prerequisite:** before implementation or any media remote execution, repository operational policy must explicitly recognize media as an approved owner-started **remote** execution host under this protocol; this planning record does not itself run a pipeline or alter host services.

## Publication protocol

1. Acquire the current book lease from media; reject if held or the expected `CURRENT` revision differs.
2. Materialize the referenced immutable snapshot into RT local staging and verify manifest hashes before execution.
3. Run the requested ordered chapter range wholly in the selected worker's local staging; `v4_book_run` owns all in-run promotion there.
4. After terminal completion, construct a candidate bundle and manifest locally; never publish live journals/progress/usage files.
5. Upload the whole candidate bundle to `incoming/<candidate-id>/`, then invoke media's restricted `pact-promote` SSH command. It verifies byte hashes and strict record identity chain, then validates terminal status, no complete-plus-quarantine conflict, PID coverage, and book-state continuity.
6. On acceptance, `pact-promote` atomically moves the validated bundle to `snapshots/<revision-id>/`, writes a new `CURRENT.json` only if the lease still references the same parent revision, releases the lease, and returns revision/manifest/promotion evidence to RT.
7. On rejection, `pact-promote` preserves the prior `CURRENT`, moves the candidate to `quarantine/<candidate-id>/`, returns a machine-readable rejection reason to RT, and the RT command exits non-zero. Quarantine automatically expires after 30 days.

## Risks / Trade-offs

- [Worker crashes after local completion but before publish] → prior media snapshot remains authoritative; a later automated publication retry or manual recovery may reuse intact local staging only after manifest validation and owner-approved lease recovery.
- [Two hosts attempt the same book] → lease and parent-revision compare reject the second writer; no automatic merge.
- [Host transport is unavailable or weak] → do not run; choose and validate transport before implementation rather than falling back to shared write.
- [Accepted-degraded policy admits a weaker terminal state] → it still undergoes the full manifest/identity/PID/quarantine validation before automatic `CURRENT` promotion; immutable history makes rollback possible.
- [Large bundles are expensive] → later implementation may deduplicate immutable files by hash, but never at the cost of a complete manifest.

## Migration Plan

Planning only. Future bootstrap takes a quiescent, owner-selected RT book snapshot, produces the first media manifest, verifies it, and creates `CURRENT`. Rollback is changing `CURRENT` back to a previous validated revision; no production run starts in this change.

## Open Questions

- Exact SSH/SFTP host-key, account, path authorization, restricted-command enforcement, and non-secret operator/bootstrap procedure remain to be designed and validated.
- The repository policy wording for media as an approved owner-started **remote** execution host must be updated in the future implementation/change approval.
- Media-controlled RT-llama worker protocol is deferred to a distinct future high-risk change.
- The RT mirror/adoption pointer mechanism remains to be designed.
- Manual lease-recovery audit record format remains to be designed.