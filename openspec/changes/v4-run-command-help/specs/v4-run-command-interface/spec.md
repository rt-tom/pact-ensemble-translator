## Purpose

Provides a single discoverable operator command for supported v4 chapter and book runs, with accurate offline help that prevents accidental use of legacy launchers or unsafe guessing of run parameters.

## ADDED Requirements

### Requirement: Unified v4 command selection
The system SHALL expose one supported v4 command with explicit `book` and `chapter` modes, with `book` as the documented primary workflow. Book mode SHALL require an explicit chapter range and runtime profile, resolve source files through the existing supported chapter HTML pattern, and delegate to the existing book entrypoint without changing translated output, audit/formatting/markup semantics, runtime routing, resume identity, or artifact layout for an equivalent invocation. The launcher SHALL use profile-provided models, reasoning, transport, and identity-bearing policy by default, applying model/reasoning overrides only when explicitly supplied.

#### Scenario: Chapter mode selection
- **WHEN** an operator invokes the unified command with the `chapter` mode and valid chapter arguments
- **THEN** the command SHALL invoke the supported strict chapter path with equivalent arguments and exit behavior

#### Scenario: Book mode selection
- **WHEN** an operator invokes the unified command with `book`, a valid range such as `27-32`, and a valid runtime profile
- **THEN** the command SHALL resolve the range using the existing chapter HTML pattern, use the profile defaults unless explicit overrides are supplied, and invoke the supported book-run path with equivalent arguments and exit behavior

#### Scenario: Invalid chapter range
- **WHEN** an operator supplies a malformed, reversed, or empty chapter range
- **THEN** the command SHALL exit before pipeline startup with a clear validation error

### Requirement: Runtime profile defaults and explicit overrides
The unified command SHALL treat the selected runtime profile as the source of truth for default role models, reasoning, transport, identity-bearing policy, and local/remote topology. Optional model and reasoning selections MAY override those profile values, but omitted selections SHALL not introduce launcher-specific quality defaults or silently alter the resolved identity.

#### Scenario: Omitted runtime selections use profile defaults
- **WHEN** an operator starts book or chapter mode without model or reasoning overrides
- **THEN** the command SHALL resolve and forward the profile's declared models and reasoning policy and expose the resulting identity in preflight output

#### Scenario: Explicit runtime selections are identity-bearing
- **WHEN** an operator supplies a supported model or reasoning override
- **THEN** the command SHALL validate it against the runtime/provider contract, forward it to the existing entrypoint, and include the effective value in resolved identity/reporting

### Requirement: Complete safe help
The unified command and each mode SHALL provide `--help` output without starting a pipeline, opening a model session, writing a run artifact, or requiring source/book data. Help SHALL identify the explicit chapter range and runtime profile required by book mode, existing source-pattern resolution, automatic output naming under `D:\\pact\\gate_bench_runs`, profile defaults and optional model/reasoning overrides, automatic offline preflight plus `--preflight`/JSON check-only modes, runtime/provider configuration, topology/resume choices, audit/formatting behavior, the explicit `--markup preserve` contract, and that production runs are owner-started on RT.

#### Scenario: Top-level help
- **WHEN** an operator invokes the unified command with `--help`
- **THEN** help SHALL describe both modes, show how to get mode-specific help, identify the supported v4 path, explain profile-resolved defaults and automatic preflight/check-only modes, and state the owner-started RT boundary

#### Scenario: Mode-specific help
- **WHEN** an operator invokes either mode with `--help`
- **THEN** help SHALL describe required arguments and the relevant operational groups without starting the pipeline

#### Scenario: Automatic output directory
- **WHEN** an operator starts a valid book run for chapters `27-32` using a local or remote runtime profile
- **THEN** the command SHALL create a distinct output subdirectory below `D:\\pact\\gate_bench_runs` named `book_0027-0032_local_<timestamp>` or `book_0027-0032_remote_<timestamp>` respectively, based on the resolved descriptor rather than a user-supplied topology claim

### Requirement: Offline preflight before configured execution
The unified command SHALL run the approved offline runtime-profile preflight by default before configured execution. It SHALL also expose `--preflight` and a machine-readable JSON check-only mode that report the resolved sanitized profile, models, reasoning, policy, topology, and identity before exiting. These modes SHALL not start the pipeline or model lifecycle, contact a provider/remote endpoint, submit source text, or create run artifacts. Existing remote endpoint preflight remains a separate transport check during actual execution.

#### Scenario: Default preflight rejects before startup
- **WHEN** a configured run has an invalid profile, missing local prerequisite, or missing required remote environment variable
- **THEN** the command SHALL fail before pipeline startup and before creating run artifacts

#### Scenario: Check-only preflight is side-effect-free
- **WHEN** an operator invokes `--preflight` or its JSON form
- **THEN** the command SHALL emit the sanitized resolved report and exit without pipeline, lifecycle, network/provider, source, or artifact side effects

### Requirement: Explicit markup preservation selection
The book mode SHALL accept only `--markup preserve` as its markup option. It SHALL forward or validate this choice without changing the existing formatting/markup preservation and normalization semantics.

#### Scenario: Preserve markup option
- **WHEN** an operator invokes book mode with `--markup preserve`
- **THEN** the command SHALL retain the existing formatting/markup policy and SHALL not introduce a new tag transformation

#### Scenario: Unsupported markup option
- **WHEN** an operator supplies a markup value other than `preserve`
- **THEN** the command SHALL exit before pipeline startup with a clear validation error

### Requirement: Legacy-launcher separation
The unified command help and operator documentation SHALL not present v3/v31 `run_full_pipeline*.ps1` launchers as supported v4 commands.

#### Scenario: Documentation navigation
- **WHEN** an operator reads the v4 command documentation
- **THEN** it SHALL direct the operator to the unified v4 command and label production execution as owner-started on RT
