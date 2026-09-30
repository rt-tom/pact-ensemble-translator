## ADDED Requirements

### Requirement: Formatting-only local zero-reasoning override

The local model registry SHALL support an explicitly validated formatting-only reasoning_budget_override of 0. For formatting, this override SHALL replace the reviewer model base plus role delta when calculating effective reasoning, server launch args and fresh-call provenance. Other roles SHALL retain the existing model-base-plus-role-delta formula. A malformed override or an override on another role SHALL fail configuration validation.

This is an explicit exception to the Hybrid reasoning (role + model) requirement in the local-matrix-v2 change. The reviewer model profile itself SHALL remain unchanged.

#### Scenario: Qwen38 switches from audit to formatting
- **WHEN** qwen38 serves qwen_audit with effective reasoning 10192 and next serves formatting
- **THEN** the router relaunches as needed with exactly one --reasoning-budget 0, and the formatting provenance reports effective 0

#### Scenario: Formatting switches back to another reviewer role
- **WHEN** qwen38 has served formatting at budget 0 and next serves entity_extractor
- **THEN** the router restores that role's effective reasoning budget and records the actual launch args

#### Scenario: Generator and audit budgets remain unchanged
- **WHEN** the registry resolves gemma31 generator and qwen38 qwen_audit
- **THEN** their effective reasoning budgets remain 4000 and 10192 respectively

#### Scenario: Invalid override rejected
- **WHEN** reasoning_budget_override is nonzero, non-integer, or attached to a role other than formatting
- **THEN** provider registry loading fails before a model server starts
