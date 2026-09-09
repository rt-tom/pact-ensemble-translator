## Context

`--remote` already has alias ergonomics: `book --remote musefree/luna` resolves via `configs/providers.yaml` (provider/model `ref` + `reasoning_contract.variants` → `model_bindings` + `reasoning_effort_map`) and via `_apply_overrides` → `BackendDescriptor` identity. `book --local` is static: `configs/runtime_local.example.yaml` (`LocalLlamaBackendConfig: exe/device/host/port + model_paths/model_names/server_args` for fixed keys `gemma`/`qwen`) + `StrictRunConfig.temperature/seed/max_tokens` (0.2/7/70000) → `GenerationParams` → `CompletionRequest.temperature/max_output_tokens`. Today **every** stage hardcodes its body temperature in code: generation `0.2` (`StrictRunConfig`), audit/repair/russian_editor/entity_extractor `0.0` (`backend_role_adapters`/`chunked_audit`/`entity_extractor`/`selective_repair`), formatting `0.1` (`phase5/formatting.py`). Local reasoning is deliberately **not** in `request_options`: `LocalOpenAIBackend` rejects `request_options`, `server_args --reasoning-budget` is the wire (see `runtime_config.py:830`), validated by `_local_generator_server_args`/`_reasoning_budget_from_server_args` and `validate_reasoning_backend`.

The owner wants `book --local [alias]` like remote, where the alias brings model-specific **server_args** (`-c`/`--reasoning-budget`/`-ngl`/`-ctk`…) and **body** (`temperature/min_p/top_p/top_k/seed/max_tokens`) without a new `runtime_localN.yaml` per model. This change is alias plumbing only; no new model is added.

## Goals / Non-Goals

**Goals:**
- `book --local` bare keeps today's defaults byte-identically (no behavior change, no cache break).
- `book --local <alias>` (and `chapter --local <alias>` transport-neutrally) resolves a `local` alias through the unified `providers.yaml` and produces the effective `LocalLlamaBackendConfig` + generation body from that alias.
- Server-args (`--reasoning-budget` etc.) stay `server_args`-based for local; body params stay `CompletionRequest.temperature` + `ALLOWED_REQUEST_OPTIONS` (`top_p/top_k/seed/reasoning`) — but local keeps `request_options` empty (reasoning not via body) to preserve the existing guard.
- Alias choice is identity-bearing (`BackendDescriptor` + `StrictRunConfig.to_config_artifact`) and visible in `preflight` and `strict_chapter_trial_record.json`.
- Keep `remote` path untouched.

**Non-Goals:**
- No new model entry, no new `runtime_local*.yaml`, no `local2` flag in this change.
- No `TRANSLATOR/REVIEWER` split for local in v1 (single alias selects the local backend fragment; a later split can layer `local:translator/reviewer` if needed).
- No migration, no pipeline run, no server lifecycle change.

## Decisions

### 1. Unified registry, local block shape

Reuse `configs/providers.yaml` (owner-approved unified registry). Add top-level provider `local: kind: local_llama, models: { alias: { model_key: gemma|qwen, model_path, model_name, server_args: [..], generation: { temperature, top_p, top_k, min_p, seed, max_tokens }, reasoning_budget } }`. Bare-alias global uniqueness applies across all providers (existing `_build_global_alias_index` now includes `local` aliases; duplicate normalized alias → fail-closed at registry load, provider-qualified `local/alias` remains supported).

Alternative separate `local_models.yaml` rejected — second registry duplicates validation/identity/preflight and drifts from the remote contract. `runtime_localN.yaml` per model rejected — N files/N flags not scalable.

### 2. Body vs server-args split per transport

*Body for every role:* All hardcoded `temperature`/`top_p`/`top_k`/`min_p`/`seed`/`max_tokens` (generation `0.2`, audit/repair/Editor `0.0`, formatting `0.1`) are removed as hardcoded defaults. When a local (and later remote) alias is selected, per-role `generation` blocks from the registry override the body params for **each** stage: `pact_v4/phase2/generation.py:GenerationParams` for generation, `pact_v4/audit/chunked_audit.py`/`entity_extractor.py`, `pact_v4/audit/russian_editor.py`, `pact_v4/repair/selective_repair.py`, `pact_v4/phase5/formatting.py`, `pact_v4/pipeline/glossary_resolver.py` for other roles. Without an alias the current values remain as registry-provided defaults (so bare `--local` is byte-identical). Each alias's `generation` is validated (`temperature` float, `top_p` in (0,1], etc.) and enters `to_config_artifact`/`BackendDescriptor` so a `0.0→0.7` change invalidates the correct cache (generation vs audit vs repair). Implementation: extend `GenerationParams` and the role-adapter call sites to take `temperature` from `StrictRunConfig`/provider per role instead of literal `0.0`/`0.2`; `top_p/top_k/min_p` travel as `CompletionRequest.request_options` for `remote` but for `local` the existing `LocalOpenAIBackend` guard must keep `request_options` empty — therefore for `local` in v1 only `temperature/seed/max_tokens` are body-overridable and `top_p/top_k/min_p` are either rejected or mapped to `server_args` in a follow-up after verifying the local binary's CLI flags. This still removes **all** hardcodes as source of truth (registry → code), while keeping the local transport guard.

