## 1. Policy and contract discovery

- [ ] 1.1 Inventory actual v4 book inputs, mutable memory files, per-chapter terminal artifacts, and existing RT/media host constraints without inspecting book text — verify: contract map cites writers/readers/identity fields.
- [ ] 1.2 Record owner decisions for media authority and owner-started remote-worker role, RT local-model/optional remote-worker role, canonical media root/book IDs, automatic complete/accepted-degraded eligibility, immutable retention plus 30-day quarantine expiry, SSH/SFTP plus restricted `pact-promote` command, and manual-after-TTL lease recovery — verify: decisions recorded coherently in planning artifacts and DECISIONS.md only when owner requests it.

## 2. Handoff protocol design

- [ ] 2.1 Specify snapshot manifest schema, immutable layout, parent revision, lease fields, no-secret boundary, and exact validation requirements — verify: design review maps every required field to an existing v4 contract or an approved new field.
- [ ] 2.2 Specify RT staging, candidate publication, validation, atomic `CURRENT` promotion, crash/partial-upload recovery, rollback, and concurrent-writer rejection — verify: failure-state table has no path that overwrites current state or merges mutable memory.
- [ ] 2.3 Specify SSH/SFTP transport, restricted `pact-promote` command, and host authentication/authorization model — verify: threat model covers source/artifact confidentiality, credentials, host identity, non-interactive command restrictions, and no live shared writes.

## 3. Implementation gate

- [ ] 3.1 Create a separate implementation OpenSpec only after all open questions and policy gates are approved; scope it to a minimal transport/protocol slice with offline tests first — verify: no code is changed under this planning-only change.
- [ ] 3.2 Validate this change with `openspec validate book-state-snapshot-handoff --strict` and review the planning diff — verify: no pipeline/config/artifact execution changes are present.