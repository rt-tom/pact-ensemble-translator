## 1. Current interface inventory

- [ ] 1.1 Inspect strict chapter and book CLI parsers, current run-config/runtime descriptors, README navigation, and formatting/markup controls; record supported source-pattern and local/remote-label derivation without selecting a legacy launcher — verify: static inventory in change notes/tests with exact paths.
- [ ] 1.2 Define the additive book-first dispatcher contract: `--chapters START-END` + `--run-config FILE`, retained chapter mode, existing source-pattern resolution, and automatic `D:\\pact\\gate_bench_runs/book_0027-0032_local|remote_<timestamp>` output — verify: parser validation test plan covers valid/invalid ranges and descriptor failure.

## 2. Unified command

- [ ] 2.1 Implement the thin v4 dispatcher with book-first and retained chapter selection, exact delegated argv/exit propagation, range validation/expansion, and automatic output naming — verify: unit tests patch existing entrypoints and assert forwarding, invalid range rejection, and local/remote output names.
- [ ] 2.2 Implement offline top-level help that documents book range + run-config, existing source-pattern resolution, automatic output root/naming, supported v4 path, `--markup preserve`, and owner-started RT boundary — verify: `--help` test has no run side effects.
- [ ] 2.3 Ensure delegated mode help exposes existing relevant strict/book options unchanged and rejects unsupported markup values before startup — verify: CLI help tests assert required input/output, runtime, topology/resume, audit/formatting/markup terms.

## 3. Documentation and verification

- [ ] 3.1 Update v4 operator navigation with the unified command and no executable production run claim — verify: README/docs test or manual link/path review; no v3/v31 launcher presented as supported v4.
- [ ] 3.2 Run narrow CLI/help tests, `git diff --check`, and `openspec validate v4-run-command-help --strict`; verify no network, model server, or pipeline execution occurred.