*Reasoning:* For `local` keep the existing `server_args --reasoning-budget` path (identity-bearing, validated by `_reasoning_budget_from_server_args` + `validate_reasoning_backend`). The registry's `reasoning_budget` for local is a convenience that must equal the `server_args` entry; mismatch → fail-closed at load. For `remote` keep `request_options reasoningEffort` via `reasoning_effort_map`. Never send `request_options` on the local path.

### 3. CLI contract

`book`/`chapter` parsers: `--local` changes from `store_true` to `nargs="?" const="__DEFAULT__"` mirroring `--remote` (bare → `__DEFAULT__` → keep defaults). Parsing: `book --local` → keep canonical `runtime_local.example.yaml`; `book --local mygemma` → resolve `mygemma` via registry (bare or `local/mygemma`). `--local` and `--remote` stay mutually exclusive, both mutually exclusive with `--runtime-config`/`--profile`. The current guard `if is_simple_local and (translator/reviewer): error` stays (local alias suffices for v1). Output label: `local` for bare, `local_<alias>` sanitized for alias (used in auto `book_XXXX-YYYY_local_<alias>_<ts>` dir; `local` stays `book_..._local_...`). Help text updated to mirror remote alias form.

Alternative ` --local2` flag rejected — not generic.

### 4. Wiring and identity

`_apply_overrides(cfg, local_alias, ...)` for `LocalLlamaBackendConfig`: replace `model_paths[model_key]`, `model_names[model_key]`, `server_args[model_key]` from the alias; for `CompositeBackendConfig` with a local sub-backend replace that sub-backend's fragment similarly. `StrictRunConfig` is re-wrapped with overridden `temperature/seed/max_tokens` when alias provides `generation`. Both enter `to_config_artifact`/`BackendDescriptor.public_record()` so an alias change → new `bundle_hash`/`config_identity` → cache/resume invalidated. `preflight` resolves the same alias and prints sanitized `server_args`/`generation`/`reasoning_budget` + identity hash. Delegation (`_delegate_*` helpers in `v4_run.py`/`v4_phase12_strict_run.py`) forwards `--local alias` to strict.

### 5. Validation and preflight

Registry load: validate `local` model entry shape (`model_key` in `SUPPORTED_LOCAL_MODEL_KEYS`, `model_path` non-empty, `model_name` non-empty, `server_args` list-of-strings, `generation` fields typed, `reasoning_budget` integer and equal to `server_args --reasoning-budget` when both present). Alias global uniqueness as above. Runtime load: fail-closed on unknown/duplicate alias. Preflight: existing `run_runtime_preflight` already checks `exe`/`model_paths` existence and port; add alias-resolved `LocalLlamaBackendConfig` to the same checks + `validate_reasoning_backend` (`--reasoning-budget` agrees with `--reasoning`).

## Risks / Trade-offs

- [Alias shadows remote bare alias] → Global alias index already rejects duplicate normalized aliases; bare `luna` that exists in both `openai` and `local` → fail-closed, require `local/luna` vs `openai/luna`.
- [User expects `top_p` for local body but local keeps `request_options` empty] → v1 documents that only `temperature/seed/max_tokens` are overridable for local; `top_p/top_k/min_p` are v2 after verifying server CLI. Fail-closed on alias that sets them for local.
- [Output dir label `local_<alias>` breaks tooling that expects exactly `local`] → Auto-naming keeps `local` as prefix (`local_<alias>`), `remote` unchanged; explicit `--out-base/--out-dir` overrides still work.
- [Cache blow-up: per-alias generation change invalidates all old generation caches] → By design (same as `--translator` for remote); documented to use new `--out-base`.

## Migration Plan

1. Extend `providers.yaml` schema/tests for `local` kind (no real model entry yet — use fixture aliases).
2. Extend `runtime_config.py` registry + `StrictRunConfig`/`GenerationParams` alias overrides + identity.
3. Update `v4_run.py` + `v4_phase12_strict_run.py` CLI/parse/delegate/label/help and `preflight` reporting.
4. Update inventory/docs and help text; add alias-parsing/preflight/identity tests.
5. `openspec validate --strict` + independent review; no pipeline run, no real model addition (next change adds a model entry).
