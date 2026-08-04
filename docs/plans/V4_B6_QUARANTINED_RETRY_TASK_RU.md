# V4 B6 — Quarantined-чанки: отдельный цикл ремонта (task)

Backing spec:
- `docs/plans/V4_IMPLEMENTATION_ORDER_PLAN_RU.md` (§4, Поток B+ — B6).
- `DECISIONS.md` (2026-08-03: порядок B4–B8, B6 — quarantined-чанки).
- `docs/audits/V4_PHASE12_STRICT_0001_RUN001_ANALYSIS_RU.md` (chunk0005/0009/0010 — quarantined, repair не прошёл re-gate).
- Зависит от B4 (JSON-устойчивость, влита в `main`) и B5 (mixed_script-политика, влита в `main`).

Target: `main`. Draft PR. Характер: REVIEW REQUIRED — изменение repair-логики для quarantined чанков.

## Зачем это отдельная карточка

run_001: 4 quarantined чанка (103 параграфа) — chunk0001 (mixed_script, разблокируется B5), chunk0005/0009/0010 (qwen_fidelity). B2 repair попытался чинить quarantined чанки, но re-gate систематически не проходит: 20 долгов chunk0005, 3 chunk0009, 5 chunk0010.

Текущая логика: quarantined чанки получают best-variant (макс. пройденных гейтов), затем repair пытается чинить findings. Но если оба кандидата упали на одинаковых finding'ах (chunk0005/0009/0010 — qwen_fidelity), repair не может закрыть finding без новой генерации.

**Проблема:** quarantined чанки остаются "недовыпущенными" — они попадают в `repair_report.final_translation` (через best-variant), но repair-долг остаётся, и `complete` недостижим.

## Что реализовать

1. **Диагностика**: определить, почему repair не проходит для quarantined чанков.
   - chunk0005: пропуск предложения + род (p00095, p00099) — модель не видит source context.
   - chunk0009: `grandchild` → `внук` (род вопреки gender-neutral source) — модель не видит следующее уточнение.
   - chunk0010: `well after dark` → `далеко за полночь` (противоречие следующей строке) — модель не видит контекст.

2. **Политика**: решить, что делать с quarantined чанками.
   - **Вариант A (рекомендуемый)**: отдельный цикл ремонта — перегенерация quarantined чанков с расширенным контекстом (следующий chunk как look-ahead), затем повторный cascade.
   - **Вариант B**: признание карантинных финальными с best-variant (текущее поведение), но с явной маркировкой `quarantined_final` в артефактах.
   - **Вариант C**: skip quarantined чанков — не включать в `translations.json`, но включать в `repair_report.final_translation` как placeholder.

3. **Реализация (Вариант A)**:
   - После B2 repair: проверить, есть ли quarantined чанки с repair-долгом.
   - Если есть — запустить отдельный цикл: перегенерация с look-ahead (следующий chunk source), повторный cascade.
   - Если перегенерация прошла — заменить best-variant на новый кандидат.
   - Если перегенерация не прошла — признать финальным с best-variant (Вариант B fallback).

4. **Артефакты**:
   - `quarantined_retry.json`: история попыток перегенерации (chunk_id, attempt, outcome).
   - Обновить `repair_report.json`: `quarantined_final` (true/false), `retry_attempts` (count).

5. **Identity**: quarantined retry не меняет source/snapshot/chunk_plan identity, но меняет `generation_outcomes` (новые кандидаты). Resume должен корректно обрабатывать quarantined_retry.

## Вне scope (другие карточки)

- B7 (библия + междуглавная) — отдельная карточка.
- B8 (повторный прогон главы 0001) — отдельная карточка.
- Phase 1/2, cascade, risk, prompts — нельзя менять (кроме передачи look-ahead context).

## Тесты

- Unit: quarantined чанк с repair-долгом → перегенерация с look-ahead → новый кандидат прошёл cascade → замена best-variant.
- Integration: fake backend возвращает quarantined чанк → retry с look-ahead → success.
- Resume: quarantined_retry корректно восстанавливается после обрыва.
- Полный `tests/pact_v4/` зелёный.

## Gate / Acceptance

1. Quarantined чанки с repair-долгом запускают отдельный цикл ремонта.
2. Перегенерация с look-ahead (следующий chunk source) — bounded (1 retry).
3. Если перегенерация прошла — замена best-variant на новый кандидат.
4. Если перегенерация не прошла — признание финальным с best-variant + маркировка `quarantined_final: true`.
5. `quarantined_retry.json` артефакт сохраняется.
6. DECISIONS.md — запись о quarantined-политике (в том же коммите).

## Роль-сплит

Обычная V4-фаза: «реализует, второй — adversarial review». Перед стартом спросить, кто пишет код.

## Компактный промпт

```text
Реализуй v4 B6 (quarantined-чанки) из
docs/plans/V4_IMPLEMENTATION_ORDER_PLAN_RU.md (Поток B+, B6).
Target: main. Draft PR. Отдельный цикл ремонта для quarantined чанков с repair-долгом.
Не трогай v3, phase1/2, cascade, risk, prompts (кроме передачи look-ahead context).
```
