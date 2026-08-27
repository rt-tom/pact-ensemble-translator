# AGENTS.md — справочник деталей и уроков (вынесено из ядра)

> Этот документ — **справочный**. Нормативные правила — в `AGENTS.md` (ядро,
> авто-загружается воркерами). Сюда вынесены исторические уроки, примеры и
> пояснения, чтобы ядро оставалось компактным. Если правило противоречит
> AGENTS.md — действует AGENTS.md.

## Исторические уроки (почему правила такие)

### B9 — раздувание доски (2026-08-04)

25+ карточек вместо 6: следствие параллельных сессий на одну задачу и
по-фиксовых карточек, а не гранулярности как таковой.

### A1 — ревьюер плодит fix/RV2 (2026-08-06)

Ревьюер создал fix-карточку и RV2 вместо комментариев. Watcher ловит как
PROTOCOL-DRIFT. При разъезде: замечания переносятся на I, developer
возвращается на ту же I; лишние карточки отменяются (complete «отменено:
дрейф протокола») — работа не теряется, она в ветке.

### B14 — RV с --parent висит в todo

RV созданная с `--parent` не может быть заклеймлена, пока родитель жив
(claim-гейт `parents_not_done`). RV создаётся БЕЗ --parent. RV не создаётся
заранее (dependency_wait-цикл = переклеймы и расход токенов: 3 RV висели в
ожидании).

### A1-fix2 / Monitor v2 — авто-декомпозер (2026-08-07)

Ядро Hermes имеет `BLOCK_RECURRENCE_LIMIT=2`: повторный `block <I>
ready_for_review` на той же карточке (штатный фикс-цикл: сдал →
changes-requested → фикс → снова сдал) триггерит `block_loop_detected` →
авто-декомпозер разваливает I на 6–11 дочерних карточек (в т.ч.
`root_assignee: architect`, воркеры-дубли заново реализуют уже закоммиченную
работу) и ломает цикл «2 карточки». Дважды реально случалось (Phase 0 — 11
детей; Monitor v2 — 6 детей).

Правильный фикс-цикл:
1. developer после changes-requested НЕ блокирует I повторно — комментирует
   «фикс готов @ <sha>» и ждёт;
2. архитектор паркует I с `--kind capability` (НЕ `needs_input` — тот же kind
   наращивает счётчик; НЕ `dependency` — автопромоут обратно в ready =
   токен-спин) и создаёт RV2 сам (тупик = архитектор может);
3. перед парковкой проверить `show <I>` на события `decomposed` /
   `block_loop_detected`.

### Monitor v2 — I остаётся blocked после CHANGES REQUESTED (2026-08-07)

«Developer re-claims the same I» работает только пока I в `ready`. Когда RV
завершилась `done` с changes-requested, I остаётся `blocked
ready_for_review` — диспетчер не клеймит blocked карточки, фикс-цикл
замирает без алармов. Fix: архитектор портирует findings на I комментарием,
`unblock` → диспетчер клеймит → developer фиксит на той же I.

### Декомпоз-дубль с running воркером (2026-08-07)

После `block_loop_detected` декомпозер спавнит детей IMMEDIATELY (некоторые
`running` в новых worktrees `wt/t_<child>`) — воркер заново реализует уже
готовую работу и жжёт токены. Recovery: `kanban runs <child>` → найти PID
(Get-CimInstance Win32_Process, grep CommandLine) → `taskkill /PID <n> /F`
(убить родителя; uv-ребёнок умрёт сам) → complete детей «отменено:
декомпоз-дубль, работа уже в ветке» → unlink связей → создать чистую RV.

### Phase 0 — «простая задача» ушла в 6 раундов ревью (2026-08-07)

Задача «оценить данные прогона» прошла 6 раундов CHANGES REQUESTED:
ревьюер каждый раунд находил новые микро-требования (n_sessions, UNC-пути,
numeric-валидация, duplicate-header, top-5 heading). Правило сходимости:
ревью аналитических задач проверяет заявленный scope и корректность
цифр/выводов, НЕ расширяет требования; старая база ветки (дифф «удаляет»
чужие коммиты) — не блокер содержания, а отдельный пункт «перебазировать
перед мержем». Архитектор вмешивается: собирает замечания в один список,
одна итерация фикса.

