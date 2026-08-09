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
