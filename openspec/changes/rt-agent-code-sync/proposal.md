## Why

The current guide prohibits agents from changing tracked production files on RT, so even an owner-approved, code-only `git pull --ff-only` deployment has to be run manually. This is tedious despite the owner having a working SSH alias. The policy should distinguish a tightly bounded Git fast-forward deployment from direct file edits, data operations, service lifecycle actions, and pipeline runs.

## What Changes

- Permit agents to perform **code-only Git syncs** on RT through the existing SSH alias `rt`, but only after an explicit owner request to deploy a specific change.
- Limit execution to the documented RT checkouts and their intended branches: `D:\pact\pact_translator_v4_1` on `main`, or `D:\pact\pact_translator_v4_2` on the specifically requested `dev/v4.2-*` branch.
- Require a clean worktree, exact checkout/branch/commit identification, fast-forward-only update, and post-sync commit verification. Stop without cleanup/reset/force if any precondition fails.
- Keep merge, deploy, and pipeline execution separate. This permission does not authorize pipeline runs, service restarts, migrations, configuration changes, direct file edits, or persistent-data operations.

## Non-goals

- No standing automatic deploy after merge; each deployment requires an explicit owner request.
- No change to the manual-only production pipeline policy.
- No RT access provisioning, credential handling, SSH configuration changes, or secret transfer.
- No service restart, model-server lifecycle, artifact/cache/output-book modification, migration, rollback, or direct source edit on RT.

## Capabilities

### New Capabilities
- `rt-agent-code-sync`: narrowly authorized agent-run fast-forward Git sync to a selected RT checkout.

## Approval boundary

High risk: this alters production deployment authority. This artifact is a proposal only. Do not edit `AGENTS.md`, change SSH access, connect to RT for deployment, merge, deploy, or run a pipeline until the owner approves the proposal/design. The implementation must preserve explicit per-deploy approval and fail-closed behavior.