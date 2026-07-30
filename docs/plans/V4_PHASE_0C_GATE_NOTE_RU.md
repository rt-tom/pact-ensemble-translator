# V4 Phase 0C — Gate (фиксация policy для Phase 1 и Phase 2)

Дата: 2026-07-30. Статус: baseline измерен, Gate фиксирует policy. Только
changelog, без нового runtime (Phase 1C chunk planner и Phase 2 risk/gate
ещё не реализованы — есть только Phase 1A data contracts в
`pact_v4/phase1/models.py`).

## Опорные записи

- Baseline: `D:\pact\gate_bench_runs\phase0c_track_a_001\phase0c_result.json`
  (`pact-v4-phase0c-result-record/v1`, `tool_version pact-0c/0.2`,
  `generated_at 2026-07-30T18:06:57+00:00`).
- Track A: chapter 046 / Phase 0B golden set, 57 accepted PID, 43 needs_review
  (исключены из численных метрик), 0 rejected, 0 gaps во всех ячейках.
- Track B: chapter 100 v3.1 production run
  (`D:\pact\pact_translator_v3_v31_production\pipeline_runs\chapter_100_to_100_v31`,
  run_identity `91e2d8ab...d1dbb52e09`).

## Зафиксированные policy-решения

### 1. Chunking — Phase 1 (Phase 1C структура-aware chunk planner)

Использовать **малый chunking-профиль** с параметрами baseline-малой ячейки
(`8_12__rc_off`):

```text
target_words = 450
min_words    = 280
max_words    = 640
following_blocks = 0   # no right-context (rc_off)
```

**Называть его «small chunk profile».** Названия `8_12` / `12_20` — это
labels baseline-сетки, а не фактические диапазоны PID: малый профиль
фактически дал 16–32 PID/чанк (mean 25.21), большой — 39–65 PID/чанк
(mean 50.43). Использовать `8_12` как имя параметра chunk size в V4 — то же,
что называть `target_words=450` через «8–12»; это вводит в заблуждение.

**Right context не включать как обязательный механизм.** Baseline не
показал, что `following_blocks=2` даёт выигрыш по FP-candidate rate поверх
57 accepted PID: все четыре ячейки равны (9/57 = 15.79%). Без отдельного
benchmark, измеряющего discourse-когерентность на стыках, добавление
right-context подаётся как дизайнерское предпочтение, а не как
data-driven решение. Не делаем.

### 2. Risk / gate categories — Phase 2 (Phase 2A/2B/2C)

`number_word` и `tone_profanity` остаются **обязательными** risk/gate
категориями. В Track B они входят в
`track_b.metrics.deterministic_integrity.remaining_required_categories` для
главы 100, и Phase 2A/2C должен включать их в risk-признаки и
deterministic consistency gate по умолчанию.

### 3. Что baseline **не** зафиксировал

- **Temperature, seed, model runtime settings** — Phase 0C их не измерял.
  Не менять v3 / V3-1 runtime на основании Phase 0C. Это отдельный
  benchmark gate, см. `V4_FINAL_REVIEW_AND_IMPLEMENTATION_PLAN_RU_v2.md` §0C.
- **Семантический recall** — `not_measurable` в этом раунде
  (`known_violations` пустые во всех 100 golden records). Суррогат не
  подставляется. Помечено в
  `track_a.source.semantic_recall.status` и в
  `fp_candidate_metric_definition` baseline record.
- **LTCR** — `pending_definition` (V4_MVP_SPEC_RU.md lists LTCR but defines
  no numeric formula). Не подменять другим числом.

### 4. Track B terminal / monitor inconsistency — видимая, не скрытая

Track B для главы 100 содержит два противоречивых сигнала:

- `state.json` и итоговый HTML помечены как `complete`.
- `monitor_state.v31.json` содержит исторический `status=FAILED`
  (стадия `11/11 Restore formatting and finalize HTML`).

Gate требует, чтобы эта несогласованность была **явно зафиксирована** в
result record, а не замаскирована `track_b.completion.status = "measured"`.
Schema (`pact-v4-phase0c-result-record/v1`) расширен опциональными полями
`track_b.notes` (string[]) и `track_b.terminal_discrepancy` (объект с
полями `detected`, `monitor_status`, `artifacts_say`, `reason`).

Следствие: Track B **не является** доказательством полного quality success
до разъяснения этой несогласованности. Промежуточные численные метрики
Track B (bad-repair 2.72%, post-gate 28/256, PID coverage 334/334) — это
нижняя граница наблюдаемого, не доказательство качества перевода.

### 5. `final_residual_total` — typed value обязателен

В исходном `phase0c_result.json` поле
`track_b.metrics.residual_errors.final_residual_total` несёт bare string
`"measured"` (или `"pending_run_completion"`). Это смешивает статус и
численное значение и маскирует факт отсутствия residual pass lifecycle
(на момент импорта residual ACTIVE). Gate требует typed форму:

```text
final_residual_total: {
  "status": "pending_run_completion" | "measured",
  "value_numeric": <int> | null,
  "reason": "<why value is null>"
}
```

Schema расширена опциональным `metric_status.value_numeric: number | null`.
Существующий record с bare-string `final_residual_total` остаётся
синтаксически валидным, но Gate помечает его как «needs re-issue with
typed `final_residual_total`» — без пересборки record численный финал
residual pass нельзя интерпретировать как success.

## Что НЕ делает этот Gate

- Не запускает модели, не запускает production pipeline, не делает
  cache recomputation.
- Не правит v3 production code, run artifacts, translated chapters, cache.
- Не вводит новых V4 runtime-файлов; Phase 1C и Phase 2A/2B/2C пока
  не реализованы, поэтому policy фиксируется как changelog.
- Не меняет Track A / Track B source данные, golden set или
  v31 run artifacts.

## Acceptance

- Phase 1/2 policy ссылается на versioned Phase 0C result record
  (`pact-v4-phase0c-result-record/v1`, `tool_version pact-0c/0.2`,
  `generated_at 2026-07-30T18:06:57+00:00`).
- Small profile параметризован явно (`target/min/max/following_blocks`),
  назван «small chunk profile», не «8–12 PID».
- Right context не включается как обязательный механизм.
- `number_word` и `tone_profanity` остаются required risk/gate categories.
- Track B `monitor_status=FAILED` vs `state.json=complete` отражена в
  result record через `notes` / `terminal_discrepancy`, не скрыта.
- `final_residual_total` либо numeric, либо explicit pending/not_measurable
  статус с reason (см. regression tests).
- Нет изменений V3 production code, run artifacts, translated chapters
  или cache.
