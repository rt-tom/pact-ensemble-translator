## Context

См. `proposal.md — Why`. `glossary.json` — единственный `locked` источник (всё в промпт как обязательно). `B1.2` (`pact_v4/audit/entity_extractor.py`, `Qwen`, source-only, `source_hash+extractor_version` кэш) даёт `ChapterEntityContext` с сущностями до перевода. После `whole-chapter` + `R-audit/repair/re-audit` (`B3AuditRepair`) финальный in-memory `translations_repaired` готов. Текущий `glossary_observations_from_entity_context` для однословных имён зовёт детерминированный `align_candidates` (счёт заглавных, `lowercase_stems`, `most frequent surface`). Он ломается на прозвищах-нарицательных и падежах. Ревью 26.08 выявил дыры в `evidence` привязке, `sidecar` identity/resume, контракте `EntityRecord`, версии финального текста, `shadow` флаге, lint, алиасах, модели.

## Goals / Non-Goals

**Goals:**
* Канонический `Именительный` для имён (`Дионис` не `Диониса`, `Роксанна` не `Бабуль`) — модель даёт номинатив, `lemma_v1` лишь подтверждает связь `surface_forms[]→proposed_ru` (не гарантирует падеж)
* Поддержка многословных (`Knights of the Basement`) с фразой целиком
* Фильтр ложных `Locket/Driver` на источнике
* `evidence_pid` детерм. привязан к source-кандидату; `surface_forms[]` обязаны быть в `evidence` тексте, `proposed_ru` — лемма `surface_forms`
* Полный `sidecar` identity + resume (включая `B3 cache hit` с `0` вызовов при валидном sidecar, иначе `acquire/restart` или `fail-closed`)
* Один батч-вызов/главу на `reviewer` транспорте (fresh path без `switch`, cache-hit 0 при валидном sidecar)
* Модель ничего не пишет в память — только предлагает, код валидирует `provenance`/коллизии
* Identity-bearing `off|shadow|promote` + `cache_miss_policy` без отката к `align`

**Non-Goals:**
* Авто-промоут generic terms (`door→дверь`) — телеметрия
* Переписывание `glossary.json` формата или `MemoryManager` контракта
* Отдельный сервис/очередь вне пайплайна
* Hard suffix-lint `а/я/у/ю` как детектор падежа

## Decisions

**D1. Источник — B1.2 записи, прошедшие `validate_entity_context`, не `EntityRecord.status`.** У `EntityRecord` нет `status`; статусы у `anchor/aliases/claims`. Кандидат берётся из записи, где `anchor` `verified`, `canonical_type` в `anchor.span`, все `aliases` с `verified` и surface в своём `pid`; поверхность `entity`/`aliases` проверяется word-boundary в `source`. `glossary_worthy` — **model gate** (не advisory): модельный `bool` в промпте, `false` — финальный `veto` даже при проходе кодовой проверки (`title-case`, не `EN_STOP`, поверхность есть в source). Код-гейт также обязателен; оба должны пройти. Отдельное поле `source_aliases[]` не вводится — используется `aliases[]`. Бамп `EXTRACTOR_VERSION`, `prompt_version`, `CACHE_SCHEMA` — старый кэш несовместим. Risk-тезис «не повышает порог» снят: `glossary_worthy=false` повышает строгость (вето).

**D2. Резолвер — отдельный батчевый LLM после `repair/re-audit` по единому пост-процессингу пути.** Точка — сразу после формирования `translations_repaired` (in-memory), до `_write_translations` и до `release()`. Fresh path: резолвер использует уже резидентную `reviewer` модель без дополнительного `switch`. При `B3` раннем `cache hit` (`b3_audit_repair.py:3769`) пост-процессинг тоже выполняется: если sidecar валиден — `0` вызовов; если `missing/stale/tampered` — разрешён `acquire/restart` `reviewer` модели для рекомпъюта **либо** `promotion fail-closed` (конфигурируемо, default — `recompute`). Честная формулировка: `0` вызовов только при валидном sidecar, иначе допускается старт. Альтернативы `whole-chapter generation` и `v4_book_run` — отклонены.

**D3. Evidence привязка и лемматизация (versioned, link-only).** Для каждой сущности `allowed_evidence_pids = {pids where source contains entity (word-boundary) or VERIFIED alias surface}`. `evidence_pid` обязан быть в этом множестве. Plumbing: `strict runner` передаёт `quarantined_pids` в `B3` пост-процессинг; если отсутствует — резолвер по полному `allowed`, quarantine только `fail-closed` в `v4_book_run`. Проверка: `surface_forms[]` точным вхождением в русском `evidence_pid`; `proposed_ru` НЕ обязан дословно — связь `surface→lemma` — versioned token-wise `stem equivalence` (`ru_stem` + `_RU_ENDINGS`, многословные — каждый токен, порядок сохранён; `len<3` — `casefold`). `lemma_v1` подтверждает связь и защищает от `Бабуль`, но **не гарантирует** номинатив (`Диониса` стем-равно и `Дионис`, и `Диониса`); номинатив обеспечивает модель (инструкция `номинатив`) и проверяется `shadow`-метрикой/морфовалидатором. Версия входит в `resolver_version`; матрица: `Сандре→Сандра`, `Завоевателю→Завоеватель`, `дробовика→Дробовик`.

