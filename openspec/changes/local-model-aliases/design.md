## Context

Remote aliases are currently resolved from `configs/providers.yaml`; local runs use static `LocalLlamaBackendConfig` mappings for `gemma` and `qwen`. Sampling and output settings are scattered through `StrictRunConfig`, role-adapter defaults, B3 configuration, `selective_repair`, `entity_extractor`, `russian_editor`, `formatting`, and `glossary_resolver`. Several `CompletionRequest` producers hardcode `temperature=0.0`; generation has `0.2`; formatting has an independent `0.1` client configuration. Some output budgets are fixed, while others add a literal per-PID allowance and ceiling.

The owner requires every model-call body policy to come from providers, at every stage. This is a policy-source change, not permission to send unsupported fields. Local reasoning is already correctly expressed by `llama-server` arguments (`--reasoning-budget`), not body `reasoning`.

## Goals / Non-Goals

**Goals**

- `book|chapter --local` preserves current behavior through explicit local provider defaults; no model-call sampling/output literal remains a production policy fallback.
- `book|chapter --local <alias>` resolves an unambiguous alias through the unified registry and replaces only the declared local model key.
- Every V4 strict/book producer has one enumerated role and receives an immutable resolved `RoleCallPolicy`.
- Local and remote transports serialize exactly the body fields allowed for that transport; unknown/unsupported fields fail before a server is started or called.
- The resolved policy is auditable and identity-bearing at the producer/cache boundary.

**Non-Goals**

- No real local model is added and no server/pipeline is run.
- No per-role command-line knobs or new `runtime_localN.yaml` files.
- No change to prompt, retry/backoff, parsing, safety gates, or model lifecycle semantics. Retry policy is not a model request-body/server-start parameter and remains outside this change.

## Decisions

### 1. Registry schema and precedence

`configs/providers.yaml` remains the single registry. It gains a `local` provider and every provider used by strict/book declares `role_policies`; policy is never inferred from a code literal.

```yaml
providers:
  local:
    kind: local_llama
    role_policies:
      generator:
        model_key: gemma
        request: {temperature: 0.2, seed: 7, max_output_tokens: 70000}
      qwen_audit:
        model_key: qwen
        request: {temperature: 0.0, max_output_tokens: 12000}
        output_budget: {mode: floor_plus_per_item, per_item_tokens: <current value>, ceiling: <current value>}
      # every required role appears; see the matrix below
    models: {}                 # allowed only for local until a real alias exists
```

A local alias has `model_key`, `model_path`, `model_name`, `server_args`, optional `reasoning_budget`, and optional `role_policy_overrides` keyed by the same role names. An override is allowed only for a role whose resolved `model_key` equals the alias `model_key`; this prevents a Gemma alias from silently changing Qwen audit policy. Merge precedence is: required local provider policy → matching alias role override. `models: {}` is valid for the local provider so this plumbing change can add explicit default policies without adding an executable-host model entry; other providers retain their non-empty model-catalog requirement.

All role policies must be complete: omission of a required role, unknown role, unknown policy key, wrong type/range, duplicate normalized alias, or incompatible override fails closed during registry load. There is no production code fallback. Test-only direct constructors must pass a policy explicitly.

### 2. Required role/call-site matrix

The registry role namespace and implementation coverage are fixed by this matrix. The task implementation must update the cited producer and tests for each row; adding a new producer requires adding a role and registry default in the same change.

