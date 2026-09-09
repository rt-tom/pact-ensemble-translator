## 1. Registry and profile contract

- [ ] 1.1 Define `providers.yaml` `local` provider schema for `kind: local_llama` models: `model_key` (`gemma`/`qwen` from `SUPPORTED_LOCAL_MODEL_KEYS`), `model_path`, `model_name`, `server_args` (list-of-strings, identity-bearing), optional `generation` (`temperature`, `seed`, `max_tokens`, deferred `top_p`/`top_k`/`min_p` handling) and `reasoning_budget` (must equal `server_args --reasoning-budget` when both present); add fixture aliases for tests (no real host path). Validate global alias uniqueness includes `local` aliases; provider-qualified `local/alias` stays supported.
- [ ] 1.2 Extend `pact_v4/runtime/runtime_config.py` registry loader/validation for `local` kind, `LocalModelSpec` carriage, and alias resolution (bare + provider-qualified) with fail-closed on duplicate/unknown alias.

## 2. Generation body vs server_args wiring

- [ ] 2.1 Remove **all** hardcoded body params as source of truth: make `temperature`/`top_p`/`top_k`/`min_p`/`seed`/`max_tokens` for **every** stage (generation `0.2`, audit/repair/Editor `0.0`, formatting `0.1`, entity_extractor, glossary_resolver) come from the provider registry's per-role `generation` block, with code literals kept only as fallback when registry gives nothing; wire `GenerationParams` and all role-adapter call sites (`pact_v4/phase2/generation.py`, `pact_v4/audit/*`, `pact_v4/repair/*`, `pact_v4/phase5/formatting.py`) to the provider-supplied values. The override enters `StrictRunConfig.to_config_artifact` / `GenerationParams` / `CompletionRequest` identity per role and `strict_chapter_trial_record.json` (`generation_*` + `audit_*`/`repair_*` fields). Reject `top_p`/`top_k`/`min_p` for local in v1 with a clear error (or map to server_args in v2 after verifying binary flags).
- [ ] 2.2 Keep local reasoning via `server_args --reasoning-budget` only: local alias's `reasoning_budget` must agree with `server_args` entry, is validated by `_reasoning_budget_from_server_args` + `validate_reasoning_backend`, and is identity-bearing; `LocalOpenAIBackend`'s `request_options` rejection stays, `reasoning_effort_map` is not used for `local`. Remote path (`request_options` `reasoning` + `top_p`/`top_k`) unchanged.

## 3. CLI, preflight, and output label

- [ ] 3.1 Update `pact_full_pipeline_runner_v1/v4_run.py` + `pact_full_pipeline_runner_v1/v4_phase12_strict_run.py`: `--local` → `nargs="?" const="__DEFAULT__"` (mirror `--remote`), parse bare alias, keep mutual exclusion (`--local` vs `--remote` vs `--runtime-config`), keep `--translator/--reviewer` advanced-only (v1), delegate alias to strict (`_delegate_*`), update auto output-dir label (`local` bare vs `local_<alias>` sanitized), and help examples. Handle `chapter` mode identically.
- [ ] 3.2 Make `run_runtime_preflight` resolve the same local alias and report sanitized `server_args`/`generation`/`reasoning_budget` + identity; extend local `exe/model_paths/port` checks to the alias-resolved `LocalLlamaBackendConfig`. Ensure no network/server start.

## 4. Docs and verification

- [ ] 4.1 Update `docs/architecture/V4_BOOK_PIPELINE_INVENTORY_RU.md`, `docs/agent_operations/AGENTS_REFERENCE_RU.md`, and top-level help to document `book --local [alias]` (bare vs alias, identity invalidation → new `--out-base`), and the local vs remote `reasoning`/`temperature` paths.
- [ ] 4.2 Add tests: registry duplicate-alias rejection including `local`, bare/provider-qualified local alias resolution, local alias overrides `server_args` + `temperature/seed/max_tokens` and changes `bundle_hash`/`config_identity`, `request_options` stays empty for local alias, `--local alias` CLI parse/delegate/label, preflight alias-resolved reporting. Ensure `remote` tests and existing local no-alias tests still pass.
- [ ] 4.3 Run `openspec validate local-model-aliases --strict`, `pact-fidelity-lint`, relevant `pact_v4/runtime` + `v4_run` dispatcher tests, and `git diff --check`; no pipeline execution or model-server lifecycle.
