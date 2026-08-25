## 1. Draft prompt delta (worktree, после аппрува change)

- [ ] 1.1 Добавить ПРАВИЛО 19 «LITERARY CONSISTENCY CHECKS» (4 проверки: voice/register continuity, cross-chunk seam, dialogue translationese, ambiguity flattening) в `QWEN_AUDIT_V4_1` (`pact_v4/runtime/prompts_runtime.py`), текст — из design.md «Proposed prompt delta» дословно; 18 старых правил без изменений — verify: `prompt_version` bump v4.1 → v4.2-lenses, compileall ok
- [ ] 1.2 Убедиться что каждое новое правило ссылается на правило 3 (SOURCE evidence) и restate правила 13 в конце секции — verify: grep по промпту, ручная проверка связности

## 2. Identity / versioning

- [ ] 2.1 Bump `prompt_version` раздельно от `harness_version`; смена входит в audit identity (как в B1) — verify: unit-тест на identity-изменение при смене prompt_version

## 3. Regression suite

- [ ] 3.1 Добавить 2–3 literary must-not-find кейса (стилистическая вариация / регистр → PASS) в gold suite B1 §6 — verify: suite 8/8 must-find + 6/6 (старые) + N/N (новые literary) negative rejection; pytest mock backend, 0 реальных вызовов
- [ ] 3.2 Прогнать существующий B1 regression suite (8 must-find + 6 must-not-find) после изменения — verify: 8/8 + 6/6 удержаны, НЕТ новых FP

## 4. Static + risk gates

- [ ] 4.1 `pact-fidelity-lint` (касается prompts/audit) — verify: static lint без нарушений
- [ ] 4.2 `pact-risk-test` классификация Medium (локализованное поведение промпта/аудита) → узкие тесты — verify: релевантные тесты зелёные

## 5. Review

- [ ] 5.1 pact-dev → pact-rev независимый ревью (pact-pi-review, max 4 раунда, convergence) — verify: APPROVED
- [ ] 5.2 `pact-git-hygiene` перед PR (focused diff, без секретов) — verify: `git diff --check` clean
- [ ] 5.3 `openspec validate --change v41-audit-literary-lenses` без ошибок — verify: validate green

## 6. Pre-merge (owner decision)

- [ ] 6.1 Аппрув владельца на merge — merge ≠ deploy ≠ запуск пайплайна (три отдельных решения)
