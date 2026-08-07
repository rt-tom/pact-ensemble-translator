# V4 Efficiency A — детерминированная оптимизация вызовов/токенов (план, ред. 2)

Дата: 2026-08-06 (ред. 1 — 2026-08-06, переработан по решениям владельца)
Статус: approved rev.2, target `main`
Основание: `docs/architecture/V5_UNIVERSAL_LITERARY_TRANSLATOR_ARCHITECTURE_RU.md` §8.1 + анализ `run_005_remote` (404 вызова / 452k input tokens, `DECISIONS.md` 2026-08-06) + разбор архитектором (A1–A2, A–D) + решения владельца
Владелец: RT. Исполнение: архитектор → карточки `docs/plans/V4_EFFICIENCY_A1/A2_TASK_RU.md` → I+RV (протокол «2 карточки», PR после approve) → валидационный прогон (владелец, вне чата)

## 0. Решения владельца (ред. 2, 2026-08-06)

1. **Библия НЕ режется** (ни по главам, ни по ролям): отделить «что нужно главе» эвристикой ненадёжно — Bible budgeter исключён из A1.
2. **A2 — делаем**: один кандидат `balanced_literary` по умолчанию (lazy `fidelity_first` только при fail); второй кандидат по умолчанию не генерируется («гигантское количество токенов в обе стороны»).
3. Из исходного A1 исключены (разбор архитектора): **audit skip** (Step 6 ловит ты/вы/referents, которые deterministic не видит), **bundle dedup по `bundle_hash`** (идентичность включает chunk_id/контекст — совпадений между чанками нет), **blanket Qwen re-gate bypass** (repair правит срез текста — соседние PID могут пострадать).
4. Предложения A–D: A (compact-форматы) и B (adaptive max_output_tokens) — в **A1b** после замера; C (repair через качество входа) — в **A1b**; D (content-addressable cache) — **отклонён** (resume-кэш уже покрывает cross-run reuse; хеш без chunk_id небезопасен).

## 1. Цель и рамки

Срезать вызовы/токены v4 детерминированно, без потери качества. Базовый ориентир — не старый план (250–270), а **~365 вызовов (−10%), input −10–13% + кэш-выигрыш** (без bible-реза/audit-skip). Все фильтры — скрипт-проверяемые. `locked`-constraints никогда не режутся.

### Что НЕ входит (guardrails)
- `reasoning > 0` — остаётся 0
- топологии chapter_context/whole_chapter — v5, не здесь
- изменение `RiskPolicy` thresholds/weights
- потеря `locked` constraints (`narrator_gender`, `glossary_conflict`, `number_word`/`tone_profanity`) — fail-closed

## 2. Карточка A1 — безопасный транспорт (3 подпункта)

### A1.1 Glossary budgeter
- `pact_v4/pipeline/_shared_runner_helpers.py: _glossary_entries` → per-chunk фильтр: пара проходит, если source-термин присутствует в `owned_source + left + right` (`_term_present` из `risk.py`, IGNORECASE, мультисловные, `(?<!\w)...(?!\w)`).
- **`always_include` (fail-closed)**: `narrator_gender`-связанные пары, `glossary_conflict`, `required_risk_feature`-flagged для чанка — никогда не режутся.
- Диагностика: отчёт «отброшено N пар: [термины]» (в `selection_meta.json` или отдельный файл).
- `bundle_hash` пересчитывается от отфильтрованного набора (кэш-инвалидация корректная).
- Ожидание: input −10–15% на генерации.

### A1.2 Prompt prefix ordering → provider cache
- Статичные блоки (template instructions + **полная библия** + style/policy константы) — в **начало** сообщения, до CHUNK_ID/source/glossary/context — в generation, Qwen fidelity, Qwen/Gemma audit промптах.
- Содержимое не меняется — меняется порядок (минимальный семантический риск).
- Цель: общий префикс между чанками → `cached_input_tokens` ↑ (деньги), не вызовы.
- Проверка после прогона по D1: `v4_usage` cached/input разрез.

### A1.3 D1-telemetry отчёт по фазам
- Разрез вызовов/токенов по ролям/фазам из `usage.ndjson`: gen / qwen fidelity / gemma preference / audit / repair / formatting.
- База замера: formatting и repair НЕ трогаем до реальных цифр post-B13/B14.

## 3. Карточка A2 — lazy balanced-only

