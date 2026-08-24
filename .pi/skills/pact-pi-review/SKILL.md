---
name: pact-pi-review
description: Orchestrates pact-dev/pact-rev review workflow with 4-round limit and convergence check. Use after pact-dev completes implementation in isolated worktree.
---

# Pact Pi Review

Implements AGENTS.md "Pi implementation and review workflow" for Pact development.

## Workflow

For approved implementation work in one isolated branch/worktree:

1. **pact-dev** implements agreed scope (already done when you invoke this skill)
2. **Launch pact-rev** as independent subagent (read-only):
   ```bash
   # pact-rev must inspect diff independently, not trust pact-dev summary
   git diff origin/main...HEAD --stat
   git diff origin/main...HEAD
   ```
3. **Verdict** — pact-rev returns EXACTLY one of:
   - `APPROVED`
   - `REQUEST_CHANGES` with numbered actionable findings
4. **Rounds** — Maximum 4 automatic rounds. On REQUEST_CHANGES:
   - return findings to SAME pact-dev and SAME worktree
   - do NOT create separate fix task/worktree
   - fix same implementation, run fresh independent review
5. **Stop conditions**:
   - APPROVED -> run openspec verify if OpenSpec change, report READY FOR USER REVIEW
   - 4 rounds exhausted with remaining findings -> STOP, report unresolved findings to owner, never silently accept
6. **Convergence** — pact-rev must verify declared scope/correctness, must NOT add new requirements each round. If scope expansion detected -> Architect consolidates findings into single list, one fix iteration.

## Rules

- pact-rev must inspect independently, never edit implementation files
- Escalate blocked decisions/ambiguous requirements to Architect/owner
- Report changed files and checks run on completion
