## Why

Исследование LAIT (arXiv:2606.26040, 24.06.2026) — первое крупное читателецентричное сравнение литературного MT vs человеческого перевода (HT). Главный результат релевантный для Pact: читатели предпочитают HT не за adequacy, а за `smoothness / clarity / immersion / voice consistency`, и вариабельность MT внутри книги выше (41.7% MT-чанков с dense poor-spans vs 11.9% HT). При этом автометрики (COMET/LiTransProQA/LLM-judge) слепы и льстят MT.

Текущий production-аудит (Qwen, промпт v4.1, 15 правил, `docs/plans/V4_1_AUDIT_B1_RU.md` §4.3) ловит factual/semantic классы: `omission/addition/referent/invented_gender/changed_fact/negation`. Он **не ловит** литературно-консистентностный класс, где HT обходит MT: дрейф голоса/регистра внутри главы, швы между чанками, translationese в диалоге, «сплющивание» исходной двусмысленности. Этим классом и объясняется провал MT в LAIT.

Предложение (из `docs/audits/LAIT_PACT_PIPELINE_IMPROVEMENT_PROPOSAL_RU.md` §2.1): расширить промпт Qwen-аудита literary-consistency линзами из P3/LAIT — **в рамках того же 1 вызова Qwen, 0 дополнительных вызовов моделей**.

## What Changes

- Расширение frozen production-промпта аудита v4.1 (`QWEN_AUDIT_V4_1` в `pact_v4/runtime/prompts_runtime.py`, используется `ChunkedAuditEvaluator`) ЕДИНЫМ ПРАВИЛОМ 19 «LITERARY CONSISTENCY CHECKS» (4 проверки), привязанным к видимости почанкового аудитора (локальная voice/register continuity между `AUDIT_PAIRS` и `CONTEXT_ONLY` окном; смежные cross-chunk seam + несогласование с `CHAPTER ENTITY FACTS`; dialogue naturalness как translationese-only; loss-of-ambiguity/flattening как meaning-change-only). Правило НЕ претендует на chapter-wide консистентность — аудитор не видит всю главу.
- Каждое новое правило жёстко привязано к существующим защитам: требует SOURCE-evidence (правило 3), наследует «when uncertain, PASS», и явно подчиняется правилу 13 («do NOT over-police register/style»). Новые правила — это semantic/consistency-проверки, не style-polishing.
- Bump `prompt_version` (раздельно от `harness_version`); смена входит в audit identity.
- Regression suite B1 §6 (8 must-find + 6 must-not-find) дополняется literary must-not-find кейсами (стилистическая вариация/регистр → PASS).

## Capabilities

### New Capabilities
- (none)

### Modified Capabilities
- `audit-qwen-prompt` — расширение промпта v4.1 литературно-консистентностными линзами (4 правила), без изменения топологии вызовов.

## Impact

- `pact_v4/audit/chunked_audit.py` — `render_qwen_audit_prompt` (текст промпта + `prompt_version`).
- `docs/plans/V4_1_AUDIT_B1_RU.md` §4.3 — фиксация новых правил (замороженный промпт v4.1 → v4.2-lenses).
- Regression suite (B1 §6) — добавление literary must-not-find контрактов.
- Количество вызовов моделей: **без изменений** (те же 1 вызов Qwen на аудит-чанк; выходные токены в пределах `max_tokens=12000`).
- Не затрагивает генерацию, repair, formatting, identity/resume детерминизм (кроме ожидаемого bump `prompt_version` в audit identity).

---
**Note 2026-08-26:** Superseded by `v41-literary-consistency-checks` (31 tasks, merged at 71a1f87). This change is kept for history.
