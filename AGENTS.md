# Pact Translator Agent Instructions

## Goals

Use this priority order:

1. translation quality and integrity;
2. reliability of the fully automated pipeline;
3. development and execution speed;
4. economical use of Codex tokens.

Do not spend context restating established state. Read only the files needed
for the current task.

## Repository roles

Production repository and worktree:

`D:\pact\pact_translator_v3`

Development worktrees:

`D:\pact\pact_translator_worktrees\`

GitHub repository: `rt-tom/pact-ensemble-translator`

Stable branch: `main`

The production worktree is used only to:

- run the stable pipeline;
- perform read-only investigation;
- perform guarded deployment of an already reviewed release.

All development must happen on a separate branch **and** in a separate Git
worktree. Never edit production source files directly during development.

## Production safety

- Never modify tracked files in the production worktree while a pipeline run
  is active.
- Never stop `llama-server` or pipeline processes unless explicitly authorized.
- Never use `-Reset`, `-RedoTranslation`, `-RedoQuality`, or force flags unless
  each destructive operation is explicitly authorized. The short commands
  defined below do not grant that authorization.
- Never delete, rewrite, fabricate, or invalidate run artifacts or caches as a
  shortcut.
- Never change tuned Qwen/Gemma profiles, temperature, context size, `top_p`,
  `top_k`, thinking, or model selection without benchmark evidence and an
  explicit reason tied to the incident.
- Never analyze chapter 60 translation quality in this development workspace.
- Never manually edit source or translated book text.
- Never silently modify the glossary, book bible, or persistent book memory.

## Source of truth

Use this priority order:

1. Active installed production files.
2. Git commit, branch, tag, and diff.
3. Generated config for the current run.
4. Run artifacts and caches.
5. Tests.
6. Documentation.
7. Patch markers and installer messages.

A patch marker or successful installer message is not proof that code was
installed. Compare the active code.

## Token-efficient working mode

By default:

- do not retell project history;
- do not reread the entire repository;
- do not open documentation unrelated to the task;
- do not print complete diffs or logs;
- do not repeat unchanged HEADs, paths, settings, or cache counts;
- do not narrate every command or keep a step-by-step diary;
- do not create a review packet for a `LOW RISK` PR;
- reuse facts already established in the current Codex task and Git history.

Open these first:

1. the traceback or specific artifact;
2. the directly implicated file;
3. its direct downstream consumers;
4. tests for that lifecycle.

Expand the investigation only for a concrete reason. Produce a full diff,
detailed logs, or a full lifecycle report only when the user asks, the PR is
`REVIEW REQUIRED`, the cause is ambiguous, validation fails, or data loss is
possible.

Summarize commands and files. In responses, cite paths and relevant lines
instead of reproducing large files.

## Short command: `Вот ошибка`

When the user writes `Вот ошибка` and provides a traceback, log, screenshot, or
failure description, that authorizes the complete safe incident workflow below
through creation or update of a draft pull request. The user does not need to
separately request investigation, cache inspection, a branch/worktree, the fix,
tests, commit, push, or draft PR.

This command does **not** authorize merge, tagging, production deployment,
pipeline execution, destructive flags, cache invalidation, or stopping a
running process.

## Incident workflow

### Phase A: inspect

Inspect, as applicable:

- active production code;
- production Git HEAD and status;
- generated run config and stage settings;
- run state, logs, caches, and aggregate artifacts;
- process state;
- relevant commits and tags;
- downstream consumers of affected fields.

Determine:

1. immediate root cause;
2. systemic root cause;
3. reproducibility;
4. whether run data is intact;
5. what is already cached;
6. whether plain resume is safe;
7. which artifacts must not be invalidated;
8. whether the failure will recur without a fix;
9. the smallest safe fix scope.

Before changing any enum, decision, confidence, category, scope, target span,
required invariant, detector family, issue identity, status, coverage, cache
field, completion/finalization field, formatting incident, or policy outcome,
trace its actual downstream consumers. Inspect creation, merge, verification,
uncertain resolution, repair, gates, postcheck, finalization, monitor, and
resume/cache reuse only where the value really flows. Do not stop at the
producer of the value, but do not expand the trace without evidence.

Incident inspection must not be used to analyze chapter 60 translation quality.
Inspect only the code, structure, state, and metadata required to diagnose the
software failure.

### Phase B: decide whether to continue

If there is one clearly safest minimal fix, continue autonomously through a
draft PR.

Stop before implementation and ask one precise question only when:

- multiple materially different safe policies exist;
- model tuning would change;
- significant cache loss or invalidation may be required;
- translation quality could change ambiguously;
- downstream consequences cannot be established;
- a running production process would need to be stopped;
- run data could be damaged;
- product behavior requires a user choice.

Do not ask for confirmation for a mechanical, unambiguous, reversible bug fix.

### Phase C: branch and worktree

For every incident:

1. start from current `main`;
2. create a dedicated branch;
3. create a dedicated worktree;
4. leave the production worktree unchanged.

Use `hotfix/<version-or-incident>` for a failure blocking the current production
run. Otherwise use `fix/<short-description>`.

When another unmerged PR exists:

- Start a new independent incident branch from current `main` by default.
- Do not base a new branch on an unmerged PR unless the new change explicitly
  depends on that PR.
- Report any file overlap with open PRs.
- Before merging, refresh the branch against latest `main` and rerun all tests.
- Never combine unrelated fixes into one PR merely because they are pending at
  the same time.
- Each PR requires its own approval unless the user explicitly approves
  multiple named PRs together.

### Phase D: implementation

- Implement the smallest complete fix.
- Do not refactor adjacent code without necessity.
- Do not change unrelated files or model/profile settings.
- Preserve caches, cache identity, and run artifacts unless an approved design
  explicitly requires otherwise.
- Do not create artificial cache records.
- Do not use Reset, Redo, or force flags.
- Do not run the production pipeline.
- Do not accept a model response merely because a JSON status appears green.
- Add a regression test reproducing the actual failure mode for every bug fix.

### Phase E: validation

Run the relevant checks, not every possible check without cause. Normally:

- Python compilation for changed files;
- targeted regression tests;
- the relevant offline self-test;
- PowerShell AST syntax checks for a changed runner;
- `git diff --check`;
- exact changed-file scope checks.

Add only when needed:

- cache reuse and resume tests;
- lifecycle and validation-failure tests;
- production cache hashes;
- full integration tests.

Do not run a model or the production pipeline for offline validation.

Verify that:

- production HEAD and tracked tree did not change;
- run artifacts did not change;
- existing cache hashes did not change;
- all edits are confined to the development worktree;
- the diff does not alter unapproved model/profile settings.

### Phase F: commit, push, and draft PR

After validation:

1. stage only intended files;
2. create a small logical commit;
3. push the incident branch;
4. create or update a draft PR to `main`;
5. include root cause, policy, changed files, tests, and cache/resume impact.

Do not merge, tag, deploy, or run the pipeline during this phase.

## PR risk classification

Every PR must be classified as `LOW RISK` or `REVIEW REQUIRED`. When uncertain,
use `REVIEW REQUIRED`.

### LOW RISK

Allowed only when all of these are true:

- the defect and fix are narrow and unambiguous;
- semantic, merge/deduplication, issue equivalence, verification, uncertain,
  repair routing/scope, gate, finalization, and completion policies do not
  change;
- cache invalidation is unnecessary;
- model tuning and substantive prompt meaning do not change;
- run artifacts remain intact;
- regression tests exist and relevant tests pass;
- ordinary Git deployment provides rollback;
- no more than five production files change unless separately justified.

### REVIEW REQUIRED

Required if any change affects:

- merge, deduplication, issue identity, or equivalence;
- independent detector agreement or verification decisions;
- uncertain policy, repair scope, or repair lifecycle;
- candidate acceptance;
- semantic, Russian, deterministic, post-repair, or residual gates;
- finalization, coverage, or complete/failed/quarantined status;
- formatting integrity;
- cache identity or invalidation;
- persistent glossary or book memory;
- substantive prompt semantics or model/profile settings;
- more than five production files;
- multiple defensible policies, possible data recomputation/loss, or unknown
  downstream consequences.

For `REVIEW REQUIRED`, include in the PR body:

- root cause and chosen policy;
- downstream lifecycle trace and behavioral matrix;
- alternatives considered and rejected;
- changed files;
- cache/resume impact;
- tests and remaining risks;
- one exact review question.

Create `docs/reviews/PR-XXXX-REVIEW_PACKET.md` only when the PR body is
insufficient or the user explicitly asks for a separate review packet.

Do not ask the user to relay intermediate messages between assistants.

## Compact incident report

After `Вот ошибка`, return one compact report rather than a long action diary:

```text
PR: #N — URL
Risk: LOW RISK / REVIEW REQUIRED
Cause: 1-3 sentences
Fix: 1-3 sentences
Files: list or count
Tests: PASS / FAIL
Caches: preserved / invalidated
Production: unchanged
Resume: safe / blocked
Decision needed: none / one precise question
```

Do not repeat the full Git status, every SHA, unchanged settings, a long diff,
or full test logs. Keep those details locally or in the PR and show them on
request.

## Short command: `Утвержден` / `PR утвержден`

When the user writes `Утвержден`, `PR утвержден`, or `PR #N утвержден`, the
named or current open draft PR has received the required external approval.
This authorizes the complete release and guarded deployment workflow below
without separate requests for ready-for-review, merge, tag, backup, cache
hashing, production update, or offline tests.

