## Context

`render_report` builds the phase line as `merged = compact_bodies + extra_parts`. `compact_bodies` is the correct, structured per-phase progress (read from `audit_cache_b3.json`, repair/reaudit records, etc.). `extra_parts` is a hand-built list of lifecycle substrings added "for legacy tests", derived from a handful of B3 `phase_progress` events and missing repair/reaudit progress (those stages are tracked via `audit_cache_b3.json`, not the specific `repair_round`/`reaudit_scope` events `extra_parts` looks for). The result is contradictory (`Selective repair: 7/7` next to `Selective repair: not started`). A forced `if len(lines) > 6: lines = lines[:6]` then crams everything onto one line.

The monitor also predates the Glossary resolver and the real Formatting span-restoration phase, so it omits them (Formatting is rendered as `n/a (whole-chapter)`).

See proposal.md — Why for motivation.

## Goals / Non-Goals

**Goals:**
- Single source of truth for phase status/metrics (remove `extra_parts`).
- Per-phase, readable layout (no forced line cap).
- Surface the currently-missing phases: Glossary (per-chapter), Formatting (aggregate spans), and book-level Promotion.
- Preserve the local speed line.

**Non-Goals:**
- Do NOT reimplement the Glossary resolver, Formatting, or promotion — those are delivered by `glossary-model-resolver` and `book-formatting-remote-server`. This change only reads their artifacts.
- Do NOT modify PowerShell `monitor_pipeline.ps1` (separate follow-up check).

## Decisions

- **Remove `extra_parts` entirely.** The structured per-phase block already renders every phase correctly (repair 7/7, reaudit done) — `extra_parts` was the broken duplicate. This eliminates the contradiction at the source.
- **Restructure into one line per phase in pipeline order.** Keep the existing per-phase metric helpers as the single source; extend them to add Glossary (read `glossary_proposals.json`) and correct Formatting (read aggregate spans from `formatting_report.json`).
- **Drop the forced 6-line truncation** (`lines = lines[:6]`). Keep coarse-mode behavior but simplified; fine-mode renders the full phase block.
- **Header**: compute elapsed from run start and `quiet` from time since the last event/usage; drop `mode=` and the duplicated phase name. `alive`/`stalled` semantics are expressed via the `quiet` age.
- **Book-level promotion**: in `render_book_report`, after the chapters table + active-chapter detail, append a promotion summary reading the promotion result (glossary.json / book_memory.json counts, or the promotion report artifact produced by `glossary-model-resolver`).
- **Tests**: rewrite the legacy tests that asserted on `not_started` / lifecycle substrings / `mode=` / 6-line shape to assert on the new per-phase layout and the no-contradiction property.

## Risks / Trade-offs

- [Risk] Removing `extra_parts` breaks legacy tests that pin those substrings → Mitigation: rewriting those tests is explicitly in scope (tasks §4).
- [Risk] Exact artifact field names for glossary proposal count and promotion counts are not yet confirmed → Mitigation: verify against `glossary-model-resolver` artifacts during implementation (task §1.1); reading `glossary_proposals.json` and the promotion output is the intended contract. Field-name details are implementation-only.
- [Risk] Book-level promotion timing (per-chapter vs end-of-book) → Mitigation: promotion is post-complete in `v4_book_run`; show a cumulative summary in the book report reflecting completed chapters only.
