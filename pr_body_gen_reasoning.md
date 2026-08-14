# GEN-REASONING: persist whole-chapter generation reasoning to disk

**Risk:** LOW (diagnostic artifacts only; no prompt/cache/identity/reasoning-effort changes).

## Cause
Whole-chapter generation (phase2b) did not persist model reasoning anywhere (run_remote_002: no *_reasoning* files; usage.ndjson kept only a token counter). Needed for translation-quality diagnostics.

## Fix
- Transport: BackendModelCaller captures reasoning per completion (raw_metadata["reasoning"]) into last_reasoning; forwarded via HttpModelCaller/LifecycleModelCaller.
- generate_whole_chapter gains optional reasoning_sink(attempt_index, text) called after EVERY attempt (success, truncated retry, abort); sink exceptions are swallowed (observability only).
- Runner persists whole_chapter_reasoning.txt (attempt 0), whole_chapter_retryN_reasoning.txt (retry attempts), compact reasoning marker in generation_outcomes.json (schema pact-v4-whole-chapter-reasoning/v1: per-attempt present+chars; full text only in .txt).
- strict_chapter_trial_record.json advertises the reasoning artifact only when actually created.
- NON-GOALS respected: reasoning NOT in identity/cache; whole_chapter_pid_map/wc_validated untouched; byte-identical record when reasoning=0; prompts and reasoning effort unchanged (remote --reasoning 3 is a run-command decision, not code).

## Review history
- RV t_a790dbab: CHANGES REQUESTED — HIGH stale-reasoning on lifecycle acquisition failure (LifecycleModelCaller must reset state before ensure_resident).
- Fix commit b92dc013: clear attempt state before lifecycle acquisition; regression tests at lifecycle and sink levels.
- RV2 t_c9990d3d: APPROVE — full diff reviewed, targeted 155 passed, full suite 1752 passed / 9 skipped.

## Files
10 files, +648/-2: pact_v4/phase2/generation.py, pact_v4/pipeline/v4_phase12_strict_runner.py, pact_v4/runtime/backend_role_adapters.py, pact_v4/runtime/model_caller.py, pact_v4/runtime/model_lifecycle_adapters.py + 5 test files.

## Tests
Full suite: 1752 passed, 9 skipped (pre-existing, external artifacts). Targeted: 155 passed.
