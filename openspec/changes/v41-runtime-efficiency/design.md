## Context

См. proposal.md — Why. Текущий `main` (0e84b90) — whole-chapter v4.1 с исправленными HIGH (variant, prompt hash, YAML allow-list, cost budget, per-coordinator offsets). Остались оптимизации без изменения наблюдаемого поведения: `v4_phase_progress` читает артефакты многократно, `backend_role_adapters` batch без ceiling, `opencode_backend.complete()` — монолит. `ChunkPlan` в `strict_runner.py` ещё используется для `WholeChapterPidMap` ownership, но `v4_phase_progress` chunk total для whole-chapter уже избыточен — нужна проверка перед отделением рендера.

## Goals / Non-Goals

**Goals:**
- Снизить IO/CPU монитора и re-gate без изменения вывода
- Упростить `complete()` для тестируемости retry/session
- Сохранить determinism/resume, совместимость со старыми `chunk_plan.json`

**Non-Goals:**
- Изменение промптов, контрактов перевода, identity, топологии
- Удаление поддержки старых chunk-артефактов (только скрыть в whole-chapter рендере)
- Новый UI/метрики

## Decisions

- **Snapshot vs per-read:** единый `_read_snapshot(out_dir) -> {chunk_plan, journal, usage}` за один проход, передаётся в рендер-функции. Альтернатива — LRU кэш — отклонена (snapshot проще, без инвалидации).
- **Batch ceiling:** `MAX_TOKENS_CEILING` (ту же что у `DEFAULT_MAX_TOKENS`) делить `4096*len` → `min(ceiling, ...)` и чанковать `items` по `ceiling//4096`. Альтернатива — динамический ceiling по модели — отложена.
- **complete() split:** ` _ensure_session / _retry_loop / _normalize_response / _record_cost`. Альтернатива — state machine класс — избыточна для 4 шагов.
- **Incremental NDJSON:** `watch` хранит `offset`/`mtime`/`inode` per file, читает только хвост. Альтернатива — `inotify` — платформенно-зависима.
- **Chunk renderer — не отделять, а унифицировать через snapshot:** оба варианта остаются (whole-chapter генерация + почанковый аудит/фазы). Прогресс-монитор рендерит обе ветки (`whole_chapter` и `chunks`) из одного snapshot, без скрытия таблиц. `ChunkPlan` для whole-chapter нужен для `WholeChapterPidMap` ownership, chunk total для аудита — всегда нужен.

## Risks / Trade-offs

- [Snapshot устареет в watch между poll] → Mitigation: snapshot пересобирается каждый `render` цикл, watch уже poll'ит
- [Batch split меняет число вызовов] → Mitigation: functional tests на 10+ items, ceiling детерминирован
- [complete() split меняет стек трейс] → Mitigation: сохранить логирование точек retry/session как раньше
- [Incremental offset пропустит truncate] → Mitigation: если `size < offset` или `mtime` сброшен — полный перечит

## Migration Plan

- Все изменения — refactor, feature-flag не нужен. Rollback — revert commit. `git pull --ff-only` на RT как в AGENTS.md.

## Open Questions

- Точный `MAX_TOKENS_CEILING` для batch — взять существующий `32768` или `70000`? Ответ в task 1 по текущему `DEFAULT_MAX_TOKENS`.
