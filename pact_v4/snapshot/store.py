"""Filesystem layout for the media-side book-state snapshot store."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional


_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_component(name: str, kind: str) -> None:
    """Validate single path component is safe (no escape). Raises ValueError on invalid."""
    if not isinstance(name, str) or not name:
        raise ValueError(f"{kind} must be non-empty string")
    # Absolute paths (POSIX, Windows drive, UNC)
    if os.path.isabs(name):
        raise ValueError(f"{kind} must not be absolute: {name!r}")
    if name.startswith("/") or name.startswith("\\"):
        raise ValueError(f"{kind} must not be absolute: {name!r}")
    # Path separators
    if "/" in name or "\\" in name or os.sep in name:
        raise ValueError(f"{kind} must not contain path separator: {name!r}")
    # Dot segments
    if name in (".", ".."):
        raise ValueError(f"{kind} must not be '.' or '..': {name!r}")
    # Charset
    if not _SAFE_COMPONENT_RE.match(name):
        raise ValueError(f"{kind} contains illegal characters (allowed [A-Za-z0-9._-]): {name!r}")


def _ensure_within(child: Path, parent: Path) -> None:
    """Ensure resolved child is strictly within resolved parent. Raises ValueError if not."""
    try:
        # resolve without strict to handle non-existent paths
        p_res = parent.resolve()
        c_res = child.resolve()
    except Exception:
        # fallback to absolute
        p_res = parent.absolute()
        c_res = child.absolute()
    # child must be strictly under parent (not equal)
    try:
        is_within = c_res.is_relative_to(p_res)
    except AttributeError:
        # Python <3.9 fallback
        try:
            c_res.relative_to(p_res)
            is_within = True
        except ValueError:
            is_within = False
    if not is_within or c_res == p_res:
        raise ValueError(f"Path {c_res} is not within {p_res}")


DEFAULT_ROOT = "/home/rt/pact_runs"


class BookStore:
    """Book-scoped store under <root>/books/<book-id>/."""

    def __init__(self, book_id: str, root: str = DEFAULT_ROOT) -> None:
        _validate_component(book_id, "book_id")
        self.book_id = book_id
        self.root = Path(root)
        # Design: <root>/books/<book-id>/
        # If root already ends with books/<book-id>, avoid duplication
        # Detect if root parts end with books/<book-id>
        # Simpler: always treat root as base containing books/
        self.book_dir = self.root / "books" / self.book_id

    # -- path resolvers --

    @property
    def current_path(self) -> Path:
        return self.book_dir / "CURRENT.json"

    @property
    def locks_dir(self) -> Path:
        return self.book_dir / "locks"

    @property
    def incoming_dir(self) -> Path:
        return self.book_dir / "incoming"

    @property
    def bootstrap_inbox_dir(self) -> Path:
        return self.book_dir / "_bootstrap_inbox"

    @property
    def quarantine_dir(self) -> Path:
        return self.book_dir / "quarantine"

    @property
    def snapshots_dir(self) -> Path:
        return self.book_dir / "snapshots"

    def lease_path(self) -> Path:
        return self.locks_dir / f"{self.book_id}.lease.json"

    def lease_audit_path(self) -> Path:
        return self.locks_dir / f"{self.book_id}.lease_audit.jsonl"

    def snapshot_dir(self, revision_id: str) -> Path:
        # revision_id itself is validated elsewhere (rev-NNNN pattern), but also ensure safe component
        _validate_component(revision_id, "revision_id")
        p = self.snapshots_dir / revision_id
        _ensure_within(p, self.root)
        return p

    def incoming_candidate_path(self, candidate_id: str) -> Path:
        _validate_component(candidate_id, "candidate_id")
        p = self.incoming_dir / candidate_id
        _ensure_within(p, self.root)
        # defense-in-depth: must be strictly under incoming_dir
        _ensure_within(p, self.incoming_dir)
        return p

    def quarantine_candidate_path(self, candidate_id: str) -> Path:
        _validate_component(candidate_id, "candidate_id")
        p = self.quarantine_dir / candidate_id
        _ensure_within(p, self.root)
        _ensure_within(p, self.quarantine_dir)
        return p

    # -- operations --

    def init_store(self) -> Dict[str, Any]:
        """Create skeleton directories and CURRENT.json if absent."""
        for d in [
            self.book_dir,
            self.locks_dir,
            self.incoming_dir,
            self.bootstrap_inbox_dir,
            self.quarantine_dir,
            self.snapshots_dir,
        ]:
            d.mkdir(parents=True, exist_ok=True)
        if not self.current_path.exists():
            initial = {
                "book_id": self.book_id,
                "revision_id": None,
                "manifest_sha256": None,
                "published_at": None,
                "operator": None,
                "host": None,
                "run_id": None,
                "lease_id": None,
                "parent_revision_id": None,
            }
            atomic_write_json(self.current_path, initial)
        return self.read_current()  # type: ignore[return-value]

    def read_current(self) -> Optional[Dict[str, Any]]:
        if not self.current_path.exists():
            return None
        with open(self.current_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def write_current_atomic(self, data: Dict[str, Any]) -> None:
        atomic_write_json(self.current_path, data)

    def compute_next_revision_id(self) -> str:
        """Scan snapshots/ for rev-NNNN dirs and return next id."""
        if not self.snapshots_dir.exists():
            return "rev-0001"
        max_n = 0
        for p in self.snapshots_dir.iterdir():
            if p.is_dir() and p.name.startswith("rev-"):
                suffix = p.name[4:]
                if suffix.isdigit() and len(suffix) == 4:
                    try:
                        n = int(suffix)
                        if n > max_n:
                            max_n = n
                    except ValueError:
                        continue
        return f"rev-{max_n + 1:04d}"


def atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    """Write JSON atomically via temp file + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create temp file in same directory for atomic rename
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".tmp.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        # Cleanup if replace failed
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".tmp.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
