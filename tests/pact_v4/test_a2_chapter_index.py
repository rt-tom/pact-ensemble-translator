"""V4.1 A2 contract tests for the deterministic per-chapter bible index.

Covers ``pact_full_pipeline_runner_v1.build_chapter_index`` (0 model calls,
``_term_present``-based presence rules, narrator/locked always) and the
chapter-based ``render_bible_section(chapter_id, chapter_index,
book_memory)`` path (plan §5.2 — bible by chapter, not "first N" caps).
"""
from __future__ import annotations

import json

from pact_full_pipeline_runner_v1.build_chapter_index import (
    build_chapter_index,
    build_index_file,
)
from pact_v4.phase2.risk import GlossaryEntry
from pact_v4.runtime.bible_renderer import render_bible_section

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _book_memory() -> dict:
    return {
        "pov": {"gender": "male", "source_name": "Blake Thorburn"},
        "characters": {
            "Blake Thorburn": {
                "gender": "male", "role": "Protagonist",
                "variants": {"Blake": 1},
            },
            "Duncan Behaim": {
                "gender": "male", "role": "Rival", "variants": {"Duncan": 1},
            },
            "Aimon Behaim": {
                "gender": "male", "role": "Uncle", "variants": {"Aimon": 1},
            },
        },
        "entities": {
            "June": {"type": "object", "notes": ["Blake's hatchet/blade."]},
        },
        "facts": [
            {
                "fact": "Blake is the first-person male narrator; Russian "
                        "first-person past-tense forms must be masculine.",
                "source_pids": [], "chapter": "0001",
            },
            {
                "fact": "Power is treated as a form of currency in this world.",
                "source_pids": [], "chapter": "0046",
            },
            {
                "fact": "Duncan Behaim is a rival practitioner from Toronto.",
                "source_pids": [], "chapter": "0001",
            },
        ],
        "address_register": [
            {"from": "Blake", "to": "Duncan", "register": "vy",
             "text": "Blake -> Duncan Behaim: вы"},
            {"from": "Blake", "to": "Aimon", "register": "ty",
             "text": "Blake -> Aimon Behaim: ты"},
        ],
    }


def _source_text() -> str:
    return (
        "Blake walked to the edge of the June clearing. Duncan stood there "
        "waiting, and the two of them stared at each other."
    )


# ---------------------------------------------------------------------------
# build_chapter_index: presence rules
# ---------------------------------------------------------------------------


def test_chapter_index_includes_present_characters_and_entities():
    idx = build_chapter_index(
        chapter_id="0001", source_text=_source_text(),
        book_memory=_book_memory(),
    )
    # Blake (variant) and Duncan are present; June is an entity present by
    # name; Aimon is NOT in the chapter source and must be excluded.
    assert "Blake Thorburn" in idx["characters"]
    assert "Duncan Behaim" in idx["characters"]
    assert "June" in idx["characters"]
    assert "Aimon Behaim" not in idx["characters"]


def test_chapter_index_narrator_always_present_even_when_unnamed():
    # The narrator is ALWAYS included even when the chapter never names him.
    source = "Duncan waited alone in the clearing."
    idx = build_chapter_index(
        chapter_id="0001", source_text=source, book_memory=_book_memory(),
    )
    assert "Blake Thorburn" in idx["characters"]


def test_chapter_index_fact_included_when_entity_present():
    idx = build_chapter_index(
        chapter_id="0001", source_text=_source_text(),
        book_memory=_book_memory(),
    )
    facts = idx["facts"]
    # The narrator fact is keyed to Blake (narrator, always) -> included.
    assert any("narrator" in fact for fact in facts)
    # The Duncan fact is keyed to Duncan (present in the chapter) -> included.
    assert any("rival practitioner" in fact for fact in facts)
    # The currency fact mentions no chapter entity -> excluded.
    assert not any("currency" in fact for fact in facts)


def test_chapter_index_locked_glossary_terms_always_included():
    # A glossary-conflict source term that is also a character name is
    # locked (fail-closed) and stays in the index even when the chapter
    # never mentions it.
    conflict_glossary = (
        GlossaryEntry(source_term="Aimon Behaim", target_terms=("Эймон", "Аймон")),
    )
    source = "Blake walked alone."
    idx = build_chapter_index(
        chapter_id="0001", source_text=source, book_memory=_book_memory(),
        glossary=conflict_glossary,
    )
    assert "Aimon Behaim" in idx["characters"]


def test_chapter_index_deterministic():
    r1 = build_chapter_index(
        chapter_id="0001", source_text=_source_text(), book_memory=_book_memory(),
    )
    r2 = build_chapter_index(
        chapter_id="0001", source_text=_source_text(), book_memory=_book_memory(),
    )
    assert r1 == r2


def test_chapter_index_address_follows_participant_presence():
    idx = build_chapter_index(
        chapter_id="0001", source_text=_source_text(),
        book_memory=_book_memory(),
    )
    # Blake and Duncan are present -> the vy form is included; Aimon is not
    # in the chapter -> the ты form is excluded.
    assert idx["address"] == ["Blake -> Duncan Behaim: вы"]


# ---------------------------------------------------------------------------
# build_index_file: writes chapter_index.json into the memory-dir
# ---------------------------------------------------------------------------


def test_build_index_file_writes_chapter_index_json(tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "book_memory.json").write_text(
        json.dumps(_book_memory(), ensure_ascii=False), encoding="utf-8",
    )
    chapter_html = tmp_path / "chapter.html"
    chapter_html.write_text(
        "<html><body><p id='p1'>Blake walked to the June clearing.</p></body></html>",
        encoding="utf-8",
    )
    entry = build_index_file(
        memory_dir=str(memory_dir), chapter_html=str(chapter_html),
        chapter_id="0001", out_path="",
    )
    assert "Blake Thorburn" in entry["characters"]
    index = json.loads((memory_dir / "chapter_index.json").read_text(encoding="utf-8"))
    assert index["0001"] == entry


# ---------------------------------------------------------------------------
# render_bible_section(chapter_id, chapter_index, book_memory)
# ---------------------------------------------------------------------------


def _chapter_index() -> dict:
    return {
        "0001": build_chapter_index(
            chapter_id="0001", source_text=_source_text(),
            book_memory=_book_memory(),
        )
    }


def test_render_bible_section_chapter_id_renders_entry():
    rendered = render_bible_section("0001", _chapter_index(), _book_memory())
    assert "BIBLE:" in rendered
    assert "Narrator: male" in rendered
    assert "Blake Thorburn, male, Protagonist" in rendered
    assert "Duncan Behaim" in rendered
    # No caps / no "(showing first N of M)" — the index is already filtered.
    assert "showing first" not in rendered
    assert "Aimon Behaim" not in rendered
    assert "currency" not in rendered


def test_render_bible_section_missing_chapter_falls_back_to_legacy():
    # No index entry for the chapter: the renderer falls back to the legacy
    # full-memory render (pre-A2 behaviour), so runs without an index work.
    rendered = render_bible_section("9999", _chapter_index(), _book_memory())
    assert "BIBLE:" in rendered
    assert "Aimon Behaim" in rendered  # legacy renders the full memory


def test_render_bible_section_legacy_form_still_supported():
    # Backward compatibility: render_bible_section(book_memory) keeps the
    # legacy full-memory render with caps.
    memory = {"pov": {"gender": "male"}}
    assert "Narrator: male" in render_bible_section(memory)
    assert render_bible_section({}) == ""
    assert render_bible_section(None) == ""
