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
        "schema": "pact-v4-book-memory/v2",
        "book_memory_policy_version": "book-memory-policy/v1",
        "policy": {"explicit_deny": [], "explicit_allow": {}, "aliases": {}, "approved_terms": [], "generic_patterns_version": "generic-memory-reject/v1"},
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
    # Finding 2: chapter_index must carry v2 schema/policy or renderer fails soft to seed
    return {
        "$schema": "pact-v4-chapter-index/v2",
        "$book_memory_policy_version": "book-memory-policy/v1",
        "0001": build_chapter_index(
            chapter_id="0001", source_text=_source_text(),
            book_memory=_book_memory(),
        )
    }


def test_render_bible_section_chapter_id_renders_entry():
    rendered = render_bible_section("0001", _chapter_index(), _book_memory())
    assert "BIBLE:" in rendered
    assert "Narrator: male" in rendered
    # Index-only invariant: the chapter index entry carries only the NAMES
    # present in this chapter's source (Rule 1). The renderer renders those
    # names as-is (or with attrs the entry itself snapshots) and never
    # enriches them with gender/role read from the full accumulated
    # book_memory beyond the entry.
    assert "Blake Thorburn" in rendered
    # No gender/role enrichment in the character lines (index-only).
    char_lines = rendered.split("Characters:")[1].split("Facts:")[0]
    assert "Blake Thorburn, male" not in char_lines
    assert "Protagonist" not in char_lines
    assert "Duncan Behaim" in rendered
    # No caps / no "(showing first N of M)" — the index is already filtered.
    assert "showing first" not in rendered
    assert "Aimon Behaim" not in rendered
    assert "currency" not in rendered


def test_render_bible_section_missing_chapter_fails_soft_to_seed():
    # P0 causal-memory contract (2026-08-14): no index entry for the chapter
    # -> narrator + explicit seed facts ONLY. The legacy full-memory dump
    # (which leaked facts from chapters 46-148 into Bonds 1.1-1.3 prompts)
    # is removed — never restored.
    memory = {
        "pov": {"gender": "male"},
        "characters": {"Aimon Behaim": {"gender": "male"}},
        "facts": [
            {"fact": "Blake's vehicle is a motorcycle.", "seed": True},
        ],
    }
    rendered = render_bible_section("9999", _chapter_index(), memory)
    assert "BIBLE:" in rendered
    assert "Narrator: male" in rendered
    assert "motorcycle" in rendered
    assert "Aimon Behaim" not in rendered  # NOT a full memory dump


def test_render_bible_section_legacy_form_still_supported():
    # Backward compatibility of the call SHAPE (positional book_memory /
    # keyword book_memory) is preserved; the CONTENT is the fail-soft seed
    # render, never a full dump.
    memory = {"pov": {"gender": "male"}}
    assert "Narrator: male" in render_bible_section(memory)
    assert render_bible_section({}) == ""
    assert render_bible_section(None) == ""


def test_render_bible_section_keyword_book_memory_compatible():
    # A2 RV fix (MEDIUM): render_bible_section(book_memory=m) — the KEYWORD
    # legacy form — used to return an empty string (chapter_id=None clobbered
    # the explicit book_memory). It must render the seed bible exactly like
    # the positional form.
    memory = {"pov": {"gender": "male"}, "characters": {"Blake": {"gender": "male"}}}
    positional = render_bible_section(memory)
    assert "Narrator: male" in positional
    keyword = render_bible_section(book_memory=memory)
    assert keyword == positional
    assert "Narrator: male" in keyword


# ---------------------------------------------------------------------------
# load_glossary: flat production glossary (A2 RV fix)
# ---------------------------------------------------------------------------


def _memory_dir_with_glossary(tmp_path, payload):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(exist_ok=True)
    (memory_dir / "glossary.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8",
    )
    return str(memory_dir)


def test_load_glossary_flat_production_shape(tmp_path):
    # A2 RV fix (HIGH/MEDIUM): the PRODUCTION D:/pact/pact_chapters/
    # glossary.json is FLAT {source: target} (137 string-valued entries), but
    # load_glossary only accepted a list or {'entries': [...]} — so locked/
    # conflict entries were silently absent from chapter_index.json. A flat
    # mapping must load every source term.
    from pact_full_pipeline_runner_v1.build_chapter_index import load_glossary

    memory_dir = _memory_dir_with_glossary(tmp_path, {
        "Blake": "Блэйк",
        "Aimon Behaim": "Эймон Бехайм",
        "Paige": "Пэйдж",
    })
    entries = load_glossary(memory_dir)
    by_source = {e.source_term: e.target_terms for e in entries}
    assert by_source == {
        "Blake": ("Блэйк",),
        "Aimon Behaim": ("Эймон Бехайм",),
        "Paige": ("Пэйдж",),
    }


