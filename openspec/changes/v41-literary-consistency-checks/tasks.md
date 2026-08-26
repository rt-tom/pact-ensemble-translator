## A. Аудитор — расширение категорий (source-grounded)

## 1. Vocab: единый источник правды
- [x] 1.1 `hard_filters.py:103` `B1_AUDIT_CATEGORIES` → +4 (`voice_continuity`, `seam`, `dialogue_translationese`, `ambiguity_flattening`) — verify: 10 категорий
- [x] 1.2 `chunked_audit.py:81` `AUDIT_V4_CATEGORIES` → синхронно +4 — verify: 10
- [x] 1.3 `chunked_audit.py:89` `PROMPT_VERSION` → `pact-v4-reviewer-qwen-audit/v4.3-lenses`; `QWEN_AUDIT_V4_1.version` синхронно — verify: равны
- [x] 1.4 docstring `chunked_audit.py:21` обновить — verify: ручная

## 2. Prompt delta (RULE 19 + OUTPUT enum)
- [x] 2.1 `prompts_runtime.py:526-528` заменить «under existing categories / do not invent» на инструкцию 4 new categories — verify: grep «do not invent» НЕТ
- [x] 2.2 `prompts_runtime.py:540` OUTPUT enum → +4 категории — verify: grep enum, 10 значений
- [x] 2.3 RULE 14 (no over-policing) сохранён — verify: grep, ручная

## 3. Repair context windows
- [x] 3.1 `prompts_runtime.py:1198` `DEFAULT_REPAIR_CONTEXT_WINDOW_BY_CATEGORY` → +`voice_continuity:10, seam:10, dialogue_translationese:3, ambiguity_flattening:3` — verify: 7 записей

## 4. Auditor regression + eligibility
- [x] 4.1 `tests/pact_v4/audit/test_chunked_audit.py:504` enum 6→10 — verify: pytest
- [x] 4.2 Hard-filter: new category НЕ REJECT-ится, классификация `TIER_B` (default-ветка) — verify: pytest, verdict==TIER_B
- [x] 4.3 Reaudit-validation: new category проходит `category not in AUDIT_V4_CATEGORIES` (`selective_repair.py:215`) — verify: pytest
- [x] 4.4 Repair eligibility: new-category high/medium → tier-B eligible; low → ineligible — verify: pytest
- [x] 4.5 Repair render: `_render_finding_block` печатает new category — verify: pytest
- [x] 4.6 B1 gold must-find: для КАЖДОЙ из 4 new categories ≥1 кейс (mock backend, 0 вызовов) — verify: pytest
- [x] 4.7 B1 gold must-not-find: 6 старых must-find удержаны; литературные must-not-find (style variation → PASS, БЕЗ new-category) — verify: 8/8 + N/N, НЕТ новых FP

## B. R-editor — LAIT-фрейминг (source-независимый, ранний)

## 5. R-editor prompt refinement (ТОЛЬКО описания REVIEW-классов)
- [x] 5.1 `prompts_runtime.py:817` `RUSSIAN_EDITOR_V4_2_R1`: переформулировать `register` → character-voice/register continuity внутри русского текста (cross-chunk) — verify: grep, описание содержит voice/register continuity
- [x] 5.2 `unnatural` → smoothness/immersion/translationese; `calque` → translationese-фрейминг — verify: grep
- [x] 5.3 Класс-перечисление в JSON-схеме НЕ меняется (остаётся 9: typo|grammar|duplicate|preposition|calque|logic|ambiguity|unnatural|register) — verify: grep enum, 9 значений
- [x] 5.4 Rules: добавить LAIT-ноту — minimal single-defect edit, литературные суждения ТОЛЬКО в REVIEW (НЕ в SAFE) — verify: grep, ручная
- [x] 5.5 `russian_editor.py:121` `RUSSIAN_EDITOR_PROMPT_VERSION` → `pact-v4.2-russian-editor/v4` — verify: compileall, константа обновлена

## 6. R-editor regression
- [x] 6.1 `tests/pact_v4/audit/test_russian_editor.py`: must-find — русско-внутренний register-break внутри чанка (class=register) + translationese/unnatural — verify: pytest mock, 0 вызовов
- [x] 6.2 must-not-find: style variation / register choice → PASS; НЕ эмитит SAFE-класс для литературного суждения — verify: pytest
- [x] 6.3 parse/route не сломаны: model всё ещё эмитит одну из 9 ALL_CLASSES — verify: pytest (существующие parse-тесты зелёные)

## 7. Legacy / scope check
- [x] 7.1 `phase3/audit.py:111` `QWEN_AUDIT_CATEGORIES` НЕ на B3-пути — verify: grep вызовов, НЕ менять
- [x] 7.2 `_NUMERIC/_STRING/_GENDER_CATEGORIES` НЕ содержат new categories; new → default TIER_B — verify: чтение кода

## 8. Static + risk gates
- [x] 8.1 `pact-fidelity-lint` (касается prompts/audit/r-editor) — verify: lint без нарушений
- [x] 8.2 `pact-risk-test` Medium (промпт/аудит/repair-гейт/r-editor) → узкие тесты — verify: зелёные

## 9. Review
- [x] 9.1 pact-dev → pact-rev (pact-pi-review, max 4 раунда, convergence) — verify: APPROVED
- [x] 9.2 `pact-git-hygiene` перед PR — verify: `git diff --check` clean
- [x] 9.3 `openspec validate --change v41-literary-consistency-checks` — verify: green

## 10. Pre-merge (owner decision)
- [x] 10.1 Аппрув владельца на merge — merge ≠ deploy ≠ запуск пайплайна
