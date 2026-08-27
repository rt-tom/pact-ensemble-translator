## Purpose

Батчевый LLM-резолвер канонических русских форм имён для `glossary.json`, построенный на `B1.2` сущностях и финальном переводе главы, с детерминированной валидацией и существующим промоут-гейтом.

## ADDED Requirements

### Requirement: Glossary candidates come from validated B1.2 records
Система SHALL брать кандидатов только из `ChapterEntityContext` записей, прошедших `validate_entity_context` (все 8 проверок), где `anchor.status=verified` и `canonical_type` в `anchor.span`, все `aliases[].status=verified` и surface в своём `pid`; `glossary_worthy` — модельный `advisory bool` (отдельное поле, не статус), финальный гейт — код: `title-case`, не `EN_STOP` (проверка English, а не `RU_STOP`), поверхность `entity`/`aliases` word-boundary в `source`. `glossary_worthy=false` + код `true` → исключается (advisory не переопределяет код-отказ). Отдельное `source_aliases[]` не вводится — используется существующее `aliases[]`.

#### Scenario: Validated record becomes candidate
- **WHEN** в `entity_context` есть запись `entity="Shotgun" anchor.verified aliases[Shotgun].verified glossary_worthy=true` прошедшая валидацию
- **THEN** `Shotgun` появляется как кандидат, а `Locket` с `glossary_worthy=false` — не появляется

#### Scenario: Term not promoted
- **WHEN** `kind=term` кандидат (`door`, `said`) найден частотным сканером
- **THEN** production `book-run` его не запускает; он доступен только как `library/diagnostic API` телеметрии, никогда не резолвится LLM и не промоутится

#### Scenario: Version bump invalidates cache
- **WHEN** `EXTRACTOR_VERSION`/`prompt_version`/`CACHE_SCHEMA` изменены для `glossary_worthy`
- **THEN** старый `entity_context_cache` с прежней identity отклоняется и пересоздаётся, а не переиспользуется

### Requirement: Batched LLM resolver after repair on unified post-processing path
Система SHALL вызывать один `glossary_resolver` LLM на главу после `B3 repair/re-audit` по единому пост-процессингу пути (включая ранний `B3 cache hit`), когда in-memory `translations_repaired` готов, до `release()` рантайма, и сохранять результат в `glossary_proposals.json`.

#### Scenario: Resolver after repair
- **WHEN** `B3AuditRepair.run` сформировал in-memory `translations_repaired` (включая случай полного `cache hit`)
- **THEN** система выполняет один батчевый вызов резолвера и атомарно пишет `glossary_proposals.json` в `out_dir`

#### Scenario: Cache hit still runs resolver
- **WHEN** `B3` вернул `cache hit` и на диске уже есть валидный `entity_context_cache` и валидный sidecar
- **THEN** пост-процессинг требует `0` вызовов

#### Scenario: Cache hit with missing sidecar may restart
- **WHEN** `B3` `cache hit`, но sidecar `missing/stale/tampered`
- **THEN** система либо `acquire/restart` `reviewer` модели для одного рекомпъюта, либо `fail-closed` без промоута (конфигурируемо, default — `recompute`)

### Requirement: Evidence PID bound to candidate source
Система SHALL детерминированно вычислять `allowed_evidence_pids` для каждого кандидата как множество `source` PID, содержащих `entity` (word-boundary) или любую `VERIFIED alias` surface, и разрешать `evidence_pid` резолвера только из этого множества. Для `accepted_degraded` quarantined PIDs исключаются из `allowed` до резолвера и отклоняются при consumption в `v4_book_run`.

#### Scenario: Evidence bound prevents Babula
- **WHEN** резолвер предлагает `Roxanne→Бабуль` с `evidence_pid` где `Бабуль` есть, но `Roxanne`/`aliases` в source этого pid отсутствуют (вне `allowed`)
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
- **WHEN** sidecar — symlink, директория, не-regular, содержит `extra fields`, `duplicate ru` или ни один `surface_forms[]` вне русского текста `evidence_pid`
- **THEN** чтение отклоняет файл и не промоутит

### Requirement: Single source of truth for final text
Источником истины для резолвера и `translation_hash` SHALL быть in-memory `translations_repaired` внутри `B3`; `v4_book_run` SHALL валидировать семантический `translation_hash` (нормализованный `pid→text` без `formatting` тегов), учитывая что `formatting` может добавить `<i>` после `B3`.

#### Scenario: Hash validated despite formatting
- **WHEN** `B3` записал sidecar с `hash(translations_repaired)` и позже `formatting` добавил теги в `translations.json`
- **THEN** `v4_book_run` не считает sidecar `stale` по семантическому hash, но считает `stale` если `pid` тексты семантически изменились

### Requirement: Deterministic validation before promotion
Система SHALL отклонить предложение, если: `source` нет в `VERIFIED` контексте, `evidence_pid` вне `allowed` или quarantined, ни один `surface_forms[]` не содержится точным вхождением в русском тексте `evidence_pid`, `surface_forms→proposed_ru` лемма-связь не проходит (отдельная проверка, не `proposed_ru ∈ evidence_text`), `proposed_ru` не кириллица/в `RU_STOP`/`blocklist`, коллизия `ru VALUE` другого ключа, `duplicate ru` между разными `entity` (внутри одной `alias-group` с общим `ru` — разрешено), или `translation_hash` sidecar не совпадает; только прошедшие уходят в `MemoryManager.add_observation("glossary")`.

