## Purpose

Defines safe, portable runtime profiles that can be validated on an execution host before a translation run is allowed to start.

## ADDED Requirements

### Requirement: Canonical local profile is fail-closed and internally coherent
The system SHALL load a `local_llama` runtime profile only when it contains exclusively documented local-profile fields with valid scalar and mapping shapes. It SHALL reject unknown keys, missing required executable/model data, non-string server arguments, invalid local host/port values, and a model-key mismatch across `model_paths`, `model_names`, and `server_args` before any lifecycle action.

The canonical local example SHALL use the current supported local identity and transport. The historical invocation without a runtime-config file SHALL retain its existing identity and transport behavior.

#### Scenario: Unknown local field is rejected
- **WHEN** an operator supplies a `local_llama` profile containing an unsupported top-level or local nested field
- **THEN** loading SHALL fail with a clear profile-validation error before a model server is started

#### Scenario: Incoherent local model mappings are rejected
- **WHEN** an operator supplies a local profile whose model paths, model names, and server-argument mappings do not describe the same supported model keys
- **THEN** loading SHALL fail with an error identifying the incoherent field relationship before a run begins

#### Scenario: Legacy no-config invocation remains compatible
- **WHEN** an operator invokes the existing strict CLI without `--runtime-config`
- **THEN** the system SHALL retain the historical local identity and transport rather than silently substituting the canonical profile file

### Requirement: Canonical remote profile is portable and policy-bearing
The system SHALL provide an `opencode_server` example profile that contains no credentials or host-specific model paths, declares its remote transport and complete model/policy defaults explicitly, and uses only environment-variable names for authentication references. The profile SHALL explicitly record default role model bindings, reasoning level/transport mapping, timeout, remote budget, structured-output policy, and other identity-bearing policy; code defaults remain compatibility fallbacks rather than hidden simple-mode quality overrides. Optional model and reasoning overrides MAY be supplied at invocation time, but when omitted the profile values SHALL be used.

The composite example SHALL remain advanced/test-only and SHALL NOT be updated by this change.

#### Scenario: Portable remote example is loaded
- **WHEN** an operator loads the canonical remote example on a supported host with its required environment variables configured
- **THEN** the resolved profile SHALL contain no credential value or host-specific local-model path and SHALL expose its explicit remote policy and model bindings

#### Scenario: Remote policy is not silently replaced
- **WHEN** a remote profile explicitly sets an identity-bearing policy value
- **THEN** the resolved runtime identity and preflight report SHALL reflect that profile value rather than a hidden command default

### Requirement: Provider aliases support unambiguous simple selection
The provider registry SHALL resolve aliases case-insensitively and SHALL reject any duplicate alias globally across configured providers during registry loading. Bare model selection for future simple remote mode SHALL therefore resolve only one unambiguous provider/model entry. Existing provider-qualified `provider/alias` resolution SHALL remain supported for backward compatibility when the registry is valid.

#### Scenario: Unique bare alias resolves
- **WHEN** a bare alias occurs under exactly one configured provider
- **THEN** the resolver SHALL return that provider's model reference and declared reasoning contract

#### Scenario: Duplicate bare alias fails closed
- **WHEN** a bare alias occurs under more than one configured provider
- **THEN** the resolver SHALL fail before run startup and report that a provider-qualified legacy reference is required until the registry is made unique

### Requirement: Host-local profile preflight is side-effect-free and auditable
The strict runtime-config path SHALL perform an offline preflight by default before every configured run and SHALL offer an explicit `--preflight` mode that performs the check and exits without starting the pipeline. It SHALL report the sanitized resolved profile, role/model bindings, identity, and effective policy, including any optional model/reasoning overrides. For the local profile it SHALL validate the executable and model paths and the configured local port on the current host; for a remote profile it SHALL validate required environment-variable presence and configuration syntax on the current host. Existing remote endpoint/network preflight remains a separate runtime check and is not part of this offline stage.

The preflight SHALL not start or stop a model server, contact an OpenCode/provider endpoint, submit source text, create run artifacts, or reveal environment-variable values or credentials. A failed prerequisite SHALL produce a non-zero result before pipeline startup.

#### Scenario: Configured run performs offline preflight by default
- **WHEN** an operator starts a configured run without an explicit preflight flag
- **THEN** the system SHALL perform the offline profile preflight before pipeline startup and SHALL reject failed prerequisites without creating run artifacts

#### Scenario: Explicit preflight exits before pipeline startup
- **WHEN** an operator invokes the configured runtime path with `--preflight`
- **THEN** the system SHALL print the resolved sanitized report and exit without starting the pipeline, model server, or remote endpoint call

#### Scenario: Local prerequisites are reported without startup
- **WHEN** an operator preflights a valid local runtime profile on its intended host
- **THEN** the preflight SHALL report path and port readiness plus the resolved sanitized identity without starting a model server or creating a run directory

#### Scenario: Missing remote environment variable fails safely
- **WHEN** an operator preflights a remote profile whose declared authentication environment variable is absent or empty
- **THEN** the preflight SHALL fail before provider contact, identify only the variable name, and not disclose a credential value

#### Scenario: Preflight reports resolved remote defaults and overrides
- **WHEN** an operator preflights a remote profile with no model/reasoning override, or with selected translator, reviewer, or reasoning overrides
- **THEN** the report SHALL show the profile defaults when overrides are absent, otherwise the resulting role/model bindings, reasoning policy, and identity after overrides are resolved
