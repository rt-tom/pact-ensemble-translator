"""Book HTML renderer tests (card: HTML-рендер книги, итоговый book.html).

Minimal contract coverage:

* a chapter renders pid -> translated text with the source structure
  preserved (wrapper tag + attributes) and the translation's ``<em>``
  italics intact;
* every pid present in translations is covered (source order);
* a missing pid does not crash the renderer — the block is skipped and
  reported in ``missing_pids``;
* book assembly: chapters in order, TOC/headings from source h-tags with
  anchors.
"""
from __future__ import annotations

import json
from pathlib import Path

from pact_full_pipeline_runner_v1.v4_book_html import (
    build_book_html,
    render_book,
    render_chapter_body,
)

_SRC = """<html lang="en"><head><meta charset="utf-8"><title>C1</title></head>
<body>
<h1>The Pact</h1>
<p>He met <em>Blake</em> at the gate.</p>
<p>Blake waited outside.</p>
</body></html>"""


def _translations() -> dict:
    return {
        "p00001": "Узы",
        "p00002": "Он встретил <em>Блэйка</em> у ворот.",
        "p00003": "Блэйк ждал снаружи.",
    }


# ---------------------------------------------------------------------------
# Chapter rendering
# ---------------------------------------------------------------------------


def test_render_chapter_preserves_structure_and_em():
    body, report = render_chapter_body(_SRC, _translations(), chapter_id="0001")
    # Wrapper tags preserved: h1 + two paragraphs in source order.
    assert "<h1" in body and "<p" in body
    assert body.index("Узы") < body.index("Блэйк")  # source order
    # The translation's <em> survives as a real tag.
    assert "<em>Блэйка</em>" in body
    # The heading got an anchor id for the TOC.
    assert 'id="ch-0001-h1"' in body
    # Full coverage: every source pid rendered, nothing missing.
    assert report["blocks_total"] == 3
    assert report["rendered"] == 3
    assert report["missing_pids"] == []
    assert report["headings"] == [
        {"level": 1, "text": "Узы", "anchor": "ch-0001-h1"},
    ]


def test_render_chapter_skips_missing_pid_without_crash():
    translations = _translations()
    del translations["p00002"]  # p00002 missing
    body, report = render_chapter_body(_SRC, translations, chapter_id="0001")
    assert report["missing_pids"] == ["p00002"]
    assert report["rendered"] == 2
    # The missing block (p00002, "Он встретил Блэйка у ворот.") is skipped —
    # its distinctive text is absent…
    assert "встретил" not in body
    # …while the preceding heading and the FOLLOWING paragraph (p00003,
    # which legitimately still contains "Блэйк") render.
    assert "Узы" in body and "снаружи" in body
    assert "Блэйк" in body
    # Headings only cover rendered heading blocks.
    assert report["headings"][0]["anchor"] == "ch-0001-h1"


def test_render_chapter_empty_translation_skipped():
    translations = _translations()
    translations["p00002"] = "   "
    _body, report = render_chapter_body(_SRC, translations, chapter_id="0001")
    assert report["missing_pids"] == ["p00002"]


def test_render_chapter_empty_source_renders_nothing():
    body, report = render_chapter_body("", {}, chapter_id="0001")
    assert body == ""
    assert report == {
        "blocks_total": 0, "rendered": 0, "missing_pids": [], "headings": [],
    }


def test_render_chapter_sanitizes_executable_markup():
    """Security regression: a translations.json value must never emit
    executable/arbitrary markup into book.html — only the inline tags Phase
    5 restores (em/strong/i/b/a) survive, everything else is unwrapped to
    inert text and unsafe attributes are dropped."""
    translations = {
        "p00001": "Узы",
        "p00002": ("Он встретил <em onclick=\"alert(1)\">Блэйка</em> "
                   "<script>alert(1)</script> <a href=\"javascript:alert(1)\">ссылка</a> "
                   "<b>жирный</b> <iframe src=\"x\"></iframe> у ворот."),
        "p00003": "Блэйк ждал снаружи.",
    }
    body, report = render_chapter_body(_SRC, translations, chapter_id="0001")
    assert report["missing_pids"] == []
    # The allowed <em> survives (without its event-handler attribute)…
    assert "<em>Блэйка</em>" in body
    assert "onclick" not in body
    # …the <b> inline tag is allowed…
    assert "<b>жирный</b>" in body
    # …while executable/foreign markup is unwrapped to inert text, never
    # emitted as live tags.
    assert "<script>" not in body and "</script>" not in body
    assert "<iframe" not in body
    assert "href=\"javascript:" not in body
    # The unsafe URLs' visible text is still present (content preserved).
    assert "alert(1)" in body
    assert "ссылка" in body


