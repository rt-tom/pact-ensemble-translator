## ADDED Requirements

### Requirement: Qwen38 uses low reasoning effort without reducing numeric budgets

This requirement supersedes only the prior `qwen38-gemma31-profile-refresh` requirement that `qwen38` use `--reasoning-effort xhigh`; that change remains unmodified, and all its other Qwen38 profile requirements remain in force. The local `qwen38` model profile SHALL contain exactly one `--reasoning-effort low` server argument and SHALL NOT contain `--reasoning-effort xhigh`. It SHALL preserve the base reasoning budget of 8192 and the existing shared role-budget deltas; in particular, `qwen_audit` and `entity_extractor` SHALL remain at effective budget 10192, and roles with zero delta SHALL retain their current effective budgets. The profile SHALL preserve its current model identity, embedded-MTP arguments, context, sampling, routing, and all other server arguments. The ordinary `qwen` profile, Gemma/Gemma31 profiles, output-token budgets, lifecycle behavior, and Qwen38 B3 gate SHALL remain unchanged. A second or duplicate reasoning-effort flag is forbidden.

#### Scenario: Qwen38 resolves to low effort with unchanged budgets
- **WHEN** the local registry resolves the `qwen38` profile and the runtime launches a role on it
- **THEN** the resolved server arguments SHALL carry exactly one `--reasoning-effort low`, the role-effective numeric budget SHALL remain the existing base-plus-delta value, and the registered model/profile identity and other arguments SHALL be unchanged

#### Scenario: Qwen38 B3 remains fail-closed
- **WHEN** whole-chapter B3 selects `qwen38` as reviewer
- **THEN** the existing model identity, embedded-MTP, context, and `qwen_audit` minimum-budget checks SHALL remain active; changing the effort value SHALL NOT bypass or weaken B3 validation
