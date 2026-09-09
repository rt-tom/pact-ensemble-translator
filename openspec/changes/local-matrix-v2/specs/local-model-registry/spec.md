## Purpose

Hybrid reasoning and extensible sampling for the model-centric registry, plus two new local models and local formatting model path.

## ADDED Requirements

### Requirement: Extensible model sampling

The registry SHALL allow `providers.local.models.<alias>.request` to contain only sampling fields from `{temperature, top_p, top_k, min_p, seed, repeat_penalty, repeat_last_n, frequency_penalty, presence_penalty}`. `max_output_tokens`/`reasoning`/`reasoning_budget` inside `request` SHALL fail-closed. Any other key SHALL fail-closed until explicitly allowlisted.

#### Scenario: New sampling field is accepted
- **WHEN** `gemma31.request.repeat_penalty` is `1.0`
- **THEN** validation SHALL pass and the request SHALL serialize `repeat_penalty`.

### Requirement: Hybrid reasoning (role + model)

`role_budgets.<role>` MAY contain optional `reasoning_budget: int 0..8192`. `providers.local.models.<alias>.reasoning_budget` remains required. Effective reasoning for a call SHALL be `model.reasoning_budget + (role_budgets[role].reasoning_budget or 0)`. Per grill: `gemma31 2000 + generator 2000 = 4000`, `qwen38 8192 + entity_extractor/qwen_audit 2000 = 10192`, others `0`. Current `ModelRouter.ensure_resident(model_key)` launches only static args per model key; implementation SHALL restart/relaunch the same model when the next role requires a different effective budget, replacing its `--reasoning-budget`. It SHALL NOT silently reuse the wrong budget or promote it to group maximum. Fresh-call provenance SHALL record actual launch args. Effective reasoning SHALL NOT create a run-identity dimension or invalidate/reject request cache or resume artifacts; a cache hit retains its original provenance.

#### Scenario: Role delta increases effective
- **WHEN** `qwen38.reasoning_budget` is `8192` and `role_budgets.qwen_audit.reasoning_budget` is `2000`
- **THEN** `qwen_audit` via `qwen38` SHALL run with effective `10192`.

#### Scenario: Consecutive same-model effective budgets
- **WHEN** one local model must serve two consecutive roles with different effective budgets
- **THEN** it SHALL restart/relaunch the model with a replaced `--reasoning-budget`, record the actual launch args, and SHALL NOT silently reuse the prior role's budget.

### Requirement: Two new local models

The registry SHALL contain `gemma31` and `qwen38` with the exact server_args/request/reasoning_budget from design §2 (PowerShell args minus host/port, `-dev` preserved, `-md` draft for `qwen38`, `xhigh` quoted). Existing `gemma`/`qwen` SHALL gain `--reasoning-budget-enable` in `server_args`.

#### Scenario: New aliases are resolvable
- **WHEN** `book --local gemma31/qwen38` is invoked
- **THEN** `gemma31` SHALL serve translator roles and `qwen38` reviewer roles with their registered paths/server_args.

#### Scenario: Existing models gain budget-enable flag
- **WHEN** `gemma` or `qwen` is inspected
- **THEN** its `server_args` SHALL contain `--reasoning-budget-enable`.

### Requirement: Local formatting model path

`formatting` SHALL belong to reviewer-group (`qwen*`) in the shared local/remote fixed-role contract. `book --local` SHALL use a separate per-chapter reviewer-model formatting server when inline spans exist: the strict chapter run has released its lifecycle first; start the reviewer model's resolved launch args, health-wait, call `resolve_format_mappings`, then close. On failure it SHALL fall back to deterministic `run_formatting_align` and mark debt. It SHALL NOT claim reuse of an already-resident re-audit process. Direct `chapter --local` strict path SHALL remain deterministic; no `--force-formatting-model` CLI is added.

#### Scenario: Local book formatting uses reviewer model
- **WHEN** `book --local gemma31/qwen38` runs on a chapter with inline spans
- **THEN** a separate per-chapter formatting server SHALL be started from `qwen38`’s resolved launch args and `resolve_format_mappings` SHALL receive `qwen38` sampling.

#### Scenario: Local chapter strict stays deterministic
- **WHEN** `chapter --local` is run
- **THEN** formatting SHALL use `run_formatting_align` with 0 model calls.

