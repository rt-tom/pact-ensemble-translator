## MODIFIED Requirements

### Requirement: Unified v4 command selection
The system SHALL expose one supported v4 command with explicit `book` and `chapter` modes, with `book` as the documented primary workflow. Book mode SHALL require a closed numeric chapter range and exactly one of `--local` or `--remote <translator>/<reviewer>`; it SHALL select the approved host-aware profile and delegate to the existing book entrypoint without changing translation, audit, formatting, or markup semantics for an equivalent resolved invocation. Existing explicit `--runtime-config` invocation SHALL remain available as an advanced compatibility path. The launcher SHALL enable `--whole-chapter` for every book invocation.

#### Scenario: Simple local book selection
- **WHEN** an operator invokes `book --chapters 27-32 --local`
- **THEN** the command SHALL resolve the local host-aware profile, enable whole-chapter mode, and invoke the supported book-run path

#### Scenario: Simple remote book selection
- **WHEN** an operator invokes `book --chapters 27-32 --remote musefree/luna`
- **THEN** the command SHALL resolve the approved remote profile and role selections, enable managed-server and whole-chapter mode, and invoke the supported book-run path

#### Scenario: Invalid book selection is rejected
- **WHEN** an operator omits a mode, combines `--local` and `--remote`, supplies malformed remote roles, or supplies a malformed/reversed range
- **THEN** the command SHALL exit before pipeline startup with a clear validation error

### Requirement: Runtime profile defaults and explicit overrides
The unified command SHALL treat its selected runtime profile as the source of truth for default role models, reasoning, transport, and identity-bearing policy. Simple remote mode SHALL use the profile's Muse Free translator/repair binding, Luna standard reviewer bindings, reasoning level `3`, and managed-server behavior unless an explicit supported override is supplied. Optional model and reasoning selections MAY override profile values, but omitted selections SHALL not introduce hidden quality defaults or silently alter the resolved identity.

#### Scenario: Omitted remote selections use approved defaults
- **WHEN** an operator starts simple remote book mode without model or reasoning overrides
- **THEN** the command SHALL resolve and report the approved Muse Free/Luna bindings, reasoning level `3`, managed-server behavior, and resulting identity

#### Scenario: Explicit runtime selections are identity-bearing
- **WHEN** an operator supplies a supported model or reasoning override
- **THEN** the command SHALL validate it against the runtime/provider contract, forward it to the existing entrypoint, and include the effective value in resolved identity/reporting

### Requirement: Complete safe help
The unified command and each mode SHALL provide `--help` output without starting a pipeline, opening a model session, writing a run artifact, or requiring source/book data. Help SHALL identify numeric range resolution, `--local`/`--remote` selection, host-aware source/state/output locations, remote defaults, whole-chapter default, media synchronization defaults and override, automatic offline preflight/check-only modes, and the owner-started execution boundary.

#### Scenario: Top-level help
- **WHEN** an operator invokes the unified command with `--help`
- **THEN** help SHALL describe both modes, their defaults and host boundaries, and how to obtain mode-specific help without starting a run

#### Scenario: Mode-specific help
- **WHEN** an operator invokes either mode with `--help`
- **THEN** help SHALL describe required arguments and the relevant operational groups without starting the pipeline

### Requirement: Offline preflight before configured execution
The unified command SHALL run approved offline runtime and book-layout preflight by default before configured execution. It SHALL also expose `--preflight` and a machine-readable JSON check-only mode that report the resolved sanitized profile, models, reasoning, policy, topology, identity, host roots, and resolved chapter files before exiting. These modes SHALL not start the pipeline or model lifecycle, contact a provider/remote endpoint, submit source text, synchronize state, or create run artifacts.

#### Scenario: Default preflight rejects before startup
- **WHEN** a configured run has an invalid profile, missing local prerequisite, missing required remote environment variable, or invalid source range
- **THEN** the command SHALL fail before pipeline startup and before creating run artifacts

#### Scenario: Check-only preflight is side-effect-free
- **WHEN** an operator invokes `--preflight` or its JSON form
- **THEN** the command SHALL emit the sanitized resolved report and exit without pipeline, lifecycle, network/provider, source-state synchronization, or artifact side effects
