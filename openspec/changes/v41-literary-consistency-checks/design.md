## Context

См. proposal.md — Why. Две части: (A) аудитор ловит source-grounded литературные
находки под 4 новыми категориями; (B) R-editor ловит русско-внутренние
литературные дефекты РАНЬШЕ (phase3) и НЕЗАВИСИМО от источника. Вместе — покрытие
литературного класса LAIT без дублирования (merge в selective repair).

Архитектурные факты, на которых стоит design:
- Аудитор: запись `{id,category,severity,confidence,note,excerpt,_debug}`
  (`b3:1007`); `category` валидируется на 3 уровнях (вывод модели
  `chunked_audit.py:446`; hard-filter `hard_filters.py:589`; reaudit
  `selective_repair.py:215`) и гейтит eligibility (`selective_repair.py:632`) +
  окно контекста repair (`prompts_runtime.py`). `B1_AUDIT_CATEGORIES`
  (`hard_filters.py:103`) — единый источник, питает дефолты hard-filter и repair;
  пайплайн НЕ передаёт `allowed_categories` явно (`b3:4251`).
- Reaudit ПЕРЕИСПОЛЬЗУЕТ `QWEN_AUDIT_V4_1` (`prompts_runtime.py:1382`).
- R-editor: `RUSSIAN_EDITOR_V4_2_R1` (`prompts_runtime.py:817`), Russian-only,
  чанково (edit_pairs + context_pairs). Классы фиксированы: SAFE (4) + REVIEW (5)
  = `ALL_CLASSES` (`russian_editor.py:132-135`); `parse_editor_edits` (`:593`) и
  `route_edits` (`:689`) валидируют `klass in ALL_CLASSES`. REVIEW-классы идут
  через верификатор (НЕ авто-apply); SAFE — авто-apply.

## Goals / Non-Goals

**Goals:**
- (A) Сделать 4 линзы RULE 19 first-class категориями аудитора; протянуть через
  весь гейт-контракт.
- (B) Добавить LAIT-фрейминг в R-editor через переформулирование СУЩЕСТВУЮЩИХ
  REVIEW-классов (`register`/`unnatural`/`calque`) — ранний, source-независимый
  детектор русско-внутренней литературной порчи.

**Non-Goals:**
- Не добавлять НОВЫЕ классы в R-editor (это contract change: сломал бы
  `ALL_CLASSES`/`_R_EDIT_KEYS`/маршрутизацию и валидатор).
- Не переносить source-grounded линзы (terminology vs entity facts,
  ambiguity flattening, voice drift vs SOURCE register) в R-editor — там нет EN.
- Не менять топологию/количество вызовов; не менять repair-промпт.
- Не трогать legacy `phase3/audit.py:111`.

## Decisions

**D1 (A). Имена категорий = 4 линзы RULE 19:** `voice_continuity`, `seam`,
`dialogue_translationese`, `ambiguity_flattening`.

**D2 (A). Единый источник `B1_AUDIT_CATEGORIES`** → +4; автоматом протягивается в
hard-filter default и repair `allowed_categories` default. Footgun (forget →
new findings REJECT/ineligible) закрыт одним местом + regression 4.2.

**D3 (A). `AUDIT_V4_CATEGORIES` синхронно +4** (вывод модели + reaudit).

**D4 (A). Окна по категории** (`prompts_runtime.py:1198`): `voice_continuity:10,
seam:10` (нужен предшествующий контекст), `dialogue_translationese:3,
ambiguity_flattening:3` (локально).

**D5 (A). Hard-filter ветки не трогаем** — новые категории НЕ в `_NUMERIC/
_STRING/_GENDER` → Default-ветка → `TIER_B` (semantic verification).

**D6 (A). prompt_version bump** v4.2-lenses → v4.3-lenses.

**D7 (A). RULE 19 инструкция ПЕРЕПИСЫВАЕТСЯ** (не дополняется): вместо
«under existing categories / do not invent» → «report under exactly ONE of the
4 new categories». OUTPUT enum (`:540`) дополняется 4 категориями.

