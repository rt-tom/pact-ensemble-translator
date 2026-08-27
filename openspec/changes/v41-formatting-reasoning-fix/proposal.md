## Why

Форматинг главы `0031_collateral-4-8` на `RT` (gemma-4-26B-A4B, local_llama) упал целиком: 6 батчей по 12 блоков, каждый `Invalid JSON response: ''` x2, итог `resolved 0/69, debt 69` (`formatting_report.json` в `book_0031-0031_local_20260826_122206_914117`). Сервер `server_logs_fmt/Gemma_135420_stderr.log` показывает что модель генерировала ровно `1600` токенов и останавливалась по лимиту (`n_decoded 1600, truncated 0, finish reason length`). При `reasoning` 2000 + `max_tokens 1600` Gemma не успевает отдать JSON - обрезает середину массива `mappings` и клиент получает пустой/битый ответ. Батчинг по 12 усугубляет: один вызов мог бы уложиться, но 6 раз платим за прогрев промпта и каждый раз упираемся в тот же лимит.

## What Changes

- Поднять лимит вывода форматинга с `1600` до достаточного для полного ответа (оценка `40 * spans + 500` overhead, для 69 spans ~3300, берем `4000` с верхним капом `8192`) и сделать его зависимым от размера батча/главы, а не константой.
- Учесть reasoning-бюджет Gemma: при `reasoning > 0` не срезать `max_tokens` до 1600, а выделять отдельный бюджет (`reasoning_budget + max_tokens`) или отключать reasoning для форматинга (`reasoning 0`, как в `_gemma_server_args_for_reasoning(0)`).
- Сохранить один подъем `llama-server` на главу (текущий `_build_formatting_client` на `8094`), но предложить опцию `single-call` для `whole_chapter`: все `inline_spans` одним `complete` вместо `ceil(spans/12)` поочередных вызовов. Если батчи остаются - не пересоздавать сервер между ними, переиспользовать KV-cache.
- Добавить диагностику: писать `formatting_batch{N}_raw.txt` + `formatting_batch{N}_reasoning.txt` и дамп `messages` в артефакты главы, логировать `finish_reason/usage/truncated` при `Invalid JSON`, чтобы `''` стало диагностируемым.

## Capabilities

### New Capabilities
- (none)

### Modified Capabilities
- `book-production-formatting`: требования к лимитам вывода и устойчивости formatting model-call (бюджет токенов должен покрывать весь JSON ответа, батчинг не должен приводить к 100% debt при валидном промпте).

## Impact

- Затрагивает: `pact_v4/phase5/formatting.py` (`DEFAULT_FORMATTING_CFG.max_tokens`, `resolve_format_mappings` - динамический `max_tokens`, логирование), `pact_full_pipeline_runner_v1/v4_book_run.py` (`_DEFAULT_FORMATTING_CFG`, `_FormattingBackendClient`, `_build_formatting_client` - reasoning=0 для форматинга, опция single-call, запись raw/reasoning артефактов), `pact_v4/runtime` (lifecyle форматинга на 8094).
- Не ломает: промпт перевода `plain`, `run_formatting_align` apply-логику, `max_formatting_incidents` политику (debt остается lenient).
- Риск: Low (локальная сборка, только бюджет токенов + логирование; изменение лимита только увеличивает вывод, не меняет контракт).
