## Why

Глоссарий `glossary.json` — единственный обязательный источник канонических переводов имён. Текущий детерминированный резолвер (`align_candidates`, 0 вызовов модели) систематически ошибается: из 4 автокоммитов в успешных главах 18,20-24,27,28 правильно только 2 (`Leanne→Лианн`, `Pauz→Пауз`), 2 — падежная форма (`Dionysus→Диониса`, `Dowght→Доутов`). Тот же механизм породил критические ошибки сида `Roxanne→Бабуль`, `Herb→Минни` (подмена), `Christoff→Кристоффа` и сломал прозвища `Shotgun→null` / `Knights→null` из-за `lowercase_stems` фильтра и выбора самой частой поверхности вместо леммы. Многословные имена (`Leonard Harlan`, `Knights of the Basement`) не поддерживаются. При этом `entity prepass B1.2` (LLM, source-only) уже корректно находит те же имена.

## What Changes

* Заменить детерминированный `align_candidates` для имён на гибрид: `B1.2 VERIFIED entities` как источник кандидатов + один батчевый LLM-резолвер канонической русской формы (именительный падеж) по финальному `translations_repaired`.
* Резолвер выполняется внутри `B3AuditRepair.run` после `repair/re-audit` (финальный текст готов), до `release()` runtime — без нового старта сервера; результат — `glossary_proposals.json` (identity-bound sidecar).
* `v4_book_run` после `terminal_status` читает sidecar и применяет существующий `quarantine/promotion gate` (никакой прямой записи в `glossary.json` из модели).
* Расширить `B1.2` промпт полем `glossary_worthy` и алиасами, чтобы отфильтровать `Locket/Driver`-шум на источнике.
* Отключить авто-промоут `kind=term` (generic terms только телеметрия в `glossary_candidates.json`), оставить только `proper_name` через новый путь.
* Добавить детерминированный `pact-fidelity-lint` на кириллические падежи, дубль `ru→[en]`, `Бабуль`-блоклист и транслит-чек.

## Capabilities

### New Capabilities
- `glossary-model-resolver`: батчевый LLM-резолвер канонических русских форм имён из `B1.2` сущностей и `translations_repaired` с детерминированной валидацией и промоутом

### Modified Capabilities
- (none — существующий `glossary.json` контракт не меняется, меняется источник предложений)

## Impact

* Код: `pact_v4/audit/entity_extractor.py` (промпт), `pact_v4/pipeline/b3_audit_repair.py` (новый `glossary_observations_from_entity_context` с LLM, sidecar), `pact_full_pipeline_runner_v1/v4_book_run.py` (чтение sidecar), `pact_v4/phase1/glossary_candidates.py` (депрекейт `align` для имён), `tools/pact_fidelity_lint`.
* Рантайм: +1 `Qwen/Luna` вызов/главу (батч 5-15 сущностей), `local Qwen` остаётся резидентным, `remote` — no-op `release()`.
* Данные: формат `glossary.json` без изменений; новый артефакт `glossary_proposals.json` в `out_dir`.
