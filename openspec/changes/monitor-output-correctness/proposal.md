## Why

The v4 run monitor (`pact_full_pipeline_runner_v1/v4_phase_progress.py`) renders output that is contradictory, duplicated, and unreadable for book runs. It shows every phase twice (from two different data sources), reports Selective repair / Re-audit scope as "not started" while they are in fact complete, crams all phase content onto a single ~600-character line under a forced 6-line budget, and omits recently-added pipeline phases (Glossary proposals, Formatting span restoration, and book-level Glossary/Memory promotion). An operator watching a live run cannot trust or read the monitor.

## What Changes

- Remove the duplicate `extra_parts` block in `render_report` so the monitor has a single source of truth (the structured per-phase progress).
- Drop the forced 6-line cap; render one line per phase (per-phase layout).
- Simplify the header: show `run <elapsed>` and `quiet <since-last-event>`; remove `mode=fine/coarse` and the duplicated phase name.
- Add a **Glossary** phase line (per-chapter proposal count from `glossary_proposals.json`, generated during the chapter's B3 stage), placed after the B3 phases — not before Translation.
- Fix **Formatting** to show aggregate inline-span restoration (restored/original across all tag types) plus incident count, instead of `n/a (whole-chapter)`.
- Add a **book-level Promotion** summary (glossary.json + memory) in `render_book_report`, since promotion runs in `v4_book_run` after chapter completion.
- Preserve the local-model **speed** line.
- Rewrite the legacy tests that pinned the broken duplicate substrings (`not_started`, `mode=`, etc.).

## Capabilities

### New Capabilities
- `v4-run-monitor`: the read-only v4 run/chapter/book progress monitor (`v4_phase_progress.py`) — what it surfaces and how.

### Modified Capabilities
<!-- none — first spec for this capability -->

## Impact

- **Code**: `pact_full_pipeline_runner_v1/v4_phase_progress.py` (`render_report`, `render_book_report`, the per-phase block / `_chapter_summary_row`) and legacy tests under `tests/pact_v4/pipeline/`.
- **Behavior (contract change)**: monitor output layout changes — per-phase lines instead of one concatenated line; `status:`/`phase:` duplicate lines removed; `mode=` token removed; new Glossary / Formatting / Promotion lines added.
- **Dependencies (read-only consumers)**: the monitor reads artifacts produced by in-flight changes — `glossary-model-resolver` (`glossary_proposals.json`, promotion to `glossary.json`/`book_memory.json`) and `book-formatting-remote-server` (`formatting_report.json`). This change does NOT implement those phases; it only surfaces their artifacts.
- **Out of scope**: PowerShell `monitor_pipeline.ps1` is a separate implementation with the same defects; aligning it is a tracked follow-up, not part of this change.
