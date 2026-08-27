# v4-run-monitor Specification

## Purpose
Read-only progress monitor for v4 chapter and book runs that surfaces per-phase progress from run artifacts without altering them.

## Requirements

### Requirement: Single source of truth
The monitor SHALL derive each phase's status and metrics from one structured source and SHALL NOT emit duplicated or contradictory phase information for the same phase.

#### Scenario: Repair and re-audit complete
- **WHEN** Selective repair and Re-audit scope are recorded as complete in the run artifacts
- **THEN** the monitor SHALL show them as complete and SHALL NOT also show them as "not started"

#### Scenario: No duplicated phase text
- **WHEN** the monitor renders a chapter
- **THEN** each phase name SHALL appear at most once in the output

### Requirement: Per-phase layout
The monitor SHALL render one line per pipeline phase in pipeline order and SHALL NOT force all phase content onto a single concatenated line.

#### Scenario: Live chapter renders per-phase
- **WHEN** rendering a fine-mode chapter with all phases populated
- **THEN** Entity extraction, Translation, R-editor, Chapter audit, Selective repair, Re-audit scope, Glossary, and Formatting SHALL each appear on its own line

### Requirement: No forced line cap
The monitor SHALL NOT truncate or cap the report to a fixed number of lines in a way that hides phase content.

#### Scenario: All phases present
- **WHEN** a run has every phase populated
- **THEN** all phase lines SHALL be present in the output (none dropped to satisfy a line budget)

### Requirement: Header clarity
The header line SHALL show the chapter id, total run elapsed time, and time since the last observed event (`quiet`); it SHALL NOT include a `mode=fine/coarse` token and SHALL NOT repeat the current phase name already shown in the phase block.

#### Scenario: Header shape
- **WHEN** a chapter is rendering
- **THEN** the first line SHALL match `[<id>] run <elapsed> · quiet <age>` and SHALL NOT contain `mode=`

### Requirement: Glossary phase per chapter
The monitor SHALL show a Glossary line with the count of glossary proposals generated during the chapter's B3 stage (read from `glossary_proposals.json`), placed after the B3 phases (Re-audit scope), not before Translation.

#### Scenario: Glossary proposals present
- **WHEN** `glossary_proposals.json` exists with N proposals
- **THEN** the monitor SHALL show `Glossary: N proposals`

#### Scenario: No proposals
- **WHEN** no glossary proposals exist (resolver produced none or did not run)
- **THEN** the monitor SHALL show `Glossary: 0 proposals` or omit the line

### Requirement: Formatting phase
The monitor SHALL show Formatting as an active phase with the aggregate count of restored versus original inline spans across all tag types plus the incident count, and SHALL NOT show `n/a (whole-chapter)`.

#### Scenario: Formatting done
- **WHEN** `formatting_report.json` reports 27 restored of 29 original spans with 0 incidents
- **THEN** the monitor SHALL show `Formatting: spans 27/29 · incidents 0`

### Requirement: Book-level promotion summary
For book runs (`--out-base`), the monitor SHALL show a promotion summary after chapter completion with counts promoted to `glossary.json` and to memory.

#### Scenario: Book promotion
- **WHEN** `v4_book_run` promotes glossary and memory after a chapter completes
- **THEN** the book report SHALL include a line such as `Glossary promoted: N → glossary.json · M → memory`

### Requirement: Local speed preserved
When the run uses local models and server logs are fresh, the monitor SHALL include a speed line (e.g., gemma/qwen speed).

#### Scenario: Local run
- **WHEN** server logs are fresh for local backends
- **THEN** the output SHALL include the speed line
