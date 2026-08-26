# Pact Translator v4

`main` now carries the v4 development tree. v3 is archived
(`archive/v3-main-20260802`, tags `v3.1.3*`) and no longer used.

> **Production runs are owner-started on RT only** (`D:\pact\pact_translator_v4_1`).
> Do not start pipelines from the `media` dev host or from worktrees.
> Agents inspect code and artifacts only.

## v4 navigation

Architecture and plans:

- `docs/architecture/V4_MVP_SPEC_RU.md` — canonical v4 MVP spec
- `docs/architecture/V4_FINAL_REVIEW_AND_IMPLEMENTATION_PLAN_RU_v2.md` — final review and implementation plan
- `docs/plans/V4_IMPLEMENTATION_ORDER_PLAN_RU.md` — implementation order
- `docs/plans/V4_OPENCODE_REMOTE_MODELS_INTEGRATION_PLAN_RU.md` — remote models integration

Pipeline and runtime:

- `pact_v4/pipeline/` — pipeline entry points (`v4_phase12_strict_runner.py` is the production driver)
- `pact_v4/runtime/` — runtime backends and coordination
- `configs/runtime_local.example.yaml`, `configs/runtime_remote.example.yaml`, `configs/runtime_composite.example.yaml` — runtime config templates
- `configs/providers.yaml` — provider/model registry for `--translator`/`--reviewer` aliases (case-insensitive, fail-closed on duplicates)
- `pact_v4/pipeline/phase_progress.py` — progress reporting (see `docs/plans/V4_PHASE12_RUN_PROGRESS_TRACKER_TASK_RU.md`)

## v4 run command (unified dispatcher, book-first, host-aware)

The supported launch surface is the thin dispatcher `pact_full_pipeline_runner_v1.v4_run` with primary `book`
and retained `chapter` modes. It forwards to the existing strict/book entrypoints without changing pipeline semantics.
Simple book mode is host-aware (RT vs media) with deterministic source discovery; advanced `--runtime-config` remains for compatibility.

```powershell
# Simple — single chapter 28, local runtime (whole-chapter enabled, media sync book 1)
python -m pact_full_pipeline_runner_v1.v4_run book --chapters 28 --local

# Simple — chapters 28-32, remote runtime defaults (Muse Free translator/repair + Luna reviewer, reasoning 3, managed server)
python -m pact_full_pipeline_runner_v1.v4_run book --chapters 28-32 --remote

# Simple — remote with explicit bare aliases (translator/reviewer)
python -m pact_full_pipeline_runner_v1.v4_run book --chapters 28 --remote musefree/luna

# Simple — remote on another book (override media book id)
python -m pact_full_pipeline_runner_v1.v4_run book --chapters 28 --remote --media-book-id 2

# Advanced — explicit runtime profile (compatibility)
python -m pact_full_pipeline_runner_v1.v4_run book --chapters 27-32 --runtime-config configs/runtime_remote.example.yaml --translator opencode-go/musefree --reviewer openai/luna --reasoning 3

# Advanced — explicit markup (preserve only)
python -m pact_full_pipeline_runner_v1.v4_run book --chapters 27-32 --runtime-config configs/runtime_local.example.yaml --markup preserve

# Chapter — single strict chapter (retained)
python -m pact_full_pipeline_runner_v1.v4_run chapter --chapter-id 0001 --chapter-html D:/pact/pact_chapters/0001_bonds-1-1.html --memory-dir D:/pact/book_state --out-dir D:/pact/gate_bench_runs/chapter_0001
```

Host-aware layout:
- RT: source `D:/pact/pact_chapters` (read-only, owner-managed mirror), mutable state `D:/pact/book_state`, outputs `D:/pact/gate_bench_runs`
- media: source `/home/rt/pact_chapters` (canonical 150 HTML), state `/home/rt/pact_runs/workers/media/book-1/state`, outputs `/home/rt/pact_runs/outputs`
- Canonical media snapshot storage (`/home/rt/pact_runs/books/1`) is never used as mutable state. Source and state roots must not be the same directory.

Source naming: each numeric chapter `N` must match exactly one regular non-symlink file `NNNN_*.html` in the host source root (e.g., `0149_judgment-16-13.html`). Zero, multiple, symlink, FIFO/socket/device, or unreadable matches fail before pipeline startup. Use `NNNN_*.html` discovery, not fabricated `NNNN.html`.

Runtime profile defaults: the selected profile supplies default role models, reasoning, transport, and
identity-bearing policy. Remote canonical defaults are Muse Free generator/repair (`opencode/muse-spark-1.2-contributor-free`), Luna standard reviewer roles (`openai/gpt-5.6-luna`), reasoning 3, managed server, and whole-chapter for every book run. Omitted `--translator`/`--reviewer`/`--reasoning` use profile values; explicit values are validated against the runtime/provider contract, are identity-bearing, and bare alias selection is globally unique and fail-closed.

