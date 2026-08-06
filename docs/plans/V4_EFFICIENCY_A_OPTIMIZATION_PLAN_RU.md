# V4 Efficiency A — детерминированная оптимизация вызовов/токенов (план)

Дата: 2026-08-06
Статус: approved plan, target `main`, docs-only (план, не реализация)
Основание: `docs/architecture/V5_UNIVERSAL_LITERARY_TRANSLATOR_ARCHITECTURE_RU.md` §8.1 уточнение + `docs/architecture/V4_LITERATURE_REVIEW_AND_RECOMMENDATION_RU.md` + анализ `run_005_remote` (глава 0001, 404 вызова, `DECISIONS.md` 2026-08-06)
Владелец: RT. Исполнение: архитектор → карточки `docs/plans/V4_Bx_*` → `vk/*` + draft PR + REVIEW REQUIRED

## 1. Цель и рамки

Срезать вызовы/токены v4 максимально детерминированно, без потери качества по литуровню (§5.2 risk-gated compute, §5.5 overthinking, §3.3 MBR). Все фильтры — скрипт-проверяемые, без новой модели/режима. Любая потеря `locked`-constraint = блокер.

База: `run_005_remote` гл.0001 `16 чанков / 400 pids / 404 вызова / $1.51` (строгий single-resident, `balanced 12 vs fidelity 2` из 14 selected, `2 quarantined`, `risk=HIGH 16/16`, `disagreement=false 16/16`, `candidates_passed=2 на 13/16` — оба прошли Qwen, решал Gemma).

План = 2 карточки:
- **Card A1** — prompt budgeter + audit/repair/formatting skip (детерминированные, 0 семантики)
- **Card A2** — lazy balanced-only генерация (`single balanced by default`)

Обе карточки — один поток, `A1` до `A2` (A2 опирается на бюджетчик для токен-подсчёта). Масштаб: только `pact_v4/pipeline`, `pact_v4/runtime/bible_renderer`, `pact_v4/phase2/*` + тесты/скрипты валидации, без `v4_book_run`/`glossary` схемы.

### Что НЕ входит (guardrails)
- `reasoning > 0` (литобзор: long CoT вредит low-risk) — остаётся `0`, отдельной карточкой позже
- `chapter_context_chunk_output / whole_chapter` топологии — `v5 Phase 10/11`, `v4 strict` остаётся оракулом
- Изменение `RiskPolicy` thresholds/weights — калибруется в A2 детерминированно, но без включения reasoning
- Потеря `locked` constraints (`narrator_gender`, `glossary_conflict`, `number_word`/`tone_profanity` flagged) — fail-closed, никогда не режем

## 2. Уточнение v5 §8.1 (уже внесено)

`docs/architecture/V5_UNIVERSAL_LITERARY_TRANSLATOR_ARCHITECTURE_RU.md:8.1` дополнен блоком `characters/aliases/pronouns — deterministic-first`: `character entities / aliases / pronouns` и `recurring terminology` извлекаются без модели (`frequency ≥2 глав + regex + consensus alignment PID→PID + co-occurrence guard` как `B9`: `proper_name ≥2 с одним target, term ≥2 глав и ≥3 вхождений, consensus 0.8`), LLM только для `ambiguous clusters` (`the fat man = ghoul`, `Mags ≠ Maggie`). Итог `Phase 6`: `deterministic map (0) + 1–2 LLM map (conflict resolver) + 1 reduce → frozen BookResearchSnapshot` — `~3` LLM-вызова на книгу вместо `~10`.

## 3. Card A1 — prompt budgeter + deterministic skip (док-ва: скрипты, не модель)

### 3.1 Glossary budgeter
- Где: `pact_v4/pipeline/_shared_runner_helpers.py: _glossary_entries` → `_glossary_entries_for_chunk(chunk, source_map, memory)` + `assess_source_risk` уже использует `_term_present`
- Правило: `source_term` в `glossary.json` проходит в `PromptBundle.glossary` только если `re.search(rf"(?<!\w){re.escape(term)}(?!\w)", owned_source+left+right, IGNORECASE)` (та же `_term_present` из `risk.py`). Остальные — cut, но `bundle_hash` пересчитывается, кэш инвалидируется корректно.
- Тест: `render` на артефактах `run_005` — вход: `source_map + chunk_plan`, ожид: `|glossary| до/после`, `input_tokens` из `usage.ndjson` срезается `~200-400 tok/чанк`, locked не режется.

