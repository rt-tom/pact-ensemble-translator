## Why

Pact v4 — это Pact-only система: пути (`pact_chapters`, `book_state`), сиды памяти/глоссария (Blake, `arc_names.json` Bonds→Узы), сингл-нарратор (`pov.gender` на книгу) и правило discovery `NNNN_*.html` зашиты в код и дефолты. Архитектура V5 (`docs/architecture/V5_UNIVERSAL_LITERARY_TRANSLATOR_ARCHITECTURE_RU.md`) требует обобщения входов вокруг доказанного v4 quality engine, принцип №10 — вертикальными слайсами.

Пилотная книга — Pale (тот же автор и сеттинг, независимая книга, EPUB, 313 глав, ротирующиеся POV Verona/Lucy/Avery, типичная глава 38–55K символов — в доказанном диапазоне Pact, хвост 95–125K). Грилл-сессия с владельцем зафиксировала скоуп первого слайса: только en-ru, только обычная проза, тот же ансамбль качества, запуски только владельцем, строгая изоляция состояния от Pact. Полный V5 (language packs, web research, adaptive topology, job API/UI) — вне скоупа.

## What Changes

- Внешний сплиттер EPUB→главы (вне пайплайна): `NNNN_*.html` + `manifest.json` (порядок, `en_title`, `pov` на главу, слоты иллюстраций-плейсхолдеров впрок). Пайплайн читает только его выход.
- Анализ всей книги моделью — вне v5-slice-1: владелец выполняет его отдельным подходящим инструментом. Его результаты вручную переносятся в `books/<slug>/chapters.json` (порядок + en/ru заголовки + POV) и в initial authoritative state новой книги: `glossary.json`/`book_memory.json`; `chapter_index.json`/`observations.json` начинают пустыми. Пайплайн не вызывает модель-анализатор и не отправляет текст книги на анализ.
- Профиль книги `books/<slug>/book.yaml` (source/state/out корни, source `manifest.json`, путь к утверждённому `chapters.json`, `state.media_book_id`, гейты `hard_filters`/`editor_pass`) — единый для Pact и Pale: alias `--book <slug>` выбирает книгу, не ревизию. Pact получает такие же profile/manifest/chapters артефакты одноразовой миграцией из существующих 150 HTML и `arc_names.json`; поведение для `book-1` не меняется. Новая книга bootstrap'ится текущим snapshot-протоколом как отдельный `book_id=2`, первая ревизия `rev-0001` с exact-four state boundary.
- Раннер: единый резолв `--book <slug>` (неуказанный slug = `pact` только для обратной совместимости); утверждённый `chapters.json` — единственный авторитет для заголовков (отдельно от `glossary.json`) и POV. `arc_names.json` перестаёт быть runtime-входом, но мигрированный Pact сохраняет байт-идентичный блок `CHAPTERS:`; у новых книг нейтральный блок `ЗАГОЛОВКИ:`.
- Пилот: Pale главы 0.0, 1.0, 1.1 тем же ансамблем + глазная приёмка владельца + регрессия Pact 0001 strict зелёная.

## Capabilities

### New Capabilities
- `v5-book-splitter`: внешний EPUB-сплиттер с детерминированным манифестом.
- `v5-book-profile`: профиль книги и адаптация раннера под `books/<slug>`.

## Impact

Затронуты: новый инструмент сплиттера, одноразовый генератор profile/manifest/chapters артефактов Pact, `v4_run.py` (диспетчер, `--book`), `StrictConfig` (`deterministic_title_map`), per-chapter POV в bible-рендере, `v4_book_html.py` (подстановка заголовков из `chapters.json`). Общая логика из общих артефактов сохраняет для Pact текущий `CHAPTERS:`-блок, для новых книг применяет нейтральный блок. Не затронуты: cascade-селекция, ledger-форматы, snapshot-протокол, lifecycle моделей, `hard_filters` en-ru таблицы, `russian_editor`. Риск Medium: новые инструменты + диспетчер, но ядро пайплайна и прод-состояние Pact не меняются; прогоны — только владельцем на RT, отдельно owner-approved. Без форка репозитория: один код, данные на книгу.
