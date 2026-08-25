## Context

The planning change `book-state-snapshot-handoff` (committed) defines the protocol and owner decisions but contains no code. The first implementation slice is media-side only and fully offline-testable. RT remains the local-model execution host; media is the canonical, immutable, single-writer authority for book *state*. The authoritative RT book state is `D:\pact\pact_chapters` (`book_memory.json`, `glossary.json`, `chapter_index.json`, `observations.json`), which is the `MemoryManager(base_dir)` convergence point.

The current `pact_chapters\book_memory.json` SHA-256 (`F4E45434…BE8D`) matches no `book_memory_hash_after` recorded in any `gate_bench_runs\v4_book_*\book_run.json`, proving those run ledgers were overwritten per run and are not a complete promotion history. The converged `pact_chapters` directory is therefore taken as the authoritative seed directly — it is not reconstructed from run ledgers.

## Goals / Non-Goals

**Goals:**
- Media-side immutable book-state store with manifest validation and atomic `CURRENT` promotion.
- Promote-time lease mutex that prevents lost updates and rejects stale/concurrent writers fail-closed.
- Bootstrap the first revision from an owner-copied `pact_chapters` bundle.
- Quarantine rejected candidates; owner-only lease release with audit.
- Fully offline tests.

**Non-Goals (this slice):**
- No SSH/SFTP client implementation; only a documented restricted `command=` snippet. Key generation and `authorized_keys` edits are owner ops outside the repo.
- No per-chapter translated bodies in the snapshot (Scope A); the 27-folder selection map is deferred.
- No pipeline execution, model-server operation, remote provider calls, or production runs from media.
- No two-way sync, shared writable directory, or cross-host resume.
- RT llama as a media-controlled worker is deferred to a separate future high-risk change.

## Decisions

- **Store root:** `/home/rt/pact_runs/books/<book-id>/` (media). Default overridable via `--root` for tests.
- **Scope A (state-only):** snapshot `state/` holds exactly the four canonical `pact_chapters` files. All other `pact_chapters` contents (`*_candidates.json`, `*_v3_archive.json`, `glossary_owner_review_*`, `manual_promote_0007_backup_*`, `.bak-*`, `README`) are excluded from the immutable snapshot by default.
- **Revision identity:** bootstrap revision is `rev-0001` with `parent_revision_id = null`. Subsequent promotions get monotonic `rev-NNNN` ids; the manifest's `parent_revision_id` MUST equal the current `CURRENT.json` revision (fail-closed).
- **Lease is a promote-time mutex, not a run-time lock (Q11):** `pact-promote` acquires the lease briefly at publish time via an atomic compare-and-swap write of `locks/<book-id>.lease.json`. A crashed run never holds a lease (no promote occurred), so the next run is never blocked by a dead lock. A stale lease from a crashed promote is NOT auto-released; `pact-promote` rejects with `LEASE_HELD` and requires `pact-release-lease`. Automatic TTL takeover is prohibited (Q8).
- **Publish is bundle-first, pointer-last (planning decision):** validate the whole candidate under `incoming/<candidate-id>/`, then atomically move it to `snapshots/<revision-id>/`, then atomically rename `CURRENT.json`. A crash leaves the prior `CURRENT` intact.
- **Rejection is non-destructive:** on any validation or lease failure, the candidate is moved to `quarantine/<candidate-id>/` and `pact-promote` exits non-zero with a machine-readable reason. Prior `CURRENT` is preserved.
- **Bootstrapping is owner-only and lease-free:** `pact-bootstrap` runs after the owner manually copies the quiescent `pact_chapters` into `_bootstrap_inbox/<ts>/`. It is not exposed through the restricted promote path.

## Manifest schema (Scope A minimal)

`snapshots/<revision-id>/manifest.json`:
```json
{
  "schema_version": "1.0.0",
  "book_id": "pact-book-ru",
  "revision_id": "rev-0001",
  "parent_revision_id": null,
  "created_at": "2026-08-26T12:00:00Z",
  "published_at": "2026-08-26T12:00:00Z",
  "terminal_status": "bootstrap-seed",
  "tool_version": "pact-snapshot/0.1.0",
  "source": { "path_on_rt": "D:\\pact\\pact_chapters", "operator": "rt", "host": "RT" },
  "state_files": [
    { "rel_path": "state/book_memory.json", "sha256": "<hex>", "size": 36513 }
  ],
  "excludes": ["glossary_candidates.json", "book_memory_v3_archive.json"],
  "code_commit": "unknown"
}
```
`terminal_status` ∈ {`bootstrap-seed`, `complete`, `accepted_degraded`}. `state_files[].rel_path` is relative to the snapshot directory (always under `state/`). The manifest MUST NOT include credentials, environment, server state, or model caches.

`CURRENT.json` (atomically renamed pointer + evidence):
```json
{
  "book_id": "pact-book-ru",
  "revision_id": "rev-0001",
  "manifest_sha256": "<hex>",
  "published_at": "2026-08-26T12:00:00Z",
  "operator": "rt",
  "host": "RT",
  "run_id": null,
  "lease_id": null,
  "parent_revision_id": null
}
```

