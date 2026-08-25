---
name: pact-fidelity-lint
description: ALWAYS use when touching prompts, selection, audit, glossary, or entity code in Pact. Static lint for translation fidelity, entity consistency, glossary and hard audit filters on diffs without running pipeline.
---

# Pact Fidelity Lint

Dev-only static checks for translation quality per AGENTS.md Priority #1.

## When to use

When editing prompts, selection/audit logic, glossary, memory, or entity handling. No pipeline run needed.

## Checks (on git diff, no model calls)

```bash
bash .pi/skills/pact-fidelity-lint/scripts/lint.sh
# or
git diff origin/main...HEAD -- pact_v4/
```

1. **Entity consistency** — entity extraction/mapping still deterministic, no silent glossary mutation
2. **Glossary/memory integrity** — no bulk overwrite, persistent memory format preserved
3. **Hard audit filters** — fail-closed behavior intact, no bypass of hard filters
4. **Prompt contracts** — JSON contracts, output format constraints preserved
5. **Determinism** — no non-deterministic ordering, no hidden model HTTP calls in model-free stages

## Output

- List of files violating fidelity invariants
- If clean: "fidelity-lint PASS (static, no pipeline)"

## Note

Green JSON status is not proof of quality — this lint does not replace pipeline audit, it catches 80% regressions statically on media host.
