# V4 Phase 0C — Baseline (task)

Backing spec:
`docs/architecture/V4_FINAL_REVIEW_AND_IMPLEMENTATION_PLAN_RU_v2.md` (§ Phase 0C),
`docs/architecture/V4_MVP_SPEC_RU.md` (§8 Phase 0 measurement).

Implementation: `pact_full_pipeline_runner_v1/v4_phase0c_baseline.py`,
tests: `pact_full_pipeline_runner_v1/self_test_v4_phase0c_baseline.py`,
result-record contract: `docs/schemas/v4_phase0c_result_record.schema.json`
(`pact-v4-phase0c-result-record/v1`).

## Roles / boundaries

- This phase is **preparation only**. No live model or pipeline runs are
  executed by the harness; Track A grid runs and any further Track B source
  runs happen by separate explicit command later.
- Read-only over: the Phase 0B golden set (Track A) and v3.1 production run
  artifacts (Track B). No mutation of `pact_v4/`, no v3 config edits, no
  llama-server interaction.
- Outputs contain aggregated metrics, hashes and identities only — never full
  translation text (same boundary as `golden_sets/` in Phase 0B).

## Two independent data sources (never merged into one metric)

| | Track A | Track B |
|---|---|---|
| Source | chapter **046** Phase 0B golden set | chapter **100** v3.1 prod run (`pipeline_runs\chapter_100_to_100_v31\`, runner `run_full_pipeline_v31.ps1`) |
| Records | `pact-v4-golden-record/v1` (100 recs: 57 accepted / 43 needs_review / 0 rejected) | v31 artifacts: `manifest.json`, `v31/primary/lifecycle.json`, `verification_report.json`, `post_gate_deterministic_round_*.json`, `v31_final_changed_pid_ledger.json`, `meta/*.translation.json`, `monitor_state.v31.json` |
| Metric type | golden-comparison (FP-candidate) over accepted PIDs | internal pipeline metrics (what v3 itself found/fixed/left/resolved) |

### Track A — chunk-size × right-context grid

Grid `8-12 / 12-20 PID × right-context on/off` = 4 cells, v3 translation
stage only (`--phase translate`). Config levers (`chunking` in
`config.v3.json`):

- right-context on/off → `chunking.following_blocks` 2 / 0
- chunk-size low/high → `chunking.target_words`/`min_words`/`max_words`
  band (low band: 450/280/640, high band: 900/550/1200); the **actually
  achieved** PID/chunk is itself a reported metric, not a hard claim.

Each cell carries a ready-to-run command (NOT executed by 0C):
`py ./pact_translate_v3.py --config <cfg> --phase translate --start 46 --end 46`.

### needs_review handling (explicit decision, documented in result record)

Only `verdict.status == "accepted"` (57) records feed numeric Track A
metrics. `needs_review` (43) are **excluded**. This is a sample limitation
(records have not reached a final verdict), not a conscious design choice;
the baseline must be recomputed once they are curated. Recorded verbatim in
`track_a.source.needs_review_policy`.

### Known limitations (recorded directly in the result record as text)

- **semantic recall — not measurable this round.** `known_violations` is
  empty in all 100 records (incl. the 57 accepted). Recorded as
  `track_a.source.semantic_recall.status == "not_measurable"` with a reason.
  No surrogate metric is substituted.
- Measurable Track A metric this round: **FP-candidate rate** =
  `|accepted PIDs whose v3 draft violates a must_preserve invariant|
  / |accepted PIDs|` (a source number absent/changed in the RU output, or a
  required inline `<tag>` occurrence dropped). A PID present in the golden
  set but absent from the v3 output is a **gap** (listed explicitly), never
  a quiet skip.

### Track B — v31 run import (read-only)

`bad_repair`, `residual_errors`, `deterministic_integrity`, `time/tokens`
are read from the run's own artifacts (no golden set, no manual curation).
`russian_rubric` is **Track-A only** (needs an independent human reference);
it is reported as `not_measurable` for Track B to avoid anchoring bias from
rereading a v3 output as its own reference. A `pact-v4-golden-record` must
**not** be built from chapter 100: the schema pins `reference.source` to
`human_translation_epub` and no such independent translation exists for
chapter 100 (`pact_ru.epub` covers chapters 1–59).

Provenance per metric (read from the real run):

| Metric | Artifact + field |
|---|---|
| PID coverage | `manifest.json.blocks` ∩ `v31_primary_translations.json` |
| residual errors (primary) | `v31/primary/lifecycle.json` status: `resolved_retry_exhausted` / `resolved_false_positive` / `resolved_repair`; `verification_report.json.decisions` (`repair`/`keep`) |
| bad repair | `v31_final_changed_pid_ledger.changed_pids` (selected) ∩ `post_gate_deterministic_round_*.json` (`passed=false` or `introduced_issues` non-empty). Rate = `|bad_repair_pids| / |selected_repaired_pids|`. |
| deterministic integrity | PID coverage + post-gate pass/fail aggregation + `remaining_required_categories` |
| time/tokens | `meta/*.translation.json` → `attempts[].generation.usage` (tokens) and `generation.wall_seconds` |
| LTCR | `pending_definition` — V4_MVP_SPEC lists it but defines no numeric formula |
| reloads | `pending_definition` — requires server-log parsing; no single deterministic artifact |

### Partial-completion handling

A v31 run may be unfinished at import time. The result record is valid
with partial fields, carrying an explicit status rather than silent null:

- `track_b.completion.status` ∈ {`measured`, `pending_run_completion`, `no_run`}
- `primary_pass_complete` / `residual_pass_complete` flags
- per-metric `status` ∈ {`measured`, `pending_live_run`, `pending_run_completion`, `pending_definition`, `not_measurable`, `no_run`}

At time of writing, the production run
`chapter_100_to_100_v31` has its **primary** pass complete but the
**residual** pass still ACTIVE (`monitor_state.v31.json` →
`"residual Gemma semantic audit"` / `ACTIVE`, residual `lifecycle.json`
absent). Primary-derived metrics (PID coverage, primary residual counts,
bad repair, deterministic integrity, translation time/tokens) are
`measured`; final residual totals are `pending_run_completion`.

## Usage

```powershell
cd D:\pact\pact_translator_v3\pact_full_pipeline_runner_v1

# Build the result record (read-only) from a golden set and a v31 run root:
py .\v4_phase0c_baseline.py `
    --golden D:\path\golden_sets\chapter_046\records.json `
    --track-b-run-root D:\pact\pact_translator_v3\pipeline_runs\chapter_100_to_100_v31 `
    --out .\phase0c_result.json

# Tests (synthetic fixtures only):
py -m unittest self_test_v4_phase0c_baseline
```

After the future Track A grid runs execute, fill each cell by importing the
run output (`attach_grid_metrics`) — the harness already handles the
measurement/gap logic; only the live runs remain operator-driven.

## Acceptance

- `pact_full_pipeline_runner_v1/v4_phase0c_baseline.py` + schema + tests in
  the tree, passing `python -m unittest self_test_v4_phase0c_baseline`.
- No new required dependencies (reuses Phase 0A harness + stdlib).
- No live model/pipeline invocation anywhere in 0C tooling.
- No `pact_v4/` changes; no v3 config edits; no golden-record fabrication
  from chapter 100.