def test_load_glossary_flat_shape_with_target_lists(tmp_path):
    # Flat {source: [target, ...]} values are tolerated too (a target list is
    # a documented glossary shape).
    from pact_full_pipeline_runner_v1.build_chapter_index import load_glossary

    memory_dir = _memory_dir_with_glossary(tmp_path, {
        "Blake": ["Блэйк", "Блейк"],
        "Aimon": "Эймон",
    })
    entries = load_glossary(memory_dir)
    by_source = {e.source_term: e.target_terms for e in entries}
    assert by_source["Blake"] == ("Блэйк", "Блейк")
    assert by_source["Aimon"] == ("Эймон",)


def test_load_glossary_wrapped_and_list_shapes_still_work(tmp_path):
    # Non-regression: the wrapped {'entries': [...]} and bare-list shapes the
    # A2 loader already accepted keep working.
    from pact_full_pipeline_runner_v1.build_chapter_index import load_glossary

    wrapped = _memory_dir_with_glossary(tmp_path, {
        "entries": [
            {"source": "Blake", "targets": ["Блэйк"]},
            {"source_term": "June", "target_terms": ["Джун"]},
        ]
    })
    entries = load_glossary(wrapped)
    assert {e.source_term for e in entries} == {"Blake", "June"}

    listed = _memory_dir_with_glossary(tmp_path, [
        {"source": "Paige", "targets": "Пэйдж"},
    ])
    entries = load_glossary(listed)
    assert {e.source_term for e in entries} == {"Paige"}


