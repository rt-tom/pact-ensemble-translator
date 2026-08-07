# V4 Phase 12 — отслеживание прогресса прогона по фазам (карточка)

Backing: решение владельца (2026-08-03). Функционал строится **на уже влитом в `main`,
но не задеплоенном коде** — PR #116 (`f867194`, L1/L2b/L3): роль-проходы
`_run_repair_round`, узкий регейт `region_fidelity_gate`, отложенный коммит,
`repair_report` с `rounds`/`debt_trace`. Продакшен на `fa2743c`; деплой до остановки
текущего прогона не выполнять.

## Зачем

В середине Step 6 (аудит) и Step 7 (ремонт) нет ни одного артефакта с прогрессом:
`audit_cache`/`b2_handoff`/`audit_findings` пишутся атомарно в конце Step 6,
`repair_cache`/`repair_report` — только в конце Step 7. Единственный «монитор» —
`server_logs/`, из которого не видно ни текущего юнита/чанка, ни числа
отремонтированных регионов, ни того, какие чанки и куда переданы. Нужен
структурированный read-only трекер: где прогон, что сделано, сколько чанков и в
каком состоянии, что осталось.

## Инварианты

1. **Прогресс — диагностика, не статус.** Новый артефакт не влияет на логику
   пайплайна, resume, кэш-identity, journal schema, терминальные статусы.
   «Зелёный статус» ≠ качество перевода.
2. **Правда из артефактов.** Трекер read-only: не модифицирует `out_dir`, не
   запускает/не останавливает пайплайн и `llama-server`, ничего не выдумывает.
3. **Без изменения identity/кэша.** Не меняются `REPAIR_POLICY_VERSION`, journal
   (`pact-v4-strict-chapter-trial-journal/v2`), `b2_handoff`, audit/repair-cache
   identity, `config_identity`/`backend_identity_hash`. Добавляется только новый
   write-only артефакт.
4. **Resume-aware.** Учитывать `resumed_from_index`, пересборку Step 6 из
   audit-кэша, рестарт Step 7 при отсутствии `repair_cache`.
5. **Graceful degradation.** Без `phase_progress.ndjson` (старые прогоны, текущий
   `run_001`) — грубая инференция по наличию артефактов.

## Что реализовать

### A. Инкрементальный артефакт прогресса (раннер)

Append-only NDJSON `phase_progress.ndjson` в `out_dir` (монотонный, crash-safe, по
образцу journal). События **started + done** (started — до модельного вызова, чтобы
долгий вызов был виден как «сейчас на X»):

- `run_started` (chapter_id, out_dir, started_at, backend_identity_hash,
  resumed_from_index);
- `chunk_started` / `chunk_done` (Steps 1–5: chunk_id, outcome; итоговые исходы
  трекер берёт из journal);
- **Step 6:** `audit_unit_started`/`audit_unit_done` на каждую пару
  `(chunk_id, detector)`; `audit_done`;
- **Step 7:** `repair_round_started` (1/2), `region_started`/`region_done`
  (chunk_id, repair_id, target_pids, action, committed/debt, reason),
  `reaudit_unit_started`/`reaudit_unit_done` (chunk_id, detector), `repair_done`;
- **Step 8:** `formatting_done` (incidents), `terminal`
  (complete / accepted_degraded / failed).

### B. Трекер CLI

`python -m pact_full_pipeline_runner_v1.v4_phase_progress --out-dir <run_dir>
[--watch <сек>]`.

Вывод:

- **Идентичность/liveness:** процесс жив, started_at, elapsed, resumed_from_index.
- **Текущая фаза** с основанием: Steps 1–5 (journal не полон) / Step 6 (journal
  полон, `b2_handoff` нет) / Step 7 (b2_handoff есть, `repair_report` нет; round
  1/2 из событий) / Step 8 / Done (`strict_chapter_trial_record.json` есть).
- **Чанки: сколько передано и куда** (центр) — таблица по всем чанкам: триал
  (pending/generated/gated/selected/quarantined/needs_synthesis/
  incomplete_generation — из journal) → аудит (clean/findings_present/unit_failed/
  no_candidate, audited_candidate_id — из `b2_handoff`) → ремонт (не начат/
  в работе/committed/debt — из `repair_cache`/`repair_report` + событий) →
  форматирование/терминал.
