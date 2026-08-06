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
