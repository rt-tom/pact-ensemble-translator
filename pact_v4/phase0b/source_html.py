"""EN source HTML → stable PID-indexed blocks. Read-only.

Mirrors the v3 leaf-block scheme (``p{index:05d}``) so downstream v4 tooling
consumes the same PID identity as v3 for direct comparability. No model
calls; no side effects.
"""
from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from bs4 import BeautifulSoup, Tag

DEFAULT_BLOCK_TAGS: tuple[str, ...] = (
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote",
)
DEFAULT_INLINE_TAGS: tuple[str, ...] = ("em", "strong", "i", "b", "a")
DEFAULT_REMOVE: tuple[str, ...] = ("script", "style", "noscript")

# Characters that indicate a paragraph opens with quoted / dashed dialogue.
_DIALOGUE_LEAD = {"“", "”", "—", "–", "«", "»",
                  "\"", "'", "‘", "’"}


@dataclass(frozen=True)
class SourceSpan:
    span_id: str
    tag: str
    text: str
    occurrence: int
    attrs: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceBlock:
    pid: str
    index: int
    tag: str
    text: str
    html: str
    structural_role: str
    inline_spans: tuple[SourceSpan, ...]
    word_count: int


def _norm(s: str) -> str:
    return " ".join(s.split())


def _leaf_blocks(soup: BeautifulSoup, names: Sequence[str]) -> list[Tag]:
    allowed = set(names)
    result: list[Tag] = []
    for tag in soup.find_all(list(names)):
        if not isinstance(tag, Tag):
            continue
        if not _norm(tag.get_text(" ", strip=True)):
            continue
        # A block is a leaf if it does not contain any of the block tags as
        # descendants. Otherwise the descendant carries the text.
        if any(
            isinstance(child, Tag) and child.name in allowed
            for child in tag.find_all(list(names))
        ):
            continue
        result.append(tag)
    return result


def _flatten_attrs(attrs: dict[str, object]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in attrs.items():
        if key not in {"href", "title", "lang", "class"}:
            continue
        if isinstance(value, list):
            out[key] = " ".join(str(v) for v in value)
        else:
            out[key] = str(value)
    return out


def _extract_inline_spans(tag: Tag, inline_tags: Sequence[str]) -> tuple[SourceSpan, ...]:
    counters: Counter[str] = Counter()
    spans: list[SourceSpan] = []
    for child in tag.find_all(list(inline_tags)):
        if not isinstance(child, Tag):
            continue
        text = _norm(child.get_text(" ", strip=True))
        if not text:
            continue
        counters[child.name] += 1
        spans.append(SourceSpan(
            span_id=f"{child.name}{counters[child.name]:02d}",
            tag=child.name,
            text=text,
            occurrence=counters[child.name],
            attrs=_flatten_attrs(child.attrs),
        ))
    return tuple(spans)


def _structural_role(tag: Tag) -> str:
    name = (tag.name or "").lower()
    if len(name) == 2 and name.startswith("h") and name[1].isdigit():
        return "heading"
    if name == "li":
        return "list_item"
    if name == "blockquote":
        return "blockquote"
    if name == "p":
        text = _norm(tag.get_text(" ", strip=True))
        if text and text[0] in _DIALOGUE_LEAD:
            return "dialogue"
        return "paragraph"
    return "unknown"


def parse_source_html(
    html_text: str,
    *,
    block_tags: Sequence[str] = DEFAULT_BLOCK_TAGS,
    inline_tags: Sequence[str] = DEFAULT_INLINE_TAGS,
    remove_tags: Sequence[str] = DEFAULT_REMOVE,
) -> list[SourceBlock]:
    soup = BeautifulSoup(html_text, "html.parser")
    for name in remove_tags:
        for t in soup.find_all(name):
            t.decompose()
    blocks: list[SourceBlock] = []
    for i, tag in enumerate(_leaf_blocks(soup, block_tags), start=1):
        text = _norm(tag.get_text(" ", strip=True))
        blocks.append(SourceBlock(
            pid=f"p{i:05d}",
            index=i - 1,
            tag=tag.name,
            text=text,
            html=str(tag),
            structural_role=_structural_role(tag),
            inline_spans=_extract_inline_spans(tag, inline_tags),
            word_count=len(text.split()),
        ))
    return blocks


def load_source(path: Path) -> tuple[list[SourceBlock], str]:
    """Read EN source file, return (blocks, sha256-hex)."""
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8-sig", errors="replace")
    return parse_source_html(text), sha
