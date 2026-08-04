# Pact v4 — план порядка реализации (quality engine + OpenCode runtime)

Дата: 2026-08-03 (обновлено)
Статус: approved implementation order
Целевая ветка: `main` (v4 tree)

## 1. Основание

- Согласованный фазовый план:
  `docs/architecture/V4_FINAL_REVIEW_AND_IMPLEMENTATION_PLAN_RU_v2.md`
  (порядок `0A→0B→0C→0D→1A→1B→1C→2A→2B→2C→gate→3A→3B→4A→4A2→4B→5→6→7`).
- Интеграция внешних моделей через OpenCode:
  `docs/plans/V4_OPENCODE_REMOTE_MODELS_INTEGRATION_PLAN_RU.md` (PR 1–4).
- Целевая архитектура:
  `docs/architecture/V5_UNIVERSAL_LITERARY_TRANSLATOR_ARCHITECTURE_RU.md`.
- Архитектурные решения: `DECISIONS.md`.

OpenCode-интеграция — отдельная transport-workstream: она не заменяет
согласованный фазовый план Phase 0–7 и не меняет алгоритм перевода. Решение
следовать этому плану записано в `DECISIONS.md`.

## 2. Статус по коду (на 2026-08-03, ветка `main`)

| Фаза | Статус |
|---|---|
| 0A / 0B / 0C — harness, golden set, baseline + gate | done |
| 0D — pre-registered non-inferiority policy | **нет** |
| 1A / 1B / 1C — contracts, JSON memory, chunk planner | done |
| 2A / 2B / 2C — risk, A/B generation, cascaded selection | done |
| 3A — immutable finding store | done (`pact_v4/phase3/findings.py`) |
| 3B — windowed assembled-chapter audit | done (B1, влит в `main`) |
| 4A / 4A2 / 4B — repair, Gemma closure, convergence, terminal | done (B2, влит в `main`) |
| 5 — formatting alignment | done (B3, влит в `main`) |
| 6 — operations (role batching, reloads) | частично (L1/L2b/L3 оптимизация, progress tracker) |
| 7 — A/B release decision | **нет** |

## 3. Доработки Phase 1–3

1. **3B → драйвер**: runtime `QwenAuditEvaluator`/`GemmaAuditEvaluator` +
   вызов `run_chapter_audit` как Step 6 после выбора всех chunks + persist
   findings. **done (B1)**.
2. **Loop-order батчинг в 3B**: `for detector in (qwen, gemma): for chunk ...`
   вместо per-chunk interleaving — сокращение перезагрузок single-resident
   (DECISIONS 2026-08-01). **done (B1)**.
3. **Resume-пробел**: `generation_outcomes.json` невоспроизводим после resume
   (записан 2026-07-31). **done (B1-followup: кумулятивный merge + selection_meta.json sidecar)**.
4. **Weak-spot strengthening**: Q5 ablation по design-note
   `docs/plans/V4_WEAK_SPOT_STRENGTHENING_RU.md`. **pending — часть B7 (библия/narrator_gender)**.
5. **1B**: `degraded_continuity_overlay` + atomic memory promotion/conflict/
   rollback — доработка по мере встраивания batch-first. **pending — часть B7 (междуглавная аккумуляция)**.
6. **Runner decoupling**: strict- и sequential-runner импортируют приватные
   хелперы из `draft_runner` (`_left_ru_for_chunk`, `_glossary_entries` и
   др.); при принятии strict (DECISIONS 2026-08-01) выносим их в общий
   модуль, `draft_runner` → reference/fixture. Карточка:
   `docs/plans/V4_RUNNER_SHARED_HELPERS_TASK_RU.md` (A2). **done (A2)**.

## 4. Порядок реализации

Три независимых потока. Порядок внутри каждого — по зависимостям; один PR = одна
workstream-тема, темы не смешивать.

### Поток A — provider boundary и runner decoupling (фундамент для Phase 3B/4)

