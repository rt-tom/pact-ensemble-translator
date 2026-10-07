## Risk and constraints

**Risk: High.** This changes production model startup flags and the Qwen38 B3 capability gate. It is offline-only: never start llama-server or a pipeline. RT acceptance of the `all` values is not asserted by static tests.

## Scope and exact profile edits

Runtime profiles to update:

1. `configs/providers.yaml`
   - `local.models.gemma`: change main-model `-ngl 99` to `-ngl all` (no explicit MTP selector; do not add a draft-layer flag).
   - `local.models.qwen38`: change main-model `-ngl 99` to `-ngl all` and add exactly one `--spec-draft-ngl all` to its embedded-MTP arguments.
   - `local.models.gemma31` already has `--spec-draft-ngl all`; leave its other arguments alone.
   - Ordinary `local.models.qwen` has no active `--spec-type draft-mtp`; leave it unchanged even though its model filename contains `MTP`.
2. `configs/runtime_local.example.yaml`
   - `server_args.gemma`: change `-ngl 99` to `-ngl all`; it has no active MTP selector, so do not add `--spec-draft-ngl`.
3. `configs/runtime_composite.example.yaml`
   - `backends.local.server_args.gemma`: change `-ngl 99` to `-ngl all` and add exactly one `--spec-draft-ngl all` because this example explicitly enables `draft-mtp`.
4. `pact_full_pipeline_runner_v1/v4_phase12_strict_run.py`
   - `GEMMA_SERVER_ARGS` is an executable default Gemma launch profile without MTP: change only `-ngl 99` to `-ngl all`; do not add a draft-layer flag.
5. `pact_full_pipeline_runner_v1/v4_model_lifecycle_bench.py`
   - `GEMMA_COMMON_ARGS` is an executable Gemma benchmark profile with MTP: change `-ngl 99` to `-ngl all` and add exactly one `--spec-draft-ngl all`.

Do not change historical OpenSpec design examples, unrelated `-ngl 99` test fixtures, model files, aliases, sampling, budgets, context, output-token limits, routing, or lifecycle policy. Update tests that assert the actual `GEMMA_SERVER_ARGS` profile; test the benchmark's declared arguments without starting the benchmark or server.

## Qwen38 B3 boundary

Update only `_validate_b3_qwen38_reviewer` in `pact_full_pipeline_runner_v1/v4_phase12_strict_run.py` to:

- Require exactly one `-ngl` with the exact value `all`.
- Require exactly one `--spec-draft-ngl` with the exact value `all` (replacing the current prohibition).
- Keep all current exact model identity, embedded-MTP (`--spec-type draft-mtp`, `--spec-draft-n-max 2`, `--spec-draft-p-min 0.5`), no-external-draft (`-md`), no-draft-device, no duplicated lifecycle device, reasoning, budget, context, and error-path checks unchanged.

Fail closed for absent, duplicated, valueless, malformed, or noncanonical values for either offload flag. Do not relax any other B3 check or skip audit/filter behavior.

## Tests

- Update the exact registry expectation for Qwen38 and assert one `-ngl all` plus one `--spec-draft-ngl all`; assert unchanged numeric budgets and other profile fields.
- Add focused checks for all three runtime config files and the two executable Gemma profiles: no in-scope `-ngl 99` remains; each explicitly MTP-enabled profile has exactly one draft offload value `all`; profiles without an active MTP selector do not gain a draft flag. Preserve the existing Gemma31 expectation.
- Update `test_gemma_server_args_match_plan_34` for the default Gemma profile and add an assertion for the lifecycle benchmark's MTP argument profile; these checks must not launch a server or benchmark.
- Extend Qwen38 B3 positives and negatives: the approved profile with both flags set to `all` passes; missing, duplicate, valueless, or wrong-valued `-ngl` and `--spec-draft-ngl` each fail; existing identity/MTP/device/budget/context negatives remain.
- Run the narrowest profile and B3 tests first, then strict validation and the relevant suite. No model server or pipeline run.

## Supersession

The new `local-model-gpu-offload` requirement is additive and narrowly overrides prior Qwen38 prohibitions of `--spec-draft-ngl` and statements that the Qwen38 B3 gate remains unchanged. The earlier OpenSpec files stay unmodified; every other requirement in them remains effective.
