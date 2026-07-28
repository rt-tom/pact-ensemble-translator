from __future__ import annotations

import io
import zipfile
from pathlib import Path

from pact_v4.phase0b.reference_epub import (
    load_reference_from_epub,
    load_reference_from_path,
    parse_reference_xhtml,
)


def test_parse_reference_produces_ordered_segments(ru_xhtml: str) -> None:
    segments = parse_reference_xhtml(ru_xhtml)
    assert [s.index for s in segments] == [1, 2, 3, 4, 5, 6]
    assert segments[0].tag == "h1"
    assert segments[-1].tag == "blockquote"
    assert "геометрию" in segments[4].text


def test_epub_entry_extraction(tmp_path: Path, ru_xhtml: str) -> None:
    epub_path = tmp_path / "sample.epub"
    with zipfile.ZipFile(epub_path, "w") as z:
        z.writestr("EPUB/chapter_044.xhtml", ru_xhtml.encode("utf-8"))
    segments, sha = load_reference_from_epub(
        epub_path, "EPUB/chapter_044.xhtml",
    )
    assert len(segments) == 6
    assert len(sha) == 64


def test_direct_path_extraction(tmp_path: Path, ru_xhtml: str) -> None:
    p = tmp_path / "chapter.xhtml"
    p.write_text(ru_xhtml, encoding="utf-8")
    segments, sha = load_reference_from_path(p)
    assert [s.tag for s in segments][:2] == ["h1", "p"]
    assert len(sha) == 64


def test_forward_slash_normalisation(tmp_path: Path, ru_xhtml: str) -> None:
    epub_path = tmp_path / "sample.epub"
    with zipfile.ZipFile(epub_path, "w") as z:
        z.writestr("EPUB/chapter_044.xhtml", ru_xhtml.encode("utf-8"))
    # Callers may pass Windows-style separators; the extractor should
    # normalise them before opening the zip entry.
    segments, _sha = load_reference_from_epub(
        epub_path, r"EPUB\chapter_044.xhtml",
    )
    assert len(segments) == 6
