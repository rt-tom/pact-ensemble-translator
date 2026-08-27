## Context

См. `proposal.md — Why`. `glossary.json` — единственный `locked` источник (всё в промпт как обязательно). `B1.2` (`pact_v4/audit/entity_extractor.py`, `Qwen`, source-only, `source_hash+extractor_version` кэш) даёт `ChapterEntityContext` с сущностями до перевода. После `whole-chapter` + `R-audit/repair/re-audit` (`B3AuditRepair`) финальный in-memory `translations_repaired` готов. Текущий `glossary_observations_from_entity_context` для однословных имён зовёт детерминированный `align_candidates` (счёт заглавных, `lowercase_stems`, `most frequent surface`). Он ломается на прозвищах-нарицательных и падежах. Ревью 26.08 выявил дыры в `evidence` привязке, `sidecar` identity/resume, контракте `EntityRecord`, версии финального текста, `shadow` флаге, lint, алиасах, модели.

## Goals / Non-Goals

**Goals:**
* Канонический `Именительный` для имён (`Дионис` не `Диониса`, `Роксанна` не `Бабуль`)
* Поддержка многословных (`Knights of the Basement`) с фразой целиком
* Фильтр ложных `Locket/Driver` на источнике
* `evidence_pid` детерм. привязан к source-кандидату, quarantined исключён
* Полный `sidecar` identity + resume (включая `B3 cache hit`, атомарность, TOCTOU)
* Один батч-вызов/главу, без нового старта `local` сервера, без трогания `whole-chapter` контракта
* Модель ничего не пишет в память — только предлагает, код валидирует `provenance`/коллизии
* Identity-bearing `off|shadow|promote` без отката к небезопасному `align`

**Non-Goals:**
* Авто-промоут generic terms (`door→дверь`) — телеметрия
* Переписывание `glossary.json` формата или `MemoryManager` контракта
* Отдельный сервис/очередь вне пайплайна
* Hard suffix-lint `а/я/у/ю` как детектор падежа

## Decisions

**D1. Источник — B1.2 записи, прошедшие `validate_entity_context`, не `EntityRecord.status`.** У `EntityRecord` нет `status`; статусы у `anchor/aliases/claims`. Кандидат берётся из записи, где `anchor` `verified`, `canonical_type` в `anchor.span`, все `aliases` с `verified` и surface в своём PID. `glossary_worthy` — модельный `advisory bool` в промпте, код валидирует: `title-case`, не `RU_STOP`, поверхность есть в source; mismatch → `conflict`. Бамп `EXTRACTOR_VERSION`, `prompt_version`, `CACHE_SCHEMA` — старый кэш несовместим. Альтернатива — оставить `freq` скан — отклонена (шум `said/looked`).

**D2. Резолвер — отдельный батчевый LLM после `repair/re-audit` по единому пост-процессингу пути.** Точка — сразу после формирования `translations_repaired` (in-memory), до `_write_translations` и до `release()`. `local Qwen` ещё резидент, `remote` no-op. Путь выполняется и при `B3` раннем `cache hit` (`b3_audit_repair.py:3769`): `_run_impl` возвращает кэш, пост-процессинг всё равно запускает резолвер/sidecar логику. Альтернативы `whole-chapter generation` (ломает `{pid:tr}`) и `v4_book_run` (сервер уже закрыт) — отклонены.

**D3. Evidence привязка.** Для каждой сущности `allowed_evidence_pids = {pids where source contains entity (word-boundary) or VERIFIED alias surface}`. `evidence_pid` из резолвера обязан быть в этом множестве. Для `accepted_degraded` quarantined PIDs исключаются из `allowed` до резолвера; при consumption в `v4_book_run` любой `evidence` с quarantined pid → `conflict`. `chunk_id` гейт недостаточен при `multiple evidence` — используется `pid`-точный.

**D4. Sidecar identity и resume.** `glossary_proposals.json` атомарно `tmp+rename`, пишется только как `regular non-symlink`. Identity поля: `schema=glossary-proposal/v1`, `chapter_id`, `snapshot_hash`, `config_identity`, `resolver_version`, `prompt_version`, `response_schema`, `model_ref`+`backend identity`, `candidate_input_hash` (hash упорядоченного входа `[{source, aliases, anchor_pid}]`), `translation_hash` (hash `translations_repaired` in-memory, см. D6). Валидация при чтении: тип файла, точная схема/ключи, размеры, отсутствие `duplicate entries`/`duplicate ru`, повторная `provenance` проверка перед промоутом. При `cache hit` + `missing/stale/tampered` sidecar → детерм. рекомпъют (один `resolver` вызов) по тому же пути; `stale` = любой hash mismatch.

