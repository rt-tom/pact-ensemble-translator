## Why

Local runs (`book --local`) still use a single static profile (`configs/runtime_local.example.yaml`) with fixed `model_paths/model_names/server_args` for `gemma`/`qwen` and a single `StrictRunConfig.temperature/seed/max_tokens` for the body. Remote runs already select models by alias (`--remote musefree/luna` → `providers.yaml` → `model_bindings` + `reasoning_contract`). The owner wants the same ergonomics for local: `book --local <alias>` (or `book --local` bare = current defaults) where the alias brings the required **server_args** (`--reasoning-budget`, `-c`, `-ngl`, `-ctk` …) and the **body params** (`temperature`, `top_p`/`top_k`/`min_p`/`seed`/`max_tokens`) per model. Adding a new `runtime_local2.yaml` per model is not scalable.

Reasoning for local is already via `server_args --reasoning-budget` (not `request_options`), so the body path must stay distinct for local vs remote.

## What Changes

- Extend the unified providers registry (`configs/providers.yaml`) to carry `local` models: for `kind: local_llama` an entry defines `model_path`/`model_name`/`server_args` (verbatim `Popen` args, identity-bearing) plus `generation` per role (`temperature`, `top_p`/`top_k`/`min_p`/`seed`/`max_tokens`, `reasoning_budget`) for **every** role (generation, entity_extractor, qwen_audit, russian_editor, selective_repair, formatting, glossary_resolver) — no hardcoded `0.0/0.2/0.1` remains in code, providers are the source of truth (code fallback only when registry gives nothing) (mirrors the existing `server_args` entry, not body `request_options`). No new registry file.
- Allow `book --local [alias]` (and `chapter --local [alias]`) analogous to `book --remote [alias]`: bare `--local` keeps canonical defaults, `alias` resolves through the registry (bare-alias global uniqueness as for remote). `--local` and `--remote` remain mutually exclusive, both mutually exclusive with `--runtime-config`. `--translator/--reviewer` remain advanced-only (not mixed with simple `--local/--remote` in v1; a future `TRANSLATOR/REVIEWER` split for local can be layered later if needed).
- When a local alias is given, the effective `LocalLlamaBackendConfig` is built from the alias's model fragment (overriding `model_paths/model_names/server_args` for the mapped keys, currently `gemma`/`qwen`), and the effective body params for **every** stage (`StrictRunConfig` → `GenerationParams` → `CompletionRequest.temperature`, plus audit/repair/Editor's `temperature`/`top_p`/`top_k`, `formatting` `0.1`, etc.) are taken from the registry's per-role `generation` block (fallback to current defaults only when registry gives nothing). For `remote` the existing body path (`CompletionRequest.temperature/request_options`) stays unchanged; for `local` the existing `LocalOpenAIBackend` guard that rejects `request_options` keeps reasoning via `server_args` only, but all body temps come from providers, not hardcoded `0.0/0.2`.
- Make the alias selection identity-bearing: alias, resolved `model_path`/`server_args`, and generation params enter `BackendDescriptor`/`StrictRunConfig.to_config_artifact` so caches/resume are invalidated (same as `--translator/--reviewer` for remote). `preflight` resolves the same alias and reports sanitized descriptor/policy/identity.
- Do **not** add any new model now — this change is plumbing only. Adding a model later is a `providers.yaml` edit + verification of its `server_args`/`generation` and preflight.

## Capabilities

### New Capabilities
- `local-model-aliases`: Alias-driven local model selection where the alias determines `LocalLlamaBackendConfig` server-args and **all** body params (generation + audit/repair/Editor/formatting), with bare `--local` default; hardcodes removed.

### Modified Capabilities
- `runtime-profile-contract`: Provider registry and simple-mode CLI now cover `local` aliases; local reasoning stays `server_args`-based.

## Impact

- `configs/providers.yaml` (additive `local` provider), `pact_v4/runtime/runtime_config.py` (registry parsing/validation for `local_llama`, alias resolution, `generation` mapping, identity), `pact_full_pipeline_runner_v1/v4_run.py` + `pact_full_pipeline_runner_v1/v4_phase12_strict_run.py` (CLI `--local` alias parsing, `_apply_overrides`, delegation, label `local` vs `local:<alias>` for output dir, help), `pact_v4/pipeline/v4_phase12_strict_runner.py` / `pact_v4/phase2/generation.py` (`StrictRunConfig`/`GenerationParams` optional per-model overrides, wiring to `CompletionRequest`), `pact_v4/runtime/backend_protocol.py` / `pact_v4/runtime/local_openai_backend.py` (keep `request_options` rejection for local), `docs/architecture/V4_BOOK_PIPELINE_INVENTORY_RU.md` + `docs/agent_operations/AGENTS_REFERENCE_RU.md` (command examples).
- High risk: touches runtime identity, CLI contract, and generation path. No pipeline execution, server start/stop, or persistent state migration in this change; adding a real model is a later `providers.yaml` edit with its own preflight check.