## Примеры из истории (для понимания контекста)

- A2: lazy balanced-only — run_005 имел 2/14 случаев, где оба кандидата
  прошли Qwen и выбирался fidelity_first; A2 убрал это сравнение (генерация
  только balanced, fidelity — лениво при fail).
- B12 batching ограничен work units одного chunk; deterministic gates
  остаются per-region; malformed batch verdict fail-closed.
- B13: финальный translations.json перезаписывается из
  repair_report.final_translation (полный PID-покрытие).
- B14: `<em>` = курсив, нормализуется перед mixed-script проверкой.
- Eff-a1a2 прогон: A1 (glossary budgeter) дал −16.5% input генерации; A2 —
  1 кандидат на chunk; 609 вызовов/$2.20 на две главы (0001: 312/$1.20,
  0002: 297/$1.00); re-audit — 47% вызовов / 78% стоимости (Qwen).
- Benchmark v4.1 (2026-08-07, владелец): DeepSeek High ≈8.3 vs DeepSeek 0
  ≈7.4 (reasoning важен!); локальная Gemma reasoning=0 ≈8.0–8.1; pipeline
  High ≈7.9 vs independent High ≈8.3 (≈−0.4 translation-stage penalty);
  Qwen лучше как semantic verifier, чем translator; T-lite слаб.

## Справочные команды

- Полный suite: `C:\Python314\python.exe -m pytest tests --ignore=deployment_backups -q -p no:cacheprovider --basetemp=<tmp>` (для параллельных прогонов — отдельный --basetemp).
- Kanban статус-чек: `hermes kanban list` + `hermes kanban diagnostics`.
- Watcher: `profiles/architect/logs/kanban_cycle_watch.log` (cron 15m).

### Финальные файлы перевода главы и сборка книги (2026-08-14)
- Иерархия: translations_raw.json (выход генератора) → translations_edited.json (после R-редактора) → translations_repaired.json (после repair, ФИНАЛЬНЫЙ) → translations.json (копия финала, пишется в конце step7).
- v4_book_html.py читает ИМЕННО translations.json из run-dir — это корректный финальный перевод (== repaired).
- v4_book_run.py НЕ собирает книгу (только book_run.json + кандидаты глоссария/памяти + promote). Склейка глав в book.html — отдельный шаг:
  python -m pact_full_pipeline_runner_v1.v4_book_html --out-base <out-base> --run-dirs chapter_*_bonds-1-* --chapter-html-pattern 'D:/pact/pact_chapters/{chapter_id}.html' --title 'Книга'

### Команды запуска прогона (RT / media) (2026-08-27)
Диспетчер: `pact_full_pipeline_runner_v1.v4_run`, подкоманда `book` (основной workflow — последовательные главы с общей памятью). Простой режим: `--local` / `--remote [translator/reviewer]` + `--chapters N|N-M`.

Пример: перевод главы 01, переводчик `sol`, ревьюер `terra` (bare-алиасы резолвятся через `providers.yaml` → `openai/gpt-5.6-sol` / `openai/gpt-5.6-terra`).

**Media (прямо на media, bash):**
```bash
cd ~/projects/pact-ensemble-translator
python3 -m pact_full_pipeline_runner_v1.v4_run book --chapters 1 --remote "sol/terra"
```
- Источник глав — `/home/rt/pact_chapters` (резолв `--chapters 1` → `0001_*.html`).
- В интерактивном shell владельца работает `python`; в агентской неинтерактивной сессии — только `python3`.

**RT (прямо на RT, PowerShell в `D:\pact\pact_translator_v4_1`):**
```powershell
cd D:\pact\pact_translator_v4_1
python -m pact_full_pipeline_runner_v1.v4_run book --chapters 1 --remote "sol/terra"
```
- Источник глав — `D:/pact/pact_chapters`.

**RT (из media через ssh rt + powershell)** — см. раздел «Деплой на RT через ssh rt + powershell»:
```bash
ssh rt 'powershell -NoProfile -Command "cd D:/pact/pact_translator_v4_1; python -m pact_full_pipeline_runner_v1.v4_run book --chapters 1 --remote sol/terra"'
```

