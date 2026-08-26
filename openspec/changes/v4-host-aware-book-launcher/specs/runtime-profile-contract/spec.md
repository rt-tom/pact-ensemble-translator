## MODIFIED Requirements

### Requirement: Canonical remote profile is portable and policy-bearing
The system SHALL provide an `opencode_server` example profile that contains no credentials or host-specific model paths, declares its remote transport and complete model/policy defaults explicitly, and uses only environment-variable names for authentication references. The canonical remote profile SHALL set reasoning level `3`, bind generator and repair to `opencode/muse-spark-1.2-contributor-free`, and bind the standard reviewer roles (`fidelity_reviewer`, `russian_selector`, `qwen_audit`, and `entity_extractor`) to `openai/gpt-5.6-luna`. `gemma_audit` remains outside the standard reviewer override unless separately selected. Code defaults remain compatibility fallbacks rather than hidden simple-mode quality overrides. The composite example SHALL remain advanced/test-only.

#### Scenario: Portable remote defaults are loaded
- **WHEN** an operator loads the canonical remote example on a supported host with required environment variables configured
- **THEN** the resolved profile SHALL expose reasoning `3`, Muse Free generator/repair, Luna standard reviewer bindings, and no credential value or host-specific local-model path

#### Scenario: Remote policy is not silently replaced
- **WHEN** a remote profile explicitly sets an identity-bearing policy value
- **THEN** the resolved runtime identity and preflight report SHALL reflect that profile value rather than a hidden command default

### Requirement: Host-local profile preflight is side-effect-free and auditable
The configured runtime path SHALL perform an offline preflight by default before every configured run and SHALL offer an explicit `--preflight` mode that performs the check and exits without starting the pipeline. It SHALL report the sanitized resolved profile, role/model bindings, identity, and effective policy, including any optional model/reasoning overrides. For a remote profile it SHALL validate required environment-variable presence and configuration syntax on the current host; host-aware book preflight extends this with source/state/output checks. Existing remote endpoint/network preflight remains a separate runtime check and is not part of this offline stage.

The preflight SHALL not start or stop a model server, contact an OpenCode/provider endpoint, submit source text, create run artifacts, or reveal environment-variable values or credentials. A failed prerequisite SHALL produce a non-zero result before pipeline startup.

#### Scenario: Missing remote environment variable fails safely
- **WHEN** an operator preflights a remote profile whose declared authentication environment variable is absent or empty
- **THEN** the preflight SHALL fail before provider contact, identify only the variable name, and not disclose a credential value

#### Scenario: Preflight reports resolved remote defaults and overrides
- **WHEN** an operator preflights a remote profile with no model/reasoning override, or with selected translator, reviewer, or reasoning overrides
- **THEN** the report SHALL show the profile defaults when overrides are absent, otherwise the resulting role/model bindings, reasoning policy, and identity after overrides are resolved
