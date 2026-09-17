"""V5 slice-1: EPUB splitter contract tests (synthetic fixtures, model-free).

No live EPUB, no pipeline, no model calls. The pilot Pale EPUB (313
chapters) verification is owner-executed on RT (§4); these tests prove
the contract the pilot relies on: spine order, byte-determinism, POV
capture without HTML changes, illustration slots, parser compatibility,
long chapters as single files, and no pipeline-state contact.
"""
from __future__ import annotations

import ast
import json
import zipfile
from pathlib import Path

import pytest

from pact_v4.phase0b.book_manifest import load_manifest, validate_source_manifest
from pact_v4.phase0b.epub_splitter import (
    SplitterError,
    generate_directory_manifest,
    split_epub,
)


def _xhtml(title, heading, first_p_strong=None, body_paras=("First para.", "Second para."), img=None):
    marker = f"<p><strong>{first_p_strong}</strong></p>" if first_p_strong else ""
    img_tag = f'<p><img src="{img}" alt="pic"/></p>' if img else ""
    paras = "\n".join(f"<p>{p}</p>" for p in body_paras)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<!DOCTYPE html>\n"
        f'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>{title}</title></head>\n'
        f"<body>\n{marker}\n<h2>{heading}</h2>\n{img_tag}\n{paras}\n</body></html>\n"
    )


def _build_epub(path: Path, chapters: list[tuple[str, str]], with_image=False) -> Path:
    """chapters: [(title/heading, pov-or-None)]. Returns epub path."""
    opf_items = []
    spine_refs = []
    files = {}
    for i, (title, pov) in enumerate(chapters, start=1):
        name = f"ch{i}.xhtml"
        img = "img/pic.png" if (with_image and i == 2) else None
        files[f"OEBPS/{name}"] = _xhtml(title, title, first_p_strong=pov, img=img)
        opf_items.append(f'<item id="c{i}" href="{name}" media-type="application/xhtml+xml"/>')
        spine_refs.append(f'<itemref idref="c{i}"/>')
    if with_image:
        opf_items.append('<item id="img1" href="img/pic.png" media-type="image/png"/>')
        files["OEBPS/img/pic.png"] = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">\n'
        "<metadata><dc:title xmlns:dc=\"http://purl.org/dc/elements/1.1/\">T</dc:title></metadata>\n"
        f"<manifest>{''.join(opf_items)}</manifest>\n"
        f"<spine>{''.join(spine_refs)}</spine>\n</package>\n"
    )
    files["OEBPS/content.opf"] = opf
    files["META-INF/container.xml"] = (
        '<?xml version="1.0"?>\n<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>\n</container>'
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data if isinstance(data, bytes) else data.encode("utf-8"))
    return path


@pytest.fixture()
def pilot_like_epub(tmp_path):
    return _build_epub(
        tmp_path / "pale.epub",
        [("0.0", "Prologue"), ("1.0", "Verona"), ("1.1", "Verona"), ("SB", "SB")],
    )


# ---------------------------------------------------------------------------
# Core contract
# ---------------------------------------------------------------------------

def test_spine_order_and_manifest_shape(tmp_path, pilot_like_epub):
    out = tmp_path / "src"
    manifest = split_epub(pilot_like_epub, out, "pale")
    assert [c.file for c in manifest.chapters] == [
        "0001_pale.html", "0002_pale.html", "0003_pale.html", "0004_pale.html",
    ]
    assert [c.order for c in manifest.chapters] == [1, 2, 3, 4]
    assert [c.en_title for c in manifest.chapters] == ["0.0", "1.0", "1.1", "SB"]
    assert manifest.source_kind == "epub" and manifest.book_slug == "pale"
    on_disk = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert on_disk["epub_hash"] == manifest.source_hash
    assert "set_hash" not in on_disk