Media prerequisites: every simple book run (local or remote, RT or media) fetches current media state before `MemoryManager` init and publishes accepted updates afterward. Defaults are book `1`, target `media-snap`, root `/home/rt/pact_runs`. On media the local restricted facade is used (no self-SSH). A final `MEDIA PUBLISH: ACCEPTED` or `REJECTED` verdict is printed; rejection is non-zero and preserves local diagnostics. SSH `media-snap` must be a restricted facade key (see Book-state snapshot handoff below). Use `ssh media-snap pact-snapshot ...` only via the dispatcher; never direct shared writes.

Offline preflight (host-local, no network/model/source/artifact side effects) runs by default before every
configured execution and before any output directory is created:
```powershell
python -m pact_full_pipeline_runner_v1.v4_run book --chapters 28 --local --preflight
python -m pact_full_pipeline_runner_v1.v4_run book --chapters 28 --remote --preflight --json
# alias: --preflight-json
```
Check-only modes report the sanitized resolved profile, model bindings, effective policy, topology, identity, selected host layout, resolved chapter file names, and exit without starting the pipeline or creating artifacts. Source/state/output readiness is validated without network or state-sync side effects.

Output naming: a distinct subdirectory below the host output root is created automatically,
named `book_0028_local_<timestamp>` or `book_0027-0032_remote_<timestamp>` — the `local|remote` label
is derived from the resolved runtime descriptor after profile defaults and explicit overrides.

Whole-chapter and managed-server: `--whole-chapter` is injected for every book run; `--managed-server` is injected for every simple remote run. Explicit incompatible topology flags fail rather than silently override.

Help is offline-only (no pipeline/model/artifact side effects):
```powershell
python -m pact_full_pipeline_runner_v1.v4_run --help
python -m pact_full_pipeline_runner_v1.v4_run book --help
python -m pact_full_pipeline_runner_v1.v4_run chapter --help
```

No live pipeline, provider, SSH/key/server, or source download is performed by the dispatcher or preflight. Deployment is only `git pull --ff-only` on RT; rollback selects the advanced explicit invocation.

Historical `run_full_pipeline*.ps1` / v3 launchers are not supported v4 commands.

Workspace:

- `AGENTS.md` — workspace safety, review workflow, mandatory skills
- `DECISIONS.md` — architectural decisions
- `docs/` — additional architecture, plans, and handoff notes

## Book-state snapshot handoff — Scope A (state-only)

Media-side immutable store for book *state* only (`book_memory.json`, `glossary.json`, `chapter_index.json`, `observations.json` copied from the authoritative RT `D:\pact\pact_chapters` directory). Per-chapter translated bodies are NOT included (deferred to `pact-collect-book`). No pipeline, model-server, or provider calls are made from media; all commands are offline filesystem operations.

Store layout (default root `/home/rt/pact_runs`):

```
<root>/books/<book-id>/
  CURRENT.json                       # atomically renamed pointer
  locks/<book-id>.lease.json         # promote-time mutex (absent when idle)
  locks/<book-id>.lease_audit.jsonl  # owner-only release audit
  incoming/<candidate-id>/           # uploaded candidate staging
  _bootstrap_inbox/<ts>/             # owner-copied pact_chapters bundle
  quarantine/<candidate-id>/         # rejected candidate
  snapshots/<revision-id>/           # immutable: manifest.json + state/
```

### Bootstrap (owner-only, first revision)

1. Quiesce RT `pact_chapters` and copy it into `_bootstrap_inbox/<ts>/` on media (manual `scp`/`rsync` or SFTP — no SSH/SFTP client is implemented in this slice).
2. Run `python -m pact_v4.snapshot.cli --root <root> bootstrap <book-id>` (or `pact-bootstrap`). The command selects exactly the four canonical state files, validates well-formed JSON, records other files in `excludes[]`, writes `snapshots/rev-0001/state/` + `manifest.json`, and atomically advances `CURRENT.json` to `rev-0001`. Non-JSON canonical files cause a fail-closed error with no `CURRENT` advance.
3. Subsequent snapshots use `promote`, not `bootstrap` (`bootstrap` rejects if snapshots already exist).

```bash
# Example (media host):
python -m pact_v4.snapshot.cli --root /home/rt/pact_runs init-store pact-book-ru
# owner copies D:\pact\pact_chapters -> /home/rt/pact_runs/books/pact-book-ru/_bootstrap_inbox/20260826T120000Z/
python -m pact_v4.snapshot.cli --root /home/rt/pact_runs bootstrap pact-book-ru
```

### Promote (candidate bundle)

RT uploads a complete candidate to `incoming/<candidate-id>/` (`manifest.json` + `state/`), then media runs:

```bash
python -m pact_v4.snapshot.cli --root /home/rt/pact_runs promote pact-book-ru <candidate-id>
```

