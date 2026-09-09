## Why

`book` без `--out-base` аллоцирует новый `book_<range>_<label>_<timestamp>` каталог при каждом запуске, а со стабильным `--out-base` книга всё равно безусловно перегоняет все главы (`run_book()` → `_run_one_chapter()` без проверки готовых глав). Внутри главы resume позиционный (`resumed_from_index = len(prior_entries)`, пропуск `index < resumed_from_index`): чанки, записанные в journal как `incomplete_generation` (переводчик 3 раза выдал кривой JSON), при повторе не переводятся заново, а переиспользуются как неуспех. Итог: упавший на одной главе ран книги продолжить нельзя — ни книгой целиком, ни повтором главы.

## What Changes

- **Chapter retry:** повторный `chapter`-прогон в тот же `--out-dir` определяет первый незавершённый шаг по durable stage artifacts и перезапускает именно его, переиспользуя валидные результаты предыдущих шагов. Для `incomplete_generation` используется отдельная перемотка journal до первой такой записи и перегенерация её и зависимого хвоста; `selected` переиспользуется, `quarantined` остаётся терминальным. Identity-проверка journal и stage artifacts (snapshot/chunk_plan/config/backend) сохраняется fail-closed.
- **Book resume:** `book` со стабильным `--out-base` перед запуском главы проверяет durable stage artifacts и `strict_chapter_trial_record.json`. Готовые главы 1, 3–4 пропускаются, а упавшая глава 2 запускается в своей папке с первого незавершённого шага, используя уже актуальную общую memory. Результаты глав 3–4 не пересчитываются и не откатываются: их дополнительные glossary/memory-находки считаются валидными. Успешный retry главы 2 только добавляет её новые находки.
- **Без ослабления identity:** сохраняются проверки source/chunk-plan/config/backend для переиспользуемых stage artifacts. Текущая общая memory не откатывается и не делает downstream-главы stale: поздние glossary/memory-находки допускаются как additive state. Для старых out-dir stage можно определить по существующим artifact/status-файлам; обязательный pre-chapter snapshot не требуется. Леджеры кандидатов уже идемпотентны (upsert по `chapter_id+key`) — изменений не требуют, только тесты на повторный прогон.
- **Явные флаги:** `chapter --resume` (автоматически продолжить с первого незавершённого шага), `chapter --retry-incomplete` (явно перемотать generation journal), `book --resume` + `book --force-rerun-chapter <id>` (opt-in). Отдельный ручной выбор шага не нужен: stage checkpoint выбирает его автоматически. Без флагов поведение не меняется.

## Capabilities

### New Capabilities
- `book-retry`: chapter retry неполных чанков и book resume готовых глав.

## Impact

- `pact_v4/pipeline/v4_phase12_strict_runner.py` (перемотка journal), `pact_full_pipeline_runner_v1/v4_phase12_strict_run.py` (флаг chapter), `pact_full_pipeline_runner_v1/v4_book_run.py` (пропуск глав, флаги), `pact_full_pipeline_runner_v1/v4_run.py` (help-текст), docs.
- High risk: resume/journal/stage-семантика, book-промоушн и additive общая memory-state. Переиспользование завершённых stage artifacts проверяется по их записанному source/plan/config/backend contract; downstream-главы намеренно не откатываются. Без запуска пайплайна, только тесты.
