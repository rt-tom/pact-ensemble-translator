# V4 Efficiency A1 — glossary budgeter + prompt prefix ordering + D1-telemetry

- План: `docs/plans/V4_EFFICIENCY_A_OPTIMIZATION_PLAN_RU.md` (ред. 2, §2)
- Статус: реализовано (A1.1–A1.3, ветка vk/v4-efficiency-a1, 7552267+c4d219d) — интеграционная проверка пройдена (134 целевых теста; смоук 21/21: статичный префикс, glossary-фильтр с always_include, D1-фазы), на ревью (RV; PR после approve)
- Масштаб: `pact_v4/pipeline`, `pact_v4/phase2/*`, `pact_v4/runtime/prompts_runtime.py`, `pact_v4/runtime/backend_role_adapters.py` + тесты/скрипты валидации. Без `v4_book_run`/glossary-схемы.

## A1.1 Glossary budgeter

- `_glossary_entries` (или `_glossary_entries_for_chunk`): пара из glossary.json попадает в `PromptBundle.glossary` только если source-термин присутствует в `owned_source + left_context + right_context` (та же `_term_present` из `pact_v4/phase2/risk.py`: `(?<!\w){re.escape(term)}(?!\w)`, IGNORECASE; мультисловные термины поддерживаются).
- **`always_include`, fail-closed**: пары, связанные с `narrator_gender`; записи с `glossary_conflict`; категории `required_risk_feature_codes` чанка (`number_word`, `tone_profanity`) — никогда не режутся.
- Диагностика: отчёт «отброшено N пар: [термины]» (новый файл в out_dir или секция в `selection_meta.json`).
- `bundle_hash` пересчитывается от отфильтрованного набора (resume/кэш-инвалидация корректная, без silent fallback).

## A1.2 Prompt prefix ordering (provider cache)

- В generation, Qwen fidelity, Qwen audit, Gemma audit промптах статичные блоки (template instructions + полная библия `bible_block` + style/policy константы) размещаются в **начале** сообщения; динамические (CHUNK_ID, source/translation, glossary, context) — после.
- Содержимое не меняется, только порядок.
- Тест: снимки промптов (snapshot) — статичный префикс идентичен для разных чанков одного прогона; контент-эквивалентность (тот же набор блоков).

## A1.3 D1-telemetry отчёт по фазам

- Скрипт/расширение `v4_usage`: разрез вызовов/токенов по ролям/фазам из `usage.ndjson` (gen / qwen fidelity / gemma preference / audit / repair / formatting), с cached/reasoning-токенами.
- Ничего не меняет в пайплайне (read-only диагностика).

## Non-goals

- Библия не режется (решение владельца) — никаких фильтров по `name in chunk_text`.
- Audit skip (Step 6) НЕ вводится.
- Bundle dedup по `bundle_hash` НЕ вводится.
- Qwen re-gate bypass для repair НЕ вводится (только A1b, после данных).
- formatting/repair транспорт не меняется (A1b).

## Приёмка

- `C:\Python314\python.exe -m pytest tests --ignore=deployment_backups -q` — все зелёные (+ новые: glossary фильтр с `always_include`, prefix-снимки).
- Dry-run скрипт на артефактах run_005: отчёт по отброшенным glossary-парам; замер input_tokens до/после на тех же чанках.
- `bundle_hash` меняется только на чанках с отфильтрованным glossary; resume не инвалидируется лишне.
- DECISIONS.md entry + merge (PR после approve).
