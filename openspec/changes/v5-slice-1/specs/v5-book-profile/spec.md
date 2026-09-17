# v5-book-profile

## Purpose

Описать книгу как данные (`books/<slug>/book.yaml`) и научить раннер работать с любой описанной книгой, сохранив поведение Pact `book-1` байт-в-байт.

## ADDED Requirements

### Requirement: Book profile resolution
The system SHALL describe every book, including Pact, in `books/<slug>/book.yaml` (id/title, `content_kind: prose`, `source_lang/target_lang`, RT/media source roots, path to source `manifest.json`, path to approved `chapters.json`, `state.media_book_id`, `policy.hard_filters/editor_pass`). The `v4_run book` dispatcher SHALL accept the human-readable `--book <slug>` and resolve source/state/out solely from that profile; `--media-book-id/--memory-dir/--chapter-html-pattern` SHALL remain working overrides. Omitted `--book` SHALL resolve to `pact` only for CLI backward compatibility, not through a separate runtime path.

#### Scenario: Pale preflights from its profile
- **WHEN** `v4_run book --book pale --preflight` runs
- **THEN** it resolves Pale source/state/out roots and reports readiness without touching Pact paths

#### Scenario: Pact uses the common profile path
- **WHEN** an existing Pact invocation runs without `--book`
- **THEN** it resolves the `pact` profile and follows the same profile/manifest/chapters resolution path as `--book pale`, while preserving Pact's resolved layout, prompts, and artifacts exactly

#### Scenario: Alias selects a book, not a snapshot revision
- **WHEN** the owner invokes `v4_run book --book pale --chapters 1-3 --remote`
- **THEN** the dispatcher selects profile slug `pale`, its source roots and `media_book_id=2`; it does not treat `pale` as an artifact revision id

### Requirement: Initial authoritative state uses the existing four-file snapshot boundary
For a new book id (Pale: `2`), approved initial glossary entries and global memory facts SHALL be placed only in the existing bootstrap inbox as `glossary.json` and `book_memory.json`; `chapter_index.json` and `observations.json` SHALL be valid empty initial files. The existing `pact-snapshot bootstrap 2` flow SHALL publish this exact four-file state as `rev-0001`. Neither `chapters.json` nor any analysis/proposal artifact SHALL be placed inside snapshot `state/`.

#### Scenario: Pale begins as an isolated first revision
- **WHEN** the owner bootstraps approved Pale state for `book_id=2`
- **THEN** `/home/rt/pact_runs/books/2/snapshots/rev-0001/state/` contains exactly `glossary.json`, `book_memory.json`, `chapter_index.json`, and `observations.json`, and `CURRENT.json` points to `rev-0001`

#### Scenario: Profile metadata stays outside state
- **WHEN** Pale's `chapters.json` is used for titles and per-chapter POV
- **THEN** it is read from `books/pale/` and no extra file is added to any snapshot `state/` directory

### Requirement: Approved chapters artifact is title authority, separate from glossary
The approved `chapters.json` SHALL be the sole authority for Russian chapter titles and per-chapter POV for every book, including Pact. Per-chapter records carry full `ru_title` (RU arc + original number format, e.g. `Bonds 1.1` → `Узы 1.1`, `Gathered Pages: 1` → `Собранные страницы: 1`, `Histories (Arc 2)` → `Хроники (Арка 2)`). The runner SHALL deterministically derive its internal title map from its `en_title`/`ru_title` records for substitution in `v4_book_html.py`, and the generation prompt block from unique arc pairs in book first-appearance order (arc extracted by stripping the trailing number/designator suffix), rendered only when that derived map is non-empty under the legacy `CHAPTERS:` label. Titles SHALL never merge into `glossary.json`. A one-time Pact metadata migration SHALL derive and review the Pact `chapters.json` from its current 150 HTML chapters, `arc_names.json` (15 legacy entries UNCHANGED) plus the owner-approved B1 RU arc table, and established POV; `arc_names.json` SHALL NOT remain a runtime input. No byte-regression of the legacy prompt block is required: the derived identity becomes the baseline (B4 waived).

#### Scenario: Empty approved titles mean free title translation
- **WHEN** Pale's approved `chapters.json` has no `ru_title` values
- **THEN** no title block is added to the generation prompt and the model translates headings freely

#### Scenario: Approved chapters drive headings
- **WHEN** an approved `chapters.json` contains a non-empty `ru_title` for a chapter
- **THEN** the builder substitutes that RU heading and the generation prompt carries the derived `CHAPTERS:` title block

#### Scenario: Pact's derived block follows the approved table
- **WHEN** a Pact `book-1` chapter renders the title mapping from its approved `chapters.json`
- **THEN** its prompt contains a `CHAPTERS:` block with the unique arc pairs in book first-appearance order — `Bonds → Узы`, `Gathered Pages → Собранные страницы`, `Damages → Ущерб`, `Histories → Хроники`, `Breach → Разрыв`, `Collateral → Залог`, `Conviction → Обвинение`, `Subordination → Подчинение`, `Void → Пустота`, `Signature → Подпись`, `Null → Нуль`, `Mala Fide → Mala Fide`, `Malfeasance → Злоупотребление`, `Duress → Принуждение`, `Execution → Казнь`, `Sine Die → Sine Die`, `Possession → Одержимость`, `Judgment → Суд`, `Epilogue → Эпилог` — without reading `arc_names.json` at runtime

#### Scenario: Zero-chapter entries are dropped from the block
- **WHEN** the derived block is rendered for Pact
- **THEN** `Transgression`, `Sundown`, and bare `Gathered` (zero chapters in the book, underivable from records) do not appear in it

### Requirement: Per-chapter POV with null global narrator for rotation books
POV SHALL be a per-chapter attribute from the approved `chapters.json`: the bible renderer SHALL include a per-chapter `POV: <name> (<gender>)` line, and the book-level `pov.gender` for rotation books (Pale) SHALL be null. Pact's migrated `chapters.json` SHALL represent its established single narrator through the same renderer.

#### Scenario: Verona chapter carries its own POV
- **WHEN** Pale chapter 1-0 renders its bible section
- **THEN** the prompt contains the chapter POV (Verona) and no global male-narrator assertion

### Requirement: Fail-closed seed isolation and pair gating
The runner SHALL refuse a profile whose source manifest, chapters metadata, or state paths belong to another book, with an error instead of falling back. `arc_names.json` SHALL not be read at runtime. Slice-1 SHALL accept only `content_kind: prose` and the `en/ru` pair; another kind, pair, or unknown policy value SHALL fail preflight before output/state/model activity. It SHALL never silently apply RU tables outside their declared pair.

#### Scenario: Pale cannot inherit Pact metadata or state
- **WHEN** a Pale profile resolves its manifest, chapters metadata, or state path to a Pact-owned file
- **THEN** it aborts before pipeline startup with an isolation error and creates no output

#### Scenario: Unsupported pair stops before run
- **WHEN** a profile declares a non-`en/ru` pair
- **THEN** preflight fails before output, state, or model activity and never applies RU filters or the Russian editor
