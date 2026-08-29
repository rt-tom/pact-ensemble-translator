## 1. Contract and fixtures

- [x] 1.1 Add a focused unit fixture for `_normalize_pid` over a chapter whose PIDs are `p00001`..`p00279`.
- [x] 1.2 Add an end-to-end validation fixture using the chapter-3 style payload (bare-integer `evidence_windows` such as `[[86,86]]`) plus a reference whose integer is outside the real PID set.

## 2. Implement PID normalization in validation

- [x] 2.1 Add `_build_pid_int_map(source_map)` and `_normalize_pid(pid, int_map)` to `pact_v4/audit/entity_extractor.py`.
- [x] 2.2 In `validate_entity_context`, build the int map from `source_map`; normalize `record.anchor.pid` and each `alias.pid` before their existence/verbatim checks and when building `verified_anchor` / kept aliases.
- [x] 2.3 Pass the int map to `_validate_claim`; normalize `claim_pids` (evidence + both `evidence_windows` endpoints), each `evidence[].pid` (existence, verbatim, and gender referent-link), and the reconstructed `EntityClaim` PIDs so stored claims are canonical.
- [x] 2.4 Preserve the dead-PID guard: a reference whose integer is absent from the real PID set still fails the existence check.

## 3. Verification

- [x] 3.1 `_normalize_pid` maps `p00086`/`00086`/`p86`/`86` → `p00086` and leaves `999` unchanged for the fixture chapter.
- [x] 3.2 The chapter-3 style payload with bare-integer `evidence_windows` is accepted (no dead-PID drop); a reference outside the real PID set is still dropped.
- [x] 3.3 Run `pytest -q tests/pact_v4/audit/` (entity-extraction suite) and `pact-fidelity-lint`; no model call or pipeline started.
- [x] 3.4 `openspec validate entity-pid-normalization --strict` passes.

Evidence (2026-08-29):
- `pytest -q tests/pact_v4/audit/test_entity_extractor.py` — 65 passed (4 new PID-normalization tests)
- `pytest -q tests/pact_v4/audit/` — 228 passed
- `openspec validate entity-pid-normalization --strict` — Change is valid
- `bash .pi/skills/pact-fidelity-lint/scripts/lint.sh` — PASS (static, no pipeline)

## 4. Delivery

- [ ] 4.1 Implement on an isolated branch/worktree; route implementation through `pact-dev` with independent `pact-rev` review.
- [ ] 4.2 Open a PR to the dev branch; do not merge to `main` without owner approval.
