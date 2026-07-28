"""RU human-translation reference extraction — read-only.

Accepts either an already-extracted xhtml file or a specific entry inside an
EPUB (zip) container. Emits an ordered list of leaf-block segments. The
reference is a translation aid; it is **not** an exact-match ground truth.
"""
from __future__ import annotations

import hashlib
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from bs4 import BeautifulSoup, Tag

BLOCK_TAGS: tuple[str, ...] = (
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote",
)


@dataclass(frozen=True)
class ReferenceSegment:
    index: int
    tag: str
    text: str
    html: str


def _norm(s: str) -> str:
    return " ".join(s.split())


def _leaf_blocks(soup: BeautifulSoup, names: Sequence[str]) -> list[Tag]:
    allowed = set(names)
    out: list[Tag] = []
    for tag in soup.find_all(list(names)):
        if not isinstance(tag, Tag):
            continue
        if not _norm(tag.get_text(" ", strip=True)):
            continue
        if any(
            isinstance(c, Tag) and c.name in allowed
            for c in tag.find_all(list(names))
        ):
            continue
        out.append(tag)
    return out


def parse_reference_xhtml(xhtml_text: str) -> list[ReferenceSegment]:
    soup = BeautifulSoup(xhtml_text, "html.parser")
    segments: list[ReferenceSegment] = []
    for tag in _leaf_blocks(soup, BLOCK_TAGS):
        text = _norm(tag.get_text(" ", strip=True))
        if not text:
            continue
        segments.append(ReferenceSegment(
            index=len(segments) + 1,
            tag=tag.name,
            text=text,
            html=str(tag),
        ))
    return segments


def load_reference_from_epub(
    epub_path: Path, entry: str,
) -> tuple[list[ReferenceSegment], str]:
    """Read one xhtml entry from an EPUB (zip) file. Returns (segments, sha256)."""
    with zipfile.ZipFile(epub_path, "r") as z:
        # Normalise separators: epub uses forward slashes internally.
        normalised = entry.replace("\\", "/")
        with z.open(normalised) as f:
            raw = f.read()
    sha = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8-sig", errors="replace")
    return parse_reference_xhtml(text), sha


def load_reference_from_path(path: Path) -> tuple[list[ReferenceSegment], str]:
    """Read a standalone xhtml/html file. Returns (segments, sha256)."""
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8-sig", errors="replace")
    return parse_reference_xhtml(text), sha
