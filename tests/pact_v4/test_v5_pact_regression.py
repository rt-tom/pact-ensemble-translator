"""V5 slice-1: Pact real-data regression guards (media/RT only, else skip).

Alternative B: substitution == approved records on all 150 headings, the
derived CHAPTERS: block == the B1 table (drops asserted), migration
deterministic. No byte-regression required (B4 waived); the derived block
is the new prompt-identity baseline. Owner-executed RT verification (§4)
re-runs them via pytest alongside the pilot/pact runs.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

REPO = Path(__file__).resolve().parents[2]
CANON_SOURCE = Path("/home/rt/pact_chapters")
ARC_NAMES = REPO / "arc_names.json"
PACT_MANIFEST = REPO / "books" / "pact" / "manifest.json"
PACT_CHAPTERS = REPO / "books" / "pact" / "chapters.json"

needs_canon = pytest.mark.skipif(
    not CANON_SOURCE.is_dir() or not PACT_MANIFEST.is_file(),
    reason="requires media canonical source + migrated pact artifacts",
)

# Owner-approved B1 derived block (alternative B, spec scenario): unique arc
# pairs in book first-appearance order. Zero-chapter entries (Transgression,
# Sundown, bare Gathered) are absent by construction.
_DERIVED_BLOCK = [
    ("Bonds", "Узы"),
    ("Gathered Pages", "Собранные страницы"),
    ("Damages", "Ущерб"),
    ("Histories", "Хроники"),
    ("Breach", "Разрыв"),
    ("Collateral", "Залог"),
    ("Conviction", "Обвинение"),
    ("Subordination", "Подчинение"),
    ("Void", "Пустота"),
    ("Signature", "Подпись"),
    ("Null", "Нуль"),
    ("Mala Fide", "Mala Fide"),
    ("Malfeasance", "Злоупотребление"),
    ("Duress", "Принуждение"),
    ("Execution", "Казнь"),
    ("Sine Die", "Sine Die"),
    ("Possession", "Одержимость"),
    ("Judgment", "Суд"),
    ("Epilogue", "Эпилог"),
]
_DERIVED_BLOCK_TEXT = "\n".join(f"- {en} → {ru}" for en, ru in _DERIVED_BLOCK)


@needs_canon
def test_manifest_resolution_matches_legacy_discovery():
    """Manifest {order: file} == legacy NNNN_*.html prefix discovery, all 150."""
    from pact_v4.phase0b.book_manifest import load_manifest, validate_source_manifest

    manifest = load_manifest(PACT_MANIFEST)
    resolved = validate_source_manifest(CANON_SOURCE, manifest)
    assert len(resolved) == 150 and sorted(resolved) == list(range(1, 151))
    # Legacy rule: each N resolves to exactly one NNNN_*.html regular file.
    legacy = {}
    for entry in CANON_SOURCE.iterdir():
        if entry.name.endswith(".html") and len(entry.name) >= 5 and entry.name[:4].isdigit():
            legacy.setdefault(int(entry.name[:4]), []).append(entry.name)
    assert all(len(v) == 1 for v in legacy.values()) and len(legacy) == 150
    for order, path in resolved.items():
        assert path.name == legacy[order][0]
        assert path.stem == path.name[: -len(".html")]


@needs_canon
def test_heading_substitution_matches_approved_records():
    """Alternative B: every source heading substitutes to its approved ru_title.

    The title map is derived ONLY from chapters.json records (150/150).
    B1 spot checks pin the new table + number-format rules; the legacy
    15 entries are covered by migration determinism + review.
    """
    from pact_v4.phase0b.book_profile import load_approved_chapters
    from pact_full_pipeline_runner_v1.v4_book_html import _substitute_title, load_title_map

    new_map = load_title_map(PACT_CHAPTERS)
    approved = {c.en_title: c.ru_title for c in load_approved_chapters(PACT_CHAPTERS, "pact").chapters}
    assert new_map and len(new_map) == 150
    for en, ru in {
        "Breach 3.1": "Разрыв 3.1",
        "Signature 8.1": "Подпись 8.1",
        "Null 9.1": "Нуль 9.1",
        "Mala Fide 10.1": "Mala Fide 10.1",
        "Malfeasance 11.1": "Злоупотребление 11.1",
        "Duress 12.1": "Принуждение 12.1",
        "Sine Die 14.1": "Sine Die 14.1",
        "Gathered Pages: 1": "Собранные страницы: 1",
        "Gathered Pages (Arc 10)": "Собранные страницы (Арка 10)",
        "Histories (Arc 2)": "Хроники (Арка 2)",
        "Histories 9": "Хроники 9",
        "Epilogue": "Эпилог",
        "Bonds 1.1": "Узы 1.1",
    }.items():
        assert approved[en] == ru, en
        assert new_map[en] == ru, en

    checked = 0
    for path in sorted(CANON_SOURCE.glob("*.html")):
        soup = BeautifulSoup(path.read_text(encoding="utf-8-sig"), "html.parser")
        heading = soup.find(["h1", "h2"])
        assert heading is not None, path.name
        text = heading.get_text(" ", strip=True)
        assert _substitute_title(text, new_map) == approved[text], path.name
        checked += 1
    assert checked == 150


@needs_canon
def test_migrated_pact_chapters_shape():
    """Approved records: 150, contiguous, files == manifest, single narrator."""
    from pact_v4.phase0b.book_profile import load_approved_chapters

    manifest_files = {
        c["file"] for c in json.loads(PACT_MANIFEST.read_text(encoding="utf-8"))["chapters"]
    }
    approved = load_approved_chapters(PACT_CHAPTERS, "pact")
    assert len(approved.chapters) == 150
    assert {c.file for c in approved.chapters} == manifest_files
    assert all(
        c.pov is not None and c.pov.name == "Blake Thorburn" and c.pov.gender == "male"
        for c in approved.chapters
    )
    assert approved.title_map()  # non-empty: mapped arcs present
    titled = [c for c in approved.chapters if c.ru_title]
    assert len(titled) == 150  # alternative B: extended table covers every heading
    flagged = [c for c in approved.chapters if c.notes]
    assert {c.en_title.split()[0] for c in flagged} == {"Breach", "Null", "Malfeasance"}
    assert all(c.notes.startswith("review:") for c in flagged)


@needs_canon
def test_pact_derived_block_matches_approved_table():
    """Alternative B derived block: unique arc pairs in book first-appearance
    order per the spec scenario (B1 table), zero-chapter entries dropped.

    This exact text (under the ``CHAPTERS:`` label) is the new
    prompt-identity baseline the owner records at §4.2 — no byte-regression
    required (B4 waived).
    """
    from pact_v4.phase0b.book_profile import load_approved_chapters

    approved = load_approved_chapters(PACT_CHAPTERS, "pact")
    pairs = approved.prompt_arc_pairs()
    assert [en for en, _ in pairs] == [en for en, _ in _DERIVED_BLOCK]
    assert pairs == tuple(_DERIVED_BLOCK)
    block = "\n".join(f"- {en} → {ru}" for en, ru in pairs)
    assert block == _DERIVED_BLOCK_TEXT
    for dropped in ("Transgression", "Sundown"):
        assert f"- {dropped} " not in block and f"\n{dropped}" not in block
    assert "- Gathered →" not in block  # bare Gathered dropped (only Gathered Pages)
    assert "- Gathered Pages → Собранные страницы" in block


@needs_canon
def test_migration_is_deterministic(tmp_path):
    """Re-running the one-time migration reproduces both artifacts byte-for-byte."""
    from pact_v4.phase0b.migrate_pact_metadata import migrate

    out1, out2 = tmp_path / "b1", tmp_path / "b2"
    for out in (out1, out2):
        (out / "pact").mkdir(parents=True)
        (out / "pact" / "book.yaml").write_text("x", encoding="utf-8")
        migrate(CANON_SOURCE, ARC_NAMES, out)
    for name in ("manifest.json", "chapters.json"):
        assert (out1 / "pact" / name).read_bytes() == (out2 / "pact" / name).read_bytes()
    # And equal to the committed artifacts (no drift since owner review).
    for name in ("manifest.json", "chapters.json"):
        assert (out1 / "pact" / name).read_bytes() == (REPO / "books" / "pact" / name).read_bytes()
