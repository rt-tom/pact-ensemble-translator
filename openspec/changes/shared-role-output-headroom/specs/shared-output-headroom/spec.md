## Purpose

Preserve existing content output headroom when local reasoning reaches its configured budget, while using one shared role policy for local and remote models.

## ADDED Requirements

### Requirement: Shared role output headroom

For each of the ten roles, the configured response maximum SHALL equal its previous content allowance plus the maximum effective reasoning budget among current local aliases serving that role. The same role budget SHALL be used for remote model paths. The entity extractor's preceding 20,000-token cap SHALL be its content allowance.

#### Scenario: Local model reasons to its allowance
- **WHEN** a current local model uses its full configured effective reasoning budget for a role
- **THEN** the role's output cap minus that budget SHALL be at least the role's previous content cap.

#### Scenario: Remote role is selected
- **WHEN** a remote model is selected for a role
- **THEN** the shared raised role cap SHALL apply without a provider-specific override.

### Requirement: Dynamic output formulas retain content behavior

For `floor_plus_per_item` and `span_formula` roles, the configured floor/base and ceiling SHALL each increase by the same role reasoning offset. Per-item and per-span increments SHALL remain unchanged.

#### Scenario: A dynamic budget reaches its ceiling
- **WHEN** a dynamic role has enough items or spans to reach its ceiling
- **THEN** the new ceiling SHALL be the prior ceiling plus that role's maximum local effective reasoning budget.

### Requirement: Reasoning and routing remain stable

This change SHALL leave model reasoning budgets, server arguments, provider endpoints, role routing, and lifecycle policy unchanged. Existing output-budget identity and cache behavior SHALL continue to apply.
