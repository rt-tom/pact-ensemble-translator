---
name: pact-git-hygiene
description: ALWAYS use before commit or PR in Pact development. Enforces focused diffs and clean git history to keep reviews reviewable — mandatory pre-commit check.
---

# Pact Git Hygiene

Implements AGENTS.md "Git, merge, deployment" dev subset.

## Checks (before commit/PR)

```bash
./scripts/check.sh
# manual:
git diff --stat
git diff origin/main...HEAD --stat
```

1. **Focused diff** — no mixed refactors, formatting churn, or unrelated fixes in one change
2. **Commit hygiene** — one logical change per commit, no secrets/tokens in messages
3. **Data boundaries** — no source text, translations, prompts, logs, artifacts, credentials committed
4. **Merge/deploy separation** — never claim merge==deploy, never auto-archive OpenSpec, never start pipeline (owner-only on RT)

## On violation

Split the change, clean the diff, re-run pact-risk-test narrow suite.

## Reference

AGENTS.md: "Keep diffs focused", "Never merge/deploy/archive without owner approval", "Data and external boundaries".
