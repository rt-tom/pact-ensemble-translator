## Why

The current `v4_run book` launcher assumes Windows paths, resolves only exact `{chapter_id}.html` names, and defaults mutable book state into the source directory. It cannot safely run the same book command on RT and media, while real chapter files use `NNNN_<variable-suffix>.html` names and the approved RT↔media state synchronization must remain fail-closed.

## What Changes

- Add host-aware, book-first launcher profiles for RT and media with separate source, mutable state/staging, and output roots.
- Resolve numeric chapter ranges deterministically from exactly one `NNNN_*.html` source file per number; reject absent or ambiguous matches before a pipeline starts.
- Extend offline launcher preflight to validate the complete requested source range and local writable state/output locations without contacting model providers.
- Make `D:\pact\book_state` the RT mutable state directory, never the source directory; stop duplicating fetched state into an unused local `state/` subdirectory.
- Provide simple explicit selection: `--local` or `--remote [translator/reviewer]`. Bare `--remote` uses remote-profile defaults; supplied role aliases override them. Remote defaults use Muse Free for translator/repair, Luna for the standard reviewer roles, reasoning level `3`, and `--managed-server`; all launcher variants enable `--whole-chapter` by default.
- Enable the existing state-only media synchronization by default for every simple book run, local or remote, on RT and media with book id `1`, target `media-snap`, and media root `/home/rt/pact_runs`. The media-host path must avoid a self-SSH loop while retaining equivalent fail-closed facade validation and a terminal media publish verdict.
- Retain explicit compatibility overrides for non-default book ID and advanced invocation paths; local RT runs fetch current media state before initialization and publish accepted updates afterward just as remote runs do. Do not change the separate future full terminal-bundle artifact protocol.

## Capabilities

### New Capabilities
- `host-aware-book-launcher`: Resolve, preflight, and launch a book range consistently on RT or media while isolating host-local inputs, mutable state, outputs, and media synchronization.

### Modified Capabilities
- `v4-run-command-interface`: Replace the provisional runtime-config-first book UX with host-aware `--local`/`--remote` selection and truthful defaults/help.
- `runtime-profile-contract`: Make the approved remote profile's default models and reasoning policy explicit for the simplified remote mode.

## Impact

Affected areas include `pact_full_pipeline_runner_v1/v4_run.py`, `v4_book_run.py`, `pact_v4/runtime/runtime_config.py`, `pact_v4/snapshot/remote_client.py`, runtime and dispatcher tests, and `configs/runtime_remote.example.yaml`. This is high risk because it changes execution defaults, source/state boundaries, and state synchronization behavior; no live pipeline, provider call, or host-service change is included.
