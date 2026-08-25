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

## Economy mode

Keep the independent fresh-review model (it catches real defects a single agent misses),
but reduce token waste from per-round re-reading and one-finding-per-round churn:

- **Proactive hardening in the first dev brief.** Brief `pact-dev` to implement
  boundary/security hardening up front: reject symlinks (candidate/state/manifest/inbox
  dirs and files), validate `book_id`/`candidate_id` as safe single path components with
  containment, enforce the exact canonical file set, and assign revision ids on the
  authority side. This removes whole later rounds.
- **Batch findings into one fix pass.** After any review, hand `pact-dev` ALL outstanding
  findings at once (not one per round), in the same worktree.
- **Dev self-review before done.** Require `pact-dev` to re-read its own diff against the
  findings and spec and run the checks before reporting; the first independent review is
  then more often `APPROVED`.
- **Short Architect briefs.** Point to the verdict + files; do not restate file contents
  (the agent re-reads them).
- **Lean review.** Let `pact-rev` rely on the test suite + `openspec validate` and read
  only changed files; full re-reads of unchanged modules are the main cost.
- **Verify boundary coverage.** In review, confirm `pact-dev`'s coverage map enumerates
  EVERY layer (entry type + symlink chain through all ancestors + exact canonical file
  set + JSON/hash), the regular-file requirement is enforced, the identical pre-move
  re-validation is present, and the negative test matrix exists (extra top-level
  file/dir, symlink at each level, non-regular special file FIFO/socket/device, post-lock
  TOCTOU mutation). This is the cheapest way to catch the partial-boundary class of
  defects that otherwise burns extra rounds.
- **Do NOT** merge dev+rev into one agent, and do NOT drop review rounds in a way that
  sacrifices independence.