# ---------------------------------------------------------------------------
# Book assembly
# ---------------------------------------------------------------------------


def test_build_book_html_chapters_in_order_with_toc():
    ch1_body, ch1_report = render_chapter_body(_SRC, _translations(), chapter_id="0001")
    ch2_body, ch2_report = render_chapter_body(
        "<h1>Chapter Two</h1><p>Text two.</p>",
        {"p00001": "Глава вторая", "p00002": "Текст второй."},
        chapter_id="0002",
    )
    html_doc = build_book_html([
        {"chapter_id": "0001", "body_html": ch1_body, "headings": ch1_report["headings"]},
        {"chapter_id": "0002", "body_html": ch2_body, "headings": ch2_report["headings"]},
    ], title="Тестовая книга")

    assert html_doc.startswith("<!DOCTYPE html>")
    assert "<title>Тестовая книга</title>" in html_doc
    # TOC from source h-tags, nested by level, with anchors.
    assert '<nav id="toc">' in html_doc
    assert '<a href="#ch-0001-h1">Узы</a>' in html_doc
    assert '<a href="#ch-0002-h1">Глава вторая</a>' in html_doc
    # Chapters in order, each in its own section.
    assert html_doc.index('id="chapter-0001"') < html_doc.index('id="chapter-0002"')
    assert "Глава вторая" in html_doc


def test_build_book_html_no_toc_without_headings():
    body, report = render_chapter_body(
        "<p>No headings here.</p>", {"p00001": "Без заголовков."},
        chapter_id="0001",
    )
    html_doc = build_book_html([
        {"chapter_id": "0001", "body_html": body, "headings": report["headings"]},
    ])
    assert '<nav id="toc">' not in html_doc


# ---------------------------------------------------------------------------
# P1 АРКИ: deterministic arc-name substitution in headings
# ---------------------------------------------------------------------------


def test_arc_names_substitute_leading_arc_key_in_heading():
    """P1 АРКИ (owner decision 2026-08-14): a heading like 'Bonds 1.3'
    becomes 'Узы 1.3' deterministically from arc_names.json — the renderer
    never relies on the model for the arc title."""
    arc_names = {"Bonds": "Узы", "Execution": "Казнь"}
    _src = "<h1>Bonds 1.3</h1><p>Text.</p>"
    _tr = {"p00001": "Узы 1.3", "p00002": "Текст."}
    body, report = render_chapter_body(
        _src, _tr, chapter_id="0001", arc_names=arc_names,
    )
    assert report["headings"] == [
        {"level": 1, "text": "Узы 1.3", "anchor": "ch-0001-h1"},
    ]


def test_arc_names_heading_exact_match_and_case_insensitive():
    arc_names = {"Bonds": "Узы"}
    # Exact match (no number suffix) and lowercase source heading.
    _body, report = render_chapter_body(
        "<h1>BONDS</h1><p>X.</p>",
        {"p00001": "Узы", "p00002": "Икс."},
        chapter_id="0001", arc_names=arc_names,
    )
    assert report["headings"][0]["text"] == "Узы"


def test_arc_names_no_mapping_leaves_heading_unchanged():
    body, report = render_chapter_body(
        "<h1>Prologue</h1><p>X.</p>",
        {"p00001": "Пролог", "p00002": "Икс."},
        chapter_id="0001",
        arc_names={"Bonds": "Узы"},
    )
    assert report["headings"][0]["text"] == "Пролог"