Ключевые нюансы:
- `--remote "sol/terra"`: слэш в простом режиме = `переводчик/ревьюер` (Bare-алиасы). НЕ путать с `provider/alias`. `--translator`/`--reviewer` флаги НЕЛЬЗЯ комбинировать с `--remote` (взаимоисключающи, ошибка).
- `--chapters N` → `NNNN_*.html`; диапазон `--chapters 1-5`. Каждый номер резолвится в ровно один файл (иначе fail-closed до старта).
- `--preflight` — check-only: валидирует профиль/пути/env/резолв главы и алиасов, НЕ запускает пайплайн. Используйте перед полным прогоном.
- `reasoning` по умолчанию 3; managed-сервер для simple remote включается автоматически.
- Обязательные env-переменные (берутся из окружения интерактивной сессии владельца, НЕ из репозитория): `OPENCODE_SERVER_USERNAME`, `OPENCODE_SERVER_PASSWORD` (см. `configs/runtime_remote.example.yaml`, `auth.basic_env`). Агентская неинтерактивная оболочка их обычно не наследует — префлайт в ней покажет «missing env», хотя у владельца PASS.
- Сам пайплайн запускает владелец вручную (manual-only); агент только готовит команды и валидирует префлайтом.

#### Монитор прогона (v42-monitor-compact)
Скрипт: `pact_full_pipeline_runner_v1/monitor_pipeline.ps1` (компактный `run-progress-monitor`; читает `phase_progress.ndjson` из каталога прогона). Запуск из корня репозитория на RT (`D:\pact\pact_translator_v4_1`):
```powershell
# Надёжный вариант — явный путь к каталогу прогона. v4_run пишет вывод в
# D:\pact\gate_bench_runs\book_<range>_<local|remote>_<timestamp>:
powershell -ExecutionPolicy Bypass -File pact_full_pipeline_runner_v1\monitor_pipeline.ps1 -RunRoot D:\pact\gate_bench_runs\book_0001-0001_remote_2026xxxx_xxxxxx

# Последний прогон под ProjectRoot\pipeline_runs (дефолтный авто-резолв):
powershell -ExecutionPolicy Bypass -File pact_full_pipeline_runner_v1\monitor_pipeline.ps1 -ProjectRoot D:\pact\pact_translator_v4_1

# Конкретная глава/диапазон (→ ProjectRoot\pipeline_runs\chapter_N_to_M):
powershell -ExecutionPolicy Bypass -File pact_full_pipeline_runner_v1\monitor_pipeline.ps1 -ProjectRoot D:\pact\pact_translator_v4_1 -Start 1 -End 1

# Однократный снимок без цикла обновления (интервал -RefreshSeconds, по умолч. 5):
powershell -ExecutionPolicy Bypass -File pact_full_pipeline_runner_v1\monitor_pipeline.ps1 -ProjectRoot D:\pact\pact_translator_v4_1 -Once
```
Параметры: `-RunRoot` (явный каталог прогона — самый надёжный), `-ProjectRoot` (дефолт устаревший `D:\pact\pact_translator_v3` → переопределять на `D:\pact\pact_translator_v4_1`), `-Start`/`-End` (→ `pipeline_runs\chapter_N_to_M`), `-RefreshSeconds` (по умолч. 5), `-Once`, `-NoClear`. Для v4_run-прогонов (вывод в `gate_bench_runs\book_*`) рекомендуется `-RunRoot` с явным путём к `book_*`-каталогу; авто-резолв через `pipeline_runs` рассчитан на старую раскладку.

**Media (Linux) — Python-монитор `v4_phase_progress` (canonical для book-прогонов):** `monitor_pipeline.ps1` — это PowerShell/RT, на media не запускается. Для media-прогонов используйте Python-монитор (читает `phase_progress.ndjson` read-only, пайплайн не трогает).

Два режима (`--out-dir` / `--out-base` — взаимоисключающие):
- `--out-base <папка_книги>` — **book-режим**: передаётся сама папка `book_*`, скрипт сам ходит по `chapter_*`-подкаталогам и показывает активную главу. **Именно это нужно для book-прогона.**
- `--out-dir <chapter_*`-подкаталог>` — single-chapter режим: один конкретный каталог главы.

```bash
# media (Linux), из корня репозитория (всегда с cd!):
cd ~/projects/pact-ensemble-translator
# book-прогон: --out-base на ПАПКУ КНИГИ (скрипт сам обходит chapter_* и показывает активную главу)
python3 -m pact_full_pipeline_runner_v1.v4_phase_progress --out-base /home/rt/pact_runs/outputs/book_0001-0001_remote_20260827_063641_998665