### 3.2 Bible budgeter
- Где: `pact_v4/runtime/bible_renderer.py: render_bible_section(book_memory)` → `render_bible_section_for_chunk(book_memory, chunk_text)` + сохранение `render_bible_section` как алиас
- Правило: `characters/facts/address_register` фильтруются по `name.lower() in chunk_text.lower()` (chunk_text = `owned_source` + `left_ru` + `right_en` конкат) + кросс-чанк алиас через тот же капитализированный частотник как `B9` (regex `[A-Z][a-z]+`, `proper_name` guard: отбрасывать если `target` == `established` значение другого ключа). **Locked всегда остаётся**: `narrator_gender` (`extract_narrator_gender`), записи где `glossary_conflict` или `required_risk_feature` flagged для чанка.
- Cap `_MAX_CHARACTERS=20/_MAX_FACTS=30` остаётся, но после фильтра, не до.
- Тест: `test_bible_budgeter_unit` — фикстура `book_memory.json` (63 chars/28 facts) + синтетический chunk_text, ожид: `filtered ⊆ original`, locked сохранён, детерминированность, `(showing first N of M)` не ломается.

### 3.3 Audit skip (Step 6)
- Где: `pact_v4/pipeline/v4_phase12_strict_runner.py: Step 6` (audit loop `for detector in (qwen,gemma): for chunk`)
- Правило: `audit` звать только если `chunk: risk=high` ИЛИ `status=quarantined/needs_synthesis` ИЛИ есть детерминированный `integrity` fail (`mixed_script`/`number_word`/`formatting incident`) в соседних чанках. На `low/medium` без детерминированных находок — детерминированного `integrity` достаточно (литобзор: одного full audit достаточно при targeted convergence).
- Gate: на `run_005` (все high) — вызовов `32→32` не меняется; на `046` / синтетике с `low` — `32→~12-16`, `audit_findings.region_count` на low-чанках `0` и не пропускает `locked`.
- Скрипт: `scripts/audit_skip_dry_run.py` на `run_005` артефактах, отчёт `proposed skipped chunks`.

### 3.4 Repair re-gate skip
- Где: `pact_v4/phase4/repair.py` (`region_fidelity_gate` с `batch` из `B12`) + `pact_v4/pipeline/v4_phase12_strict_runner.py: Step 7`
- Правило: `B12` уже сбатчил `95→16`. Дополнительно: детерминированный fast-path — если правка затрагивает только `number_word`/`mixed_script`/`gender` (regex-таблицы из `_integrity_checks.py`), проверка `re` без Qwen. Остальные — батч Qwen как есть. Плюс уже есть `convergence` re-audit только `changed+neighbours`.
- Gate: `16→~6-8` на главу, `debt_trace` не растёт на детерминированных фиксах, `qwen_smoke` не деградирует.

### 3.5 Formatting fallback skip
- Где: `pact_v4/pipeline/v4_phase12_strict_runner.py` Step 8 formatting + `B14` em-нормализация
- Правило: `occurrence-aware` + `conservative fuzzy` детерминированно закрывают `~80%` как сейчас; `model fallback` батчем по чанку (B12 `model_call_count 15`) — только для оставшихся `~20%` где детерминированный `incident_count` >0. `B14` уже снял `66→0` ложных `em`. Ожид: `model_call_count 15→~3-5` на главу, `formatting_report.incident_count` не растёт.
- Скрипт: dry-run на `run_005/formatting_report.json`.

### 3.6 Suspense coalescing (bundle dedup до сети)
- Где: `pact_v4/phase2/generation.py: PromptBundle.bundle_hash` + `GenerationCache`
- Правило: до `BackendModelCaller.call` проверить `bundle_hash` dedup (если `owned_source` текст повторяется чанк-в-чанк — `POV/штампы`). Хиты — из `GenerationCache` без сети. Не путать с provider prefix cache.
- Gate: хитрейт на `run_005` `~5-10%`, `0` семантического эффекта, `generation_outcomes.json` помечает `cache_hit`.

