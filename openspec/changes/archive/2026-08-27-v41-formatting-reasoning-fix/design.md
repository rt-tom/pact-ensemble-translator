## Context

См. `proposal.md Why`: форматинг падает из-за `max_tokens 1600` при `reasoning 2000` на Gemma. Текущая реализация `pact_v4/phase5/formatting.py:resolve_format_mappings` - фиксированный `max_tokens 1600` per batch, `max_blocks_per_call 12`, `generation_retries 2`, без логирования `finish_reason/reasoning`. `pact_full_pipeline_runner_v1/v4_book_run.py:_build_formatting_client` поднимает отдельный `llama-server` на `8094` с `_gemma_server_args_for_reasoning(0)` (reasoning 0), но `_DEFAULT_FORMATTING_CFG.max_tokens` все равно 1600. Сервер `server_logs_fmt` показал `prompt 2397 + 1600` ровно в лимит, `formatting_report 0/69`.

## Goals / Non-Goals

**Goals:**
- Гарантировать что JSON ответа форматинга помещается в бюджет токенов при любом `span_count` главы.
- Сохранить один подъем сервера на главу, добавить single-call опцию для whole_chapter.
- Сделать `''` диагностируемым (raw/reasoning/finish_reason).

**Non-Goals:**
- Менять промпт перевода plain или контракт `run_formatting_align` (wrap остается model-free).
- Трогать `max_formatting_incidents` политику.
- Пересобирать старые главы.

## Decisions

**Decision: динамический max_tokens = max(800, 40*span_count + 500), кап 8192**
- Почему: 69 spans * 40 ~2760 + overhead 500 = 3260, берем 4000. 1600 хватило бы на ~27 spans. Кап 8192 чтобы не взорвать KV.
- Альтернатива: фиксированно 4000 для всех - рассматривали, но для 5-spans главы waste 3x прогрев; динамический экономнее и проще объяснить.
- Реализация: в `resolve_format_mappings` считать `needed = 40*len(allowed) + 500` для single-call, или `40*len(batch_spans)+500` для батча, `effective = min(8192, max(800, needed, cfg_max))`. Если `cfg.max_tokens` уже больше - не уменьшать.

**Decision: formatting = reasoning 0**
- Почему: форматинг - точечная локализация подстроки, reasoning не нужен (как в `phase3/audit` нет). Gemma с `reasoning_budget 2000` съедает бюджет контента.
- Альтернатива: суммировать бюджеты `reasoning + max_tokens` - сложнее, требует прокидывать оба лимита в `ApiClient`; проще отключить для форматинга (уже делает `_gemma_server_args_for_reasoning(0)`).
- Реализация: `_build_formatting_client` явно передает `reasoning 0` в server_args, `_FormattingBackendClient.complete` ставит `temperature` из `fmt_cfg` и не передает `reasoning` в `request_options`.

**Decision: single-call для whole_chapter, батчи оставить для чанков**
- Почему: whole_chapter 437 PIDs но только ~60 с spans - один промпт ~3500 токенов, укладывается в 49152. Один прогрев + один парсинг = меньше шансов на частичный debt.
- Альтернатива: всегда батчи по 12 - оставляем как fallback когда `single-call` выключен или prompt > 12000.
- Реализация: флаг `formatting_single_call_whole_chapter` (default true) в `v4_book_run.py`; при true - `resolve_format_mappings` получает все PIDs одним батчем.

**Decision: артефакты raw/reasoning**
- Почему: без них `''` не воспроизвести. Другие фазы пишут `*_raw.txt`.
- Реализация: в `resolve_format_mappings` после каждого `client.complete` писать `out_dir/formatting_batch{N}_raw.txt` (content) и `..._reasoning.txt` (CallRecord.reasoning), плюс `messages.json` для дебага. При ошибке парсинга логировать `finish_reason`, `usage`, `truncated`, первые 500 символов content.

## Risks / Trade-offs

- [Увеличение вывода до 4000 удвоит latency форматинга на Gemma (~50с -> 110с)] → Mitigation: только для глав с spans (69 глав из ~200), один вызов вместо 6 - суммарно быстрее.
- [Single-call промпт 3500 токенов может превысить лимит при 150 spans] → Mitigation: fallback к батчам если `len(prompt) > 12000` или `span_count > 80`.
- [Динамический max_tokens меняет identity_hash] → Mitigation: `max_tokens` не входит в `backend_identity_hash` (только `temperature/top_p/context_size`), identity не меняется.

## Migration Plan

1. Обновить `DEFAULT_FORMATTING_CFG.max_tokens` с 1600 на `None` (динамический) + кап 8192, прокинуть в `v4_book_run._DEFAULT_FORMATTING_CFG`.
2. Патч `resolve_format_mappings` + `_FormattingBackendClient` + запись артефактов.
3. Деплой без миграции данных: старые `formatting_report.json` остаются, новые главы получают `resolved > 0`.
4. Откат: вернуть константу 1600 через `formatting_cfg.max_tokens=1600` (переопределяет динамику).

## Open Questions

- Нужен ли `json_schema` вместо `json_object` для Gemma (строже валидация)? Можно ответить после прогона с новыми бюджетами - если 4000 решит `''`, схема не нужна.