# цикл обновления раз в SEC секунд (авто-поиск свежего book_*-каталога):
cd ~/projects/pact-ensemble-translator
python3 -m pact_full_pipeline_runner_v1.v4_phase_progress --out-base "$(ls -dt /home/rt/pact_runs/outputs/book_0001* | head -1)" --watch 5
```
Пример вывода (per-phase, без 6-line cap, `--out-base` добавляет book-таблицу + промошен):
```
== V4 run progress: /home/rt/pact_runs/outputs/book_0001-0001_remote_.../chapter_0001_... ==
[0001_bonds-1-1] run 1322s · quiet ?
  Entity extraction        : сущностей: 12 | claims: verified 4 / candidate 2
> Whole-chapter translation: attempt 1/3 | source 286 слов → перевод 1200 слов · PID ok
  R-editor                 : chunks done=2/2 | safe (применено)=5 | review (предложено)=1
  Chapter audit            : chunks done=8/8 | findings per chunk: [3, 0, 1, ...] | всего 12
  Selective repair         : batches done=2/2 | repaired per batch: [1/1, 1/2] | findings eligible: 4 | PID edits committed: 2
  Re-audit scope           : chunks done=2/2 | residual: 0 | debt: 0
  Glossary                 : 12 proposals
  Formatting               : spans 102/102 · incidents 0
usage: 42 calls in=1.2k out=800 reas=500 $1.23
Glossary promoted: 153 → glossary.json · 7 → memory
```
Формат заголовка — `[<id>] run <elapsed> · quiet <age>` (без `mode=fine`, без дублирующих `status:`/`phase:`); каждая фаза — одна строка, активная помечена `>`; `Glossary`/`Formatting` появляются только когда есть `glossary_proposals.json`/`formatting_report.json`; `Glossary promoted: … → glossary.json · … → memory` — book-уровень (`--out-base` + `--memory-dir`, по умолчанию state-root).

Вывод run-каталога на media: `/home/rt/pact_runs/outputs/book_<range>_<local|remote>_<timestamp>`; артефакты прогона (`phase_progress.ndjson`, `server_logs`) лежат внутри `book_*/chapter_<NNNN>_*/`, НО для book-режима указывается сама папка книги (`--out-base`), а не подкаталог главы. Свежий book-каталог: `ls -dt /home/rt/pact_runs/outputs/book_0001* | head -1`.

**Ландшафт мониторов (чтобы не путаться):** `monitor_pipeline_v31.ps1` — устаревший v3.1-монитор (только в старых handoff-доках, к v4 не относится); `monitor_pipeline.ps1` — новый компактный v42-монитор (PowerShell/RT); `v4_phase_progress.py` — Python-ядро монитора (book/chapter режимы, работает на media). Изменение **v42-monitor-compact (PR #220, `aa08858`)** добавило/переработало новые мониторы; старый `v31` оставлен как legacy и v4-пайплайном не используется.

### Деплой на RT через ssh rt + powershell (2026-08-27)
- Хост RT доступен по ssh-алиасу `rt` (ключ `~/.ssh/id_rt`, `IdentitiesOnly yes`). Дефолтный удалённый шелл — `cmd`, но `powershell`/`pwsh` вызываются явно и работают: `ssh rt 'powershell -NoProfile -Command "Write-Host OK"'`.
- Деплой-синхронизация продакшн-чекаута (per AGENTS.md: после каждого деплоя `git pull --ff-only` на RT). Проверенный вариант:
  `ssh rt 'powershell -NoProfile -Command "git -C D:/pact/pact_translator_v4_1 pull --ff-only"'`
- Пути на RT — Windows; внутри powershell-команды используйте `D:/...` (прямой слэш). Внутри cmd-шелла одинарные кавычки не распознаются — оборачивайте удалённую команду внешними одинарными кавычками, а строки/пути внутри powershell — двойными.
- Сам пайплайн удалённо НЕ запускать: владелец стартует его на RT вручную (manual-only).