| Registry role | Current producer(s) | Routing binding | Policy fields / dynamic rule |
| --- | --- | --- | --- |
| `generator` | `BackendModelCaller`, whole-chapter generation | `generator` | request fields; fixed output budget |
| `fidelity_reviewer` | `BackendQwenEvaluator`, `BackendRegionFidelityGate` single and batch | `fidelity_reviewer` | request fields; batch headroom/ceiling from `output_budget` |
| `russian_selector` | `BackendGemmaSelector` | `russian_selector` | request fields |
| `qwen_audit` | `BackendQwenAuditEvaluator`, `ChunkedAuditEvaluator`, B3 audit and re-audit | `qwen_audit` | request fields; per-PID headroom/ceiling from policy |
| `gemma_audit` | `BackendGemmaAuditEvaluator` | `gemma_audit` | request fields |
| `repair` | `BackendRepairCaller`, `SelectiveRepair` and B3 repair | policy-declared binding (normally `repair`) | request fields; per-PID headroom/ceiling from policy |
| `entity_extractor` | `BackendEntityExtractor` / B3 entity prepass | policy-declared binding (normally `entity_extractor`) | request fields |
| `russian_editor` | `russian_editor` / B3 editor | policy-declared binding | request fields |
| `formatting` | `phase5/formatting.py` adapter/client | policy-declared binding | request fields; span-based output-budget formula from policy |
| `glossary_resolver` | `GlossaryResolver` | policy-declared binding | request fields; it does not introspect descriptor internals for hidden values |

All `request` maps use the same validated vocabulary: `temperature`, `top_p`, `top_k`, `min_p`, `seed`, and `max_output_tokens`. A role may omit a field only where the target transport's protocol documents it as not applicable; it may not obtain the omitted value from a literal. `output_budget` is explicit for dynamic calls and contains a mode plus all factors/ceilings necessary to derive the final `max_output_tokens` deterministically. Response schema and prompt contracts remain code-owned protocol, not sampling policy.

### 3. Transport split

`CompletionRequest` gets a typed, validated sampling map (or equivalent explicit fields) that includes `min_p`; `ALLOWED_REQUEST_OPTIONS` and payload serialization are updated accordingly.

- **Remote:** sampling body/options are serialized through the existing OpenCode mapping. `reasoning` continues to map through the remote reasoning contract.
- **Local:** `LocalOpenAIBackend`/`ApiClient` serializes the declared sampling body fields, including `seed`, `top_p`, `top_k`, and `min_p`, rather than rejecting all request options. `reasoning` remains rejected for local. The registry validates `reasoning_budget` equals the `--reasoning-budget` server argument, and preflight validates the paired `--reasoning` switch where required.

The implementation must add serialization tests for each local body field and a transport-contract test with a mock `llama-server` request. If the supported RT llama-server version rejects any named field, the change must not silently drop it: remove that field from the allowed local registry schema, retain its policy only as a documented server argument if available, and obtain owner approval for the changed capability.

### 4. CLI and alias resolution

`--local` becomes `nargs="?"`, with a bare sentinel matching `--remote`. Bare local uses the canonical runtime local transport plus `providers.local.role_policies`; `--local alias` applies the alias fragment. Local/remote/runtime-config remain mutually exclusive and simple mode cannot mix translator/reviewer overrides. Output labels are `local` and `local_<sanitized-alias>`.

### 5. Identity, artifacts, cache and preflight

`ResolvedRolePolicies` is a canonical, immutable mapping and receives a hash. It is included in `BackendDescriptor.public_record`, `StrictRunConfig.to_config_artifact`, preflight output, and the strict trial record. Each producer additionally includes its own `role_policy_hash` and final derived output budget in the cache/request identity that it already uses:

- generation: `PromptBundle.bundle_hash` and generation trial fields;
- audit/re-audit: audit-cache unit hash/envelope and audit trial fields;
- repair: repair-unit hash/cache envelope and repair trial fields;
- formatting and glossary resolver: their call/cache/sidecar provenance fields.

Changing an audit policy must not reuse old audit results; changing a repair policy must not reuse old repair results. A run-level identity change may invalidate broader resume reuse, which is safe; producer-level hashes are still required for correctness and auditability.

`--preflight` performs registry validation/resolution and reports sanitized alias, routing, `server_args`, complete resolved role policies, per-role hashes, and aggregate identity without starting a server or making a network call.

## Risks / Migration

A malformed policy could otherwise change translation behavior silently. Completeness, type/range validation, transport-specific field validation, and identity tests are therefore fail-closed. Implementation sequence: add registry types/defaults and tests; add resolved policy to runtime identity; wire all matrix producers and serialization; add CLI/preflight; then run strict OpenSpec validation and focused tests. No pipeline execution is part of this change.
