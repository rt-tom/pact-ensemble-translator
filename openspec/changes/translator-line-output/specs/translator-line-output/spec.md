## ADDED Requirements

### Requirement: PID-labelled line output for V4 whole-chapter translation
The V4 whole-chapter translator SHALL return exactly one line per requested target segment, in exact source order. Each line SHALL use the form `PID: translated text`, with the exact PID followed by a colon and exactly one space. A segment SHALL occupy one physical line. The response SHALL contain no JSON wrapper, Markdown, headings, blank lines, notes, or commentary.

The generation prompt's existing `OUTPUT CONTRACT — MANDATORY AND AUTHORITATIVE:` block SHALL be replaced in place in a separate V4 whole-chapter-only template variant. The shared chunked-generation template and its JSON contract SHALL remain unchanged. The implementation SHALL NOT append a second contract or retain the JSON block in the whole-chapter variant. The whole-chapter input SHALL label its OWNED_SOURCE PID map as TARGET (e.g. `TARGET (OWNED_SOURCE):`). The example PIDs may be changed only to match the request's PID spelling:

```text
OUTPUT CONTRACT — MANDATORY:

Return only the translations for the PIDs in TARGET, in the exact order shown there.
Output exactly one line per TARGET PID, using this format:

p00001: Первая переведённая строка.
p00002: Вторая переведённая строка.

Preserve each TARGET PID exactly and output it exactly once.
Put exactly one space after the colon.
Keep each translation on a single line; do not merge or split segments.
Do not output context-only PIDs.
Do not output JSON, Markdown, headings, blank lines, notes, or commentary.
Do not add wrapper quotation marks around PIDs or translations.
Preserve punctuation and quotation marks that belong in the Russian translation.
```

#### Scenario: Valid ordered translation lines
- **WHEN** the translator returns one correctly formatted line for every requested PID in order
- **THEN** the parser converts the response to the existing ordered PID-to-text mapping
- **AND** downstream candidate validation and pipeline stages continue unchanged

#### Scenario: Translation text contains a colon or punctuation
- **WHEN** a translated segment contains one or more colons, commas, or quotation marks
- **THEN** the parser treats only the first PID separator colon as protocol syntax
- **AND** preserves subsequent punctuation as part of the translated text

#### Scenario: Output violates the PID contract
- **WHEN** output contains a missing, extra, duplicate, malformed, or reordered PID line
- **THEN** the response is rejected through the existing invalid-output/PID-mismatch handling and bounded retry path
- **AND** the system does not silently drop, reorder, infer, or merge segments

#### Scenario: Output contains blank or continuation lines
- **WHEN** output contains a blank line or a line without a valid PID separator
- **THEN** the response is rejected rather than attaching that line to a neighboring segment

#### Scenario: Segment translation is empty
- **WHEN** a PID line has no non-whitespace translation text after the separator
- **THEN** the response is rejected as invalid translation output

### Requirement: Preserve translation-map and artifact contracts
A valid line-formatted response SHALL be converted to the existing in-memory PID-to-text mapping before candidate creation and persistence. The response-format change SHALL NOT alter downstream step inputs or persisted translation-map schemas.

#### Scenario: Persist validated raw translation snapshot
- **WHEN** whole-chapter generation succeeds
- **THEN** `translations_raw.json` remains a JSON object mapping each PID to its validated Russian text
- **AND** it remains the pre-QA/repair generator snapshot

#### Scenario: Persist raw model response diagnostics
- **WHEN** a whole-chapter model attempt returns text
- **THEN** the per-attempt `whole_chapter_attempt{N}_raw.txt` file retains that exact response text
- **AND** under the new contract the file contains the line-formatted response, not normalized JSON

### Requirement: Isolate non-JSON response mode to whole-chapter translation
The V4 whole-chapter translator SHALL not request or depend on JSON-constrained response mode. Other model roles that use JSON contracts SHALL retain their existing response constraints and parsers. The chunked translation path SHALL retain its shared JSON template, response mode, and parser unchanged. The whole-chapter final pre-output checklist SHALL preserve existing fidelity/risk checks while using line-protocol checks instead of contradictory JSON-output wording.

#### Scenario: Whole-chapter translation request
- **WHEN** the system submits a V4 whole-chapter translation request
- **THEN** the request does not constrain the response to a JSON object

#### Scenario: Chunked translation request
- **WHEN** the system submits a chunked translation request
- **THEN** it continues to use the shared JSON prompt, JSON response mode, and JSON parser unchanged

#### Scenario: Non-translation JSON request
- **WHEN** the system submits an audit, repair, formatting, or other JSON-contract request
- **THEN** its current JSON response mode and parsing behavior remain unchanged
