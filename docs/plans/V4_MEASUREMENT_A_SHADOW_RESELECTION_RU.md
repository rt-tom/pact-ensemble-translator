# V4 Measurement Task A — shadow re-selection over an existing sequential run

Backing spec: `docs/plans/V4_SINGLE_RESIDENT_DRIVER_ARCHITECTURE_RU.md`
(§ "Как использовать текущий двухфазный sequential run", измерительная
задача 1 в § "Что измерить до кода").

Existing input run:
`D:\pact\gate_bench_runs\v4_phase12_046_seq\draft_001\` (`generation_bundle.json`,
`selection_results.json`, `translations.json`, `provenance.json` — sequential
generate+select run, `sequential_model_caveat` recorded, `--use-gemma-selector`
NOT used, tie-break selection only).

## Why this task

`draft_001`'s `selection_results.json` was produced with a deterministic
role-order tie-break, not a full Qwen→Gemma cascade winner. Its
`selected_role_counts` therefore under-represents how often the cascade
would actually diverge from the `fidelity_first` draft that generation used
as (unverified) left context. This task produces the missing number without
re-running generation.

## Roles / boundaries

- **Read-only against `draft_001`.** This task must not overwrite
  `generation_bundle.json`, `selection_results.json`, `translations.json` or
  `provenance.json` in that directory. `run_select` in
  `pact_v4/pipeline/v4_phase12_sequential_runner.py` derives its output
  directory from the same `--out-dir` it reads `generation_bundle.json`
  from — so it must be pointed at a **new, empty output directory**, not at
  `draft_001` itself.
- **No new generation.** Only `generation_bundle.json` is consumed as input;
  Phase 2B (Gemma generation) is not re-run.
- **One live model swap, not the production pipeline.** This does invoke
  live Qwen and Gemma over HTTP (`select_candidate`'s Qwen fidelity gate,
  then one Qwen→Gemma preference pass) — it is a small measurement run, not
  a `pact_translate_v3`/production invocation, and does not touch v3
  artifacts, glossary, or book memory.
- **Output is a new measurement record only** — a comparison document, not a
  replacement selection result and not a claim about final v4 quality.

## Procedure

1. Copy `generation_bundle.json` from `draft_001` into a new directory,
   e.g. `D:\pact\gate_bench_runs\v4_phase12_046_seq\shadow_reselect_001\`.
2. Run the `select` phase against that new directory with
   `--use-gemma-selector`, so both the Qwen fidelity gate and the real Gemma
   Russian preference execute (one Qwen→Gemma swap):

   ```powershell
   py .\v4_phase12_sequential_run.py `
       --phase select `
       --out-dir D:\pact\gate_bench_runs\v4_phase12_046_seq\shadow_reselect_001 `
       --use-gemma-selector `
       --qwen-url <qwen-llama-server-url> `
       --gemma-url <gemma-llama-server-url> `
       --run-label v4-shadow-reselect-046
   ```

   This writes `selection_results.json`, `translations.json`,
   `provenance.json` into `shadow_reselect_001` only; `draft_001` is
   untouched.

3. Offline-compare, per chunk, `draft_001/generation_bundle.json`'s
   `fidelity_first` candidate PID-map against `shadow_reselect_001`'s
   selected candidate PID-map (the true cascade winner, not tie-break).

## Metrics to report (new measurement record, not a run artifact)

- Per chunk: was a `fidelity_first` draft available; cascade winner role or
  terminal non-selection state (`quarantined` / `needs_synthesis` /
  `incomplete_generation`); role match/mismatch against `fidelity_first`;
  **PID-map mismatch** between `fidelity_first` draft and selected map
  (the correctness criterion — not "different role").
- Position of the **first** context-impacting mismatch in chunk order, and
  the length of the suffix a speculative/batch-first driver would have had
  to invalidate from that point.
- Aggregate: divergence rate, quarantine rate, `needs_synthesis` rate —
  compared against the same aggregates already in `draft_001/provenance.json`
  (tie-break run) to show how much the tie-break undercounted divergence.

## Explicit non-goals

- Does not predict later waves of a speculative/windowed driver: once a
  suffix is regenerated with corrected left context, its own candidates can
  change again. This task measures only the **first** wave.
- Does not measure lifecycle/reload cost (that is a separate task — model
  swap timing on the same hardware) or boundary-rubric quality (that
  requires the boundary golden set, a separate task).

## Acceptance

- New measurement record (JSON or short report) with the per-chunk table
  and aggregate numbers above, referencing both run directories by path and
  `provenance.json` hashes (source/snapshot/chunk-plan/config identities)
  from `draft_001`, so the comparison is reproducible and identity-checked.
- `draft_001/*` unchanged (verify via file mtimes/hashes before and after).
- No `pact_v4/` code changes, no v3 production run, no `llama-server`
  lifecycle management beyond the operator starting/stopping it manually
  for this one select pass.
