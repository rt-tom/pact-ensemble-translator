# v5-book-splitter

## Purpose

Превратить произвольный EPUB обычной прозы в детерминированный набор глав, который существующий пайплайн умеет переводить как Pact-главы. Сплиттер — внешний инструмент: пайплайн знает только его выход.

## ADDED Requirements

### Requirement: Spine-ordered deterministic chapters
The splitter SHALL read chapter order from the EPUB spine (OPF) and emit chapters in spine order as `NNNN_<slug>.html` files compatible with the existing `phase0b/source_html.py` parser, plus a `manifest.json` carrying per-chapter `file/order/en_title/pov/parent/illustrations` and book-level `epub_hash/splitter_version`. Re-running the splitter on the same EPUB SHALL produce byte-identical HTML and manifest.

#### Scenario: Pale splits deterministically
- **WHEN** the splitter runs twice on the pilot Pale EPUB (313 spine items)
- **THEN** both runs emit identical `NNNN_pale.html` files and an identical `manifest.json` including the same `epub_hash`

#### Scenario: Pipeline reads splitter output like Pact chapters
- **WHEN** a `v4_run book --preflight` targets the split Pale source root
- **THEN** chapter discovery, ordering, and readiness validation succeed from the manifest without EPUB knowledge in the pipeline

### Requirement: Structural POV capture
The splitter SHALL capture each chapter's POV from its structural marker (first-line `<p><strong>Name</strong></p>`, e.g. Verona/Lucy/Prologue/SB) into the manifest `pov` field and SHALL leave the marker line in the HTML unchanged.

#### Scenario: Rotating narrators land in the manifest
- **WHEN** chapters 0-0 (Prologue), 1-0 (Verona), and 13-18 (Lucy/SB) are split
- **THEN** the manifest records their respective `pov` values while the emitted HTML still contains the original marker line

### Requirement: Forward-compatible illustration slots
The splitter SHALL record illustration slots as `{id, anchor}` in the manifest and insert an `[ILLUSTRATION id]` placeholder in the HTML; an EPUB with zero images (like Pale) SHALL yield a valid empty list and SHALL NOT affect parsing.

#### Scenario: Imageless EPUB stays valid
- **WHEN** the pilot Pale EPUB (0 images) is split
- **THEN** every manifest entry carries an empty `illustrations` list and `source_html.py` parses all chapters cleanly

### Requirement: No pipeline-state contact
The splitter SHALL NOT read or write pipeline state (`book_state`, `pact_runs`, ledgers, snapshots); it operates only on the EPUB input and the book's output source directory.

#### Scenario: Splitter leaves Pact state untouched
- **WHEN** the splitter runs for Pale while Pact state exists on the host
- **THEN** no file under Pact state roots is created or modified
