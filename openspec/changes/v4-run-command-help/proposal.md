## Why

The supported v4 launch surface is split between the strict chapter CLI and book runner, while historical v3/v31 commands remain visible in repository history. Operators need one simple, explicit v4 invocation path and complete self-describing `--help` output so they can select inputs, outputs, runtime config, whole-chapter mode, and formatting/tag policy without consulting stale scripts or guessing defaults.

## What Changes

- Define and implement a simplified, supported book-first v4 command over the existing production CLI: an explicit chapter range plus a runtime profile, while retaining a chapter mode.
- Resolve the range through the existing `v4_book_run` chapter HTML pattern; automatically create an output subdirectory under `D:\pact\gate_bench_runs` named `book_0027-0032_local|remote_<timestamp>`, with the label derived from the resolved runtime descriptor rather than an operator claim.
- Resolve models, reasoning, transport, and identity-bearing policy from the selected profile by default; allow optional invocation overrides without introducing hidden quality defaults, and fail closed on ambiguous aliases or invalid profile selections.
- Run the offline runtime-profile preflight by default before configured execution; expose `--preflight` and machine-readable JSON output as check-only modes without starting pipeline or provider/network work.
- Make `--help` complete, grouped, and truthful for range/source resolution, output naming, runtime configuration, topology, resume, audit/formatting/markup behavior, preflight, and safety/owner-run boundaries.
- Offer only explicit `--markup preserve`, which documents and requests the existing preservation/normalization policy without introducing new markup semantics.
- Add offline CLI/help contract tests and documentation examples that do not start a pipeline.
- Explicitly retain existing strict/book CLI compatibility unless a deprecated alias is documented and tested.

## Capabilities

### New Capabilities

- `v4-run-command-interface`: A discoverable, safe v4 operator command with complete help and validated forwarding to the existing strict/book entrypoint.

### Modified Capabilities

- (none)

## Impact

- Likely `pact_full_pipeline_runner_v1/v4_phase12_strict_run.py`, `v4_book_run.py`, or a new thin v4 launcher; README/operator documentation; CLI tests.
- No prompts, translation/audit/formatting/tag semantics, persistent artifact schema, runtime-profile semantics, or production pipeline run is changed by this proposal; the launcher consumes the approved runtime-profile contract without redefining it.
- The approved command shape and options are recorded in design.md; implementation remains subject to a separate owner approval.