# v3.1.3-rc1 — пакет независимого ревью для Claude

## Задание

Проведи adversarial integration review кандидата `b06599d` (`develop/v3.1.3` против `main`). Не оценивай перевод и не запускай модель. Проверь, что feature contracts не отменяют друг друга на границах resume/redo/finalization.

## Behavioral matrix

| Сценарий | Ожидаемое безопасное поведение | Доказательство |
| --- | --- | --- |
| Clean synthetic aggregate | `REUSED`, без model start | stage protocol self-test |
| Interrupted/missing aggregate | `MODEL_REQUIRED`, resume не выдаёт ложный complete | stage protocol self-test |
| Invalid/truncated aggregate | `MODEL_REQUIRED` (код 22 для invalid) | stage protocol self-test |
| `RedoSourceAnalysis` | invalidate всё downstream | DAG self-test |
| `RedoTranslation` | source analysis reuse; audit/finalization invalidate | DAG self-test |
| `RedoQuality` | translation reuse; audit/repair/finalization invalidate | DAG self-test |
| `RedoFormatting` | только finalization invalidate | DAG self-test |
| Required formatting loss | final output blocked | six formatting regression tests |
| Final repair changes PID | PID остаётся в ledger и final verification coverage | v31 offline self-test |
| Gross/ambiguous final finding | quarantine, не auto-complete | v31 offline self-test |

## Contract boundaries to trace

- `v31_artifact_dag.plan/apply` -> `run_full_pipeline_v31.ps1` -> finalizer.
- `v31_stage_protocol.valid_aggregate` -> PowerShell aggregate model stage.
- `v31_chapter_resolver` -> collector/monitor/runner selected chapters.
- `v31_final_lifecycle.append_ledger` -> `v31_finalize_quality.py` coverage and terminal status.
- formatting integrity result -> HTML finalization and quality gate.

## Audit facts

All offline tests below passed in a development worktree. No model, production pipeline, production cache or production tracked file was changed.

- `self_test_v31.py`
- `self_test_stage_protocol_v31.py`
- `self_test_chapter_resolver.py`
- `test_formatting_integrity.py` (6 tests)
- runner preflight/startup/model-policy/monitor PowerShell self-tests
- Python compileall; PowerShell AST; `git diff --check`

The production/profile comparison found no inference change: all model names, context settings and sampling/thinking values are identical. Only release metadata changes from `3.1.2j` to `3.1.3`.

## Required output

For each finding provide: priority, exact file/symbol, triggering sequence, why an existing test does not cover it, smallest safe fix and a regression test. Explicitly answer whether this candidate is safe for human RC review. Do not recommend merge/deploy; those are intentionally out of scope.
