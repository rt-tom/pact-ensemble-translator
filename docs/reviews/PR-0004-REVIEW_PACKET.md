# PR #4 Review Packet — v3.1.2g Performance Preflight

Risk: **REVIEW REQUIRED**

## Root cause

The active `Invoke-GemmaPreflight` ran one 512-token request immediately after
`GemmaTranslate` startup, parsed the last timing row from llama-server stderr,
and failed the pipeline whenever prompt or generation TPS was below threshold.
There was no warm-up, repeated measurement, or aggregation.

The incident measured 220.37 prompt t/s and 12.06 generation t/s and therefore
failed. A later unchanged user-run attempt measured 208.86 prompt t/s and 31.37
generation t/s and passed. This confirms that a single startup-sensitive sample
is not a stable blocking signal.

## Chosen policy

- Keep preflight enabled.
- Keep the existing model/profile, prompt, thresholds, and `max_tokens=512`.
- Execute one warm-up request and exclude it from statistics.
- Execute three measured requests.
- Use the median prompt TPS, generation TPS, and available MTP acceptance.
- A valid median below either threshold produces `status=advisory` and permits
  pipeline continuation.
- Transport failures, missing/new timing parse failures, invalid policy config,
  incomplete sample counts, and invalid/non-finite samples remain blocking.
- Preserve `passed` as “median meets thresholds”; add
  `execution_allowed=true` to represent advisory continuation.

## Downstream lifecycle trace

1. `run_full_pipeline_v31.ps1` starts the unchanged `GemmaTranslate` profile.
2. Unless `-SkipPreflight` is explicitly supplied, preflight sends one warm-up
   and three measured requests to the same local completion endpoint.
3. Each probe requires a new prompt and generation timing entry in the active
   server stderr log. Failure to obtain them throws before pipeline stages.
4. `v31_preflight_policy.ps1` validates all samples and computes measured-only
   medians. Warm-up values are retained in the report but excluded from medians.
5. `preflight_performance.json` records samples, medians, thresholds, status,
   `meets_thresholds`, `passed`, `blocking`, and `execution_allowed`.
6. A valid low median logs a warning and continues to prepare/source-analysis/
   translation. Invalid measurement state still stops here.
7. No active runner, monitor, repair, gate, finalizer, or cache loader consumes
   `preflight_performance.json`; it is diagnostic only.
8. The existing stage and issue-level cache paths and identities are unchanged.
   Plain resume therefore reaches normal cache reuse after preflight.
9. No Reset, Redo, force, cache deletion, or aggregate invalidation is added.

## Behavioral matrix

| Condition | Status | Pipeline action |
|---|---|---|
| Cold/slow warm-up; measured median meets thresholds | `pass` | Continue |
| Valid measured median meets both thresholds | `pass` | Continue |
| Valid prompt median below threshold | `advisory` | Warn and continue |
| Valid generation median below threshold | `advisory` | Warn and continue |
| Both valid medians below thresholds | `advisory` | Warn and continue |
| HTTP/transport failure | no report completion | Throw and stop |
| Missing active stderr log | no report completion | Throw and stop |
| No new prompt/generation timing rows | no report completion | Throw and stop |
| Fewer than configured measured samples | no report completion | Throw and stop |
| Zero, negative, NaN, or infinite TPS | no report completion | Throw and stop |
| Invalid warm-up/sample-count/policy config | no report completion | Throw and stop |

## Alternatives considered

1. Remove preflight entirely.
2. Make `-SkipPreflight` the normal resume path.
3. Add warm-up/repeats/median but retain a hard TPS threshold.
4. Use mean, minimum, or percentile aggregation instead of median.
5. Keep one sample and lower the generation threshold.

## Rejected alternatives

- Removing or routinely skipping preflight loses useful health and performance
  diagnostics.
- Retaining a hard TPS threshold still blocks a valid but slower machine/run
  even after measurement noise is reduced; TPS is throughput, not correctness.
- Mean is more sensitive to startup/outlier behavior; minimum preserves the
  original false-blocking problem; a percentile is not meaningful with three
  measured values.
- Lowering the threshold tunes around one observation and does not fix the
  single-sample design defect.

## Changed files

- `pact_full_pipeline_runner_v1/run_full_pipeline_v31.ps1`
- `pact_full_pipeline_runner_v1/v31_common.py`
- `pact_full_pipeline_runner_v1/v31_preflight_policy.ps1`
- `pact_full_pipeline_runner_v1/self_test_preflight_v31.ps1`
- `docs/reviews/PR-0004-REVIEW_PACKET.md`

## Cache and resume impact

- Cache format, identity, keys, paths, and reuse logic are unchanged.
- Development did not write production run artifacts or cache files.
- Production cache count changed externally from 34 to 37 during investigation
  due to later user-run activity; the production tracked tree remained clean.
- Plain resume is structurally safe after reviewed deployment: preflight runs,
  valid results continue, and existing caches are reused normally.
- `preflight_performance.json` will be overwritten by the next run with the
  expanded v3.1.2g diagnostic schema. No active consumer was found.

## Tests

- PowerShell AST: runner, policy helper, targeted test — pass.
- Targeted policy test under PowerShell 7 — pass.
- Targeted policy test under Windows PowerShell — pass.
- Cold warm-up excluded from a healthy measured median — pass.
- Valid below-threshold measured median is advisory/nonblocking — pass.
- Incomplete and invalid measured samples remain blocking — pass.
- Report-contract probe — pass.
- Python compilation — pass.
- Existing `self_test_v31.py` — pass.
- `git diff --check` — pass.
- No real model or production pipeline was invoked by validation.

## Remaining risks

- The four-request sequence was not integration-tested against the live model;
  validation is offline to protect production.
- Preflight startup time increases by three additional completion requests.
- The expanded report schema changes the meaning split explicitly:
  `passed` means thresholds met, while `execution_allowed` means the run may
  continue. No current consumer exists, but external tooling should use the
  explicit fields.
- Advisory policy permits a very slow but structurally healthy run to proceed;
  operators must use the warning/report for capacity diagnosis.

## Exact review question

Approve one excluded warm-up plus three measured medians, with valid
below-threshold performance treated as advisory while transport, parsing,
configuration, sample-count, and sample-validity failures remain blocking?
