# PR #5 Review Packet — v3.1.2h compact cross-verification retries

## Risk classification

**REVIEW REQUIRED.** The change is limited to output-format guidance, but it
changes the prompt text supplied to the cross-verification model on initial and
retry requests.

## Root cause

The active parser correctly rejects a `reason` longer than 800 characters and
correctly rejects every response with `finish_reason=length`. The retry loop,
however, only describes the immediately preceding failure. After this sequence:

1. schema validation rejects `reason > 800`;
2. the next response ends with `finish_reason=length`;
3. the final retry receives the length instruction but no longer receives the
   cross-verification field limits.

The initial prompt asks for 2–3 short sentences but did not state the enforced
character limits. This allowed all three strict attempts to fail without any
invalid response being accepted.

## Chosen policy

- Keep strict parser limits: `reason <= 800`, `required_invariant <= 500`, and
  `target_span <= 600` characters.
- Keep Qwen budgets at 1400/1600 tokens and attempts at 3.
- Add an opt-in `retry_guidance` argument to `complete_json()`.
- Append that guidance to every retry instruction, including length retries.
- State the same hard limits in the initial cross-verification prompt.
- Continue rejecting truncated JSON before parsing.
- Never truncate, slice, repair, complete, or heuristically accept model JSON.

Stages that do not pass `retry_guidance` retain their current messages and
retry behavior.

## Behavioral matrix

| Model outcome | Result | Next retry |
| --- | --- | --- |
| Valid schema, `finish_reason=stop` | Accepted | None |
| `reason > 800`, `stop` | Rejected | Same budget; validation error plus persistent compact-schema guidance |
| `required_invariant > 500`, `stop` | Rejected | Same budget; validation error plus persistent compact-schema guidance |
| `target_span > 600`, `stop` | Rejected | Same budget; validation error plus persistent compact-schema guidance |
| Any JSON, `finish_reason=length` | Rejected before parsing | 1600-token capped retry; length instruction plus persistent compact-schema guidance |
| Schema error followed by length | Both rejected | Final retry still receives field limits and full-regeneration instruction |
| All three attempts invalid | `JsonGenerationError` | No cache record written |

## Downstream lifecycle

1. `v31_cross_verify.messages()` creates the initial prompt.
2. `complete_json()` performs generation and supplies retry-only guidance.
3. `v31_cross_verify.parse()` remains the strict schema boundary; decisions,
   enums, scope policy, and field limits are unchanged.
4. `load_or_generate()` writes an issue cache only after successful generation
   and parsing. Failed issue `v31-primary-00095` has no cache file.
5. Existing cached verdicts are returned without calling the model.
6. Finalization, uncertain-policy resolution, repair routing, repair scope,
   target-span fallback, gates, and completion logic receive the same record
   format and are unchanged.

## Alternatives rejected

- Raising or removing the 800-character limit: weakens the reviewed schema and
  does not address runaway explanations.
- Silently slicing `reason`: would accept content the model did not emit as a
  valid complete record.
- Repairing truncated JSON: violates the strict length-aware policy.
- Increasing token budgets or attempts: increases cost and can encourage a
  longer explanation without preserving the forgotten constraint.
- Fabricating a cache record for issue 00095: bypasses independent judgment.

## Changed files

- `pact_full_pipeline_runner_v1/run_full_pipeline_v31.ps1`
- `pact_full_pipeline_runner_v1/v31_common.py`
- `pact_full_pipeline_runner_v1/v31_cross_verify.py`
- `pact_full_pipeline_runner_v1/self_test_v31.py`

This review packet is the only additional documentation file.

## Cache and resume impact

Cache keys, paths, record format, and reuse logic are unchanged. Production had
58 Qwen cross-verification cache files at inspection time; all are preserved.
`v31-primary-00095.json` does not exist, so a normal future resume will reuse
the existing 58 records and call the model for the first uncached issue. No
Reset, Redo, or force flag is required or permitted.

The current generated run config records the version used by the stopped run.
A future ordinary invocation will regenerate configuration from the installed
runner without modifying existing issue caches.

## Tests

- Mixed sequence: overlong reason → length-truncated response → compact valid
  response; budgets are 1400, 1400, 1600 and the final response is accepted.
- Cross-verification guidance is present after both validation and length
  failures.
- Non-opt-in length-aware stages do not receive cross-verification guidance.
- Truncated JSON is still rejected before parsing and is never repaired.
- Existing strict field, enum, scope, uncertain-policy, target fallback, and
  cache-reuse regression tests remain enabled.
- Python compilation, offline self-tests, PowerShell AST, and
  `git diff --check` are required before merge.

## Remaining risks

- A model may still ignore explicit field limits three times, in which case the
  stage correctly remains failed rather than accepting an invalid verdict.
- The new guidance adds a small number of prompt tokens on retries only.
- No live model or production pipeline run is part of development validation.

## Exact review question

Approve persistent, cross-verification-only retry guidance that restates the
existing strict field limits after every retry—including mixed schema-error and
length sequences—without changing parser limits, attempts, token budgets,
model settings, cache format, or downstream verdict policy?