def test_rerun_is_byte_identical(tmp_path, pilot_like_epub):
    out1, out2 = tmp_path / "a", tmp_path / "b"
    split_epub(pilot_like_epub, out1, "pale")
    split_epub(pilot_like_epub, out2, "pale")
    for f1 in sorted(out1.iterdir()):
        f2 = out2 / f1.name
        assert f2.exists(), f1.name
        assert f1.read_bytes() == f2.read_bytes(), f1.name


def test_pov_capture_leaves_html_unchanged(tmp_path, pilot_like_epub):
    out = tmp_path / "src"
    manifest = split_epub(pilot_like_epub, out, "pale")
    assert [c.pov for c in manifest.chapters] == ["Prologue", "Verona", "Verona", "SB"]
    html1 = (out / "0002_pale.html").read_text(encoding="utf-8")
    assert "<strong>Verona</strong>" in html1  # marker line preserved verbatim
    # Chapter without a marker yields null pov.
    epub2 = _build_epub(tmp_path / "b.epub", [("1", None)])
    out_b = tmp_path / "srcb"
    manifest_b = split_epub(epub2, out_b, "pale")
    assert manifest_b.chapters[0].pov is None


def test_imageless_epub_yields_valid_empty_slots(tmp_path, pilot_like_epub):
    out = tmp_path / "src"
    manifest = split_epub(pilot_like_epub, out, "pale")
    assert all(c.illustrations == () for c in manifest.chapters)
    assert "[ILLUSTRATION" not in (out / "0001_pale.html").read_text(encoding="utf-8")


def test_image_slots_and_placeholder(tmp_path):
    epub = _build_epub(tmp_path / "img.epub", [("A", "Verona"), ("B", None)], with_image=True)
    out = tmp_path / "src"
    manifest = split_epub(epub, out, "img")
    assert manifest.chapters[0].illustrations == ()
    assert len(manifest.chapters[1].illustrations) == 1
    slot = manifest.chapters[1].illustrations[0]
    html = (out / "0002_img.html").read_text(encoding="utf-8")
    assert f"[ILLUSTRATION {slot.slot_id}]" in html
    assert slot.anchor == "img/pic.png"


def test_output_parses_with_source_html(tmp_path, pilot_like_epub):
    """Splitter output must feed phase0b/source_html.py cleanly (task §1.3)."""
    from pact_v4.phase0b.source_html import parse_source_html

    out = tmp_path / "src"
    manifest = split_epub(pilot_like_epub, out, "pale")
    for chapter in manifest.chapters:
        blocks = parse_source_html((out / chapter.file).read_text(encoding="utf-8"))
        assert blocks, chapter.file
    # Imageless placeholder-free chapters keep their POV marker as a block.
    blocks = parse_source_html((out / "0002_pale.html").read_text(encoding="utf-8"))
    assert blocks[0].text == "Verona"
    # Placeholder chapter also parses (image fixture).
    epub = _build_epub(tmp_path / "img.epub", [("B", None)], with_image=False)
    out2 = tmp_path / "src2"
    split_epub(epub, out2, "x")
    assert parse_source_html((out2 / "0001_x.html").read_text(encoding="utf-8"))


