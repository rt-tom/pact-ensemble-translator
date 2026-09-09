## 1. Registry and fixed role map

- [ ] 1.1 Define one fixed role mapping, used by local and remote: translator (`generator/repair/formatting/gemma_audit`) and reviewer (`qwen_audit/fidelity_reviewer/russian_selector/entity_extractor/russian_editor/glossary_resolver`). Update remote `TRANSLATOR_ROLES`/`REVIEWER_ROLES`, runtime profiles, defaults, and binding tests to this map. Bind every role explicitly; remove role-to-role and `default` resolution fallbacks.
- [ ] 1.2 Replace local `role_policies` with top-level shared `role_budgets` (all ten roles, only `max_output_tokens/output_budget`) and model-owned `providers.local.models.<alias>.request` (`temperature/top_p/top_k/min_p/seed`). Validate all shapes/types/ranges and reject model request `max_output_tokens` fail-closed.
- [ ] 1.3 Retain production `gemma`/`qwen` aliases from `runtime_local.example.yaml`; validate paths/names/string args/reasoning agreement. Define immutable `ResolvedModelPair` and case-insensitive local alias lookup.

## 2. Transport and producers

- [ ] 2.1 Wire every V4 producer to `ResolvedModelPair`: translator roles take model request from left pair model, reviewer roles from right; final budget from `role_budgets[role]` through one `derive_max_output_tokens` helper. Each producer resolves only its exact role and fails closed when it is absent. Remove sampling/budget code literals and glossary descriptor introspection.
- [ ] 2.2 Serialize local `temperature/top_p/top_k/min_p/seed` from selected model and `max_output_tokens` from role budget; local `reasoning` rejected, server args only. Apply to generation/repair/selector/Gemma audit/formatting/Qwen audit/fidelity/re-gate/entity/Russian editor/glossary/B3.
- [ ] 2.3 Thread resolved pair through strict config, B3, role adapters, formatting and glossary; code must fail closed if normal simple local execution has no pair.

## 3. CLI, cache, preflight

- [ ] 3.1 Implement `book|chapter --local` → `gemma/qwen`, `--local a/b` → left translator/right reviewer, single `a` fail-closed. Resolve components only under local registry; forward pair and providers config through delegation; labels `local` and `local_<a>_<b>`.
- [ ] 3.2 Preflight resolves/validates same pair plus all shared budgets, paths/server args/reasoning agreement, reports sanitized pair/request/budget data, no network/server start.
- [ ] 3.3 Routing/server args/budgets remain run identity-bearing. Sampling is excluded from run/output-dir identity but included in each request cache key/provenance: changed sampling must regenerate and overwrite in same directory, never replay stale sample. Update schemas and stale-cache tests accordingly.

## 4. Tests, docs, verification

- [ ] 4.1 Tests: bare `gemma/qwen`, explicit `glm/glimmer`, single-alias rejection, remote-under-local rejection, malformed model request/budget/reasoning input, remote fixed-group bindings, pair preflight.
- [ ] 4.2 Payload-capture: model sampling + role budget for both group positions, all allowed fields, local reasoning rejection/no silent drop.
- [ ] 4.3 Producer/cache matrix: every role receives correct group model and role budget; changing sampling regenerates only affected group in same output directory; budget/routing change has correct stale-resume behavior.
- [ ] 4.4 Docs/help: pair syntax, fixed shared groups, model sampling vs role budgets, overwrite semantics, `runtime_local.example.yaml` reference-only status.
- [ ] 4.5 Run `openspec validate local-model-aliases --strict`, focused runtime/adapter/strict/book tests, `git diff --check`; no server/pipeline execution.
