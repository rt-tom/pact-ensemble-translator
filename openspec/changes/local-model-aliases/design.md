## Context

Remote aliases are resolved from `configs/providers.yaml`; local runs used static `LocalLlamaBackendConfig`. Sampling settings were scattered and role budgets were literals. The new requirement is model-centric: sampling (`temperature/top_p/top_k/min_p/seed`) belongs to the model, output budget (`max_output_tokens/output_budget`) belongs to the role. Role groups are fixed and identical for local and remote.

## Goals / Non-Goals

**Goals**
- `book|chapter --local` (bare) uses `gemma` (translator) + `qwen` (reviewer) with production paths/server_args, preserving current behavior.
- `book|chapter --local a/b` requires a pair, each `a`/`b` resolves to a `local` model; `a` supplies all translator roles, `b` all reviewer roles.
- Sampling is model-owned, budgets role-owned, both validated fail-closed before preflight.
- Local transport serializes model sampling + role budget; `reasoning` remains server-start only.
- Sampling changes overwrite in place (not identity-bearing), per owner; routing/budget/alias/server_args remain identity-bearing.

**Non-Goals**
- No new local model beyond `gemma`/`qwen`; no `runtime_localN.yaml`.
- No per-role sampling knobs, no dynamic budget literals in call sites.
- No change to prompt/retry/parsing/lifecycle; `runtime_local.example.yaml` kept as doc example only, pipeline does not read it.

## Decisions

### 1. Registry shape (identical role set for local and remote)

```yaml
providers:
  local:
    kind: local_llama
    models:
      gemma: {model_key: gemma, model_path: C:/..., server_args: [...], reasoning_budget: 2048, request: {temperature: 0.2, seed: 7}}
      qwen:  {model_key: qwen,  model_path: C:/..., server_args: [...], reasoning_budget: 8192, request: {temperature: 0.0}}
      # future: glm: {model_key: gemma, ...}  # model_key may differ from alias
    role_budgets:
      generator: {max_output_tokens: 70000}
      repair: {max_output_tokens: 16384, output_budget: {mode: floor_plus_per_item, floor_tokens: 16384, per_item_tokens: 128, ceiling: 24576}}
      # ... fixed set identical to remote role set
```

- `models.<alias>` allowed fields: `model_key`, `model_path`, `model_name`, `server_args` (list-of-strings), `reasoning_budget` (must equal `--reasoning-budget` in `server_args`), `request` (only `temperature/top_p/top_k/min_p/seed`, validated type/range; `max_output_tokens` forbidden). Unknown field → fail-closed.
- `role_budgets` required for every fixed role, same keys for local and remote; each entry is `max_output_tokens` + optional `output_budget` (`mode/base/floor/per_item/per_span/ceiling`). Unknown role/field → fail-closed.
- Global bare-alias uniqueness as with remote; `local/glm` qualified supported; bare `glm` inside `a/b` resolves via global index.

### 2. Fixed role groups (translator / reviewer)

- `translator_roles = (generator, repair, russian_selector, gemma_audit, formatting)` — always bound to the first alias of the pair (and to `gemma` for bare).
- `reviewer_roles = (qwen_audit, fidelity_reviewer, entity_extractor, russian_editor, glossary_resolver)` — always bound to the second alias (and to `qwen` for bare).
- The set never varies per model; adding a model never adds a role.

### 3. CLI pair syntax

- `--local` → `nargs="?"` with bare sentinel, but validation requires: `None` → bare `gemma/qwen`; `a` (single slash-less) → fail-closed “pair required”; `a/b` → two aliases, each resolved fail-closed as local model (reject remote alias). `local/a` qualified inside pair accepted.
- `--local` mutually exclusive with `--remote/--runtime-config/--translator/--reviewer`. Delegation forwards `a/b` unchanged via `--local a/b --providers-config`.

### 4. Wiring

- `ResolvedLocalPair = (translator_model: LocalModelAlias, reviewer_model: LocalModelAlias, role_budgets: Mapping[role, OutputBudgetPolicy])` with `derive_max_output_tokens(role, item_count)` using `role_budgets[role]`.
- Each producer receives `(model_request, role_budget)`: `CompletionRequest.temperature/top_p/...` from `model.request`, `max_output_tokens` from `role_budgets[role].derive(...)`. No fallback literal.
- `ApiClient/LocalOpenAIBackend` serializes `temperature/top_p/top_k/min_p/seed` from model and `max_output_tokens` from role; `reasoning` rejected for local.

### 5. Identity

- Sampling (`request`) changes are **not** identity-bearing (overwrite, per owner). `BackendDescriptor`/`StrictRunConfig.to_config_artifact` include only routing (`model_path/server_args`/`alias`), role budgets, and `role_budgets` hash. Cache resume reuses previous outputs after a sampling change — intentional.
- `run_runtime_preflight` validates alias/pair, server_args/reasoning agreement, and transport field support, and reports sanitized pair + budgets without network/server start.

## Risks / Migration

Pair requirement is breaking for single-alias callers (intentional). Bare `--local` preserves old behavior. `role_budgets` identical for local/remote ensures a budget change invalidates the correct role cache (still identity-bearing), while sampling changes do not.
