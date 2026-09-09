## 1. Spec & registry

- [ ] 1.1 Extend `ALLOWED_MODEL_REQUEST_FIELDS` + `_validate_model_request` with `repeat_penalty/repeat_last_n/frequency_penalty/presence_penalty` (type/range, forbid `max_output_tokens`/`reasoning_budget` still).
- [ ] 1.2 Extend `role_budgets` schema with optional `reasoning_budget: int 0..8192`; validate, fail-closed on bad type/range.
- [ ] 1.3 Implement `effective_reasoning_budget(role, model_alias) = model.reasoning_budget + (role_budgets[role].reasoning_budget or 0)`. Restart/relaunch the same local model when the next role requires a different effective budget; replace (do not append) `--reasoning-budget`. Record actual args for fresh calls. Do not add effective reasoning to run identity or invalidate/reject request cache or resume artifacts; cache hits retain their existing provenance.
- [ ] 1.4 Add `--reasoning-budget-enable` to `gemma`/`qwen` `server_args`; verify `reasoning_budget` agreement; `xhigh` quoted as `"xhigh"`.
- [ ] 1.5 Add `gemma31` and `qwen38` as specified in design §2 (exact PowerShell args minus host/port, `-dev` preserved, `-md` draft for qwen38).

## 2. Local formatting (model)

- [ ] 2.1 Move `formatting` to reviewer in the shared local/remote fixed-role contract. Add a separate per-chapter local reviewer-model formatting lifecycle for `book --local`: strict run releases its local lifecycle first; start the reviewer model's resolved launch args, health-wait, `resolve_format_mappings`, close, fallback deterministic. Do not claim it reuses an already-resident re-audit process.
- [ ] 2.2 Keep direct `chapter --local` strict deterministic; add no `--force-formatting-model` CLI.

## 3. Matrix & provenance

- [ ] 3.1 Render one authoritative matrix table (role × group × max_output_tokens/output_budget × model base × request sampling × effective reasoning) in docs and `strict_chapter_trial_record.json` provenance.

## 4. Tests, docs, verification

- [ ] 4.1 Tests: allowlist includes 4 new sampling fields, forbid unknown still; same-model role-effective restart with replacement `--reasoning-budget`; effective reasoning leaves run identity and existing request-cache/resume artifacts untouched; `gemma31`/`qwen38` registry shape + alias pair `gemma31/qwen38`; shared reviewer formatting role and separate local reviewer formatting lifecycle with deterministic fallback.
- [ ] 4.2 Docs/help: update local pair syntax, matrix, hybrid reasoning, sampling extensibility.
- [ ] 4.3 Run `openspec validate local-matrix-v2 --strict`, focused runtime/adapter/strict/book tests, `git diff --check`.

