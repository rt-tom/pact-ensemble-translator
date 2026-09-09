## Why

`book --local` uses a fixed local runtime profile, while remote models can be selected by alias from `configs/providers.yaml`. The owner needs `book --local [alias]` without creating `runtime_localN.yaml`/`localN` flags. An alias must select model path/name and exact `llama-server` arguments.

In addition, no model-call body setting may be an implicit code policy. At every V4 strict/book pipeline stage, the effective sampling/output policy (for example `temperature`, `top_p`, `top_k`, `min_p`, `seed`, and `max_output_tokens`) must come from the selected provider registry policy. This includes generation, all audits and re-gates, repair, entity extraction, Russian editing, formatting, and glossary resolution. Local reasoning remains an explicit server-start policy (`--reasoning-budget`), not a request body option.

## What Changes

- Extend the unified `configs/providers.yaml` registry with a `local` provider (`kind: local_llama`). It supplies two independent, validated concerns:
  - `role_policies`: canonical policy for every V4 strict/book model-call role, including model-key routing and every request-body setting; these registry defaults preserve the current production behavior for bare `--local`.
  - `models.<alias>`: an optional local model replacement (`model_key`, `model_path`, `model_name`, `server_args`, and per-role policy overrides). No real model is added by this change; test fixtures prove the contract.
- Add `book --local [alias]` and `chapter --local [alias]`, analogous to remote aliases. Bare `--local` resolves the canonical local runtime plus the registry's local role policies. `--local alias` replaces only the declared `model_key` and applies only its compatible per-role policy overrides. It never silently changes unrelated roles.
- Replace all V4 strict/book literal request-body sampling/output settings with a resolved immutable `RoleCallPolicy`. Each `CompletionRequest` producer receives its role's policy explicitly. Dynamic output-budget calculations are expressed by registry policy fields (base, per-item headroom, and ceiling), not literals in call sites.
- Extend the local OpenAI-compatible request serialization to send declared sampling fields (`temperature`, `top_p`, `top_k`, `min_p`, `seed`, `max_output_tokens`) in the request body. The existing prohibition is narrowed to the remote-only `reasoning` request option: local reasoning remains solely in alias `server_args` and is validated against `reasoning_budget`.
- Make the fully resolved role-policy map, local alias/server arguments, and effective routing identity-bearing. Each cache/artifact records the policy hash relevant to the call that produced it; changing a role policy invalidates that role's cache/resume data and appears in preflight/trial provenance.

## Capabilities

### New Capabilities
- `local-model-aliases`: scalable local alias selection with server-start configuration and per-role model-call policies from `providers.yaml`.

### Modified Capabilities
- `runtime-profile-contract`: simple local and remote execution consume explicit provider policy rather than code sampling defaults.

## Impact

- `configs/providers.yaml`; provider-registry parsing and effective-policy resolution in `pact_v4/runtime/runtime_config.py`; the local request adapter/client and `CompletionRequest` sampling contract; strict/book CLI/preflight/identity plumbing; every V4 strict/book `CompletionRequest` producer and formatting adapter.
- High risk: runtime contract, cache/resume identity, and all model-call paths change. The change adds no real model entry, starts no server, runs no pipeline, and does not migrate or delete persistent state.
