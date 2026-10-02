## ADDED Requirements

### Requirement: Explicit owner request gates every RT code sync
An agent SHALL perform an RT code sync only after an explicit owner request to deploy the specific change. Merge or project policy alone SHALL NOT trigger a sync. Before execution the agent SHALL report the exact checkout, branch, expected commit, and code-only effects.

#### Scenario: Explicit deployment request
- **WHEN** the owner explicitly requests deployment and the agent identifies the permitted RT checkout, branch, and target commit
- **THEN** the agent may perform only the approved fast-forward Git sync to that target
- **AND** it does not infer authorization for another checkout, branch, or operation

#### Scenario: Merge without deployment request
- **WHEN** a PR is merged but the owner has not requested deployment
- **THEN** the agent does not access or update RT

### Requirement: RT Git sync is limited to documented checkouts and branches
An agent SHALL use only `D:\pact\pact_translator_v4_1` on `main` or `D:\pact\pact_translator_v4_2` on the specifically requested `dev/v4.2-*` branch. The agent SHALL verify checkout, branch, and remote identity before update. The worktree SHALL be clean before mutation. The agent SHALL record the current local `HEAD`, fetch only the approved target branch, and verify `origin/<target-branch>` resolves exactly to the owner-requested expected commit before switching or pulling (and, where checked, that the current `HEAD` is an ancestor of that target). The update SHALL use `git pull --ff-only` after fetching the exact target branch.

#### Scenario: Clean matching checkout
- **WHEN** the selected RT checkout and branch match the approved request and the worktree is clean
- **THEN** the agent may fetch and fast-forward only that branch
- **AND** verify the resulting HEAD equals the requested commit

#### Scenario: Dirty checkout or branch mismatch
- **WHEN** the RT checkout is dirty, has unexpected branch/remote identity, or does not match the requested target
- **THEN** the agent stops without changing files, switching destructively, cleaning, resetting, or attempting repair

#### Scenario: Remote target does not match expected commit
- **WHEN** the fetched `origin/<target-branch>` does not resolve exactly to the owner-requested expected commit (or the current `HEAD` is not an ancestor of that target, where checked)
- **THEN** the agent stops before any mutation without switching, pulling, resetting, or attempting repair
- **AND** it reports the recorded current `HEAD` and the resolved remote target

#### Scenario: Fast-forward is impossible
- **WHEN** `git pull --ff-only` cannot fast-forward to the requested commit
- **THEN** the agent stops and reports the conflict without merge, reset, force, or rollback

#### Scenario: SSH access unavailable
- **WHEN** the `rt` SSH alias or authentication is unavailable
- **THEN** the agent stops without requesting or exposing credentials in chat

### Requirement: RT sync permission excludes production data and lifecycle actions
The code-sync exception SHALL NOT authorize pipeline execution, direct edits, configuration changes, cache/snapshot/output-book/persistent-data operations, migrations, service restarts, or model-server lifecycle actions. Merge, deploy, and pipeline execution remain distinct decisions.

#### Scenario: Code-only deployment
- **WHEN** an authorized RT sync succeeds
- **THEN** only the named checkout's Git code revision changes
- **AND** no pipeline, model server, service, configuration, or persistent artifact is touched

#### Scenario: Post-sync reporting
- **WHEN** the sync completes
- **THEN** the agent reports checkout, branch, verified HEAD, and whether RT reported `Already up to date`
- **AND** claims completion only if the requested commit is verified