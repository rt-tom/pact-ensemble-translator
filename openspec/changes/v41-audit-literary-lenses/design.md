## Context

См. proposal.md — Why. Production-аудит заморожен как `prompt v4.1` (семантика) + `harness v4.2 infra` (`docs/plans/V4_1_AUDIT_B1_RU.md` §7.6, решение 2026-08-10). Текущий промпт — 15 правил, правило 13 прямо запрещает «over-police register/style» чтобы не вернулись FP. LAIT (2026) доказал, что именно literary-consistency класс (voice/immersion/consistency) — главная причина выбора HT над MT. Цель этого change: добавить этот класс в аудит **не ломая FP-защиту**, в рамках того же 1 вызова Qwen.

## Goals / Non-Goals

**Goals:**
- Ловить literary-consistency провалы (дрейф голоса/регистра внутри главы, швы между чанками, translationese в диалоге, сплющивание двусмысленности) как semantic/consistency findings.
- Сохранить 0 доп. вызовов моделей (тот же 1 Qwen-вызов, расширенный промпт).
- Не нарушить защиту от FP: каждое новое правило требует SOURCE-evidence, наследует «when uncertain PASS», подчиняется правилу 13.

**Non-Goals:**
- Не менять топологию аудита (chunking/overlap/Tier A/Tier B/repair-as-verifier) — только текст промпта.
- Не вводить style-polishing / «сделать более литературным» — это запрещено правилом 13.
- Не менять генерацию/repair/formatting, не менять identity/resume кроме `prompt_version` в audit identity.
- Не копировать per-chunk agentic loops из P3 (дорого, не лучше — см. LAIT_PACT proposal §1).

## Decisions

**D1. Форма изменения — delta к существующим 15 правилам, не переписывание.** Добавляется новая секция «§ Literary consistency (semantic, NOT stylistic preference)» с правилами 16–19. Все 15 старых правил сохраняются дословно.

**D2. Правило 19 — bounded к видимости почанкового аудитора и source-grounded.** Аудитор видит НЕ весь chapter, а `AUDIT_PAIRS` (текущий чанк, аудируемый) + `CONTEXT_ONLY` предшествующее окно (~2–6 пар, НЕ аудируемое) + narrator/entity context (канонические имена, fallback-only) + `CHAPTER ENTITY FACTS` (canonical map, отдельный слой). Поэтому секция добавляется КАК ЕДИНОЕ ПРАВИЛО 19 (в production-промпте `QWEN_AUDIT_V4_1` правила уже нумерованы 1–18; новая секция — 19, без коллизий), содержащее 4 проверки, привязанные к окну, а не к главе целиком:
- 19·voice/register continuity (ЛОКАЛЬНО): флаг только КОНКРЕТНЫЙ, source-grounded дрейф между `CONTEXT_ONLY` предшествующим окном и `AUDIT_PAIRS` (тот же speaker внезапно меняет регистр без source-триггера). Суждение ТОЛЬКО в пределах окна; нельзя выводить chapter-wide inconsistency. Одиночный выбор регистра — не finding.
- 19·cross-chunk seam / terminology consistency (СМЕЖНОЕ + entity facts): флаг несогласованный рендеринг одного и того же source-токена/термина/обращения (а) против `CONTEXT_ONLY` предшествующего окна и (б) против canonical-рендеринга в `CHAPTER ENTITY FACTS`. Дрейф между НЕсмежными чанками вне видимости — вне scope (см. Risks). Детерминированный glossary-B9 — отдельный слой, не дублируется.
- 19·dialogue naturalness (translationese only): флаг только translationese/поломку голоса персонажа в пределах `AUDIT_PAIRS`, где теряется смысл/отношение. Регистр/диалект/«could be more natural» — не finding.
- 19·loss of ambiguity / flattening: флаг только когда двусмысленность/ирония, ВИДИМАЯ в исходном source окна, КОНКРЕТНО сплющена в одно прочтение, меняющее смысл. «Плоскость» без смены смысла — не finding. Двусмысленность, зависящая от контекста шире окна — вне scope.
- Все 4 проверки наследуют RULE 3 (evidence priority) и RULE 14 (DO NOT OVER-POLICE STYLE); findings репортятся под СУЩЕСТВУЮЩИЕ 6 категорий (typically `changed_fact`/`referent`), новых категорий не вводится (валидатор fail-closed отвергает чужие).

**D3. Явный restate правила 13 в конце новой секции:** «do NOT over-police register/style; новые правила — consistency/meaning с SOURCE-evidence, не приглашение переписывать в „более литературное“; стилистическая вариация → PASS».

**D4. prompt_version bump** (v4.1 → v4.2-lenses), раздельно от harness_version. Смена входит в audit identity (как уже сделано для prompt/harness version в B1).

**D5. Regression-контракты:** B1 §6 gold suite (8 must-find + 6 must-not-find) должен удержаться ПОСЛЕ изменения (никаких новых FP на p00106 Десяти / p00151 тётей / p00309 он=кот и т.п.). Добавляются 2–3 literary must-not-find кейса (стилистическая вариация/регистр → PASS), чтобы зафиксировать отсутствие стилевого over-policing.

## Proposed prompt delta (реализовано — финальный текст)

