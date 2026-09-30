## ADDED Requirements

### Requirement: Format only source-tagged translation pairs

The book formatter SHALL request model mappings only for PIDs with source inline_spans. Each PID SHALL appear once in the request with its English source context, span IDs/text and paired final Russian translation. Source text SHALL be encoded as data, not interpreted as an instruction. The model SHALL return only pid/span_id to target_text/occurrence mappings; it SHALL NOT rewrite Russian text.

#### Scenario: Untagged PID is excluded
- **WHEN** a chapter has 400 PIDs and 78 contain source inline spans
- **THEN** only those 78 PIDs are in formatting model input and untagged PIDs make no formatting model call

#### Scenario: Chapter has no inline spans
- **WHEN** no source PID contains an inline span
- **THEN** no formatting server or mapping call is started, and the final translation text is unchanged

### Requirement: Prefer one fitting mapping call

The formatter SHALL attempt one request for the entire tagged set whenever the serialized prompt, role-policy output allowance and safety margin fit the resolved context envelope. It SHALL NOT split solely because the chapter exceeds 80 spans or 12 tagged PIDs. When one request does not fit, it SHALL pack the tagged PIDs into the fewest safe contiguous groups and record the reason for splitting.

#### Scenario: Large tagged set fits
- **WHEN** 78 tagged PIDs with 102 spans fit the resolved model envelope
- **THEN** the formatter makes one mapping call for all 102 spans

#### Scenario: Tagged set exceeds context
- **WHEN** the complete tagged request cannot fit with its output allowance
- **THEN** the formatter sends only bounded groups of tagged PIDs, preserves their order and records each group's PID/span counts

### Requirement: Formatting uses its resolved role policy

Production formatting SHALL use the resolved formatting RoleCallPolicy for sampling and max_output_tokens, with enough allowance for the planned mapping response. It SHALL NOT silently use legacy 40*spans+500/min-800 fallback budgeting when the policy is missing. A missing policy SHALL be diagnosed and unresolved spans SHALL become lenient formatting debt without a model call.

#### Scenario: Policy is present
- **WHEN** a configured local or remote formatting client handles a mapping group
- **THEN** its request budget is derived from the formatting role policy and the actual span count, and the effective budget is recorded in call metadata

#### Scenario: Policy is missing
- **WHEN** a production formatting call has no resolved role policy
- **THEN** no request is issued with a fallback 900-1180 token budget, the missing-policy cause is recorded, and the chapter follows the existing lenient debt behavior

### Requirement: Reduce failed formatting work

On a length/context failure or invalid whole response, the formatter SHALL split only the failed group into smaller groups and SHALL NOT repeat an identical request with the same limiting budget. On a valid partial response, it SHALL retain validated mappings and request only unresolved spans. Transient transport retries MAY repeat a request once with bounded backoff. Recovery SHALL have a finite call/depth limit; unresolved spans SHALL be recorded as FormattingIncident debt.

#### Scenario: Length-limited empty JSON
- **WHEN** a mapping call returns empty content and finish_reason=length
- **THEN** the next model call contains a strict subset of that group's tagged PIDs/spans, or the group is marked debt if it cannot be split safely

#### Scenario: Partial valid mappings
- **WHEN** a valid mapping response covers only part of the requested spans
- **THEN** already validated mappings are retained and the follow-up request contains only unresolved spans

### Requirement: Preserve formatting safety and reports

Every accepted target_text SHALL occur verbatim in its own PID's final Russian translation, its occurrence SHALL identify a nonoverlapping position, and its tag SHALL be allowed by the source span. Tag application SHALL be deterministic and SHALL preserve every non-tag character of the translation. Unresolved or ambiguous spans SHALL not receive guessed tags. The translations.json and formatting_report.json shapes and per-attempt diagnostics SHALL remain readable; a new formatting policy version SHALL distinguish new output from older runs.

#### Scenario: Model proposes foreign or overlapping target
- **WHEN** a model returns a substring absent from the PID translation, a foreign pid/span_id or a position overlapping another mapping
- **THEN** that mapping is rejected, the original Russian text remains unchanged, and the span becomes unresolved for bounded recovery or debt

#### Scenario: Existing completed chapter
- **WHEN** a previously completed chapter is resumed without an explicit formatting rerun
- **THEN** its stored translation and formatting report are not silently rewritten