- **A1 = PR 1 интеграционного плана**: `CompletionBackend` contracts,
  `LocalOpenAIBackend` поверх текущего `ApiClient`, backend-neutral адаптеры
  (`BackendModelCaller`/`BackendQwenEvaluator`/`BackendGemmaSelector`), нынешние
  `Http*` — compatibility wrappers. **done**.
- **A2 = `docs/plans/V4_RUNNER_SHARED_HELPERS_TASK_RU.md`** (отдельная
  карточка, не PR интеграционного плана): вынос приватных хелперов из
  `draft_runner` в общий модуль + фиксация статуса `draft_runner`
  (reference/fixture) и `sequential_runner` (archive vs regression fixture) —
  требование решения о strict (DECISIONS 2026-08-01). **done**.
- Gate: local strict tests + chapter fixture неизменны по смыслу.

### Поток B — достройка quality engine (ядро v4)

- **B1**: runtime audit evaluators на новом boundary + встраивание
  `run_chapter_audit` (Step 6) в strict-драйвер + persist findings. Валидация на
  chapter_046 `run_003` (с Qwen `max_tokens` fix). **done**.
- **B2**: Phase 4A/4A2/4B — minimal region/PID repair, обязательный Gemma
  re-check, targeted convergence, terminal states, regression. **done**.
- **B3**: Phase 5 formatting alignment (exact → occurrence-aware → conservative
  fuzzy → model fallback) + fixtures. Обязательно до «финального pipeline»
  (заметка в Phase 5 согласованного плана). **done**.

### Поток B+ — post-run_001 quality refinements (после B1–B3)

Порядок по зависимостям: B4 → B5 → B6 → B7 → B8.

- **B4**: JSON-устойчивость — retry для пустого/обрезанного JSON в qwen-audit
  и repair. База для всех последующих задач.
  `docs/plans/V4_B4_JSON_RESILIENCE_TASK_RU.md`.
- **B5**: mixed_script-политика — allowlist легитимных латинских инициалов/имён
  или транслитерация. Разблокирует chunk0001 (integrity failed).
  `docs/plans/V4_B5_MIXED_SCRIPT_POLICY_TASK_RU.md`.
- **B6**: quarantined-чанки — отдельный цикл ремонта (или признание карантинных
  чанков финальными с best-variant). Зависит от B4.
  `docs/plans/V4_B6_QUARANTINED_RETRY_TASK_RU.md`.
- **B7**: библия + междуглавная + book-run — импорт фактов из v3, рендер в
  промпты (генерация + fidelity + аудит), narrator_gender, междуглавная
  аккумуляция (promote при `complete` + non-quarantined при `accepted_degraded`),
  book-run wrapper. Зависит от B4.
  `docs/plans/V4_B7_BIBLE_AND_CROSS_CHAPTER_TASK_RU.md`.
