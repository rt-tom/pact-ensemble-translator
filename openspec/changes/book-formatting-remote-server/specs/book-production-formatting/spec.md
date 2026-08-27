## ADDED Requirements

### Requirement: Remote formatting requires live server at format time
The system SHALL ensure that remote (`opencode_server`) book-run formatting has a live `opencode serve` reachable at `GET /global/health` at the moment `resolve_format_mappings` is called. If the server is not reachable, the system SHALL NOT silently produce 70/70 debt; it SHALL log a WARNING with the health error and either retry health or start a managed server on the configured port.

#### Scenario: 70 spans are not all debt when server is live
- **WHEN** a remote whole-chapter with 70 `inline_spans` is formatted with a live server
- **THEN** `formatting_report.json` has `resolved_count` > 0 and `incident_count` < 70, and `formatting_batch1_meta.json` has no `connection error` on `/global/health`

#### Scenario: Failure is diagnosable per chapter
- **WHEN** formatting health fails for a chapter
- **THEN** `formatting_batch1_meta.json` contains `error` with the health path and `effective_max_tokens`, and `out_dir/server_logs/opencode_serve_fmt_*.log` exists with server stdout/stderr

