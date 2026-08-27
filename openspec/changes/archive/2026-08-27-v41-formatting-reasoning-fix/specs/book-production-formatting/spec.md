## ADDED Requirements

### Requirement: Formatting output budget covers full response
The system SHALL allocate a `max_tokens` budget for formatting model-calls that is sufficient to emit the complete `{"mappings":[...]}` JSON for all `inline_spans` in the request (batch or whole-chapter). The budget SHALL scale with span count (e.g., `40 * span_count + overhead`) and SHALL NOT be a fixed 1600 that truncates 69 spans.

#### Scenario: Large chapter with 69 spans succeeds
- **WHEN** formatting is invoked for a chapter with 69 `inline_spans`
- **THEN** the model-call `max_tokens` is >= 3000 (e.g., 4000) and the returned JSON parses without truncation, with `finish_reason` not `length` due to budget

#### Scenario: Small chapter budget does not waste
- **WHEN** formatting is invoked for a chapter with 5 spans
- **THEN** the budget is proportionally smaller (e.g., ~700) but still >= 500 and the call succeeds

### Requirement: Formatting shares server lifecycle without reasoning starvation
The system SHALL run formatting model-calls on a single resident `llama-server` lift per chapter (reuse on port 8094, KV-cache preserved between batches/calls) and SHALL NOT restart the server per batch. When Gemma reasoning is enabled for other phases, formatting SHALL either disable reasoning (`reasoning_budget 0`) or account for it separately so reasoning tokens do not consume the `max_tokens` JSON budget.

#### Scenario: Single lift for whole-chapter formatting
- **WHEN** formatting runs for a `whole_chapter` with multiple batches or a single call
- **THEN** all completions are served by the same server process (one `listening` log entry) and successive calls show `LCP similarity` reuse, not a restart

#### Scenario: Reasoning does not truncate JSON
- **WHEN** Gemma is configured with `reasoning_budget 2000` for translation
- **THEN** a formatting call with `max_tokens 4000` still returns complete JSON (reasoning tokens are budgeted separately or formatting uses `reasoning 0`)

### Requirement: Formatting failures are diagnosable
The system SHALL persist per-call diagnostics for formatting: raw model `content`, `reasoning_content`, `finish_reason`, `usage` and `response_format` attempt, and SHALL log them at WARNING level when `parse_format_mappings` fails. Empty `content` SHALL be logged with the first 500 chars of raw response and `finish_reason`.

#### Scenario: Empty JSON is diagnosable
- **WHEN** a formatting call returns `content ''` and `finish_reason length`
- **THEN** the log contains `Invalid JSON response: '' finish_reason=length max_tokens=...` and `formatting_batch{N}_raw.txt` / `formatting_batch{N}_reasoning.txt` are written to the chapter `out_dir`

### Requirement: Single-call option for whole-chapter chapters
The system SHALL support a `whole_chapter` formatting mode that sends all PIDs with `inline_spans` in one `complete` call (no per-12 batching) when span count fits into context, with the scaled `max_tokens` budget. The deterministic wrap (`apply_span_mappings`) remains model-free.

#### Scenario: Whole-chapter single call
- **WHEN** chapter has 69 spans and `whole_chapter` single-call is enabled
- **THEN** exactly one `formatting: attempt 1` call is made and `formatting_report.json` shows `resolved_count` > 0 when translation contains the target substrings
