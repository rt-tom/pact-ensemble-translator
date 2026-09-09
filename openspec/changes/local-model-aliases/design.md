## Context

Current remote CLI overrides bind only a subset of roles (`TRANSLATOR_ROLES = generator, repair`; `REVIEWER_ROLES = qwen_audit, fidelity_reviewer, russian_selector, entity_extractor`) and use fallback chains for the remaining roles. Current local aliases are role-policy-centric. Neither matches the owner's required model-centric design: two models in every run, fixed groups identical across transports, explicit binding for every role with no fallback, sampling on models, budgets on roles.

## Goals / Non-Goals

**Goals**
- Bare `--local` preserves production `gemma` translator + `qwen` reviewer.
- `--local translator/reviewer` switches both models atomically; one alias is invalid.
- Fixed groups are the same for local and remote, and no model config can alter them.
- Sampling is on model; budgets are shared role policy; no model-call setting is a code default.
- Changed sampling regenerates the affected request in the same output directory; it does not demand a new directory or replay a stale cache.

**Non-Goals**
- Add no new model in this implementation (schema permits later `glm`/`glimmer`).
- Do not delete `runtime_local.example.yaml`; it remains a documentation/reference profile, not the simple local runtime source.
- Do not alter prompts, retries, parsers, lifecycle, or start a server/pipeline.

## Decisions

### 1. Shared fixed role map

This is the sole role map for local **and remote**, including alias overrides and runtime profile bindings:

```text
translator = generator, repair, formatting, gemma_audit
reviewer   = qwen_audit, fidelity_reviewer, russian_selector,
             entity_extractor, russian_editor, glossary_resolver
```

Implementation changes existing `TRANSLATOR_ROLES`/`REVIEWER_ROLES` and adds explicit bindings for all ten roles in local and remote profiles. Every producer resolves its exact role only: no role-to-role or `default` fallback is permitted. No `models.<alias>` field controls role membership. A formatter/auditor model change is therefore always selected by its group position, never by an ad hoc role setting.

### 2. Registry shape

```yaml
role_budgets:                       # shared by local and remote
  generator: {max_output_tokens: 70000}
  repair: {max_output_tokens: 16384, output_budget: {mode: floor_plus_per_item, floor_tokens: 16384, per_item_tokens: 128, ceiling: 24576}}
  # all ten fixed roles required
providers:
  local:
    kind: local_llama
    models:
      gemma:
        model_key: gemma
        model_path: C:/...
        model_name: gemma.gguf
        server_args: [..., --reasoning-budget, "2048", ...]
        reasoning_budget: 2048
        request: {temperature: 0.2, seed: 7}
      qwen: {model_key: qwen, ...}
```

`models.<alias>.request` permits only `temperature`, `top_p`, `top_k`, `min_p`, `seed`; it forbids `max_output_tokens`. It is validated type/range fail-closed. Models require a non-empty path/name, string-only `server_args`, and exact `reasoning_budget` agreement with `--reasoning-budget`.

Top-level `role_budgets` requires all ten role keys, only output-budget fields, and is the sole source of output limit calculations for both transports. This avoids duplicating budgets per provider/model while preserving provider-file ownership.

### 3. CLI grammar

- `--local` → default pair `local/gemma` + `local/qwen`.
- `--local a/b` → local model `a` for translator and local model `b` for reviewer.
- `--local a` → fail closed (“translator/reviewer pair required”).
- Each `a` and `b` is looked up only in `providers.local.models`, case-insensitively. Provider-qualified components are deliberately not supported because `/` is the pair delimiter; use aliases unique inside `local`.
- Same positional semantics as remote (`left=translator`, `right=reviewer`); local/remote/runtime-config/translator/reviewer options remain mutually exclusive.

### 4. Wiring

`ResolvedModelPair(translator_model, reviewer_model, role_budgets)` is immutable. Every producer obtains:

- request sampling from translator model for translator role, reviewer model for reviewer role;
- final `max_output_tokens` only from `derive_max_output_tokens(role_budgets[role], item_count/span_count)`;
- local reasoning only from the selected model's `server_args`.

All ten producers (generation, repair, formatting, Gemma audit, Qwen audit, fidelity/re-gate, selector, entity extractor, Russian editor, glossary resolver) receive that resolved pair and fail closed if their exact role is not bound. `ApiClient` only serializes sampling explicitly in that model request and limit explicitly derived from role budget.

### 5. Cache and provenance

Routing (`translator/reviewer aliases`, path/name/server args) and shared role budgets are run identity-bearing. Sampling is excluded from run/output-directory identity, so changing `temperature` does not force a new out-base.

Sampling is included in each request-level cache key and provenance. A sampling change yields a cache miss, regenerates the affected output, and overwrites it in the same directory. It must not reuse an older sample. Trial records report both run routing/budget identity and per-call sampling fingerprint.

### 6. Preflight

Preflight resolves precisely the same default/pair, checks both models' path/server args/reasoning agreement plus budget/request shapes, and prints sanitized pair, model requests, and role budgets without network/server side effects.

## Migration / Risks

`--local a` becomes deliberately invalid; bare `--local` stays compatible. Existing remote routing changes to the owner-approved fixed groups, so focused remote binding regression tests are required. Sampling-cache behavior is intentionally revised from “new output directory” to “regenerate/overwrite in place”.
