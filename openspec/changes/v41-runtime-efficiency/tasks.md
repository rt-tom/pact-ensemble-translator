## 1. Investigation

- [x] 1.1 Подтвердить что оба пути нужны: whole-chapter (`WholeChapterPidMap`, `chunk_id=whole_chapter`) и почанковый (аудит и др. фазы всегда по чанкам) — `grep` по `strict_runner`/`WholeChapterPidMap` и `v4_phase_progress`, зафиксировать что chunk-таблицы НЕ legacy — verify: отчёт `investigation.md` с примерами `chunk_plan.json` для обоих вариантов.

## 2. Snapshot чтений

- [x] 2.1 Ввести `_read_snapshot(out_dir)` в `v4_phase_progress.py` и прокинуть snapshot в рендер-функции вместо повторных `read_json` — verify: `python -m pytest tests/pact_v4/pipeline/test_v4_phase_progress* -q` 458+ passed
- [x] 2.2 Убедиться что snapshot пересобирается каждый `render` цикл и не ломает `watch` — verify: ручной `phase_progress --watch 1` не показывает устаревшие данные

## 3. Batch ceiling

- [x] 3.1 Добавить `MAX_TOKENS_CEILING` в `backend_role_adapters.py:935` — `min(ceiling, 4096*len)` и чанкование `items` — verify: unit тест с 10 items + `pytest tests/pact_v4/runtime/test_* -q` 458 passed, число вызовов детерминировано

## 4. Разбиение complete()

- [x] 4.1 Разбить `OpenCodeServerBackend.complete()` (`opencode_backend.py:1118`) на `_ensure_session/_retry_loop/_normalize/_record_cost` без изменения логики — verify: `pytest tests/pact_v4/runtime/test_opencode_backend -q` 138+ passed, `compileall` ok

## 5. Incremental NDJSON

- [x] 5.1 Добавить incremental чтение `phase_progress.ndjson`/`usage.ndjson` по `offset/mtime` в `watch` — verify: `watch` с большим файлом не перечитывает целиком (ручная проверка `strace`/`offset` лог)

## 6. Отделение chunk renderer

- [x] 6.1 Унифицировать chunk/whole-chapter рендер через единый snapshot — обе ветки рендерятся (chunk таблицы + whole-chapter ветка), без скрытия — verify: whole-chapter и почанковый run оба показывают корректные таблицы/ветки из одного snapshot

## 7. Verification

- [x] 7.1 Прогнать `pytest tests/pact_v4/runtime -q` и `pytest tests/pact_v4/pipeline/test_v4_phase_progress* -q` — оба зелёные — verify: CI 458+ passed
- [x] 7.2 `git diff --check` clean, `openpec validate` для change — verify: `openspec validate --change v41-runtime-efficiency` без ошибок
