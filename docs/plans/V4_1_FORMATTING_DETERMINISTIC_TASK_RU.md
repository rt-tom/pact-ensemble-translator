# Карточка C — детерминированный formatting: аудит + спека исполнителя (2026-08-11)

Источники: `docs/plans/V4_1_AUDIT_B1_RU.md` §11; `docs/plans/V4_1_WHOLE_CHAPTER_ARCHITECTURE_PLAN_RU.md` §C
(стр. 538–539), табл. 1.1 (стр. 38), стр. 213; accepted plan `docs/architecture/PACT_RESPONSE_TO_CLAUDE_REVISED_ACCEPTED_PLAN_RU.md` §8.14.

Статус модуля: `pact_v4/phase5/formatting.py` (929 строк) — **IDENTICAL** в worktree `t_1bb733a2` и dev-клоне
`D:\pact\pact_translator_v4_1` (ветка `fix/b1-2-extractor-max-tokens-20000`, база — `dev/v4.1-reasoning-transport`).

## 1. Model-call sites (нарушение «formatting = 0 model calls») — удалить

| # | Файл | Функция/элемент | Строки |
|---|------|-----------------|--------|
| 1 | `pact_v4/phase5/formatting.py` | `TIER_MODEL = "model_fallback"` | 98 |
| 2 | `pact_v4/phase5/formatting.py` | `FormattingCaller` Protocol (+`batch`) | 254–285 |
| 3 | `pact_v4/phase5/formatting.py` | `_parse_format_mappings` | 490–528 |
| 4 | `pact_v4/phase5/formatting.py` | `_apply_model_mappings` | 531–595 |
| 5 | `pact_v4/phase5/formatting.py` | `FormattingOutcome.model_fallback_count` / `model_call_count` | 214–215, 242–243 |
| 6 | `pact_v4/phase5/formatting.py` | `run_formatting_align`: параметр `formatting_caller` | 661 |
| 7 | `pact_v4/phase5/formatting.py` | `run_formatting_align`: `batch_fn`/`use_batch`/`pending` | 720–731 |
| 8 | `pact_v4/phase5/formatting.py` | `run_formatting_align`: ветки model-fallback (per-PID) | 771–805 |
| 9 | `pact_v4/phase5/formatting.py` | `run_formatting_align`: batched-фаза + singles | 807–905 |
| 10 | `pact_v4/phase5/formatting.py` | `__all__`: `TIER_MODEL`, `FormattingCaller` | 79–80 |
| 11 | `pact_v4/runtime/backend_role_adapters.py` | `BackendFormattingCallerConfig`, `BackendFormattingCaller` (`__call__`+`batch`, реальные `backend.complete()`) | 886–997 |
| 12 | `pact_v4/runtime/prompts_runtime.py` | `FORMAT_SPANS_V1` + `render_formatting_prompt` + `render_formatting_prompt_batch` | 686–…, 895–… |
| 13 | `pact_v4/runtime/runtime_config.py` | `build_formatting_adapters` (+`__all__`) | 912–927, 1095 |
| 14 | `pact_v4/pipeline/v4_phase12_strict_runner.py` | параметр `formatting_adapters` | 2278 |
| 15 | `pact_v4/pipeline/v4_phase12_strict_runner.py` | `_formatting_step` closure: `formatting_caller=…`, `pid_batches=…` | 3081–3098 |

## 2. Deterministic-путь и incident report — сохранить

- Тир-каскад `_resolve_deterministic` (exact → occurrence_aware → fuzzy), `_fuzzy_pattern`,
  `occurrence_ranges`, `find_nonoverlapping_occurrence` — строки 292–482.
- Wrap-only `apply_span_mappings` (B14), guard `_MARKER_RE` — строки 603–639, 912–918.
- `FormattingIncident` (pid, span_id, tier, reason, required=True, detail) — строки 165–192.
- Отчёт: `formatting_report.json` (schema `pact-v4-formatting-report/v1`), блок `formatting` в
  `repair_report.json`; `blocking = incident_count > max_formatting_incidents` (default 0).
  Пишется в strict runner: строки 1639–1660, 2223–2232.
- `max_formatting_incidents` / `formatting_required` / `formatting_policy_version` — policy-поля
  `StrictRunConfig` (строки 268–275) — сохранить как есть.

