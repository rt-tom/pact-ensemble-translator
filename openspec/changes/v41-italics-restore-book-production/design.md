# Design — v41-italics-restore-book-production

## Подход (подтверждён V3 + п.2 владельца)

### Корень потери италиков (скорректирован)
V4 портировал из V3 ТОЛЬКО обёртку (`run_formatting_align` ← `apply_inline_mappings`),
но выбросил model-call `formatting_messages`, который производит `target_text`.
`run_formatting_align` резолвит спаны через `_resolve_deterministic`, который ищет
**английский** `span.text` в русском переводе — для EN→RU 0 совпадений. V3 работал,
потому что модель возвращала `target_text`.

### Существующая инфраструктура V4 (повторно используем)
`pact_v4/phase5/formatting.py` уже содержит:
- `SpanMappingRecord` с полем `translated_text` (строка ~157) — запись разрешённого спана.
- `apply_span_mappings(text, records)` (636) — детерминированная обёртка: оборачивает
  локацию `record.start:end` в `<{tag} {attrs}>…</{tag}>`, wrap-only без эскейпинга (B14).
- `find_nonoverlapping_occurrence(text, needle, preferred, occupied)` (292) и
  `occurrence_ranges` (270) — уже реализованы (порт V3).
- `run_formatting_align(*, blocks, translation, backend_identity_hash, policy_version,
  max_formatting_incidents)` (689) — перебирает PIDs со `inline_spans`, резолвит через
  `_resolve_deterministic` (поиск английского `span.text`), строит `SpanMappingRecord`,
  применяет `apply_span_mappings`.

Недостаёт ТОЛЬКО шага, который производит `target_text` (русскую подстроку) через
модель — его и добавляем.

### Точка интеграции
`v4_book_run.py` (per-chapter B7/B9). После финализации перевода (post-repair/edit,
до/при записи `translations.json`):

```python
from pact_v4.phase0b.source_html import parse_source_html
from pact_v4.phase5.formatting import (
    resolve_format_mappings,   # MODEL-CALL (только PIDs с inline_spans)
    run_formatting_align,      # model-free обёртка (target_text)
)

blocks = parse_source_html(source_html_text)          # inline_spans (англ. source_text)
translations = {...}                                   # plain Russian, БЕЗ тегов
mapping = resolve_format_mappings(client, cfg, blocks, translations)
# -> {(pid, span_id): (target_text, occurrence)}
outcome = run_formatting_align(
    blocks=blocks, translation=translations,
    backend_identity_hash=..., policy_version=...,
    max_formatting_incidents=MAX, mappings=mapping,
)
formatted = dict(outcome.formatted_text)
write_json(translations_path, formatted)               # с <em>
write_json(formatting_report_path, outcome.to_payload())
```

`source_html_text` уже доступен в `v4_book_run.py` (аргумент `--chapter-html-pattern`
→ `chapter_html`). `client` — translation client (уже используется для перевода).
`cfg` — конфиг форматирования (новый раздел, зеркало V3).

### resolve_format_mappings (порт V3 formatting_messages + parse_format_mappings)
- Входные блоки фильтруются: только PIDs с `inline_spans`.
- Сообщение (порт V3, строка 2743): system «ты — специалист по inline-форматированию;
  не меняй текст; для каждого SOURCE_SPAN найди его место в TRANSLATION; target_text —
  точная подстрока из TRANSLATION; при неоднозначности укажи occurrence» + item
  `<FORMAT_ITEM pid>` с `<SOURCE>` / `<SOURCE_SPANS>` (JSON спанов: span_id, tag,
  source_text, attrs, required) / `<TRANSLATION>`.
- Модель возвращает `{"mappings":[{"pid","span_id","target_text","occurrence"}]}`.
- `parse_format_mappings` валидирует: ключ `(pid, span_id)` в allowed; `target_text`
  непустой; `occurrence >= 1`.
