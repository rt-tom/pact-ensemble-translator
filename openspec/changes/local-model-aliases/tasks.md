## 1. Registry contract and effective policy

- [ ] 1.1 Define immutable `RoleCallPolicy`, `OutputBudgetPolicy`, and `ResolvedRolePolicies` in `pact_v4/runtime/runtime_config.py`. Implement the complete role namespace: `generator`, `fidelity_reviewer`, `russian_selector`, `qwen_audit`, `gemma_audit`, `repair`, `entity_extractor`, `russian_editor`, `formatting`, `glossary_resolver`.
- [ ] 1.2 Extend `configs/providers.yaml` schema/loader for provider `role_policies` and `local: kind: local_llama`. Require complete local defaults that reproduce existing V4 strict/book behavior. Allow an empty local `models` map for this plumbing-only change; validate non-empty remote catalogs as today.
- [ ] 1.3 Add local alias validation/resolution: `model_key`, path/name, string-only `server_args`, reasoning-budget/server-arg agreement, global normalized alias uniqueness, and compatible role-keyed overrides only. Reject missing/unknown roles, unknown request keys, invalid value/range, incompatible overrides, and unsupported transport fields fail-closed.
- [ ] 1.4 Define canonical role-policy merge precedence and hashes. Include aggregate resolved policies in backend/config identity and preserve the per-role hash plus final derived output budget for each producing artifact/cache.

## 2. Request transport and all producer wiring

- [ ] 2.1 Extend `CompletionRequest`/`ApiClient`/`LocalOpenAIBackend` to carry and serialize typed local sampling fields: `temperature`, `top_p`, `top_k`, `min_p`, `seed`, and `max_output_tokens`; add `min_p` validation. Keep local `reasoning` request options rejected and local reasoning solely in `server_args --reasoning-budget`.
- [ ] 2.2 Replace all V4 strict/book code-owned sampling/output policy literals with `RoleCallPolicy`. Cover every matrix row: generator/whole-chapter; Qwen fidelity and region re-gate (single+batch); Gemma selector; Qwen/Gemma audit and B3 re-audit; repair/selective-repair/B3 repair; entity extractor; Russian editor; formatting; glossary resolver. Remove descriptor-private-field introspection in glossary resolution.
- [ ] 2.3 Move all dynamic request-budget constants used by those producers (base/floor, per-PID/per-span allowance, ceiling) into `OutputBudgetPolicy`, implement one deterministic derivation helper, and record its final result with the call identity.
- [ ] 2.4 Wire per-role policy through `StrictRunConfig`, B3 construction, role adapters, and formatting client without changing prompt, parser, retry/backoff, hard-filter, or lifecycle behavior.

## 3. CLI, preflight, identity, and provenance

- [ ] 3.1 Update `v4_run.py` and `v4_phase12_strict_run.py`: `--local` accepts optional alias, is mutually exclusive with remote/runtime config, delegates unchanged through book/chapter paths, and uses `local` / `local_<alias>` output labels.
- [ ] 3.2 Resolve exactly the same local alias/policies in `run_runtime_preflight`; validate paths, server args/reasoning agreement, request-field transport support, and emit sanitized routing, policy map, per-role hashes, aggregate identity, and no network/server side effect.
- [ ] 3.3 Update generation, audit/re-audit, repair, formatting, glossary resolver, and strict trial records/cache envelopes to preserve their role-policy hash and effective final output budget. Bump only the schemas/versions necessary to prevent replay under a different policy, with backward compatibility handled fail-closed.

## 4. Tests, documentation, and verification

- [ ] 4.1 Add registry tests: complete defaults, empty local model catalog, alias resolution (bare/qualified), global collision, every malformed policy/override, and local server-args/reasoning consistency.
- [ ] 4.2 Add transport tests with captured mock requests proving local and remote serialization/identity for every allowed field, including `seed` and `min_p`, and rejection/no-silent-drop of unsupported local fields.
- [ ] 4.3 Add one focused producer test per matrix row. Mutate that role's temperature, seed where applicable, and dynamic budget inputs; prove the precise request body, policy hash, final budget, artifact/cache identity, and no stale reuse. Retain bare-local regression tests proving behavior is preserved from explicit registry defaults.
- [ ] 4.4 Update `V4_BOOK_PIPELINE_INVENTORY_RU.md`, `AGENTS_REFERENCE_RU.md`, help, and `providers.yaml` comments with the role matrix, alias syntax, local-vs-remote reasoning transport, policy identity, and new-model preflight procedure.
- [ ] 4.5 Run `openspec validate local-model-aliases --strict`, `pact-fidelity-lint`, focused runtime/adapter/strict/book tests, and `git diff --check`; no server lifecycle action or pipeline execution.
