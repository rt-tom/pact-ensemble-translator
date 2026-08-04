# V4 B8 — Повторный прогон главы 0001 (валидация B4–B7) (task)

Backing spec:
- `docs/plans/V4_IMPLEMENTATION_ORDER_PLAN_RU.md` (§4, Поток B+ — B8).
- `DECISIONS.md` (2026-08-03: порядок B4–B8, B8 — повторный прогон; 2026-08-04: B7 REVIEW REQUIRED — реальный impact библии нуждается в валидации именно в B8).
- `docs/audits/V4_PHASE12_STRICT_0001_RUN001_ANALYSIS_RU.md` — базовый прогон для сравнения (run_001 выполнен на коде до PR #116 L1/L2b/L3 и PR #117 tracker, до B4–B7).
- Зависит от B4 (JSON-устойчивость), B5 (mixed_script-политика), B6 (quarantined retry), B7 (библия + междуглавная + book-run) — все влиты в `main`.

Target: `main`. Характер: **валидационный прогон, не PR с кодом** — повторный запуск strict-драйвера на главе 0001, сравнение метрик с run_001, запись решения в `DECISIONS.md`. Мелкие фиксы, блокирующие валидацию, — только с записью в `DECISIONS.md` и отдельным обоснованием.

## Зачем это отдельная карточка

run_001 (глава `0001_bonds-1-1`) выявил шесть проблем, которые закрыли B4–B7. B8 — контрольная точка: повторный прогон **той же главы** на текущем `main`, проверяющий, что рефайнменты действительно работают в реальном прогоне, а не только на unit/integration-тестах. Это последний шаг Потока B+; после него открывается Поток D (Phase 6/0D/7).

run_001 (факты для сравнения):
- Глава `0001_bonds-1-1` (книга Bonds, глава 1-1), 400 параграфов, 16 чанков.
- Wall clock **66 937 c ≈ 18,6 часа** (08/02 21:29 → 08/03 16:05), `resumed_from_index: 0`, `halted_early: False`.
- Бэкенд: `local_llama` (`http://127.0.0.1:8094`, `C:\llama-sycl-new\llama-server.exe`, SYCL0); Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf (fidelity + qwen audit), gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf (generator + gemma audit + selector).
- **463 запуска llama-server / 462 перезапуска**, ~2,9 ч (~16% wall clock) на переключения моделей.
- Генерация 32/32 complete; отбор **12 selected / 4 quarantined** (chunk0001 mixed_script «R.D.T.»; chunk0005 пропуск предложения p00099 + род p00095; chunk0009 `grandchild`→`внук` p00193; chunk0010 «well after dark»→«далеко за полночь» p00239).
- step6: **incomplete** — chunk0011 qwen_chapter_audit пустой/невалидный JSON; findings 114 (58 gemma + 55 qwen + 1 deterministic), 95 регионов.
- step7: 2 раунда, 187 repair-записей, 93 закоммичено, **82 долга** (8/8 у chunk0001); terminal **accepted_degraded**.
- Identity run_001: source `2cef6f3a…`, snapshot `5cc3a9f2…`, chunk_plan `b99f1623…`, config `77bd1595…`, backend `c29c9461…`.

## Что сделать

### 1. Подготовка окружения

- Убедиться, что llama-server поднят с обеими моделями (Qwen + gemma) на `127.0.0.1:8094`; **запуск пайплайна — только по явной команде владельца «Запускай»** (инвариант из `DECISIONS.md`).
- Пайплайн сейчас не запущен — подтвердить отсутствие активных процессов перед стартом.
- **Новый run_label и новый out-dir** (например `v4-phase12-strict-0001-run002`): артефакты run_001 (`pact-v4-strict-chapter-trial/v2`) не перезатирать; resume в новом прогоне — только внутри него, не от run_001.
- Проверить наличие входов главы 0001 (chapter HTML, memory/glossary), при необходимости `--mixed-script-allow` для «R.D.T.» (B5: источник сам содержит инициалы; allowlist токенизируется тем же `_SCRIPT_TOKEN_RE`).

### 2. Прогон

- Команда: `python -m pact_full_pipeline_runner_v1.v4_phase12_strict_run --chapter-id 0001_bonds-1-1 --chapter-html <путь> --memory-dir <путь> --out-dir <новый out-dir>` (+ `--mixed-script-allow` при необходимости, `--runtime-config` если нужен).
- Один проход главы 0001 целиком (16 чанков, 400 pid), без остановок, с resume-политикой прогона как обычно.
- Если используется book-run (B7): допустимо прогнать через `v4_book_run.py` с одной главой — главное, чтобы строгий драйвер и promotion (B7) отработали на реальном тексте.

### 3. Сравнение метрик с run_001

Заполнить сравнительную таблицу по артефактам нового прогона (не по памяти):

| Метрика | run_001 | Ожидание B8 | Факт |
|---|---|---|---|
| Wall clock | 66 937 c (~18,6 ч) | меньше (L1/L2b/L3 + меньше долга) | |
| Запуски/перезапуски llama-server | 463 / 462 | ~6 (план: ~190→~6) | |
| Qwen-токены | ~620k | ~166k | |
| chunk0001 | quarantined (mixed_script «R.D.T.»), 8/8 долга | не quarantined, ремонтопригоден (B5) | |
| chunk0005/0009/0010 | quarantined, долг после repair | repaired через B6-цикл или явный `quarantined_final` с задокументированной причиной | |
| step6 (audit) | incomplete (chunk0011 пустой JSON) | complete (B4 JSON-retry) | |
| narrator_gender | смена рода chunk0015→0016 не поймана | консистентен или пойман finding'ом (B7) | |
| repair-долг | 82 | существенно меньше | |
| Terminal status | accepted_degraded | complete; accepted_degraded допустим только с задокументированной причиной | |

- Переключения и токены считать из артефактов/журналов нового прогона (usage-записи, journal.ndjson, run record), а не из плановых оценок.
- Identity-цепочка нового прогона должна быть консистентна во всех артефактах (как в run_001).

### 4. Запись решения

- Результаты B8 (таблица сравнения + вывод по каждому ожиданию: подтверждено / не подтверждено с причиной) — в `DECISIONS.md` тем же коммитом или отдельным docs-коммитом.
- Зафиксировать, что именно валидировал B8: B4 (JSON-retry), B5 (chunk0001), B6 (quarantined), B7 (библия/narrator_gender/promotion), L1/L2b/L3 (PR #116), tracker (PR #117).
- Решение о следующем шаге: Поток D — D1 (Phase 6: role batching, monitor/usage record) → D2 (Phase 0D: non-inferiority policy) → D3 (Phase 7: A/B release decision v3/v4).

## Что НЕ входит в B8

- Новые фичи и рефакторинг кода (кроме мелких фиксов, блокирующих валидацию, — с записью в `DECISIONS.md`).
- Прогон других глав (book-run по нескольким главам — отдельная задача после B8).
- Изменения Phase 1/2, cascade, risk.
- Транслитерация (альтернатива mixed_script) — не реализуем.

## Gate / Acceptance

1. Прогон главы 0001 завершён на текущем `main`, identity-цепочка консистентна, артефакты run_001 не затронуты.
2. Сравнительная таблица метрик заполнена по фактическим артефактам.
3. Каждое ожидание проверено: подтверждено или опровергнуто с причиной (нет «не проверили»).
4. `DECISIONS.md` — запись о результатах B8 + решение о переходе к Потоку D.
5. Если прогон выявил регрессии — карточки/фиксы заведены до D1.

## Роль-сплит

Обычная V4-фаза: «реализует, второй — adversarial review». Для B8: один готовит окружение и запускает прогон, второй — adversarial review результатов (метрик, identity, выводов) до записи в `DECISIONS.md`. Перед стартом спросить, кто что делает.

## Компактный промпт

```text
Выполни v4 B8 (повторный прогон главы 0001, валидация B4–B7) из
docs/plans/V4_IMPLEMENTATION_ORDER_PLAN_RU.md (Поток B+, B8).
Target: main. Прогон strict-драйвера на главе 0001_bonds-1-1 на текущем main
(новый run_label, не затирая артефакты run_001), сравнение метрик с
docs/audits/V4_PHASE12_STRICT_0001_RUN001_ANALYSIS_RU.md, запись результатов
в DECISIONS.md. Запуск пайплайна — только по явной команде «Запускай».
```
