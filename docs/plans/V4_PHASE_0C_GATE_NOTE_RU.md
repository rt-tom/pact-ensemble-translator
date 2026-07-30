# V4 Phase 0C — Gate (фиксация policy для Phase 1 и Phase 2)

Дата: 2026-07-30. Статус: baseline измерен, Gate фиксирует policy.
Gate-only: документация, schema, producer (`v4_phase0c_baseline.py`) и
regression tests. Интеграция в runtime consumers Phase 1C/2A/2B/2C —
отдельные тематические PR (см. §5).

## Опорные записи

- Baseline: `D:\pact\gate_bench_runs\phase0c_track_a_001\phase0c_result.json`
  (`pact-v4-phase0c-result-record/v1`, `tool_version pact-0c/0.2`,
  `generated_at 2026-07-30T18:06:57+00:00`).
- Track A: chapter 046 / Phase 0B golden set, 57 accepted PID,
  43 needs_review (исключены из численных метрик), 0 rejected, 0 gaps
  во всех ячейках.
- Track B: chapter 100 v3.1 production run
  (`D:\pact\pact_translator_v3_v31_production\pipeline_runs\chapter_100_to_100_v31`,
  run_identity `91e2d8ab...d1dbb52e09`).

**Tool version 0.1 → 0.2.** Live record сгенерирован `pact-0c/0.2`
(внешний прогон от 2026-07-30). В репозитории до этого PR константа
`TOOL_VERSION` в `pact_full_pipeline_runner_v1/v4_phase0c_baseline.py`
всё ещё была `pact-0c/0.1`. Этот PR поднимает её до `0.2` и обновляет
assert в `self_test_v4_phase0c_baseline.py`, чтобы репозиторий снова
мог воспроизвести именно ту baseline-запись, на которую Gate
ссылается как на источник истины (`generated_at` + `tool_version` —
это identity record'а, по которому Gate её опознаёт). Сам bump не
меняет поведения producer'а кроме строковой метки.

## Ограничения интерпретации Track A / Track B

Gate фиксирует только то, что **Track A и Track B действительно
измерили**. Что они **не** измеряли — тоже явно зафиксировано, чтобы
не делать заявлений за пределами benchmark'а:

- **Качество перевода.** Track A измерял FP-candidate rate поверх
  57 accepted PID: 9/57 = 15.79% во всех четырёх ячейках, 0 gaps.
  Track A **не** измерял discourse-когерентность стыков,
  reference-free semantic quality, anaphora resolution и т. п.
  Semantic recall — `not_measurable` в этом раунде
  (`known_violations` пустые во всех 100 golden records). Суррогат не
  подставляется.
- **Right context: «не измерен как лучше» ≠ «доказанно хуже».** Track A
  не выявил измеримого преимущества `rc_on` (following_blocks=2) над
  `rc_off` (following_blocks=0) по FP-candidate rate. Это значит, что
  Track A **не даёт оснований требовать** right context как
  обязательный механизм V4. Это **не** означает, что right context
  ухудшает качество, увеличивает стоимость или бесполезен — эти
  свойства данным benchmark'ом **не измерялись**. Right context
  остаётся допустимой будущей опцией; решение о его включении/исключении
  пересматривается отдельным benchmark'ом на discourse-когерентность
  стыков, latency и token usage.
- **Performance (time / tokens / reloads).** Track A **не** измерял
  cost, latency и reloads. Любые заявления о том, что small profile
  быстрее или дешевле большого, **не** подтверждены этим benchmark'ом
  и должны опираться на отдельный performance gate.
- **LTCR.** `pending_definition` (V4_MVP_SPEC_RU.md lists LTCR but
  defines no numeric formula). Не подменять другим числом.
- **Track B как доказательство quality success.** Промежуточные
  численные метрики Track B для главы 100
  (bad-repair 2.72%, post-gate 28/256, PID coverage 334/334) — это
  нижняя граница наблюдаемого, а не доказательство качества
  перевода. Track B содержит неразрешённую несогласованность
  `state.json=complete` vs `monitor_state.v31.json status=FAILED`,
  которая отдельно зафиксирована в result record через
  `track_b.terminal_discrepancy` и `track_b.notes` (см. §4).

## Зафиксированные policy-решения

### 1. Chunking — Phase 1 (Phase 1C структура-aware chunk planner)

**Initial/default profile: small chunk profile.** Параметры взяты
из baseline-малой ячейки (`8_12__rc_off`):

```text
target_words = 450
min_words    = 280
max_words    = 640
following_blocks = 0   # initial default: no right context
```

`small` — это **policy-имя** initial/default profile, а не измеренный
диапазон PID. Baseline-сетка `8_12`/`12_20` — это labels baseline'а,
а не фактические диапазоны: малый профиль фактически дал
16–32 PID/чанк (mean 25.21), большой — 39–65 PID/чанк (mean 50.43).
Использовать `8_12` как имя параметра chunk size в V4 — то же, что
называть `target_words=450` через «8–12»; вводит в заблуждение.
В runtime коде Phase 1C и в V4 конфигах параметр chunk size следует
называть `small` / `large` (или по явным числовым параметрам), а не
`8_12` / `12_20`.

**Right context остаётся допустимой будущей опцией.** В этом PR
Gate фиксирует `following_blocks=0` как **initial default** для
дальнейшей V4 разработки — потому что Track A не выявил
FP-candidate-rate преимущества `rc_on` (см. §"Ограничения"). Это
**не** запрет на right context; решение о включении rc_on
принимается отдельным benchmark'ом и отдельным PR.

### 2. Risk / gate categories — Phase 2 (Phase 2A/2B/2C)

`number_word` и `tone_profanity` остаются **обязательными** risk/gate
категориями для V4. В Track B они входят в
`track_b.metrics.deterministic_integrity.remaining_required_categories`
для главы 100, и Phase 2A/2C должен включать их в risk-признаки
и deterministic consistency gate по умолчанию.

### 3. Что baseline **не** зафиксировал

- **Temperature, seed, model runtime settings** — Phase 0C их не
  измерял. Не менять v3 / V3-1 runtime на основании Phase 0C.
  Это отдельный benchmark gate, см.
  `V4_FINAL_REVIEW_AND_IMPLEMENTATION_PLAN_RU_v2.md` §0C.
- **Семантический recall** — `not_measurable` в этом раунде
  (`known_violations` пустые во всех 100 golden records).
  Суррогат не подставляется.
- **LTCR** — `pending_definition` (V4_MVP_SPEC_RU.md lists LTCR but
  defines no numeric formula). Не подменять другим числом.
- **Performance (time / tokens / reloads)** — не измерялся.
  Никаких performance-заявлений small profile не делается.
- **Discourse-когерентность стыков и right context** — не измерялась.
  Right context остаётся допустимой опцией; решение пересматривается
  отдельным benchmark'ом.

### 4. Track B terminal / monitor inconsistency — visible, not masked

Track B для главы 100 содержит два противоречивых сигнала:

- `state.json` и итоговый HTML помечены как `complete`
  (primary pass adjudicated, primary artifacts present).
- `monitor_state.v31.json` содержит исторический `status=FAILED`
  (стадия `11/11 Restore formatting and finalize HTML`).

Gate требует, чтобы эта несогласованность была **явно зафиксирована**
в result record как `track_b.terminal_discrepancy` (с полями
`detected: true`, `monitor_status`, `artifacts_say`, `reason`) и
сопровождалась записью в `track_b.notes[]`. Без этого Track B
нельзя интерпретировать как `complete` terminal state.

Schema (`pact-v4-phase0c-result-record/v1`) делает оба поля
`track_b.notes` и `track_b.terminal_discrepancy` **required**
(`terminal_discrepancy` — nullable). Producer
`v4_phase0c_baseline.py:import_track_b` заполняет их автоматически,
когда `monitor_status=FAILED` + `primary_complete`.

### 5. `final_residual_total` — typed form обязателен

В исходном `phase0c_result.json` (live запись до этого PR) поле
`track_b.metrics.residual_errors.final_residual_total` несло bare
string `measured` / `pending_run_completion` в value-слоте. Это
смешивало статус и численное значение и маскировало факт отсутствия
residual pass lifecycle.

Gate требует typed форму:

```text
final_residual_total: {
  "status": "pending_run_completion" | "measured",
  "value_numeric": <int> | null,
  "reason": "<why value is null, or empty>"
}
```

Schema вводит `typed_residual_total` $ref, `additionalProperties: false`.
Producer `v4_phase0c_baseline.py` теперь:

- при `residual_complete=True` — `value_numeric` =
  `len(residual_lifecycle resolved_retry_exhausted)`;
- при primary complete, residual pending — `value_numeric=null`,
  `reason="primary pass adjudicated; residual pass lifecycle.json
  absent, final residual count not yet measurable"`;
- при primary pending — `value_numeric=null`,
  `reason="primary pass not adjudicated yet"`.

Существующий live record с bare-string `final_residual_total`
**не** совместим с новой schema. **Переиздание live record'а —
отдельный approved шаг**, не часть этого PR. Старый артефакт
остаётся неизменным; новый versioned record пишется рядом.

## §5. Что НЕ делает этот Gate

- Не запускает модели, не запускает production pipeline, не делает
  cache recomputation.
- Не правит v3 production code, run artifacts, translated chapters,
  cache.
- Не вводит новых V4 runtime-файлов и **не** правит существующие
  Phase 1C/2A/2B/2C consumers (`pact_v4/phase1/chunker.py`,
  `pact_v4/phase2/risk.py`, `pact_v4/phase2/cascade.py` и т. п.).
  Интеграция policy в эти consumers — отдельные тематические PR
  (Phase 1C PR, Phase 2 PR), как требует «один этап — один PR».
- Не переиздаёт live baseline record в
  `D:\pact\gate_bench_runs\phase0c_track_a_001\phase0c_result.json`.
  Переиздание — отдельный approved шаг, требующий явного «approved»
  на запись в persistent memory Phase 0C.
- Не меняет Track A / Track B source данные, golden set или
  v31 run artifacts.
- Не делает заявлений о качестве перевода, performance
  (time/tokens/reloads) и discourse-когерентности стыков:
  эти свойства данным benchmark'ом не измерялись.

## §6. Acceptance criteria (Gate-only)

- Phase 1/2 policy ссылается на versioned Phase 0C result record
  (`pact-v4-phase0c-result-record/v1`, `tool_version pact-0c/0.2`,
  `generated_at 2026-07-30T18:06:57+00:00`).
- Small profile параметризован явно (`target/min/max/following_blocks`),
  назван «small chunk profile», не «8–12 PID».
- Right context зафиксирован как `following_blocks=0` initial default;
  это не запрет на right context как будущую опцию.
- `number_word` и `tone_profanity` остаются required risk/gate
  categories.
- Track B `monitor_status=FAILED` vs `state.json=complete` отражена
  в result record через `track_b.notes` и `track_b.terminal_discrepancy`
  автоматически, не скрыта.
- `final_residual_total` имеет typed форму
  `{status, value_numeric: int|null, reason}`; bare-string форма
  schema-rejected.
- Producer, schema и regression tests зелёные на synthetic fixtures.
- Live baseline record в `D:\pact\gate_bench_runs\...` остаётся
  неизменным до отдельного approved re-issue.
- Нет изменений V3 production code, run artifacts, translated
  chapters или cache.
- Phase 1C/2 consumers не правятся этим PR.
