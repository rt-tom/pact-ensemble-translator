## Context

The strict chapter and book-run scripts expose different, long argparse surfaces. The project has removed stale v3 run instructions from README, but an operator still needs to know which v4 entrypoint applies and which arguments materially control a run. The existing scripts are the behavioral source of truth.

## Goals / Non-Goals

**Goals:**
- Provide one small dispatcher with `book` as the primary subcommand and retained `chapter` subcommand.
- Make book invocation short and explicit: a chapter range plus the approved runtime profile; reuse the existing source-pattern and CLI entrypoints wherever possible, so forwarding preserves behavior while profile defaults and optional model/reasoning overrides remain visible in resolved identity.
- Make top-level and mode help usable offline and explicit about RT owner-run policy.
- Test help and forwarding without model/network/pipeline execution.

**Non-Goals:**
- No change to strict/book argument semantics, defaults, tag/markup behavior, audit/formatting policy, model routing, output schemas, resume identity, or lifecycle.
- No replacement, removal, or execution of v3/v31 launchers.
- No production run, managed-server startup, or remote-provider call.

## Decisions

- **Thin Python dispatcher, not a new execution engine.** Add one v4-facing command with primary `book` and retained `chapter`; dispatch to the existing `main(argv)` paths. Alternative: duplicate all argparse options in a wrapper — rejected because it risks default/help drift.
- **Book short form is range + runtime profile.** Book mode accepts `--chapters 27-32` and a runtime profile path. The profile supplies source-pattern/runtime information and all default models, reasoning, transport, and identity-bearing policy; optional invocation model/reasoning overrides are applied only when explicitly supplied. The dispatcher validates and expands a closed numeric range, including zero-padded output naming, before delegation. Alternative: ask operators to repeat full source paths or silently inject launcher defaults — rejected as error-prone and non-reproducible.
- **Automatic output naming is deterministic and collision-safe.** The default root is `D:\\pact\\gate_bench_runs`; each run gets `book_0027-0032_local_<timestamp>` or `book_0027-0032_remote_<timestamp>`. The local/remote label is derived from the resolved runtime descriptor after profile defaults and explicit overrides are applied, not a separate user claim; an unknown descriptor is a validation error. Alternative: operator-invented output names — rejected as inconsistent and collision-prone.
- **Top-level help is curated; mode help remains source-of-truth parser help.** The dispatcher explains range/source resolution, auto-output, profile-resolved defaults, optional overrides, automatic offline preflight, `--preflight`/JSON check-only modes, safety, and argument groups; it hands remaining args to the existing parser for mode-specific detail. This ensures every existing option remains available and prevents duplicate option definitions.
- **Markup option is explicit but preserve-only.** `--markup preserve` is the only accepted value and documents/validates the current preservation/normalization policy; it introduces no tag transformation or new default. Alternatives `strip` and policy files are rejected because they would alter output semantics and require a separate formatting design.
- **Compatibility is testable forwarding.** Tests will patch mode entrypoints and assert exact argument forwarding and exit propagation; profile-resolution tests assert omitted values use profile defaults and explicit model/reasoning overrides are identity-bearing; preflight/help tests assert no pipeline, model lifecycle, provider/network, source, or artifact side effects. Alias selection remains case-insensitive and fail-closed according to the approved registry contract.

## Risks / Trade-offs

- [Dispatcher masks an argparse error or changes exit codes] → Preserve delegated argv and test success/error propagation.
- [Curated help becomes stale] → Keep only stable mode/safety text in dispatcher; keep option-level help owned by existing parsers and add contract tests for required topics.
- [Runtime descriptor cannot identify local/remote] → Fail pre-start with a clear error rather than mislabeling an output directory; resolve the approved descriptor fields before any output directory is created.
- [Launcher defaults diverge from profile identity] → Treat the profile as the source of truth, apply only explicit overrides, and test the resolved descriptor/identity before delegation.
- [Operator treats wrapper as permission to run production] → State owner-started RT boundary in top-level and documented help.

## Migration Plan

Additive only: existing strict/book commands remain supported. Rollback removes the wrapper and docs/tests without changing any run artifacts. Deployment is only a normal `git pull --ff-only` on RT; no pipeline starts.

## Open Questions

None. The exact displayed command name follows repository CLI conventions and will be verified against the available Python execution path during implementation.