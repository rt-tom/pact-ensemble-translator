# Design — v42-monitor-compact

## Подход

### Источник правды
`phase_progress.ndjson` — единственный источник для `phase`, `chunks done/started`, `batches done`, `findings`. `audit_journal.ndjson` и `usage.ndjson` — только для fallback и для `last call`/`model activity`, не для подсчёта done. Убирает `10/7` и `3/8 vs not started`.

### Компактный вывод (6 строк)
```
[0031] 78m | Repair 3/8 (38%) | Audit 22→12 | alive 29s
R-editor 9/9 22+10 | Audit 7/7 [0,7,6,2,6,1,0] | Repair 3/8 [3/4,4/4,4/4]
Gemma 31.9 t/s batch4 583 tok | Qwen 10ch | Total 25
```
* Хедер: `elapsed` + `phase + прогресс-бар` + `alive` (для `local` — `last usage + fresh log`, для `remote` — `strict_chapter_trial_record`/`usage`)
* Вторая строка: сводка всех фаз в одну строку, прогресс-бары
* Удалить таблицу `chunks 12×3` и блок `counters` (дубль `Phase`)

### Local vs Remote
* `local`: показывать `скорость генерации` (live `eval/prompt/tg_3s` из `server_logs`), скрывать `server_logs age` когда статично не надо; `usage` с токенами — брать из `server_logs` через `set_usage_sink` для whole-chapter
* `remote`: скрывать `скорость генерации`, показывать `usage` с реальными `input/output/reasoning` из `usage.ndjson`

### Local usage для whole-chapter
Прокинуть `LocalOpenAIBackend.set_usage_sink` (уже есть для остальных фаз, `MONITOR-V2`) и на whole-chapter `B3` путь в `v4_phase12_strict_run.py`. Парсить `server_logs` (`prompt_tokens`, `n_decoded`) как сейчас для `tg_3s`, и писать в `usage.ndjson`. Без токенизатора.

## Границы
* Не меняет пайплайн, только `v4_phase_progress.py` + `v4_phase12_strict_run.py` (sink) + `monitor_pipeline.ps1` (если нужно)
* Не трогает `phase_progress.ndjson` схему

## Тесты
* Юнит: `phase_progress` с `7` чанками → монитор показывает `7/7`, не `10/7`
* Юнит: `local` whole-chapter пишет `usage` с `in>0 out>0`
* Ручная проверка: `local` 31 глава и `remote` 31 глава — компактный вывод 6 строк в обоих режимах
