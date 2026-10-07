## Approval gate

- [x] 0.1 Owner requests `-ngl 99` → `-ngl all` and `--spec-draft-ngl all` for explicit MTP profiles.
- [x] 0.2 Owner approves this standalone OpenSpec proposal before implementation, including the narrow Qwen38 B3 validator change.

## Implementation (after approval only)

- [x] 1.1 Update only the specified runtime config profiles in `providers.yaml`, `runtime_local.example.yaml`, and `runtime_composite.example.yaml`; preserve profiles already using `all` and models without active MTP.
- [x] 1.2 Update the Qwen38 B3 gate to require exactly one main/draft `all` offload flag, preserving all other fail-closed checks.
- [x] 1.3 Update focused config/profile/B3 tests, including negative cases for missing, duplicate, malformed, and non-`all` values.
- [x] 1.4 Change the executable default `GEMMA_SERVER_ARGS` in `v4_phase12_strict_run.py` from `-ngl 99` to `-ngl all`; update its exact-profile test.
- [x] 1.5 Change the MTP lifecycle benchmark `GEMMA_COMMON_ARGS` from `-ngl 99` to `-ngl all` and add `--spec-draft-ngl all`; add a static argument assertion without running the benchmark/server.

## Verification (offline only)

- [x] 2.1 Run narrow profile and B3 tests first, then applicable focused suite, `openspec validate local-model-gpu-offload-all --strict`, `git diff --check`, and independent `pact-rev` review.
- [x] 2.2 Record that RT llama-sycl-edge acceptance of `-ngl all` and `--spec-draft-ngl all` was not tested; make no runtime-compatibility claim.
- [x] 2.3 Confirm no model server or pipeline was started and no unrelated model settings, budgets, routing, or lifecycle policy changed.

## Rollout boundary

- [ ] 3.1 Merge/deploy only after separate owner authorization and successful approval, implementation, tests, and review. Do not start a model server or pipeline.
