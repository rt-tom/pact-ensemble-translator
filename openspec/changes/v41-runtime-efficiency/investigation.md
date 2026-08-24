# Investigation: whole-chapter vs chunked paths (v41-runtime-efficiency 1.1)

Date: 2026-08-24
Scope: confirm both paths remain — whole-chapter generation + chunked audit/repair — and that chunk tables are not legacy.

## Whole-chapter generation (WholeChapterPidMap)

- `pact_v4/pipeline/v4_phase12_strict_runner.py` imports `WholeChapterPidMap` and derives it from `ChunkPlanArtifact` when `cfg.whole_chapter` is true (`WholeChapterPidMap.derive(chunk_plan, snapshot)`). The `chunk_plan.json` is still persisted with `mode=whole-chapter-derived` annotation; the ordered PID source of truth becomes `whole_chapter_pid_map.json`, but the plan itself remains for ownership and cache hash (`chunk_plan_hash` in record).
- `v4_phase_progress.py:_whole_chapter_mode()` checks for `wc_generation_started` event; `_whole_chapter_chunk_row()` collapses the N chunk rows into a single `chunk_id=whole_chapter` entry for the generation leg only.
- Identity/provenance (`v4_book_run.py:_pid_to_chunk`) still reads `chunk_plan.json` to map PID→chunk for glossary/shadow unless whole-chapter map overrides.
- `DECISIONS.md` 2026-08-10 W explicitly: "`chunk_plan.json` больше не выглядит как активный чанкинг-контракт … пишется новый `whole_chapter_pid_map.json` … а persisted `chunk_plan.json` аннотируется `mode=whole-chapter-d...`"

Conclusion: `ChunkPlan` stays required for `WholeChapterPidMap` ownership; cannot be deleted.

## Chunked audit/repair (always per-chunk)

- `run_chapter_audit` iterates `for chunk in chunk_plan.chunks: for detector in (qwen, gemma)` — audit is per chunk even in whole-chapter runs (B3 reads `chunk_plan.chunks` to split the chapter into audit chunks; `audit_journal.ndjson` records `audit_chunk_started/done` with chunk/total).
- `v4_phase_progress.py:_phase_audit/_phase_r_editor/_phase_repair/_phase_reaudit` each read `audit_cache_b3.json` which stores per-chunk results (`chunks:[{issue_count}]`, `r_editor.outcome`, `repair.batches`). `_detect_whole_chapter_phase()` after generation relies on B3 journal's per-chunk events to surface "Chapter audit chunk N/8" and "Selective repair round…".
- `_chunk_table()` whole-chapter special-case still renders one row, but the Phase block and B3 counters continue to report per-chunk audit/repair — the per-chunk vocabulary never disappears.
- Monitor tests `test_v4_phase_progress_monitor_whole_chapter.py` assert whole-chapter generation lives alongside 8-chunk audit expectations (e.g. `chapter_046` style audit chunks).

Conclusion: chunk-audit tables are not legacy; they are the live audit/repair signal even when generation is whole-chapter.

## chunk_plan.json examples

- Chunked (legacy-compatible): `{"chunks":[{"chunk_id":"chunk0001","pids":["p0001",...],"word_counts":[...], "boundaries":[...]}, ...]}`, e.g. 16-chunk chapter_046.
- Whole-chapter-derived: same `chunks` array persisted plus `mode: "whole-chapter-derived"` annotation and companion `whole_chapter_pid_map.json: {"schema":"pact-v4-whole-chapter-pid-map/v1","chapter_id":"...","pid_count":N,"entries":[{"pid":..., "order":...}]}`. The monitor reads both: generation derives order from `whole_chapter_pid_map`, audit still slices by `chunk_plan.chunks`.

## Monitor implication (task 6)

The progress monitor must not hide either branch:
- Keep `chunk_plan` read for journal counts, WholeChapterPidMap ownership, and audit chunk totals.
- Render both the `whole_chapter` generation row and the per-chunk audit/repair Phase lines from a single snapshot, rather than suppressing chunk tables in whole-chapter mode.

Verification: grep results in this file + manual check of `strict_runner.py:2635` branch and `v4_phase_progress.py:782-810` whole-chapter row.