**D8 (B). Только правка описаний REVIEW-классов, НЕ новые классы.**
`register` → «character voice / register continuity внутри русского текста
(сдвиг регистра персонажа без нарративного триггера; несогласованность регистра
между чанками)». `unnatural` → «неидиоматичный / машинный / translationese
русский (smoothness/immersion)». `calque` → добавить translationese-фрейминг.
Класс-перечисление в JSON-схеме промпта НЕ меняется (остаётся 9) → парсер и
маршрутизация нетронуты.

**D9 (B). LAIT-нота в Rules:** явно удержать «minimal single-defect edit, do not
rewrite the paragraph» и «литературные суждения — ТОЛЬКО в REVIEW-классах, НЕ в
SAFE» (иначе voice-правка попадёт в авто-apply и сработает over-police style без
верификатора). Это защита безопасности применений.

**D10 (B). Bump `RUSSIAN_EDITOR_PROMPT_VERSION`** `v3` → `v4` (identity-bearing,
инвалидирует R-cache). Однострочно, не contract change.

## Proposed implementation delta

**A. Аудитор**
1. `chunked_audit.py`: `AUDIT_V4_CATEGORIES` (`:81`) +4; `PROMPT_VERSION` (`:89`)
   → `pact-v4-reviewer-qwen-audit/v4.3-lenses`; docstring (`:21`).
2. `hard_filters.py`: `B1_AUDIT_CATEGORIES` (`:103`) +4.
3. `prompts_runtime.py`: `QWEN_AUDIT_V4_1.version` → v4.3-lenses; RULE 19 конец
   (`:526-528`) заменить на инструкцию 4 new categories; OUTPUT enum (`:540`)
   +4; `DEFAULT_REPAIR_CONTEXT_WINDOW_BY_CATEGORY` (`:1198`) +4.

**B. R-editor**
4. `prompts_runtime.py` `RUSSIAN_EDITOR_V4_2_R1` (`:817`): переформулировать
   описания `register`/`unnatural`/`calque` + короткая LAIT-нота в Rules
   (D8/D9). Класс-перечисление НЕ менять.
5. `russian_editor.py` `RUSSIAN_EDITOR_PROMPT_VERSION` (`:121`) →
   `pact-v4.2-russian-editor/v4`.

## Risks / Trade-offs

- [A: смена схемы находки enum 6→10] → старый `audit_cache_b3.json` новых категорий
  не содержит; cross-version resume старого аудита новым repair fail-loud (safe).
- [A: footgun B1_AUDIT_CATEGORIES] → D2 + regression 4.2 (new category НЕ reject,
  eligible tier-B).
- [B: R-editor начнёт «улучшать голос» сверх minimal] → D9 (Rules: minimal
  single-defect, только REVIEW); regression 6.2 (style variation → PASS, не лезть
  в SAFE).
- [B: bump версии R-editor] → инвалидирует R-cache (осознанно).
- [Рост токенов] → в пределах `max_tokens` (B1 §2 / R-editor лимиты).
- [Legacy `phase3/audit.py:111`] → проверить не-B3, не менять.

## Migration Plan

- A локализовано в промпте + 2 vocab-сетах + карте окон. B локализовано в промпте
  + bump версии. Feature-flag не нужен. Rollback — revert commit.
- Реализация ТОЛЬКО в изолированном worktree (не main, не RT), через
  pact-dev → pact-rev (pact-pi-review, max 4 раунда) → pact-git-hygiene → аппрув
  владельца на merge. Merge ≠ deploy ≠ запуск пайплайна.
- Аналитика: аудитор — группировка по `category` в `audit_cache_b3.json`
  (`stage_progress.audit.issues[].category`) и `audit_journal.ndjson`. R-editor —
  группировка по `class` в `r_editor_report.json` / stage_progress.r_editor;
  литературные находки видны как `register`/`unnatural`/`calque` с `source_stage=
  russian_editor`. Обе стадии сходятся в repair через merge (`source_stage`).

## Open Questions

- Нужен ли отдельный spec.md — решается по `openspec validate` (опционален).
- Хотим ли wider window ещё и для `dialogue_translationese` (диалог может занимать
  несколько PID) — уточняется на реальных главах.
- Достаточно ли текущих REVIEW-классов R-editor для LAIT, или со временем всё-таки
  нужен НОВЫЙ REVIEW-class (тогда contract change, риск выше) — решается по
  данным прогона, НЕ в этом change.
