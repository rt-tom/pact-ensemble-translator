"""V5 slice-1: external EPUB -> chapter splitter (outside the pipeline).

Reads chapter order from the EPUB spine (OPF) and emits, into the book's
output source directory ONLY:

* ``NNNN_<slug>.html`` — normalized chapter HTML compatible with the
  existing ``phase0b/source_html.py`` parser (leaf blocks ``p/h/li/
  blockquote``, inline ``em/strong/i/b/a`` preserved byte-for-byte from
  the EPUB body);
* ``manifest.json`` — the shared source-manifest contract
  (``pact_v4.phase0b.book_manifest``): per-chapter
  ``file/order/en_title/pov/parent/illustrations`` plus book-level
  ``epub_hash/splitter_version``.

POV is captured from the structural first-line marker
(``<p><strong>Name</strong></p>`` — Verona/Lucy/Prologue/SB) into the
manifest WITHOUT changing the HTML. Illustration slots are
``{id, anchor}`` with an ``[ILLUSTRATION id]`` placeholder appended to
the chapter HTML; an imageless EPUB (like Pale) yields a valid empty
list and untouched bodies.

Determinism: re-running on the same EPUB produces byte-identical HTML
and manifest (no timestamps, no set iteration, sorted JSON, ``\\n``
line endings).

Long chapters (95-125K, the Pale tail) are post-pilot tech debt: the
splitter emits them deterministically as SINGLE chapters (no scene
splitting in slice-1).

The splitter knows nothing about pipeline state: it imports no
snapshot/state/ledger modules and touches only the EPUB input and the
output directory. The pipeline knows nothing about EPUBs: it reads only
the manifest.
"""
from __future__ import annotations

import argparse
import hashlib
import html as _html
import re
import sys
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as _ET

from pact_v4.phase0b.book_manifest import (
    SPLITTER_VERSION,
    IllustrationSlot,
    ManifestChapter,
    SourceManifest,
    check_no_symlink_chain,
)

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_BODY_RE = re.compile(r"<body[^>]*>(.*)</body\s*>", re.DOTALL | re.IGNORECASE)
_POV_MARKER_RE = re.compile(r"^\s*<strong>([^<>]+)</strong>\s*$", re.IGNORECASE)


class SplitterError(ValueError):
    """Fail-closed splitter error (no partial output is left behind)."""


@dataclass
class _ChapterScan:
    en_title: str
    pov: Optional[str]
    image_srcs: list = field(default_factory=list)


