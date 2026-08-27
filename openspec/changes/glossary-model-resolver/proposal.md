## Why

Глоссарий `glossary.json` — единственный обязательный источник канонических переводов имён. Текущий детерминированный резолвер (`align_candidates`, 0 вызовов модели) систематически ошибается: из 4 автокоммитов в успешных главах 18,20-24,27,28 правильно только 2 (`Leanne→Лианн`, `Pauz→Пауз`), 2 — падежная форма (`Dionysus→Диониса`, `Dowght→Доутов`). Тот же механизм породил критические ошибки сида `Roxanne→Бабуль`, `Herb→Минни` (подмена), `Christoff→Кристоффа` и сломал прозвища `Shotgun→null` / `Knights→null` из-за `lowercase_stems` фильтра и выбора самой частой поверхности вместо леммы. Многословные имена (`Leonard Harlan`, `Knights of the Basement`) не поддерживаются. При этом `entity prepass B1.2` (LLM, source-only) уже корректно находит те же имена.

Дополнительно ревью выявил дыры в `provenance`/`resume`: `evidence_pid` не привязан к источнику, `sidecar` identity слабая, `B3` ранний `cache hit` не определён, спецификация не совпадает с `EntityRecord` контрактом, финальный текст версии не определён, `shadow` одним `bool` не выражается, `fidelity lint` по суффиксам математически некорректен, алиасы конфликтуют с запретом `ru←[en]`, модель/retry/failure не определены, тестовый план не покрывает `persistent-data` границу.

## What Changes

* Заменить детерминированный `align_candidates` для имён на гибрид: `B1.2 VERIFIED` записи (прошедшие `validate_entity_context`) как источник кандидатов + один батчевый LLM-резолвер канонической формы (именительный) по in-memory `translations_repaired` (единственный источник истины внутри `B3`).
* Резолвер выполняется внутри `B3AuditRepair.run` после `repair/re-audit` по единому пост-процессингу пути (включая `cache hit`), до `release()` runtime — fresh path без дополнительного switch, `cache hit` с валидным sidecar `0` вызовов, иначе `acquire/restart` разрешён либо `fail-closed`; результат — `glossary_proposals.json` (identity-bound sidecar, атомарная запись, строгая валидация).
* `B1.2` промпт расширить `glossary_worthy` (model gate, `false` — финальный `veto` даже при проходе кодовой проверки) c использованием существующего `aliases[]` (новое поле не вводится), bump `EXTRACTOR_VERSION`/`prompt_version`/`CACHE_SCHEMA`; кандидат берётся только из записи, прошедшей все 8 проверок и `glossary_worthy=true`.
* Детерм. вычисление `allowed_evidence_pids` из source PID, содержащих `entity` или VERIFIED `aliases`; `evidence` только из этого множества; для `accepted_degraded` quarantined PID исключаются до резолвера и отклоняются при consumption.
* `Sidecar` identity: `chapter_id`, `snapshot_hash`, `config_identity`, `resolver_version`, `prompt_version`, `response_schema`, `model_ref`/`backend identity`, hash упорядоченного `candidate input`, hash `translations_repaired` (in-memory). Строгая проверка: regular non-symlink, точная схема/ключи, размеры, отсутствие duplicate, повторная проверка перед промоутом; `cache hit` + missing/stale/tampered sidecar → рекомпъют/ignore по единому пути.
* `v4_book_run` после `terminal_status` читает sidecar и применяет существующий `quarantine/promotion` gate (никакой прямой записи в `glossary.json` из модели); валидирует семантический hash перевода с учётом последующего `formatting`.
* Отключить авто-промоут `kind=term` (generic terms только телеметрия), оставить только `proper_name` через новый путь.
* Заменить suffix/translit hard lint на `blocklist` как incident regression + проверку пары `proposed_ru + evidence surface forms` с fixture-набором.
* Ввести `glossary_resolver_mode = off | shadow | promote` и `glossary_resolver_cache_miss_policy = recompute | fail_closed` (оба identity-bearing); `off` запрещает новые observations, отката к небезопасному `align` нет; `recompute` разрешает `acquire/restart` при `cache hit` с `missing/stale` sidecar.
* Переиспользовать существующие параметры `reviewer` роли (`russian_selector` primary, fallback `fidelity_reviewer` → `local Qwen / remote Luna`, его `max_output_tokens`/`reasoning`/`temperature`/`prompt_only` как есть, без отдельного `3072` — его `content`-оценка `~1k` без учёта `reasoning 8192` привела бы к `truncated`) для резолвера — `1 logical batch = N transport attempts` (bounded JSON retry), `failure → fail-closed promotion, глава не падает`, запись в `usage.ndjson`/`backend events`/`phase_progress`.

## Capabilities

### New Capabilities
- `glossary-model-resolver`: батчевый LLM-резолвер канонических русских форм имён из `B1.2` сущностей и `translations_repaired` с детерминированной валидацией и промоутом

### Modified Capabilities
- (none — существующий `glossary.json` контракт не меняется, меняется источник предложений)

## Impact

* Код: `pact_v4/audit/entity_extractor.py` (промпт, версия), `pact_v4/pipeline/b3_audit_repair.py` (резолвер на `reviewer` параметрах, sidecar, post-processing), `pact_full_pipeline_runner_v1/v4_book_run.py` (чтение sidecar), `pact_v4/phase1/glossary_candidates.py` (депрекейт `align` для имён), `tools/pact_fidelity_lint`.
* Рантайм: +1 батчевый вызов/главу на `reviewer` транспорте (батч 5-15 сущностей) с его существующим бюджетом (`reasoning+content` уже покрывает аудит `~3.6k` входа), `local Qwen` остаётся резидентным на fresh path / `acquire/restart` на cache-miss, `remote` — no-op `release()`.
* Данные: формат `glossary.json` без изменений; новый артефакт `glossary_proposals.json` в `out_dir` (persistent-data boundary).
