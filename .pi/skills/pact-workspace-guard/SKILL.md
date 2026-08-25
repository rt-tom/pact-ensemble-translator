---
name: pact-workspace-guard
description: ALWAYS use before any edit, write, or bash that modifies code, config, or artifacts in Pact Ensemble Translator. Enforces isolated branch/worktree safety on media host and blocks production edits.
---

# Pact Workspace Guard

Dev-only safety checks for Pact Ensemble Translator. Enforces AGENTS.md "Production and workspace safety" on media host.

## When to use

MANDATORY before ANY edit/write/bash that touches code, config, or artifacts. The agent MUST run this check automatically without user request. Run as pre-flight check.

## Checks

Run the script from repo root:

```bash
bash .pi/skills/pact-workspace-guard/scripts/check.sh
# or
bash /home/rt/projects/pact-ensemble-translator/.pi/skills/pact-workspace-guard/scripts/check.sh
```

It verifies:

1. **Isolated branch/worktree** — current branch != main, worktree path == /home/rt/projects/pact-worktrees/<change-name> or /home/rt/projects/pact-ensemble-translator on main only for docs
2. **Never edit production** — D:\pact\pact_translator_v4_1 not in any staged diff, no absolute RT paths introduced
3. **No destructive git** — no reset --hard, force push, or bulk artifact deletion without owner approval
4. **No model server ops** — no llama-server start/stop/kill in scope

## On failure

STOP and report to Architect. Do not proceed with edits. Ask owner for explicit approval if production artifact change is truly required.

## Reference

See AGENTS.md sections: "Environments and shells", "Production and workspace safety".
