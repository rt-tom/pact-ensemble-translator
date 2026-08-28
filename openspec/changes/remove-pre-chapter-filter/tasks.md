## 1. Contract and test preparation

- [x] 1.1 Inspect all `pre_chapter_book_memory`, `_variants_with_provenance`, `_chapter_before`, and `_field_provenance_before` call sites; distinguish functional gates from stale comments/docstrings.
- [x] 1.2 Add synthetic, versioned test fixtures that model an out-of-order full-memory state. Do not read, copy, or mutate `/home/rt/pact_runs/books/1/` or any production/run artifact during automated tests.
- [x] 1.3 Establish expected Rule 1 outputs: source-present characters/entities/terms, key-present facts, and participant-present address forms are included; an unrelated absent record remains excluded.

## 2. Remove Rule 2 in the shared selector

- [x] 2.1 Change `pre_chapter_book_memory(book_memory, chapter_id)` to return `dict(book_memory)`: a distinct top-level shallow copy with no provenance-based section filtering, no mutation, and the existing signature retained.
- [x] 2.2 Change `_variants_with_provenance(...)` to delegate to `_variants_for(...)`; retain the signature but make `chapter_id` non-gating.
- [x] 2.3 Remove the `_chapter_before` eligibility condition from the `world_term` branch; retain its existing source-presence and approved-term rules. Stored `world_term` entities now require `policy.approved_terms` approval and source presence; non-approved stored world_terms are excluded from both `terms` and `named_entities` (negative fixture added: `test_unapproved_stored_world_term_excluded_even_when_present`).
- [x] 2.4 Preserve schema/policy fail-soft, narrator handling, glossary-conflict locks, and index-only rendering. Remove `_field_provenance_before` only if static inspection confirms it is unused.
- [x] 2.5 Update stale causal `< N` comments/docstrings in `build_chapter_index.py`, `phase1/memory.py`, and `bible_renderer.py`; preserve `bible_renderer`'s no-full-memory fallback behavior.

## 3. v4.2 consumer compatibility

- [x] 3.1 Confirm `select_relevant` receives the full shallow copy through the shared helper and still computes one deterministic presence-based relevance result per chapter.
- [x] 3.2 Update role-view documentation/variable wording that says its input is pre-chapter; preserve `_is_excluded` / `_excluded_conflict` conflict exclusion and existing narrator/seed/global-voice exceptions.
- [x] 3.3 Confirm the promotion index rebuild in `phase1/memory.py` uses the non-filtering helper consistently and does not alter transaction semantics.

## 4. Focused verification

- [x] 4.1 Verify the helper returns a distinct top-level mapping with every fact/entity/variant still available, including records attributed to the target or a later chapter.
- [x] 4.2 Replace the causal/backward-leak assertions in `tests/pact_v4/test_v2_index_scope_causal.py` and affected `test_a2_chapter_index.py` cases: a later alias, target-chapter fact, and target-chapter world term are eligible only when their applicable Rule 1 source surface is present; absent records remain excluded. Stale `test_future_chapter_fact_never_visible_earlier` / `test_future_character_attrs_never_visible_earlier` reworded as index-only/fail-soft assertions; A2 and `test_v2_index_scope_causal` module comments updated.
- [x] 4.3 Verify v4.2 `select_relevant`/role views select multiple source-relevant records from full memory and still exclude an `_excluded_conflict` record.
- [x] 4.4 Verify missing/foreign schema or policy remains narrator + seed fail-soft and `render_bible_section` never falls back to a full-memory dump.
- [x] 4.5 Run the narrow focused pytest modules for chapter-index and role-view selection, then `pact-fidelity-lint`; do not start a model server or pipeline.
- [x] 4.6 Run `openspec validate remove-pre-chapter-filter --strict`; resolve any findings.

## 5. Owner evaluation boundary

- [x] 5.1 Document (but do not launch) the dev-branch command for a read-only-state v4.2 chapter-1 evaluation and its expected presence-based context separately from automated test fixtures. Launch requires subsequent independent code-review approval and separate owner approval.

### 5.1 Read-only v4.2 chapter-1 evaluation (documented, not launched)

No model server or pipeline was launched in this change. The following dev-branch
read-only evaluation is recorded for separate owner approval and must be run only
after independent code-review approval:

```bash
# Dry-run / read-only-state evaluation of chapter 1 with full-memory presence-based selection.
# Does NOT mutate /home/rt/pact_runs/books/1/ or any production artifact.
# Uses a transient copy of the book-state directory (or a synthetic fixture)
# and prints the selected chapter index / role-view without invoking a model.

# Example (no network, no model calls):
cd /home/rt/projects/pact-worktrees/remove-pre-chapter-filter
python - << 'PY'
from pathlib import Path
from pact_full_pipeline_runner_v1.build_chapter_index import build_chapter_index, pre_chapter_book_memory, load_glossary
from pact_v4.runtime.book_memory_role_views import compute_role_views
import json

# Load a snapshot of book_memory (e.g. rev-0012) into a temp dir copy.
memory_dir = Path("/tmp/pact-eval-memory")  # transient copy, not production
book_memory = json.loads((memory_dir / "book_memory.json").read_text())
chapter_id = "0001"
source_text = (memory_dir / "chapter_0001.html").read_text() if (memory_dir / "chapter_0001.html").exists() else "Blake Thorburn walked to Hillsglade House and discussed Demesnes."

# Full-memory shallow copy, presence-based selection (Rule 1)
full_bm = pre_chapter_book_memory(book_memory, chapter_id)
entry = build_chapter_index(chapter_id=chapter_id, source_text=source_text, book_memory=full_bm, glossary=load_glossary(str(memory_dir)))
print(json.dumps(entry, ensure_ascii=False, indent=2))
# Expected: entry["characters"] includes every approved character whose
# canonical name/variant is present in source_text, including those whose
# provenance is at or after 0001 (later-learned alias, later chapter fact
# keys when present, world_term first recorded in 0001 when approved+present).
# Absent records remain excluded. narrator + conflict locks still apply.

# v4.2 role-view projection (same full state)
views = compute_role_views(book_memory, load_glossary(str(memory_dir)), chapter_id, {"p1": source_text})
print(views["views"]["translator"].text[:2000])
PY
```

Expected presence-based context: with out-of-order accumulated state, chapter 1
may include later-promoted source-derived records whose surface is present in
its own source (self-reference / future-leakage is intentional and bounded by
Rule 1). Context remains a bounded chapter-relevant slice (not a full dump);
absent/unapproved terms remain excluded.

### Evidence (focused checks, this fix iteration)

- `pytest -q tests/pact_v4/test_v2_index_scope_causal.py tests/pact_v4/test_a2_chapter_index.py tests/pact_v4/runtime/test_book_memory_role_views.py` → 59 passed (includes new `test_unapproved_stored_world_term_excluded_even_when_present`)
- `pytest -q tests/pact_v4/test_b7_bible_and_cross_chapter.py::TestRenderBibleSection` (index-only subset, -k not promotion/wrapper) → 8 passed; stale causal tests reworded as index-only/fail-soft
- `bash .pi/skills/pact-fidelity-lint/scripts/lint.sh` → PASS (static, no pipeline; pair-based skipped — no proposal files)
- `openspec validate remove-pre-chapter-filter --strict` → Change is valid
- `git diff --check` → no whitespace errors
- Broader `tests/pact_v4/test_b7_bible_and_cross_chapter.py` promotion/wrapper tests show pre-existing exact-four-file fixture failures (missing `chapter_index.json`) outside this diff; not claimed as passing.
