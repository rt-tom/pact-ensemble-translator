## Context

См. `proposal.md — Why`. Сейчас `glossary.json` — единственный `locked` источник (всё в промпт как обязательно). `B1.2` (`pact_v4/audit/entity_extractor.py`, `Qwen`, source-only, `source_hash+extractor_version` кэш) даёт `ChapterEntityContext` с `VERIFIED` сущностями до перевода. После `whole-chapter` + `R-audit/repair/re-audit` (`B3AuditRepair`) текст финальный (`translations_repaired`). Текущий `glossary_observations_from_entity_context` (`b3_audit_repair.py:472`) для однословных имён зовёт детерминированный `align_candidates` (счёт заглавных кириллических слов, `lowercase_stems` фильтр, `most frequent surface`). Он ломается на прозвищах-нарицательных и падежах.

## Goals / Non-Goals

**Goals:**
* Каноническая форма `Именительный падеж` для имён ( `Дионис` не `Диониса`, `Роксанна` не `Бабуль` )
* Поддержка многословных имён (`Knights of the Basement`, `Leonard Harlan`)
* Фильтр ложных `proper_name` (`Locket`, `Driver`) на источнике
* Один батч-вызов/главу, без нового старта `local` сервера, без вмешательства в `whole-chapter` JSON-контракт
* Модель ничего не пишет в память — только предлагает, код валидирует `provenance` и коллизии

**Non-Goals:**
* Авто-промоут generic terms (`door→дверь`) — остаётся телеметрией
* Переписывание `glossary.json` формата или `MemoryManager` контракта
* Отдельный сервис/очередь предложений вне пайплайна

## Decisions

**D1. Источник кандидатов — B1.2 VERIFIED entities, не частотный скан.** Причина: LLM уже отличает `Shotgun-человек` vs `shotgun-ружьё` и находит `Knights/Teddy` с `1` вхождением. Альтернатива — оставить `generate_candidates` (freq) — отклонена: шум `said/looked` и пропуск редких имён.

**D2. Резолвер — отдельный батчевый LLM после `repair/re-audit` внутри `B3AuditRepair.run`.** Причина: есть финальный русский текст, `VERIFIED` контекст, `local Qwen` ещё резидент (`R → audit → repair`), `remote` — `release()` no-op (`runtime_coordinator.py:448`), teardown только в `v4_phase12_strict_runner.py:4120`. Альтернативы: `whole-chapter generation` (риск сломать `{pid:tr}`) и `v4_book_run` после возврата (локальный сервер уже закрыт → рестарт) — отклонены.

**D3. Промпт B1.2 расширить `glossary_worthy: bool` + `source_aliases[]`.** Причина: `Locket/Driver` отсекаются на источнике без русского текста, `Knights of the Basement` попадает целиком. Альтернатива — фильтровать постфактум — оставляет шум в резолвере.

**D4. Sidecar `glossary_proposals.json` (identity = `source_hash+entity_context_hash+translation_hash`).** Причина: не трогает `journal`/`translations` артефакты, `v4_book_run` читает его только после `terminal_status complete/accepted_degraded` и уже применяет существующий `quarantine/promotion` gate (идемпотентно). Альтернатива — прямая запись в `glossary_candidates.json` из `B3` — нарушает слойность (B3 не владелец ledger).

**D5. Код-валидация до промоута.** Проверки: `source` есть в `VERIFIED` контексте, `evidence pid` существует, `proposed_ru` кириллица и не `RU_STOP`, не `Бабуль`-блоклист, не `VALUE` другого ключа, транслит `H→Х` sanity, коллизия `Дубль ru←[en]` → `conflict`. Только после — `MemoryManager.add_observation("glossary")`.

**D6. Term-кандидаты — отключить промоут.** Причина: частота ≠ термин (см. `HANDOFF_GLOSSARY 1.5` — `545` кандид. на 3 главы). Альтернатива — оставить `align` для `term` — сохраняет `50%` ошибок.

## Risks / Trade-offs

* [LLM галлюцинация имени в `proposed_ru`] → Mitigation: `evidence pid` обязан содержать `proposed_ru` поверхность, `source` provenance, `quarantine` gate.
* [Падежная форма проскочит] → Mitigation: промпт требует `номинатив`, `pact-fidelity-lint` ловит `а/у/ом` хвост + стемминг-валидация, `share` не нужен — LLM даёт лемму.
* [Лишний вызов/главу] → Mitigation: батч `5-15` сущностей, `~300` токенов, `local` без рестарта.
* [Многословные — разнобой перевода] → Mitigation: модель возвращает фразу целиком, `evidence_windows` проверяет, `target` — фраза, не слово.
* [B1.2 пропустит редкое имя] → Mitigation: `B1.2` уже `≥1` вхождение, `glossary_worthy` не повышает порог, а фильтрует ложные.

## Migration Plan

1. Деплой кода без изменения `glossary.json` — новый `sidecar` пишется, но `v4_book_run` пока логирует `proposed` без промоута (shadow mode, 5-10 глав).
2. Включить промоут за флагом `glossary_model_resolver_enabled` (default `false` → `true` после ревью `50%` → `>90%`).
3. Депрекейт `align_candidates` ветки для `proper_name` (оставить для `term` телеметрии).
4. Rollback: флаг `false` — поведение как до изменения (детерм. `align`).

## Open Questions

* Точный `response_schema` резолвера (`glossary_proposal/v1`) — `max_tokens` `1024` vs `2048` — подобрать на 3-5 главах.
* `model_bindings` для `glossary_resolver` — reuse `russian_selector` (`Luna`) vs `entity_extractor` (`Qwen`) — сравнить `translit` качество.
