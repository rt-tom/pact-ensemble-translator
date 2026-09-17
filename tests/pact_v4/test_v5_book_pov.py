"""V5 slice-1: per-chapter POV in the bible renderer.

The default (``chapter_pov=None``) is byte-identical to the legacy
output; a rotation book (book-level ``pov.gender`` null, e.g. Pale)
renders ``POV: <name> (<gender>)`` with no global narrator assertion;
Pact's established single narrator keeps the legacy ``Narrator:`` line.
"""
from __future__ import annotations

import pytest

from pact_v4.runtime.bible_renderer import _pov_lines, render_bible_section


def _mem(gender="male"):
    return {"pov": {"gender": gender}, "facts": [{"text": "Seed fact", "seed": True}]}


def test_default_is_legacy_byte_identical():
    mem = _mem()
    before = render_bible_section("0001", None, mem)
    assert "Narrator: male" in before
    assert render_bible_section("0001", None, mem, chapter_pov=None) == before
    assert render_bible_section(mem) == render_bible_section(mem, chapter_pov=None)


def test_pact_consistent_chapter_pov_keeps_legacy_line():
    mem = _mem("male")
    out = render_bible_section("0001", None, mem,
                               chapter_pov={"name": "Blake Thorburn", "gender": "male"})
    assert out == render_bible_section("0001", None, mem)
    assert "Narrator: male" in out and "POV:" not in out


def test_rotation_book_renders_chapter_pov_without_global_narrator():
    mem = {"pov": {"gender": None}, "facts": []}
    out = render_bible_section("0001", None, mem,
                               chapter_pov={"name": "Verona", "gender": "female"})
    assert "POV: Verona (female)" in out
    assert "Narrator" not in out


def test_conflicting_chapter_pov_wins_explicitly():
    out = render_bible_section("0001", None, _mem("male"),
                               chapter_pov={"name": "Lucy", "gender": "female"})
    assert "POV: Lucy (female)" in out
    assert "Narrator" not in out


def test_bare_string_and_missing_gender():
    mem = {"pov": {"gender": None}, "facts": []}
    assert "POV: SB" in render_bible_section("0001", None, mem, chapter_pov="SB")
    assert "POV: SB" in render_bible_section(
        "0001", None, mem, chapter_pov={"name": "SB"})
    assert _pov_lines(mem, None) == []
    assert _pov_lines(mem, {}) == []
    assert _pov_lines(mem, {"name": "  "}) == []


def test_chapter_entry_path_carries_pov():
    from pact_v4.runtime.book_memory_policy import BOOK_MEMORY_POLICY_VERSION

    index = {
        "$schema": "pact-v4-chapter-index/v2",
        "$book_memory_policy_version": BOOK_MEMORY_POLICY_VERSION,
        "0001": {"characters": ["Verona"], "facts": [], "address": []},
    }
    mem = {"pov": {"gender": None}, "facts": []}
    out = render_bible_section("0001", index, mem,
                               chapter_pov={"name": "Verona", "gender": "female"})
    assert "POV: Verona (female)" in out
    assert "Verona" in out
    # Same entry without chapter POV: no narrator lines at all.
    out2 = render_bible_section("0001", index, mem)
    assert "POV:" not in out2 and "Narrator" not in out2


# ---------------------------------------------------------------------------
# Threading: approved chapters.json record -> runtime prompts (BLOCKER fix)
# ---------------------------------------------------------------------------

def _write_pale_chapters(home, books_dir=None):
    """Pale-style approved file: rotation POVs, no approved titles."""
    import json as _json

    home.mkdir(parents=True, exist_ok=True)
    path = home / "chapters.json"
    path.write_text(_json.dumps({
        "schema_version": "pact-v5-approved-chapters/v1",
        "book_slug": "pale",
        "chapters": [
            {"file": "0001_pale.html", "order": 1, "en_title": "1.0",
             "ru_title": None,
             "pov": {"name": "Verona", "gender": "female"}, "notes": ""},
            {"file": "0002_pale.html", "order": 2, "en_title": "1.1",
             "ru_title": None,
             "pov": {"name": "Lucy", "gender": "female"}, "notes": ""},
        ],
    }), encoding="utf-8")
    return path


def test_pale_chapter_pov_reaches_generation_prompt(tmp_path):
    """End-to-end (file -> config -> prompt text): a Pale profile chapter
    renders ``POV: Verona (female)`` in the whole-chapter generation
    prompt with no global narrator assertion."""
    b3 = pytest.importorskip("tests.pact_v4.pipeline.test_v4_phase12_strict_runner_b3")
    import argparse as _argparse

    from pact_full_pipeline_runner_v1.v4_phase12_strict_run import _load_chapter_pov

    chapters_json = _write_pale_chapters(tmp_path / "books" / "pale")
    name, gender = _load_chapter_pov(_argparse.Namespace(
        chapters_json=str(chapters_json), book_slug="pale", chapter_id="0001_pale"))
    assert (name, gender) == ("Verona", "female")

    cfg = b3._whole_chapter_cfg(
        tmp_path,
        chapter_pov_name=name,
        chapter_pov_gender=gender,
    )
    backend = b3._B3MockBackend(audit_issues=[], reaudit_issues=[])
    caller = b3._DefectiveWholeChapterCaller()
    b3._run_with_b3(cfg, backend, caller=caller, entity_context_enabled=True)

    from pact_v4.phase2.prompts import render_prompt

    assert caller.calls
    prompts = [render_prompt(b) for b in caller.calls]
    assert any("POV: Verona (female)" in p for p in prompts)
    assert all("Narrator:" not in p for p in prompts)


def test_pale_chapter_pov_reaches_adapter_bible(tmp_path):
    """Adapter-injection path carries the same validated POV line."""
    import json as _json

    from pact_full_pipeline_runner_v1.v4_phase12_strict_run import _load_bible_text

    chapters_json = _write_pale_chapters(tmp_path / "books" / "pale")
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "book_memory.json").write_text(
        _json.dumps({"pov": {"gender": None}, "facts": []}), encoding="utf-8")
    (memory_dir / "glossary.json").write_text("[]", encoding="utf-8")

    text = _load_bible_text(
        memory_dir, "0001_pale", str(chapters_json), "pale")
    assert "POV: Verona (female)" in text
    assert "Narrator:" not in text
    # Numeric chapter id resolves through the same records.
    text2 = _load_bible_text(
        memory_dir, "2", str(chapters_json), "pale")
    assert "POV: Lucy (female)" in text2
    # Unconfigured path stays legacy-soft (no POV line, no crash).
    text3 = _load_bible_text(memory_dir, "0001_pale")
    assert "POV:" not in text3
