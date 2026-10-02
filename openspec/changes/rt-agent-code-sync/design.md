## Decision frame

The guide currently says never edit tracked production files directly on RT, while separately requiring the owner to synchronize the production checkout after every deploy. The intended policy is a limited exception for a mechanical Git fast-forward only, not an authority to alter files or state directly. Owner decision: each deploy still needs an explicit deploy request; the permission is not automatic after merge. Available targets are only the documented v4.1 production and v4.2 development/test checkouts, with branch-to-checkout matching.

The current Pi session runs on Debian `media`, separate from Windows RT. The SSH alias `rt` resolves in this shell; this OpenSpec does not provision credentials or claim that remote authentication is available in every session. A remote action must stop if the alias/authentication is unavailable, without asking the owner to paste secrets into chat.

## 1. Authorization and pre-action report

Each remote sync SHALL require an explicit owner request to deploy. A merged PR or standing project-level policy alone SHALL NOT trigger deployment. Before the command, the agent SHALL state the exact RT checkout path, branch, expected commit, that no pipeline/config/input/output is involved, and the effect (advance that checkout's Git working tree to the requested commit). The explicit deploy request authorizes only the code sync to the named checkout/branch; a different target or commit requires clarification.

## 2. Allowed targets and branch mapping

Only these paths are eligible:

| RT checkout | Allowed branch | Intended role |
|---|---|---|
| `D:\pact\pact_translator_v4_1` | `main` | Live production 4.1 checkout |
| `D:\pact\pact_translator_v4_2` | explicitly requested `dev/v4.2-*` branch | v4.2 development/test checkout |

The implementation SHALL derive the exact path/branch from the approved request and existing deployment workflow. It SHALL NOT infer a production target from a generic "deploy" request or substitute one checkout for the other. Any other path or branch is out of scope.

## 3. Fail-closed Git-sync sequence

Before mutation on RT, the agent SHALL use the SSH alias only for a bounded, auditable remote command and verify the named checkout exists, current branch is the expected branch (or the documented first-time branch-switch procedure applies), and `git status --porcelain` is empty. It SHALL verify the remote/branch identity without printing credentials or private secrets. It SHALL record the current local `HEAD`, fetch only the approved target branch, and verify `origin/<target-branch>` resolves exactly to the owner-requested expected commit before switching or pulling (and, where checked, that the current `HEAD` is an ancestor of that target so the fast-forward is known to be possible). If the remote target does not resolve to the expected commit, it SHALL stop before any mutation and report.

The agent may then execute only the established Git sync for the chosen target:

- v4.1: fetch/update `origin/main`, switch to `main` if necessary and clean, then `git pull --ff-only`.
- v4.2: fetch the exact approved `origin/dev/v4.2-*` branch, switch to that branch (creating the local tracking branch only as documented on first use), then `git pull --ff-only`.

No `reset`, `clean`, force operation, direct file write/copy, arbitrary shell script, configuration edit, or automatic rollback is allowed. If the tree is dirty, branch/remote identity is unexpected, network/authentication fails, the fetched remote target does not resolve to the expected commit, fast-forward is impossible, or verification differs from the expected commit, stop immediately and report; do not attempt repair or cleanup.

After sync, verify and report the exact `HEAD` commit and branch on RT. Claim deployment complete only when RT reports the expected commit (or `Already up to date` at that commit).

## 4. Explicit exclusions

This exception SHALL NOT authorize starting a pipeline; reading/overwriting/migrating caches, snapshots, output books, or persistent data; changing config/model/provider/lifecycle settings; starting/stopping/restarting services or model servers; or making direct edits to tracked files on RT. Each remains under existing owner-approval/manual-only policies. Merge remains a separate owner decision and is not implied by deploy permission.

## 5. Verification

Tests SHALL verify the policy text names exact checkouts/branches, requires per-deploy explicit request and clean-tree/fast-forward verification, requires pre-mutation expected-commit verification (recorded HEAD, fetch-only-target, origin/<branch> resolves to expected SHA, ancestor check) and stops on expected-SHA mismatch before mutation, stops on dirty/non-FF/identity mismatch, and preserves pipeline/data/service exclusions. Validate OpenSpec strictly, lint the focused diff for secrets and unrelated policy changes, and run `git diff --check`. No production/RT action is part of artifact implementation.