Добавляется КАК ПРАВИЛО 19 в `QWEN_AUDIT_V4_1` (`pact_v4/runtime/prompts_runtime.py`),
перед секцией `OUTPUT`. В production-промпте правила уже нумерованы 1–18, поэтому
новая секция — 19 (без коллизий). Аудит почанковый: аудитор видит `AUDIT_PAIRS` +
предшествующее `CONTEXT_ONLY` окно + narrator/entity context + `CHAPTER ENTITY FACTS`,
НО НЕ весь chapter — правило привязано к видимому окну.

```
19. LITERARY CONSISTENCY CHECKS (semantic, not stylistic preference)

You audit a bounded WINDOW of pairs (AUDIT_PAIRS) with a short preceding
CONTEXT_ONLY window and the provided CHAPTER ENTITY FACTS / narrator context.
You do NOT see the whole chapter, so judge only what is visible in this window.
These rules catch the class where the translation is 'fine' but loses to a human
translation on voice, immersion, and consistency (LAIT 2026). They are SEMANTIC
consistency checks, not style-polishing. Every finding MUST cite concrete SOURCE
evidence (RULE 3 priority) and survive 'when uncertain, PASS'.

Voice/register continuity (LOCAL): within the audited window, a character or
narrator whose register/voice is set by the source (casual vs formal, blunt vs
polished) must stay consistent with the preceding CONTEXT_ONLY window. Flag only
CONCRETE, source-grounded drift between the provided preceding context and the
audited pairs (same speaker suddenly switches register without a source
trigger). You cannot see the full chapter; do NOT infer chapter-wide
inconsistency. A single-instance register choice is NOT a finding.

Cross-chunk seam / terminology consistency (ADJACENT + entity facts): the same
named entity, term, or address form must match (a) its rendering in the
preceding CONTEXT_ONLY window and (b) the canonical rendering in CHAPTER ENTITY
FACTS. Flag an inconsistent rendering of the same source token against either.
You cannot see non-adjacent chunks; drift between distant chunks is out of scope
here.

Dialogue naturalness (translationese only): flag dialogue that reads as machine
translationese or breaks the established character voice such that
meaning/relation is obscured. Do NOT flag register choice, dialect, or 'could be
more natural' -- those are NOT findings.

Loss of deliberate ambiguity / flattening: flag only when the source's deliberate
ambiguity, irony, or double meaning (visible in the audited source) is concretely
collapsed into a single reading that changes meaning. Stylistic 'flatness'
without meaning change is NOT a finding. Ambiguity that depends on broader
chapter context beyond this window is out of scope.

The DO NOT OVER-POLICE STYLE rule (RULE 14) still governs: do NOT over-police
register/style. The checks above are consistency/meaning checks with SOURCE
evidence, not an invitation to rewrite toward 'more literary'. When a difference
is stylistic preference rather than consistency/meaning loss -> PASS.

Report literary-consistency findings under the existing categories (typically
changed_fact or referent). Do not invent new categories.
```

Версия промпта: `pact-v4-reviewer-qwen-audit/v4.1` → `pact-v4-reviewer-qwen-audit/v4.2-lenses`
(поле `QWEN_AUDIT_V4_1.version` + `PROMPT_VERSION` в `pact_v4/audit/chunked_audit.py`).

## Risks / Trade-offs

- [Новые правила вернут FP, как раньше v4.2] → Mitigation: каждое правило bounded к «concrete, source-grounded, consistency/meaning»; regression suite B1 §6 + новые literary must-not-find контракты должны удержаться (6/6 negative + новые). fail-closed сохранён.
- [Рост выходных токенов Qwen] → Mitigation: в пределах `max_tokens=12000` (B1 §2); если превышение — RetryShrink по входу (уже есть).
- [Смена prompt_version инвалидирует audit cache/resume] → Mitigation: осознанно, как уже принято для prompt/harness version в B1; старые out-dir не resumable по audit — приемлемо.
- [Литературные линзы дублируют B9 glossary] → Mitigation: правило 17 — только semantic drift одного source-токена, детерминированный glossary-B9 остаётся отдельным слоем; не пересекаются.
- [Дрейф голоса/термина между НЕсмежными чанками не ловится почанковым аудитом] → Известное ограничение видимости, НЕ регрессия. Whole-chapter генерация (v4.1) уже минимизирует швы на этапе генерации; почанковый аудит ловит смежные швы (правило 17а) + несогласование с CHAPTER ENTITY FACTS (17б). Non-adjacent drift — кандидат на отдельную future-карточку (опциональный whole-chapter audit pass), вне scope этого change.

## Migration Plan

- Изменение локализовано в `render_qwen_audit_prompt` + bump `prompt_version`. Feature-flag не нужен. Rollback — revert commit.
- Реализация ТОЛЬКО в изолированном worktree (не main, не RT), через pact-dev → pact-rev (pact-pi-review, max 4 раунда) → pact-git-hygiene → аппрув владельца на merge. Merge ≠ deploy ≠ запуск пайплайна.

## Open Questions

- Точный набор literary must-not-find кейсов для regression suite (2–3) — уточняется при реализации карточки, на основе реальных глав 0001/0002.
- Нужен ли отдельный spec.md для capability `audit-qwen-prompt` — решается по `openspec validate` (опционален, как в v41-runtime-efficiency).