def test_build_index_file_includes_locked_terms_from_flat_glossary(tmp_path):
    # A2 RV fix: with the flat production glossary the locked/conflict policy
    # reaches the chapter index — a glossary CONFLICT source term (two
    # distinct targets) absent from the chapter text is still ALWAYS included
    # (fail-closed). Uses the flat-with-target-list shape to exercise both
    # the flat parsing and the conflict locking.
    from pact_full_pipeline_runner_v1.build_chapter_index import build_index_file

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "book_memory.json").write_text(
        json.dumps(_book_memory(), ensure_ascii=False), encoding="utf-8",
    )
    (memory_dir / "glossary.json").write_text(
        json.dumps({"Aimon Behaim": ["Эймон", "Аймон"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    chapter_html = tmp_path / "chapter.html"
    chapter_html.write_text(
        "<html><body><p id='p1'>Blake walked alone.</p></body></html>",
        encoding="utf-8",
    )
    entry = build_index_file(
        memory_dir=str(memory_dir), chapter_html=str(chapter_html),
        chapter_id="0001", out_path="",
    )
    # Aimon Behaim is a glossary conflict -> locked, present even though the
    # chapter text never mentions him.
    assert "Aimon Behaim" in entry["characters"]


# ---------------------------------------------------------------------------
# v4_book_run builds chapter_index.json after accepted chapters
# (presence-based full-memory selection: the renderer needs a fresh
# per-chapter entry built from the full accumulated book_memory;
# a failed chapter never creates an entry).
# ---------------------------------------------------------------------------


def test_book_run_builds_chapter_index_after_accepted_chapter(tmp_path, monkeypatch):
    from pact_full_pipeline_runner_v1 import v4_book_run

    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "book_memory.json").write_text(
        json.dumps(_book_memory(), ensure_ascii=False), encoding="utf-8",
    )
    (memory / "glossary.json").write_text("{}", encoding="utf-8")
    (memory / "observations.json").write_text("{}", encoding="utf-8")
    (memory / "chapter_index.json").write_text("{}", encoding="utf-8")

    out_base = tmp_path / "out"
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "0001.html").write_text(
        "<html><body><p id='p1'>Blake walked to the June clearing.</p>"
        "<p id='p2'>He saw the gate.</p></body></html>",
        encoding="utf-8",
    )
    out_dir = out_base / "chapter_0001"
    out_dir.mkdir(parents=True)
    (out_dir / "selection_results.json").write_text(
        json.dumps({"chapter_id": "0001", "results": [
            {"chunk_id": "chunk0001", "status": "selected"},
        ]}),
        encoding="utf-8",
    )
    (out_dir / "strict_chapter_trial_record.json").write_text(
        json.dumps({"chapter_id": "0001", "step8": {"status": "complete"}}),
        encoding="utf-8",
    )
    (out_dir / "translations.json").write_text(
        json.dumps({"p00001": "Блэйк шёл к июньской поляне.",
                    "p00002": "Он увидел ворота."}),
        encoding="utf-8",
    )
    (out_dir / "chunk_plan.json").write_text(
        json.dumps({"artifact": "pact-v4-chunk-plan/v1", "snapshot_hash": "t",
                    "plan_hash": "t", "chunks": [
                        {"chunk_id": "chunk0001", "snapshot_hash": "t",
                         "pids": ["p00001", "p00002"], "word_counts": [],
                         "context": {"left_ru": "", "right_en": []},
                         "undersized_exception": False},
                    ]}),
        encoding="utf-8",
    )

    def fake_run_one(*args, **kwargs):
        return {"status": "ok"}

    monkeypatch.setattr(v4_book_run, "_run_one_chapter", fake_run_one)

    result = v4_book_run.run_book(
        memory_dir=memory,
        chapter_ids=["0001"],
        chapter_html_pattern=str(src_dir / "{chapter_id}.html"),
        out_base=out_base,
    )

    # Accepted chapter -> index built and recorded.
    assert result["chapters"][0]["terminal_status"] == "complete"
    assert result["chapters"][0]["index_built"] is True
    index_path = memory / "chapter_index.json"
    assert index_path.exists()
    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert "0001" in index
    assert "Blake Thorburn" in index["0001"]["characters"]


def test_book_run_skips_chapter_index_for_failed_chapter(tmp_path, monkeypatch):
    from pact_full_pipeline_runner_v1 import v4_book_run

    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "book_memory.json").write_text(
        json.dumps(_book_memory(), ensure_ascii=False), encoding="utf-8",
    )
    (memory / "glossary.json").write_text("{}", encoding="utf-8")
    (memory / "observations.json").write_text("{}", encoding="utf-8")
    (memory / "chapter_index.json").write_text("{}", encoding="utf-8")

    out_base = tmp_path / "out"
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "0001.html").write_text(
        "<html><body><p id='p1'>Blake walked alone.</p></body></html>",
        encoding="utf-8",
    )
    out_dir = out_base / "chapter_0001"
    out_dir.mkdir(parents=True)
    (out_dir / "selection_results.json").write_text(
        json.dumps({"chapter_id": "0001", "results": []}), encoding="utf-8",
    )
    (out_dir / "strict_chapter_trial_record.json").write_text(
        json.dumps({"chapter_id": "0001", "step8": {"status": "failed"}}),
        encoding="utf-8",
    )
    (out_dir / "translations.json").write_text("{}", encoding="utf-8")
    (out_dir / "chunk_plan.json").write_text(
        json.dumps({"artifact": "pact-v4-chunk-plan/v1", "snapshot_hash": "t",
                    "plan_hash": "t", "chunks": []}), encoding="utf-8",
    )

    def fake_run_one(*args, **kwargs):
        return {"status": "ok"}

    monkeypatch.setattr(v4_book_run, "_run_one_chapter", fake_run_one)

    result = v4_book_run.run_book(
        memory_dir=memory,
        chapter_ids=["0001"],
        chapter_html_pattern=str(src_dir / "{chapter_id}.html"),
        out_base=out_base,
    )

    assert result["chapters"][0]["terminal_status"] == "failed"
    assert result["chapters"][0]["index_built"] is False
    # Strict boundary (finding 6) ensures file always exists after init (empty), but entry must not be built for failed chapter
    assert (memory / "chapter_index.json").exists()
    import json as _j
    idx = _j.loads((memory / "chapter_index.json").read_text())
    assert "0001" not in idx