### 3.1 Логика
```
balanced_literary (1 вызов)
  → Qwen fidelity + deterministic
      passed  → selected (done; Gemma не зовём)
      failed  → lazy: gen fidelity_first (1) + его Qwen (1)
                один passed → selected; оба failed → quarantined
```
- Gemma Russian preference: при одном кандидате не с чем сравнивать → 0–2 вызова вместо 13 (вызывается только при >1 passed, что в lazy-схеме почти не бывает).
- `fidelity_first` — страховка именно там, где нужен (balanced провалился).
- Флаг отката: `efficiency.lazy_balanced=false` → старое 2-кандидатное поведение (identity_hash меняется, resume инвалидируется корректно).

### 3.2 Изменения
- `pact_v4/phase2/generation.py: _roles_for_band` → lazy-режим (default `lazy_balanced=true`, откат флагом)
- `pact_v4/pipeline/v4_phase12_strict_runner.py`: Phase 2 loop — gen balanced → Qwen → (lazy fidelity if needed) → select
- `pact_v4/phase1/models.py` / prompt templates — без изменений (роль/шаблоны те же)

### 3.3 Приёмка
- Unit: low→1 balanced; high+failed→lazy fidelity; high+passed→no lazy; оба failed→quarantined
- Dry-run на run_005: gen 32→~18, Qwen 32→~18, Gemma 13→0–2
- Валидационный прогон (владелец вне чата): 0001 (+046); **контроль 2/14 fidelity-wins (chunk0010/0014)**: не должны деградировать (translations.json diff против run_005)

## 4. Ожидаемая экономия (честная, ред. 2)

| Фаза | run_005 | После A1+A2 |
|---|---:|---:|
| Generation | 32 | ~18 |
| Qwen fidelity | 32 | ~18 |
| Gemma preference | 13 | 0–2 |
| Audit (Step 6) | 32 | 32 (не трогаем) |
| Repair re-gate | 16 | 16 (A1b после замера) |
| Formatting | 15 | 15 (A1b после замера) |
| **Всего** | **404** | **~365 (−10%)** |
| input_tokens | 452k | ~390–400k (−10–13%) + кэш A1.2 |

## 5. Карточка A1b (после первого прогона, отдельно)

- Formatting: реальный `model_call_count` post-B13/B14 → deterministic matcher только по данным
- Repair: качество входа (детерминированная классификация мягких findings, L3-расширение) — меньше слабых repair-планов → меньше вызовов редактора/re-gate; формально безопасный fast-path только при доказуемо точечной замене с проверкой diff
- Compact-форматы промптов (A) и adaptive max_output_tokens (B) — как измеримые эксперименты, с регрессионной проверкой
- **Исследование финального перевода перед решениями о типографике (владелец, 2026-08-07):** перед тем как решать, что и как править (ASCII-кавычки, хвостовые `\n`/`\r`, смешанная типографика „…", em-разметка), провести исследование финального `translations.json` обоих глав валидационного прогона: полная инвентаризация артефактов по категориям (ASCII-кавычки по паттернам/парности, control-символы, двойные пробелы/тире, разметка), с привязкой к chunk/кандидату/фазе происхождения (generation/repair/formatting) и примерами. Только по итогам этого исследования принимается решение, что конкретно нормализовать и каким детерминированным правилом (с тестами на найденные паттерны). Предварительные находки прогона eff-a1a2 (0001): p00157 с хвостовым `\n` из кандидата fidelity_first; chunk0012 — 21 PID с ASCII-`"` вместо верхней лапки из balanced_literary (0002 — чисто).

## 6. Порядок и зависимости

1. Этот план (docs) → main
2. Карточка A1 → I+RV → merge
3. Карточка A2 → I+RV → merge
4. Валидационный прогон 0001 (+046) — только владелец вне чата
5. A1b — отдельная карточка после данных

## 7. Риски и откат

- A2: теоретическая потеря литературности в 2/16 (fidelity был бы лучше, но balanced прошёл Qwen). Митигация: lazy при fail, валидация chunk0010/0014, флаг отката.
- A1.2 (порядок промптов): минимальный семантический риск — проверяется dry-run и регрессией тестов.
- A1.1: риск — пара реально нужна, но термин в иной форме (морфология). Митигация: `always_include` + диагностика отброшенных + IGNORECASE/мультисловные.

## 8. Ссылки

- `docs/architecture/V5_UNIVERSAL_LITERARY_TRANSLATOR_ARCHITECTURE_RU.md` §8.1
- `D:\pact\gate_bench_runs\v4_phase12_strict_0001\run_005_remote` (usage.ndjson, selection_results.json, generation_outcomes.json)
- `pact_v4/phase2/risk.py`, `pact_v4/phase2/generation.py`, `pact_v4/pipeline/_shared_runner_helpers.py`, `pact_v4/runtime/prompts_runtime.py`, `pact_v4/runtime/backend_role_adapters.py`
