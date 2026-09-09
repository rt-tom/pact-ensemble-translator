## 1. Registry contract

- [ ] 1.1 Replace `role_policies` with `models.<alias>.request` (temperature/top_p/top_k/min_p/seed) and `role_budgets` (max_output_tokens/output_budget) for the fixed role set identical to remote. Enforce `model_key` enum, non-empty path/name, `server_args` list-of-strings, `reasoning_budget == --reasoning-budget`, fail-closed on unknown field/type/range/`max_output_tokens` in model request.
- [ ] 1.2 Keep `local` provider `models` containing production `gemma`/`qwen` with production paths/server_args; validate global alias uniqueness and qualified `local/alias` inside `a/b`.
- [ ] 1.3 Define fixed `TRANSLATOR_ROLES`/`REVIEWER_ROLES` identical to remote and expose `ResolvedLocalPair` (translator_model, reviewer_model, role_budgets).

## 2. Transport and wiring

- [ ] 2.1 Extend `CompletionRequest/ApiClient/LocalOpenAIBackend` to serialize sampling from model and budget from role (`temperature/top_p/top_k/min_p/seed` + `max_output_tokens`), `min_p` validated, `reasoning` rejected for local.
- [ ] 2.2 Wire all V4 producers via fixed groups: translator group uses `translator_model.request` + `role_budgets[role]`, reviewer group uses `reviewer_model.request` + `role_budgets[role]`. Remove descriptor introspection in glossary resolver. Budgets derived via single `derive_max_output_tokens(role, item_count)`.
- [ ] 2.3 Ensure `StrictRunConfig`/`B3` carry `ResolvedLocalPair`; no per-role sampling literal remains.

## 3. CLI, preflight, identity

- [ ] 3.1 CLI `--local` → `nargs="?"` pair `a/b` (bare `a` fail-closed “pair required”), bare `--local` → `gemma/qwen`, inside `a/b` bare `glm` or qualified `local/glm` both accepted as with `remote`. Mutual exclusion with `--remote/--runtime-config/--translator/--reviewer`, delegation forwards `a/b` + `providers-config`, output label `local` vs `local_<a>_<b>`.
- [ ] 3.2 Preflight resolves same pair/budgets, validates paths/server_args/reasoning agreement, reports sanitized pair + budgets without network/server start.
- [ ] 3.3 Identity: only routing (`model_path/server_args/alias`) + role budgets are identity-bearing; model `request` (temperature etc) is **not** identity-bearing (overwrite). Update `BackendDescriptor.public_record`/`StrictRunConfig.to_config_artifact`/trial provenance accordingly, bump cache schemas only for budget/routing changes.

## 4. Tests, docs, verification

- [ ] 4.1 Registry tests: bare → `gemma/qwen`, pair `glm/glimmer` and `local/glm/local/glimmer`, single `a` fail-closed, remote alias under `--local` fail-closed, malformed request/budget fields fail-closed, global alias collision.
- [ ] 4.2 Transport payload-capture tests for sampling from model + budget from role, both transports, no silent drop, local `reasoning` rejected.
- [ ] 4.3 One producer test per fixed group (translator vs reviewer) proving sampling from correct model and budget from role, plus overwrite semantics for sampling change (reuse, not new dir).
- [ ] 4.4 Docs: `V4_BOOK_PIPELINE_INVENTORY_RU.md`, `AGENTS_REFERENCE_RU.md`, help, `providers.yaml` comments — document pair syntax, fixed groups (translator/reviewer), model-owned sampling vs role-owned budgets, overwrite identity, `runtime_local.example.yaml` as doc-only.
- [ ] 4.5 `openspec validate local-model-aliases --strict`, relevant runtime/adapter/strict/book tests, `git diff --check`; no server/pipeline execution.
