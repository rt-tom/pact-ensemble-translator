## Context

After `local-model-aliases` (PR #235) the registry is top-level `role_budgets` + `providers.local.models.<alias>.request` (5 fields) + `server_args`/`reasoning_budget`. Production has `gemma`/`qwen`. Two new locals are ready, remote formatting is model-based and good, owner wants a single readable matrix and hybrid reasoning (role + model) that composes rather than duplicates.

## Goals / Non-Goals

**Goals**
- One table that answers “what budget/reasoning/sampling applies to `russian_editor` when `qwen38` is reviewer?”.
- Hybrid reasoning: role delta + model base with an explicit role-effective local launch-profile policy.
- Add `gemma31`/`qwen38` verbatim from owner PowerShell args (minus host/port).
- Make sampling extensible for `repeat_penalty`/`presence_penalty` etc.
- Reuse remote formatting lifecycle for `book --local` without forking formatting logic.

**Non-Goals**
- Do not change prompts, retries, lifecycle batching, or pipeline ordering.
- Do not switch local reasoning transport from `server_args` to `request_options`.

## Decisions

### 1. Matrix (authoritative)

| role | group | max_output_tokens / output_budget | model examples (server reasoning base) | request sampling (model-owned) | hybrid reasoning effective* |
|---|---|---|---|---|---|
| `generator` | translator (`gemma*`) | 70000 | `gemma 2048`, `gemma31 2000` | `gemma:0.2/seed7`, `gemma31:1.0/0.95/64/0.0/1.0` | **4000** (`2000+2000` on gemma31) |
| `repair` | translator | 16384 floor+128*items ceil24576 | same as generator | same | **2000** (`2000+0` on gemma31) |
| `formatting` | reviewer (`qwen*`; shared local/remote fixed-role contract) | 8000 span 64 ceil24576 | `qwen 8192`, `qwen38 8192` | `qwen:0.0`, `qwen38:0.2/0.95/20/0.0/0.0` | **8192** (`8192+0` on qwen38) |
| `gemma_audit` | translator | 4096 | same | same | **2000** |
| `qwen_audit` | reviewer (`qwen*`) | 12000 floor+128*items ceil24576 | `qwen 8192`, `qwen38 8192 + xhigh` | `qwen:0.0`, `qwen38:0.2/0.95/20/0.0/0.0` | **10192** (`8192+2000` on qwen38) |
| `fidelity_reviewer` | reviewer | 16384 floor+128*items | same | same | **8192** (`8192+0`) |
| `russian_selector` | reviewer | 1024 | same | same | **8192** |
| `entity_extractor` | reviewer | 12000 | same | same | **10192** (`8192+2000`) |
| `russian_editor` | reviewer | 12000 | same | same | **8192** (`8192+0`) |
| `glossary_resolver` | reviewer | 4096 | same | same | **8192** |

*Hybrid: `effective = model.reasoning_budget + (role_budgets[role].reasoning_budget or 0)`. Per grill decisions 2026-09-09: `gemma31 base 2000`, `qwen38 base 8192`, deltas `entity_extractor +2000`, `generator +2000`, `qwen_audit +2000`, others `0`; `formatting` moves to `reviewer` in the shared local/remote role contract. **Implementation fact:** current `ModelRouter.ensure_resident(model_key)` launches only static `server_args[model_key]`; calls to the same resident model do not restart. The router SHALL restart/relaunch the same model when the next role's effective budget changes; it SHALL NOT use group maximum as a substitute. Fresh calls record `model_base`, `role_delta`, `effective`, and actual launch args in provenance. Effective-reasoning changes SHALL NOT alter run identity or invalidate/reject request cache or resume artifacts; a cache hit retains its original provenance rather than being rewritten to claim new launch args.

### 2. Registry shape

```yaml
role_budgets:
  generator: {max_output_tokens: 70000, reasoning_budget: 2000}
  repair: {max_output_tokens: 16384, output_budget: {...}, reasoning_budget: 0}
  formatting: {max_output_tokens: 8000, output_budget: {...}, reasoning_budget: 0} # reviewer group for local and remote
  gemma_audit: {max_output_tokens: 4096, reasoning_budget: 0}
  qwen_audit: {max_output_tokens: 12000, output_budget: {...}, reasoning_budget: 2000}
  fidelity_reviewer: {max_output_tokens: 16384, output_budget: {...}, reasoning_budget: 0}
  russian_selector: {max_output_tokens: 1024, reasoning_budget: 0}
  entity_extractor: {max_output_tokens: 12000, reasoning_budget: 2000}
  russian_editor: {max_output_tokens: 12000, reasoning_budget: 0}
  glossary_resolver: {max_output_tokens: 4096, reasoning_budget: 0}
providers:
  local:
    kind: local_llama
    models:
      gemma:   {model_key: gemma,  model_path: C:/llama-cpp/models/gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf, model_name: gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf, server_args: [-ngl, "99", -ncmoe, "18", --load-mode, mmap, --reasoning-budget, "2048", --reasoning-budget-enable, -np, "1", -c, "49152", -fa, "on", --jinja, -ctk, q8_0, -ctv, q4_0, --cache-ram, "0", --ctx-checkpoints, "0"], reasoning_budget: 2048,  request: {temperature: 0.2, seed: 7}}
      qwen:    {model_key: qwen,   model_path: C:/llama-cpp/models/Qwen3.6-35B-A3B-MTP/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf, model_name: Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf, server_args: [--spec-type, draft-mtp, --spec-draft-n-max, "3", --device, SYCL0, -fit, "on", -fitt, "1280", -b, "2048", -ub, "512", -ctk, q8_0, -ctv, q4_0, -t, "6", -tb, "12", --load-mode, mmap, --reasoning, "on", --reasoning-budget, "8192", --reasoning-budget-enable, -np, "1", -c, "49152", -fa, "on", --jinja, --cache-ram, "0", --ctx-checkpoints, "0"], reasoning_budget: 8192, request: {temperature: 0.0}}
      gemma31: {model_key: gemma31, model_path: C:/llama-cpp/models/Gemma4-31B-Q5/gemma-4-31B-it-UD-Q5_K_XL.gguf, model_name: gemma-4-31B-it-UD-Q5_K_XL.gguf, server_args: [-dev, SYCL0, -fit, "on", -fitt, "512", -b, "1024", -ub, "512", -ctk, q8_0, -ctv, q4_0, -t, "12", -tb, "12", -fa, "on", --load-mode, mmap, -c, "44000", -np, "1", --reasoning, "on", --reasoning-budget-enable, --reasoning-budget, "2000", --cache-ram, "0", --ctx-checkpoints, "0", --jinja], reasoning_budget: 2000, request: {temperature: 1.0, top_p: 0.95, top_k: 64, min_p: 0.0, repeat_penalty: 1.0}}
      qwen38:  {model_key: qwen38, model_path: C:/llama-cpp/models/Qwen3.8-27B/Qwen3.8-27B-UD-Q4_K_XL.gguf, model_name: Qwen3.8-27B-UD-Q4_K_XL.gguf, server_args: [-md, "C:/llama-cpp/models/Qwen3.8-27B/MTP/mtp-Qwen3.8-27B-Q4_0.gguf", --spec-type, draft-mtp, --spec-draft-n-max, "2", --spec-draft-ngl, "99", --spec-draft-device, SYCL0, -ngl, "99", -dev, SYCL0, --load-mode, mmap, -b, "2048", -ub, "1024", -ctk, q8_0, -ctv, q4_0, -t, "6", -tb, "12", -np, "1", -c, "44000", -fa, "on", --jinja, --cache-ram, "0", --ctx-checkpoints, "0", --reasoning, "on", --reasoning-budget-enable, --reasoning-effort, "xhigh", --reasoning-budget, "8192"], reasoning_budget: 8192, request: {temperature: 0.2, top_p: 0.95, top_k: 20, min_p: 0.0, presence_penalty: 0.0}}
```

`request` allowlist becomes `temperature/top_p/top_k/min_p/seed/repeat_penalty/repeat_last_n/frequency_penalty/presence_penalty`. Adding a new sampling param is one frozenset entry + one validator branch. `response_format: {type: json_object}` is not copied into models: it remains existing transport-level structured-output configuration.

### 3. Validation & operational policy

- `_validate_model_request` forbids `max_output_tokens`/`reasoning`/`reasoning_budget`; unknown sampling field fail-closed until allowlisted (explicit error lists allowed).
- `_validate_role_budgets` now accepts optional `reasoning_budget: int 0..8192` per role; type/range fail-closed; missing role still fail-closed.
- Effective reasoning is computed by `effective_reasoning_budget(role, model_alias)`. Before a role whose effective budget differs from the currently resident model's launch budget, the router SHALL restart/relaunch that same model with `--reasoning-budget` replaced by the role-effective value. It SHALL NOT promote a role budget to group maximum. Effective reasoning SHALL NOT enter run identity or request-cache/resume invalidation; a cache hit preserves prior provenance rather than being rewritten.

### 4. Local formatting

The shared role contract moves `formatting` to reviewer-group (`qwen*`) for local and remote. At `book --local` formatting (`terminal complete/accepted_degraded + inline_spans`), the strict chapter run has already released its local lifecycle, so start a separate per-chapter local `qwen*` formatting server using the reviewer model's resolved launch args; health-wait, call `resolve_format_mappings` with reviewer sampling, then close. On failure use deterministic `run_formatting_align`. This does not reuse an already-resident re-audit process; it reuses the reviewer **model/profile**. `chapter --local` strict path stays deterministic; no `--force-formatting-model` is introduced.

## Migration / Risks

`gemma31`/`qwen38` are additive; existing aliases retain their paths and sampling. `gemma`/`qwen` gain `--reasoning-budget-enable`, and all aliases use the universal role deltas. The dynamic effective reasoning value does not alter run identity or invalidate/reject request cache or resume artifacts; its only runtime effect is the local-server relaunch when the next role needs a different value.

