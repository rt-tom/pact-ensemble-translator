## Context

См. `proposal.md — Why`. Текущий `v4_book_run.py` строит форматинг-клиент один раз до цикла (`_build_formatting_client` → `formatting_client`) и переиспользует его для всех глав. Для `local` профиля клиент — `LocalLlamaBackendConfig` с `ManagedServerProcess` на `8094` (жив весь прогон). Для `remote` (`OpenCodeBackendConfig` / `Composite`) клиент — `OpenCodeServerBackend` с `server_mode: external` (без `ManagedServerProcess`), поэтому после закрытия строго-сервера главы (`RemoteRuntimeCoordinator.close()`) на форматирование не остаётся живого `opencode serve` → `Connection refused` на `/global/health`. PR #221 пофиксил `max_tokens`/`reasoning`, но не жизненный цикл. Фикс должен поднять сервер именно на этапе форматирования главы.

## Goals / Non-Goals

**Goals:**
- Удаленный букран форматирует главы через живой `opencode serve` (70 спанов → не 70/70 debt).
- Сервер форматирования живёт только на этапе форматирования главы, не конфликтует по порту со строго-сервером следующей главы.
- Логи/диагностика как в PR #221 (`formatting_batch*_meta.json`, `server_logs/opencode_serve_fmt_*`).

**Non-Goals:**
- Менять `pact_v4/phase5/formatting.py` логику (`resolve_format_mappings`/`run_formatting_align`) — уже покрыта PR #221.
- Менять формат `formatting_report.json` / `translations.json`.
- Трогать `local` путь кроме унификации `log_dir`.

## Decisions

- **D1: Lazy build форматинг-клиента внутри `run_book` цикла.** Вместо `formatting_client = _build_formatting_client(...)` до цикла — строить внутри `if terminal_status in _PROMOTING_STATUSES and has_spans:` перед `resolve_format_mappings`. Альтернатива — держать глобальный managed сервер весь прогон — отклонена (требование владельца + конфликт портов).
- **D2: `build_runtime(log_dir=out_dir/"server_logs")` per-chapter.** Для `OpenCodeBackendConfig` с `server_mode: external` трактовать форматирование как `managed` на том же порту (`base_url` → `ManagedServerSpec(port=...)`) или переключать `server_mode` на `managed` для форматирования. Альтернатива — требовать внешний сервер от оператора — отклонена (пайплайн сам поднимает сервер).
- **D3: `try/finally` закрытие.** `runtime.close()` / `client.close()` в `finally` после форматирования, чтобы следующая глава могла поднять свой строго-сервер на том же порту. Ошибка старта/health → `LOG.warning` + fallback в debt (lenient), без падения главы.
- **D4: `_build_formatting_client` принимает `out_dir` для `log_dir` и `reasoning 0` оверрайд.** Уже реализован для `Composite`/`Local`, расширить на `OpenCode` (через `_default_managed_spec`).

## Risks / Trade-offs

- [Порт `4097` занят строго-сервером следующей главы] → Mitigation: сервер форматирования закрывается до следующей итерации цикла, health-wait 120с, `assert_port_free_or_owned` fail-fast с fallback в debt.
- [Двойной `ManagedServerProcess` на одном порту] → Mitigation: последовательный жизненный цикл (строго → форматирование → закрытие → следующий строго).
- [External профиль без прав на managed] → Mitigation: если `server_mode: external` и порт свободен — поднять managed; если занят внешним сервером — health-чекать существующий, не стартовать новый.

## Migration Plan

- Деплой — обычный `git pull --ff-only` на `media`, без миграции артефактов. Откат — revert коммита `v4_book_run.py`.
- Валидация — `openspec validate`, `pytest tests/pact_v4/test_formatting_v41_fix.py` + ручной `book_0032-0033_remote` на `media`.

## Open Questions

- Нужен ли отдельный `formatting` порт (4098) чтобы избежать даже последовательного конфликта? Пока нет — последовательный цикл решает.
