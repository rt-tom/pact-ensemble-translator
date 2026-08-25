"""Bootstrap first revision from _bootstrap_inbox/<ts>/."""

from __future__ import annotations

import datetime
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from .errors import ValidationError
from .manifest import Manifest, StateFileEntry, compute_sha256_and_size
from .store import BookStore, atomic_write_json

CANONICAL_FILES = ["book_memory.json", "glossary.json", "chapter_index.json", "observations.json"]


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _resolve_inbox_dir(store: BookStore, ts: Optional[str] = None) -> Path:
    base = store.bootstrap_inbox_dir
    if not base.exists():
        raise ValidationError(f"_bootstrap_inbox not found: {base}")
    if ts is not None:
        p = base / ts
        if not p.is_dir():
            raise ValidationError(f"_bootstrap_inbox/<ts> not found: {p}")
        return p
    # latest = most recent subdir by name (lexicographically max)
    subdirs = [d for d in base.iterdir() if d.is_dir()]
    if not subdirs:
        raise ValidationError(f"No subdirs in _bootstrap_inbox: {base}")
    # Sort by name descending, pick first
    subdirs.sort(key=lambda p: p.name, reverse=True)
    return subdirs[0]


def bootstrap(
    store: BookStore,
    ts: Optional[str] = None,
    operator: str = "rt",
    host: str = "RT",
    tool_version: str = "pact-snapshot/0.1.0",
    source_path: str = "D:\\pact\\pact_chapters",
    code_commit: str = "unknown",
) -> Dict[str, Any]:
    """Create rev-0001 from inbox. Fail closed if missing/non-JSON."""
    # Reject if snapshots already exist (bootstrap is first-revision only)
    if store.snapshots_dir.exists():
        existing = [p.name for p in store.snapshots_dir.iterdir() if p.is_dir()]
        if existing:
            raise ValidationError(f"Snapshots already exist, bootstrap only for first revision: {existing}")

    # Also check CURRENT.json has no revision (if exists and non-null)
    current = store.read_current()
    if current is not None and current.get("revision_id") is not None:
        raise ValidationError("CURRENT already points to a revision; bootstrap only for first revision")

    inbox_dir = _resolve_inbox_dir(store, ts)

    # Validate exactly four canonical files
    for fname in CANONICAL_FILES:
        fpath = inbox_dir / fname
        if not fpath.is_file():
            raise ValidationError(f"Canonical file missing: {fname} in {inbox_dir}")
        # Validate well-formed JSON
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                json.load(f)
        except Exception as e:
            raise ValidationError(f"Canonical file not valid JSON: {fname}: {e}") from e

    # Collect excludes: any file in inbox not in canonical list
    excludes: List[str] = []
    for p in inbox_dir.iterdir():
        if p.is_file() and p.name not in CANONICAL_FILES:
            excludes.append(p.name)
        # Also handle directories? Treat top-level files only; subdirs ignored but listed
        if p.is_dir():
            # Not part of excludes per spec but we could note
            pass

    # Prepare snapshot dir
    revision_id = "rev-0001"
    snap_dir = store.snapshot_dir(revision_id)
    state_dir = snap_dir / "state"
    # Ensure not exists (already checked)
    if snap_dir.exists():
        raise ValidationError(f"Snapshot dir already exists: {snap_dir}")
    state_dir.mkdir(parents=True, exist_ok=False)

    now = _now_iso()
    state_files: List[StateFileEntry] = []
    for fname in CANONICAL_FILES:
        src = inbox_dir / fname
        dst = state_dir / fname
        # Copy file content (shutil.copy to preserve bytes exactly)
        shutil.copy2(src, dst)
        sha, size = compute_sha256_and_size(dst)
        state_files.append(StateFileEntry(rel_path=f"state/{fname}", sha256=sha, size=size))

    manifest = Manifest(
        schema_version="1.0.0",
        book_id=store.book_id,
        revision_id=revision_id,
        parent_revision_id=None,
        created_at=now,
        published_at=now,
        terminal_status="bootstrap-seed",
        tool_version=tool_version,
        source={"path_on_rt": source_path, "operator": operator, "host": host},
        state_files=state_files,
        excludes=sorted(excludes),
        code_commit=code_commit,
    )
    # Write manifest.json atomically
    manifest_path = snap_dir / "manifest.json"
    manifest.write(manifest_path)

    # Compute manifest sha for CURRENT
    manifest_sha, _ = compute_sha256_and_size(manifest_path)

    current_data = {
        "book_id": store.book_id,
        "revision_id": revision_id,
        "manifest_sha256": manifest_sha,
        "published_at": now,
        "operator": operator,
        "host": host,
        "run_id": None,
        "lease_id": None,
        "parent_revision_id": None,
    }
    store.write_current_atomic(current_data)

    return {
        "status": "ACCEPTED",
        "revision_id": revision_id,
        "manifest": manifest.to_dict(),
        "current": current_data,
        "inbox_dir": str(inbox_dir),
    }