- **B8**: повторный прогон главы 0001 — валидирует B4–B7 + L1/L2b/L3 + tracker.
  Ожидается: консистентность рода/персонажей, разблокировка chunk0001,
  ~190→~6 переключений, ~620k→~166k Qwen-токенов.
  `docs/plans/V4_B8_CHAPTER_0001_REVALIDATION_TASK_RU.md`.
  Статус: карточка создана, CLI-фиксы влиты в main (PR #127, --run-label);
  прогон главы 0001 выполняется (run_002_remote, remote-бекенд opencode serve,
  2026-08-04): генерация завершена (16/16 чанков), идёт Step 6 аудит;
  по итогам прогона — см. БЛОКЕР ниже.
- **B9**: генератор кандидатов глоссария + сбор наблюдений в проде — перенос
  v3-механики `glossary_candidates` (`pact_translate_v3.py`) в v4: вызов
  `add_observation` (`MemoryManager`, `pact_v4/phase1/memory.py`) в book-run
  после главы, shadow-наблюдения → `observations.json` → `promote` (B7) при
  complete/accepted_degraded. Отдельная задача, параллельна Потоку D, НЕ
  блокирует B8-прогон и D1 (решение владельца 2026-08-04): `observations.json`
  не входит в снапшот/identity и не влияет на промпты/гейты; кандидаты по
  главе 0001 собираются офлайн из артефактов B8-прогона (генератор
  детерминированный, модели не нужны).
  `docs/plans/V4_B9_GLOSSARY_OBSERVATIONS_TASK_RU.md`.
- **БЛОКЕР (2026-08-04, из DECISIONS.md)**: перед следующей карточкой B-серии
  проверить 3× `incomplete_generation` в run_002_remote (chunk0010/0012/0015,
  одна из двух ролей не прошла валидацию; локальный run_001 — 0 таких):
  после завершения прогона прочитать `generation_outcomes.json` (пишется в
  конце) → точный GenerationErrorCode упавших ролей → отделить обрыв
  remote-канала от систематического невалидного JSON → решить, нужен ли retry
  упавшей роли (если да — отдельная карточка). Гипотеза «gemma отдаёт один
  перевод при почти одинаковых вариантах» кодом не подтверждается (Phase 2B —
  два отдельных вызова fidelity_first/balanced_literary, правила «один вместо
  двух» нет).

### Поток C — OpenCode (параллельно B, после A1)

- **C1 = PR 2**: `OpenCodeServerBackend` + fake-server contract suite (до любых
  платных вызовов). **done**.
- **C2 = PR 3**: tagged backend configs, `RuntimeCoordinator`, journal/record v2,
  resume identity. Желательно до live-прогонов Phase 4, чтобы не перепрогонять
  главу на двух record-схемах. **done**.
  - **В карточку C2 включить (маркер, реализация не начата)**: managed-режим
    `opencode serve` — новый модуль
    `pact_v4/runtime/opencode_server_lifecycle.py` (самозапуск, health-wait,
    PID ownership, `assert_port_free_or_owned`), `runtime.server_mode:
    managed | external`, эфемерные basic-auth креды в env subprocess'а,
    `--pure`, fail-fast при занятом порту. Решение: `DECISIONS.md`
    (2026-08-01). **done (C2)**.
- **C3 = PR 4**: CLI `--runtime-config`, live one-chunk smoke → chapter_046
  remote trial → запись решения в `DECISIONS.md`. **done**.

### Поток D — финал

- **D1 = Phase 6**: role batching (loop-order fix), fewer reloads, monitor/usage
  record.
- **D2 = Phase 0D**: pre-registered non-inferiority policy.
- **D3 = Phase 7**: A/B release decision v3/v4 (зависит от 0D + benchmark).

## 5. Зависимости и guardrails

- A1 ставится первым: дешёвый рефакторинг без изменения поведения, разблокирует
  и B, и C; Phase 3B/4 пишутся сразу backend-neutral.
- B и C идут параллельно с приоритетом B (ядро качества важнее transport).
- strict-архитектура — производственная архитектура v4 (решение владельца,
  `DECISIONS.md`); batch-first/sequential остаются экспериментальными.
- `draft_runner` → reference/fixture, `sequential_runner` → archive vs
  regression fixture; решение и вынос хелперов — A2 (до архивации любых
  runner-модулей).
- Identity/resume, secrets, no silent fallback, transport failure ≠ semantic gate
  failure — по интеграционному плану (§5.4/§10/§12).
- Phase 6/7 — только после замыкания quality engine и наличия 0D-политики.
- **B4–B8 порядок**: B4 (JSON-устойчивость) — база для всех; B5 (mixed_script) и
  B6 (quarantined) — после B4; B7 (библия + междуглавная) — после B4; B8
  (повторный прогон) — после B5/B6/B7, валидирует все refinements + L1/L2b/L3 +
  tracker. Решение владельца 2026-08-03. B9 (генератор кандидатов глоссария +
  add_observation) — отдельная задача, параллельна Потоку D, не блокирует
  B8-прогон и D1 (решение владельца 2026-08-04).
