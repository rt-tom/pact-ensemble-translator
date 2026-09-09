## Purpose

Model-centric local selection with per-model sampling and per-role output budgets, identical role set for local and remote.

## ADDED Requirements

### Requirement: Model-owned sampling, role-owned budgets

The system SHALL store `temperature/top_p/top_k/min_p/seed` on `local.models.<alias>.request` and `max_output_tokens/output_budget` on `local.role_budgets.<role>` for the fixed role set identical to remote. Unknown request field, wrong type/range, or `max_output_tokens` inside model `request` SHALL fail-closed before preflight.

#### Scenario: Model sampling used by its group

- **WHEN** `local.models.gemma.request.temperature` is `0.2` and translator group is `gemma`
- **THEN** every translator role (`generator/repair/russian_selector/gemma_audit/formatting`) SHALL send `temperature 0.2` (and other model fields) from that model, with `max_output_tokens` from `role_budgets[role]`.

#### Scenario: Budget is role-owned

- **WHEN** `role_budgets.generator.max_output_tokens` is `70000`
- **THEN** generator SHALL use `70000` regardless of which translator model (`gemma` or `glm`) is selected.

### Requirement: Local alias pair and bare defaults

The system SHALL accept `book|chapter --local` (bare → `gemma/qwen`) and `book|chapter --local a/b` (pair). Single `book --local a` SHALL fail-closed with “pair required”. Each `a`/`b` SHALL resolve to a `local` model; remote alias under `--local` SHALL fail-closed.

#### Scenario: Bare uses production pair

- **WHEN** `book --local` is invoked
- **THEN** translator SHALL be `local/gemma`, reviewer `local/qwen` with their production `model_path/server_args`.

#### Scenario: Pair overrides both groups

- **WHEN** `book --local glm/glimmer` is invoked
- **THEN** translator roles SHALL use `glm`, reviewer roles `glimmer`; each side's `server_args/request` from its model.

#### Scenario: Bare alias inside pair is globally unique

- **WHEN** alias `glm` is globally unique
- **THEN** `glm` and `local/glm` SHALL resolve identically; duplicate normalized alias across providers SHALL fail at load.

### Requirement: Local request transport and reasoning

Local transport SHALL serialize `temperature/top_p/top_k/min_p/seed` from the selected model and `max_output_tokens` from `role_budgets[role]`. `reasoning` SHALL remain rejected for local; `reasoning_budget` SHALL equal `server_args --reasoning-budget`.

#### Scenario: Sampling from model

- **WHEN** `local.models.gemma.request.seed` is `7`
- **THEN** generator `CompletionRequest` SHALL carry `seed 7`.

#### Scenario: Local reasoning remains server-side

- **WHEN** `local.models.qwen.reasoning_budget` is `8192`
- **THEN** `server_args` SHALL contain `--reasoning-budget 8192` and no `reasoning` request option SHALL be sent.

### Requirement: Fixed role groups and preflight

Translator group SHALL be `generator, repair, russian_selector, gemma_audit, formatting`; reviewer group SHALL be `qwen_audit, fidelity_reviewer, entity_extractor, russian_editor, glossary_resolver`. Preflight SHALL resolve the same pair/budgets, validate paths/server_args/reasoning agreement, and report sanitized pair + per-role budgets without network/server start. Sampling changes (`request`) SHALL NOT affect cache identity (overwrite in place).

#### Scenario: Sampling change overwrites

- **WHEN** `local.models.gemma.request.temperature` changes from `0.2` to `0.7`
- **THEN** next run SHALL reuse the same output directory/cache (overwrite), not require a new `--out-base`.
