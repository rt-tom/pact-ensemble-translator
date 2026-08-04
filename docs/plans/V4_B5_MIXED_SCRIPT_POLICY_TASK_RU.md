# V4 B5 — mixed_script-политика: allowlist легитимных латинских токенов (task)

Backing spec:
- `docs/plans/V4_IMPLEMENTATION_ORDER_PLAN_RU.md` (§4, Поток B+ — B5).
- `DECISIONS.md` (2026-08-03: порядок B4–B8, B5 — mixed_script-политика).
- `docs/audits/V4_PHASE12_STRICT_0001_RUN001_ANALYSIS_RU.md` (chunk0001 p00013 "R.D.T." — единственный структурный блокер run_001).
- Зависит от B4 (JSON-устойчивость, влита в `main`).

Target: `main`. Draft PR. Характер: REVIEW REQUIRED — изменение deterministic-слоя, влияние на gate/audit/repair.

## Зачем это отдельная карточка

run_001: chunk0001 карантин из-за `mixed_script` p00013 "R.D.T." (легитимные латинские инициалы источника). 8 правок chunk0001 заблокированы mixed_script-gate. Это единственный структурный блокер run_001 (integrity failed, `complete` недостижим).

Механизм allowlist уже есть (`find_mixed_script(text, allow)` в `_integrity_checks.py:123`), но `deterministic_mixed_script_allow` в `StrictRunConfig` по умолчанию пустой и нигде не заполняется автоматически.

## Что реализовать

1. **Библия (book_bible.json)**: извлекать латинские термины из библии. Если термин в библии содержит латинские символы (например, "R.D.T.", "Dr.", "Mr.") — добавлять в allowlist. Это основной источник, т.к. библия уже содержит термины и их переводы.
2. **Глоссарий (glossary.json)**: извлекать латинские термины из глоссария. Если термин в глоссарии содержит латинские символы — добавлять в allowlist. Дополнительный источник.
3. **Source-derived allowlist**: автоматически извлекать латинские токены из источника (source HTML), которые присутствуют в переводе. Если токен есть в source и в translation — он легитимен.
4. **Manual allowlist через конфиг**: `deterministic_mixed_script_allow` в `StrictRunConfig` — ручной override для случаев, когда автоматика не покрывает.
5. **Комбинированный allowlist**: `final_allow = bible + glossary + source_derived + manual_config`.
6. **Применение**: передавать `final_allow` в `find_mixed_script` в gate (Phase 2C cascade), audit (Phase 3B), repair (Phase 4).
7. **Identity**: allowlist зависит от библии/глоссария/source — смена любого из них инвалидирует cache/resume (уже есть `book_memory_hash`/`glossary_hash`/`source_hash` в снапшоте).

## Вне scope (другие карточки)

- Транслитерация (альтернативный подход) — не реализуем в B5 (решение владельца: allowlist).
- B6–B8 — отдельные карточки.
- Phase 1/2, cascade, risk, prompts — нельзя менять (кроме передачи allowlist).

## Тесты

- Unit: библия/глоссарий извлекают латинские термины; source-derived извлекает токены из source; manual config override работает; комбинированный allowlist корректен.
- Integration: chunk с "R.D.T." в библии — mixed_script не срабатывает; chunk с "R.D.T." в source и translation — mixed_script не срабатывает; chunk с "R.D.T." в source, но "А.Б.В." в translation и не в библии — mixed_script срабатывает.
- Resume: смена библии/глоссария/источника инвалидирует cache (уже есть book_memory_hash/glossary_hash/source_hash).
- Полный `tests/pact_v4/` зелёный.

## Gate / Acceptance

1. Библия (book_bible.json) — латинские термины извлекаются в allowlist.
2. Глоссарий (glossary.json) — латинские термины извлекаются в allowlist.
3. Source-derived allowlist автоматически извлекает латинские токены из source.
4. Manual allowlist через конфиг работает.
5. Комбинированный allowlist передаётся в gate/audit/repair.
6. chunk0001 p00013 "R.D.T." — mixed_script не срабатывает (разблокировка).
7. DECISIONS.md — запись о mixed_script-политике (в том же коммите).

## Роль-сплит

Обычная V4-фаза: «реализует, второй — adversarial review». Перед стартом спросить, кто пишет код.

## Компактный промпт

```text
Реализуй v4 B5 (mixed_script-политика) из
docs/plans/V4_IMPLEMENTATION_ORDER_PLAN_RU.md (Поток B+, B5).
Target: main. Draft PR. Библия/глоссарий + source-derived allowlist + manual config override.
Не трогай v3, phase1/2, cascade, risk, prompts (кроме передачи allowlist).
```
