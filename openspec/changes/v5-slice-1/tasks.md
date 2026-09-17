## 1. Сплиттер (вне пайплайна)

- [ ] 1.1 Реализовать EPUB-сплиттер (OPF/spine-порядок → `NNNN_<slug>.html` + `manifest.json` с `file/order/en_title/pov/parent/illustrations`, `epub_hash/splitter_version`); проверить байт-детерминизм повторного прогона на Pale EPUB (313 глав)
- [ ] 1.2 Реализовать снятие POV с маркера `<p><strong>Имя</strong></p>` (Verona/Lucy/…, Prologue/SB) в манифест без изменения HTML; проверить на главах 0-0, 1-0, 1-1 и SB-кейсе
- [ ] 1.3 Реализовать слоты иллюстраций (`{id, anchor}` + `[ILLUSTRATION id]`), пустой список для Pale — валиден; проверить отсутствие влияния на `source_html.py`-парсинг
- [ ] 1.4 Реализовать schema-versioned directory-input manifest generator для Pact (150 HTML, детерминированный set hash); проверить валидацию общего manifest-контракта (порядок, hash, один файл на главу, reject symlink/special) в `--preflight` для разложенного Pale и Pact

## 2. Внешний анализ и ручное утверждение (вне реализации slice-1)

- [ ] 2.1 Владелец отдельным инструментом анализирует Pale и вручную утверждает `books/pale/chapters.json` (ru-заголовки + POV); этот OpenSpec не реализует, не запускает и не валидирует модель-анализатор
- [ ] 2.2 Владелец выполняет `pact-snapshot init-store 2`, затем вручную переносит утверждённые glossary entries/global facts в bootstrap inbox `book_id=2` как `glossary.json`/`book_memory.json`, создаёт пустые `chapter_index.json`/`observations.json` и bootstrap'ит `rev-0001`; проверить exact-four boundary и отсутствие Pact-сидов/будущих сюжетных фактов

## 3. Профиль книги и раннер

- [ ] 3.1 Одноразово сгенерировать и owner-review `books/pact/chapters.json` из 150 HTML, `arc_names.json` и established POV; ввести `books/<slug>/book.yaml` для Pact/Pale и единый резолв `--book <slug>` в `v4_run.py` (omitted = `pact`); хеш approved `chapters.json` включить в profile/prompt identity
- [ ] 3.2 Обобщить `deterministic_arc_names` → `deterministic_title_map`: single derivation из записей утверждённого `chapters.json` (полные `ru_title`; блок — unique arc pairs в порядке first-appearance, label `CHAPTERS:`; zero-chapter entries Transgression/Sundown/bare Gathered dropped); удалить runtime fallback к `arc_names.json`; B1 RU arc table как данные (+ review-флаги Breach/Null/Malfeasance в notes); проверить пустые `ru_title` Pale, подстановку заголовков в `v4_book_html.py` и русские заголовки Pact; byte-regression НЕ требуется — derived identity становится baseline (B4 waived)
- [ ] 3.3 Ввести per-chapter POV в bible-рендер (`POV: <имя> (<пол>)` из approved `chapters.json`), книжный `pov.gender=null` для Pale; проверить общий renderer для Pact с сингл-нарратором
- [ ] 3.4 Реализовать fail-closed изоляцию профилей (manifest/chapters/state принадлежат тому же slug); проверить негативными тестами
- [ ] 3.5 Реализовать preflight-гейты профиля (`content_kind: prose`, `en/ru` only, известные policy); иное — fail-closed до output/state/model activity; проверить unit-тестами без запуска моделей

## 4. Пилот и приёмка

- [ ] 4.1 Прогнать Pale 0.0, 1.0, 1.1 book-режимом тем же ансамблем (прогоны — владелец на RT); собрать глазную приёмку владельца
- [ ] 4.2 Прогнать Pact `--book pact` и legacy-invocation без `--book` для 0001 strict (владелец на RT); сравнить resolved layout, derived `CHAPTERS:` block (B1 table, §3.2), итоговый русский heading; зафиксировать НОВЫЙ derived identity baseline (byte-regression не требуется, B4 waived)
- [ ] 4.3 Прогнать `openspec validate v5-slice-1 --strict`, `pact-fidelity-lint` (если тронуты промпты), узкие pytest по затронутым модулям, `pact-git-hygiene`; зафиксировать техдолг «резка глав 95–125K» post-pilot задачей
