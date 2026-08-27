## Purpose

Батчевый LLM-резолвер канонических русских форм имён для `glossary.json`, построенный на `B1.2` сущностях и финальном переводе главы, с детерминированной валидацией и существующим промоут-гейтом.

## ADDED Requirements

### Requirement: Glossary candidates come from validated B1.2 records
Система SHALL брать кандидатов только из `ChapterEntityContext` записей, прошедших `validate_entity_context` (все 8 проверок), где `anchor.status=verified` и `canonical_type` в `anchor.span`, все `aliases[].status=verified` и surface в своём `pid`, `glossary_worthy=true` (модельный advisory, валидируется кодом: `title-case`, не `RU_STOP`, поверхность есть в `source`), а не из частотного скана.

#### Scenario: Validated record becomes candidate
- **WHEN** в `entity_context` есть запись `entity="Shotgun" anchor.verified aliases[Shotgun].verified glossary_worthy=true` прошедшая валидацию
- **THEN** `Shotgun` появляется как кандидат, а `Locket` с `glossary_worthy=false` — не появляется

#### Scenario: Term not promoted
- **WHEN** `kind=term` кандидат (`door`, `said`) найден частотным сканером
- **THEN** он попадает только в `glossary_candidates.json` телеметрии, никогда не резолвится LLM и не промоутится

#### Scenario: Version bump invalidates cache
- **WHEN** `EXTRACTOR_VERSION`/`prompt_version`/`CACHE_SCHEMA` изменены для `glossary_worthy`
- **THEN** старый `entity_context_cache` с прежней identity отклоняется и пересоздаётся, а не переиспользуется

### Requirement: Batched LLM resolver after repair on unified post-processing path
Система SHALL вызывать один `glossary_resolver` LLM на главу после `B3 repair/re-audit` по единому пост-процессингу пути (включая ранний `B3 cache hit`), когда in-memory `translations_repaired` готов, до `release()` рантайма, и сохранять результат в `glossary_proposals.json`.

#### Scenario: Resolver after repair
- **WHEN** `B3AuditRepair.run` сформировал in-memory `translations_repaired` (включая случай полного `cache hit`)
- **THEN** система выполняет один батчевый вызов резолвера и атомарно пишет `glossary_proposals.json` в `out_dir`

#### Scenario: Cache hit still runs resolver
- **WHEN** `B3` вернул `cache hit` и на диске уже есть валидный `entity_context_cache`
- **THEN** пост-процессинг всё равно выполняет резолвер/sidecar логику; при `missing/stale/tampered` sidecar — рекомпъют

### Requirement: Evidence PID bound to candidate source
Система SHALL детерминированно вычислять `allowed_evidence_pids` для каждого кандидата как множество `source` PID, содержащих `entity` (word-boundary) или любую `VERIFIED alias` surface, и разрешать `evidence_pid` резолвера только из этого множества. Для `accepted_degraded` quarantined PIDs исключаются из `allowed` до резолвера и отклоняются при consumption в `v4_book_run`.

#### Scenario: Evidence bound prevents Babula
- **WHEN** резолвер предлагает `Roxanne→Бабуль` с `evidence_pid` где `Бабуль` есть, но `Roxanne` в source этого pid отсутствует
- **THEN** предложение отклоняется как `conflict` (evidence вне `allowed`)

#### Scenario: Quarantined evidence excluded
- **WHEN** глава `accepted_degraded` и `evidence_pid` входит в quarantined множество
- **THEN** proposal с этим PID исключается до резолвера и отклоняется при чтении sidecar, даже если `proposed_ru` кириллица

### Requirement: Resolver returns canonical nominative
Резолвер SHALL для каждой сущности вернуть `proposed_ru` в именительном падеже (лемма), `surface_forms[]` как в переводе, `evidence_pid` из `allowed`, `type` (`person/place/group/nickname`), `confidence`, `decision accept/reject`.

#### Scenario: Case canonicalization
- **WHEN** в `evidence_pid` встречается `Диониса`, `Сандре`, `Завоевателю`
- **THEN** `proposed_ru` равно `Дионис`, `Сандра`, `Завоеватель`

#### Scenario: Multi-word phrase kept whole
- **WHEN** `entity="Knights of the Basement"` и в `evidence` `Рыцари Подвала`
- **THEN** `proposed_ru` равно `Рыцари Подвала` целиком

#### Scenario: Nickname common noun resolved
- **WHEN** `entity="Shotgun"` и в переводе `Дробовик`
- **THEN** `proposed_ru` равно `Дробовик`, несмотря на строчную `дробовика` в других пидах

### Requirement: Sidecar identity and strict validation
Система SHALL писать `glossary_proposals.json` атомарно (`tmp+rename`) только как `regular non-symlink` с точной схемой `glossary-proposal/v1`, полями `chapter_id`, `snapshot_hash`, `config_identity`, `resolver_version`, `prompt_version`, `response_schema`, `model_ref`/`backend identity`, `candidate_input_hash`, `translation_hash` (hash in-memory `translations_repaired`), `proposals[]`; при чтении валидировать тип файла, схему/ключи, размеры, отсутствие `duplicate entries`/`duplicate ru`, и повторно проверять `provenance` перед промоутом. `stale` = любой hash mismatch.

