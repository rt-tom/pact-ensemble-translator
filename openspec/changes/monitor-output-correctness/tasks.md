## 1. Investigation & fixtures

- [ ] 1.1 Confirm exact artifact field names: `glossary_proposals.json` proposal count, `formatting_report.json` aggregate restored/original spans + incidents, and the promotion result location/counts (glossary.json / book_memory.json) produced by `glossary-model-resolver`. Verify by reading the in-flight change artifacts and code.
- [ ] 1.2 Inventory legacy tests in `tests/pact_v4/pipeline/` that assert on the broken substrings (`not_started`, lifecycle strings, `mode=`, 6-line shape) and list them for rewrite.

## 2. Render fix (single source + per-phase)

- [ ] 2.1 In `render_report` (fine mode), remove `extra_parts` and the `merged = compact_bodies + extra_parts` concatenation; render `compact_bodies` only. Verify no contradiction (repair/reaudit shown once, correctly).
- [ ] 2.2 Remove the forced 6-line truncation (`if len(lines) > 6: lines = lines[:6]`); render one phase per line in pipeline order.
- [ ] 2.3 Simplify the header to `[<id>] run <elapsed> · quiet <age>`; drop `mode=` and the duplicated phase name. Keep alive/stalled expressed via `quiet`.
- [ ] 2.4 Extend the per-phase block: add a **Glossary** line (count from `glossary_proposals.json`) placed after Re-audit scope; correct **Formatting** to aggregate spans restored/original + incidents from `formatting_report.json` (drop `n/a`).
- [ ] 2.5 Preserve the local **speed** line when server logs are fresh for local backends.

## 3. Book-level promotion summary

- [ ] 3.1 In `render_book_report`, after the chapter detail, append a promotion summary line with glossary.json and memory promotion counts from the `glossary-model-resolver` promotion output.

## 4. Tests & validation

- [ ] 4.1 Rewrite legacy monitor tests to assert the new per-phase layout and the no-contradiction property (no phase shown both complete and `not_started`).
- [ ] 4.2 Add tests for the new Glossary line (present/absent), Formatting aggregate, and the book-level promotion summary.
- [ ] 4.3 Run `openspec validate` for this change and the monitor test suite (`python3 -m pytest tests/pact_v4/pipeline/test_v4_phase_progress_*`) and confirm all pass.

## 5. Docs

- [ ] 5.1 Update `docs/agent_operations/AGENTS_REFERENCE_RU.md` monitor section to reflect the new per-phase output, Glossary/Formatting/Promotion lines, and the removed `mode=` / duplicate `status:`/`phase:` lines.
