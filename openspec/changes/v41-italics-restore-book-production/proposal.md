## Why

В v4 курсив (`<em>`) намеренно срезается на стадии перевода: `SourceBlock.text`
в `phase0b/source_html.py` строится через `get_text()` (убирает ВСЕ теги, включая
`<em>`), а промпт перевода (`phase2/prompts.py:127-128`) инструктирует модель
«plain Russian text only». Курсив должен восстанавливаться ПОСЛЕ перевода в
Phase 5 (`pact_v4/phase5/formatting.py::run_formatting_align`), которая оборачивает
переведённый фрагмент в `<em>` по `inline_spans` исходника (ловятся в
`phase0b/source_html.py` из `<em>`/`<i>` тегов). НО `run_formatting_align`
вызывается ТОЛЬКО из strict-runner (`v4_phase12_strict_runner.py`), который НЕ
производил главы 29/30/31. Реальный per-chapter путь (`v4_book_run.py`) и ручной
рендер (`v4_book_html.py`) его НЕ вызывают. Итог: `<em>` никогда не
восстанавливается → финальная книга без курсива. Эмпирика: рендер глав 29/30/31
через `v4_book_html.py` дал **0 `<em>`** при **51/72/69 `<em>`** в исходнике.

## What Changes

**Подключаем Phase 5-восстановление `<em>` в per-chapter путь (Вариант 2).**
- В `v4_book_run.py` после финализации перевода (post-repair/edit) вызываем
  `run_formatting_align(blocks=parse_source_html(source_html), translation=translations)`.
- Форматированный текст (с `<em>`) пишем обратно в **`translations.json`** главы
  (её финальный перевод, который читает рендер) — зеркалим то, как strict-runner
  трактует `formatted_text` как финальный текст.
- Рядом пишем `formatting_report.json` (resolved_count / incident_count /
  blocking / model_fallback_count) для прозрачности, зеркалим strict-runner.
- Теги восстанавливаются из `inline_spans` исходника сопоставлением по ТЕКСТУ
  (tiers preserved/exact/occurrence_aware/fuzzy) — ровно те слова, что были
  курсивом в источнике, а не по позиции. Функция не выдумывает теги.

**Политика блокировки (решение владельца):** strict-runner использует
`max_formatting_incidents=0` (любой неразрешённый спан = blocking). Для сборки
книги предлагаю НЕ блокировать главу при инцидентах форматирования — писать
`formatting_report.json` и завершать главу, а неразрешённые спаны оставлять
долгом (debt). Италики восстанавливаются где возможно; глава не падает из-за
пропущенного курсива. `max_formatting_incidents` делаем конфигом (по умолчанию
мягкий / не блокирующий).

## Capabilities

### New Capabilities
- (none)

### Modified Capabilities
- `book-production-formatting` — per-chapter сборка теперь восстанавливает
  `<em>` из `inline_spans` исходника в `translations.json` (Phase 5), а не
  теряет курсив.

## Impact

- Затрагивает: `v4_book_run.py` (вызов `run_formatting_align` + запись
  `translations.json`/`formatting_report.json`); возможно маппинг выходного
  артефакта главы.
- Не трогает: промпты перевода/аудита, схему `inline_spans`, парсер
  `phase0b/source_html.py`, рендер `v4_book_html.py` (он просто читает уже
  отформатированный `translations.json`).
- Риск: **Medium** (локальное поведение сборки книги; детерминировано, 0 вызовов
  модели — `run_formatting_align` model-free по card C).
- Тесты: юнит — после прогона главы с `<em>` в исходнике `translations.json`
  содержит `<em>` в ожидаемых pid, а `formatting_report.json` — корректные
  счётчики; регрессия — рендер такой главы через `v4_book_html.py` даёт `<em>`
  в `book.html`.
- Отношение к предыдущим change: независимо от `v41-literary-consistency-checks`
  (аудит LAIT). Курсив и литературная консистентность — разные оси.