If multiple plausible PRs are open, ask one short question listing their
numbers. Approval does not authorize running or resuming the pipeline.

## Approved PR release and guarded deployment

### Phase A: pre-merge validation

Before merge, verify:

- the intended PR, branch, reviewed HEAD, and diff scope;
- PR state is clean and mergeable;
- there are no unreviewed commits;
- relevant tests passed;
- production is still on the expected previous release;
- production tracked tree is clean;
- pipeline process state.

If the pipeline is running, do not stop it. Merge and tagging may proceed, but
do not deploy. Report that deployment is deferred until the run stops.

If the diff changed materially after review, do not merge. Reclassify it as
`REVIEW REQUIRED` and report the exact difference.

### Phase B: merge

1. Mark the PR ready for review.
2. Prefer squash merge into `main` unless a merge commit is specifically
   justified.
3. Use a concise release-oriented commit message.
4. Verify the new `main` SHA and PR `MERGED` state.

### Phase C: release tag

1. Read the release version from reviewed code and PR.
2. Create an annotated version tag on the merged `main` commit.
3. Use a concise tag message describing the fix.
4. Push and verify the tag target.

### Phase D: guarded deployment preflight

Before changing production:

1. verify that the pipeline is not running;
2. verify expected old production HEAD and exact tag target;
3. verify a clean tracked tree and exact release changed-file diff;
4. back up every changed tracked file;
5. save old HEAD, new tag SHA, and diff manifest;
6. save a manifest only for caches actually affected by the release or critical
   for resume, containing relative path, size, SHA-256, and `LastWriteTimeUtc`;
