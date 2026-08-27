## Why

Удаленный (`--remote` / `opencode_server`) букран на этапе форматирования (Phase 5, `v4_book_run.py`) падает с 70/70 `missing_mapping` debt: `resolve_format_mappings` не доходит до модели, `GET /global/health` на `127.0.0.1:4097` возвращает `Connection refused`. Причина — `run_book` строит форматинг-клиент один раз до цикла по главам, для `remote` профиля не стартует `ManagedServerProcess`, а строго-ран каждой главы свой сервер уже закрыл (`runtime.close()`) до форматирования. На `local` профиль стартует `llama-server` отдельно, поэтому баг не виден. Без фикса удаленный Phase 5 не работает вообще, даже после PR #221.

## What Changes

- В `v4_book_run.py` перенести жизненный цикл форматинг-сервера на этап форматирования главы: поднимать сервер **когда букран доходит до этапа форматирования**, health-ждать `GET /global/health`, затем вызывать `resolve_format_mappings` + `run_formatting_align`, затем закрывать сервер в `finally`.
- Для `OpenCodeBackendConfig` (`server_mode: external` → трактовать как управляемый для форматирования, или явно стартовать `ManagedServerProcess` на порту из `base_url` с `log_dir=out_dir/server_logs`) и для `CompositeBackendConfig` пробрасывать тот же путь. Ошибка старта/ health-таймаут — fallback в debt с логом, без падения главы (lenient).
- `out_dir` для `resolve_format_mappings` — `out_dir` главы (как в PR #221), а не `memory_dir/server_logs_fmt`.
- Логи форматинг-сервера — `out_dir/server_logs/opencode_serve_fmt_*`.

## Capabilities

### New Capabilities
- `book-formatting-remote-lifecycle`: жизненный цикл форматинг-сервера для удаленного букрана (подъем/health-wait/закрытие на этапе форматирования главы).

### Modified Capabilities
- `book-production-formatting`: уточнить требование: форматирование главы обязано иметь живой сервер на этапе форматирования, иначе главы уходят в debt.

## Impact

- Код: `pact_full_pipeline_runner_v1/v4_book_run.py` (`_build_formatting_client`, `run_book` форматинг-блок, `Composite/OpenCode` ветки), `tests/pact_v4/test_formatting_v41_fix.py` (дополнить кейс remote).
- Нет breaking change для CLI, нет миграции артефактов, нет изменения формата `formatting_report.json`.
- Риск: средний — runtime lifecycle, но изолирован ленточным тестом и lenient-fallback.
