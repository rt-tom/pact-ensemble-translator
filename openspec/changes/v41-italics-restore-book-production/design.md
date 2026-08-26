# Design — v41-italics-restore-book-production

## Подход

### Точка интеграции
`v4_book_run.py` — per-chapter wrapper (B7/B9). После того как перевод
сгенерирован / отремонтирован / отредактирован и записан в финальный
`translations.json` (или промежуточный финальный файл главы), добавляем шаг
Phase 5:

```python
from pact_v4.phase0b.source_html import parse_source_html
from pact_v4.phase5.formatting import run_formatting_align

blocks = parse_source_html(source_html_text)
outcome = run_formatting_align(blocks, translations, max_formatting_incidents=MAX)
formatted = dict(outcome.formatted_text)
# перезаписываем финальный translations.json отформатированным текстом (с <em>)
write_json(translations_path, formatted)
write_json(formatting_report_path, outcome.to_payload())
```

`source_html_text` уже доступен в `v4_book_run.py` (аргумент `--chapter-html-pattern`
→ `chapter_html`, см. строку ~975). `translations` — финальный pid→text мап главы
на момент интеграции.

### Где берутся теги (важно — «хитрая настройка» сохраняется)
`run_formatting_align` НЕ изобретает теги. `parse_source_html` извлекает
`inline_spans` из `<em>`/`<i>` исходника (`DEFAULT_INLINE_TAGS =
("em","strong","i","b","a")`), и функция сопоставляет **ТЕКСТ** спана с
переводом по каскадам (preserved → exact → occurrence_aware → fuzzy).
Оборачивается только совпавший кусок перевода. Так выделяются ровно те слова,
что были курсивом в источнике, а не «N-ное слово по порядку». Оба варианта
(1 и 2) используют одну и ту же `run_formatting_align` + одни и те же
`inline_spans` — разница только в ТОЧКЕ вызова. Вариант 2 надёжнее: `<em>`
становится частью канонического `translations.json`, и любой рендер/консьюмер
получает курсив автоматически.

### Файлы
- `translations.json` главы: перезаписывается отформатированным текстом (с
  `<em>`). Это канонический финальный перевод, который читает `v4_book_html.py`.
- `formatting_report.json`: пишется рядом, счётчики + инциденты (зеркало
  strict-runner, см. `v4_phase12_strict_runner.py:1650` / `_formatting_report_path`).

### Политика инцидентов (решение владельца)
- `max_formatting_incidents` — конфиг (по умолчанию **мягкий: не блокирует
  главу**), в отличие от strict-runner (где default `0` = blocking).
- Неразрешённый обязательный спан → `FormattingIncident` (debt), пишется в
  `formatting_report.json`. Глава завершается; курсив восстановлен где возможно.
- НЕ делаем hard-block по умолчанию, чтобы сборка книги не падала из-за одного
  пропущенного курсива. Решение владельца может изменить на строгое
  (`max_formatting_incidents=0`).

### Безопасность / риск
- `run_formatting_align` model-free (card C: «formatting = 0 model calls») —
  0 вызовов модели, детерминирован, model-free по правилу.
- Не меняет промпты, схему вывода, парсер исходника, рендер.
- Fail-closed: неразрешённый спан НЕ теряется молча — он в `formatting_report.json`
  как инцидент (debt), никогда не «тихая потеря».

## Границы (что НЕ делаем)
- Не трогаем промпт перевода (`phase2/prompts.py` «plain Russian text only») —
  восстановление остаётся post-translation, как задумано B14.
- Не меняем `phase0b/source_html.py` (inline_spans уже корректны).
- Не меняем `v4_book_html.py` (он корректно хранит `<em>`, просто получает
  уже отформатированный `translations.json`).
- Не добавляем новые model-calls.

## Тесты
- Юнит: глава с `<em>` в исходнике → после шага `translations.json` содержит
  `<em>` в ожидаемых pid; `formatting_report.json` с `resolved_count > 0` и
  корректным `incident_count`.
- Юнит: парафраз внутри `<em>` → спан неразрешён → `formatting_report.json`
  фиксирует инцидент, глава НЕ падает (debt, не blocking).
- Регрессия: рендер такой главы через `v4_book_html.py` даёт `<em>` в `book.html`.
