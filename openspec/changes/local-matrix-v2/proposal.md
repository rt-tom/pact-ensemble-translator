## Why

Current `main` (PR #235) is model-centric: sampling on model, budgets on role, two fixed groups. Three gaps remain: (1) local `formatting` is deterministic `0` calls while `book --remote` uses a per-chapter formatting model (PR #224) and shows good quality; (2) two new production local models are needed (`gemma31` 31B Q5, `qwen38` 27B+mtp) with their exact `llama-server` args and sampling; (3) `reasoning_budget` is only on model, but role output limits and model thinking appetite must be co-designed — `qwen38` with `2048` is squeezed on reviewer roles.

Owner also wants `--reasoning-budget-enable` on every local model and an extensible sampling allowlist (`repeat_penalty`, `presence_penalty` etc.) instead of a hard `5`-field limit.

## What Changes

- **Unified role/model matrix (single source of truth):** one table lists all 10 roles vs group vs `max_output_tokens`/`output_budget` vs model `server_args`/`reasoning_budget`/`request` sampling plus hybrid effective reasoning.
- **Hybrid reasoning (local only):** `effective_reasoning(role, model) = model.reasoning_budget + (role_budgets[role].reasoning_budget or 0)`: `gemma31 2000` + `generator 2000 = 4000`, `qwen38 8192` + `entity_extractor/qwen_audit 2000 = 10192`, others `0`. A local server has static startup args, so the router SHALL restart/relaunch the same model when the next role requires a different effective budget; current `ModelRouter` only keys residency by model and cannot do that yet. `formatting` moves to reviewer (`qwen*`) in the shared local/remote fixed-role contract. Remote `reasoningEffort` remains categorical and is out of scope.
- **Model sampling extensibility:** `providers.local.models.<alias>.request` allowlist expands from `temperature/top_p/top_k/min_p/seed` to also `repeat_penalty/repeat_last_n/frequency_penalty/presence_penalty` (and future additions via one frozenset entry). `max_output_tokens`/`reasoning` remain forbidden in `request`. Validation is type/range fail-closed; unknown sampling key also fail-closed until explicitly allowlisted.
- **Local model additions:** add `gemma31` (31B Q5, `-dev SYCL0`, `2000` reasoning, `temperature 1.0/top_p 0.95/top_k 64/min_p 0.0/repeat_penalty 1.0`) and `qwen38` (27B Q4 + `mtp` draft, `8192` reasoning + `--reasoning-effort "xhigh"`, `temperature 0.2/top_p 0.95/top_k 20/min_p 0.0/presence_penalty 0.0`). Both add `--reasoning-budget-enable` and keep `-dev` style. Existing `gemma`/`qwen` also gain `--reasoning-budget-enable`.
- **Formatting role and local model formatting:** `formatting` moves to reviewer (`qwen*`) in the shared fixed-role contract, including remote formatting. `book --local` uses a separate per-chapter reviewer-model formatting flow (`qwen38 8192`, sampling `0.2/0.95/20/presence 0.0`) — same `resolve_format_mappings` path, health-wait and deterministic fallback on failure. Local lifecycle logs use their normal local-server naming, not `opencode_serve_fmt`. The direct `chapter --local` strict path remains deterministic; no new `--force-formatting-model` CLI is added.

## Capabilities

### New Capabilities
- `local-matrix-v2`: hybrid reasoning, extensible sampling, two new local models, local formatting model path.

### Modified Capabilities
- `local-model-registry`: request allowlist and reasoning_budget hybrid.
- `book-production-formatting`: local reuses remote per-chapter server lifecycle.

## Impact

- `configs/providers.yaml`, `pact_v4/runtime/runtime_config.py` (validation + role-effective local launch profile), `pact_full_pipeline_runner_v1/v4_book_run.py` (local formatting lifecycle), `pact_v4/phase5/formatting.py`, docs.
- **High risk:** lifecycle/server args, role routing, and book formatting path change. Dynamic effective reasoning intentionally leaves identity/cache/resume reuse unchanged. No pipeline execution in this change beyond preflight.

