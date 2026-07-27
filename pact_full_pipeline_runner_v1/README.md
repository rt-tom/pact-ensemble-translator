# Pact full pipeline runner v1

Автоматически переключает локальные модели и выполняет полный конвейер:

1. Gemma создаёт и санитизирует chapter bible.
2. Gemma переводит (`temperature=0`, thinking off).
3. Qwen проверяет перевод пакетами 8 PID с контекстом ±2.
4. Gemma с thinking budget 128 подтверждает каждое замечание Qwen.
5. Gemma исправляет только детерминированные и подтверждённые замечания.
6. Gemma восстанавливает форматирование и создаёт финальный HTML.

Проектный glossary копируется в отдельный каталог запуска и не изменяется.

## Release deployment tooling

`v31_release_deploy.ps1` supports Windows PowerShell 5.1 and PowerShell 7+.
Its release-manifest hashes are calculated from the exact bytes emitted by
`git cat-file blob`; the script never sends a Git blob through a PowerShell
string pipeline. Unsupported editions or versions stop during preflight with
a clear error.

## Установка

Распаковать папку рядом с проектом или внутрь него. Файлы проекта не заменяются.

Проверка verifier:

```powershell
cd D:\pact\pact_translator_v3\pact_full_pipeline_runner_v1
py .\verify_pipeline_issues.py --self-test
```

## Первый запуск

Рекомендуемый первый тест — глава 60, первая глава после существующего перевода 7.04:

```powershell
cd D:\pact\pact_translator_v3\pact_full_pipeline_runner_v1
Set-ExecutionPolicy -Scope Process Bypass
.\run_full_pipeline.ps1 -Start 60 -End 60 -Reset
```

Для повторного продолжения после сбоя запустить без `-Reset`. Уже завершённые стадии используют сохранённые файлы.

Для главы 148:

```powershell
.\run_full_pipeline.ps1 -Start 148 -End 148 -Reset
```

Результаты сохраняются в:

```text
D:\pact\pact_translator_v3\pipeline_runs\chapter_<start>_to_<end>\
```

Основные файлы:

- `output\*.html` — финальная глава;
- `work\<chapter>\draft_translations.json` — первый перевод Gemma;
- `work\<chapter>\issues.qwen_raw.json` — все замечания Qwen и детерминированные проверки;
- `work\<chapter>\verified_issues.json` — замечания после фильтра Gemma;
- `work\<chapter>\repaired_translations.json` — текст после исправлений;
- `work\<chapter>\quality_report.json` — итоговый отчёт;
- `result_*.zip` — полный диагностический архив.
