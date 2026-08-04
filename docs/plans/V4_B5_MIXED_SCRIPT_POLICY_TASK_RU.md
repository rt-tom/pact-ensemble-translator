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

1. **Source-derived allowlist**: автоматически извлекать латинские токены из источника (source HTML), которые присутствуют в переводе. Если токен есть в source и в translation — он легитимен (не mixed_script violation).
   - Извлекать из `source_map` (уже есть в strict runner).
   - Сравнивать с tokens в translation (case-insensitive).
   - Добавлять в `mixed_script_allow` для каждого chunk.
2. **Manual allowlist через конфиг**: `deterministic_mixed_script_allow` в `StrictRunConfig` — ручной override для случаев, когда автоматика не покрывает (например, специальные термины).
3. **Комбинированный allowlist**: `final_allow = source_derived + manual_config`.
4. **Применение**: передавать `final_allow` в `find_mixed_script` в gate (Phase 2C cascade), audit (Phase 3B), repair (Phase 4).
5. **Identity**: source-derived allowlist зависит от source — смена источника инвалидирует cache/resume (уже есть `source_hash` в снапшоте).

## Вне scope (другие карточки)

- Транслитерация (альтернативный подход) — не реализуем в B5 (решение владельца: allowlist).
- B6–B8 — отдельные карточки.
- Phase 1/2, cascade, risk, prompts — нельзя менять (кроме передачи allowlist).

## Тесты

- Unit: source-derived allowlist извлекает латинские токены из source; manual config override работает; комбинированный allowlist корректен.
- Integration: chunk с "R.D.T." в source и translation — mixed_script не срабатывает; chunk с "R.D.T." в source, но "А.Б.В." в translation — mixed_script срабатывает (не легитимен).
- Resume: смена источника инвалидирует cache (уже есть source_hash).
- Полный `tests/pact_v4/` зелёный.

## Gate / Acceptance

1. Source-derived allowlist автоматически извлекает латинские токены из source.
2. Manual allowlist через конфиг работает.
3. Комбинированный allowlist передаётся в gate/audit/repair.
4. chunk0001 p00013 "R.D.T." — mixed_script не срабатывает (разблокировка).
5. DECISIONS.md — запись о mixed_script-политике (в том же коммите).

## Роль-сплит

Обычная V4-фаза: «реализует, второй — adversarial review». Перед стартом спросить, кто пишет код.

## Компактный промпт

```text
Реализуй v4 B5 (mixed_script-политика) из
docs/plans/V4_IMPLEMENTATION_ORDER_PLAN_RU.md (Поток B+, B5).
Target: main. Draft PR. Source-derived allowlist + manual config override.
Не трогай v3, phase1/2, cascade, risk, prompts (кроме передачи allowlist).
```