def test_long_chapter_emitted_as_single_chapter(tmp_path):
    """95-125K tail chapters are post-pilot tech debt: single deterministic file."""
    big = "слово " * 22000  # ~130K chars
    epub = tmp_path / "big.epub"
    with zipfile.ZipFile(epub, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("META-INF/container.xml",
                    '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                    '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        zf.writestr("OEBPS/content.opf",
                    '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
                    "<manifest><item id=\"c1\" href=\"ch1.xhtml\" media-type=\"application/xhtml+xml\"/></manifest>"
                    '<spine><itemref idref="c1"/></spine></package>')
        zf.writestr("OEBPS/ch1.xhtml",
                    f'<?xml version="1.0"?><html><head><title>Long</title></head><body><h2>Long</h2><p>{big}</p></body></html>')
    out = tmp_path / "src"
    manifest = split_epub(epub, out, "pale")
    assert len(manifest.chapters) == 1
    html = (out / "0001_pale.html").read_text(encoding="utf-8")
    assert len(html) > 95000 and big[:100] in html
    out2 = tmp_path / "src2"
    split_epub(epub, out2, "pale")
    assert (out2 / "0001_pale.html").read_bytes() == (out / "0001_pale.html").read_bytes()


def test_split_manifest_validates_against_output(tmp_path, pilot_like_epub):
    out = tmp_path / "src"
    split_epub(pilot_like_epub, out, "pale")
    manifest = load_manifest(out / "manifest.json")
    resolved = validate_source_manifest(out, manifest)
    assert sorted(resolved) == [1, 2, 3, 4]


# ---------------------------------------------------------------------------
# Fail-closed + no state contact
# ---------------------------------------------------------------------------

def test_failures_leave_no_partial_output(tmp_path):
    bad = tmp_path / "bad.epub"
    bad.write_bytes(b"not a zip")
    out = tmp_path / "src"
    with pytest.raises(SplitterError):
        split_epub(bad, out, "pale")
    assert not out.exists() or not list(out.iterdir())


def test_refuses_nonempty_out_dir(tmp_path, pilot_like_epub):
    out = tmp_path / "src"
    out.mkdir()
    (out / "existing.txt").write_text("x", encoding="utf-8")
    with pytest.raises(SplitterError, match="not empty"):
        split_epub(pilot_like_epub, out, "pale")


def test_rejects_bad_slug_and_missing_epub(tmp_path):
    epub = _build_epub(tmp_path / "a.epub", [("1", None)])
    with pytest.raises(SplitterError, match="slug"):
        split_epub(epub, tmp_path / "o", "Bad Slug!")
    with pytest.raises(SplitterError):
        split_epub(tmp_path / "missing.epub", tmp_path / "o", "pale")


def test_splitter_has_no_pipeline_state_contact():
    """Static guarantee: the splitter imports no snapshot/state/ledger modules."""
    src = (Path(__file__).resolve().parents[3] / "pact_v4" / "phase0b" / "epub_splitter.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module)
    for name in imported:
        assert "snapshot" not in name and "ledger" not in name, name
    assert "book_state" not in src and "pact_runs" not in src


# ---------------------------------------------------------------------------
# Directory-input generator (Pact migration path)
# ---------------------------------------------------------------------------

def test_gen_directory_manifest(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "0001_bonds-1-1.html").write_text("<html><head><title>Bonds 1.1</title></head><body><h1>Bonds 1.1</h1></body></html>", encoding="utf-8")
    (src / "0002_bonds-1-2.html").write_text("<html><head><title>Bonds 1.2</title></head><body><h1>Bonds 1.2</h1></body></html>", encoding="utf-8")
    manifest = generate_directory_manifest(src, "pact", pov_for_all="Blake Thorburn", manifest_out=src / "m.json")
    assert manifest.source_kind == "directory"
    assert [c.en_title for c in manifest.chapters] == ["Bonds 1.1", "Bonds 1.2"]
    assert all(c.pov == "Blake Thorburn" for c in manifest.chapters)
    assert all(c.illustrations == () for c in manifest.chapters)
    loaded = load_manifest(src / "m.json")
    assert loaded.source_hash == manifest.source_hash
    # Deterministic rerun.
    manifest2 = generate_directory_manifest(src, "pact", pov_for_all="Blake Thorburn")
    assert manifest2.source_hash == manifest.source_hash


def test_gen_directory_manifest_rejects_symlink(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "0001_a.html").write_text("<html><body><p>x</p></body></html>", encoding="utf-8")
    (src / "0002_b.html").symlink_to(src / "0001_a.html")
    with pytest.raises(SplitterError, match="[Ss]ymlink"):
        generate_directory_manifest(src, "pact")


def test_gen_directory_manifest_requires_contiguous(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "0001_a.html").write_text("<html><body><p>x</p></body></html>", encoding="utf-8")
    (src / "0003_c.html").write_text("<html><body><p>x</p></body></html>", encoding="utf-8")
    with pytest.raises(SplitterError, match="contiguous"):
        generate_directory_manifest(src, "pact")
