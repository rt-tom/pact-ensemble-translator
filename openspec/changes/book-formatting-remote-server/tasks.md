## 1. Фикс жизненного цикла форматинг-сервера

- [ ] 1.1 Перенести создание форматинг-клиента внутрь `run_book` цикла: в блоке `if terminal_status in _PROMOTING_STATUSES and has_spans` перед `resolve_format_mappings` строить `backend = load_runtime_config(rc_path)` → `runtime = backend.build_runtime(log_dir=out_dir/"server_logs")` с `ManagedServerProcess` для `OpenCodeBackendConfig` (port из `base_url`) и health-wait, затем `fmt_backend = build_role_backend(backend, runtime)` → `_FormattingBackendClient`, вызов `resolve_format_mappings(..., out_dir=out_dir)`, закрытие `runtime/client` в `finally`; удалить глобальный `formatting_client` до цикла и `server_logs_fmt` — верификация: `book_0032_collateral-4-9` на `media` с `--remote` дает `formatting_report.json` с `incident_count < 70` (не 70/70) и `model_call_count == 1` для whole-chapter
- [ ] 1.2 Расширить `_build_formatting_client(out_dir)` чтобы для `OpenCodeBackendConfig`/`CompositeBackendConfig` с `server_mode: external` стартовал `ManagedServerProcess` на порту из `base_url` (или `_default_managed_spec`) с `log_dir=out_dir/server_logs`, health `GET /global/health`, и `reasoning 0` оверрайд как для `Local` — верификация: `grep -n "ManagedServerProcess\|base_url.*4097" pact_full_pipeline_runner_v1/v4_book_run.py` показывает ветку для `OpenCode`/`Composite`, и `pytest tests/pact_v4/test_formatting_v41_fix.py -k remote` проходит

## 2. Диагностика и регрессия

- [ ] 2.1 Убедиться что `resolve_format_mappings` пишет `formatting_batch1_meta.json` с `effective_max_tokens`/`finish_reason`/`usage` и `server_logs/opencode_serve_fmt_*.log` per-chapter, а при `Connection refused`/`health timeout` — `LOG.warning` с портом и fallback в lenient debt без падения главы — верификация: `ls chapter_*/formatting_batch1_meta.json` и `ls chapter_*/server_logs/opencode_serve_fmt_*` существуют после remote букрана
- [ ] 2.2 Запустить `openspec validate --strict` и `pytest tests/pact_v4/test_formatting_v41_fix.py` — верификация: обе команды зеленые, нет `skip_specs` ошибки

## 3. Интеграция

- [ ] 3.1 Ручной smoke на `media`: `python -m pact_full_pipeline_runner_v1.v4_book_run --chapters 0032 --remote` (или `v4_run book --chapters 32 --remote --out-base` новый) с живым `opencode serve` на 4097 — проверить `translations.json` содержит `<em>` и `formatting_report.json` без `connection error` — верификация: `jq .incident_count chapter_0032_collateral-4-9/formatting_report.json` < 70
