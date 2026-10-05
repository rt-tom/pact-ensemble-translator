## Approval gate

- [x] 0.1 Owner requests only the Qwen38 `reasoning-effort` change from `xhigh` to `low`; numeric reasoning budgets are to remain unchanged. Owner accepts the RT compatibility risk.
- [x] 0.2 Owner approves this standalone OpenSpec proposal before implementation.

## Implementation (after proposal approval only)

- [x] 1.1 Change only Qwen38's existing `--reasoning-effort xhigh` value to `low`; preserve the flag exactly once and leave base/role budgets and other profile behavior unchanged.
- [x] 1.2 Update focused Qwen38 profile tests for the effort value and unchanged numeric budgets; do not modify the B3 gate or ordinary-Qwen/Gemma profiles.

## Verification (offline only)

- [x] 2.1 Run strict OpenSpec validation, focused local-matrix tests, diff checks, and independent `pact-rev` review. Do not launch a model server or pipeline.
- [x] 2.2 Record that RT llama-sycl-edge acceptance of `--reasoning-effort low` was not tested here; no claim of runtime validation.

## Rollout boundary

- [ ] 3.1 Use the already authorized branch/PR/merge/deploy path only after proposal approval, implementation, tests, and independent review. Deployment updates code/config only; do not start a model server or pipeline.
