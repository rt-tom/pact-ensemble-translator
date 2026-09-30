## Approach

Keep the existing shared role-budget data model. For each role, compute the offset from current local aliases as `max(model.reasoning_budget + role.reasoning_budget)` within that role's fixed translator or reviewer group. Add the offset to `max_output_tokens`; for dynamic policies, also add it to `floor_tokens` or `base_tokens` and `ceiling`. Preserve `per_item_tokens` and `per_span_tokens`.

The selected offsets are: generator 4,048; repair and gemma_audit 2,048; qwen_audit and entity_extractor 10,192; all other reviewer roles 8,192. The 20,000 entity content cap comes from the preceding branch commit. Remote reasoning effort is categorical, so no exact remote reasoning token allowance can be established from this registry; the shared raised caps are applied uniformly.

The role budget hash and local aggregate identity include output caps, so new configurations have distinct identity and cache keys by the existing contract. No change is needed to the loader or request adapter.
