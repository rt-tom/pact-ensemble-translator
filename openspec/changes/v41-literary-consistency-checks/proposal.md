## Why

Change `v41-audit-literary-lenses` добавил RULE 19 (4 литературно-консистентностные
линзы: voice/register continuity, cross-chunk seam, dialogue translationese,
ambiguity flattening) в промпт Qwen-аудита v4.2-lenses. Изначально находки RULE 19
складывались в СУЩЕСТВУЮЩИЕ категории (`changed_fact`/`referent`) ради backward
compatibility схемы. Владелец + независимый ревьюер подняли, что семантически
странно класть «герой сменил регистр» в `changed_fact`, и через 20 глав нельзя
ответить «Rule 19 что ловит».

Решение (вариант B, расширение самих категорий) дополняется ВТОРОЙ частью:
**ранним и source-независимым детектором в R-editor** (русский редактор, phase3,
сразу после перевода). R-editor видит только русский текст (без EN), поэтому
source-grounded линзы (terminology vs entity facts, ambiguity flattening, voice
drift vs SOURCE register) ему недоступны — они остаются за аудитором. Но
русско-внутренние измерения литературной порчи (register/voice continuity внутри
русского текста, translationese/smoothness) R-editor ловит РАНЬШЕ (phase3) и
НЕЗАВИСИМО от источника, плюс его окно не привязано к bounded-окну аудитора.
Обе стадии сходятся в selective repair, где `fidelity_auditor` + `russian_editor`
уже склеиваются в одну находку (`source_stage`) — дублирование не страшно.

## What Changes

**A. Аудитор — расширение категорий (source-grounded).**
- RULE 19 в промпте v4.2-lenses ПЕРЕПИСЫВАЕТСЯ: вместо «report under existing
  categories (changed_fact/referent), do not invent new categories» модель
  инструктируется репортить каждую literary-consistency находку под РОВНО ОДНОЙ
  из 4 НОВЫХ категорий: `voice_continuity`, `seam`, `dialogue_translationese`,
  `ambiguity_flattening`. RULE 14 (no over-policing) сохраняется.
- Bump `prompt_version` → `pact-v4-reviewer-qwen-audit/v4.3-lenses`.
- Протяжка через гейт-контракт (единый источник `B1_AUDIT_CATEGORIES`):
  `AUDIT_V4_CATEGORIES` (`chunked_audit.py:81`), `B1_AUDIT_CATEGORIES`
  (`hard_filters.py:103`), `DEFAULT_REPAIR_CONTEXT_WINDOW_BY_CATEGORY`
  (`prompts_runtime.py:1198`).

**B. R-editor — доработка промпта (source-независимая, ранняя).**
- `RUSSIAN_EDITOR_V4_2_R1` (`prompts_runtime.py:817`): переформулировать ТОЛЬКО
  описания существующих REVIEW-классов `register` / `unnatural` / `calque` под
  LAIT literary-consistency (character-voice/register continuity внутри русского
  текста; smoothness/immersion/translationese). **Новых классов НЕ добавляем** —
  класс-перечисление фиксировано (9), валидируется в `parse_editor_edits`
  (`russian_editor.py:593`); добавление класса = contract change (сломал бы
  `_R_EDIT_KEYS`/маршрутизацию). Поэтому — только правка описаний + короткая
  LAIT-нота в Rules (держать minimal single-defect edit, НЕ лезть в SAFE).
- Bump `RUSSIAN_EDITOR_PROMPT_VERSION` (`russian_editor.py:121`) →
  `pact-v4.2-russian-editor/v4` (identity-bearing, инвалидирует R-cache).

## Capabilities

### New Capabilities
- (none)

### Modified Capabilities
- `audit-qwen-prompt` — расширение схемы вывода 4 литературными категориями
  (v4.2-lenses → v4.3-lenses).
- `russian-editor-prompt` — LAIT-фрейминг существующих REVIEW-классов
  (`v3` → `v4`), без изменения схемы/парсера/маршрутизации.

## Impact

- `pact_v4/audit/chunked_audit.py` — `AUDIT_V4_CATEGORIES` (+4), `PROMPT_VERSION`.
- `pact_v4/audit/hard_filters.py` — `B1_AUDIT_CATEGORIES` (+4).
- `pact_v4/runtime/prompts_runtime.py` — `QWEN_AUDIT_V4_1` (RULE 19 + OUTPUT enum
  + version) и `RUSSIAN_EDITOR_V4_2_R1` (REVIEW-классы + version bump) +
  `DEFAULT_REPAIR_CONTEXT_WINDOW_BY_CATEGORY` (+4).
- `pact_v4/audit/russian_editor.py` — `RUSSIAN_EDITOR_PROMPT_VERSION` bump.
- `tests/pact_v4/audit/test_chunked_audit.py:504` — enum 6→10.
- `tests/pact_v4/audit/test_russian_editor.py` — must-find/must-not-find для
  уточнённых REVIEW-классов.
- B1 gold suite — must-find для 4 new auditor categories + литературные
  must-not-find (style-variation → PASS).
- Количество вызовов моделей: без изменений.
- Смена схемы находки аудитора (enum 6→10) + смена версии R-editor: старые
  кэши/out-dir не resumable (осознанно, как в B1).
