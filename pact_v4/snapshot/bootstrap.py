"""Bootstrap first revision from _bootstrap_inbox/<ts>/."""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional

from .errors import ValidationError
from .manifest import Manifest, StateFileEntry, compute_sha256_and_size
from .store import BookStore, atomic_write_json

CANONICAL_FILES = ["book_memory.json", "glossary.json", "chapter_index.json", "observations.json"]


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _reject_if_symlink(p: Path, label: str) -> None:
    if p.is_symlink():
        raise ValidationError(f"{label} is a symlink (rejected): {p}")


def _assert_within(child: Path, parent: Path, label: str) -> None:
    try:
        c_res = child.resolve()
        p_res = parent.resolve()
    except Exception:
        c_res = child.absolute()
        p_res = parent.absolute()
    try:
        is_within = c_res.is_relative_to(p_res)
    except AttributeError:
        try:
            c_res.relative_to(p_res)
            is_within = True
        except ValueError:
            is_within = False
    if not is_within or c_res == p_res:
        raise ValidationError(f"{label} path escape: {c_res} not within {p_res}")


def _reject_symlink_chain(target: Path, root: Path, label: str) -> None:
    """Reject if target or any lexical ancestor up to root inclusive is a symlink or non-regular directory/special file."""
    def _ensure_regular_dir(p: Path) -> None:
        if p.is_symlink():
            raise ValidationError(f"{label} path component is a symlink (rejected): {p}")
        if p.exists():
            # lstat check: must be a regular directory, not FIFO/socket/device/file
            try:
                st = p.lstat()
            except FileNotFoundError:
                return
            if not stat.S_ISDIR(st.st_mode):
                raise ValidationError(f"{label} path component is not a regular directory (rejected): {p}")
            # is_dir also covers special files (FIFO returns False)
            if not p.is_dir():
                raise ValidationError(f"{label} path component is not a regular directory (rejected): {p}")
    cur = target
    # Walk lexical parents up to root inclusive
    while True:
        _ensure_regular_dir(cur)
        if cur == root:
            break
        parent = cur.parent
        if parent == cur:  # filesystem root
            # Never hit store.root lexically; ensure root itself is not a symlink/special
            _ensure_regular_dir(root)
            break
        # If we've walked above root depth without hitting root, ensure root checked and stop
        if len(cur.parts) < len(root.parts) or len(parent.parts) < len(root.parts):
            _ensure_regular_dir(root)
            # Still need to check remaining ancestors up to parent? already checked cur, now check parent chain quickly
            # Walk remaining parents from parent up to root if not yet visited, but root already checked
            break
        cur = parent
        # If cur depth is less than root depth, we have exited the store subtree
        if len(cur.parts) < len(root.parts):
            _ensure_regular_dir(root)
            break


