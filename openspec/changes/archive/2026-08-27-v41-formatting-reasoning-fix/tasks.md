## 1. Бюджет токенов форматинга

- [ ] 1.1 Поднять `DEFAULT_FORMATTING_CFG["max_tokens"]` с 1600 на динамический расчет в `pact_v4/phase5/formatting.py:resolve_format_mappings`: `needed = 40*span_count + 500`, `effective = min(8192, max(800, max(needed, cfg_max)))`, per-batch и single-call варианты, verify `pytest tests/pact_v4/test_formatting.py -k max_tokens` проходит
- [ ] 1.2 Прокинуть динамический бюджет в `pact_full_pipeline_runner_v1/v4_book_run.py:_DEFAULT_FORMATTING_CFG` (убрать хардкод 1600, оставить кап 8192 как дефолт для ручного оверрайда), verify `grep max_tokens v4_book_run.py` показывает динамический расчет
- [ ] 1.3 Зафиксировать `reasoning 0` для форматинга: `_build_formatting_client` строит Gemma server с `_gemma_server_args_for_reasoning(0)` и не передает `reasoning` в `CompletionRequest`, verify `server_logs_fmt` после прогона показывает `reasoning_budget 0` и `eval 4000` не срезается

## 2. Single-call для whole_chapter

- [ ] 2.1 Добавить флаг `formatting_single_call_whole_chapter` (default true) в `v4_book_run.py` и ветку в `resolve_format_mappings`: когда флаг true и запрос из whole_chapter - собрать все PIDs с spans в один батч, иначе батчи по `max_blocks_per_call`, verify ручной прогон на `0031` дает `1 formatting call` вместо `6` (лог `formatting:batch1`)
- [ ] 2.2 Fallback к батчам если `prompt_tokens > 12000` или `span_count > 80`, verify тест с 100 spans режет на 2 батча

## 3. Диагностика

- [ ] 3.1 В `resolve_format_mappings` после каждого `client.complete` писать `out_dir/formatting_batch{N}_raw.txt` (content), `formatting_batch{N}_reasoning.txt` (CallRecord.reasoning) и `formatting_batch{N}_messages.json` (messages), verify после прогона файлы существуют в `chapter_*/`
- [ ] 3.2 При `parse_format_mappings` ошибке логировать `finish_reason`, `usage`, `response_format_attempted`, `max_tokens`, первые 500 символов content (`LOG.warning`), verify `grep "Invalid JSON.*finish_reason" logs` появляется при `''`
- [ ] 3.3 Прокинуть `reasoning` и `finish_reason` из `LocalOpenAIBackend.CallRecord` через `_FormattingBackendClient` (расширить `_Gen` полями), verify `usage.ndjson` содержит `reasoning_tokens` для форматинга

## 4. Верификация

- [ ] 4.1 Регресс: прогнать `v41` главу с 69 spans на RT с новыми бюджетами, verify `formatting_report.json resolved_count > 0`, `incident_count < 10`, `formatting_batch*_raw.txt` содержит валидный JSON
- [ ] 4.2 `openspec validate --strict` проходит, verify команда без ошибок
- [ ] 4.3 Юнит: мок-модель возвращает `target_text` для всех spans одним вызовом, `run_formatting_align(mappings=...)` оборачивает в `<em>`, verify `pytest tests/pact_v4/test_phase5_formatting.py`
