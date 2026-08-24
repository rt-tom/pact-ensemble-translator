Pact Ensemble Translator — Agent Guide

## Priorities

1. Translation quality, fidelity, and production-state safety outrank speed.
2. Make the smallest correct, reviewable change.
3. Prefer targeted inspection and focused checks over repository-wide scans.
4. Be economical with tokens: reuse context, avoid repeated discovery, and report concise evidence.

## Environments and shells

Agent/development host: Debian `media`.

```text
Repository: /home/rt/projects/pact-ensemble-translator
Worktrees: /home/rt/projects/pact-worktrees/<change-name>
```

Production/execution host: Windows `RT`.

```text
Production checkout: D:\\pact\\pact_translator_v4_1
```

- Commands executed by agents on `media` use Bash/Linux syntax.
- Commands prepared for the owner to run on `RT` use PowerShell/Windows syntax.
- Do not assume paths, environments, model servers, or caches exist on both hosts.

## Production and workspace safety

- Develop only in an isolated branch/worktree on `media`.
- Never edit tracked production files directly on `RT`.
- Never overwrite, delete, migrate, or bulk-rebuild production artifacts, caches,
  snapshots, output books, or persistent data without explicit owner approval.
- Do not change model settings, runtime routing, provider endpoints, or lifecycle
  policy unless the approved change explicitly requires it.
- Do not start, stop, kill, or reconfigure foreign/local Llama or model servers
  unless explicitly authorized and in scope.
- A clean git diff is not proof that runtime state is safe; inspect relevant
  artifacts and configuration before making a claim.

## Sources of truth

Resolve conflicts in this order unless an approved change says otherwise:

1. Actual production code and runtime behavior.
2. Git history and the relevant branch diff.
3. Active runtime configuration and immutable run artifacts.
4. Tests and reproducible checks.
5. `DECISIONS.md` for recorded architectural rationale.
6. Other documentation.

For an active approved OpenSpec change, its proposal/specs define intended behavior and acceptance criteria; `design.md` defines the approved approach; and `tasks.md` defines the agreed scope. If code or tests conflict with an approved requirement, surface the discrepancy to the Architect—do not silently reinterpret it.

## Requirements and planning

- The main Pi session is the Architect and owns scope, sequencing, and owner communication.
- Use `grill-me`/grilling only for meaningful requirements, behavior, UX, architecture, or policy ambiguity—not obvious or fully specified work.
- Use OpenSpec for substantial changes: new behavior, non-trivial design,
  cross-module changes, runtime/model policy, data-format changes, or production
  risk. Small, clearly scoped fixes may proceed without it.
- Obtain owner approval for the OpenSpec proposal before implementation when the
  change materially affects behavior, data, runtime, or production risk.

## Pi implementation and review workflow

For approved implementation work:

1. `pact-dev` implements the agreed scope in one isolated branch/worktree.
2. `pact-rev` independently reviews the actual diff and relevant code read-only.
3. `pact-rev` returns exactly `APPROVED` or `REQUEST_CHANGES` with numbered,
   actionable findings.
4. Maximum automatic review rounds: 4.
5. On `REQUEST_CHANGES`, return findings to the same `pact-dev` and worktree.
   Do not create a separate fix task or worktree merely for another round.
6. After `REQUEST_CHANGES`:
   - return actionable findings to the same implementation agent;
   - fix the same implementation;
   - run a fresh independent review.
7. Stop early on `APPROVED`.
8. At the limit:
   - never silently accept remaining findings;
   - report the unresolved findings to the owner.
9. When approved, run the applicable OpenSpec verification for OpenSpec changes.
10. Report `READY FOR USER REVIEW`, including changed files and checks run.

- `pact-rev` must inspect independently; it must not treat `pact-dev`'s summary
  as evidence and must never edit implementation files.
- **Review convergence:** `pact-rev` verifies the declared scope and correctness
  of the implementation/findings and must not add new requirements each round.
  If `pact-rev` starts expanding scope instead of verifying previous findings,
  the Architect stops the cycle, consolidates findings into a single list, and
  allows one fix iteration (preserved from former `AGENTS.md` analytical-task
  convergence rule 2026-08-07).
- Escalate blocked decisions, ambiguous requirements, or material disagreement to
  the Architect/owner; do not invent product policy.
- Historical Hermes/Vibe Kanban procedures are not active workflow rules. Consult
  `AGENTS_REFERENCE_RU.md` only for historical context when it is relevant.

## Mandatory dev skills (auto, no user prompt)

All agents (Architect, `pact-dev`, `pact-rev`) MUST auto-load these project skills when relevant — do not wait for `/skill:` from user:

- Before any edit/write/bash: `pact-workspace-guard` (isolated worktree, no RT production edit)
- Before/after code change: `pact-risk-test` (Low/Medium/High + narrowest tests)
- When touching prompts/selection/audit/glossary/entity: `pact-fidelity-lint` (static, no pipeline)
- Before commit/PR: `pact-git-hygiene` (focused diff, no secrets)
- Starting substantial change: `pact-openspec-dev` (+ `grill-me` if ambiguous)
- After implementation: `pact-pi-review` (pact-dev→pact-rev, max 4 rounds, convergence)

Skills are in `.pi/skills/pact-*/SKILL.md` — load via `read` when their trigger matches.

## Risk and testing

Classify work before editing:

- **Low:** isolated docs, tests, or mechanical refactors with no runtime impact.
- **Medium:** localized behavior, prompts, selection/audit logic, or config changes.
- **High:** pipeline orchestration, model lifecycle/routing, source and output
  contracts, persistent data, caches, resume/journal behavior, or production runs.

- For medium/high risk, inspect callers, contracts, failure paths, and affected
  tests before changing code.
- Run the narrowest relevant checks first; expand only when risk or failures
  justify it. Do not claim tests passed unless they were actually run.
- Preserve determinism, resumability, auditability, and backward compatibility
  where the affected contract requires them.
- Treat translation fidelity, entity consistency, glossary/memory integrity, and
  hard audit filters as functional requirements, not cosmetic concerns.

## Git, merge, deployment, and pipeline execution

- Keep diffs focused. Do not mix refactors, formatting churn, or unrelated fixes
  into a change.
- Never merge, deploy, archive an OpenSpec change, or start any pipeline without
  explicit owner approval.
- Merge ≠ deployment ≠ pipeline execution. Each is a separate owner decision.
- The production pipeline is manual-only: the owner starts it on `RT` from the
  production checkout. Agents may prepare commands, inspect results, and advise,
  but must not launch it.
- Before a requested production action, state the exact target checkout, config,
  input, intended output location, and irreversible effects.
- After every deploy, sync production checkout `D:\pact\pact_translator_v4_1` with `main` (`git pull --ff-only` on `RT`); deployment is not complete until `RT` reports `Already up to date` or the deployed commit.

## Data and external boundaries

- Do not upload source text, translations, prompts, logs, artifacts, credentials,
  or customer/book data to external services unless the owner explicitly approves
  the destination and scope.
- Never expose secrets, tokens, private endpoints, or personal data in commands,
  commit messages, reports, or logs.
- Do not fabricate run results, audit status, provenance, or completion claims.
- Preserve existing persistent-data formats and ownership boundaries; propose a
  migration and rollback path before any approved format-changing work.