class _XhtmlScanner(HTMLParser):
    """Single-pass stdlib scan: <title>, first h1/h2, first <p> inner HTML, <img> srcs."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self._in_title = False
        self._title_done = False
        self.heading: Optional[str] = None
        self._in_heading: Optional[str] = None
        self._heading_parts: list[str] = []
        self.first_p_inner: Optional[str] = None
        self._in_first_p = False
        self._first_p_parts: list[str] = []
        self._p_seen = False
        self.image_srcs: list[str] = []
        self._tag_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        low = tag.lower()
        self._tag_stack.append(low)
        if low == "title" and not self._title_done:
            self._in_title = True
        elif low in ("h1", "h2") and self.heading is None and self._in_heading is None:
            self._in_heading = low
            self._heading_parts = []
        elif low == "p" and not self._p_seen:
            self._p_seen = True
            self._in_first_p = True
            self._first_p_parts = []
        elif low == "img":
            for name, value in attrs:
                if name.lower() == "src" and value:
                    self.image_srcs.append(value)
                    break
        if self._in_first_p and low != "p":
            self._first_p_parts.append(self.get_starttag_text() or "")

    def handle_endtag(self, tag: str) -> None:
        low = tag.lower()
        if self._tag_stack and self._tag_stack[-1] == low:
            self._tag_stack.pop()
        if low == "title" and self._in_title:
            self._in_title = False
            self._title_done = True
        elif low == self._in_heading:
            text = "".join(self._heading_parts).strip()
            if text and self.heading is None:
                self.heading = text
            self._in_heading = None
        elif low == "p" and self._in_first_p:
            self.first_p_inner = "".join(self._first_p_parts)
            self._in_first_p = False
        elif self._in_first_p:
            self._first_p_parts.append(f"</{tag}>")

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        low = tag.lower()
        if low == "img":
            for name, value in attrs:
                if name.lower() == "src" and value:
                    self.image_srcs.append(value)
                    break
        if self._in_first_p:
            self._first_p_parts.append(self.get_starttag_text() or "")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._in_heading is not None:
            self._heading_parts.append(data)
        if self._in_first_p:
            self._first_p_parts.append(data)

    def handle_entityref(self, name: str) -> None:
        text = f"&{name};"
        if self._in_title:
            self.title_parts.append(text)
        if self._in_heading is not None:
            self._heading_parts.append(text)
        if self._in_first_p:
            self._first_p_parts.append(text)

    def handle_charref(self, name: str) -> None:
        text = f"&#{name};"
        if self._in_title:
            self.title_parts.append(text)
        if self._in_heading is not None:
            self._heading_parts.append(text)
        if self._in_first_p:
            self._first_p_parts.append(text)


def _norm_text(value: str) -> str:
    return " ".join(value.split())


def _scan_xhtml(xhtml: str, order: int) -> _ChapterScan:
    scanner = _XhtmlScanner()
    try:
        scanner.feed(xhtml)
        scanner.close()
    except Exception as exc:
        raise SplitterError(f"chapter {order}: cannot parse XHTML ({exc})") from exc
    title = _norm_text("".join(scanner.title_parts))
    heading = _norm_text(scanner.heading or "")
    en_title = title or heading or f"Chapter {order}"
    pov: Optional[str] = None
    if scanner.first_p_inner is not None:
        marker = _POV_MARKER_RE.match(scanner.first_p_inner.strip())
        if marker:
            name = _norm_text(marker.group(1))
            if name:
                pov = name
    return _ChapterScan(en_title=en_title, pov=pov, image_srcs=list(scanner.image_srcs))


def _body_inner(xhtml: str, order: int) -> str:
    match = _BODY_RE.search(xhtml)
    if match is None:
        raise SplitterError(f"chapter {order}: no <body> element in spine item")
    return match.group(1).strip("\n")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _read_opf_spine(epub_path: Path) -> tuple[list[str], dict[str, str], str]:
    """Return (spine_hrefs_in_order, image_href_to_id, opf_dir)."""
    try:
        with zipfile.ZipFile(epub_path, "r") as zf:
            names = set(zf.namelist())
            if "META-INF/container.xml" not in names:
                raise SplitterError("EPUB has no META-INF/container.xml")
            container = zf.read("META-INF/container.xml").decode("utf-8-sig")
            try:
                croot = _ET.fromstring(container)
            except _ET.ParseError as exc:
                raise SplitterError(f"EPUB container.xml malformed: {exc}") from exc
            opf_path: Optional[str] = None
            for elem in croot.iter():
                if _local_name(elem.tag) == "rootfile":
                    media = (elem.get("media-type") or "").strip()
                    full = (elem.get("full-path") or "").strip()
                    if full and (not media or media == "application/oebps-package+xml"):
                        opf_path = full.replace("\\", "/")
                        break
            if opf_path is None:
                raise SplitterError("EPUB container.xml lists no OPF rootfile")
            if opf_path not in names:
                raise SplitterError(f"EPUB OPF not in archive: {opf_path}")
            opf_raw = zf.read(opf_path).decode("utf-8-sig")
            try:
                oroot = _ET.fromstring(opf_raw)
            except _ET.ParseError as exc:
                raise SplitterError(f"EPUB OPF malformed: {exc}") from exc
            opf_dir = opf_path.rsplit("/", 1)[0] if "/" in opf_path else ""
            manifest: dict[str, tuple[str, str]] = {}
            spine_ids: list[str] = []
            for elem in oroot.iter():
                local = _local_name(elem.tag)
                if local == "item":
                    iid = (elem.get("id") or "").strip()
                    href = (elem.get("href") or "").strip()
                    media = (elem.get("media-type") or "").strip()
                    if iid and href:
                        manifest[iid] = (href.replace("\\", "/"), media)
                elif local == "itemref":
                    idref = (elem.get("idref") or "").strip()
                    if idref:
                        spine_ids.append(idref)
            if not spine_ids:
                raise SplitterError("EPUB spine is empty")
            spine_hrefs: list[str] = []
            for idref in spine_ids:
                if idref not in manifest:
                    raise SplitterError(f"EPUB spine idref missing from manifest: {idref}")
                href, media = manifest[idref]
                if media not in ("application/xhtml+xml", "text/html", "application/xml"):
                    raise SplitterError(
                        f"EPUB spine item {idref} has unsupported media-type {media!r}"
                    )
                full_href = f"{opf_dir}/{href}" if opf_dir else href
                spine_hrefs.append(full_href)
            images: dict[str, str] = {}
            for iid, (href, media) in manifest.items():
                if media.startswith("image/"):
                    full_href = f"{opf_dir}/{href}" if opf_dir else href
                    images[full_href] = href.rsplit("/", 1)[-1]
            # Read all spine blobs while the archive is open (deterministic bytes).
            blobs: list[bytes] = []
            for href in spine_hrefs:
                if href not in names:
                    raise SplitterError(f"EPUB spine entry missing from archive: {href}")
                blobs.append(zf.read(href))
    except zipfile.BadZipFile as exc:
        raise SplitterError(f"not a valid EPUB/zip: {epub_path} ({exc})") from exc
    except OSError as exc:
        raise SplitterError(f"cannot read EPUB {epub_path}: {exc}") from exc
    return blobs, spine_hrefs, images


def _slot_id(image_name: str, order: int, index: int) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", image_name.rsplit(".", 1)[0]).strip("_.") or "img"
    return f"{order:04d}-{stem}-{index}"


def split_epub(epub_path: Path, out_dir: Path, slug: str) -> SourceManifest:
    """Split EPUB into ``out_dir``; return the source manifest.

    Fail-closed: any malformed input raises ``SplitterError`` and leaves no
    partial output (files are staged then moved into place only on success).
    """
    if not _SLUG_RE.match(slug):
        raise SplitterError(f"invalid book slug {slug!r} (expected [a-z0-9-])")
    check_no_symlink_chain(epub_path)
    if not epub_path.is_file() or epub_path.is_symlink():
        raise SplitterError(f"EPUB is not a regular file: {epub_path}")
    if out_dir.is_symlink():
        raise SplitterError(f"output dir is a symlink (rejected): {out_dir}")

    epub_hash = hashlib.sha256(epub_path.read_bytes()).hexdigest()
    blobs, hrefs, images = _read_opf_spine(epub_path)

    staging = out_dir.parent / f".{out_dir.name}.splitter-staging"
    if staging.is_symlink():
        raise SplitterError(f"staging path is a symlink (rejected): {staging}")
    import shutil as _shutil

    if staging.exists():
        _shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=False)

    try:
        chapters: list[ManifestChapter] = []
        for index, (blob, href) in enumerate(zip(blobs, hrefs), start=1):
            order = index
            try:
                xhtml = blob.decode("utf-8-sig", errors="strict")
            except UnicodeDecodeError as exc:
                raise SplitterError(f"chapter {order} ({href}): not UTF-8 ({exc})") from exc
            xhtml = xhtml.replace("\r\n", "\n").replace("\r", "\n")
            scan = _scan_xhtml(xhtml, order)
            body = _body_inner(xhtml, order)
            slots: list[IllustrationSlot] = []
            base_dir = href.rsplit("/", 1)[0] if "/" in href else ""
            for img_index, src in enumerate(scan.image_srcs):
                src_norm = src.split("#", 1)[0].strip()
                if not src_norm:
                    continue
                resolved = f"{base_dir}/{src_norm}" if base_dir else src_norm
                image_name = images.get(resolved, src_norm.rsplit("/", 1)[-1])
                sid = _slot_id(image_name, order, img_index)
                slots.append(IllustrationSlot(slot_id=sid, anchor=src))
                body += f"\n<p>[ILLUSTRATION {sid}]</p>"
            fname = f"{order:04d}_{slug}.html"
            shell = (
                "<!DOCTYPE html>\n"
                '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
                f"<title>{_html.escape(scan.en_title)}</title>\n"
                "</head>\n<body>\n"
                f"{body}\n"
                "</body>\n</html>\n"
            )
            (staging / fname).write_text(shell, encoding="utf-8", newline="\n")
            chapters.append(ManifestChapter(
                file=fname,
                order=order,
                en_title=scan.en_title,
                pov=scan.pov,
                parent=None,
                illustrations=tuple(slots),
            ))
        manifest = SourceManifest(
            book_slug=slug,
            source_kind="epub",
            source_hash=epub_hash,
            source_name=epub_path.name,
            splitter_version=SPLITTER_VERSION,
            chapters=tuple(chapters),
        )
        (staging / "manifest.json").write_text(manifest.to_json(), encoding="utf-8", newline="\n")
        # Commit: move staging into place atomically-ish (out_dir must be fresh or identical).
        if out_dir.exists():
            if not out_dir.is_dir():
                raise SplitterError(f"output path exists and is not a directory: {out_dir}")
            # Refuse to overwrite a non-empty directory (never silently merge).
            if any(out_dir.iterdir()):
                raise SplitterError(f"output directory not empty (refusing to merge): {out_dir}")
        else:
            out_dir.mkdir(parents=True, exist_ok=False)
        for item in sorted(staging.iterdir(), key=lambda p: p.name):
            (item).replace(out_dir / item.name)
        _shutil.rmtree(staging, ignore_errors=True)
        return manifest
    except Exception:
        _shutil.rmtree(staging, ignore_errors=True)
        raise


def _extract_heading_title(html_text: str) -> str:
    scanner = _XhtmlScanner()
    try:
        scanner.feed(html_text)
        scanner.close()
    except Exception:
        return ""
    title = _norm_text("".join(scanner.title_parts))
    return title or _norm_text(scanner.heading or "")


def generate_directory_manifest(
    source_dir: Path,
    slug: str,
    *,
    pov_for_all: Optional[str] = None,
    manifest_out: Optional[Path] = None,
) -> SourceManifest:
    """One-time schema-versioned manifest generator for directory input.

    Used for the Pact migration (150 HTML) and to produce a reviewable
    manifest for any already-split source directory. ``NNNN_*.html``
    regular non-symlink files only; ``order`` = the ``NNNN`` number and
    must be contiguous from 1. ``en_title`` comes from ``<title>``/first
    heading; ``pov_for_all`` stamps the established narrator when known.
    """
    if not _SLUG_RE.match(slug):
        raise SplitterError(f"invalid book slug {slug!r} (expected [a-z0-9-])")
    check_no_symlink_chain(source_dir)
    if source_dir.is_symlink() or not source_dir.is_dir():
        raise SplitterError(f"source dir is not a real directory: {source_dir}")
    num_re = re.compile(r"^(\d{4})_.*\.html$")
    entries: list[tuple[int, Path]] = []
    try:
        for item in source_dir.iterdir():
            if item.is_symlink():
                raise SplitterError(f"source file is symlink (rejected): {item.name}")
            if not item.is_file():
                continue
            match = num_re.match(item.name)
            if not match:
                continue
            check_no_symlink_chain(item)
            from pact_v4.phase0b.book_manifest import _is_regular_file

            if not _is_regular_file(item):
                raise SplitterError(f"source file is not regular (rejected): {item.name}")
            entries.append((int(match.group(1)), item))
    except OSError as exc:
        raise SplitterError(f"cannot list source dir {source_dir}: {exc}") from exc
    if not entries:
        raise SplitterError(f"no NNNN_*.html chapters in {source_dir}")
    entries.sort(key=lambda e: e[0])
    numbers = [n for n, _ in entries]
    if numbers != list(range(1, len(entries) + 1)):
        raise SplitterError(
            f"chapter numbers must be contiguous from 1, got {numbers[0]}..{numbers[-1]} ({len(numbers)} files)"
        )
    if len({n for n, _ in entries}) != len(entries):
        raise SplitterError("duplicate chapter numbers in source dir")
    from pact_v4.phase0b.book_manifest import deterministic_set_hash, sha256_file  # noqa: E402

    files = [p for _, p in entries]
    set_hash = deterministic_set_hash(files)
    chapters: list[ManifestChapter] = []
    for number, path in entries:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise SplitterError(f"cannot read {path.name}: {exc}") from exc
        en_title = _extract_heading_title(text) or path.stem
        chapters.append(ManifestChapter(
            file=path.name,
            order=number,
            en_title=en_title,
            pov=pov_for_all,
            parent=None,
            illustrations=(),
        ))
    manifest = SourceManifest(
        book_slug=slug,
        source_kind="directory",
        source_hash=set_hash,
        source_name=source_dir.name,
        splitter_version=SPLITTER_VERSION,
        chapters=tuple(chapters),
    )
    if manifest_out is not None:
        check_no_symlink_chain(manifest_out.parent)
        manifest_out.write_text(manifest.to_json(), encoding="utf-8", newline="\n")
    _ = sha256_file  # (re-export guard for lint)
    return manifest


def build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="V5 slice-1 EPUB splitter (external tool — pipeline reads only its manifest output)."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_split = sub.add_parser("split-epub", help="split EPUB spine into NNNN_<slug>.html + manifest.json")
    p_split.add_argument("--epub", type=Path, required=True)
    p_split.add_argument("--out-dir", type=Path, required=True)
    p_split.add_argument("--slug", required=True)
    p_dir = sub.add_parser(
        "gen-directory-manifest",
        help="one-time schema-versioned manifest for a directory of NNNN_*.html (Pact migration)",
    )
    p_dir.add_argument("--source-dir", type=Path, required=True)
    p_dir.add_argument("--slug", required=True)
    p_dir.add_argument("--pov", default=None)
    p_dir.add_argument("--manifest-out", type=Path, required=True)
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_argparser().parse_args(argv)
    try:
        if args.command == "split-epub":
            manifest = split_epub(args.epub, args.out_dir, args.slug)
            print(f"split {len(manifest.chapters)} chapters -> {args.out_dir} (epub_hash={manifest.source_hash[:16]}...)")
        else:
            manifest = generate_directory_manifest(
                args.source_dir, args.slug, pov_for_all=args.pov, manifest_out=args.manifest_out
            )
            print(f"manifest for {len(manifest.chapters)} chapters -> {args.manifest_out} (set_hash={manifest.source_hash[:16]}...)")
    except SplitterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
