# Design: v5-slice-1

## Контекст

V5-документ фиксирует: «V5 не заменяет доказанные правила качества v4. Она обобщает входы… вокруг этих правил», «языковая специфика — plugin», «исследование отделено от перевода», «новое внедряется вертикальными slices». Настоящий чейндж — slice-1: обобщить вход (EPUB→главы) и подготовку контекста (анализ→seed→профиль), не трогая quality engine.

## Подход

### 1. Сплиттер — отдельный инструмент, пайплайн его не знает

- Вход: произвольный EPUB (OPF/spine — порядок, XHTML — контент). Картинок в Pale нет (0), но формат их учитывает.
- Выход в `<book-source-root>/`: файлы `NNNN_<slug>.html` (нормализованный chapter-HTML, совместимый с `phase0b/source_html.py`) + source `manifest.json`:
  `[{file, order, en_title, pov, parent, illustrations: [{id, anchor}]}, …, {epub_hash, splitter_version}]`. Тот же schema-versioned manifest для Pact одноразово генерируется из существующего набора 150 HTML; для directory-input вместо `epub_hash` он содержит детерминированный hash набора файлов.
- POV снимается со структурного маркера первой строки (`<p><strong>Имя</strong></p>` — Verona/Lucy/…), строка остаётся в HTML как есть. Заголовок главы — `<title>`/`h2` (у Pale номера `0.0`, `1.1`, `SB`). Сценовые разделители не режутся в slice-1 (длинные главы — техдолг).
- Иллюстрации: слоты `{id, anchor}` + плейсхолдер `[ILLUSTRATION id]` в HTML уже сейчас; подстановка картинок в сборщик — позже, v1 их игнорирует.
- Пайплайн валидирует выход по манифесту (порядок, hash, ровно один файл на главу, reject symlink/special — как сейчас) и дальше работает как с Pact-главами.

### 2. Анализ книги — внешняя ручная входная граница

- Анализ всей книги моделью не реализуется и не запускается в v5-slice-1: Pale слишком велик для единого корректного контекста, а дополнительная orchestration/валидация увеличит число проверок и модельных вызовов.
- Владелец вправе получить анализ отдельным подходящим инструментом и вручную подготовить/утвердить `books/<slug>/chapters.json` (`{file, order, en_title, ru_title|null, pov, notes}`) до первого запуска. Внешний инструмент и передача ему текста — вне доверенной границы и не являются частью этого OpenSpec.
- Результаты ручного анализа не образуют новые runtime seed-файлы: после `pact-snapshot init-store 2` утверждённые glossary entries и глобальные facts вносятся в `glossary.json` и `book_memory.json` bootstrap-inbox новой книги; `chapter_index.json` и `observations.json` создаются пустыми. Затем существующий `pact-snapshot bootstrap 2` публикует отдельный `rev-0001`. Тем самым `/home/rt/pact_runs/books/2/snapshots/rev-0001/state/` сохраняет exact-four boundary и дальнейшие ревизии работают без смены snapshot-протокола.
- `chapters.json` — профильный неизменяемый input вне snapshot state; его hash входит в resolved profile/prompt identity. Раннер читает его только для конкретной книги; он не может наследоваться из Pact. Заголовки живут только в `chapters.json`, никогда в `glossary.json` (защита B9-эвристик: заголовок встречается 1 раз).

### 3. Профиль книги — данные, не код

`books/<slug>/book.yaml`: `id/title`, `content_kind: prose`, `source_lang/target_lang` (slice-1: только `en/ru`, иное — ошибка preflight), source roots RT/media, source `manifest.json`, `state.media_book_id`, путь к утверждённому `chapters.json`, `policy.hard_filters/editor_pass` (только известные значения).

- Pact = `books/pact/book.yaml` с нынешними дефолтами (`book-1`, `pact_chapters`) и теми же source `manifest.json`/`chapters.json`, что у Pale. Одноразовый мигратор генерирует и владелец ревьюит их из текущих 150 HTML, `arc_names.json` (15 legacy-записей без изменений) + B1 RU arc table и established narrator; после этого `arc_names.json` не читается runtime-кодом. Для `book-1` действует single derivation (alternative B, owner decision): блок `CHAPTERS:` — unique arc pairs в порядке first-appearance из записей; byte-regression не требуется, derived identity — новый baseline (B4 waived).
- `v4_run.py`: `--book <slug>` резолвит профиль; slug — человекочитаемый alias (`pact`, `pale`), а `state.media_book_id` — внутренний snapshot namespace (`1`, `2`). Неуказанный `--book` означает `pact` только ради совместимости. Существующие `--media-book-id/--memory-dir/--chapter-html-pattern` — оверрайды.
- `StrictConfig.deterministic_arc_names` обобщается до `deterministic_title_map`, который для всех книг детерминированно извлекается из утверждённого `chapters.json`. Общий renderer сохраняет `CHAPTERS:` для мигрированного Pact и применяет нейтральный `ЗАГОЛОВКИ:` для новых книг только при непустом map.
- POV: поле главы из `chapters.json` → per-chapter блок в bible-рендере (`POV: Verona (female)`); книжный `pov.gender` для ротационных книг = null; единственный narrartor Pact описывается тем же контрактом.
- Fail-closed: профиль не может ссылаться на manifest/chapters/state другой книги; runtime-код не имеет fallback к `arc_names.json`.

## Альтернативы (отвергнуты)

- Форк репозитория: удвоение сопровождения при нулевой дивергенции пайплайна — отвергнут владельцем.
- Резка длинных глав в slice-1: касается ~10–15 глав Pale 95–125K, пилотные главы (37–50K) в зелёной зоне — отложено как техдолг post-pilot.
- Смешивание заголовков в глоссарий: ломает B9-частотные эвристики — отвергнуто, `chapters.json` остаётся отдельным артефактом.
- Встроенный анализатор всей книги: отвергнут для slice-1; не гарантирует пригодный контекст для Pale и добавляет лишние model calls/проверки.

## Верификация

- Контрактные тесты сплиттера/манифеста на реальном Pale EPUB (313 глав, детерминизм: повторный прогон — байт-идентично).
- Юнит-тесты резолва профиля, подстановки заголовков из `chapters.json`, per-chapter POV и fail-closed изоляции сидов.
- E2E пилота: Pale 0.0/1.0/1.1 book-режим + `--preflight`; регрессия Pact 0001 strict; `openspec validate v5-slice-1 --strict`; `pact-fidelity-lint` при касании промптов; `pact-git-hygiene` перед коммитом. Прогоны — владелец на RT.
