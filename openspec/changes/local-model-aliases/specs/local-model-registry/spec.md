## Purpose
Alias-driven selection of local models where the alias determines server-start arguments and generation body params.

## ADDED Requirements

### Requirement: Unified local registry

The system SHALL allow `local` models to be defined in the unified `configs/providers.yaml` registry alongside `opencode_server` models.

#### Scenario: Local provider in registry

- **WHEN** `configs/providers.yaml` contains a provider `local` with `kind: local_llama`
- **THEN** the registry loader SHALL accept it, validate each model entry's `model_key` (`gemma`/`qwen`), `model_path`, `model_name`, `server_args` (list-of-strings) and optional `generation` (`temperature`, `seed`, `max_tokens`) and `reasoning_budget`, and SHALL reject a duplicate normalized alias across all providers (including local) fail-closed.

#### Scenario: Bare alias resolution for local

- **WHEN** the alias is given as bare `mygemma` (no slash) and is globally unique
- **THEN** it SHALL resolve to the `local/mygemma` model; a duplicate bare alias across providers SHALL fail-closed and require provider-qualified `local/mygemma` vs `openai/luna`.

### Requirement: Alias-driven LocalLlamaBackendConfig

The system SHALL build the effective `LocalLlamaBackendConfig` from the alias's model fragment when a local alias is given.

#### Scenario: Local alias overrides server_args

- **WHEN** `book --local mygemma` is given and `local/mygemma` defines `server_args` for `gemma`
- **THEN** the effective `LocalLlamaBackendConfig.server_args[gemma]` SHALL equal the alias's `server_args` and the `BackendDescriptor`/`StrictRunConfig.to_config_artifact` identity SHALL change vs the no-alias run.

#### Scenario: Bare local keeps defaults

- **WHEN** `book --local` is given with no alias
- **THEN** the effective `LocalLlamaBackendConfig` SHALL be the canonical `configs/runtime_local.example.yaml` unchanged and byte-identical to the pre-change local run.

### Requirement: All body params from registry (no hardcodes)

The system SHALL have no hardcoded `temperature`/`top_p`/`top_k`/`min_p`/`seed`/`max_tokens` as source of truth for any stage; every stage's body params SHALL come from the provider registry's per-role `generation` block (code literals only as fallback when registry gives nothing).

#### Scenario: Temperature override for generation

- **WHEN** a local alias defines `generation.temperature: 0.7` for the generator role
- **THEN** the generation `CompletionRequest.temperature` SHALL be `0.7` (vs registry default `0.2`), `GenerationParams`/`StrictRunConfig.to_config_artifact` SHALL reflect it.

#### Scenario: Temperature override for audit/repair

- **WHEN** a local (or remote) alias defines `generation.temperature: 0.3` for the `qwen_audit` / `selective_repair` / `russian_editor` / `entity_extractor` / `formatting` role
- **THEN** that stage's `CompletionRequest.temperature` SHALL be `0.3` instead of the former hardcoded `0.0`/`0.1`, and its identity SHALL change.

#### Scenario: Unsupported top_p for local in v1

- **WHEN** a local alias defines `generation.top_p` (or `top_k`/`min_p`) in this change
- **THEN** the system SHALL fail-closed with a clear error (local v1 only supports `temperature`/`seed`/`max_tokens` via body; `top_p`/`top_k` for local are deferred to server_args mapping).

### Requirement: Local reasoning via server_args only

The system SHALL keep local reasoning budget via `server_args --reasoning-budget` and SHALL NOT send `request_options` for local runs.

#### Scenario: Local alias reasoning

- **WHEN** a local alias defines `reasoning_budget` and `server_args` contains `--reasoning-budget`
- **THEN** the two values SHALL agree or loading SHALL fail-closed; the `LocalOpenAIBackend` SHALL receive an empty `request_options` and `validate_reasoning_backend` SHALL pass when the budget agrees with `--reasoning`.

#### Scenario: Remote unchanged

- **WHEN** a remote alias is used
- **THEN** `request_options` (`reasoning`, `top_p`/`top_k`) SHALL continue to be used and `server_args` for remote SHALL not be affected.

### Requirement: CLI alias ergonomics and identity

The system SHALL accept `book --local [alias]` and `chapter --local [alias]` like `book --remote [alias]` and SHALL make the alias identity-bearing.

#### Scenario: CLI parse

- **WHEN** `book --chapters 28 --local mygemma` is given
- **THEN** it SHALL resolve the alias, build the alias-specific backend, and auto-name the output `book_0028_local_mygemma_<ts>` (bare `book --local` stays `book_..._local_...`); `--local` and `--remote` remain mutually exclusive and both exclusive with `--runtime-config`.

#### Scenario: Cache invalidation

- **WHEN** the alias or its `server_args`/`generation` changes
- **THEN** the generation `bundle_hash`/`config_identity` SHALL change so prior cached `translations_raw.json`/`audit_cache_b3.json` are not replayed (new `--out-base` required).

#### Scenario: Preflight

- **WHEN** `book --local mygemma --preflight` is given
- **THEN** `run_runtime_preflight` SHALL resolve the same alias, check `exe`/`model_path`/`port` of the alias-resolved `LocalLlamaBackendConfig`, and report the sanitized `server_args`/`generation`/`reasoning_budget` and identity without starting a server or making a network call.