def test_book_run_two_accepted_chapters_full_memory_presence_based(
    tmp_path, monkeypatch,
):
    """Presence-based selection (remove-pre-chapter-filter): with two
    ACCEPTED chapters, each chapter's prompt is built from the full
    accumulated book_memory filtered only by Rule 1 source presence.

    The book_memory.json below carries facts for BOTH chapters (full
    accumulated state). ``build_index_file`` now uses a non-filtering
    shallow copy, so chapter_index["0002"] includes both facts when their
    keys are present in the chapter source, and the same source-absent
    exclusion still applies.
    """
    from pact_full_pipeline_runner_v1 import v4_book_run

    memory = tmp_path / "memory"
    memory.mkdir()
    book_memory = {
        "schema": "pact-v4-book-memory/v2",
        "book_memory_policy_version": "book-memory-policy/v1",
        "policy": {"explicit_deny": [], "explicit_allow": {}, "aliases": {}, "approved_terms": [], "generic_patterns_version": "generic-memory-reject/v1"},
        "pov": {"gender": "male", "source_name": "Blake Thorburn"},
        "characters": {
            "Blake Thorburn": {
                "gender": "male", "chapters": ["0001"], "variants": {"Blake": 1},
            },
            "Rose": {
                "gender": "female", "chapters": ["0002"], "variants": {"Rose": 1},
            },
        },
        "facts": [
            {
                "fact": "Blake is the narrator.",
                "keys": ["Blake Thorburn"], "chapter": "0001",
            },
            {
                "fact": "Rose is Blake's sister.",
                "keys": ["Rose"], "chapter": "0002",
            },
        ],
    }
    (memory / "book_memory.json").write_text(
        json.dumps(book_memory, ensure_ascii=False), encoding="utf-8",
    )
    (memory / "glossary.json").write_text("{}", encoding="utf-8")
    (memory / "observations.json").write_text("{}", encoding="utf-8")
    (memory / "chapter_index.json").write_text("{}", encoding="utf-8")

    out_base = tmp_path / "out"
    src_dir = tmp_path / "src"
    src_dir.mkdir()

    def _write_chapter(chapter_id: str, html: str) -> None:
        (src_dir / f"{chapter_id}.html").write_text(
            html, encoding="utf-8",
        )
        out_dir = out_base / f"chapter_{chapter_id}"
        out_dir.mkdir(parents=True)
        (out_dir / "selection_results.json").write_text(
            json.dumps({"chapter_id": chapter_id, "results": [
                {"chunk_id": f"chunk{chapter_id}", "status": "selected"},
            ]}),
            encoding="utf-8",
        )
        (out_dir / "strict_chapter_trial_record.json").write_text(
            json.dumps({"chapter_id": chapter_id,
                        "step8": {"status": "complete"}}),
            encoding="utf-8",
        )
        (out_dir / "translations.json").write_text(
            json.dumps({"p00001": "Блэйк шёл.", "p00002": "Роуз улыбнулась."}),
            encoding="utf-8",
        )
        (out_dir / "chunk_plan.json").write_text(
            json.dumps({"artifact": "pact-v4-chunk-plan/v1",
                        "snapshot_hash": "t", "plan_hash": "t", "chunks": [
                            {"chunk_id": f"chunk{chapter_id}",
                             "snapshot_hash": "t", "pids": ["p00001", "p00002"],
                             "word_counts": [],
                             "context": {"left_ru": "", "right_en": []},
                             "undersized_exception": False},
                        ]}),
            encoding="utf-8",
        )

    # 0002's source names BOTH Blake and Rose: with full-memory presence-based
    # selection BOTH facts are present in the 0002 entry (both keys occur
    # in the chapter source).
    _write_chapter("0001", "<html><body><p id='p1'>Blake walked.</p></body></html>")
    _write_chapter(
        "0002",
        "<html><body><p id='p1'>Blake saw Rose at the gate.</p></body></html>",
    )

    def fake_run_one(*args, **kwargs):
        return {"status": "ok"}

    monkeypatch.setattr(v4_book_run, "_run_one_chapter", fake_run_one)

    def _run_and_check() -> None:
        result = v4_book_run.run_book(
            memory_dir=memory,
            chapter_ids=["0001", "0002"],
            chapter_html_pattern=str(src_dir / "{chapter_id}.html"),
            out_base=out_base,
        )
        assert [r["terminal_status"] for r in result["chapters"]] == [
            "complete", "complete",
        ]
        index_path = memory / "chapter_index.json"
        assert index_path.exists()
        index = json.loads(index_path.read_text(encoding="utf-8"))
        assert "0002" in index
        entry_0002 = index["0002"]
        # Both facts are present when their keys are in the chapter source (full memory, Rule 1).
        assert "Blake is the narrator." in entry_0002["facts"]
        assert "Rose is Blake's sister." in entry_0002["facts"]
        # Both characters are present when named in the source.
        assert "Rose" in entry_0002["characters"]
        assert "Blake Thorburn" in entry_0002["characters"]
        # The PROMPT for 0002 carries both facts when source-present.
        from pact_v4.runtime.bible_renderer import render_bible_section

        prompt_0002 = render_bible_section(
            "0002", index, json.loads(
                (memory / "book_memory.json").read_text(encoding="utf-8")
            )
        )
        assert "Blake is the narrator." in prompt_0002
        assert "Rose is Blake's sister." in prompt_0002
        # 0001's own entry reflects presence-based selection: its fact IS
        # in 0001's entry because its key is in 0001's source.
        assert "Blake is the narrator." in index["0001"]["facts"]

    # First run (both chapters accepted) and a RERUN: the authoritative
    # chapter_index must be identical — full-memory presence-based.
    _run_and_check()
    _run_and_check()
