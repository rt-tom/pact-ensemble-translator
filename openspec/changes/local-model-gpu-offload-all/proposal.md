## Why

The owner requests consistent full GPU-layer offload for local model profiles: replace `-ngl 99` with `-ngl all`, and ensure every profile that explicitly enables MTP also sets `--spec-draft-ngl all`.

The current Qwen38 B3 contract explicitly rejects `--spec-draft-ngl`, so applying the requested Qwen38 MTP setting also requires a narrow fail-closed B3 validator and test update. This is a runtime/model-policy change, not a mechanical config sweep.

## What Changes

- Across current runtime config files and executable local-model launch profiles, change every model's `-ngl 99` to `-ngl all`.
- For each profile that explicitly enables MTP with `--spec-type draft-mtp`, require exactly one `--spec-draft-ngl all`; add it where absent. Profiles already using `all` remain unchanged.
- Update the Qwen38 B3 gate to require exactly one `-ngl all` and one `--spec-draft-ngl all`, while preserving its exact model identity, embedded-MTP settings, context/budget checks, and all other fail-closed constraints.
- Update focused config, executable-profile, and B3 tests, including negative cases for missing, duplicate, malformed, or non-`all` offload values.

The intended targets are `configs/providers.yaml`, `configs/runtime_local.example.yaml`, `configs/runtime_composite.example.yaml`, the default `GEMMA_SERVER_ARGS` profile in `pact_full_pipeline_runner_v1/v4_phase12_strict_run.py`, and the MTP `GEMMA_COMMON_ARGS` profile in `pact_full_pipeline_runner_v1/v4_model_lifecycle_bench.py`. Do not rewrite historical OpenSpec examples, unrelated test fixtures, or launch profiles whose main-layer value is not `99`; update tests that assert an actual executable profile.

## Supersession and invariants

This standalone change supersedes only prior Qwen38 requirements that prohibit `--spec-draft-ngl` or require the Qwen38 B3 gate to remain unchanged. All other Qwen38/Gemma profile requirements remain in force: identity, reasoning effort and numeric budgets, context, sampling, MTP selector and draft parameters, routing, lifecycle-injected device handling, and the absence of external-draft/device flags for Qwen38. The ordinary `qwen` profile remains without an active MTP selector and is not changed.

No role budgets, output-token budgets, model paths, routing, lifecycle policy, B3 audit execution, or hard audit filters change. Do not launch a model server or pipeline during development. RT llama-sycl-edge acceptance of `-ngl all` and `--spec-draft-ngl all` will remain unverified unless separately authorized and tested by the owner.

## Capabilities

### Added Capabilities
- `local-model-gpu-offload`: defines full GPU-layer offload for local model profiles and the corresponding Qwen38 B3 contract.

## Approval boundary

The owner has requested the behavior. Because it changes production model startup arguments and the Qwen38 B3 validation contract, implementation begins only after the owner approves this standalone OpenSpec proposal. Work must remain in its isolated branch/worktree, pass independent review, and remain offline-only; merge, deployment, model-server startup, and pipeline execution are separate owner decisions.
