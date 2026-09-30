## Why

The local Qwen3.8 entity attempt exhausted its 12,000-token response cap while reasoning consumed much of that allowance. The entity cap was raised to 20,000 in the preceding commit, but the same mismatch exists across the other roles. The owner approved adding each role's reasoning allowance to its existing output allowance for both local and remote paths.

## What Changes

The shared ten-role budgets in `configs/providers.yaml` gain the maximum effective local reasoning allowance for the role. This retains at least the prior content allowance for every current local alias. Because remote calls use the same role budgets, their caps rise too. Dynamic budgets add the allowance to both floor/base and ceiling while retaining the per-item/per-span slope. Model reasoning settings, server args, routes, and lifecycle are unchanged.

## Impact

High runtime-policy risk: larger requested outputs can increase time and token use and can exceed a model's remaining context window. For example, the earlier B1.2 prompt was approximately 19,385 tokens; its new 30,192-token response allowance totals more than the 44,000-token Qwen3.8 context setting. The configured cap is a maximum, not a guarantee that a complete output will fit. No production run or deployment is part of this change.
