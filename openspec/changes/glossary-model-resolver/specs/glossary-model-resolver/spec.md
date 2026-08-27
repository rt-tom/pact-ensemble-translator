## Purpose

Батчевый LLM-резолвер канонических русских форм имён для `glossary.json`, построенный на `B1.2` сущностях и финальном переводе главы, с детерминированной валидацией и существующим промоут-гейтом.

## ADDED Requirements

### Requirement: Glossary candidates come from B1.2 VERIFIED entities
Система SHALL брать кандидатов для словаря только из `ChapterEntityContext.entities[]` с `status=verified` (фильтр `glossary_worthy=true` и `is_proper_noun_entity_name`), а не из частотного скана `generate_candidates`.

#### Scenario: Entity becomes candidate
- **WHEN** в `entity_context` есть `entity="Shotgun" canonical_type="father" glossary_worthy=true`
- **THEN** `Shotgun` появляется как кандидат для резолвера, а `Locket` с `glossary_worthy=false` — не появляется

#### Scenario: Term not promoted
- **WHEN** `kind=term` кандидат (`door`, `said`) найден частотным сканером
- **THEN** он попадает только в `glossary_candidates.json` телеметрии, никогда не резолвится LLM и не промоутится

### Requirement: Batched LLM resolver after repair
Система SHALL вызывать один `glossary_resolver` LLM на главу после `B3 repair/re-audit`, когда доступен `translations_repaired` и `translations.json`, до `release()` рантайма, и сохранять результат в `glossary_proposals.json` (identity = `source_hash + entity_context_hash + translation_hash`).

#### Scenario: Resolver timing
- **WHEN** `B3AuditRepair.run` завершил `repair` и `re-audit` и `translations_repaired` сформирован
- **THEN** система выполняет один батчевый вызов резолвера и пишет `glossary_proposals.json` в `out_dir`

### Requirement: Resolver returns canonical nominative
Резолвер SHALL для каждой сущности вернуть `proposed_ru` в именительном падеже (лемма), `surface_forms[]` как в переводе, `evidence_pid`, `type` (`person/place/group/nickname`), `confidence`, `decision accept/reject`.

#### Scenario: Case canonicalization
- **WHEN** в переводе встречается `Диониса`, `Сандре`, `Завоевателю`
- **THEN** `proposed_ru` равно `Дионис`, `Сандра`, `Завоеватель`

#### Scenario: Multi-word phrase
- **WHEN** `entity="Knights of the Basement"` и в переводе `Рыцари Подвала`
- **THEN** `proposed_ru` равно `Рыцари Подвала` целиком, а не `Рыцари`

#### Scenario: Nickname common noun
- **WHEN** `entity="Shotgun"` и в переводе `Дробовик`
- **THEN** `proposed_ru` равно `Дробовик`, несмотря на строчную форму `дробовика` в других пидах

### Requirement: Deterministic validation before promotion
Система SHALL отклонить предложение, если: `source` нет в `VERIFIED` контексте, `evidence_pid` отсутствует, `proposed_ru` не кириллица/в `RU_STOP`/в блоклисте (`бабуль`/`бабуля`), коллизия `ru VALUE` другого ключа, или дубль `ru←[en]`; только прошедшие уходят в `MemoryManager.add_observation("glossary")`.

#### Scenario: Blocklist prevents Babula
- **WHEN** резолвер предлагает `Roxanne → Бабуль`
- **THEN** предложение отклоняется как `conflict` и не промоутится, даже при `confidence high`

#### Scenario: Duplicate RU blocked
- **WHEN** `Dowght→Даут` уже в `glossary` и резолвер предлагает `Dowghty→Даут`
- **THEN** второе предложение уходит в `conflicts` и не перезаписывает

### Requirement: Promotion uses existing gate
Промоут SHALL идти через существующий `v4_book_run` `quarantine/promotion` gate (требует `terminal_status complete/accepted_degraded`, `no conflicts`, `single distinct target`) и писать в `glossary_candidates.json` ledger и далее в `glossary.json`.

#### Scenario: Sidecar consumed after terminal
- **WHEN** `glossary_proposals.json` существует и `chapter terminal_status` — `complete`
- **THEN** `v4_book_run` читает sidecar, валидирует и вызывает `add_observation`; при `fail_closed` — пропускает

### Requirement: No impact on generation contract
Вызов резолвера SHALL NOT менять `whole-chapter` `{pid: translation}` контракт и SHALL NOT затрагивать `journal.ndjson`/`entity_context_cache.json` identity.

#### Scenario: Generation unchanged
- **WHEN** `glossary_model_resolver_enabled=true`
- **THEN** `generation` валидация принимает только `{pid: translation}` без дополнительных ключей

### Requirement: Fidelity lint
Система SHALL предоставлять `pact-fidelity-lint` проверку глоссария на падежные хвосты (`а/я/у/ю/ом/ем`), `Бабуль`-блоклист и транслит-несоответствие (`Herb→М`).

#### Scenario: Lint catches genitive
- **WHEN** `glossary.json` содержит `Christoff→Кристоффа`
- **THEN** `pact-fidelity-lint` возвращает ошибку с указанием ключа