**D4. Sidecar identity и resume + cache-miss policy.** `glossary_proposals.json` атомарно `tmp+rename`, только `regular non-symlink`. Identity: `schema=glossary-proposal/v1`, `chapter_id`, `snapshot_hash`, `config_identity`, `resolver_version` (включает `lemma_v1`), `prompt_version`, `response_schema`, `model_ref`+`backend identity`, `candidate_input_hash`, `translation_hash` (in-memory `translations_repaired`). Валидация: тип, точная схема/ключи, размеры, отсутствие `duplicate entries`/`duplicate ru`, `surface_forms` в `evidence`, `provenance` повторно перед промоутом. `B3 cache hit` + `missing/stale/tampered` → по `glossary_resolver_cache_miss_policy` (`recompute | fail_closed`, identity-bearing, default `recompute`): `recompute` — один `reviewer` `acquire/restart` разрешён, `fail_closed` — `0` вызовов, без промоута. `stale` = любой hash mismatch. Политика входит в `config_identity`.

**D5. Финальный текст — in-memory `translations_repaired`.** `B3` не имеет окончательного `translations.json` на диске (его пишет `v4_phase12_strict_runner` после `B3`). Источник истины для резолвера и `sidecar` — in-memory `translations_repaired`. `book_run` валидирует семантический `translation_hash` (нормализованный `pid→text` без `formatting` тегов) с учётом последующего `formatting` — `formatting` может добавить `<i>` но не менять `proposed_ru`.

**D6. Mode.** `glossary_resolver_mode = off | shadow | promote` (identity-bearing, в `config_identity`). `off` — резолвер не вызывается, новые `glossary` observations запрещены. `shadow` — sidecar пишется и логируется, `v4_book_run` не вызывает `add_observation`. `promote` — полный путь. Отката к `align_candidates` как `automatic rollback` нет (небезопасен). `default` `off` → `shadow` (5 глав) → `promote`.

**D7. Алиасы и выбор canonical ключа.** Только один `canonical English key` становится `glossary` ключом; `aliases[]` (`Craig Dowght`, `C. Dowght`, `Dowght`) — не отдельные ключи, а `evidence` для одного. Приоритет выбора canonical (детерм.): 1) существующий `glossary` ключ среди `entity`/`aliases` (если уже есть `Dowght` → `Dowght`), 2) валидированная `canonical surface` из `B1.2` (entity с наименьшим `pid`), 3) `EntityRecord.entity`. Дубликат `ru` внутри одной `alias-group` (один `entity` + его `aliases` → один `ru`) разрешён; между разными `entity` записями `ru←[en]` остаётся `conflict`.

**D8. Модель/ретрай/фейл — reuse reviewer роли (точный binding, bounded budget).** Отдельная роль НЕ заводится; резолвер использует `reviewer` транспорт: primary `russian_selector`, fallback `fidelity_reviewer`, далее `qwen_audit` для `local`; если ни одного binding нет — `fail-closed`. Наследует `temperature 0`/`seed`/`reasoning`/`structured_output prompt_only` reviewer'а, но output budget — `min(reviewer_budget, 3072)` либо собственный override `3072` (bounded, не простое наследование — гарантирует лимит для батча `~1k`). `response_schema=glossary_proposal/v1` (strict). `1 logical batch` = `≤3` transport attempts; `≤1` batch/главу при непустом сете (`off`/пусто/валидный sidecar → `0`). `Failure` → `LOG warning`, sidecar не пишется, глава не падает, `promotion fail-closed`. Каждый `attempt` — в `usage.ndjson` (`glossary_resolver`), `backend events`, `phase_progress`.

**D9. Term — отключить в production.** Production `book-run` НЕ запускает частотный `generic term` скан; существующая `term` ветка остаётся только как `library/diagnostic API` (`glossary_candidates.py` доступен для offline телеметрии), не вызывается из `B3`/`v4_book_run` и не резолвится/не промоутится.

**D10. Lint и nominative.** Убрать suffix `а/я/у/ю/ом/ем` и транслит `H→Х` из hard lint. `Бабуль`-блоклист — только `incident regression` на `glossary_proposals.json` / `resolver fixtures` (временно). Deterministic lint проверяет только `evidence/link` (`surface_forms[]∈evidence`, `surface→lemma` `lemma_v1`, `blocklist`, `provenance`); `Кристоффа/Диониса` стем-равны `Кристофф/Дионис` и пройдут deterministic проверку — такие падежные ошибки ловятся `shadow` quality evaluation (post-factum метрика по набору, не `fail-closed` перед промоутом), либо будущим обязательным морфовалидатором (отдельная задача с алгоритмом/тестами). По одному `glossary.json` без `evidence` пару проверить невозможно.

## Risks / Trade-offs

* [Галлюцинация `proposed_ru` с валидным `evidence pid` другого персонажа] → `allowed_evidence_pids` + проверка `surface_forms[]` в `evidence` тексте + `surface→lemma`.
* [Падеж проскочит] → промпт `номинатив`, `surface_forms[]` проверка, `lint` по паре, не по суффиксу.
* [Многословные — разнобой] → модель возвращает фразу целиком, `evidence_windows` + `phrase` проверка.
* [Крэш между `B3 cache` и `sidecar`] → единый пост-процессинг, атомарная запись, при следующем `resume` — рекомпъют.
* [Лишний вызов] → батч `5-15` сущ., `~400` tok, `local` без рестарта.
* [B1.2 пропустит редкое имя / `glossary_worthy=false` veto снизит recall] → `≥1` вхождение, `veto` отслеживается shadow-метрикой `proposed vs link` , не порогом вхождения.

## Migration Plan

1. Shadow (`mode=shadow`) на 5-10 глав: `sidecar` пишется, `v4_book_run` логирует без промоута, метрика `precision` (`50%→>90%`).
2. Включить `promote` за флагом (identity-bearing). Депрекейт `align` ветки для `proper_name`.
3. Rollback: `mode=off` (no-op), не `align`.

## Open Questions

* Нет — модель/лимиты переиспользуют reviewer, отдельная настройка не требуется.
