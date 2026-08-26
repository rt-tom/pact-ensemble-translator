# Tasks — v42-monitor-compact

## 1. Починка подсчёта
- [x] 1.1 `phase_progress.ndjson` — единственный источник для `chunks done/started`, `batches done`, `phase`; `audit_journal` — только fallback
- [x] 1.2 Убрать `10/7` — done не больше total, `3/8 vs not started` — один статус

## 2. Компактный вывод
- [x] 2.1 Удалить таблицу `chunks 12×3` и блок `counters` (дубль)
- [x] 2.2 Схлопнуть в 6 строк: хедер + сводка фаз + скорость/usage

## 3. Local vs Remote
- [x] 3.1 `local` — live `eval/prompt/tg_3s`, `remote` — `usage` с токенами; `server_logs` age скрывать когда статично

## 4. Local usage для whole-chapter
- [x] 4.1 Прокинуть `LocalOpenAIBackend.set_usage_sink` на whole-chapter `B3` в `v4_phase12_strict_run.py` — писать `usage.ndjson` из `server_logs` (без токенизатора)

## 5. Проверка
- [x] 5.1 `openspec validate --strict` проходит
- [x] 5.2 Ручная проверка монитора на `local` и `remote` прогоне 31 главы — компактный вывод