#### Scenario: Alias group allowed duplicate
- **WHEN** `entity="Dowght"` с алиасами `["Craig Dowght","C. Dowght"]` резолвит `Даут` для всех трёх surface
- **THEN** один `canonical` `Dowght→Даут` промоутится, алиасы не становятся отдельными ключами и не конфликтуют

#### Scenario: Cross-entity duplicate blocked
- **WHEN** `Dowght→Даут` уже в `glossary` и другой `entity` предлагает `Dowghty→Даут` как отдельную сущность
- **THEN** второе предложение уходит в `conflicts`

### Requirement: Canonical English key selection is deterministic
Система SHALL выбирать один canonical English ключ для алиас-группы по приоритету: 1) существующий `glossary` ключ среди `entity`/`aliases` (если уже есть `Dowght`), 2) валидированная `canonical surface` из `B1.2` (entity с наименьшим `pid`), 3) `EntityRecord.entity`. Другие surface алиас-группы не становятся отдельными `glossary` ключами.

#### Scenario: Existing key wins
- **WHEN** entity `Craig Dowght` с алиасами `[C. Dowght, Dowght]` и в `glossary` уже есть `Dowght→Даут`
- **THEN** canonical остаётся `Dowght`, а не `Craig Dowght`

#### Scenario: No existing key uses B1.2 canonical
- **WHEN** entity `Leonard Harlan` без существующего ключа
- **THEN** canonical выбирается как `EntityRecord.entity` (`Leonard Harlan`)

### Requirement: Glossary resolver mode is identity-bearing
Система SHALL поддерживать `glossary_resolver_mode = off | shadow | promote` (в `config_identity`). `off` — резолвер не вызывается и новые `glossary` observations запрещены. `shadow` — sidecar пишется и логируется, промоут не вызывается. `promote` — полный путь. Отката к детерм. `align_candidates` как автоматического `rollback` SHALL NOT быть.

#### Scenario: Shadow logs without promotion
- **WHEN** `mode=shadow` и sidecar валиден с `Pauz→Пауз`
- **THEN** proposal логируется, но `add_observation` не вызывается и `glossary.json` не меняется

#### Scenario: Off forbids observations
- **WHEN** `mode=off`
- **THEN** `glossary_proposals.json` не читается и любые новые `glossary` observations от `B3` отклоняются

### Requirement: Model binding and failure semantics
Система SHALL переиспользовать `reviewer` транспорт без новой роли: primary `russian_selector`, fallback `fidelity_reviewer`, далее `qwen_audit` для `local`; если ни одного `reviewer` binding нет — резолвер не вызывается (`fail-closed`, sidecar не пишется, глава не падает). Наследует `temperature 0`, `seed` детерм., `reasoning` как у `reviewer` (low), `structured_output mode prompt_only`, `bounded output budget ~3072` (достаточно для батча `~1k` content, effectively unlimited для этой роли). `response_schema=glossary_proposal/v1` (strict). `1 logical batch` = `≤3` transport attempts (bounded JSON retry). При `failure` — `LOG warning`, sidecar не пишется (не кэшируемый `failed`), глава не падает, `promotion fail-closed`; `≤1` logical batch/главу при непустом candidate set (`off`/нет кандидатов/валидный sidecar → `0` вызовов). Каждый `attempt` — в `usage.ndjson` (label `glossary_resolver`), `backend events`, `phase_progress`.

#### Scenario: Resolver failure is fail-closed
- **WHEN** резолвер вернул `truncated`/`invalid` после `3` попыток
- **THEN** `glossary_proposals.json` не пишется (не кэшируемый `failed`), глава завершается, `book_run` не промоутит, `phase_progress` показывает `glossary_resolver failed`, следующий `resume` повторяет вызов

### Requirement: No impact on generation contract
Вызов резолвера SHALL NOT менять `whole-chapter` `{pid: translation}` контракт и SHALL NOT затрагивать `journal.ndjson`/`entity_context_cache.json` identity.

#### Scenario: Generation unchanged
- **WHEN** `glossary_resolver_mode=promote`
- **THEN** `generation` валидация принимает только `{pid: translation}` без дополнительных ключей

### Requirement: Fidelity lint is pair-based on proposals
Система SHALL предоставлять `pact-fidelity-lint` проверку пары `proposed_ru + evidence surface_forms[]` по `glossary_proposals.json` / `resolver fixtures` / `validation report` (не по плоскому `glossary.json` `{en:ru}` без `evidence`); суффикс `а/я/у/ю/ом` и транслит `H→Х` SHALL NOT быть hard rule, `Бабуль`-блоклист — только временный incident regression test на proposals.

#### Scenario: Pair-based lint
- **WHEN** `glossary_proposals.json` содержит `Roxanne→Роксанна` с `surface_forms [Роксанна]`
- **THEN** lint проходит, а `Roxanne→Бабуль` с `surface_forms [Бабуль]` падает только по blocklist, не по суффиксу `а`

#### Scenario: Suffix not hard
- **WHEN** proposals содержат `Sandra→Сандра` и `Roxanne→Роксанна`
- **THEN** lint не падает на окончании `а`