def test_arc_names_none_and_unknown_key_unchanged():
    # No mapping at all -> unchanged.
    _body, report = render_chapter_body(
        "<h1>Bonds 1.1</h1><p>X.</p>",
        {"p00001": "Узы 1.1", "p00002": "Икс."},
        chapter_id="0001",
    )
    assert report["headings"][0]["text"] == "Узы 1.1"


# ---------------------------------------------------------------------------
# Disk assembly (render_book) + missing-pid reporting
# ---------------------------------------------------------------------------


def _write_artifacts(tmp_path: Path) -> dict:
    out_base = tmp_path / "out"
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True)
    (src_dir / "0001.html").write_text(_SRC, encoding="utf-8")
    (src_dir / "0002.html").write_text(
        "<h1>Chapter Two</h1><p>Text two.</p>", encoding="utf-8",
    )
    ch1 = out_base / "chapter_0001"
    ch1.mkdir(parents=True)
    (ch1 / "translations.json").write_text(
        json.dumps(_translations(), ensure_ascii=False), encoding="utf-8",
    )
    ch2 = out_base / "chapter_0002"
    ch2.mkdir(parents=True)
    (ch2 / "translations.json").write_text(
        json.dumps({"p00001": "Глава вторая", "p00002": "Текст второй."},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    return {"out_base": out_base, "src_dir": src_dir}


def test_render_book_writes_book_html_and_report(tmp_path: Path):
    paths = _write_artifacts(tmp_path)
    report = render_book(
        out_base=paths["out_base"],
        chapter_ids=["0001", "0002"],
        chapter_html_pattern=str(paths["src_dir"] / "{chapter_id}.html"),
        title="Тестовая книга",
    )
    book_path = paths["out_base"] / "book.html"
    assert report["book_path"] == str(book_path)
    assert book_path.exists()
    content = book_path.read_text(encoding="utf-8")
    assert "Тестовая книга" in content
    assert "Блэйк" in content and "Глава вторая" in content
    assert report["errors"] == []
    assert len(report["chapters"]) == 2
    # Report file written next to the book.
    assert (paths["out_base"] / "book_html_report.json").exists()


def test_render_book_missing_pid_warns_but_succeeds(tmp_path: Path):
    paths = _write_artifacts(tmp_path)
    # Drop p00002 from chapter 0001's translations on disk.
    ch1_tr = json.loads(
        (paths["out_base"] / "chapter_0001" / "translations.json")
        .read_text(encoding="utf-8")
    )
    del ch1_tr["p00002"]
    (paths["out_base"] / "chapter_0001" / "translations.json").write_text(
        json.dumps(ch1_tr, ensure_ascii=False), encoding="utf-8",
    )

    report = render_book(
        out_base=paths["out_base"],
        chapter_ids=["0001"],
        chapter_html_pattern=str(paths["src_dir"] / "{chapter_id}.html"),
    )
    assert report["errors"] == []
    assert any("p00002" in w for w in report["warnings"])
    book_content = (paths["out_base"] / "book.html").read_text(encoding="utf-8")
    # The skipped block's distinctive text is absent; the following
    # paragraph (which legitimately still contains "Блэйк") is retained.
    assert "встретил" not in book_content
    assert "Узы" in book_content and "снаружи" in book_content


def test_render_book_missing_chapter_is_error_not_crash(tmp_path: Path):
    paths = _write_artifacts(tmp_path)
    report = render_book(
        out_base=paths["out_base"],
        chapter_ids=["0001", "9999"],  # 9999 has no source HTML
        chapter_html_pattern=str(paths["src_dir"] / "{chapter_id}.html"),
    )
    assert len(report["errors"]) == 1
    assert "9999" in report["errors"][0]
    # The good chapter still rendered.
    book_content = (paths["out_base"] / "book.html").read_text(encoding="utf-8")
    assert "Узы" in book_content


def test_render_book_corrupt_translations_warns_but_succeeds(tmp_path: Path):
    paths = _write_artifacts(tmp_path)
    (paths["out_base"] / "chapter_0002" / "translations.json").write_text(
        "{not json", encoding="utf-8",
    )
    report = render_book(
        out_base=paths["out_base"],
        chapter_ids=["0001", "0002"],
        chapter_html_pattern=str(paths["src_dir"] / "{chapter_id}.html"),
    )
    assert report["errors"] == []
    assert any("0002" in w for w in report["warnings"])
    book_content = (paths["out_base"] / "book.html").read_text(encoding="utf-8")
    assert "Узы" in book_content  # chapter 0001 still rendered


def test_render_book_script_value_never_reaches_output(tmp_path: Path):
    """Security regression at the disk-assembly level: a translations.json
    value carrying a <script> tag must not appear as a live tag in
    book.html (the reviewer's repro: `alert(1)` in a final value)."""
    paths = _write_artifacts(tmp_path)
    (paths["out_base"] / "chapter_0001" / "translations.json").write_text(
        json.dumps({
            "p00001": "Узы",
            "p00002": "<script>alert(1)</script> текст",
            "p00003": "Блэйк ждал снаружи.",
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    report = render_book(
        out_base=paths["out_base"],
        chapter_ids=["0001"],
        chapter_html_pattern=str(paths["src_dir"] / "{chapter_id}.html"),
    )
    assert report["errors"] == []
    book_content = (paths["out_base"] / "book.html").read_text(encoding="utf-8")
    assert "<script>" not in book_content
    assert "</script>" not in book_content
    # The visible text survives as inert text.
    assert "alert(1)" in book_content


# ---------------------------------------------------------------------------
# V4.1 whole-chapter mode: run_<label>/translations.json + records
# ---------------------------------------------------------------------------


def _write_v41_runs(tmp_path: Path) -> dict:
    """Two v4.1 whole-chapter run dirs (run_002, run_001) with records.

    run_001 carries chapter_id ``0001``, run_002 carries chapter_id
    ``0002`` — deliberately shuffled vs. the glob sort so chapter order
    must come from ``chapter_ids``, not from directory-name order.
    """
    out_base = tmp_path / "out"
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True)
    (src_dir / "0001.html").write_text(_SRC, encoding="utf-8")
    (src_dir / "0002.html").write_text(
        "<h1>Chapter Two</h1><p>Text two.</p>", encoding="utf-8",
    )

    def _write_run(name: str, chapter_id: str, translations: dict) -> Path:
        run_dir = out_base / name
        run_dir.mkdir(parents=True)
        (run_dir / "translations.json").write_text(
            json.dumps(translations, ensure_ascii=False), encoding="utf-8",
        )
        (run_dir / "strict_chapter_trial_record.json").write_text(
            json.dumps({
                "schema": "pact-v4-strict-chapter-trial/v2",
                "run_label": f"v4.1-{name}",
                "chapter_id": chapter_id,
                "identities": {},
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        return run_dir

    run_001 = _write_run("run_001", "0001", _translations())
    run_002 = _write_run("run_002", "0002", {
        "p00001": "Глава вторая", "p00002": "Текст второй.",
    })
    return {"out_base": out_base, "src_dir": src_dir,
            "run_001": run_001, "run_002": run_002}


def test_render_book_v41_run_dirs_book_order_toc_sections(tmp_path: Path):
    """Acceptance: 2+ v4.1 run dirs assemble a book with chapters in
    chapter_ids order, TOC from headings and per-chapter sections."""
    paths = _write_v41_runs(tmp_path)
    report = render_book(
        out_base=paths["out_base"],
        chapter_ids=["0001", "0002"],
        chapter_html_pattern=str(paths["src_dir"] / "{chapter_id}.html"),
        run_dirs=[paths["run_002"], paths["run_001"]],  # shuffled input
        title="Тестовая книга",
    )
    assert report["errors"] == []
    assert len(report["chapters"]) == 2
    book_path = paths["out_base"] / "book.html"
    assert report["book_path"] == str(book_path)
    content = book_path.read_text(encoding="utf-8")
    # Chapters in chapter_ids order (0001 before 0002), each in a section.
    assert content.index('id="chapter-0001"') < content.index('id="chapter-0002"')
    # TOC anchors from headings.
    assert '<a href="#ch-0001-h1">Узы</a>' in content
    assert '<a href="#ch-0002-h1">Глава вторая</a>' in content
    # Translations came from the run dirs.
    assert "Блэйк" in content and "Текст второй." in content
    # Report records point at the run dir's translations.json.
    assert "run_002" in report["chapters"][1]["translations"]
    # Report written next to the book.
    assert (paths["out_base"] / "book_html_report.json").exists()


def test_render_book_v41_run_dirs_pattern_expansion(tmp_path: Path):
    """A ``run_*`` pattern expands to the matching run dirs."""
    paths = _write_v41_runs(tmp_path)
    report = render_book(
        out_base=paths["out_base"],
        chapter_ids=["0001", "0002"],
        chapter_html_pattern=str(paths["src_dir"] / "{chapter_id}.html"),
        run_dirs=["run_*"],
    )
    assert report["errors"] == []
    assert len(report["chapters"]) == 2
    content = (paths["out_base"] / "book.html").read_text(encoding="utf-8")
    assert "Блэйк" in content and "Глава вторая" in content


def test_render_book_v41_run_dirs_chapter_id_from_record_beats_dirname(
    tmp_path: Path,
):
    """chapter_id comes from strict_chapter_trial_record.json even when the
    run dir name is unrelated (run_00X -> chapter 0003)."""
    paths = _write_v41_runs(tmp_path)
    run_dir = paths["run_001"]
    (run_dir / "strict_chapter_trial_record.json").write_text(
        json.dumps({
            "schema": "pact-v4-strict-chapter-trial/v2",
            "run_label": "v4.1-run_001",
            "chapter_id": "0003",
            "identities": {},
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    (paths["src_dir"] / "0003.html").write_text(
        "<h1>Chapter Three</h1><p>Text three.</p>", encoding="utf-8",
    )
    report = render_book(
        out_base=paths["out_base"],
        chapter_ids=["0003"],
        chapter_html_pattern=str(paths["src_dir"] / "{chapter_id}.html"),
        run_dirs=["run_001"],
    )
    assert report["errors"] == []
    assert report["chapters"][0]["chapter_id"] == "0003"
    content = (paths["out_base"] / "book.html").read_text(encoding="utf-8")
    assert 'id="chapter-0003"' in content
    # The heading block of 0003 is replaced by its translation (p00001).
    assert "Узы" in content


def test_render_book_v41_run_dirs_no_record_falls_back_to_dirname(
    tmp_path: Path,
):
    """No record/metadata: chapter_id falls back to the run dir name with a
    warning, and the chapter still renders."""
    paths = _write_v41_runs(tmp_path)
    # Strip the record; the run dir name (run_001) becomes the chapter id.
    (paths["run_001"] / "strict_chapter_trial_record.json").unlink()
    # Source HTML for the fallback chapter id (dir name) so it renders.
    (paths["src_dir"] / "run_001.html").write_text(_SRC, encoding="utf-8")
    report = render_book(
        out_base=paths["out_base"],
        chapter_ids=[],
        chapter_html_pattern=str(paths["src_dir"] / "{chapter_id}.html"),
        run_dirs=["run_001"],
    )
    assert report["errors"] == []
    assert any("run_001" in w for w in report["warnings"])
    assert report["chapters"][0]["chapter_id"] == "run_001"
    content = (paths["out_base"] / "book.html").read_text(encoding="utf-8")
    assert "Блэйк" in content


def test_render_book_v41_run_dirs_missing_run_is_error(tmp_path: Path):
    """A run dir that does not exist is a per-chapter error, not a crash —
    the remaining chapters still render (mirrors legacy missing-chapter
    behavior)."""
    paths = _write_v41_runs(tmp_path)
    report = render_book(
        out_base=paths["out_base"],
        chapter_ids=["0001"],
        chapter_html_pattern=str(paths["src_dir"] / "{chapter_id}.html"),
        run_dirs=["run_001", "run_missing"],
    )
    assert len(report["errors"]) == 1
    assert "run_missing" in report["errors"][0]
    content = (paths["out_base"] / "book.html").read_text(encoding="utf-8")
    assert "Блэйк" in content  # run_001 still rendered


def test_render_book_v41_run_dirs_translations_metadata_fallback(
    tmp_path: Path,
):
    """No record but translations.json carries chapter_id metadata (envelope
    form) — the id is taken from there."""
    paths = _write_v41_runs(tmp_path)
    run_dir = paths["run_001"]
    (run_dir / "strict_chapter_trial_record.json").unlink()
    (run_dir / "translations.json").write_text(
        json.dumps({
            "chapter_id": "0004",
            "translations": {
                "p00001": "Узы",
                "p00002": "Он встретил <em>Блэйка</em> у ворот.",
                "p00003": "Блэйк ждал снаружи.",
            },
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    (paths["src_dir"] / "0004.html").write_text(_SRC, encoding="utf-8")
    report = render_book(
        out_base=paths["out_base"],
        chapter_ids=["0004"],
        chapter_html_pattern=str(paths["src_dir"] / "{chapter_id}.html"),
        run_dirs=["run_001"],
    )
    assert report["errors"] == []
    assert report["chapters"][0]["chapter_id"] == "0004"
    content = (paths["out_base"] / "book.html").read_text(encoding="utf-8")
    assert 'id="chapter-0004"' in content
    assert "<em>Блэйка</em>" in content  # envelope translations rendered


def test_render_book_v41_run_dirs_chapter_ids_order_overrides_run_order(
    tmp_path: Path,
):
    """chapter_ids order is the book order even when run dirs are passed in
    a different order (owner fixes book order explicitly)."""
    paths = _write_v41_runs(tmp_path)
    report = render_book(
        out_base=paths["out_base"],
        chapter_ids=["0002", "0001"],
        chapter_html_pattern=str(paths["src_dir"] / "{chapter_id}.html"),
        run_dirs=["run_001", "run_002"],
    )
    assert report["errors"] == []
    content = (paths["out_base"] / "book.html").read_text(encoding="utf-8")
    assert content.index('id="chapter-0002"') < content.index('id="chapter-0001"')


def test_render_book_v41_run_dirs_no_chapter_ids_orders_by_resolved_id(
    tmp_path: Path,
):
    """RV finding (abe40c1): with ``run_dirs`` but no ``chapter_ids`` the
    book order must follow the RESOLVED chapter_id (natural sort), not the
    run-dir glob/insertion order. run_001 carries chapter_id 0002 and
    run_002 carries 0001, so glob order (run_001 first) disagrees with the
    ids — the report and book.html must still list 0001 before 0002."""
    out_base = tmp_path / "out"
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True)
    (src_dir / "0001.html").write_text(_SRC, encoding="utf-8")
    (src_dir / "0002.html").write_text(
        "<h1>Chapter Two</h1><p>Text two.</p>", encoding="utf-8",
    )

    def _write_run(name: str, chapter_id: str, translations: dict) -> Path:
        run_dir = out_base / name
        run_dir.mkdir(parents=True)
        (run_dir / "translations.json").write_text(
            json.dumps(translations, ensure_ascii=False), encoding="utf-8",
        )
        (run_dir / "strict_chapter_trial_record.json").write_text(
            json.dumps({
                "schema": "pact-v4-strict-chapter-trial/v2",
                "run_label": f"v4.1-{name}",
                "chapter_id": chapter_id,
                "identities": {},
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        return run_dir

    # Glob order run_001 < run_002, but the resolved chapter ids are the
    # reverse — the dir names deliberately disagree with the ids.
    _write_run("run_001", "0002", {
        "p00001": "Глава вторая", "p00002": "Текст второй.",
    })
    _write_run("run_002", "0001", _translations())

    report = render_book(
        out_base=out_base,
        chapter_ids=[],
        chapter_html_pattern=str(src_dir / "{chapter_id}.html"),
        run_dirs=["run_*"],
        title="Тестовая книга",
    )
    assert report["errors"] == []
    # Report chapter order follows the resolved chapter ids.
    assert [ch["chapter_id"] for ch in report["chapters"]] == ["0001", "0002"]
    # book.html sections follow the same order.
    content = (out_base / "book.html").read_text(encoding="utf-8")
    assert content.index('id="chapter-0001"') < content.index('id="chapter-0002"')
    # Each chapter's translations come from the run dir whose record carries
    # that id (0001 -> run_002, 0002 -> run_001).
    assert "run_002" in report["chapters"][0]["translations"]
    assert "run_001" in report["chapters"][1]["translations"]
