## Scope and source of truth

A small, owner-approved follow-up to `local-matrix-v2`, not a replacement of the shared matrix. Owner-provided RT command lines determine the new model/throughput settings; existing Pact runtime contracts determine preserved reasoning flags, sampling and identity. `llama-server -dev` and `--device` denote the same device option: `LocalModelLifecycleAdapter._start_once` already injects `--device SYCL0`, `--host 127.0.0.1`, `--port 8094` and `-m <model_path>`; do not repeat these in `server_args`. No RT checkout edits are part of this change.

## 1. Target registry profiles

### `gemma31` — current QAT-MTP profile (authoritative)

`model_key: gemma31` remains (no `--alias` added). `model_path`/`model_name` become `C:/llama-cpp/models/Gemma4-31B-QAT-Q4/gemma-4-31B-it-qat-UD-Q4_K_XL.gguf` and its matching basename. Keep reasoning base `2000` and all existing role-budget deltas/sampling (normative retained set in the spec); apply only: `-fitt 1280`; `-b/-ub 2048`; `-ctv q8_0`; `-t 6`. Add the external MTP draft block first:

```text
--spec-type draft-mtp --spec-draft-model C:/llama-cpp/models/Gemma4-31B-QAT-Q4/MTP/mtp-gemma-4-31B-it.gguf
--spec-draft-n-max 3 --spec-draft-device SYCL0 --spec-draft-ngl all
-fit on -fitt 1280 -b 2048 -ub 2048 -ctk q8_0 -ctv q8_0 -t 6 -tb 12
-fa on --load-mode mmap -c 44000 -np 1 --reasoning on
--reasoning-budget-enable --reasoning-budget 2000
--cache-ram 0 --ctx-checkpoints 0 --jinja
```

There is no `-m`, main `--device`, host, port, or `--alias` in `server_args`: the lifecycle adapter injects model/device/host/port; only draft-specific options belong here. `--spec-draft-device SYCL0` is a draft-offload selector, not a duplicate of the injected main `--device`. The new Gemma31 external draft is translator-side and outside the reviewer-only B3 gate, which is unchanged. Qwen38 and ordinary-`qwen` profiles in §1 below are unchanged.

`qwen38`: `model_path` and `model_name` become `C:/llama-cpp/models/Qwen3.8-27B/Qwen3.8-27B-Q4_0.gguf` and `Qwen3.8-27B-Q4_0.gguf`. Keep the current model-owned request sampling (`temperature 0.2`, `top_p 0.95`, `top_k 20`, `min_p 0.0`, `presence_penalty 0.0`). Desired `server_args` (ordered; the three reasoning-budget/effort flags are retained from Pact, not claimed to have been tested in the owner's manual command):

```text
--spec-type draft-mtp --spec-draft-n-max 2 --spec-draft-p-min 0.5
-ngl 99 -c 44000 -b 2048 -ub 1024 -ctk q8_0 -ctv q4_0 -t 6 -tb 12
--load-mode mmap --reasoning on --no-reasoning-preserve
--reasoning-budget-enable --reasoning-effort xhigh --reasoning-budget 8192
-np 1 -fa on --jinja --cache-ram 0 --ctx-checkpoints 0
```

There is no `-md`, `--spec-draft-ngl`, `--spec-draft-device`, `-dev`, or `--device` in the `qwen38` `server_args` (addendum B gives `gemma31` its own external-draft flags; see above). Owner-approved addendum: in the ordinary `qwen` Qwen3.6 profile (`C:/llama-cpp/models/Qwen3.6-35B-A3B-MTP/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf`; registry, `configs/runtime_local.example.yaml`, and `QWEN_SERVER_ARGS`) remove **only** `--spec-type draft-mtp`. Its main model file/path, `--reasoning-budget-enable`, other ordered args, role budgets and request sampling stay as before. Absence of a draft selector uses llama.cpp's documented default `none`; if the RT fork or environment can override it, verify on RT before any owner-approved run rather than claiming that static args prove drafting is off. No registry schema, lifecycle adapter, role budgets, request body, provider endpoints or aliases change. Preserve `gemma31` base 2000 / generator delta +2000 and `qwen38` base 8192 / qwen_audit and entity_extractor delta +2000; the router still replaces the server budget and relaunches when effective reasoning changes.

## 2. B3 gate and evidence

Update only the `qwen38` branch of `_validate_b3_qwen_profile` in `pact_full_pipeline_runner_v1/v4_phase12_strict_run.py`: replace the exact old Q4_K_XL stem + external `-md` requirement with the exact approved Q4_0 model identity, matching model name, embedded `draft-mtp` config (`--spec-draft-n-max 2`, `--spec-draft-p-min 0.5`, no external draft), context `>= 44000`, and role-effective budget `>= 8192`. Fail closed for absent/duplicate/malformed capability flags, unapproved main-model path, noncanonical path spellings that escape the approved model directory, lookalikes and external-draft combinations. Resolve identity from the **selected reviewer**, never the legacy `qwen` entry. Separately update the ordinary `qwen` B3 branch to require the unchanged exact approved Qwen3.6 model path/name, no `--spec-type draft-mtp`/other draft selector, no `-md` or speculative draft flags, role-effective reasoning >= 8192, and context >= 49152. Reject foreign or lookalike model identities, malformed/duplicate draft flags and lowered limits; do not bypass B3 audit or hard filters. The `qwen38` branch still requires embedded MTP. Update `test_qwen36_server_profile.py` and qwen B3 negatives as well as the qwen38 tests.

The approved RT path and flags are configuration evidence; offline preflight cannot prove the GGUF has embedded MTP. The owner reported a manual launch of the Q4_0 MTP profile; the added Pact reasoning flags combined with `--no-reasoning-preserve` still require a separate owner-side server compatibility check before any production pipeline run. If incompatible, return for a new decision rather than silently dropping budgets or relaxing B3.

## 3. Verification and rollout boundary

- Update focused registry/role-effective/B3 positive and negative tests, including exact profile shape and no doubled device; check change in run identity and old-profile resume rejection using temporary test artifacts only. Run offline preflight and static checks; tests must not launch servers or run the pipeline.
- Keep old outputs intact; profile changes are identity-bearing. For owner-approved future manual RT testing, use a **new, explicitly agreed test output and state location**, not existing production results; model and state paths must be verified on RT first. Deployment and pipeline execution are separate owner decisions.
- In the `local-matrix-v2` spec/design, keep historical original profile text as the earlier contract; this change supersedes the two alias profiles, the ordinary qwen draft requirement and both corresponding B3 eligibility contracts from that point onward. Do not rewrite history or silently reinterpret its old tests.