**D5. Финальный текст — in-memory `translations_repaired`.** `B3` не имеет окончательного `translations.json` на диске (его пишет `v4_phase12_strict_runner` после `B3`). Источник истины для резолвера и `sidecar` — in-memory `translations_repaired`. `book_run` валидирует семантический `translation_hash` (нормализованный `pid→text` без `formatting` тегов) с учётом последующего `formatting` — `formatting` может добавить `<i>` но не менять `proposed_ru`.

**D6. Mode.** `glossary_resolver_mode = off | shadow | promote` (identity-bearing, в `config_identity`). `off` — резолвер не вызывается, новые `glossary` observations запрещены. `shadow` — sidecar пишется и логируется, `v4_book_run` не вызывает `add_observation`. `promote` — полный путь. Отката к `align_candidates` как `automatic rollback` нет (небезопасен). `default` `off` → `shadow` (5 глав) → `promote`.

**D7. Алиасы.** Только один `canonical English key` становится `glossary` ключом; `source_aliases[]`/`aliases[]` (`Craig Dowght`, `C. Dowght`, `Dowght`) — не отдельные ключи, а `evidence` для одного `Dowght→Даут`. Дубликат `ru` внутри одной `alias-group` разрешён и не считается конфликтом; между разными `entity` записями `ru←[en]` остаётся `conflict`.

**D8. Модель/ретрай/фейл.** Отдельная роль `glossary_resolver` (`runtime_config.py`, `ROLE_GLOSSARY_RESOLVER`), `local` → `qwen_audit`, `remote` → `russian_selector` (Luna), `composite` — fallback как у остальных роles. `max_tokens 1536`, `temperature 0`, `reasoning 0`, `response_schema glossary_proposal/v1` (strict). `1 logical batch` = `≤3` transport attempts (bounded JSON retry, exponential backoff). `Failure` → `LOG warning`, `sidecar` не пишется/помечается `failed`, глава не падает, `promotion fail-closed` (0 наблюдений). Каждый `attempt` пишется в `usage.ndjson` (label `glossary_resolver`), `backend events`, `phase_progress`.

**D9. Term — отключить.** `generic term` (`door→дверь`) остаётся телеметрией в `glossary_candidates.json`, не резолвится.

**D10. Lint.** Убрать suffix `а/я/у/ю/ом/ем` и транслит `H→Х` из hard lint. `Бабуль`-блоклист — только `incident regression test` (временный fixture). Основная проверка — пара `proposed_ru + evidence surface forms` с `fixture-набором` (`Роксанна/Херб/Дионис` pass, `Кристоффа/Диониса/Бабуль` fail).

## Risks / Trade-offs

* [Галлюцинация `proposed_ru` с валидным `evidence pid` другого персонажа] → `allowed_evidence_pids` + проверка `proposed_ru` содержится в `evidence pid` русском тексте.
* [Падеж проскочит] → промпт `номинатив`, `surface_forms[]` проверка, `lint` по паре, не по суффиксу.
* [Многословные — разнобой] → модель возвращает фразу целиком, `evidence_windows` + `phrase` проверка.
* [Крэш между `B3 cache` и `sidecar`] → единый пост-процессинг, атомарная запись, при следующем `resume` — рекомпъют.
* [Лишний вызов] → батч `5-15` сущ., `~400` tok, `local` без рестарта.
* [B1.2 пропустит редкое имя] → `≥1` вхождение, `glossary_worthy` не повышает порог.

## Migration Plan

1. Shadow (`mode=shadow`) на 5-10 глав: `sidecar` пишется, `v4_book_run` логирует без промоута, метрика `precision` (`50%→>90%`).
2. Включить `promote` за флагом (identity-bearing). Депрекейт `align` ветки для `proper_name`.
3. Rollback: `mode=off` (no-op), не `align`.

## Open Questions

* Точный `max_tokens` резолвера (`1536` vs `2048`) — подобрать на 3 главах без truncation.
