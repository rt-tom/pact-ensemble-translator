## Purpose

Follow-up to the still-unarchived `local-matrix-v2` change: once approved, this later change supersedes its exact `gemma31`/`qwen38` server-profile statements, the old external-draft `qwen38` B3 gate, and the owner's subsequently approved ordinary `qwen` no-draft update. It uses its own additive capability because `local-model-registry` does not yet exist in canonical `openspec/specs/`; `MODIFIED` there cannot be archived. Shared role budgets, routing, sampling and formatting remain as specified in `local-matrix-v2`; only Qwen3.6's draft enablement changes. Do not archive either change without separate owner approval.

## ADDED Requirements

### Requirement: Tuned existing gemma31 and qwen38 local profiles

`providers.local.models.gemma31` SHALL retain its existing model path/name, `-fit on`, `-fitt 512`, `-b 1024`, context 44000, sampling and reasoning base 2000 with `--reasoning on` and `--reasoning-budget-enable`, but SHALL use `-ub 1024`. `providers.local.models.qwen38` SHALL use `C:/llama-cpp/models/Qwen3.8-27B/Qwen3.8-27B-Q4_0.gguf` for both its path and matching file name, with the exact approved server argument set/order in design §1. It SHALL use embedded MTP (`--spec-type draft-mtp`, `--spec-draft-n-max 2`, `--spec-draft-p-min 0.5`) with no `-md`, `--spec-draft-ngl` or `--spec-draft-device`. It SHALL include `--no-reasoning-preserve` while retaining `--reasoning on`, `--reasoning-budget-enable`, `--reasoning-effort xhigh` and base `--reasoning-budget 8192`. Neither alias SHALL duplicate the lifecycle adapter's `--device SYCL0` with `-dev` or another `--device` in model `server_args`. The `gemma` alias SHALL not change. The `qwen` alias SHALL change only as specified in the separate no-draft requirement below.

#### Scenario: Local aliases resolve to the refreshed profiles
- **WHEN** the registry resolves `--local gemma31/qwen38`
- **THEN** it SHALL expose the new Q4_0 main-model path/name, embedded-MTP server args, Gemma31 `-ub 1024` and retained `-fitt 512`, without duplicate device flags

#### Scenario: Role-effective reasoning is preserved
- **WHEN** the resolved pair serves `generator`, `repair`, `qwen_audit`, `entity_extractor` or `formatting`
- **THEN** the existing base-plus-role deltas SHALL still yield respectively 4000, 2000, 10192, 10192 and 8192 and a changed effective budget SHALL trigger the existing same-model relaunch

### Requirement: B3 validates only the approved qwen38 embedded-MTP profile

For whole-chapter local runs with B3 enabled and `qwen38` selected as reviewer, the B3 gate SHALL assess the **resolved reviewer actually launched**, not the legacy `qwen` entry. It SHALL accept the canonical new Q4_0 main-model identity only with the declared embedded-MTP spec (`draft-mtp`, `--spec-draft-n-max 2`, `--spec-draft-p-min 0.5`, no external draft), a context of at least 44000, and the actual `qwen_audit` role-effective reasoning budget of at least 8192. It SHALL reject the old external-draft Q4_K_XL build under the `qwen38` alias after the switch, as well as missing/incorrect MTP configuration, lookalike or malformed model paths/names, reduced context and reduced role-effective reasoning. The Qwen3.6 `qwen` B3 path is updated only as specified in the separate no-draft requirement below. Checking a path and flags is a configured-profile identity check, not proof of the physical file's MTP contents.

#### Scenario: New approved reviewer passes B3
- **WHEN** whole-chapter B3 uses the registered new `qwen38` profile with `qwen_audit` effective budget 10192
- **THEN** offline B3 validation SHALL pass and report the resolved reviewer configuration

#### Scenario: Old or spoofed reviewer fails B3
- **WHEN** the selected `qwen38` reviewer supplies the previous Q4_K_XL main model, an external draft, a different model path/name, or a non-canonical embedded-MTP argument set
- **THEN** offline B3 validation SHALL fail closed before model startup rather than treating `draft-mtp` alone as sufficient

#### Scenario: Capability budget and context cannot silently weaken
- **WHEN** the selected `qwen38` reviewer loses its role-effective budget flag, has a budget below 8192 or a context below 44000
- **THEN** B3 SHALL fail closed before model startup

### Requirement: Ordinary qwen runs B3 without speculative decoding

The ordinary `qwen` alias SHALL preserve its approved Qwen3.6 MTP-variant GGUF path/name, model-owned request sampling, context 49152, base reasoning budget 8192, `--reasoning-budget-enable` and role-effective reasoning; it SHALL remove `--spec-type draft-mtp` and SHALL carry no other speculative-draft activation flags or external draft model. Keep `configs/providers.yaml`, `configs/runtime_local.example.yaml` and `QWEN_SERVER_ARGS` aligned on the ordered server argument profile. The model filename's MTP marker does not, by itself, enable drafting. The B3 gate SHALL continue to validate the **resolved ordinary `qwen` reviewer**, its exact approved model identity, context >= 49152, and actual `qwen_audit` role-effective budget >= 8192, but SHALL accept the no-draft server args; it SHALL reject an unexpectedly active draft mode, foreign/lookalike model identities and lowered budget/context. B3 audit itself and all hard audit filters SHALL remain enabled. The `qwen38` embedded-MTP B3 profile is a separate contract and remains enabled.

#### Scenario: Default local reviewer passes B3 without MTP
- **WHEN** a whole-chapter local run selects the ordinary `qwen` reviewer with its exact Qwen3.6 model and no draft flags
- **THEN** B3 pre-run validation SHALL accept it and still enforce the 49152 context and role-effective reasoning minimum without skipping the audit

#### Scenario: Accidental drafting and weak B3 profiles fail closed
- **WHEN** the ordinary `qwen` reviewer declares `draft-mtp`, an external draft or another speculative draft option, a foreign model, a lower context or a lower role-effective reasoning budget
- **THEN** B3 validation SHALL fail before startup rather than silently disabling or bypassing audit

### Requirement: Model profile changes do not reuse old run identity

Path and server-argument changes to `gemma31`, `qwen38` or ordinary `qwen` SHALL remain run-identity-bearing. The refreshed aliases SHALL keep the existing model-owned request sampling and role split. Previous artifacts SHALL NOT be presented as if generated with the new model/profile; a retry/resume with incompatible prior identity SHALL fail closed rather than reusing it.

#### Scenario: Old profile cannot be resumed as new
- **WHEN** an existing output directory was created with the former `qwen38` or ordinary `qwen` server profile and a resumed run selects the refreshed profile
- **THEN** the run SHALL refuse incompatible artifact reuse and instruct use of a fresh output directory