- Батчи по `max_blocks_per_call` (V3: 12); retry `generation_retries` (V3: 2).
- Неудачный батч → пустой маппинг → спаны станут инцидентами (debt), НЕ тихая потеря.

### Адаптация run_formatting_align (model-free обёртка)
- Новый необязательный параметр `mappings: Optional[Dict[Tuple[str,str], Tuple[str,int]]]`.
- Если `mappings` задан (per-chapter путь): для каждого спана берём `target_text`
  (русский) и локализуем через `find_nonoverlapping_occurrence` (учёт `occurrence` и
  непересечения `occupied`), строим `SpanMappingRecord(start, end, tag, attrs,
  translated_text=target_text, preserved=False)`; `apply_span_mappings` оборачивает.
  Нет локации → `FormattingIncident` (missing_mapping / target_not_found / overlap,
  severity по `required`).
- Если `mappings` НЕ задан (legacy strict-runner path, где перевод несёт `<em>`) —
  сохраняем текущее поведение `_resolve_deterministic` (tiers preserved→exact→
  occurrence→fuzzy). Обратная совместимость со strict-runner.
- Обёртка детерминирована, 0 вызовов модели (card C соблюдён ДЛЯ apply-шага).

### Политика инцидентов (lenient_debt, решение владельца)
- `max_formatting_incidents` — конфиг (по умолчанию мягкий: не блокирует главу),
  в отличие от strict-runner (default 0 = blocking).
- Неразрешённый обязательный спан → `FormattingIncident` (debt) в `formatting_report.json`.
  Глава завершается; курсив восстановлен где возможно.

### Файлы
- `translations.json` главы: перезаписывается отформатированным текстом (с `<em>`).
  Это канонический финальный перевод, который читает `v4_book_html.py`.
- `formatting_report.json`: рядом, счётчики + инциденты (зеркало strict-runner).

### Безопасность / риск
- Перевод до шага форматирования — plain, БЕЗ тегов (ограничение владельца соблюдено;
  аудит/repair/интегрити не ломаются).
- Теги появляются только в финальном `translations.json` после обёртки.
- **Отклонение от card C:** добавляем targeted formatting model-call
  (`resolve_format_mappings`). Обоснование: card C ошибочно считал детерминированные
  тиры достаточными для EN→RU; POC (0/69) + V3 (работает через `target_text`) это
  опровергают. Одобрено владельцем (п.2). Обёртка (`apply_span_mappings`) остаётся
  model-free.
- Fail-closed: неразрешённый спан НЕ теряется молча — инцидент (debt) в отчёте.

## Границы (что НЕ делаем)
- Не кладём теги в перевод (ограничение владельца).
- Не меняем промпт перевода «plain Russian text only» (восстановление post-translation, B14).
- Не меняем `phase0b/source_html.py` (inline_spans уже корректны).
- Не меняем `v4_book_html.py` (читает отформатированный `translations.json`).
- Не меняем поведение strict-runner (legacy-path `run_formatting_align` без `mappings`).
- Не пересобираем старые главы 29/30/31 (решение владельца, п.3) — они остаются
  без курсива. Фикс для будущих прогонов.

## Тесты
- Юнит: `resolve_format_mappings` (мок модели) возвращает `target_text` →
  `run_formatting_align(mappings=...)` оборачивает русскую подстроку в `<em>`;
  `translations.json` содержит `<em>` в ожидаемых pid; `formatting_report.json`
  `resolved_count > 0`.
- Юнит: неточный/парафразный `target_text` → спан неразрешён → инцидент (debt),
  глава НЕ падает (lenient).
- Юнит: PIDs БЕЗ `inline_spans` → `resolve_format_mappings` НЕ вызывает модель
  (мок-счётчик = 0); их перевод не меняется.
- Регрессия: рендер такой главы через `v4_book_html.py` → `<em>` в `book.html`;
  глава без em → вывод без изменений.
