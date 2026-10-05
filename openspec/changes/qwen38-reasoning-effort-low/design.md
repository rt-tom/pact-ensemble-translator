## Scope

Create a standalone follow-up for the Qwen38 reasoning-effort setting. Leave `qwen38-gemma31-profile-refresh` and every unrelated active change untouched.

## Approach

1. In `configs/providers.yaml`, change only the value following the existing `--reasoning-effort` flag in `providers.local.models.qwen38.server_args`: `xhigh` → `low`. Keep one occurrence of the flag.
2. Preserve `reasoning_budget: 8192`, `--reasoning-budget 8192` in the registered base profile, `--reasoning-budget-enable`, and all shared role-budget deltas. Runtime role resolution continues to replace only the numeric budget per role: Qwen38 `qwen_audit` and `entity_extractor` remain 10192 effective; zero-delta roles remain 8192; formatting remains overridden to 0.
3. Update the focused profile-matrix expectation and add/adjust an assertion that the Qwen38 profile has exactly one `--reasoning-effort low`, no `xhigh`, and unchanged numeric base/role-effective budgets. Do not alter the Qwen38 B3 gate or its minimum budget/context checks.

## Compatibility and rollout

`--reasoning-effort` and `--reasoning-budget` are separate controls. This change requests lower effort but does not lower the numeric budget ceiling. The RT run artifacts/config show the old effort value; this design does not claim the RT server has accepted `low`. Do not start llama-server or a pipeline during implementation. Existing server-argument identity behavior remains in force; do not resume old-profile run artifacts as though they used the new setting.
