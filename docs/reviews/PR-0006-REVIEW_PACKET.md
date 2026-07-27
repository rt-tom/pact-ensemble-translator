# PR #6 Review Packet — v3.1.2i advisory cross-verification reason length

## Risk classification

**LOW RISK.** This change relaxes validation only for explanatory metadata that
does not control the verdict, confidence, repair scope, invariant, or target.

## Root cause

Qwen returned two complete JSON objects whose non-empty `reason` exceeded 800
characters, then used the final attempt on a response ending with
`finish_reason=length`. PR #5 repeated the compact-output instruction on every
retry, but prompt guidance cannot guarantee that the model obeys a character
limit. The strict explanatory-field limit therefore remained a pipeline-wide
failure point.

## Policy

- A complete JSON object with a non-empty `reason` is accepted regardless of
  reason length and is stored verbatim after the existing whitespace
  normalization. It is never sliced or summarized.
- The prompt continues to request 2–3 short sentences, preferably within 400
  characters.
- Decision, confidence, repair scope, required invariant, forbidden
  interpretations, and target span retain strict validation.
- Every response with `finish_reason=length` is still rejected before parsing.
- Truncated JSON is never repaired or completed.
- Model settings, token budgets, attempts, cache paths, cache keys, record shape,
  and downstream policy are unchanged.

## Cache and resume safety

Existing issue cache files are read before any model call and remain compatible.
The failed issue has no cache entry. A normal resume reuses all completed cache
files and generates only the first missing verdict. No Reset, Redo, force, or
fabricated cache is required.

## Regression coverage

- A complete verdict with an 801-character reason is accepted on the first call.
- A 5000-character reason is preserved without slicing.
- An empty or whitespace-only reason remains invalid.
- Existing length-truncation tests still reject before JSON parsing.
- Existing enum, scope, invariant, target-span, retry-budget, and cache-reuse
  tests remain enabled.

## Changed production files

- `pact_full_pipeline_runner_v1/run_full_pipeline_v31.ps1`
- `pact_full_pipeline_runner_v1/v31_common.py`
- `pact_full_pipeline_runner_v1/v31_cross_verify.py`
- `pact_full_pipeline_runner_v1/self_test_v31.py`

## Review question

Approve treating reason length as advisory while preserving the complete
non-empty reason and keeping all controlling fields and truncated-response
handling strict?
