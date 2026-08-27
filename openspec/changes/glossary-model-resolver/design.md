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

**D1. Источник — B1.2 записи, прошедшие `validate_entity_context`, не `EntityRecord.status`.** У `EntityRecord` нет `status`; статусы у `anchor/aliases/claims`. Кандидат берётся из записи, где `anchor` `verified`, `canonical_type` в `anchor.span`, все `aliases` с `verified` и surface в своём `pid`; поверхность `entity`/`aliases` проверяется word-boundary в `source`. `glossary_worthy` — модельный `advisory bool` в промпте, но финальный гейт — код: `title-case`, не `EN_STOP` (проверка English, а не `RU_STOP`), поверхность есть в source. `advisory false` + код `true` → кандидат исключается (advisory не переопределяет код-отказ), `advisory true` + код `false` → также исключается. Отдельное поле `source_aliases[]` не вводится — используется существующее `aliases[]`. Бамп `EXTRACTOR_VERSION`, `prompt_version`, `CACHE_SCHEMA` — старый кэш несовместим. Альтернатива — оставить `freq` скан — отклонена (шум `said/looked`).

**D2. Резолвер — отдельный батчевый LLM после `repair/re-audit` по единому пост-процессингу пути.** Точка — сразу после формирования `translations_repaired` (in-memory), до `_write_translations` и до `release()`. Fresh path: резолвер использует уже резидентную `reviewer` модель без дополнительного `switch`. При `B3` раннем `cache hit` (`b3_audit_repair.py:3769`) пост-процессинг тоже выполняется: если sidecar валиден — `0` вызовов; если `missing/stale/tampered` — разрешён `acquire/restart` `reviewer` модели для рекомпъюта **либо** `promotion fail-closed` (конфигурируемо, default — `recompute`). Честная формулировка: `0` вызовов только при валидном sidecar, иначе допускается старт. Альтернативы `whole-chapter generation` и `v4_book_run` — отклонены.

**D3. Evidence привязка.** Для каждой сущности `allowed_evidence_pids = {pids where source contains entity (word-boundary) or VERIFIED alias surface}`. `evidence_pid` обязан быть в этом множестве. Проверка лемматизации: `surface_forms[]` обязаны точным вхождением содержаться в русском тексте `evidence_pid`; `proposed_ru` (лемма, именительный) НЕ обязан дословно содержаться — его связь с `surface_forms` проверяется отдельной `surface→lemma` валидацией (стем/морфология), а не `proposed_ru ∈ evidence_text`. Для `accepted_degraded` quarantined PIDs исключаются из `allowed` до резолвера; при consumption любой `evidence` с quarantined pid → `conflict`. `chunk_id` гейт недостаточен — используется `pid`-точный.

**D4. Sidecar identity и resume.** `glossary_proposals.json` атомарно `tmp+rename`, пишется только как `regular non-symlink`. Identity поля: `schema=glossary-proposal/v1`, `chapter_id`, `snapshot_hash`, `config_identity`, `resolver_version`, `prompt_version`, `response_schema`, `model_ref`+`backend identity`, `candidate_input_hash` (hash упорядоченного входа `[{source, aliases, anchor_pid}]`), `translation_hash` (hash `translations_repaired` in-memory, см. D6). Валидация при чтении: тип файла, точная схема/ключи, размеры, отсутствие `duplicate entries`/`duplicate ru`, повторная `provenance` проверка перед промоутом. При `cache hit` + `missing/stale/tampered` sidecar → детерм. рекомпъют (один `resolver` вызов) по тому же пути; `stale` = любой hash mismatch.

**D5. Финальный текст — in-memory `translations_repaired`.** `B3` не имеет окончательного `translations.json` на диске (его пишет `v4_phase12_strict_runner` после `B3`). Источник истины для резолвера и `sidecar` — in-memory `translations_repaired`. `book_run` валидирует семантический `translation_hash` (нормализованный `pid→text` без `formatting` тегов) с учётом последующего `formatting` — `formatting` может добавить `<i>` но не менять `proposed_ru`.

**D6. Mode.** `glossary_resolver_mode = off | shadow | promote` (identity-bearing, в `config_identity`). `off` — резолвер не вызывается, новые `glossary` observations запрещены. `shadow` — sidecar пишется и логируется, `v4_book_run` не вызывает `add_observation`. `promote` — полный путь. Отката к `align_candidates` как `automatic rollback` нет (небезопасен). `default` `off` → `shadow` (5 глав) → `promote`.

**D7. Алиасы и выбор canonical ключа.** Только один `canonical English key` становится `glossary` ключом; `aliases[]` (`Craig Dowght`, `C. Dowght`, `Dowght`) — не отдельные ключи, а `evidence` для одного. Приоритет выбора canonical (детерм.): 1) существующий `glossary` ключ среди `entity`/`aliases` (если уже есть `Dowght` → `Dowght`), 2) валидированная `canonical surface` из `B1.2` (entity с наименьшим `pid`), 3) `EntityRecord.entity`. Дубликат `ru` внутри одной `alias-group` (один `entity` + его `aliases` → один `ru`) разрешён; между разными `entity` записями `ru←[en]` остаётся `conflict`.

**D8. Модель/ретрай/фейл — reuse reviewer роли (точный binding).** Отдельная роль НЕ заводится; резолвер использует `reviewer` транспорт: primary `russian_selector` (`Luna`/`Qwen`), fallback `fidelity_reviewer` (если `russian_selector` не задан), далее `qwen_audit` для `local`. Наследует `temperature/seed/reasoning` и `structured_output mode` reviewer'а; `max_tokens` — bounded `~3072` output budget (наследованный, effectively unlimited для батча `~1k`, без отдельного лимита). `response_schema=glossary_proposal/v1` (strict). `1 logical batch` = `≤3` transport attempts (bounded JSON retry, exponential backoff). Если ни одного `reviewer` binding нет — `fail-closed`, `sidecar` не пишется, глава не падает. `Failure` → `LOG warning`, **sidecar не пишется** (не `failed` кэшируемый файл), глава не падает, `promotion fail-closed`. Каждый `attempt` — в `usage.ndjson` (label `glossary_resolver`), `backend events`, `phase_progress`.

**D9. Term — отключить в production.** Production `book-run` НЕ запускает частотный `generic term` скан; существующая `term` ветка остаётся только как `library/diagnostic API` (`glossary_candidates.py` доступен для offline телеметрии), не вызывается из `B3`/`v4_book_run` и не резолвится/не промоутится.

**D10. Lint.** Убрать suffix `а/я/у/ю/ом/ем` и транслит `H→Х` из hard lint. `Бабуль`-блоклист — только `incident regression` на `glossary_proposals.json` / `resolver fixtures` (временно), не на `glossary.json` (`{en:ru}` без `evidence`). Основная проверка — пара `proposed_ru + evidence surface_forms[]` (из `glossary_proposals.json` или `validation report`) с fixture-набором (`Роксанна/Херб/Дионис` pass, `Кристоффа/Диониса/Бабуль` fail); по одному `glossary.json` проверить пару невозможно.

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

* Нет — модель/лимиты переиспользуют reviewer, отдельная настройка не требуется.
