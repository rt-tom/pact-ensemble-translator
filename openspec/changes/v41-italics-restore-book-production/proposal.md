## Why

В v4 курсив (`<em>`) теряется при переводе: `SourceBlock.text` строится через
`get_text()` (срезает все теги), промпт перевода требует «plain Russian text only».
Восстановление задумано ПОСЛЕ перевода (B14 / Phase 5), но V4 **портировал только
детерминированную обёртку** (`run_formatting_align`, порт V3 `apply_inline_mappings`)
и **выбросил model-call**, который производит `target_text` (русскую подстроку для
обёртки) — V3 `formatting_messages` + `parse_format_mappings`.

Доказательство из V3 (`D:\pact\pact_translator_v3\pact_translate_v3.py`):
- `formatting_messages` (строка 2743) отдаёт модели `<SOURCE>` (английский блок) +
  `<SOURCE_SPANS>` (спаны с `source_text`) + `<TRANSLATION>` (русский перевод) и
  просит вернуть `target_text` = точную русскую подстроку для каждого спана.
  Модель НЕ меняет текст, только локализует эквивалент.
- `parse_format_mappings` (2787) валидирует `{(pid, span_id): {target_text, occurrence}}`.
- `apply_inline_mappings` (2865) ДЕТЕРМИНИРОВАННО оборачивает `target_text` в `<em>`
  (model-free обёртка, через `find_nonoverlapping_occurrence`).
- `run_formatting` (2917): только PIDs с `inline_spans`, батчами по 12, retry,
  `on_failure: omit_tag`.

V4 же в `run_formatting_align` резолвит спаны через `_resolve_deterministic`,
который ищет **английский** `span.text` в русском переводе (tiers
preserved→exact→occurrence→fuzzy). Для EN→RU английский спан не встречается в
русском → **0 совпадений → 0 `<em>`**. V3 работал, потому что модель давала
`target_text` (русскую подстроку).

Корень: card C («Phase 5 = 0 model calls») ошибочно предположил, что
детерминированных тиров достаточно для EN→RU. Для переведённой прозы они НЕ
достаточны — нужен `target_text` от модели. Эмпирика: POC на старой 31-й дал
**0/69** `<em>` при вызове `run_formatting_align` на plain-Russian переводе;
рендер глав 29/30/31 через `v4_book_html.py` дал **0 `<em>`** при **51/72/69 `<em>`**
в исходнике.

Инфраструктура §8.14 в V4 УЖЕ есть (`pact_v4/phase5/formatting.py`):
`SpanMappingRecord.translated_text`, `apply_span_mappings`, `find_nonoverlapping_occurrence`,
`occurrence_ranges` — но `run_formatting_align` заполняет `translated_text` из
результата поиска английского `span.text`, а не из model-call. Недостаёт только
шага разрешения `target_text` через модель.

## What Changes

**Возвращаем отдельный formatting model-call (как в V3, по п.2 владельца) и
оставляем обёртку детерминированной (model-free).**

1. **Новый шаг разрешения спанов (model-call)** — порт V3 `formatting_messages`/
   `parse_format_mappings` (назовём `resolve_format_mappings`). Только для PIDs,
   у которых есть `inline_spans`. Вход: английский исходник блока + спаны
   (`source_text`, обёрнутый в `<em>`) + русский перевод (plain). Выход:
   `target_text` (точная русская подстрока) и `occurrence` на спан. Модель НЕ
   меняет текст, только локализует эквивалент — это ровно п.2 владельца.
2. **Адаптируем `run_formatting_align`** (обёртка, model-free): новый необязательный
   параметр `mappings` (от `resolve_format_mappings`). При наличии — для каждого
   спана локализуем `target_text` через `find_nonoverlapping_occurrence` (уже есть в
   V4) и строим `SpanMappingRecord` для `apply_span_mappings`. При отсутствии
   `mappings` (legacy-path strict-runner, где перевод сам несёт `<em>`, tier
   `preserved`) — сохраняем текущее детерминированное поведение (`_resolve_deterministic`).
3. **Подключаем в `v4_book_run.py`** (per-chapter B7/B9): после финализации перевода
   (post-repair/edit) — `resolve_format_mappings(client, cfg, blocks, translations)`
   (model-call) → `run_formatting_align(..., mappings=...)` (apply) → перезапись
   `translations.json` отформатированным текстом (с `<em>`) + запись
   `formatting_report.json`.
4. **Перевод остаётся plain** (без тегов) до шага форматирования — ограничение
   владельца («теги в перевод писать нельзя») соблюдено; аудит/repair/интегрити не
   ломаются. Теги появляются только в финальном `translations.json` после обёртки.

**Старые главы (29/30/31):** остаются БЕЗ курсива (решение владельца, п.3). Фикс
применяется к БУДУЩИМ прогонам; существующие переводы не пересобираются.

**Политика инцидентов (решение владельца, п.2 policy=lenient_debt):** неразрешённый
обязательный спан → `FormattingIncident` (debt) в `formatting_report.json`; глава
завершается, не падает. `max_formatting_incidents` — конфиг (по умолчанию мягкий:
не блокирует главу), в отличие от strict-runner (default `0` = blocking).

## Capabilities

### New Capabilities
- (none)

### Modified Capabilities
- `book-production-formatting` — per-chapter сборка восстанавливает `<em>` из
  `inline_spans` исходника через отдельный formatting model-call (`target_text`) +
  детерминированную обёртку (Phase 5), а не теряет курсив.

## Impact

- Затрагивает: `v4_book_run.py` (новый шаг + запись артефактов); `phase5/formatting.py`
  (`resolve_format_mappings` (model-call, порт V3) + адаптация `run_formatting_align`
  под `mappings`/`target_text`); конфиг форматирования (модель/температура/батч/retry).
- Не трогает: промпт перевода («plain Russian text only» сохраняется до шага
  форматирования), схему `inline_spans`, парсер `phase0b/source_html.py`, рендер
  `v4_book_html.py` (читает уже отформатированный `translations.json`), поведение
  strict-runner (legacy-path без `mappings`).
- Риск: **Medium** (локальное поведение сборки; добавляем 1 targeted model-call на
  главы с `<em>` и меняем контракт выхода `translations.json` на tagged — отклонение
  от card C, владелец одобрил).
- **Отклонение от card C:** card C требовал «Phase 5 = 0 model calls». POC (0/69) + V3
  (работает через `target_text`) доказывают, что для EN→RU детерминированные тиры не
  дают `target_text`. Фикс возвращает targeted formatting model-call (только PIDs с em,
  маленькие батчи) — коррекция ошибочного допущения card C, одобрена владельцем (п.2).
  Обёртка (`apply_span_mappings`) остаётся model-free.
- Тесты: юнит — model-call (мок) возвращает `target_text`, `run_formatting_align`
  оборачивает русскую подстроку в `<em>`; unresolved → debt (не blocking); только
  PIDs с em получают вызов. Регрессия — plain-перевод без em не меняется; рендер даёт
  `<em>` в `book.html`.
- Отношение к `v41-literary-consistency-checks`: независимо (курсив vs
  литературная консистентность — разные оси).