`locks/<book-id>.lease.json` (promote-time mutex):
```json
{
  "book_id": "pact-book-ru",
  "lease_id": "lease-<uuid>",
  "revision_id": "rev-0001",
  "operator": "rt",
  "host": "RT",
  "acquired_at": "2026-08-26T12:00:00Z",
  "expires_at": "2026-08-26T13:00:00Z",
  "run_id": "v4_book_0025_..."
}
```
`expires_at` is informational only; expiry NEVER triggers automatic takeover.

`locks/<book-id>.lease_audit.jsonl`: append-only JSONL release records (`{book_id, lease_id, action, reason, operator, ts, prior_staging_reviewed, recovery_decision}`).

## Logical layout

```text
<root>/books/<book-id>/
  CURRENT.json                         # atomically renamed pointer + evidence
  locks/<book-id>.lease.json           # promote-time mutex; absent when no promote in flight
  locks/<book-id>.lease_audit.jsonl    # owner-only release audit trail
  incoming/<candidate-id>/              # uploaded candidate staging; never CURRENT
  _bootstrap_inbox/<ts>/               # owner-copied pact_chapters bundle (read-only source)
  quarantine/<candidate-id>/            # rejected candidate; auto-expire after 30 days
  snapshots/<revision-id>/
    manifest.json
    state/                             # book_memory.json, glossary.json, chapter_index.json, observations.json
```

## Module layout

`pact_v4/snapshot/`:
- `errors.py` — `SnapshotError` hierarchy (`ValidationError`, `LeaseHeld`, `StaleParent`, `HashMismatch`).
- `manifest.py` — schema dataclass, serialization, hash computation, validation.
- `store.py` — path resolution, `init_store`, atomic write/rename helpers.
- `bootstrap.py` — seed first revision from `_bootstrap_inbox/<ts>/`.
- `promote.py` — validate + lease mutex + atomic promote + quarantine.
- `lease.py` — acquire/release lease, audit append.
- `cli.py` — argparse subcommands `init-store`, `bootstrap`, `promote`, `release-lease`; JSON verdicts on stdout, exit 0 accept / non-zero reject.

## Promotion protocol (pact-promote)

1. Read candidate manifest + `state/` files from `incoming/<candidate-id>/`.
2. Validate manifest schema; verify each `state_files` entry's byte SHA-256 and size against the actual file.
3. Require `terminal_status` ∈ {`complete`, `accepted_degraded`, `bootstrap-seed`}.
4. Read current `CURRENT.json` → `current_revision`. Require `manifest.parent_revision_id == current_revision` (fail-closed `StaleParent` otherwise).
5. Acquire lease mutex: atomic CAS write of `locks/<book-id>.lease.json` bound to `current_revision`. If a lease file already exists → `LeaseHeld` reject (manual `pact-release-lease` required).
6. Generate `revision_id = rev-<next>`; atomically move candidate to `snapshots/<revision-id>/`.
7. Re-check lease still references `current_revision`; write new `CURRENT.json` via atomic rename referencing the new revision and manifest hash.
8. Release lease (delete `locks/<book-id>.lease.json`); print `ACCEPTED` with revision/manifest/current evidence.
9. On any failure → move candidate to `quarantine/<candidate-id>/`, print `REJECTED` with machine-readable reason, exit non-zero. Prior `CURRENT` preserved.

## Transport (documented, not implemented this slice)

RT SFTP-uploads the whole candidate bundle to `incoming/<candidate-id>/`, then invokes a restricted media `command=` over SSH:
```
command="/home/rt/pact_runs/venv/bin/python -m pact_v4.snapshot.cli promote <book-id> <candidate-id>",restrict,no-pty <rt-public-key>
```
RT's key is limited to bundle upload and this command, not an interactive shell. Actual key generation and `authorized_keys` edits are owner operations outside the repository.

## Risks / Trade-offs

- [Stale lease from crashed promote] → `pact-promote` rejects `LEASE_HELD`; owner reviews prior staging and runs `pact-release-lease` (audit-recorded). No automatic takeover.
- [Two promotes race] → lease CAS + parent-revision compare reject the second writer; no merge.
- [Bad/partial upload] → validation fails, candidate quarantined, prior `CURRENT` preserved.
- [accepted_degraded admits weaker state] → still undergoes full manifest/hash/terminal validation before promotion; immutable history allows rollback.
- [Large future bundles] → later implementation may deduplicate by hash; never at the cost of a complete manifest.

## Migration Plan

Bootstrap takes a quiescent, owner-selected RT `pact_chapters` snapshot, produces `rev-0001` + `CURRENT.json`, verified by manifest hash. Rollback is changing `CURRENT.json` back to a previous validated revision. No production run starts in this change.

## Open Questions (resolved for this slice)

- SSH/SFTP host-key/account/path authorization and restricted-command enforcement: documented as a snippet; actual setup is owner ops outside the repo.
- Repository policy wording for media as an approved owner-started remote execution host: tracked in the planning change; this implementation does not alter host services.
- Per-chapter translated-body collection (`pact-collect-book`) is deferred.
