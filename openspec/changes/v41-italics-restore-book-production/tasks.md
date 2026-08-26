# Tasks — v41-italics-restore-book-production

## 1. Интеграция Phase 5 в `v4_book_run.py`
- [ ] 1.1 импорт `parse_source_html` (`phase0b.source_html`) + `run_formatting_align` (`phase5.formatting`) в `v4_book_run.py`
- [ ] 1.2 найти точку финализации перевода главы (после repair/edit, перед/при записи `translations.json`) и вызвать `run_formatting_align(blocks, translations, max_formatting_incidents=...)`
- [ ] 1.3 перезаписать финальный `translations.json` отформатированным текстом (`dict(outcome.formatted_text)`) — verify: файл содержит `<em>`
- [ ] 1.4 записать `formatting_report.json` рядом (`outcome.to_payload()`) — verify: файл существует, счётчики корректны

## 2. Конфигурация политики инцидентов
- [ ] 2.1 `max_formatting_incidents` как параметр/конфиг `v4_book_run.py` (по умолчанию мягкий / не блокирующий)
- [ ] 2.2 глава НЕ падает при инцидентах форматирования (debt в `formatting_report.json`, не blocking) — verify: тест 3.2

## 3. Тесты
- [ ] 3.1 юнит: глава с `<em>` в исходнике → после шага `translations.json` содержит `<em>` в ожидаемых pid; `formatting_report.json` `resolved_count > 0`
- [ ] 3.2 юнит: парафраз внутри `<em>` → спан неразрешён → инцидент в `formatting_report.json`, глава завершается (не падает)
- [ ] 3.3 регрессия: рендер такой главы через `v4_book_html.py` → `<em>` в `book.html`

## 4. Проверка (openspec verify)
- [ ] 4.1 `openspec validate --strict` проходит
- [ ] 4.2 `pytest` проходит (узкий набор: форматирование + рендер с `<em>`)
