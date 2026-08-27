## Purpose

Обеспечивает жизненный цикл сервера для Phase 5 форматирования в удаленном букране — сервер поднимается именно на этапе форматирования главы, а не на весь прогон.

## ADDED Requirements

### Requirement: Formatting server lifecycle is bound to formatting stage
The system SHALL start the formatting `CompletionBackend` server when the book run reaches the formatting stage for a chapter (`terminal_status in (complete, accepted_degraded)` and chapter has `inline_spans`), SHALL health-wait `GET /global/health` until healthy (or timeout), SHALL execute `resolve_format_mappings` + `run_formatting_align` via that backend, and SHALL close the backend/runtime in `finally`. The server SHALL NOT be kept alive for the whole book run.

#### Scenario: Remote formatting starts server on demand
- **WHEN** book run processes a chapter with 70 `inline_spans` and remote profile `opencode_server` (managed or external pointing to 4097)
- **THEN** a formatting backend runtime is started at formatting stage (log `opencode_serve_fmt_*` under `out_dir/server_logs`), health `GET /global/health` succeeds, exactly one `resolve_format_mappings` call is made with `effective_max_tokens` per PR #221, and `formatting_report.json` has `incident_count == 0` (or < 70) instead of 70/70 debt

#### Scenario: Server is closed after formatting
- **WHEN** formatting for a chapter completes (success or fallback to debt)
- **THEN** the formatting runtime is closed and the next chapter's strict run can start its own managed server on the same port without `port already served` conflict

#### Scenario: Health failure falls back to lenient debt without crashing chapter
- **WHEN** formatting server fails to start or health times out (e.g., `Connection refused` on `/global/health`)
- **THEN** the chapter does NOT crash; formatting falls back to empty mappings with lenient debt (`formatting_report.json` written with `incident_count == span_count`), `translations.json` is left with plain text, and a WARNING is logged with `health error` and `port`

### Requirement: Formatting backend respects remote and composite profiles
The system SHALL build the formatting backend from the same `runtime_config` (`--runtime-config` / `--translator` / `--reviewer` overrides) that the strict run uses, including `CompositeBackendConfig` routing. For `OpenCodeBackendConfig` the server SHALL be started via `ManagedServerProcess` when `server_mode` is `managed`, and SHALL health-connect via `OpenCodeServerBackend` with Basic-auth from env when `server_mode` is `external`. The `log_dir` for the formatting server SHALL be `out_dir/server_logs` (per-chapter), not a global `server_logs_fmt`.

#### Scenario: Composite remote profile routes formatting via opencode
- **WHEN** book run uses composite profile with `generator: opencode/muse-spark-1.2-contributor-free` and `gemma_audit: opencode/...`
- **THEN** formatting `model_ref` resolves to the opencode sub-backend and the call succeeds via `CompositeCompletionBackend` without falling back to debt

#### Scenario: Formatting uses reasoning 0 even when profile has reasoning 3
- **WHEN** runtime config carries Gemma `reasoning_budget` 2000 / `reasoning: 3`
- **THEN** the formatting backend is built with `reasoning 0` (`_gemma_server_args_for_reasoning(0)`) so reasoning tokens do not consume the `max_tokens` JSON budget
