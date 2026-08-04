# V4 B4 — JSON-устойчивость: retry для пустого/обрезанного JSON (task)

Backing spec:
- `docs/plans/V4_IMPLEMENTATION_ORDER_PLAN_RU.md` (§4, Поток B+ — B4).
- `DECISIONS.md` (2026-08-03: порядок B4–B8, B4 — база для всех).
- Зависит от B1–B3 (все влиты в `main`).

Target: `main`. Draft PR. Характер: REVIEW REQUIRED — retry-логика для модельных
вызовов, влияние на resume/identity.

## Зачем это отдельная карточка

run_001 выявил: пустой ответ qwen-audit (chunk0011) → `step6: incomplete`;
≥1 обрезанный JSON при repair → долг. Оба корректно не стали семантическими
терминальными статусами (правило «never accept truncated JSON»), но риск
повторяется. B4 — база для всех последующих задач (B5–B8): без retry
qwen-audit/repair будут ломаться на тех же ошибках.

## Что реализовать

1. **Retry для qwen-audit (Step 6)**: при пустом ответе или `JSONDecodeError` —
   retry с bounded count (по умолчанию 2), exponential backoff. Transport
   failure (timeout/network) — отдельный error class, не retry'ится как JSON
   error. `reasoning=0` соблюдается (B1).
2. **Retry для repair (Step 7)**: при обрезанном JSON в repair-ответе — retry
   с bounded count (по умолчанию 2). Transport failure — отдельный error class.
   Repair-артефакты несут backend identity (B2).
3. **Error classification**: ввести явные классы `EmptyResponseError`,
   `TruncatedJSONError`, `TransportError` (уже есть в C1 `OpenCodeError`).
   Retry только для первых двух, не для transport.
4. **Resume-совместимость**: retry не меняет identity (тот же prompt/backend).
   Если retry исчерпан — failed unit (audit) или debt (repair), не semantic
   verdict.
5. **Конфиг**: `max_retries: 2` по умолчанию, override через runtime config
   (опционально).

## Вне scope (другие карточки)

- Phase 1/2, cascade, risk, prompts — нельзя менять.
- B5–B8 — отдельные карточки.
- Transport retry (timeout/network) — уже в C1, не дублировать.

## Тесты

- Unit: empty response → retry → success; empty response → retry exhausted →
  failed unit; truncated JSON → retry → success; truncated JSON → retry
  exhausted → debt.
- Integration: fake backend возвращает empty/truncated JSON на первый вызов,
  success на второй — audit/repair проходит.
- Resume: retry не меняет identity; foreign-identity проверка работает.
- Полный `tests/pact_v4/` зелёный.

## Gate / Acceptance

1. qwen-audit retry при empty/truncated JSON (bounded count).
2. repair retry при truncated JSON (bounded count).
3. Transport failure не retry'ится как JSON error.
4. Retry исчерпан → failed unit (audit) / debt (repair), не semantic verdict.
5. Identity не меняется при retry.
6. DECISIONS.md — запись о retry-политике (в том же коммите).

## Роль-сплит

Обычная V4-фаза: «реализует, второй — adversarial review». Перед стартом
спросить, кто пишет код.

## Компактный промпт

```text
Реализуй v4 B4 (JSON-устойчивость) из
docs/plans/V4_IMPLEMENTATION_ORDER_PLAN_RU.md (Поток B+, B4).
Target: main. Draft PR. Retry для empty/truncated JSON в qwen-audit и repair.
Не трогай v3, phase1/2, cascade, risk, prompts.
```
