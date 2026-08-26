## 1. Media restricted facade + CLI surface

- [x] 1.1 Add `fetch-current <book-id>` to `pact_v4.snapshot` CLI that streams the current state (CURRENT.json, referenced manifest.json, and the four canonical files) to stdout/tar; verify it returns the exact four files for a seeded book (use the `books/1` store).
- [x] 1.2 Implement `remote_facade.py` (or shell wrapper) that parses subcommand + `book-id`, allows ONLY `fetch-current`, `receive-candidate`, `promote`, `release-lease --check-expired`, and rejects any other subcommand/argument/book-id; verify a disallowed request is refused without side effects.
- [x] 1.3 Have `receive-candidate <book-id> <candidate-id>` read a candidate archive from stdin and write it under `incoming/<candidate-id>/`; verify the candidate lands where media `promote` expects it.

## 2. RT remote client (system ssh/scp, offline store untouched)

- [x] 2.1 Create `pact_v4/snapshot/remote_client.py` that shells out to the system `ssh`/`scp` (no SSH library, no code in the store package); verify `pact_v4/snapshot` store modules still import with zero network imports.
- [x] 2.2 Implement `fetch_current(book_id)` → downloads the four canonical files to a target dir; verify the four files arrive and nothing else.
- [x] 2.3 Implement `push_candidate(book_id, candidate_id, local_dir)` → `receive-candidate` + `promote` over SSH, returning the parsed media verdict; verify it surfaces ACCEPTED/`revision_id` and REJECTED/reason.
- [x] 2.4 Implement `check_expired(book_id)` via `release-lease --check-expired`; verify the read-only report is returned.

## 3. Run-hook integration (v4 run command)

- [x] 3.1 Add a pre-init hook to the v4 run command that calls `remote_client.fetch_current`, validates the four files (regular, non-symlink, allowed names, valid JSON) before `MemoryManager` init, and fails fast on media unreachable; verify the run starts from media-authoritative state and fails clearly when media is down.
- [x] 3.2 Add a post-`MemoryManager.promote('complete')` hook that builds the candidate (manifest + four canonical files from the working dir), calls `remote_client.push_candidate`, and records the returned `revision_id` as confirmation; verify the confirmation is captured on ACCEPTED.
- [x] 3.3 Handle `STALE_PARENT` with a bounded re-pull + retry (default 1) and otherwise report the rejection; verify a stale base triggers one retry then a clear report.
- [x] 3.4 Ensure rejected/transport failures preserve local RT state and are reported, not silently dropped; verify local `pact_chapters\` is intact after a failed push.

## 4. Tests

- [x] 4.1 Facade allow-list tests: allowed subcommands execute, disallowed subcommand / wrong `book-id` are rejected; verify with a mocked store.
- [x] 4.2 Client tests with a fake/loopback SSH target (or injected transport) confirming `fetch_current` returns exactly four files, `push_candidate` returns ACCEPTED/`revision_id`, and `STALE_PARENT` surfaces; verify no network import in the store package.
- [x] 4.3 Run-hook integration test: fake media returning a state → run pulls → promotes → pushes → confirmation recorded; and stale-parent retry path; verify state-only boundary (no translation bodies moved).
- [x] 4.4 Reuse the existing boundary/negative matrix (extra file, symlink, special file, post-lock mutation) to confirm the end-to-end path rejects smuggled candidates; verify the same 35+ snapshot tests still pass.

## 5. Docs / ops

- [x] 5.1 Document the `authorized_keys command=` snippet (dedicated RT key, `restrict`, `no-pty`, wrapper path + allowed args) as owner host-config; verify the snippet matches the facade's allow-list.
- [x] 5.2 Add a runbook section: RT run now pulls at start / pushes+confirms at end; what to do on media unreachable or `STALE_PARENT`; how to recover with `release-lease`.
