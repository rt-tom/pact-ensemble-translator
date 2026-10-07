## ADDED Requirements

### Requirement: Local model profiles use full GPU-layer offload

In `configs/providers.yaml`, `configs/runtime_local.example.yaml`, `configs/runtime_composite.example.yaml`, and the executable profiles `GEMMA_SERVER_ARGS` in `pact_full_pipeline_runner_v1/v4_phase12_strict_run.py` and `GEMMA_COMMON_ARGS` in `pact_full_pipeline_runner_v1/v4_model_lifecycle_bench.py`, every local model profile whose main-model server arguments specify `-ngl 99` SHALL instead specify `-ngl all`. A profile with a different main-model offload setting SHALL retain that setting unless separately approved. This requirement applies to executable model profiles, not historical OpenSpec examples or unrelated test fixtures.

For every in-scope local profile that explicitly enables MTP using `--spec-type draft-mtp`, the server arguments SHALL contain exactly one `--spec-draft-ngl all`. Profiles already satisfying this requirement SHALL retain their current MTP configuration. A model filename containing `MTP` without an active `--spec-type draft-mtp` does not make that profile MTP-enabled.

This requirement supersedes only prior Qwen38 clauses that prohibit `--spec-draft-ngl` or require the Qwen38 B3 gate to remain unchanged. All other Qwen38 and local-model profile requirements remain in force.

#### Scenario: Existing main-model 99 offload is replaced
- **WHEN** one of the in-scope runtime config files or executable local-model profiles declares `-ngl 99`
- **THEN** that profile SHALL declare `-ngl all` instead, with unrelated arguments unchanged

#### Scenario: Explicit MTP profiles offload draft layers fully
- **WHEN** an in-scope configured or executable local model profile explicitly contains `--spec-type draft-mtp`
- **THEN** its arguments SHALL contain exactly one `--spec-draft-ngl all`
- **AND** profiles without an active MTP selector SHALL NOT gain a draft-layer flag solely because `MTP` appears in a model filename

### Requirement: Qwen38 B3 validates the approved offload settings

The Qwen38 B3 reviewer gate SHALL require exactly one `-ngl all` and exactly one `--spec-draft-ngl all` in the selected reviewer arguments. It SHALL continue to require the approved Qwen38 model identity, embedded-MTP selector and parameters, context floor, reasoning options, and role-effective minimum budget. It SHALL continue to reject external-draft and duplicate lifecycle-device flags. Absent, duplicate, valueless, malformed, or non-`all` offload flags SHALL fail closed. No other B3 check, audit execution, or hard filter is weakened.

#### Scenario: Approved Qwen38 offload profile passes B3
- **WHEN** B3 selects the approved Qwen38 reviewer with exactly one `-ngl all` and `--spec-draft-ngl all`, and all existing identity/MTP/context/budget conditions are met
- **THEN** the Qwen38 capability gate SHALL accept the profile

#### Scenario: Qwen38 offload flags fail closed
- **WHEN** either offload flag is missing, duplicated, valueless, malformed, or has a value other than the exact string `all`
- **THEN** the Qwen38 B3 capability gate SHALL reject the profile
- **AND** the error SHALL identify the missing or invalid offload requirement
