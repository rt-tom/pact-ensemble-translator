## 1. Contract-to-test gap map

- [ ] 1.1 Map existing strict-runner tests to the four Stage 1 contracts: resume/foreign identity, single-entry whole-chapter journal/PID order, chunked audit under whole-chapter generation, and terminal artifacts/provenance; verify the map cites test paths and `v4-phase12-strict-runner/contract-map.md` sections.
- [ ] 1.2 Identify only genuine assertion gaps; document why each new or adjusted test is contract-facing rather than private-implementation-facing; verify no production files are modified.

## 2. Offline characterization coverage

- [ ] 2.1 Add or adjust deterministic synthetic tests for resume identity rejection and append-only journal behavior; verify relevant `test_v4_phase12_strict_runner*` tests pass.
- [ ] 2.2 Add or adjust deterministic synthetic tests for whole-chapter single-entry journal, PID order, and whole-chapter generation while audit remains chunked; verify the whole-chapter suite passes.
- [ ] 2.3 Add or adjust deterministic synthetic tests for terminal translations/strict-record provenance; verify the relevant terminal/translation suite passes.

## 3. Baseline verification

- [ ] 3.1 Run the narrow strict-runner test families affected by Stage 1 and record exact commands/results; verify no network or pipeline execution occurred.
- [ ] 3.2 Run `git diff --check`, `openspec validate v4-strict-runner-characterization-baseline --strict`, and confirm the diff is limited to tests/OpenSpec artifacts; verify results are clean.