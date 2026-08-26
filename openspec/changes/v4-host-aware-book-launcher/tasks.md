## 1. Host-aware book layout and source resolution

- [x] 1.1 Add a declarative RT/media launcher-layout contract with the approved RT roots and isolated media worker state/output roots — verify: unit tests resolve each host layout and reject a source/state path collision.
- [x] 1.2 Implement safe numeric source discovery for exactly one regular non-symlink `NNNN_*.html` per requested chapter, accepting single `N` as shorthand for `N-N` — verify: tests cover variable suffix success, single/range expansion, zero matches, multiple matches, symlink/non-regular rejection, and actual full-ID forwarding.
- [x] 1.3 Extend book check-only preflight with source-range readability and state/output readiness checks without directory creation or network/state-sync side effects — verify: preflight/JSON tests assert reports and failure behavior for RT-like Windows and media-like Linux fixture layouts.

## 2. Simple command and remote policy

- [x] 2.1 Implement mutually exclusive `book --local` and `book --remote [translator/reviewer]` selection, where bare remote uses profile defaults, while retaining explicit `--runtime-config` advanced compatibility — verify: dispatcher tests cover bare/overridden remote selection, invalid combinations, and exact delegated argv/exit propagation.
- [x] 2.2 Make all book modes inject `--whole-chapter`; make simple remote mode inject `--managed-server` and resolve remote defaults — verify: forwarding tests prove defaults and reject incompatible explicit topology choices.
- [x] 2.3 Update the canonical remote example to reasoning `3`, Muse Free generator/repair, Luna standard reviewer bindings, and explicit entity-extractor binding — verify: runtime-config/profile tests assert bindings, reasoning transport, identity, and provider-registry resolution without provider contact.
- [x] 2.4 Update safe help and preflight output for host roots, source discovery, local/remote modes, whole-chapter, managed-server, model/reasoning defaults, and advanced compatibility — verify: help/check-only tests have no pipeline, lifecycle, provider, state-sync, or artifact side effects.

## 3. Media state synchronization integration

- [x] 3.1 Apply default media arguments (`book-id=1`, `target=media-snap`, `root=/home/rt/pact_runs`) to every simple local/remote book mode with an explicit safe book-id override — verify: dispatcher/book-run tests assert RT local and remote pre-init fetch/post-promotion forwarding plus override behavior.
- [x] 3.2 Add a media-host local restricted-facade transport path with behavior equivalent to the reviewed SSH facade and no self-SSH — verify: transport tests assert the media path does not spawn SSH and preserves allowlist/rejection behavior.
- [x] 3.3 Stop creating a duplicate `memory_dir/state/` mirror during current-state fetch while retaining root canonical files, `CURRENT.json`, and `manifest.json` — verify: snapshot tests assert no nested state copy and existing root-state consumers still pass.
- [x] 3.4 Emit one final machine-readable and human-readable `MEDIA PUBLISH: ACCEPTED` or `REJECTED` verdict with revision/reason evidence, and return non-zero for failed/missing confirmation — verify: book-run tests cover accepted, rejected, transport failure, and missing confirmation.

## 4. Validation and documentation

- [x] 4.1 Add focused regression tests across runtime profile, dispatcher, book run, and snapshot transport boundaries — verify: run the narrowest relevant pytest suites and `python -m compileall` for changed Python modules.
- [x] 4.2 Update v4 operator documentation with the supported simple commands, canonical RT/media paths, source naming rules, media prerequisites, and explicit no-live-run boundary — verify: documentation/link review and `openspec validate v4-host-aware-book-launcher --strict` pass.
- [x] 4.3 Perform pact-dev self-review and independent pact-rev review in the same isolated implementation worktree before any merge — verify: review verdict follows the project `APPROVED`/`REQUEST_CHANGES` workflow and recorded checks cover the declared scope.
