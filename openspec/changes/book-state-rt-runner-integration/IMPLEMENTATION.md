# Implementation — book-state-rt-runner-integration

**Status:** Implemented — all 17 tasks verified against `origin/main` at `f79e139` + `v4_book_run.py:1104/1651`.

**Date:** 2026-08-26
**Branch merged via:** `v4_book_run.py` + `pact_v4/snapshot/*` (RT↔media state-only sync)

## What was implemented (state-only, 4 canonical files)

**1. Media restricted facade + CLI (3 tasks)**
- `pact_v4/snapshot/cli.py:264` `fetch-current <book-id>` streams `CURRENT.json` + `manifest.json` + `state/*` (4 files) as tar
- `pact_v4/snapshot/remote_facade.py:30` `ALLOWED_SUBCOMMANDS = {fetch-current, receive-candidate, promote, release-lease}` — rejects others
- `receive-candidate <book-id> <candidate-id>` writes to `incoming/<candidate-id>/` where `promote` expects it

**2. RT remote client (4 tasks)**
- `pact_v4/snapshot/remote_client.py:239 fetch_current`, `:309 push_candidate`, `check_expired` — shells out to system `ssh`/`scp`, no SSH library, store package has zero network imports
- `fetch_current` returns 4 files, `push_candidate` returns `ACCEPTED/revision_id` or `STALE_PARENT`

**3. Run-hook integration (4 tasks) — in `pact_full_pipeline_runner_v1/v4_book_run.py:1104` + `:1651`**
- Pre-init: `pre_init_fetch(book_id, working_dir)` before `MemoryManager` — validates 4 files (regular, non-symlink, allowed names, valid JSON), fail-fast on media unreachable
- Post-promote: `post_promote_push` after `MemoryManager.promote('complete')` — builds candidate, pushes, records `revision_id`
- `STALE_PARENT` bounded retry (1) with re-pull
- Rejected/transport failures preserve `D:\pact\pact_chapters\` (local state intact)

Library: `pact_v4/snapshot/run_hooks.py:37 pre_init_fetch`, `:54 post_promote_push`, `:152 run_book_with_media_sync`

**4. Tests (4 tasks)**
- `tests/pact_v4/snapshot/test_remote_facade.py` — allow-list
- `tests/pact_v4/snapshot/test_remote_client.py` (420 lines) — fake/loopback SSH
- `tests/pact_v4/snapshot/test_run_hooks.py` (301 lines) — integration
- `tests/pact_v4/snapshot/test_boundary_matrix.py` — 35+ boundary checks still pass

**5. Docs / ops (2 tasks)**
- `authorized_keys command="pact-snapshot ..."` snippet (owner host-config, `restrict, no-pty`)
- Runbook: pull at start / push+confirm at end, handling `STALE_PARENT` and `release-lease --check-expired`

**Verification:** `grep` for `pre_init_fetch` at `v4_book_run.py:1104` + `post_promote_push` at `:1651` on both `media` and `D:\pact\pact_translator_v4_1` (both at `9e4757a` + `f79e139`), `pytest tests/pact_v4/snapshot/test_run_hooks.py` etc. All 17 tasks now marked `[x]` in `tasks.md`.