7. verify the expected cache count when known.

If the cache count changed unexpectedly, stop before deployment.

Do not add, delete, move, rename, or modify untracked legacy files. Their
presence is not a failure of the clean tracked-tree check.

### Phase E: deployment

Update production only by fast-forwarding to the exact reviewed tag:

```powershell
git merge --ff-only <tag>
```

Do not use reset, destructive checkout, force, manual copying of tracked source
files, or an old ZIP patch over a Git release.

### Phase F: post-deploy validation

After deployment:

1. verify exact production HEAD, clean tracked tree, and changed-file scope;
2. run relevant Python compilation, offline self-tests, targeted regression
   tests, PowerShell AST checks, and `git diff --check` where applicable;
3. read actual version strings and stage settings;
4. compare model/profile settings before and after;
5. recompute the cache manifest and compare path, size, SHA-256, and
   `LastWriteTimeUtc` entry by entry.

Treat any unplanned difference as a deployment failure. Do not run the pipeline.
Do not perform an automatic destructive rollback. Report the exact failure,
safe rollback commit/tag, and backup path.

### Phase G: compact release report

```text
PR: #N merged
Tag:
Production: old SHA -> new SHA
Backup:
Tests: PASS / FAIL
Caches: unchanged / exact difference
Deployment: PASS / FAILED
Resume: READY / BLOCKED
```

Do not print the full deployment log when deployment passes.

## Short command: `Запускай` / `Возобновляй`

After a successful deployment, `Запускай` or `Возобновляй` authorizes starting
the production pipeline with the ordinary resume command.

Before starting:

1. verify production HEAD/tag and clean tracked tree;
2. verify that the latest deployment validation passed;
3. verify cache preservation;
4. use the normal resume command without Reset, Redo, or force flags;
5. do not delete existing artifacts;
6. state the exact command;
7. start only when the user explicitly instructed Codex to do so;
8. inspect initial output and confirm cache reuse and continuation from the
   first uncached item.

If the user only asks how to start, show the command without executing it.

After startup, report briefly:

- the exact command;
- version and stage;
- whether cache reuse was confirmed;
- the first actually unfinished item.

Do not keep the Codex task open to monitor a long-running pipeline unless the
user explicitly asks for ongoing monitoring.

## Working style

- Inspect before editing.
- Use separate branches and worktrees.
- Make small logical commits.
- Add regression tests for every bug fix.
- Show diff and test results before merge.
- Never push directly to `main`.
- Do not require separate confirmation for each mechanical step covered by the
  short commands above.
- Keep intermediate updates brief unless a blocking decision is required.

The normal interaction points are:

1. `Вот ошибка` — investigate through draft PR;
2. external review when required;
3. `Утвержден` — merge, tag, guarded deploy, and offline validation;
4. `Возобновляй` — start the production pipeline.

## Permanent safety rules

- Do not interfere with another process's `llama-server`.
- Reuse a server only when its ownership and matching profile are proven.
- Do not treat a partial cache as a completed stage.
- Do not skip a model call merely because individual issue caches exist.
- Do not accept truncated JSON.
- Do not treat a green JSON status as proof of text quality.
- Do not change tuned model settings to work around a software defect.
- Treat merge, deployment, and pipeline execution as three separate stages.

## Data restrictions

Do not commit:

- pipeline runs or other run data;
- book source text or translated chapters;
- model files;
- logs;
- secrets;
- backup archives.

The verified initial private baseline may contain existing local absolute paths
because it preserves the exact production state. Do not introduce new
machine-specific paths when a configurable local setting is practical. Future
refactors should move machine-specific paths into ignored local configuration
or committed example templates.
