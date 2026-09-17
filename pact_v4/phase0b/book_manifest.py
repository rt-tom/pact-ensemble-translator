"""V5 slice-1: book source manifest contract (shared, model-free).

A source manifest describes the deterministic chapter set a pipeline run
consumes: ``NNNN_<slug>.html`` files plus per-chapter metadata. It is the
SAME schema for EPUB-splitter output and for directory-input sets (Pact's
150 HTML): the only difference is which book-level hash is populated
(``epub_hash`` for EPUB input, ``set_hash`` for directory input).

The pipeline never parses EPUBs and never discovers chapters by glob: it
validates this manifest (order, hashes, exactly one file per chapter,
reject symlink/special) and resolves chapters from it.

Boundary hardening (standing rule): ``validate_source_manifest`` is the
SINGLE validation routine used (a) in ``--preflight`` and (b) immediately
before the run starts (TOCTOU re-validation). Trust checks (symlink chain
of the source root, entry types) run BEFORE any read of manifest-listed
files. Every expected file must be a regular non-symlink file; FIFOs,
sockets, devices, directories and symlinks are rejected, never skipped.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat as _stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

MANIFEST_SCHEMA_VERSION = "pact-v5-book-manifest/v1"
SPLITTER_VERSION = "pact-v5-splitter/v1"

_MANIFEST_FILENAME = "manifest.json"


class ManifestError(ValueError):
    """Fail-closed manifest validation error (preflight/run refusal)."""


@dataclass(frozen=True)
class IllustrationSlot:
    slot_id: str
    anchor: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.slot_id, "anchor": self.anchor}


@dataclass(frozen=True)
class ManifestChapter:
    file: str
    order: int
    en_title: str
    pov: Optional[str] = None
    parent: Optional[str] = None
    illustrations: tuple = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "order": self.order,
            "en_title": self.en_title,
            "pov": self.pov,
            "parent": self.parent,
            "illustrations": [
                s.to_dict() if isinstance(s, IllustrationSlot) else dict(s)
                for s in self.illustrations
            ],
        }


@dataclass(frozen=True)
class SourceManifest:
    book_slug: str
    source_kind: str  # "epub" | "directory"
    source_hash: str  # epub sha256 or deterministic set hash
    source_name: Optional[str]
    splitter_version: str
    chapters: tuple

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "book_slug": self.book_slug,
            "source_kind": self.source_kind,
            "splitter_version": self.splitter_version,
            "chapters": [c.to_dict() for c in self.chapters],
        }
        if self.source_kind == "epub":
            payload["epub_hash"] = self.source_hash
        else:
            payload["set_hash"] = self.source_hash
        if self.source_name is not None:
            payload["source_name"] = self.source_name
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _is_regular_file(path: Path) -> bool:
    try:
        st = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return _stat.S_ISREG(st.st_mode)


def check_no_symlink_chain(path: Path) -> None:
    """Reject any symlink in path + all ancestors (trust check before I/O)."""
    cur: Optional[Path] = path.absolute()
    seen: set[Path] = set()
    while cur is not None and cur not in seen:
        seen.add(cur)
        try:
            if cur.is_symlink():
                raise ManifestError(f"symlink in path chain rejected: {cur}")
        except OSError as exc:
            raise ManifestError(f"cannot stat path chain {cur}: {exc}") from exc
        parent = cur.parent
        if parent == cur:
            break
        cur = parent


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def deterministic_set_hash(files: Sequence[Path]) -> str:
    """Deterministic hash of a directory-input file set (sorted by name)."""
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda p: p.name):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(sha256_file(path).encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def parse_manifest_dict(raw: Any) -> SourceManifest:
    """Parse + structural validation of a manifest payload (no disk I/O)."""
    if not isinstance(raw, dict):
        raise ManifestError("manifest must be a JSON object")
    allowed_top = {
        "schema_version", "book_slug", "source_kind", "splitter_version",
        "chapters", "epub_hash", "set_hash", "source_name",
    }
    for key in raw:
        if key not in allowed_top:
            raise ManifestError(f"unexpected top-level manifest key: {key!r}")
    if raw.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ManifestError(
            f"unsupported manifest schema_version: {raw.get('schema_version')!r} "
            f"(expected {MANIFEST_SCHEMA_VERSION!r})"
        )
    slug = raw.get("book_slug")
    if not isinstance(slug, str) or not slug.strip():
        raise ManifestError("manifest book_slug must be a non-empty string")
    kind = raw.get("source_kind")
    if kind not in ("epub", "directory"):
        raise ManifestError(f"manifest source_kind must be 'epub'|'directory', got {kind!r}")
    epub_hash = raw.get("epub_hash")
    set_hash = raw.get("set_hash")
    if kind == "epub":
        if not isinstance(epub_hash, str) or not epub_hash.strip():
            raise ManifestError("epub manifest requires a non-empty epub_hash")
        if set_hash is not None:
            raise ManifestError("epub manifest must not carry set_hash")
        source_hash = epub_hash
    else:
        if not isinstance(set_hash, str) or not set_hash.strip():
            raise ManifestError("directory manifest requires a non-empty set_hash")
        if epub_hash is not None:
            raise ManifestError("directory manifest must not carry epub_hash")
        source_hash = set_hash
    splitter_version = raw.get("splitter_version")
    if not isinstance(splitter_version, str) or not splitter_version.strip():
        raise ManifestError("manifest splitter_version must be a non-empty string")
    source_name = raw.get("source_name")
    if source_name is not None and not isinstance(source_name, str):
        raise ManifestError("manifest source_name must be a string when present")
    raw_chapters = raw.get("chapters")
    if not isinstance(raw_chapters, list) or not raw_chapters:
        raise ManifestError("manifest chapters must be a non-empty list")
    chapters: list[ManifestChapter] = []
    seen_orders: set[int] = set()
    seen_files: set[str] = set()
    for idx, entry in enumerate(raw_chapters):
        chapters.append(_parse_chapter_entry(entry, idx, seen_orders, seen_files))
    orders = sorted(seen_orders)
    if orders != list(range(1, len(chapters) + 1)):
        raise ManifestError(
            f"manifest orders must be contiguous 1..{len(chapters)}, got {orders!r}"
        )
    return SourceManifest(
        book_slug=slug,
        source_kind=kind,
        source_hash=source_hash,
        source_name=source_name,
        splitter_version=str(splitter_version),
        chapters=tuple(chapters),
    )


def _parse_chapter_entry(
    entry: Any, idx: int, seen_orders: set[int], seen_files: set[str]
) -> ManifestChapter:
    if not isinstance(entry, dict):
        raise ManifestError(f"manifest chapters[{idx}] must be an object")
    allowed = {"file", "order", "en_title", "pov", "parent", "illustrations"}
    for key in entry:
        if key not in allowed:
            raise ManifestError(f"manifest chapters[{idx}] unexpected key: {key!r}")
    fname = entry.get("file")
    if not isinstance(fname, str) or not fname.strip():
        raise ManifestError(f"manifest chapters[{idx}].file must be a non-empty string")
    if "/" in fname or "\\" in fname or fname.startswith("."):
        raise ManifestError(f"manifest chapters[{idx}].file must be a bare filename, got {fname!r}")
    if not fname.endswith(".html"):
        raise ManifestError(f"manifest chapters[{idx}].file must end with .html, got {fname!r}")
    if fname in seen_files:
        raise ManifestError(f"manifest duplicate file entry: {fname!r}")
    seen_files.add(fname)
    order = entry.get("order")
    if not isinstance(order, int) or isinstance(order, bool) or order < 1:
        raise ManifestError(f"manifest chapters[{idx}].order must be a positive int")
    if order in seen_orders:
        raise ManifestError(f"manifest duplicate order entry: {order}")
    seen_orders.add(order)
    en_title = entry.get("en_title")
    if not isinstance(en_title, str) or not en_title.strip():
        raise ManifestError(f"manifest chapters[{idx}].en_title must be a non-empty string")
    pov = entry.get("pov")
    if pov is not None and (not isinstance(pov, str) or not pov.strip()):
        raise ManifestError(f"manifest chapters[{idx}].pov must be a string when present")
    parent = entry.get("parent")
    if parent is not None and (not isinstance(parent, str) or not parent.strip()):
        raise ManifestError(f"manifest chapters[{idx}].parent must be a string when present")
    illustrations = entry.get("illustrations", [])
    if not isinstance(illustrations, list):
        raise ManifestError(f"manifest chapters[{idx}].illustrations must be a list")
    slots: list[IllustrationSlot] = []
    seen_ids: set[str] = set()
    for j, slot in enumerate(illustrations):
        if not isinstance(slot, dict) or set(slot) != {"id", "anchor"}:
            raise ManifestError(
                f"manifest chapters[{idx}].illustrations[{j}] must be {{id, anchor}}"
            )
        sid, anchor = slot.get("id"), slot.get("anchor")
        if not isinstance(sid, str) or not sid.strip():
            raise ManifestError(f"illustration id must be a non-empty string (chapters[{idx}][{j}])")
        if not isinstance(anchor, str) or not anchor.strip():
            raise ManifestError(f"illustration anchor must be a non-empty string (chapters[{idx}][{j}])")
        if sid in seen_ids:
            raise ManifestError(f"duplicate illustration id {sid!r} in chapters[{idx}]")
        seen_ids.add(sid)
        slots.append(IllustrationSlot(slot_id=sid, anchor=anchor))
    return ManifestChapter(
        file=fname,
        order=order,
        en_title=en_title,
        pov=pov,
        parent=parent,
        illustrations=tuple(slots),
    )


def load_manifest(manifest_path: Path) -> SourceManifest:
    """Load a manifest file (trust checks before read)."""
    check_no_symlink_chain(manifest_path)
    if not _is_regular_file(manifest_path):
        raise ManifestError(f"manifest is not a regular non-symlink file: {manifest_path}")
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ManifestError(f"manifest invalid JSON: {manifest_path} ({exc})") from exc
    return parse_manifest_dict(raw)


def validate_source_manifest(
    source_root: Path, manifest: SourceManifest
) -> dict[int, Path]:
    """Full boundary validation of manifest against live source root.

    THE shared routine: ``--preflight`` and the pre-start re-check call
    this same function (validate-then-act + identical re-validation).

    Checks, in order (trust before I/O):
    1. source_root symlink chain + is a real directory (no symlink).
    2. every listed file: bare filename, resolves strictly inside
       source_root, regular non-symlink file (reject FIFO/socket/device/
       directory/symlink — never skip).
    3. exactly one file per chapter (unique file/order enforced at parse;
       orders contiguous 1..N).
    4. each listed file is non-empty and hashed (returned for callers that
       need the set hash check).

    Returns ``{order: absolute Path}``. Raises ``ManifestError`` fail-closed.
    """
    check_no_symlink_chain(source_root)
    if source_root.is_symlink():
        raise ManifestError(f"source root is a symlink (rejected): {source_root}")
    if not source_root.is_dir():
        raise ManifestError(f"source root missing or not a directory: {source_root}")
    try:
        root_resolved = source_root.resolve()
    except OSError as exc:
        raise ManifestError(f"cannot resolve source root {source_root}: {exc}") from exc

    resolved: dict[int, Path] = {}
    for chapter in manifest.chapters:
        candidate = root_resolved / chapter.file
        # Containment: bare filename + resolved-within-root (paranoia: no ..).
        try:
            cand_resolved = candidate.resolve()
        except OSError as exc:
            raise ManifestError(f"cannot resolve {chapter.file}: {exc}") from exc
        try:
            cand_resolved.relative_to(root_resolved)
        except ValueError:
            raise ManifestError(f"manifest file escapes source root: {chapter.file!r}") from None
        if cand_resolved == root_resolved:
            raise ManifestError(f"manifest file escapes source root: {chapter.file!r}")
        # Type check BEFORE any read: regular non-symlink file only.
        try:
            if candidate.is_symlink() or cand_resolved.is_symlink():
                raise ManifestError(f"source file is symlink (rejected): {chapter.file}")
        except OSError as exc:
            raise ManifestError(f"cannot stat {chapter.file}: {exc}") from exc
        if not _is_regular_file(cand_resolved):
            raise ManifestError(
                f"source file is not a regular file (rejected): {chapter.file}"
            )
        check_no_symlink_chain(cand_resolved)
        try:
            size = cand_resolved.stat().st_size
        except OSError as exc:
            raise ManifestError(f"cannot stat {chapter.file}: {exc}") from exc
        if size == 0:
            raise ManifestError(f"source file is empty: {chapter.file}")
        resolved[chapter.order] = cand_resolved
    return resolved


def verify_set_hash(source_root: Path, manifest: SourceManifest) -> None:
    """Verify a directory manifest's set_hash against live files (fail-closed)."""
    if manifest.source_kind != "directory":
        raise ManifestError("set_hash verification applies to directory manifests only")
    resolved = validate_source_manifest(source_root, manifest)
    actual = deterministic_set_hash([resolved[o] for o in sorted(resolved)])
    if actual != manifest.source_hash:
        raise ManifestError(
            f"manifest set_hash mismatch: expected {manifest.source_hash}, got {actual}"
        )


def manifest_path_for_source_root(source_root: Path) -> Path:
    return source_root / _MANIFEST_FILENAME
