# V4 Phase 0B — golden-set tooling

Read-only tooling for building the Phase 0 golden set from an EN chapter
and its human RU translation.

Backing docs:

- `docs/plans/V4_PHASE_0B_GOLDEN_SET_TASK_RU.md` — task and boundaries.
- `docs/schemas/v4_golden_record.schema.json` — `pact-v4-golden-record/v1`.
- `docs/architecture/V4_MVP_SPEC_RU.md` — Phase 0 context.

## Constraints

- No model calls. No production pipeline access. No v3 code touched.
- Chapter text, human RU translation, and generated records are **not**
  committed — `.gitignore` covers `/golden_sets/`.
- Human RU is a **reference**, not an exact-match ground truth. Cascade
  selection judges semantic equivalence, not literal match.

## Quick start (local, off-git)

```powershell
# 1. Extract EN + RU into a raw alignment draft.
py -m pact_v4.phase0b.cli extract `
   --source-html D:\pact\pact_translator_v3\pact_chapters\0044_subordination-6-1.html `
   --reference   D:\path\pact_ru.epub `
   --reference-entry EPUB/chapter_044.xhtml `
   --chapter 044 `
   --out-dir .\golden_sets\chapter_044

# 2. Build up to 100 schema-valid golden records.
py -m pact_v4.phase0b.cli build --in-dir .\golden_sets\chapter_044 --max-count 100

# 3. Validate against pact-v4-golden-record/v1.
py -m pact_v4.phase0b.cli validate --records .\golden_sets\chapter_044\records.json

# 4. Curate verdicts interactively.
py -m pact_v4.phase0b.cli curate --records .\golden_sets\chapter_044\records.json --reviewer rt --ask-notes

# 5. Summary.
py -m pact_v4.phase0b.cli report --records .\golden_sets\chapter_044\records.json
```

Also available:

- `extract` accepts a plain `.xhtml`/`.html` reference (no epub entry).
- `sample --in-dir <dir> --max-count N` prints the deterministic PID
  selection without writing records.
- `curate --input <file>` replaces the TTY with a scripted action list
  (one letter per line: `a`, `n`, `r`, `s`, `q`). Useful for tests and
  reproducible re-curation.

## Auto-verdict policy

Auto-verdict is conservative:

- `risk.band == "high"` → `needs_review`
- `alignment.confidence < 0.8` → `needs_review`
- otherwise → `unreviewed`

`accepted` is never assigned automatically — a human reviewer must accept.

## Files

| File | Purpose |
| ---- | ------- |
| `source_html.py` | EN HTML → leaf-block PID list |
| `reference_epub.py` | RU EPUB/xhtml → indexed segments |
| `alignment.py` | structural EN↔RU alignment |
| `risk.py` | source-only deterministic risk pre-screen |
| `schema.py` | in-repo JSON-schema validator (no new deps) |
| `golden_records.py` | assemble + atomic dump/load records |
| `cli.py` | `python -m pact_v4.phase0b.cli` entry point |
