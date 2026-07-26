# Pact Translator Agent Instructions

## Production safety

- Never modify files in an active production worktree while a pipeline run is active.
- Never stop llama-server or pipeline processes unless explicitly authorized.
- Never use -Reset, -RedoTranslation, or -RedoQuality without explicit approval.
- Never change tuned Qwen/Gemma model profiles without benchmark evidence.
- Never analyze chapter 60 translation quality in this development workspace.

## Source of truth

1. Active installed files
2. Git commit and diff
3. Generated run config
4. Run artifacts
5. Documentation

Patch markers are not proof that code was installed.

## Required workflow

- Work on a feature branch or separate worktree.
- Inspect before editing.
- Make small logical commits.
- Add regression tests for every bug fix.
- Run compilation, unit tests, self-tests, and relevant integration tests.
- Show the diff and test results before merging.
- Do not push directly to main.
- Do not silently modify glossary or book memory.

## Data restrictions

Do not commit:
- pipeline runs
- book source text
- translated chapters
- model files
- logs
- secrets
- The verified initial private baseline may contain existing local absolute
  paths because it must preserve the exact production state.
- Do not introduce new machine-specific paths when a configurable local
  setting is practical.
- Future refactors should move machine-specific paths into ignored local
  configuration or committed example templates.
- backup archives