#### Scenario: Stale sidecar recomputed
- **WHEN** `candidate_input_hash` или `translation_hash` sidecar не совпадает с текущим
- **THEN** sidecar считается `stale` и пересоздаётся одним resolver вызовом

#### Scenario: Tampered sidecar rejected
- **WHEN** sidecar — symlink, директория, не-regular, содержит `extra fields`, `duplicate ru` или `proposed_ru` вне `evidence` текста
- **THEN** чтение отклоняет файл и не промоутит

### Requirement: Single source of truth for final text
Источником истины для резолвера и `translation_hash` SHALL быть in-memory `translations_repaired` внутри `B3`; `v4_book_run` SHALL валидировать семантический `translation_hash` (нормализованный `pid→text` без `formatting` тегов), учитывая что `formatting` может добавить `<i>` после `B3`.

#### Scenario: Hash validated despite formatting
- **WHEN** `B3` записал sidecar с `hash(translations_repaired)` и позже `formatting` добавил теги в `translations.json`
- **THEN** `v4_book_run` не считает sidecar `stale` по семантическому hash, но считает `stale` если `pid` тексты семантически изменились

### Requirement: Deterministic validation before promotion
Система SHALL отклонить предложение, если: `source` нет в `VERIFIED` контексте, `evidence_pid` вне `allowed` или quarantined, `evidence` текст не содержит `proposed_ru`/`surface`, `proposed_ru` не кириллица/в `RU_STOP`, коллизия `ru VALUE` другого ключа, `duplicate ru` между разными `entity` (внутри одной `alias-group` с общим `ru` — разрешено), или `translation_hash` sidecar не совпадает; только прошедшие уходят в `MemoryManager.add_observation("glossary")`.

#### Scenario: Alias group allowed duplicate
- **WHEN** `entity="Dowght"` с алиасами `["Craig Dowght","C. Dowght"]` резолвит `Даут` для всех трёх surface
- **THEN** один `canonical` `Dowght→Даут` промоутится, алиасы не становятся отдельными ключами и не конфликтуют

#### Scenario: Cross-entity duplicate blocked
- **WHEN** `Dowght→Даут` уже в `glossary` и другой `entity` предлагает `Dowghty→Даут` как отдельную сущность
- **THEN** второе предложение уходит в `conflicts`

### Requirement: Glossary resolver mode is identity-bearing
Система SHALL поддерживать `glossary_resolver_mode = off | shadow | promote` (в `config_identity`). `off` — резолвер не вызывается и новые `glossary` observations запрещены. `shadow` — sidecar пишется и логируется, промоут не вызывается. `promote` — полный путь. Отката к детерм. `align_candidates` как автоматического `rollback` SHALL NOT быть.

#### Scenario: Shadow logs without promotion
- **WHEN** `mode=shadow` и sidecar валиден с `Pauz→Пауз`
- **THEN** proposal логируется, но `add_observation` не вызывается и `glossary.json` не меняется

#### Scenario: Off forbids observations
- **WHEN** `mode=off`
- **THEN** `glossary_proposals.json` не читается и любые новые `glossary` observations от `B3` отклоняются

### Requirement: Model binding and failure semantics
Система SHALL использовать отдельную роль `glossary_resolver` (`local→qwen_audit`, `remote→russian_selector/Luna`, `composite` fallback как у остальных), `max_tokens 1536`, `temperature 0`, `reasoning 0`, `response_schema glossary_proposal/v1`, bounded retry `≤3` transport attempts как `1 logical batch`, при `failure` глава не падает, promotion `fail-closed` (0 наблюдений), каждый `attempt` пишется в `usage.ndjson` (`glossary_resolver`), `backend events`, `phase_progress`.

#### Scenario: Resolver failure is fail-closed
- **WHEN** резолвер вернул `truncated`/`invalid` после `3` попыток
- **THEN** `glossary_proposals.json` помечается `failed`, глава завершается, `book_run` не промоутит, `phase_progress` показывает `glossary_resolver failed`

### Requirement: No impact on generation contract
Вызов резолвера SHALL NOT менять `whole-chapter` `{pid: translation}` контракт и SHALL NOT затрагивать `journal.ndjson`/`entity_context_cache.json` identity.

#### Scenario: Generation unchanged
- **WHEN** `glossary_resolver_mode=promote`
- **THEN** `generation` валидация принимает только `{pid: translation}` без дополнительных ключей

### Requirement: Fidelity lint is pair-based
Система SHALL предоставлять `pact-fidelity-lint` проверку пары `proposed_ru + evidence surface forms` с fixture-набором (`Роксанна/Херб/Дионис` pass, `Кристоффа/Диониса/Бабуль` fail); суффикс `а/я/у/ю/ом` и транслит `H→Х` SHALL NOT быть hard rule, `Бабуль`-блоклист — только временный incident regression test.

#### Scenario: Pair-based lint
- **WHEN** `glossary.json` содержит `Roxanne→Роксанна` с evidence `Роксанна`
- **THEN** lint проходит, а `Roxanne→Бабуль` с evidence `Бабуль` падает только по blocklist, не по суффиксу `а`

#### Scenario: Suffix not hard
- **WHEN** `glossary.json` содержит `Sandra→Сандра` и `Roxanne→Роксанна`
- **THEN** lint не падает на окончании `а`
