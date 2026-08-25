"""Minimal manifest schema for Scope A (state-only) snapshot."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .errors import ValidationError

ALLOWED_TERMINAL_STATUSES = {"bootstrap-seed", "complete", "accepted_degraded"}

TOP_LEVEL_KEYS = {
    "schema_version",
    "book_id",
    "revision_id",
    "parent_revision_id",
    "created_at",
    "published_at",
    "terminal_status",
    "tool_version",
    "source",
    "state_files",
    "excludes",
    "code_commit",
}

SOURCE_ALLOWED_KEYS = {"path_on_rt", "operator", "host", "run_id"}

CANONICAL_REL_PREFIX = "state/"
CANONICAL_STATE_PATHS = {
    "state/book_memory.json",
    "state/glossary.json",
    "state/chapter_index.json",
    "state/observations.json",
}


def _is_normalized_rel_path(rel_path: str) -> bool:
    """Return True iff rel_path is normalized POSIX relative path without traversal."""
    if not rel_path:
        return False
    if rel_path.startswith("/") or rel_path.startswith("\\"):
        return False
    if "\\" in rel_path:
        return False
    if "//" in rel_path:
        return False
    # No .. segments, no . segments, no empty segments
    parts = rel_path.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            return False
        if part != part.strip():
            return False
    # Reconstruct and ensure no normalization change (e.g. a/./b)
    normalized = "/".join(parts)
    if normalized != rel_path:
        return False
    return True


def compute_sha256_and_size(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(8192)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def _validate_hex64(value: str) -> None:
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value.lower()):
        raise ValidationError(f"Invalid sha256 hex: {value}")


@dataclass
class StateFileEntry:
    rel_path: str
    sha256: str
    size: int

    def to_dict(self) -> Dict[str, Any]:
        return {"rel_path": self.rel_path, "sha256": self.sha256, "size": self.size}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "StateFileEntry":
        if set(d.keys()) != {"rel_path", "sha256", "size"}:
            raise ValidationError(f"state_files entry has unexpected keys: {d}")
        rel_path = d["rel_path"]
        sha256 = d["sha256"]
        size = d["size"]
        if not isinstance(rel_path, str) or not rel_path.startswith(CANONICAL_REL_PREFIX):
            raise ValidationError(f"rel_path must be under state/: {rel_path}")
        if not _is_normalized_rel_path(rel_path):
            raise ValidationError(f"rel_path must be normalized without traversal: {rel_path}")
        if not isinstance(sha256, str):
            raise ValidationError("sha256 must be string")
        _validate_hex64(sha256)
        if not isinstance(size, int) or size < 0:
            raise ValidationError(f"size must be non-negative int: {size}")
        return cls(rel_path=rel_path, sha256=sha256, size=size)


@dataclass
class Manifest:
    schema_version: str
    book_id: str
    revision_id: str
    parent_revision_id: Optional[str]
    created_at: str
    published_at: str
    terminal_status: str
    tool_version: str
    source: Dict[str, Any]
    state_files: List[StateFileEntry]
    excludes: List[str]
    code_commit: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "book_id": self.book_id,
            "revision_id": self.revision_id,
            "parent_revision_id": self.parent_revision_id,
            "created_at": self.created_at,
            "published_at": self.published_at,
            "terminal_status": self.terminal_status,
            "tool_version": self.tool_version,
            "source": dict(self.source),
            "state_files": [e.to_dict() for e in self.state_files],
            "excludes": list(self.excludes),
            "code_commit": self.code_commit,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Manifest":
        # Strict allow-list at top level
        unknown = set(d.keys()) - TOP_LEVEL_KEYS
        if unknown:
            raise ValidationError(f"Unknown top-level keys rejected (no-secrets boundary): {unknown}")
        missing = TOP_LEVEL_KEYS - set(d.keys())
        if missing:
            raise ValidationError(f"Missing required keys: {missing}")

        # Validate individual fields
        schema_version = d["schema_version"]
        book_id = d["book_id"]
        revision_id = d["revision_id"]
        parent_revision_id = d["parent_revision_id"]
        created_at = d["created_at"]
        published_at = d["published_at"]
        terminal_status = d["terminal_status"]
        tool_version = d["tool_version"]
        source = d["source"]
        state_files_raw = d["state_files"]
        excludes = d["excludes"]
        code_commit = d["code_commit"]

        if not isinstance(schema_version, str) or not schema_version:
            raise ValidationError("schema_version must be non-empty string")
        if not isinstance(book_id, str) or not book_id:
            raise ValidationError("book_id must be non-empty string")
        if not isinstance(revision_id, str) or not revision_id.startswith("rev-"):
            raise ValidationError(f"revision_id must be rev-NNNN: {revision_id}")
        if parent_revision_id is not None and not isinstance(parent_revision_id, str):
            raise ValidationError("parent_revision_id must be string or null")
        if parent_revision_id is not None and not parent_revision_id.startswith("rev-"):
            raise ValidationError(f"parent_revision_id must be rev-NNNN or null: {parent_revision_id}")
        if not isinstance(created_at, str) or not created_at:
            raise ValidationError("created_at must be non-empty string")
        if not isinstance(published_at, str) or not published_at:
            raise ValidationError("published_at must be non-empty string")
        if terminal_status not in ALLOWED_TERMINAL_STATUSES:
            raise ValidationError(f"terminal_status must be one of {ALLOWED_TERMINAL_STATUSES}, got {terminal_status}")
        if not isinstance(tool_version, str) or not tool_version:
            raise ValidationError("tool_version must be non-empty string")
        if not isinstance(source, dict):
            raise ValidationError("source must be object")
        unknown_source = set(source.keys()) - SOURCE_ALLOWED_KEYS
        if unknown_source:
            raise ValidationError(f"Unknown source keys rejected: {unknown_source}")
        for k in ("path_on_rt", "operator", "host"):
            if k not in source:
                raise ValidationError(f"source missing required key: {k}")
            if not isinstance(source[k], str) or not source[k]:
                raise ValidationError(f"source.{k} must be non-empty string")
        if "run_id" in source and source["run_id"] is not None and not isinstance(source["run_id"], str):
            raise ValidationError("source.run_id must be string or null")
        if not isinstance(state_files_raw, list) or len(state_files_raw) == 0:
            raise ValidationError("state_files must be non-empty list")
        state_files: List[StateFileEntry] = []
        for entry in state_files_raw:
            if not isinstance(entry, dict):
                raise ValidationError("state_files entry must be object")
            state_files.append(StateFileEntry.from_dict(entry))
        # Enforce Scope A exact four-file state boundary (Finding 2)
        rel_paths = [e.rel_path for e in state_files]
        if len(rel_paths) != len(set(rel_paths)):
            raise ValidationError(f"Duplicate state_files paths rejected: {rel_paths}")
        for rp in rel_paths:
            if not _is_normalized_rel_path(rp):
                raise ValidationError(f"state_files rel_path not normalized: {rp}")
            if ".." in rp.split("/"):
                raise ValidationError(f"state_files rel_path traversal rejected: {rp}")
        if set(rel_paths) != CANONICAL_STATE_PATHS:
            raise ValidationError(
                f"state_files must be exactly {sorted(CANONICAL_STATE_PATHS)}, got {sorted(set(rel_paths))}"
            )
        if not isinstance(excludes, list) or any(not isinstance(x, str) for x in excludes):
            raise ValidationError("excludes must be list of strings")
        if not isinstance(code_commit, str):
            raise ValidationError("code_commit must be string")

        return cls(
            schema_version=schema_version,
            book_id=book_id,
            revision_id=revision_id,
            parent_revision_id=parent_revision_id,
            created_at=created_at,
            published_at=published_at,
            terminal_status=terminal_status,
            tool_version=tool_version,
            source=dict(source),
            state_files=state_files,
            excludes=list(excludes),
            code_commit=code_commit,
        )

    def validate(self) -> None:
        # Re-validate via round-trip
        Manifest.from_dict(self.to_dict())

    @classmethod
    def read(cls, path: Path) -> "Manifest":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)

    def write(self, path: Path) -> None:
        from .store import atomic_write_json

        atomic_write_json(path, self.to_dict())
