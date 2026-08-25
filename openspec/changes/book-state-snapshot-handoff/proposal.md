## Why

Pact v4 book runs mutate glossary, book memory, chapter index, candidate ledgers, and per-chapter artifacts in causal chapter order. Local models reside on RT while media is always available and should become the durable artifact authority. Direct shared-folder writes or two-way synchronization would corrupt mutable book state, violate resume identities, or lose promotion updates.

## What Changes

This change is **planning-only**. It defines a safe host-handoff protocol before any implementation.

- Define media as the canonical store of immutable source snapshots, accepted book-state snapshots, and terminal run bundles.
- Define media as canonical artifact/state authority and owner-started remote execution host; RT remains the local-model execution host and may also execute remote profiles. Each host stages locally and publishes only a validated terminal candidate snapshot.
- Define a one-writer book lock/lease, snapshot manifest, integrity validation, atomic current-snapshot promotion, rollback, crash recovery, and explicit terminal transfer rules.
- Define bootstrap from the existing authoritative RT book state to media and a manual media-to-RT terminal bundle mirror procedure.
- Explicitly prohibit live shared writes, two-way merge of mutable memory, cross-host resume, implicit artifact copying, pipeline execution, or deployment in this planning change.

## Capabilities

### New Capabilities

- (none — planning and contract definition only; no runtime behavior is changed)

### Modified Capabilities

- (none)

## Impact

- Future impact: book orchestration, memory promotion, artifact storage/transfer, host-side credentials/transport, and operational policy.
- This planning change edits only `openspec/changes/book-state-snapshot-handoff/**`.
- A later implementation change requires separate owner approval after transport, policy, layout, and recovery decisions are finalized. Media-controlled execution using RT-only llama servers is explicitly deferred to another high-risk change.