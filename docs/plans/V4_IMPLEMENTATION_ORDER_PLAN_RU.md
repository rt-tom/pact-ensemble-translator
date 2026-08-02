# Pact v4 — план порядка реализации (quality engine + OpenCode runtime)

Дата: 2026-08-01
Статус: approved implementation order
Целевая ветка: `v4.0`

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

## 2. Статус по коду (на 2026-08-01, ветка `v4.0`)

| Фаза | Статус |
|---|---|
| 0A / 0B / 0C — harness, golden set, baseline + gate | done |
| 0D — pre-registered non-inferiority policy | **нет** |
| 1A / 1B / 1C — contracts, JSON memory, chunk planner | done |
| 2A / 2B / 2C — risk, A/B generation, cascaded selection | done |
| 3A — immutable finding store | done (`pact_v4/phase3/findings.py`) |
| 3B — windowed assembled-chapter audit | модуль + тесты есть (`pact_v4/phase3/audit.py:run_chapter_audit`), **не встроен в драйвер**, нет runtime `QwenAuditEvaluator`/`GemmaAuditEvaluator` |
| 4A / 4A2 / 4B — repair, Gemma closure, convergence, terminal | **нет** (только контракты `Repair`/`TerminalState` в `pact_v4/phase1/models.py`) |
| 5 — formatting alignment | **нет** (только deterministic checks в `pact_v4/_integrity_checks.py`) |
| 6 — operations (role batching, reloads) | частично (lifecycle timing в strict-драйвере) |
| 7 — A/B release decision | **нет** |

## 3. Доработки Phase 1–3

1. **3B → драйвер**: runtime `QwenAuditEvaluator`/`GemmaAuditEvaluator` +
   вызов `run_chapter_audit` как Step 6 после выбора всех chunks + persist
   findings. Strict-драйвер сейчас покрывает только Phase 1–2.
2. **Loop-order батчинг в 3B**: `for detector in (qwen, gemma): for chunk ...`
   вместо per-chunk interleaving — сокращение перезагрузок single-resident
   (DECISIONS 2026-08-01).
3. **Resume-пробел**: `generation_outcomes.json` невоспроизводим после resume
   (записан 2026-07-31, не исправлен).
4. **Weak-spot strengthening**: Q5 ablation по design-note
   `docs/plans/V4_WEAK_SPOT_STRENGTHENING_RU.md`.
5. **1B**: `degraded_continuity_overlay` + atomic memory promotion/conflict/
   rollback — доработка по мере встраивания batch-first.
6. **Runner decoupling**: strict- и sequential-runner импортируют приватные
   хелперы из `draft_runner` (`_left_ru_for_chunk`, `_glossary_entries` и
   др.); при принятии strict (DECISIONS 2026-08-01) выносим их в общий
   модуль, `draft_runner` → reference/fixture. Карточка:
   `docs/plans/V4_RUNNER_SHARED_HELPERS_TASK_RU.md` (A2).

## 4. Порядок реализации

Три независимых потока. Порядок внутри каждого — по зависимостям; один PR = одна
workstream-тема, темы не смешивать.

### Поток A — provider boundary и runner decoupling (фундамент для Phase 3B/4)

- **A1 = PR 1 интеграционного плана**: `CompletionBackend` contracts,
  `LocalOpenAIBackend` поверх текущего `ApiClient`, backend-neutral адаптеры
  (`BackendModelCaller`/`BackendQwenEvaluator`/`BackendGemmaSelector`), нынешние
  `Http*` — compatibility wrappers.
- **A2 = `docs/plans/V4_RUNNER_SHARED_HELPERS_TASK_RU.md`** (отдельная
  карточка, не PR интеграционного плана): вынос приватных хелперов из
  `draft_runner` в общий модуль + фиксация статуса `draft_runner`
  (reference/fixture) и `sequential_runner` (archive vs regression fixture) —
  требование решения о strict (DECISIONS 2026-08-01).
- Gate: local strict tests + chapter fixture неизменны по смыслу.

### Поток B — достройка quality engine (ядро v4)

- **B1**: runtime audit evaluators на новом boundary + встраивание
  `run_chapter_audit` (Step 6) в strict-драйвер + persist findings. Валидация на
  chapter_046 `run_003` (с Qwen `max_tokens` fix).
- **B2**: Phase 4A/4A2/4B — minimal region/PID repair, обязательный Gemma
  re-check, targeted convergence, terminal states, regression.
- **B3**: Phase 5 formatting alignment (exact → occurrence-aware → conservative
  fuzzy → model fallback) + fixtures. Обязательно до «финального pipeline»
  (заметка в Phase 5 согласованного плана).

### Поток C — OpenCode (параллельно B, после A1)

- **C1 = PR 2**: `OpenCodeServerBackend` + fake-server contract suite (до любых
  платных вызовов).
- **C2 = PR 3**: tagged backend configs, `RuntimeCoordinator`, journal/record v2,
  resume identity. Желательно до live-прогонов Phase 4, чтобы не перепрогонять
  главу на двух record-схемах.
- **C3 = PR 4**: CLI `--runtime-config`, live one-chunk smoke → chapter_046
  remote trial → запись решения в `DECISIONS.md`.

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
