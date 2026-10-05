## Why

The Qwen38 profile currently launches llama-server with `--reasoning-effort xhigh`. RT run artifacts show long Qwen38 stages. The owner requests a narrow profile adjustment to `low`, while preserving all numeric reasoning budgets. This is a separate change from the active `qwen38-gemma31-profile-refresh`; do not edit or merge this scope into that change. It supersedes only that change's `qwen38` `--reasoning-effort xhigh` clause; all other requirements and profile constraints there remain in force.

## What Changes

- Replace the existing `--reasoning-effort xhigh` value with `--reasoning-effort low` in the `qwen38` entry in `configs/providers.yaml`.
- Keep the Qwen38 base `--reasoning-budget 8192` and shared role deltas unchanged, including `qwen_audit +2000` and `entity_extractor +2000`.
- Do not append a second effort flag. Preserve all other Qwen38 arguments, model identity, embedded-MTP configuration, context, sampling, role routing, and B3 validation.
- Do not change the ordinary `qwen`, Gemma/Gemma31 profiles, output budgets, or runtime logic.

## Capabilities

### Modified Capabilities
- `qwen38-reasoning-effort`: defines the Qwen38 launch effort while preserving the existing numeric budget contract.

## Impact and approval boundary

This is a runtime/model-policy change despite its one-line implementation. The owner has requested `low` and accepted the RT compatibility risk. The change must still receive approval of this separate proposal before implementation, use an isolated worktree, and pass independent review. Development validation is offline only: no model server or pipeline launch. Compatibility of `low` with the RT llama-sycl-edge build will remain unverified unless separately tested by the owner.
