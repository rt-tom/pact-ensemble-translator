## Why

`book --local` uses a fixed local runtime profile, while remote models are selected by alias from `configs/providers.yaml`. The owner needs `book --local` with an explicit pair `translator/reviewer` without `runtime_localN.yaml`. Each local model must carry its own server arguments and its own body sampling policy (`temperature/top_p/top_k/min_p/seed`), while output budgets (`max_output_tokens/output_budget`) are per-role and fixed (same role set for local and remote). Bare `--local` must select the default pair `gemma/qwen`.

Body sampling must not be an implicit code literal. Local reasoning remains a server-start policy (`--reasoning-budget`), not a request body option.

## What Changes

- Extend `configs/providers.yaml` with `local` provider (`kind: local_llama`). It contains:
  - `models.<alias>`: each model defines `model_key` (`gemma`/`qwen`/future), `model_path`/`model_name`/`server_args`/`reasoning_budget` and `request` (`temperature/top_p/top_k/min_p/seed` — no `max_output_tokens`). `models` already contains production `gemma`/`qwen`.
  - `role_budgets`: fixed per-role output budgets for every V4 strict/book role (same set as remote), each `output_budget` + `max_output_tokens` base. Roles are `generator`, `repair` (translator group) and `qwen_audit`, `fidelity_reviewer`, `russian_selector`, `entity_extractor` (reviewer group) plus `gemma_audit`, `russian_editor`, `formatting`, `glossary_resolver` — identical to remote, not configurable per-model.
- Add `book --local [translator_alias/reviewer_alias]` and `chapter --local [translator_alias/reviewer_alias]` analogous to `remote`. `bare --local` → `gemma/qwen` defaults. `a/b` pair requires two aliases (`translator` and `reviewer`), bare `glm` alone is fail-closed. Inside `a/b`, `glm` (bare) resolves globally uniquely, `local/glm` qualified — as with `remote`.
- Bind roles to models by fixed groups: `translator` group always uses the first alias, `reviewer` group always the second. The mapping does not vary per model.
- Sampling policy is model-owned: each `CompletionRequest` for a translator role uses `local.models[translator].request`, each reviewer role uses `local.models[reviewer].request`. Output budget is role-owned: `local.role_budgets[role]` deterministically derives final `max_output_tokens`.
- Extend local request serialization to send `temperature/top_p/top_k/min_p/seed` from the model and `max_output_tokens` from the role budget. `reasoning` remains rejected for local; `reasoning_budget` is validated against `server_args`.

## Capabilities

### New Capabilities
- `local-model-aliases`: model-centric local selection with per-model sampling and per-role budgets, pair syntax translator/reviewer.

### Modified Capabilities
- `runtime-profile-contract`: local and remote use the same fixed role groups; sampling is model-owned, budgets role-owned.

## Impact

- `configs/providers.yaml`; `pact_v4/runtime/runtime_config.py` (registry `local` with `models`+`role_budgets`, pair resolution, fail-closed); request adapter/client and `CompletionRequest` contract; strict/book CLI/preflight; all V4 producers and formatting adapter.
- Sampling changes (`temperature` etc) are intentionally **not** identity-bearing — they overwrite in place (per owner: “молча перезаписать поверх”). Cache invalidation remains only for routing/budget/alias/server_args.
- No pipeline execution, no server start, no new model beyond `gemma`/`qwen` in this change.
