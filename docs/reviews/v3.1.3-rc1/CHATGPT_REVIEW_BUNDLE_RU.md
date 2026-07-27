# v3.1.3-rc1 — пакет независимого ревью для ChatGPT

## Роль и границы

Проведи статическое архитектурное ревью кандидата `b06599d` (`develop/v3.1.3`) относительно `main`. Не предлагай запуск модели, pipeline, reset/redo в production или ручное редактирование текста книги. Интересуют только корректность жизненного цикла артефактов, resume, cache safety, форматирование и finalization.

## Изменённые feature contracts

| Контракт | Основные файлы | Инвариант |
| --- | --- | --- |
| Atomic cache identity | `v31_common.py`, consumers | старый/чужой cache не считается valid по mtime или наличию файла |
| Artifact DAG | `v31_artifact_dag.py`, runner | selective redo инвалидирует только прямых downstream consumers |
| Stage protocol | `v31_stage_protocol.py`, runner | partial/invalid aggregate всегда `MODEL_REQUIRED`, не `REUSED` |
| Chapter manifest | `v31_chapter_resolver.py`, collector, monitor | один canonical selected-chapter set проходит runner/monitor/collector |
| Formatting integrity | `pact_translate_v3.py` | required inline incident блокирует final output, даже при зелёном JSON |
| Final lineage | `v31_final_lifecycle.py`, finalizer | все изменённые PID сохраняются и получают final verification coverage |

## Известные пересечения и проверенные решения

1. DAG, protocol, chapter manifest, monitor и final lineage пересекаются в `run_full_pipeline_v31.ps1`. Проверен синтаксис PowerShell и runner-policy self-test; каждый contract имеет отдельный тест.
2. `RedoFormatting` инвалидирует только finalization, а final quality/review переиспользуются. Это проверено в `self_test_v31.py`.
3. Частичные aggregate не разрешают пропустить model stage; protocol test проверяет missing, invalid JSON и truncated completion counters.
4. Required formatting incident не может быть скрыт final JSON/status; `test_formatting_integrity.py` проверяет блокирующие и optional cases.
5. Final repair добавляет PID в append-only ledger; финализация проверяет coverage именно changed PID, а quarantine остаётся монотонным.

## Результаты audit

- `self_test_v31.py`: PASS (cache identity, DAG redo, final lifecycle, truncate handling, repair fallback).
- `self_test_stage_protocol_v31.py`: PASS (synthetic clean reuse, interrupted/partial resume, invalid aggregate, force and translation).
- `self_test_chapter_resolver.py`: PASS.
- `test_formatting_integrity.py`: PASS, 6 tests.
- Runner preflight/startup/model-policy/monitor PowerShell self-tests: PASS.
- Python compilation, PowerShell AST and `git diff --check`: PASS.
- Production profile comparison: only `ensemble.version` differs (`3.1.2j` -> `3.1.3`); model, context, temperature, top-p, top-k and thinking settings match.

## Review questions

1. Может ли какой-либо invalidated authoritative artifact оставаться входом finalization при каждой комбинации four `Redo*` flags?
2. Есть ли путь, на котором `MODEL_REQUIRED` способен ошибочно стать `REUSED` из-за aggregate с валидным JSON, но неполным семантическим содержимым?
3. Достаточно ли changed-PID ledger защищает от потери PID после residual и final repair, включая quarantine path?
4. Есть ли конфликт между fail-closed formatting блокировкой и terminal status/quality gate, который разрешает output?

Верни findings в формате: severity; файл/функция; конкретный execution path; доказательство; минимальная безопасная правка; тест. Если критических findings нет, напиши `APPROVE FOR RC REVIEW` и перечисли оставшиеся риски.
