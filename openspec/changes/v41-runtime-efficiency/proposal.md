## Why

Ревьюер `pact-rev` (4 раунда на `main` 61d8a1d → APPROVED) закрыл 5 HIGH correctness-фиксов, но оставил 5 низкорисковых оптимизаций кодовой базы/эффективности без потери fidelity. Сейчас `v4_phase_progress.py` перечитывает большие JSON/NDJSON на каждый render, `backend_role_adapters` не лимитирует batch re-gate, `opencode_backend.complete()` — 242 строки, а монитор дублирует чтения chunk/whole-chapter путей. Это усложняет поддержку v4.1 и тратит токены/IO.

## What Changes

- Единый snapshot чтений для `v4_phase_progress` — один `_read_snapshot(out_dir)` на render цикл вместо повторных `read_json`/`read_ndjson` для `chunk_plan/journal/usage`.
- Batch region re-gate с `MAX_TOKENS_CEILING` — `4096*len(items)` лимитировать ceiling и разбивать крупные батчи на группы.
- Разбить `OpenCodeServerBackend.complete()` на `session admission / retry loop / response normalization / cost recording`.
- Incremental NDJSON в `watch` режиме — читать `phase_progress.ndjson`/`usage.ndjson` по offset/mtime вместо полного перечитывания.
- Оптимизировать chunk renderer без отделения — в v4.1 остаются оба варианта: whole-chapter (`WholeChapterPidMap`, `chunk_id=whole_chapter`) и почанковый (аудит и другие фазы всегда по чанкам). Chunk-таблицы НЕ legacy — аудит и часть фаз всегда чанковые, поэтому прогресс-монитор должен рендерить оба пути через единый snapshot (whole-chapter ветка + chunk ветка), а не скрывать chunk-таблицы.

## Capabilities

### New Capabilities
- (none — pure efficiency/refactor, no observable behavior change)

### Modified Capabilities
- (none)

## Impact

- `pact_full_pipeline_runner_v1/v4_phase_progress.py`, `pact_v4/runtime/backend_role_adapters.py`, `pact_v4/runtime/opencode_backend.py`, `pact_v4/runtime/runtime_coordinator.py`
- Тесты `tests/pact_v4/runtime/*`, `tests/pact_v4/pipeline/test_v4_phase_progress*`
- Нет изменения промптов/контрактов перевода, identity/resume determinism сохраняется