def _resolve_inbox_dir(store: BookStore, ts: Optional[str] = None) -> Path:
    base = store.bootstrap_inbox_dir
    if not base.exists() and not base.is_symlink():
        raise ValidationError(f"_bootstrap_inbox not found: {base}")
    # Reject symlink on bootstrap_inbox_dir or any ancestor up to store.root BEFORE listing
    _reject_symlink_chain(base, store.root, "_bootstrap_inbox")
    if ts is not None:
        p = base / ts
        # Reject symlink chain for selected inbox (covers p and all ancestors up to root)
        _reject_symlink_chain(p, store.root, "_bootstrap_inbox/<ts>")
        if not p.is_dir():
            raise ValidationError(f"_bootstrap_inbox/<ts> not found: {p}")
        # Containment: resolved selected inbox must be within resolved bootstrap_inbox_dir
        _assert_within(p, base, "_bootstrap_inbox/<ts>")
        return p
    # latest = most recent subdir by name (lexicographically max) — do NOT silently skip symlinks
    # Collect all directory-like entries including symlinks that look like dirs
    candidates: list[Path] = []
    for d in base.iterdir():
        # Include if it's a dir, or a symlink (to be rejected), or otherwise named entry
        # We consider any entry that is_dir or is_symlink as candidate for latest selection
        if d.is_symlink() or d.is_dir():
            candidates.append(d)
        elif d.exists():
            # Regular file? ignore for ts selection, but still allow detection if someone symlinked file as ts
            # Only consider directories/symlinks; skip plain files
            continue
    if not candidates:
        raise ValidationError(f"No subdirs in _bootstrap_inbox: {base}")
    # Sort by name descending, pick first (lexicographically max)
    candidates.sort(key=lambda p: p.name, reverse=True)
    cand = candidates[0]
    # Reject symlink chain for selected inbox BEFORE any file access
    _reject_symlink_chain(cand, store.root, "_bootstrap_inbox/<ts>")
    _assert_within(cand, base, "_bootstrap_inbox/<ts>")
    if not cand.is_dir():
        raise ValidationError(f"_bootstrap_inbox/<ts> not found or not a directory: {cand}")
    return cand


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
    # REGRESSION FIX (round 4): validate inbox/ancestor symlink chain and containment
    # BEFORE any store-path read/listing/write (before read_current, before snapshots/
    # iteration, before mkdir). Prevents symlinked books/<book-id> with malformed
    # CURRENT.json from raising JSONDecodeError instead of controlled ValidationError.
    inbox_dir = _resolve_inbox_dir(store, ts)
    # Defense-in-depth: re-assert same chain/containment immediately (still before any mkdir/read_current)
    _reject_symlink_chain(inbox_dir, store.root, "_bootstrap_inbox/<ts>")
    _reject_symlink_chain(store.bootstrap_inbox_dir, store.root, "_bootstrap_inbox")
    _assert_within(inbox_dir, store.bootstrap_inbox_dir, "_bootstrap_inbox/<ts>")
    _reject_if_symlink(inbox_dir, "_bootstrap_inbox/<ts>")

    # Ensure required directories exist WITHOUT creating CURRENT.json (Finding 3)
    # Only mkdir parents; CURRENT.json is written only after successful validation/seed.
    store.book_dir.mkdir(parents=True, exist_ok=True)
    store.snapshots_dir.mkdir(parents=True, exist_ok=True)
    store.locks_dir.mkdir(parents=True, exist_ok=True)
    store.quarantine_dir.mkdir(parents=True, exist_ok=True)
    store.incoming_dir.mkdir(parents=True, exist_ok=True)
    # Note: _bootstrap_inbox must already exist (owner-copied); do not create it here.
    # Reject if snapshots already exist (bootstrap is first-revision only)
    if store.snapshots_dir.exists():
        existing = [p.name for p in store.snapshots_dir.iterdir() if p.is_dir()]
        if existing:
            raise ValidationError(f"Snapshots already exist, bootstrap only for first revision: {existing}")

    # Also check CURRENT.json has no revision (if exists and non-null)
    current = store.read_current()
    if current is not None and current.get("revision_id") is not None:
        raise ValidationError("CURRENT already points to a revision; bootstrap only for first revision")

    # Reuse already-validated inbox_dir; still enforce invariants (no second _resolve listing that could race)
    _reject_symlink_chain(inbox_dir, store.root, "_bootstrap_inbox/<ts>")
    _reject_symlink_chain(store.bootstrap_inbox_dir, store.root, "_bootstrap_inbox")
    _assert_within(inbox_dir, store.bootstrap_inbox_dir, "_bootstrap_inbox/<ts>")
    _reject_if_symlink(inbox_dir, "_bootstrap_inbox/<ts>")

    # Validate exactly four canonical files — reject symlinks for consistency (Finding 1)
    for fname in CANONICAL_FILES:
        fpath = inbox_dir / fname
        _reject_if_symlink(fpath, f"Canonical file {fname}")
        if not fpath.is_file() or not os.path.isfile(str(fpath)):
            raise ValidationError(f"Canonical file missing: {fname} in {inbox_dir}")
        # Reject symlink source that points outside (copyfile would follow, but reject for consistency)
        # Validate well-formed JSON
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                json.load(f)
        except Exception as e:
            raise ValidationError(f"Canonical file not valid JSON: {fname}: {e}") from e
        # Also ensure no symlink nesting inside inbox_dir itself (already checked file)
        # Defense: resolved file must be within inbox_dir
        try:
            if not fpath.resolve().is_relative_to(inbox_dir.resolve()):
                raise ValidationError(f"Canonical file escape: {fpath}")
        except AttributeError:
            try:
                fpath.resolve().relative_to(inbox_dir.resolve())
            except ValueError:
                raise ValidationError(f"Canonical file escape: {fpath}")

    # Collect excludes: any file in inbox not in canonical list (reject symlinks/special files in inbox)
    excludes: List[str] = []
    for p in inbox_dir.iterdir():
        if p.is_symlink():
            raise ValidationError(f"Symlink rejected in inbox: {p.name}")
        # Class-fix: reject FIFOs/sockets/devices at inbox top-level
        is_file = p.is_file()
        is_dir = p.is_dir()
        if not is_file and not is_dir:
            raise ValidationError(f"Special file rejected in inbox: {p.name}")
        if is_file and p.name not in CANONICAL_FILES:
            excludes.append(p.name)
        # Also handle directories? Treat top-level files only; subdirs ignored but listed
        if is_dir:
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
