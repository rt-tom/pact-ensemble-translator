## 1. Stage-aware chapter resume

- [ ] 1.1 Add durable stage checkpoint/manifest recording completion, artifact paths, input identities and integrity for `generation/selection`, `audit`, `repair`, `formatting/finalization`; support inferring the first stage from existing artifacts for legacy out-dir.
- [ ] 1.2 Add `chapter --resume`: choose the first failed/missing stage; reuse valid completed stages and rerun only that stage plus dependent stages, using current shared memory for any newly executed stage.
- [ ] 1.3 Ensure book retry does not roll back or recalculate already completed downstream chapters; successful retry only adds that chapter's new observations.
- [ ] 1.4 Add `chapter --retry-incomplete`: after journal identity validation, rewind to the first `incomplete_generation` and regenerate it plus the dependent tail; journal remains append-only and replay uses revision/attempt markers rather than raw journal length.
- [ ] 1.5 Preserve old positional journal behavior without resume flags.
- [ ] 1.6 Keep journal/stage identity checks fail-closed; foreign identity is an error, not silent reuse.

## 2. Book resume

- [ ] 2.1 Add `book --resume`: for each chapter, choose stage-aware reuse when its artifacts and source/config/backend contract are valid; otherwise rerun from the first failed stage in the same chapter folder using current shared memory.
- [ ] 2.2 Do not roll back or rebuild shared memory and do not recalculate already completed downstream chapters; apply only the retried chapter's new promotion observations.
- [ ] 2.3 Add `book --force-rerun-chapter <id>` (repeatable): exclude the chapter from every skip/reuse decision.
- [ ] 2.4 For a skipped ready chapter, run only acceptance+promotion; make repeated promotion idempotent and rebuild `book_run.json` completely.
- [ ] 2.5 Do not blindly pass `--retry-incomplete` to every chapter; automatic resume selects the failed stage, while generation rewind is used only when selected by checkpoint or explicit flag.
- [ ] 2.6 Without `--resume`, book behavior remains unchanged.

## 3. Invariants (tests)

- [ ] 3.1 Chapter: repair failure + `--resume` → generation/selection/audit artifacts reused, repair rerun, then finalization; no unnecessary generation calls.
- [ ] 3.2 Chapter: formatting/finalization failure + `--resume` → translation/repair artifacts reused, formatting/finalization rerun.
- [ ] 3.3 Chapter: journal `selected, incomplete, <tail>` + `--retry-incomplete` → selected prefix reused (0 calls), incomplete and tail regenerated, journal only appended.
- [ ] 3.4 Chapter: same journal without retry flag preserves old behavior; incomplete is not regenerated.
- [ ] 3.5 Chapter: foreign identity in journal/stage artifact + resume/retry → fail-closed.
- [ ] 3.6 Book: ready chapter with matching current identity + `--resume` → strict stages not run, promotion only.
- [ ] 3.7 Book: retrying chapter 2 after chapters 3–4 already ran keeps chapters 3–4 and their observations intact, and adds chapter 2's new promotion observations.
- [ ] 3.8 Book: repeated resume does not duplicate glossary/book-memory ledger or canonical memory/index entries.
- [ ] 3.9 Book: `--force-rerun-chapter` reruns even a ready chapter.

## 4. Docs, verification

- [ ] 4.1 Docs/help: `--retry-incomplete`, `--resume`, `--force-rerun-chapter`, стабильный `--out-base` для продолжения; что `quarantined` не ретраится.
- [ ] 4.2 `openspec validate book-chapter-retry --strict`, фокусные strict/book/journal тесты, `git diff --check`.