## 3. Текущая обработка unresolved spans

- Без caller: unresolved → `FormattingIncident` сразу (reason `target_not_found` / `ambiguous_occurrence`,
  tier = последний детерминированный) — строки 753–765.
- С caller: model fallback (per-PID или B12 batch) → `_apply_model_mappings` → остаток → incident
  (`missing_mapping` / `target_not_found` / `transport_error`, tier=model_fallback).
- Любой incident при `max_formatting_incidents=0` → `blocking` → terminal `accepted_degraded`
  (валидный PID-map) или `failed` (нет валидного PID-map). Транспортная ошибка ≠ семантический вердикт.

## 4. Ожидаемое поведение по §11 (после фикса)

- Model-fallback тир удалён **структурно**: из formatting невозможно сделать модельный вызов.
- unresolved → debt (incident + blocking-политика), не тихая потеря.
- «0 model calls» ≠ успех, если глава стала `accepted_degraded` из-за formatting debt.
- Deterministic incident report сохраняется в неизменной схеме.

## 5. Эмпирическая проверка на замороженных артефактах (2026-08-11)

Глава 0001: 78 блоков со spans, **102 spans** (101 `em` + 1 `strong`).

Прогон `run_formatting_align(blocks, translation, formatting_caller=None, …)` по замёрзшим артефактам:

| Артефакт | resolved | incidents | blocking | Примечание |
|----------|----------|-----------|----------|------------|
| run_006_local_gemma / translations.json | **0** | **102** | True | все `target_not_found` |
| run_007_remote_deepseek / translations.json | **0** | **102** | True | все `target_not_found` |
| run_008_remote_deepseek_affix / translations.json | **0** | **102** | True | все `target_not_found` |
| run_009_smoke_full_pipeline / translations.json | **0** | **102** | True | все `target_not_found` |
| run_005_remote (chunked, formatting_report.json) | 84 | 18 | True | **все 84 resolved — через model_fallback** |
| eff-a1a2 chapter_0001 (formatting_report.json) | 97 | 5 | True | **все 97 resolved — через model_fallback** |

Ключевые факты:

1. Во всех whole-chapter переводах (run_006–009) **0 инлайн-тегов** — генерационный промпт
   `BALANCED_LITERARY_V3` (pact_v4/phase2/prompts.py:101–102) явно запрещает markup
   («Do not output any HTML or markup — plain Russian text only»).
2. Детерминированный матчер ищет **исходный английский текст в русском переводе**
   (`needle = group[0].text` — source span; haystack — translation). Реальный литературный перевод
   не сохраняет исходные фрагменты (кроме имён/чисел/заимствований): p00002 `em01`
   `"Damn me, damn them, damn it all."` → «Будь я проклят, будь они прокляты, будь всё проклято.»
   → deterministic-тиры физически не могут найти фрагмент.
3. Ожидание §11 «~0 unresolved (whole-chapter перевод держит `<em>` 101/101)» взято из **independent
   control** (`docs/audits/pact_ch1_independent_vs_pipeline_report_v2.md`, стр. 62 whole-chapter плана) —
   там модель сама ставила `<em>` (101/101). В v4.1-пайплайне промпт это запрещает → переводы теги не несут.

**Вывод: acceptance §11 «formatting на 0001 → 0 unresolved» на текущих артефактах не достигается.**
Причина — не удаление model-fallback, а генерационный промпт, запрещающий инлайн-разметку.

## 6. Развилка для владельца (нужно решение перед I-карточкой)

- **Путь A (минимальный, в scope карточки, NON-GOALS соблюдены):** удалить model-fallback тир;
  deterministic-тиры остаются; unresolved → debt. Acceptance: «0 model calls + детерминированный
  incident report + полный suite». Глава 0001 на текущих артефактах станет `accepted_degraded`
  (102 formatting-debt) — по §11 это честный, но НЕ «успех». Промпт генерации не трогаем.
- **Путь B (чтобы достичь «0 unresolved», расширенный scope):** разрешить whole-chapter генерации
  выводить инлайн `<em>` (изменение `BALANCED_LITERARY_V3`, архитектурное решение — сейчас стр. 213
  whole-chapter плана это запрещает), formatting переписать в verify+B14-normalize уже-присутствующих
  тегов (`normalize_inline_markup`), deterministic-тиры — вторичны (только для пропущенных).
  Это трогает генерационный промпт (NON-GOAL карточки) и требует отдельного решения/карточки.

