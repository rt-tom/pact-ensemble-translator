# Tasks — v41-italics-restore-book-production

## 1. resolve_format_mappings — formatting model-call (порт V3)
- [ ] 1.1 добавить `resolve_format_mappings(client, cfg, blocks, translations, ...)`
  в `phase5/formatting.py`: фильтр PIDs с `inline_spans`; сообщение (порт V3
  `formatting_messages`, строка 2743) с `<SOURCE>`/`<SOURCE_SPANS>`(JSON)/`<TRANSLATION>`;
  вызов модели; `parse_format_mappings` (валидация `(pid, span_id)`, `target_text`,
  `occurrence`) → `Dict[Tuple[str,str], Tuple[str,int]]`
- [ ] 1.2 батчи по `max_blocks_per_call` + retry `generation_retries` (конфиг форматирования)
- [ ] 1.3 неудачный батч → пустой маппинг (спаны → инциденты debt, не тихая потеря)
- [ ] 1.4 PIDs без `inline_spans` → модель НЕ вызывается (ранний return пустого маппинга)

## 2. Адаптация run_formatting_align (model-free обёртка по target_text)
- [ ] 2.1 добавить необязательный параметр `mappings` в `run_formatting_align`;
  при наличии — локация `target_text` через `find_nonoverlapping_occurrence` (уже в V4)
  + построение `SpanMappingRecord` (translated_text=target_text, preserved=False) для
  `apply_span_mappings`
- [ ] 2.2 инциденты: missing_mapping / target_not_found / overlap (severity по `required`)
- [ ] 2.3 legacy-path (`mappings=None`, strict-runner) — сохранить текущее поведение
  `_resolve_deterministic` (tiers preserved→exact→occurrence→fuzzy)
- [ ] 2.4 обновить docstring `run_formatting_align`: уточнить, что model-call —
  отдельный шаг `resolve_format_mappings`, обёртка остаётся model-free (card C
  соблюдён для apply-шага; отклонение от card C задокументировано в proposal)

## 3. Интеграция в v4_book_run.py
- [ ] 3.1 импорт `parse_source_html` + `resolve_format_mappings` + `run_formatting_align`
- [ ] 3.2 точка после финализации перевода: `resolve_format_mappings(client, cfg, blocks,
  translations)` → `run_formatting_align(mappings=...)` → перезапись `translations.json`
  (с `<em>`) — verify: файл содержит `<em>`
- [ ] 3.3 запись `formatting_report.json` рядом (`outcome.to_payload()`) — verify: файл
  существует, счётчики корректны
- [ ] 3.4 конфиг форматирования (модель/температура/max_tokens/батч/retry/on_failure) —
  зеркало V3 (раздел `formatting` в конфиге v4_book_run)

## 4. Политика инцидентов (lenient_debt)
- [ ] 4.1 `max_formatting_incidents` как параметр/конфиг v4_book_run.py (по умолчанию
  мягкий / не блокирует)
- [ ] 4.2 глава НЕ падает при инцидентах (debt в report, не blocking) — verify: тест 5.2

## 5. Тесты
- [ ] 5.1 юнит: `resolve_format_mappings` (мок) → `target_text` → `run_formatting_align`
  оборачивает русскую подстроку в `<em>`; `formatting_report.json` resolved_count>0
- [ ] 5.2 юнит: неточный/парафраз target_text → инцидент debt, глава завершается (не падает)
- [ ] 5.3 юнит: PIDs без inline_spans → модель НЕ вызывается (мок-счётчик=0), перевод не меняется
- [ ] 5.4 регрессия: рендер через `v4_book_html.py` → `<em>` в `book.html`; глава без em → без изменений
- [ ] 5.5 юнит: legacy-path (`mappings=None`) сохраняет поведение strict-runner (backward-compat)

## 6. Проверка (openspec verify)
- [ ] 6.1 `openspec validate --strict` проходит
- [ ] 6.2 `pytest` (узкий набор: форматирование + рендер с `<em>`)
