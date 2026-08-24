---
name: pact-openspec-dev
description: Guides OpenSpec proposal/specs/design/tasks creation and grill-me checks for Pact development. Use when starting substantial changes, new behavior, or cross-module work.
---

# Pact OpenSpec Dev

Dev-only planning gate for Pact Ensemble Translator per AGENTS.md "Requirements and planning".

## When to use

- Starting substantial change: new behavior, non-trivial design, cross-module, runtime/model policy, data-format
- Small clearly scoped fixes may skip — skill will tell you

## Workflow

1. **Scope check** — Is this substantial? If docs/tests/mechanical refactor only -> Low, may proceed without OpenSpec
2. **Grill check** — If requirements/behavior/UX/architecture ambiguous -> run grilling skill first (grill-me). Do not use grilling for obvious/fully specified work
3. **OpenSpec artifact check**:
   ```bash
   openspec list --json 2>/dev/null | head
   openspec status --change "<name>" --json 2>/dev/null | head
   ```
   - proposal.md defines intended behavior + acceptance criteria
   - specs/ defines requirements
   - design.md defines approved approach
   - tasks.md defines agreed scope
4. **Approval gate** — Owner approval required before implementation when change materially affects behavior/data/runtime/production risk. Do not silently reinterpret conflicts.

## Rules

- Main Pi session is Architect and owns scope/sequencing
- If code/tests conflict with approved requirement -> surface to Architect, do not reinterpret
