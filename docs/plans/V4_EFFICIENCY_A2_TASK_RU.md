# V4 Efficiency A2 — lazy balanced-only generation (один кандидат)

- План: `docs/plans/V4_EFFICIENCY_A_OPTIMIZATION_PLAN_RU.md` (ред. 2, §3)
- Статус: к реализации после A1 (I+RV, PR после approve)
- Масштаб: `pact_v4/phase2/generation.py` (`_roles_for_band`), `pact_v4/pipeline/v4_phase12_strict_runner.py` (Phase 2 loop), конфиг-флаг. Prompt templates/models без изменений.

## Логика

```
balanced_literary (1 вызов)
  → Qwen fidelity + deterministic
      passed  → selected (done; Gemma не зовём)
      failed  → lazy: gen fidelity_first (1) + его Qwen (1)
                один passed → selected; оба failed → quarantined (как сейчас)
```

- Gemma Russian preference: вызывается только при >1 passed-кандидате (в lazy-схеме практически не бывает) — 0–2 вызова вместо ~13.
- `fidelity_first` остаётся страховкой (lazy ветка) — покрывает кейсы run_005 chunk0010/0014 (fidelity-wins), если balanced не прошёл.
- Флаг: `efficiency.lazy_balanced` (env/ConfigArtifact), default `true`; `false` → легаси 2-кандидатное поведение (полный откат). identity_hash меняется при смене флага → resume инвалидируется корректно.
- `expected_roles`/`risk_band`/`generation_outcomes.json`/`selection_meta.json` — совместимы (роль кандидата сохраняется).

## Non-goals

- Никаких изменений гейт-семантики (Qwen fidelity/deterministic — как есть), каскада, промпт-шаблонов, риск-политики.
- reasoning остаётся 0.

## Приёмка

- Unit: `low→1 balanced`; `high+failed→lazy fidelity`; `high+passed→no lazy`; `оба failed→quarantined`; флаг `false` → старое поведение (2 кандидата + Gemma).
- Dry-run на run_005 артефактах: gen 32→~18, Qwen fidelity 32→~18, Gemma 13→0–2; суммарные вызовы ~365 (−10%).
- Валидационный прогон (владелец, вне чата): 0001 (+046). Контроль 2/14 fidelity-wins (chunk0010/0014): в `translations.json` diff против run_005 не должны деградировать; не должны уйти в quarantined/debt без причины.
- DECISIONS.md entry + merge (PR после approve).
