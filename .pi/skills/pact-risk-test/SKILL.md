---
name: pact-risk-test
description: ALWAYS use before and after code changes in Pact development. Classifies risk Low/Medium/High and runs narrowest relevant tests first — mandatory for every code change.
---

# Pact Risk Test

Implements AGENTS.md "Risk and testing" for dev-only checks.

## Classification (before editing)

- **Low:** isolated docs, tests, mechanical refactors, no runtime impact
- **Medium:** localized behavior, prompts, selection/audit logic, config changes
- **High:** pipeline orchestration, model lifecycle/routing, source/output contracts, persistent data, caches, resume/journal

## Workflow

1. **Classify** the change
2. **Pre-change inspection** (for Medium/High):
   - inspect callers, contracts, failure paths, affected tests
   - check determinism, resumability, auditability, backward compat
3. **Test execution**:
   - Run narrowest relevant checks first
   - Expand only when risk or failures justify it
   - Do not claim tests passed unless actually run
   ```bash
   python -m pytest pact_v4/tests/test_<relevant> -q
   # or targeted suite per area
   ```
4. **Fidelity check** — treat translation fidelity, entity consistency, glossary/memory integrity, hard audit filters as functional requirements

## Reference

AGENTS.md "Risk and testing". No pipeline execution — dev checks only.