- **Счётчики по фазам:** Steps 1–5 — processed/total и разбивка исходов; Step 6 —
  audit-юниты done/(2×chunks); Step 7 — регионы план/сделано/committed/debt по
  раундам, scope re-audit, blocking findings; Step 8 — formatting incidents,
  терминал.
- **Модельная активность:** текущий `*_started` без `*_done`, число и свежесть
  `server_logs/`.

`--watch` — периодический повторный рендер.

### C. Привязка к слитому (не задеплоенному) main

Step-7-вью отражает новую структуру роль-проходов (все правки Gemma → все регейты
Qwen → все речеки Gemma → отложенный коммит → re-audit → round 2); `repair_report`
читается в актуальной схеме (`status`, `rounds[].records/reaudit_findings/
changed_chunk_ids`, `debt_trace`, `formatting`).

## Явные не-цели

Без веб-дашборда/графиков и оценки качества; без изменения Steps 1–6, gate-логики,
алгоритма ремонта (L1/L2b/L3 уже слит), Phase 5, resume/кэш/identity; текущий
прогон не трогать; деплой не выполнять; `phase_progress.ndjson` не используется в
решениях пайплайна.

## Acceptance criteria

1. События пишутся в каждой точке; файл append-only, crash-safe (частичная запись
   не ломает чтение).
2. На синтетическом каталоге с `phase_progress.ndjson` трекер показывает точный
   прогресс юнитов/регионов, совпадающий с источником.
3. На `run_001` (без `phase_progress.ndjson`) корректно работает coarse-режим
   (фазы, journal, b2_handoff-статусы).
4. Трекер не пишет в out_dir и не трогает пайплайн (тест read-only).
5. Раннер-семантика не изменена: `tests/pact_v4` зелёный (703+), journal/schema/
   identity не менялись; новые тесты на писатель прогресса.
6. Resume-сценарий покрыт тестом.
7. Деплой не выполняется; draft PR.

## Данные для проверки

- `D:\pact\gate_bench_runs\v4_phase12_strict_0001\run_001\` — read-only реплей
  (coarse-режим).
- Синтетические каталоги с/без `phase_progress.ndjson` в фикстурах.

## Открытые пункты (разрешены)

- Гранулярность: **started + done**.
- CLI: **`pact_full_pipeline_runner_v1/v4_phase_progress.py`** (по образцу
  `v4_phase12_strict_run.py`).
- Live: **`--watch`**.

## Корректировки по первому боевому прогону (eff-a1a2, 2026-08-07) — TODO

Наблюдения владельца при мониторинге главы 0002 (фаза repair round 2), что
монитор показывает неверно/вводит в заблуждение:

1. **Фаза Step 7 с активным ремонтом выглядит как «ремонт не начат»**:
   `phase: step7 -- b2_handoff.json exists; repair_report.json absent` —
   `repair_report.json` пишется только в конце Step 7, поэтому «absent» не
   значит «не начат». Монитор должен выводить прогресс ремонта из событий
   `region_done`/`region_*` (regions done/committed/debt), а не только из
   наличия `repair_report.json`. (Реальный случай: `committed=47 debt=26` при
   «repair_report.json absent».)
2. **Step 8 блок на активной фазе 7**: `formatting incidents=None
   blocking=None (no formatting artifacts); terminal=None` — выглядит как
   «8 фаза сломана». Когда Step 8 ещё не начался, выводить явно `Step 8: not
   started (ожидание formatting/terminal)` вместо `None`.
3. **`server_logs` не индикатор живости для remote-прогонов**: файлы
   `opencode_serve_*.log` статичны с момента старта сервера (возраст ~14000s
   при живом прогоне). Живость модели определять по свежести `usage.ndjson`
   (последний `ts`) и `phase_progress.ndjson`, а `server_logs` показывать как
   «age с момента старта сервера» отдельно, без тревожной формулировки.
4. **«no model call currently visible» ложно при активном remote-прогоне**:
   монитор смотрит только пары `*_started`/`*_done` в `phase_progress.ndjson`
   и игнорирует `usage.ndjson` (D1), который пишется на каждый вызов. Считать
   активность модели по последней записи `usage.ndjson` (label, ts) и выводить
   её, если она свежее последнего `*_started`.

## Известные риски

- Шум событий при 95+ регионах — файл лёгкий (десятки КБ), append-only.
- «started без done» при аварии — трекер показывает юнит «в работе»; при резюме не
  восстанавливается (диагностика).
- Coarse-режим для старых прогонов не даёт детализации Step 6/7 — ожидаемое
  ограничение.
