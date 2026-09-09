## Purpose

Defines alias-driven local model selection and explicit provider-owned model-call policy for every V4 strict/book stage.

## ADDED Requirements

### Requirement: Complete provider-owned role policy

The system SHALL obtain every V4 strict/book model-call body setting from a validated provider registry role policy, not from a code literal or descriptor-introspection fallback. Required roles SHALL be `generator`, `fidelity_reviewer`, `russian_selector`, `qwen_audit`, `gemma_audit`, `repair`, `entity_extractor`, `russian_editor`, `formatting`, and `glossary_resolver`.

Each role policy SHALL declare its routing model key/binding and a `request` map whose allowed fields are `temperature`, `top_p`, `top_k`, `min_p`, `seed`, and `max_output_tokens`. A dynamically sized request SHALL additionally declare all deterministic budget inputs (mode, base/floor, per-item allowance, and ceiling) in `output_budget`.

#### Scenario: Complete default local policy

- **WHEN** bare `book --local` is resolved
- **THEN** the resolved local provider policy SHALL contain every required role and preserve each former generation/audit/repair/editor/formatting/resolver request setting without reading a sampling/output default from code.

#### Scenario: Missing or unknown role fails closed

- **WHEN** a provider policy omits a required role, names an unknown role, supplies an unknown request field, or has an invalid value/range
- **THEN** registry loading SHALL fail before preflight, server start, or model call.

#### Scenario: Dynamic audit budget is policy-owned

- **WHEN** a `qwen_audit` policy uses a per-PID output budget
- **THEN** its base/floor, per-PID allowance, and ceiling SHALL be read from the role policy and the final derived `max_output_tokens` SHALL be recorded in the request identity.

### Requirement: Unified local alias registry

The system SHALL allow a `local` provider of `kind: local_llama` in `configs/providers.yaml`. Its explicit `role_policies` are required; its `models` mapping MAY be empty until a local alias is added. Each alias SHALL define `model_key`, `model_path`, `model_name`, `server_args`, optional `reasoning_budget`, and optional role-keyed policy overrides.

#### Scenario: Compatible alias override

- **WHEN** `local/mygemma` replaces model key `gemma` and overrides `generator.request.temperature`
- **THEN** it SHALL replace only the Gemma server fragment and compatible role policies; a Qwen audit role SHALL retain the local provider default unless independently and compatibly overridden.

#### Scenario: Invalid local model entry fails closed

- **WHEN** a local alias has an unsupported model key, empty path/name, non-string server argument, incompatible role override, or `reasoning_budget` that differs from `--reasoning-budget`
- **THEN** registry loading SHALL fail closed with the alias and field identified.

#### Scenario: Alias uniqueness and qualified selection

- **WHEN** a bare alias is globally unique
- **THEN** it SHALL resolve case-insensitively; a duplicate normalized alias across providers SHALL fail at registry load, while `local/alias` remains a supported qualified form.

### Requirement: Local request body and reasoning transport

The local OpenAI-compatible transport SHALL serialize every declared local sampling field that it supports: `temperature`, `top_p`, `top_k`, `min_p`, `seed`, and `max_output_tokens`. Local reasoning SHALL remain exclusively a `llama-server` policy (`server_args --reasoning-budget`) and SHALL NOT be serialized as remote `reasoning` request options.

#### Scenario: Local seed and sampling serialization

- **WHEN** a local role policy supplies `seed`, `top_p`, `top_k`, or `min_p`
- **THEN** the local request body SHALL contain that exact value, the effective policy/identity SHALL contain it, and no value SHALL be silently ignored or replaced with a code default.

#### Scenario: Unsupported local request option

- **WHEN** a local policy requests a field unsupported by the verified local transport contract
- **THEN** registry validation or preflight SHALL fail with a clear error; the transport SHALL not silently drop the field.

#### Scenario: Local reasoning remains server-side

- **WHEN** a local alias uses reasoning
- **THEN** its `reasoning_budget` SHALL agree with `server_args --reasoning-budget`, local `CompletionRequest` SHALL have no `reasoning` request option, and the local server start/preflight validation SHALL enforce the required reasoning server flags.

### Requirement: Alias CLI, provenance, and cache identity

The system SHALL accept `book|chapter --local [alias]`. Bare local SHALL use the canonical runtime transport and the explicit provider default role policies; alias local SHALL use the alias-resolved transport and policies. `--local`, `--remote`, and `--runtime-config` SHALL remain mutually exclusive.

The complete resolved role-policy map SHALL have an aggregate identity hash. Every model-call producer SHALL include its own role-policy hash and final derived request budget in the cache/artifact identity that governs its output.

#### Scenario: Audit-only policy change invalidates audit data

- **WHEN** only `qwen_audit.request.temperature` or its dynamic-budget inputs change
- **THEN** audit/re-audit cache identity and audit provenance SHALL change, prior audit results SHALL not be replayed, and unrelated role policy hashes SHALL remain unchanged.

#### Scenario: Generation policy change invalidates generation data

- **WHEN** `generator.request.temperature`, `seed`, or output-budget policy changes
- **THEN** `PromptBundle.bundle_hash`, generation provenance, and run identity SHALL change before cached generation output can be reused.

#### Scenario: Preflight is side-effect free and complete

- **WHEN** `book --local mygemma --preflight` is invoked
- **THEN** it SHALL resolve the same alias and complete role-policy map as execution, validate model path/server arguments and transport field support, print sanitized per-role policies and hashes, and neither start a server nor make a network call.