`pact-promote` validates the manifest schema (strict allow-list — unknown/credential/env/server/model-cache fields are rejected), verifies each `state_files` byte SHA-256/size, requires an eligible `terminal_status`, requires `parent_revision_id` equal to the current revision (fail-closed `StaleParent`), acquires the promote-time lease mutex via atomic `O_EXCL` (`LeaseHeld` on contention), atomically moves the bundle to `snapshots/<next-rev>/`, writes `CURRENT.json` via atomic rename only while the lease still references the same parent, releases the lease, and prints `ACCEPTED` (exit 0). On any failure the candidate is moved to `quarantine/<candidate-id>/`, prior `CURRENT.json` is preserved, and a `REJECTED` JSON verdict is printed (exit 2).

### Lease recovery (owner-only)

A crashed promote may leave `locks/<book-id>.lease.json` held. Automatic TTL takeover is prohibited; expiry is informational only. Media does NOT auto-release. The owner reviews prior staging and runs:

```bash
python -m pact_v4.snapshot.cli --root /home/rt/pact_runs release-lease pact-book-ru --operator rt --reason "stale promote crashed" --prior-staging-reviewed --recovery-decision released
```

This appends a JSONL record to `locks/<book-id>.lease_audit.jsonl` and clears the lease. `release-lease --check-expired` is a read-only report that never deletes or audits.

### RT<->media state-only sync (book-state-rt-runner-integration)

RT syncs the four canonical `pact_chapters` files (`glossary.json`, `book_memory.json`, `chapter_index.json`, `observations.json`) with media via a restricted SSH facade.

**Media facade** (`pact_v4/snapshot/remote_facade.py`) invoked through `authorized_keys command=` — allow-list: `fetch-current <book-id>`, `receive-candidate <book-id> <candidate-id>` (tar on stdin), `promote <book-id> <candidate-id>`, `release-lease <book-id> --check-expired`. Any other subcommand/argument/book-id is rejected without side effects.

**RT client** (`pact_v4/snapshot/remote_client.py`) shells out to system `ssh`/`scp` (no library), injectable transport for tests: `fetch_current(book_id, dest_dir)`, `push_candidate(book_id, candidate_id, local_dir)` (=receive+promote verdict), `check_expired(book_id)`.

**Run hooks** (`pact_v4/snapshot/run_hooks.py`) in `pact_full_pipeline_runner_v1/v4_book_run.py` (`--media-book-id`):
- Pre-init: `fetch_current` -> validate four files (regular, non-symlink, allowed names, valid JSON) -> `MemoryManager` init. Fail-fast on unreachable (no stale fallback).
- Post-`promote('complete')`: build candidate manifest+four files, `push_candidate`, record `revision_id`; `STALE_PARENT` -> bounded re-pull+retry (1) then report; other rejections/transport failure -> report, preserve local state.

**Owner host-config** (`~/.ssh/authorized_keys` on media, dedicated RT key):
```
restrict,command="PACT_SNAPSHOT_BOOK_ID=pact-book-ru /home/rt/pact_runs/venv/bin/python -m pact_v4.snapshot.remote_facade",no-pty,no-port-forwarding,no-X11-forwarding,no-agent-forwarding <rt-public-key>
```
The `PACT_SNAPSHOT_BOOK_ID=<id>` prefix scopes the facade to exactly one book-id (fail-closed — unset/empty rejects every request); the deployed facade MUST be scoped, never unscoped.
Dedicated key only; `restrict` + `no-pty`; wrapper path is `remote_facade.py` (not direct CLI) so only the four allow-listed subcommands execute. The `command=` string itself is host-config outside the repo; the repo ships wrapper + CLI surface `fetch-current`/`receive-candidate`.

**Runbook**
- Normal: `python -m pact_full_pipeline_runner_v1.v4_book_run --memory-dir <dir> --media-book-id pact-book-ru ...` pulls at start, pushes+confirms at end (confirmation = promote `revision_id`).
- Media unreachable at start: run fails fast, local `pact_chapters` intact, no silent fallback. Fix SSH/network, re-run.
- `STALE_PARENT` on push: run re-pulls and retries once (bounded). If still stale, reports rejection; re-run to pick up newest media revision.
- Other REJECTED (`VALIDATION_ERROR`, `HASH_MISMATCH`, `LEASE_HELD`): reported, local state preserved, candidate quarantined on media. Recover lease with `release-lease --operator ... --reason ... --prior-staging-reviewed` after reviewing `quarantine/<candidate-id>/` and `CURRENT.json`.
- Read-only lease check: `ssh media pact-snapshot release-lease <book-id> --check-expired` (facade allow-listed).
- State-only: exactly the four canonical files move; translation bodies never.

## v3 (archived, non-operational)

v3 code and prior operational procedures are archived and not executed from this tree.
All former v3 operational paths and scripts referenced in earlier README versions
no longer exist in the v4 tree and are not used.
For historical v3 context, see `archive/v3-main-20260802` and tags `v3.1.3*`.
