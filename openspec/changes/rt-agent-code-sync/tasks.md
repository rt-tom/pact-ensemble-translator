## Approval gate

- [x] 0.1 Owner reviews and approves proposal/spec/design before changing `AGENTS.md` or other policy/code.

## Implementation (after approval only)

- [x] 1.1 Replace the blanket RT tracked-file prohibition with a narrowly worded exception for explicit-request, code-only Git fast-forward sync through the existing `rt` SSH alias; retain prohibition on direct file edits.
- [x] 1.2 Document the exact allowed v4.1/v4.2 checkout-to-branch mapping, per-deploy authorization/reporting, preflight clean-tree/identity checks, pre-mutation expected-commit verification (record HEAD, fetch-only-target, resolve origin/<branch> to expected SHA, ancestor check), allowed commands, post-sync commit verification, and fail-closed conditions.
- [x] 1.3 Explicitly preserve all exclusions: no automatic deploy after merge; no pipeline, services/model servers, configuration, migrations, caches, outputs, or persistent data; no reset/clean/force/automatic rollback.
- [x] 1.4 Confirm `pact-workspace-guard` and other instructions do not contradict the approved RT-sync exception; adjust only if strictly necessary and test the relevant guard behavior without touching RT.

## Offline validation

- [x] 2.1 Add focused static/policy checks or a reviewed policy test matrix for allowed paths/branches, explicit-request gating, clean-tree/fast-forward-only constraints, pre-mutation expected-commit match/mismatch, and all exclusions.
- [x] 2.2 Run `openspec validate rt-agent-code-sync --strict` and `git diff --check`; run focused fidelity/security/git hygiene checks.
- [x] 2.3 Obtain independent `pact-rev` review. Do not connect to RT, sync a checkout, run a pipeline, alter credentials, merge, or deploy while implementing this policy change.
- [x] 2.4 Record that the policy grants no standing auto-deploy, `ssh rt` is lowercase and locally resolves in this session; do not test RT reachability during this policy change. Remote access preflight belongs to a separately authorized deploy.