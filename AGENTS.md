# Pact Translator Agent Instructions

## Priorities

1. Translation quality and integrity.
2. Preserve runs, caches, and production state.
3. Fast, simple development with economical token use.

This is a home project. Prefer the smallest reliable workflow; do not add enterprise ceremony without a concrete risk.

## Repository and safety

- Production: `D:\pact\pact_translator_v3`
- Development worktrees: `D:\pact\pact_translator_worktrees\` or `%LOCALAPPDATA%\Temp\vibe-kanban\worktrees\` (Vibe Kanban-managed)
- Stable branch: `main`
- Develop only in a separate branch/worktree. Never edit tracked production files directly.
- Do not stop a pipeline or `llama-server` without explicit authorization.
- Do not use Reset, RedoTranslation, RedoQuality, force, destructive checkout, cache deletion, or fabricated artifacts without explicit approval.
- Do not silently modify book text, glossary, book bible, or persistent memory.
- Do not change tuned Qwen/Gemma settings without explicit reason and benchmark evidence.
- Do not attach to or stop a foreign `llama-server`.
- Do not run production pipeline during development/testing.
- Treat a mismatch between production `HEAD` and `deployment_provenance.v31.json` as release drift: stop deployment work, preserve the active tree/runs, and reconcile through a reviewed release path. Never repair drift with reset, force, destructive checkout, or manual replacement of tracked files.
- Keep V3 production releases and unfinished V4 development in separate worktrees. Do not use a `main` tag for V3 deployment when it contains unapproved V4 runtime/schema changes.

## Source of truth

1. Active production code.
2. Git commit/tag/branch/diff.
3. Generated run config.
4. Run artifacts and caches.
5. Tests.
6. `DECISIONS.md` — architectural decisions and their rationale.
7. Documentation.
8. Patch markers and installer messages.


Do not trust a marker or success message without checking active code.

## Token-efficient behavior

- Start with the reported artifact/traceback, implicated code, immediate consumers, and relevant tests.
- Do not scan the whole repository, print full logs/diffs/status, narrate commands, or repeat unchanged facts.
- Run targeted tests by default. Expand only if root cause is unclear, data may be lost, lifecycle/cache behavior changes, or targeted tests fail.
- Git and PRs are shared memory. Do not require the user to relay long summaries.

## Short commands

### `Проверь`

Read-only inspection. Do not edit files, create a PR, or stop processes unless separately asked.

### `Вот ошибка` / `Ошибка`

Investigate production read-only; determine root cause and resume safety; then create a minimal separate-worktree fix with regression test and draft PR. Do not merge, deploy, or start pipeline.

### `Сделай` / `Реализуй`

Implement in a separate worktree and create a draft PR. Use the named target branch. If omitted: production incident -> `main`; release-development task -> current development branch.

### `Forward-port`

Create/update a separate PR adapting an approved production fix to the development branch. Do not deploy or merge unless also approved.

### `Утвержден` / `PR #N утвержден`

Apply the target-aware approved-PR workflow below. If multiple plausible PRs are open, ask one short question listing numbers.

### `Deploy отложить`

Merge may proceed, but production deployment is forbidden until explicitly requested later.

### `Запускай` / `Возобновляй`

Start the normal production pipeline only after successful deployment checks.

## Incident workflow

1. Determine root cause, run/cache preservation, recurrence risk, plain-resume safety, and whether a fix already exists elsewhere.
2. Trace downstream consumers only when changing lifecycle fields: decision, confidence, repair scope, issue identity, cache identity, gate or terminal status.
3. Ask one precise question only if multiple safe policies exist, model tuning changes, cache invalidation is needed, a running process must stop, or data may be damaged.
4. Otherwise make the smallest complete change in a separate worktree, add a regression test, run targeted validation, commit, push, and open a draft PR.

Never refactor unrelated code in an incident fix.

When a change reverses a prior decision, abandons a branch, or resolves a non-obvious tradeoff, append a dated entry to `DECISIONS.md` in the same commit. A one-line "what and why" is sufficient. Default `git revert` messages are not acceptable as the sole record.

## Open PRs and parallel agents

- Independent fixes use independent branches from the current target branch.
- Do not base a fix on an unmerged PR unless it truly depends on it.
- Before merge, refresh against the target when needed and rerun relevant tests.
- Codex and Claude Code may work in parallel only in distinct branches/worktrees.
- For handoff, push a checkpoint commit; the next agent fetches and continues with a new commit.

## Risk classification

Use `LOW RISK` only for a narrow, unambiguous implementation defect that does not alter translation semantics, issue merging, verification, repair lifecycle, gates, terminal policy, cache identity/invalidation, persistent memory, or model/prompt policy. A regression test is required.

Use `REVIEW REQUIRED` for changes to those areas, data recomputation, multiple defensible policies, uncertain downstream effects, or large scope.

For `REVIEW REQUIRED`, PR body needs: cause, policy, affected lifecycle, changed files, cache/resume impact, tests, and one review question. Do not create a separate review packet unless requested.

## Approved PR workflow

### PR targeting a development branch

Refresh if needed, run relevant tests, and merge. Do not tag, deploy, or run pipeline.

### Documentation-only PR targeting `main`

Run lightweight validation and merge. No production tag/deploy unless explicitly needed by production tooling.

### Production-code PR targeting `main`

Confirm reviewed diff has not materially changed, tests pass, and whether pipeline is active. Merge may proceed while pipeline runs, but deployment must be deferred. If deployable and safe, tag/deploy only when pipeline is stopped and deployment is not deferred.

## Lightweight guarded deployment

Before deployment: verify exact tag target, expected production HEAD, clean tracked tree, stopped pipeline, changed files, and create a backup. Also verify that `deployment_provenance.v31.json` exists, its tag resolves to its recorded commit, and that commit equals active production `HEAD`; otherwise classify the state as release drift and stop.

Hash caches only if the change affects cache/resume/repair/terminal artifacts, the user asks, or the cache is known fragile.

Fast-forward production to the exact reviewed tag. Never use reset, force, ZIP overlays, or manual copying of tracked files.

After deployment: confirm production HEAD/clean tree, run relevant offline tests, confirm active version values, and check affected run/cache data when applicable. Do not start pipeline automatically.

## Pipeline start

Before `Запускай` / `Возобновляй`: confirm production tag/HEAD, clean tracked tree, successful last deployment, and no destructive flags. Use normal resume.

After start, report only command, version, current stage, reuse/clean-start, and first unfinished item when relevant.

## Compact reports

### Incident/draft PR

```text
PR:
Risk:
Cause:
Fix:
Files:
Tests:
Caches:
Production:
Resume:
Decision needed:
```

### Approved merge/deployment

```text
PR:
Target:
Merge:
Tag:
Production:
Tests:
Caches:
Deployment:
Resume:
```

Omit irrelevant fields. Do not output full logs or repeated Git state unless requested.

## Permanent pipeline rules

- Partial per-item cache is not a completed stage.
- Skip a model only when a completed authoritative aggregate permits reuse.
- Never accept truncated JSON.
- Green JSON status is not proof of translation quality.
- Diagnostic monitor metrics never determine authoritative status/resume.
- Model-free stages must not make hidden model HTTP calls.
- Foreign servers must not be reused or stopped.
- Merge, deployment, and pipeline execution are separate actions.

## Data restrictions

Do not commit pipeline runs, source/translated chapters, models, logs, secrets, or backups. Existing absolute paths in the historical private baseline may remain, but do not introduce new machine-specific paths when local config/templates are practical.
