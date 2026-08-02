# V4 A2 — Runner decoupling: общий модуль хелперов (task)

Backing spec:
- `DECISIONS.md` (2026-08-01: strict — производственная архитектура v4;
  `draft_runner` → reference/fixture, его приватные хелперы вынести в общий
  модуль, чтобы strict-runner не зависел от архивного файла).
- `docs/plans/V4_IMPLEMENTATION_ORDER_PLAN_RU.md` (§4, Поток A — A2; §3 п.6).

Target: `v4.0`. Draft PR. Характер: чистый рефакторинг **без изменения
поведения** (LOW RISK).

## Зачем это отдельная карточка

С принятием strict-архитектуры (DECISIONS 2026-08-01) `draft_runner` теряет
роль production-driver'а и становится reference/fixture (рассчитан на
одновременно резидентные Gemma+Qwen, недоступные на текущем железе). Но два
оставшихся runner'а импортируют его приватные хелперы:

- `pact_v4/pipeline/v4_phase12_strict_runner.py:88-94` —
  `_glossary_entries`, `_left_ru_for_chunk`, `_record_selection`,
  `_risk_for_chunk`, `_serialize_generation_outcome`;
- `pact_v4/pipeline/v4_phase12_sequential_runner.py:81` — `_glossary_entries`.

Хелперы топология-агностичны (left_context assembly, glossary parsing,
selection recording, risk, сериализация) и живут в `draft_runner` только
исторически. Зависимость от приватных функций модуля, который переводим в
fixture/архив, — ломкое сцепление: любая правка `draft_runner` или его
архивация ломает strict/sequential.

## Что реализовать

1. Новый модуль `pact_v4/pipeline/_shared_runner_helpers.py` (или
   `runner_helpers.py`): перенести 5 хелперов + `_glossary_entries` без
   изменения сигнатур и поведения.
2. Перевести импорты в `v4_phase12_strict_runner.py` и
   `v4_phase12_sequential_runner.py` на общий модуль; `draft_runner`
   перестаёт быть источником этих функций для runner'ов.
3. Обновить остальных потребителей (`v4_phase12_sequential_run.py`,
   `v4_phase12_draft_run.py`, `v4_v3_draft_compare.py`, тесты
   `tests/pact_v4/pipeline/`), если они импортируют эти хелперы.
4. Зафиксировать статусы: `draft_runner` — reference/fixture,
   `sequential_runner` — archive vs regression fixture; запись в
   `DECISIONS.md` в этом же коммите (обязательно, AGENTS.md).

## Вне scope

- Provider boundary / `CompletionBackend` (A1), OpenCode (Поток C),
  journal v2, strict-runner оркестрация.
- Логика left_context / glossary / risk / selection / сериализации.
- Prompt templates, Phase 1/2, cascade, risk, chunking, repair policy.

## Тесты

- Существующий suite `tests/pact_v4/` зелёный (особенно
  `pipeline/test_v4_phase12_strict_runner.py`,
  `test_v4_phase12_sequential_runner.py`, `test_v4_phase12_draft_runner.py`).
- Регрессионная проверка: strict/sequential не импортируют хелперы из
  `draft_runner` (grep-ассерт в тесте или проверка импортов).

## Gate / Acceptance

1. `v4_phase12_strict_runner.py` и `v4_phase12_sequential_runner.py` не
   содержат импортов из `v4_phase12_draft_runner`.
2. Поведение идентично: полный `tests/pact_v4/` зелёный.
3. `DECISIONS.md` содержит запись о статусе draft/sequential.

## Компактный промпт

```text
Реализуй v4 A2 из docs/plans/V4_RUNNER_SHARED_HELPERS_TASK_RU.md
и docs/plans/V4_IMPLEMENTATION_ORDER_PLAN_RU.md (Поток A, A2).
Target: v4.0. Draft PR. Не трогай v3 и production; не меняй логику
хелперов и Phase 1/2.
```
