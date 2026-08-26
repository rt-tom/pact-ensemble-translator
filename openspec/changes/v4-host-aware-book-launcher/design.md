## Context

See `proposal.md`. `v4_run.py` currently hard-codes RT paths and exact `{chapter_id}.html`; `v4_book_run.py` consumes a mutable `memory_dir`; and the merged Scope-A media integration transfers only the four canonical state JSON files plus `CURRENT.json`/`manifest.json`. It does not transfer source HTML or full terminal artifacts.

The canonical media source is `/home/rt/pact_chapters` (150 owner-approved HTML files). RT retains its stable, owner-managed local mirror at `D:\pact\pact_chapters`; the launcher neither modifies nor synchronizes source HTML. RT's existing mutable state root is `D:\pact\book_state`.

## Goals / Non-Goals

**Goals:**
- Make the simple book command deterministic on RT and media without using a source directory for mutable state.
- Fail before pipeline startup for bad runtime prerequisites or unresolved source chapters.
- Make simple-mode remote policy explicit and identity-bearing.
- Reuse the reviewed Scope-A state synchronization with host-appropriate transport and an operator-visible final verdict.

**Non-Goals:**
- No live run, provider probe, server setup, SSH-key setup, or source download.
- No transfer or merge of HTML sources; RT source maintenance remains owner-managed.
- No replacement of Scope-A state synchronization with the later full terminal-bundle protocol.
- No composite-runtime simplification; composite stays advanced/test-only.

## Decisions

- **Book layout is selected by execution host, not embedded in the portable remote runtime profile.** Runtime profiles remain transport/policy documents. A separate declarative launcher layout defines:

  ```text
  RT:    source D:\pact\pact_chapters
         state  D:\pact\book_state
         output D:\pact\gate_bench_runs
  media: source /home/rt/pact_chapters
         state  /home/rt/pact_runs/workers/media/book-1/state
         output /home/rt/pact_runs/outputs
  ```

  The media state/output paths are outside `/home/rt/pact_runs/books/1`, which remains canonical snapshot storage. Alternative: one cross-host path/default — rejected because Windows and Linux paths differ and shared mutable state is unsafe.

- **Chapter discovery replaces fabricated filenames.** Resolve each numeric range item with a non-recursive `NNNN_*.html` scan in the selected source root, require one regular non-symlink result, and pass its actual full chapter identifier/path to existing book execution. Alternative: fixed `{chapter_id}.html` pattern — rejected because Pact has variable title suffixes. Explicit advanced patterns remain compatibility-only and are subject to the same uniqueness/safety validation.

- **Preflight has two offline layers.** First resolve runtime config/aliases/reasoning and host-local runtime checks; then resolve the requested chapter files and check state/output readiness. It must not create directories or fetch/publish state in check-only mode. Alternative: rely on late `FileNotFoundError` — rejected because it creates confusing partial setup and hides host-path mistakes.

- **Simple CLI is profile-selecting, not profile-editing.** `--local` selects the canonical local profile. `--remote translator/reviewer` selects the canonical remote profile; defaults are Muse Free translator/repair, Luna standard reviewer roles, reasoning 3, and managed server. A provider-qualified compatibility form remains accepted where the registry requires it. Existing explicit `--runtime-config` is advanced mode. `--whole-chapter` is injected for every book run; incompatible explicit topology choices fail rather than silently override the simple policy.

- **Scope-A state sync remains the launcher integration.** Simple remote mode injects media book id `1`, target `media-snap`, root `/home/rt/pact_runs`, with explicit book-id override. RT uses the restricted SSH facade. Media selects a local facade adapter offering the same restricted operations and validation, avoiding a self-SSH dependency. The adapter produces machine-readable acceptance/rejection data; launcher output summarizes the final publication outcome and returns non-zero on rejection/missing confirmation.

- **One local copy of fetched state.** Fetch writes canonical mutable JSON files in the working state root plus `CURRENT.json` and `manifest.json` needed for parent binding. It no longer writes a duplicate `state/` subtree. Existing duplicate directories are not silently deleted by this change; migration is operator-reviewed.

## Risks / Trade-offs

- [RT and media sources diverge] → RT is owner-managed; preflight proves only the selected local files. Source synchronization/hash-manifest design is explicitly out of scope.
- [Numeric discovery changes logical chapter IDs] → test exact full-name forwarding and reject ambiguity instead of guessing.
- [Simple defaults alter an established invocation] → keep explicit runtime-config path and make defaults preflight-visible/identity-bearing.
- [Media local adapter broadens authority] → reuse the facade allowlist and validation; do not construct store operations directly from the launcher.
- [Output/state preflight races with later filesystem changes] → use preflight for early diagnostics and revalidate required paths at execution boundary.
- [Existing state-only protocol lacks full run artifacts] → terminal full-bundle protocol remains an explicitly separate change; do not claim artifact handoff completeness.

## Migration Plan

1. Add no-network tests for RT/media layout resolution, unique/missing/ambiguous discovery, preflight side effects, forwarded defaults, and acceptance/rejection verdicts.
2. Implement the new simple mode additively while retaining explicit `--runtime-config` compatibility.
3. Update the remote example profile and help/documentation.
4. On RT, owner verifies the existing `D:\pact\book_state` content and removes the redundant child `state/` only after confirming it is a duplicate; no automatic deletion.
5. Deploy only by owner-approved `git pull --ff-only`; run no pipeline as part of deployment. Rollback selects the advanced explicit invocation and reverts launcher/profile changes; canonical source and media snapshots are untouched.