### 3.7 Приёмка A1
- `C:\Python314\python.exe -m pytest tests --ignore=deployment_backups -q --basetemp=D:/pact/_pytest_tmp_check` — 823+ новые тесты зелёные
- Dry-run скрипты на `run_005_remote` артефактах: `input_tokens` `~452k→~300-340k` (`-15-25%`), `model_call_count` `32/16/15 → ~12/8/4` (audit/re-gate/formatting)
- `bundle_hash` меняется только на отфильтрованных чанках, resume не инвалидируется лишне
- `DECISIONS.md` entry + PR `vk/v4-efficiency-a1-budgeter` → `main` (separate worktree, REVIEW REQUIRED)

## 4. Card A2 — lazy balanced-only генерация (single balanced by default)

### 4.1 Текущее (до)
- `risk low → 1 (fidelity_first)`, `medium/high → 2 (fidelity_first + balanced_literary)` — всегда два вызова (`pact_v4/phase2/generation.py:_roles_for_band`)
- `cascade`: Qwen `faithful` на каждом → deterministic → Gemma preference если оба прошли. На `run_005`: `32 gen + 32 Qwen + ~13 Gemma =77` на Phase 2, `12/14` победил `balanced`.

### 4.2 Новое (lazy)
- **Default**: генерировать только `balanced_literary` (1 вызов/чанк) — `balanced` логичен как default: `run_005 86%` побед `balanced` при обоих прошедших Qwen, решал только русский.
- **Страховка (lazy `fidelity_first`)**: после `Qwen fidelity + deterministic` на `balanced`:
  - если `passed` → `selected`, done, Gemma не зовём (исключение — оба прошли и `disagreement_detected=true` → нужен Gemma, но на `run_005` `disagreement=false 16/16`, т.е. `0`)
  - если `failed` (`faithful=false`/`completeness=false`/`deterministic` fail) → lazy `gen fidelity_first` (1) + его `Qwen` (1) → выбираем прошедшего; если оба failed → `quarantined` как сейчас; Gemma зовём только если оба `passed` и есть disagreement — иначе бесспорный winner
- `fidelity_first` остаётся страховкой для `2/14` кейсов (`chunk0010/0014` в `run_005` где победил fidelity) — они покрываются lazy веткой только если balanced не прошёл; если balanced прошёл, но fidelity был бы лучше по русскому — потеря `quality` возможна, поэтому A2 требует валидации на `046`/`0001` с `Gemma preference` гапом (см. 4.4).
- Совместимость: `expected_roles`/`risk_band` в `generation_outcomes.json`/`selection_meta.json` сохраняются, `bundle_hash`/`PromptBundle` без изменений, `resume` по `bundle_hash` + `selected_role`.

### 4.3 Изменения
- `pact_v4/phase2/generation.py: _roles_for_band` → `_roles_for_band_lazy(band, mode="balanced_only")` + флаг `V4_LAZY_BALANCED=1` (env/ConfigArtifact `efficiency.lazy_balanced: bool`, default `true` на `A2`, `false` — легаси 2-кандидат для отката)
- `pact_v4/pipeline/v4_phase12_strict_runner.py`: Phase 2 loop переписан на `gen balanced → Qwen → (lazy fidelity if needed) → select` с батч-инвариантом `for detector: for chunk` сохранён
- `pact_v4/phase1/models.py` / `Phase2B prompt` — без изменений