**Рекомендация аудита:** для карточки C — Путь A (удаление model-fallback структурно, 0 model calls,
debt вместо тихой потери); вопрос «0 unresolved» вынести отдельной карточкой на генерационный промпт
(Path B), т.к. в текущей архитектуре «модель не расставляет HTML» (стр. 213) и «0 unresolved» одновременно
невыполнимы.

## 7. Спека исполнителя (I-карточка)

### Удалить (см. таблицу §1)

- `formatting.py`: тир model_fallback целиком (элементы 1–10), включая Protocol, парсеры, поля
  `model_fallback_count`/`model_call_count` и их чтение в `to_payload`.
- `backend_role_adapters.py`: `BackendFormattingCaller` + config (11).
- `prompts_runtime.py`: `FORMAT_SPANS_V1` + оба рендера (12).
- `runtime_config.py`: `build_formatting_adapters` + экспорт (13).
- strict runner: параметр `formatting_adapters` (14); `_formatting_step` (15) — строить step при
  `cfg.formatting_required` без caller; чтение `model_fallback_count`/`model_call_count` в summary
  (строки 1656–1657, 2228–2229) убрать.

### Сохранить (см. §2)

- Тир-каскад exact → occurrence_aware → fuzzy, `_fuzzy_pattern`, wrap-only `apply_span_mappings`,
  `_MARKER_RE` guard, `FormattingIncident`, `FormattingOutcome` (без model-полей),
  schema/формат `formatting_report.json`, policy `max_formatting_incidents`/`formatting_required`.
- Поведение: `formatting_caller` больше не параметр; unresolved → incident сразу (как ветка
  «no caller», строки 753–765 — единственный путь).

### Тесты

- `tests/pact_v4/phase5/test_formatting.py`: удалить model-fallback-тесты
  (`test_model_fallback_*`, `test_batch_call_*`, `test_pid_outside_batches_*`,
  `test_model_call_count_in_payload`, `test_ambiguous_occurrence_falls_through_to_model`,
  `test_conflicting_spans_fall_through_to_next_tier` — переписать: overlapping span → incident,
  не model). `test_ambiguous_occurrence_without_caller_is_blocking_incident` — остаётся.
- `tests/pact_v4/phase4/test_b12_call_optimization_validation.py`: formatting-batch часть удалить
  (repair re-gate batching остаётся); `_EmptyBatchCaller`/formatting-импорты — убрать.
- `tests/pact_v4/pipeline/test_v4_phase12_strict_runner_formatting.py`: инъекцию caller убрать,
  `model_fallback_count > 0` → отсутствие поля/0; тест «without formatting adapters the step is
  skipped» переписать (step теперь строится по `formatting_required`).
- `tests/pact_v4/runtime/test_runtime_role_backend.py:46`: убрать импорт `build_formatting_adapters`.
- Проверить ссылки на `BackendFormattingCaller`/`build_formatting_adapters`/`FORMAT_SPANS_V1` по всему
  `tests/` и `pact_v4/` (grep), включая `pact_v4/phase4/repair.py` docstring-упоминания (240–253).

### Acceptance (Путь A)

1. `grep -rn "formatting_caller\|BackendFormattingCaller\|FORMAT_SPANS_V1\|build_formatting_adapters\|model_fallback\|model_call_count" pact_v4/ tests/` → 0 совпадений в коде (кроме исторических docstring-ссылок, если оставлены осознанно).
2. Полный suite: `C:\Python314\python.exe -m pytest tests --ignore=deployment_backups -q` — зелёный.
3. Unit: unresolved span без caller → blocking incident; «0 model calls» структурно (нет кода вызова).
4. `formatting_report.json` schema и блок `formatting` в `repair_report.json` — без model-полей, формат прежний.

### NON-GOALS

Не менять HTML-рендер, не тюнить промпт (включая `BALANCED_LITERARY_V3`), не трогать remote-путь,
не запускать pipeline, не менять `normalize_inline_markup`/`strip_inline_markup`.
