## Purpose

Two-model local selection with model-owned sampling, shared role-owned budgets, and the same fixed role groups as remote.

## ADDED Requirements

### Requirement: Fixed shared translator/reviewer groups

The system SHALL use exactly these groups for both local and remote runtime selection:

- translator: `generator`, `repair`, `russian_selector`, `gemma_audit`, `formatting`;
- reviewer: `qwen_audit`, `fidelity_reviewer`, `entity_extractor`, `russian_editor`, `glossary_resolver`.

No provider model entry SHALL alter group membership.

#### Scenario: Remote and local use identical groups

- **WHEN** either `--remote translator/reviewer` or `--local translator/reviewer` is resolved
- **THEN** the left model SHALL bind every translator role and the right model SHALL bind every reviewer role.

### Requirement: Model-owned sampling and shared role budgets

The registry SHALL store only `temperature/top_p/top_k/min_p/seed` in `providers.local.models.<alias>.request`. It SHALL store `max_output_tokens` and dynamic budget inputs only in a top-level shared `role_budgets.<role>` map containing all ten fixed roles. Unknown request/budget key, wrong type/range, missing role, or `max_output_tokens` inside model request SHALL fail-closed before preflight.

#### Scenario: Sampling belongs to selected model

- **WHEN** translator model `glm` has `request.temperature: 0.7`
- **THEN** every translator request SHALL send `0.7`; reviewer requests SHALL use only reviewer model sampling.

#### Scenario: Budget belongs to role

- **WHEN** `role_budgets.generator.max_output_tokens` is `70000`
- **THEN** generator SHALL use `70000` for `gemma`, `glm`, or any future translator model.

### Requirement: Local pair selection

The system SHALL accept bare `book|chapter --local` as production pair `gemma/qwen` and explicit `--local translator/reviewer` where both components are local aliases. A single component SHALL fail-closed with a pair-required error.

#### Scenario: Bare local

- **WHEN** `book --local` is invoked
- **THEN** `gemma` SHALL serve translator roles and `qwen` reviewer roles with their registered production path/server args.

#### Scenario: Explicit pair

- **WHEN** `book --local glm/glimmer` is invoked
- **THEN** `glm` SHALL replace the whole translator group and `glimmer` the whole reviewer group atomically.

#### Scenario: Alias is local-only

- **WHEN** either pair component is unknown under `providers.local.models` or names a remote model
- **THEN** parsing SHALL fail-closed before any preflight/server action.

### Requirement: Transport, overwrite, and preflight

Local transport SHALL serialize sampling from the selected model and final `max_output_tokens` from its role budget. Local reasoning SHALL remain only `server_args --reasoning-budget`; request `reasoning` is rejected. Sampling changes SHALL not require new output directory/run identity, but SHALL change the request cache key/provenance and regenerate/overwrite in place rather than replay stale output.

#### Scenario: Temperature change regenerates in place

- **WHEN** `gemma.request.temperature` changes from `0.2` to `0.7` and the same output directory is reused
- **THEN** translator cache lookup SHALL miss, a new request at `0.7` SHALL replace its old output in that directory, and reviewer results may be reused if their request identities are unchanged.

#### Scenario: Side-effect-free preflight

- **WHEN** `book --local glm/glimmer --preflight` is invoked
- **THEN** it SHALL validate both local aliases, their paths/server args/reasoning agreement, requests and all role budgets, and report sanitized pair data without server/network activity.
