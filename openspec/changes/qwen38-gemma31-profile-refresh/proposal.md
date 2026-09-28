## Why

The owner has a new Qwen3.8 27B Q4_0 model with an embedded MTP draft and a tuned Gemma4 31B Q5 server profile on RT. `main` still registers `qwen38` as a different Q4_K_XL build with an external `-md` draft, and its fail-closed B3 gate requires precisely that old combination. The new Qwen profile therefore cannot be adopted by changing only a path; the approved B3 capability contract must change explicitly. The owner's Gemma31 command also increases `-ub` from 512 to 1024; the existing `-fitt 512` is to remain.

## What Changes

- Keep the aliases `gemma31`/`qwen38`, their role split, model-owned sampling and hybrid reasoning budgets. Refresh those profiles in `configs/providers.yaml`; do not alter remote models or the `gemma` alias. An owner-approved addendum also changes the ordinary `qwen` (Qwen3.6) profile in the registry, `configs/runtime_local.example.yaml`, and historical `QWEN_SERVER_ARGS` consistently: disable MTP drafting without removing its B3 audit.
- Point `qwen38` to `C:/llama-cpp/models/Qwen3.8-27B/Qwen3.8-27B-Q4_0.gguf`. Replace the external draft configuration (`-md`, `--spec-draft-ngl`, `--spec-draft-device`) with the owner's embedded-MTP configuration (`--spec-type draft-mtp`, `--spec-draft-n-max 2`, `--spec-draft-p-min 0.5`). Add `--no-reasoning-preserve`. Preserve existing `--reasoning on`, `--reasoning-budget-enable`, `--reasoning-effort xhigh` and base `--reasoning-budget 8192` for the current role-effective launch contract. Do not duplicate the `--device SYCL0` injected by the lifecycle adapter; remove the redundant `-dev SYCL0` in these two aliases.
- Increase `gemma31` `-ub` to 1024, retain its existing `-fitt 512`, context 44000 and existing reasoning profile (base 2000); no change to its model file or sampling.
- Replace the `qwen38`-specific B3 check for the old main-model stem and external draft with a **narrow, fail-closed** check for the approved new model identity and embedded-MTP configuration. For ordinary `qwen`, remove `--spec-type draft-mtp` (no drafter requested), preserve its exact Qwen3.6 model path/name, `--reasoning-budget-enable`, base budget 8192 and context 49152, and update its B3 branch to accept **only** the approved non-speculative profile. Neither B3 audit nor any other hard audit filter is disabled. Retain role-effective budget/context gates and update focused tests/docs for both contracts.

## Capabilities

### New Capabilities
- `local-model-profile-refresh`: a narrow follow-up to the still-unarchived `local-matrix-v2` change (whose `local-model-registry` capability is not yet in canonical `openspec/specs/`). It supersedes that change's original `gemma31`/`qwen38` exact-profile requirements and its ordinary `qwen` draft requirement. B3 eligibility becomes model-specific: `qwen38` embedded-MTP Q4_0; ordinary `qwen` Qwen3.6 without a drafter. Other reviewer/audit validation remains unchanged.

## Impact and approval boundary

High risk: model identity and server args are run-identity-bearing; B3 audit gating is quality-critical. Existing run directories made with an old `qwen38`, `gemma31` or ordinary `qwen` profile are **not** to be resumed with the changed profile. Registry/model-validation unit tests and offline preflight are allowed; no model server, pipeline, RT production-state edit or deployment is part of implementation. The owner's manual run established that the new Q4_0 starts with embedded MTP, **not** that it works with the preserved budget/effort flags; the combined command needs separate owner-side confirmation before a production run. This proposal requires owner approval before code/config implementation.
