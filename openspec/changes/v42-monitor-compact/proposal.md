## Why

Монитор прогона `v4_phase_progress` врёт и раздут (30 строк):

* `Chapter audit: 10/7 done=10` — `audit_journal.ndjson` (10 стартов) + `phase_progress.ndjson` (7 чанков) мешаются, done > total
* `Selective repair: 3/8` в шапке vs `not started` в `counters` и `whole_chapter → not_started` в таблице
* Таблица `chunks 12×3` показывает 12 `pending` при реальных `audit 7` / `R-editor 9`
* `local` whole-chapter `B3` не пишет `usage.ndjson` (`in=0 out=0`), хотя `server_logs` уже содержит `n_decoded`/`prompt` — токены можно взять оттуда без токенизатора (вариант 1, владелец одобрил)

На `remote` те же баги + другие строки: `alive` по `strict_chapter_trial_record`, `usage` с реальными токенами, `server_logs` статичны — сейчас монитор не различает режимы явно.

## What Changes

* Починить источник правды: `phase_progress.ndjson` — единственный источник для `chunks done/started`, `batches done`, `phase`; `audit_journal` — только fallback
* Сжать вывод: 6 строк вместо 30 — хедер + одна строка активной фазы, убрать `chunks` таблицу и дубли `counters`/`usage by step`
* Развести `local`/`remote`: `local` — показывать live `eval/prompt/tg_3s`, `remote` — `usage` с токенами; `server_logs` age скрывать когда статично
* Прокинуть `LocalOpenAIBackend.set_usage_sink` и на whole-chapter `B3`, чтобы `local` писал `usage.ndjson` из уже парсимых `server_logs` (без токенизатора)

## Capabilities

### New Capabilities
- (none)

### Modified Capabilities
- `run-progress-monitor` — компактный и честный отчёт для `local` и `remote` прогонов
