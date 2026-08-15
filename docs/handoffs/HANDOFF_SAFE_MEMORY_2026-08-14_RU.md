# HANDOFF SAFE-MEMORY (t_d55f0d3e, ветка fix-safe-memory)

Дата: 2026-08-14. Ревью-источник: `HANDOFF_GLOSSARY_BOOKMEMORY_2026-08-14_RU.md`
(глоссарий + book_memory, P0-1/2/3, P1-4/6/7).
Ветка: `fix-safe-memory` (base `origin/dev/v4.1-reasoning-transport`, HEAD `60826dd`).
Коммит: `4354111` (pushed to origin/fix-safe-memory).

## Что сделано

### 1. Каузальный BIBLE-рендер (P0 — запрет future leakage)
`pact_v4/runtime/bible_renderer.py`:
- `render_bible_section(chapter_id, chapter_index, book_memory)` — рендерит
  ТОЛЬКО детерминированную entry текущей главы (characters/facts/address из
  chapter_index.json) + narrator gender + явные seed-факты (`"seed": true`).
- Нет entry в chapter_index → fail-soft: НАРРАТОР + SEED, НИКОГДА full-dump.
- `_render_legacy_bible` + caps (`_MAX_CHARACTERS/_MAX_FACTS/_MAX_ADDRESS`)
  УДАЛЕНЫ. Модуль: `__all__ = ["render_bible_section", "extract_narrator_gender"]`.
- Регрессии: `test_future_chapter_fact_never_visible_earlier`,
  `test_fail_soft_missing_index_never_dumps`.

### 2. Dead-PID фикс экстрактора (P0)
`pact_v4/audit/entity_extractor.py`: `render_entity_extraction_prompt`
эмитит `VALID PIDS` секцию (валидные pid из source); правило 6 в промпте.
Регрессии: `test_point2_dead_pid_drops_claim` (retained > 0).

### 3. Экстрактор ДО перевода (P0)
`pact_v4/pipeline/v4_phase12_strict_runner.py` + `b3_audit_repair.py`:
- `B3AuditRepair.entity_context_prepass(source, out_dir)` — cache-aware
  экстракция, пишет `entity_context_cache.json` + validation report.
- Runner вызывает prepass ПЕРЕД whole-chapter генерацией; verified-claims →
  блок `CHAPTER ENTITY FACTS - SOURCE-DERIVED` в промпт генерации;
  candidate → ТОЛЬКО в аудит (full block).
- B3 step1 читает тот же кэш → 0 доп. вызовов (`entity_calls() == 1`).
- Сбой prepass = fail-closed ДО генерации (`RuntimeError`),
  `test_b3_entity_extractor_failure_fails_closed_before_generation`.

### 4. Verified → book_memory promote (P0, B7 replace)
`b3_audit_repair.book_memory_observations_from_entity_context(context,
chapter_id)`:
- verified gender → `characters:<name>` (high-precision: извлечённые
  verified-claims с pronoun+referent в одном PID);
- verified claims → `facts:<name>:<idx>`; candidate claims НИКОГДА.
- Rosalyn≠male, English/Shamanism≠персонажи, Blake's vehicle=motorcycle —
  `test_b3_p10_regressions_verified_only_promote`.
`v4_book_run.py`: детерминированный B7 (`book_memory_candidates.py`)
ВЫКЛЮЧЕН — после accepted-главы promote идёт из entity_context_cache.json
(0 доп. вызовов), chapter-accumulation (union `chapters`), кросс-главный
gender-disagreement fail-closed (никогда не pick-winner). Функции
`_generate_book_memory_candidates_chapter`/`_auto_promote_book_memory`
оставлены в модуле как мёртвый код-справка (тесты прямого вызова остались).

### 5. Term auto-promotion OFF (P1)
`v4_book_run._auto_promote_glossary`: ТОЛЬКО `proper_name` (порог 2
вхождения) промоутится; generic `term` (частота+стабильность) НИКОГДА —
остаётся в ledger (observations, не в prompt). Регрессии:
`test_term_never_promoted_even_above_threshold`, cumulative-guard тесты
переведены на proper_name.

### 6. АРКИ deterministic (P1)
- `StrictRunConfig.deterministic_arc_names` (config identity) → блок
  `АРКИ:\n- Bonds → Узы` в whole-chapter промпт генерации.
- CLI `--arc-names` (default: `<memory-dir>/../arc_names.json` →
  `<cwd>/arc_names.json`).
- `v4_book_html`: `arc_names` в `render_chapter_body`/`render_book` +
  `_substitute_arc_name` (заголовок «Bonds 1.3» → «Узы 1.3»).

### 7. chapter_index в book_run (A2 causal <N)
`v4_book_run`: после accepted-главы (complete/accepted_degraded) строит
`chapter_index.json` (`build_index_file`), записывает `index_built` в
book_run.json; failed-глава не трогает. Регрессии: 2 новых теста в
test_a2_chapter_index.py.

### 8. Rebuild-артефакты для ревью (НЕ применяются к production)
`safe_memory_rebuild/` (gitignored, прикладываются к карточке):
- `book_memory_seed.json` — чистый seed (pov.male, narrator Blake,
  имена глав 1-3 Bonds-арки из source, Blake's vehicle=motorcycle,
  факты `"seed": true`);
- `glossary_review_147.json` — все 147 записей классифицированы
  (107 proper_name, 12 junk_word, 19 term_candidate, 9 locked_world_term);
- `archive/` — копии текущего production book_memory/glossary (rollback).
Владелец решает, что применить — PR production-данные не правит.

## Тесты

- Полный suite: **1914 passed / 9 skipped** (Windows-safe,
  `--ignore=deployment_backups`).
- Ключевые новые/изменённые: test_b7, test_a2, test_bm, test_b9,
  test_entity_extractor (46), test_v4_phase12_strict_runner_b3 (78),
  test_v4_book_html_render (24).
- Известный Windows-flake: test_quarantined_retry_resume_reuses_prior_attempt
  (WinError 5 при переиспользованном basetemp; в изоляции/fresh basetemp
  проходит).

## Caches / resume

- `entity_context_enabled`, `deterministic_arc_names`,
  `glossary_budget_policy_version` — часть config identity; смена
  инвалидирует cache/resume (штатно).
- entity_context_cache.json: пишется ДО генерации; resume с тем же
  source_hash+extractor_version = cache hit (0 вызовов).
- Кэши и прогоны НЕ тронуты.

## Production / resume

- Production-дерево (`main`) не тронуто; production book_memory/glossary
  НЕ изменяются этим PR (артефакты review-only).
- Прогон глав: entity_extractor работает ДО генерации; verified в промпт;
  candidate только в аудит; dead PID исправлен (retained > 0).

## Decision needed (для ревьюера/владельца)

1. Применять ли `book_memory_seed.json` к production book_memory.json
   (с архивированием текущего)? Seed содержит только source-подтверждённые
   факты глав 1-3 + pov.male.
2. Glossary review: какие из 19 term_candidate и 9 locked_world_term
   зафиксировать (русские эквиваленты выбирает владелец), какие из 12
   junk_word удалить из glossary.json.
3. B7-функции оставить как мёртвый код (текущий выбор) или удалить
   целиком вместе с тестами прямого вызова?