### 4.4 Приёмка A2
- Unit: `low→1 balanced`, `high+failed→lazy fidelity`, `high+passed→no lazy`, `both passed+disagreement→Gemma`, `both failed→quarantined`
- Dry-run на `run_005/046`: `gen 32→~18-19 (+2-3 lazy)` (`-40%`), `Qwen 32→~18-19` (`-40%`), `Gemma 13→0-2` (`-85%`), Phase 2 `77→~36`
- Валидационный прогон (владелец вне чата): `046` + `0001` remote, сравнение `selection_results` (те `2/14 fidelity wins` не должны уходить в `quarantined` или `debt` без компенсации), `translations.json` diff review
- Вся глава: `404→~310-330` (`-18-23%` вызовов, `-30-35% input_tokens` с учётом A1), + A1 audit/re-gate/formatting `→ ~250-270` (`-33-38%`), без включения reasoning/topology
- Откат: `ConfigArtifact.values["efficiency.lazy_balanced"]=false` → старое `2` поведение, `identity_hash` меняется — `resume` инвалидируется корректно

## 5. Оценка экономии (на `0001`)

| Фаза | Было (run_005) | После A1 | После A2 (+A1) |
|---|---|---|---|
| Generation | 32 | 32 (input -15%) | ~18-19 (-40%) |
| Qwen fidelity | 32 | 32 | ~18-19 (-40%) |
| Gemma preference | ~13 | ~13 | 0-2 (-85%) |
| Audit (Step 6) | 32 | ~22-24* | ~22-24 |
| Repair re-gate | 16 | ~6-8 | ~6-8 |
| Formatting fallback | 15 | ~3-5 | ~3-5 |
| **Вся глава** | **404** | **~340-360** | **~250-270** |
| `input_tokens` | 452k | ~340k (-25%) | ~300k (-33%) |
| * на 0001 все high → audit не срезается, на low-главах ~12 | | | |

`*` На `0001` все `high` → audit `32→32`; на `low`-главах `32→~12`.

Без потери по литобзору: `1 candidate` на `low` — защита от `overthinking` (`2412.21187`, `DRT`), `minimal region repair` + `cap 2-3 + quarantine` сохранены (`B2`), MBR гап закрыт Gemma только при реальном disagreement (`2310.06707`).

## 6. Порядок и зависимости

1. Внести `V5 §8.1` уточнение (уже) + этот план (docs-only) → `main`
2. Карточка A1 `docs/plans/V4_B10_EFFICIENCY_A1_TASK_RU.md` (budgeter + skip) → `vk/v4-efficiency-a1` → draft PR → review → merge
3. Карточка A2 `docs/plans/V4_B11_EFFICIENCY_A2_TASK_RU.md` (lazy balanced) → `vk/v4-efficiency-a2` → draft PR → review → merge
4. Валидационные прогоны `046` + `0001` — только владелец вне чата (правило `AGENTS.md` 2026-08-06: агент не стартует pipeline/llama-server)
5. Дальше — опционально `reasoning>0` gated по `high` и `RiskPolicy` калибровка (отдельные карточки, с `golden set` + `non-inferiority`, не в этом плане)

## 7. Риски и откат

- Потеря `locked` constraint → dead letter: `locked` never filtered, тест `locked_preserved` обязателен
- `fidelity_first` был бы лучше, но `balanced` прошёл Qwen — теоретическая потеря литературности в `2/16` (A2 §4.4). Митигация: выбор `balanced` как default (86% побед), lazy только при fail, валидация на `046` до принятия, флаг отката
- Промежуточная `bundle_hash` смена → кэш miss — ожидаемо, `resume` инвалидируется корректно, не silent fallback
- `audit skip` на low — митигация: детерминированные gate уже ловят `mixed_script/number/…`, skipped чанки не дают `findings`

## 8. Ссылки

- `docs/architecture/V5_UNIVERSAL_LITERARY_TRANSLATOR_ARCHITECTURE_RU.md` §8.1/Phase 6
- `docs/architecture/V4_LITERATURE_REVIEW_AND_RECOMMENDATION_RU.md` §2.3-2.5/§5
- `docs/plans/V4_IMPLEMENTATION_ORDER_PLAN_RU.md` §4 Поток D (Phase 6 — operations, fewer reloads/monitor)
- `D:\pact\gate_bench_runs\v4_phase12_strict_0001\run_005_remote` + анализ `selection_results.json/generation_outcomes.json`
- `pact_v4/phase2/risk.py`, `pact_v4/phase2/generation.py`, `pact_v4/runtime/bible_renderer.py`, `pact_v4/pipeline/_shared_runner_helpers.py`
