## Why

The owner needs one model-centric registry for local execution: each model owns its llama-server arguments and body sampling (`temperature/top_p/top_k/min_p/seed`); fixed roles own only output budgets. Every run always has two models: a translator and a reviewer. Bare `--local` keeps the production pair `gemma/qwen`; `--local glm/glimmer` selects a different translator/reviewer pair.

The existing remote mapping is not yet the owner's fixed translator/reviewer mapping, so this change makes the role groups identical for both transports. Sampling values must not be code literals or force a new output directory. If sampling changes, the affected model call must be regenerated and written over the old result in the same run directory; it must never replay a cache created with different sampling.

## What Changes

- `configs/providers.yaml` becomes model-centric:
  - top-level `role_budgets` is one shared fixed budget map for all local and remote roles; it owns `max_output_tokens` and `output_budget` only;
  - `providers.local.models.<alias>` owns `model_key`, `model_path`, `model_name`, `server_args`, `reasoning_budget`, and `request` sampling only. Production `gemma`/`qwen` entries are ported unchanged from `runtime_local.example.yaml`.
- Define one fixed role split and use it for **both** local and remote:
  - translator: `generator`, `repair`, `russian_selector`, `gemma_audit`, `formatting`;
  - reviewer: `qwen_audit`, `fidelity_reviewer`, `entity_extractor`, `russian_editor`, `glossary_resolver`.
  No model entry may change this map.
- Local CLI: bare `book|chapter --local` selects `gemma/qwen`; explicit selection requires exactly `--local translator/reviewer`, for example `--local glm/glimmer`. A single alias fails closed. Pair components are local aliases (not cross-provider/global lookups); remote aliases fail closed. This is the same two-position CLI semantics as remote, without ambiguous provider-qualified slash syntax.
- A translator model supplies sampling to every translator role; a reviewer model supplies sampling to every reviewer role. The shared role budget supplies the final output limit. Local reasoning remains server-start-only (`--reasoning-budget`).
- Sampling is excluded from run/output-directory identity, so no new `--out-base` is needed. It remains in each call/cache request key and provenance, so a changed temperature causes regeneration/overwrite rather than stale cache replay. Routing, server args and role budgets remain run identity-bearing.

## Capabilities

### New Capabilities
- `local-model-aliases`: two-model local pair selection with model-owned sampling and shared fixed role budgets.

### Modified Capabilities
- `runtime-profile-contract`: remote and local share one fixed translator/reviewer role map and shared role budgets.

## Impact

- `configs/providers.yaml`, `runtime_config.py`, local/remote role binding, local request serialization, strict/book CLI/preflight, all V4 producers, cache request identities and provenance.
- High risk: role routing and cache behavior change. No pipeline/server action or new model beyond current `gemma/qwen` is included.
