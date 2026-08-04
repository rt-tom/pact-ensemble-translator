# V4 D1 — usage record: per-call remote usage (task)

Backing spec:
- `docs/plans/V4_IMPLEMENTATION_ORDER_PLAN_RU.md` (§4, Поток D — D1: «Phase 6: role batching (loop-order fix), fewer reloads, monitor/usage record»).
- Решение владельца (2026-08-04): в D1 обязательно реализовать usage-record — по итогам remote-прогона главы 0001 (`run_002_remote`) владельцу нужно видеть, сколько токенов ушло/пришло через opencode, через какие модели, и скорости чтения/генерации токенов.

## Зачем

Сейчас per-call remote usage собирается **только в памяти**:

- `BackendCallRecord` (`pact_v4/runtime/backend_protocol.py`) — label, model_ref, request_id, session_id, retry_count, finish_reason, usage, wall_seconds, raw_metadata;
- `RemoteRuntimeCoordinator._events` (`pact_v4/runtime/runtime_coordinator.py`) — `EVENT_KIND_REMOTE_CALL`, синхронизируется из `backend.call_records()`;
- в `strict_chapter_trial_record.json` попадает **только агрегат** `runtime.remote_calls` (`remote_calls_summary`: count, input_tokens, output_tokens, cached_input_tokens, reported_cost) — без разбивки по моделям и ролям, без скорости, без request_id/session_id.

Подтверждено (2026-08-04): лог `opencode serve` 1.4.7 (`~/.local/share/opencode/log/*.log`, `server_logs/`) usage/tokens/tps **не содержит**; `journal.ndjson` хранит только `backend_event_indices` (сами события не сериализуются).

## Что реализовать

- **Append-only артефакт `usage.ndjson`** в `out_dir` (по образцу `phase_progress.ndjson`, crash-safe: частичная строка не ломает чтение), одна JSON-строка на remote-вызов, включая failed:
  - `ts`, `label` (роль: generator / fidelity_reviewer / russian_selector / qwen_audit / gemma_audit / repair / formatting);
  - `model_ref` (provider/model), `provider`, `model`;
  - `input_tokens`, `output_tokens`, `reasoning_tokens`, `cached_input_tokens`, `cached_write_tokens`, `reported_cost` (только то, что реально сообщил провайдер — по образцу `_normalize_usage`, план §9.3: не выдумывать);
  - `wall_seconds`, `request_id`, `session_id`, `finish_reason`, `retry_count`, `error_class` (для ошибок).
- **Точка записи**: в `RemoteRuntimeCoordinator`/`OpenCodeServerBackend` при каждом завершённом вызове (или в runner'е через `runtime.events_since(...)` — выбор на реализации; важна дозапись при resume, без дублей за уже журналированные сессии).
- **Read-only агрегатор** (CLI по образцу `v4_phase_progress` или его расширение):
  - токены по моделям и по ролям, суммарно;
  - скорости: `input_tokens / wall_seconds` и `output_tokens / wall_seconds` на вызов и в среднем (грубая оценка: wall включает сеть/очередь, это не чистая decode-скорость провайдера — в карточке/отчёте это оговорить);
  - стоимость (если провайдер сообщил).
- **Resume-aware**: при resume продолжать дозапись; `config_identity`/backend identity/journal schema не менять.

## Что НЕ входит

- Мониторинг локальных llama-server вызовов (`local_lifecycle` уже покрыт).
- Веб-дашборд, графики, оценка качества перевода.
- Изменения Phase 1/2, cascade, risk, journal schema, gate-логики, terminal states, identity/cache.
- Перенос usage-данных в billing/бухгалтерию.

## Инварианты

1. Usage — **диагностика, не статус**: не влияет на логику пайплайна, resume, кэш-identity, journal schema, терминальные статусы.
2. Агрегатор read-only: не пишет в `out_dir`, не запускает/не останавливает пайплайн и `llama-server`.
3. Секреты (API keys, пароли) в артефакты не пишутся (план §12).
4. Никаких вычисленных значений там, где провайдер данных не дал (план §9.3).

## Gate / Acceptance

1. `usage.ndjson` пишется для каждого remote-вызова (успех и ошибка); append-only, crash-safe; при resume — без дублей.
2. Агрегатор показывает: токены по моделям и ролям, суммарно; input/output tps; стоимость.
3. На каталоге без `usage.ndjson` (например, `run_001`/`run_002_remote`) — корректный fallback: существующая сводка `runtime.remote_calls` из record.
4. Новые unit/integration тесты (writer, агрегатор, resume); полный `tests/pact_v4` зелёный; identity/journal не менялись.
5. Проверка на реальном remote-прогоне (повтор главы 0001 на remote-профиле).

## Данные для проверки

- `D:/pact/gate_bench_runs/v4_phase12_strict_0001/run_002_remote/` — read-only fallback-режим (без `usage.ndjson`).
- Синтетические каталоги с/без `usage.ndjson` в фикстурах.

## Роль-сплит

Обычная V4-фаза: один реализует, второй — adversarial review. Открытый вопрос на реализацию: писать из бэкенда (при каждом complete/failed) или из координатора (через события) — выбрать вариант, который не ломает локальный режим (local-вызовы в usage.ndjson не пишутся, остаются в `local_lifecycle`).

## Компактный промпт

```text
Реализуй usage-record (часть v4 D1, Phase 6) из
docs/plans/V4_D1_USAGE_RECORD_TASK_RU.md: append-only usage.ndjson
с per-call remote usage (роль, model_ref, токены, wall_seconds, request_id,
finish_reason, retry_count, error_class) + read-only агрегатор (токены по
моделям/ролям, input/output tps, стоимость). Target: main. Не менять
journal/identity/gates; fallback без usage.ndjson; tests зелёные.
```
