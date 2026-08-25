"""Promote candidate bundle to new immutable snapshot."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict

from .errors import HashMismatch, LeaseHeld, StaleParent, ValidationError
from .lease import acquire_lease, read_lease, release_lease
from .manifest import Manifest, compute_sha256_and_size
from .store import BookStore, _validate_component


def _validate_candidate_id(candidate_id: str) -> None:
    try:
        _validate_component(candidate_id, "candidate_id")
    except ValueError as e:
        raise ValidationError(str(e)) from e


def _reject_if_symlink(p: Path, label: str) -> None:
    if p.is_symlink():
        raise ValidationError(f"{label} is a symlink (rejected): {p}")


def _ensure_regular_file(p: Path, label: str) -> None:
    _reject_if_symlink(p, label)
    if not os.path.isfile(str(p)):
        raise ValidationError(f"{label} is not a regular file: {p}")


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


def _quarantine_candidate(store: BookStore, candidate_id: str) -> None:
    try:
        src = store.incoming_candidate_path(candidate_id)
    except (ValueError, ValidationError):
        return
    if not src.exists() and not src.is_symlink():
        return
    dst = store.quarantine_candidate_path(candidate_id)
    dst.parent.mkdir(parents=True, exist_ok=True)
    # If quarantine destination exists, remove it first
    if dst.exists():
        shutil.rmtree(dst)
    os.replace(str(src), str(dst))


def validate_candidate_boundary(candidate_dir: Path) -> Manifest:
    """Full boundary validation used BOTH before lease and at pre-move recheck.

    Re-asserts against live on-disk candidate:
    - top-level entries are EXACTLY regular file manifest.json + regular directory state/ (no symlink, no extra)
    - manifest.json is regular file, valid JSON, state_files exactly four canonical
    - state/ is regular directory containing EXACTLY four canonical JSON files, each regular file, valid JSON, hash matches manifest
    """
    _reject_if_symlink(candidate_dir, "candidate_dir")
    if not candidate_dir.is_dir():
        raise ValidationError(f"Candidate not found: {candidate_dir}")
    # Top-level boundary
    top_entries = list(candidate_dir.iterdir())
    for _p in top_entries:
        if _p.is_symlink():
            raise ValidationError(f"Top-level symlink rejected: {_p.name}")
    top_names = {p.name for p in top_entries}
    allowed_top = {"manifest.json", "state"}
    unexpected_top = top_names - allowed_top
    if unexpected_top:
        raise ValidationError(f"Unexpected top-level entry in candidate: {sorted(unexpected_top)}")
    missing_top = allowed_top - top_names
    if missing_top:
        raise ValidationError(f"Candidate top-level missing required entry: {sorted(missing_top)}")
    if top_names != allowed_top or len(top_entries) != 2:
        raise ValidationError(f"Candidate top-level must contain exactly manifest.json and state/: found {sorted(top_names)}")
    manifest_path = candidate_dir / "manifest.json"
    state_dir = candidate_dir / "state"
    _ensure_regular_file(manifest_path, "manifest.json")
    if state_dir.is_symlink():
        raise ValidationError(f"state/ is a symlink (rejected): {state_dir}")
    if not state_dir.is_dir() or not os.path.isdir(str(state_dir)):
        raise ValidationError(f"state is not a regular directory: {state_dir}")
    _assert_within(state_dir, candidate_dir, "state_dir")

    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        raise ValidationError(f"manifest.json invalid JSON: {e}") from e

    manifest = Manifest.from_dict(raw)

    # Verify each state_files entry's actual bytes - reject symlinks first
    for entry in manifest.state_files:
        file_path = candidate_dir / entry.rel_path
        _reject_if_symlink(file_path, f"state file {entry.rel_path}")
        if not file_path.is_file() or not os.path.isfile(str(file_path)):
            raise ValidationError(f"state file missing: {entry.rel_path}")
        _assert_within(file_path, candidate_dir, f"state file {entry.rel_path}")
        actual_sha, actual_size = compute_sha256_and_size(file_path)
        if actual_sha != entry.sha256 or actual_size != entry.size:
            raise HashMismatch(
                f"Hash/size mismatch for {entry.rel_path}: expected {entry.sha256}/{entry.size}, got {actual_sha}/{actual_size}"
            )

    # Scope A: reject smuggled extra files in candidate state/ and validate JSON
    if state_dir.is_symlink():
        raise ValidationError(f"state/ is a symlink (rejected): {state_dir}")
    if state_dir.is_dir():
        _assert_within(state_dir, candidate_dir, "state_dir")
        allowed = {e.rel_path for e in manifest.state_files}
        for p in state_dir.iterdir():
            if p.is_symlink():
                raise ValidationError(f"Symlink rejected in state/: {p.name}")
            if p.is_file():
                _ensure_regular_file(p, f"state file state/{p.name}")
                rel = f"state/{p.name}"
                if rel not in allowed:
                    raise ValidationError(f"Extra file in state/ not listed in state_files: {rel}")
                try:
                    with open(p, "r", encoding="utf-8") as jf:
                        json.load(jf)
                except Exception as e:
                    raise ValidationError(f"Canonical state file not valid JSON: {rel}: {e}") from e
            elif p.is_dir():
                raise ValidationError(f"Unexpected subdirectory in state/: {p.name}")
    return manifest


def promote(
    store: BookStore,
    candidate_id: str,
    operator: str = "rt",
    host: str = "RT",
    run_id: str | None = None,
) -> Dict[str, Any]:
    """Validate candidate, acquire lease, atomically promote."""
    _validate_candidate_id(candidate_id)
    lease_acquired = False
    candidate_dir = None  # will be set inside try
    try:
        try:
            candidate_dir = store.incoming_candidate_path(candidate_id)
        except ValueError as e:
            raise ValidationError(str(e)) from e
        # Defense-in-depth: resolved candidate must be strictly under incoming dir
        try:
            _assert_within(candidate_dir, store.incoming_dir, "candidate_dir")
            _assert_within(candidate_dir, store.root, "candidate_dir")
        except ValueError as e:
            raise ValidationError(str(e)) from e

        # --- Phase 1: full boundary validation BEFORE lease (shared function) ---
        manifest = validate_candidate_boundary(candidate_dir)

        # Require eligible terminal_status
        if manifest.terminal_status not in {"complete", "accepted_degraded", "bootstrap-seed"}:
            raise ValidationError(f"Ineligible terminal_status: {manifest.terminal_status}")

        # Book id must match store
        if manifest.book_id != store.book_id:
            raise ValidationError(f"book_id mismatch: manifest {manifest.book_id} vs store {store.book_id}")

        # Require parent_revision_id == current CURRENT revision (fail-closed)
        current = store.read_current()
        if current is None:
            raise ValidationError("CURRENT.json not found; run init-store/bootstrap first")
        current_rev = current.get("revision_id")
        if manifest.parent_revision_id != current_rev:
            raise StaleParent(
                f"parent_revision_id {manifest.parent_revision_id!r} != current {current_rev!r}"
            )

        # Finding 1: media assigns revision id — do NOT require candidate revision_id == next_rev.
        # next_rev is computed AFTER acquiring lease to avoid TOCTOU.

        # --- Phase 2: acquire lease mutex bound to current revision ---
        try:
            lease = acquire_lease(store, operator, host, parent_revision=current_rev, run_id=run_id)
            lease_acquired = True
        except LeaseHeld:
            raise

        # --- Phase 3: atomically move bundle to snapshots ---
        # Media assigns revision id after lease (Finding 1)
        next_rev = store.compute_next_revision_id()
        snap_dir = store.snapshot_dir(next_rev)
        # Defense-in-depth: snapshot path must be under store root
        _assert_within(snap_dir, store.root, "snapshot_dir")
        _assert_within(snap_dir, store.snapshots_dir, "snapshot_dir")
        if snap_dir.exists() or snap_dir.is_symlink():
            raise ValidationError(f"Snapshot dir already exists: {snap_dir}")
        # Atomic move via os.replace (candidate dir -> snapshot dir)
        # Ensure snapshots parent exists
        snap_dir.parent.mkdir(parents=True, exist_ok=True)
        # Full boundary re-validation immediately before move (TOCTOU defense) — SAME function
        validate_candidate_boundary(candidate_dir)
        # os.replace works for directories on POSIX
        os.replace(str(candidate_dir), str(snap_dir))

        # Re-check lease still references same parent revision
        current_lease = read_lease(store)
        if current_lease is None or current_lease.get("revision_id") != current_rev:
            raise LeaseHeld(f"Lease lost or mismatched after move; expected parent {current_rev!r}")

        # Rewrite stored manifest.json revision_id to next_rev so directory, manifest, and CURRENT agree (Finding 1)
        moved_manifest_path = snap_dir / "manifest.json"
        try:
            with open(moved_manifest_path, "r", encoding="utf-8") as f:
                stored_manifest = json.load(f)
        except Exception as e:
            raise ValidationError(f"Stored manifest.json unreadable after move: {e}") from e
        stored_manifest["revision_id"] = next_rev
        # Atomically rewrite manifest.json with corrected revision_id
        tmp_manifest = moved_manifest_path.with_suffix(".tmp")
        with open(tmp_manifest, "w", encoding="utf-8") as f:
            json.dump(stored_manifest, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp_manifest), str(moved_manifest_path))
        # Update in-memory manifest for CURRENT construction
        manifest.revision_id = next_rev

        # Compute manifest hash (now at snap_dir/manifest.json, after rewrite)
        manifest_sha, _ = compute_sha256_and_size(moved_manifest_path)

        # Write CURRENT.json via atomic rename
        new_current = {
            "book_id": store.book_id,
            "revision_id": next_rev,
            "manifest_sha256": manifest_sha,
            "published_at": manifest.published_at,
            "operator": manifest.source.get("operator", operator),
            "host": manifest.source.get("host", host),
            "run_id": manifest.source.get("run_id", run_id),
            "lease_id": current_lease.get("lease_id"),
            "parent_revision_id": manifest.parent_revision_id,
        }
        store.write_current_atomic(new_current)

        return {
            "status": "ACCEPTED",
            "revision_id": next_rev,
            "manifest_sha256": manifest_sha,
            "current": new_current,
            "lease_id": current_lease.get("lease_id"),
        }

    except (ValidationError, LeaseHeld, HashMismatch, StaleParent) as e:
        try:
            if candidate_dir is not None and (candidate_dir.exists() or candidate_dir.is_symlink()):
                _quarantine_candidate(store, candidate_id)
            else:
                try:
                    if "next_rev" in locals():
                        snap = store.snapshot_dir(next_rev)  # type: ignore[possibly-undefined]
                        if snap.exists():
                            cur = store.read_current()
                            if cur is not None and cur.get("revision_id") != next_rev:
                                dst = store.quarantine_candidate_path(candidate_id)
                                if dst.exists():
                                    shutil.rmtree(dst)
                                os.replace(str(snap), str(dst))
                except Exception:
                    pass
        finally:
            pass
        raise
    except Exception as e:
        try:
            if candidate_dir is not None and (candidate_dir.exists() or candidate_dir.is_symlink()):
                _quarantine_candidate(store, candidate_id)
        except Exception:
            pass
        raise
    finally:
        if lease_acquired:
            try:
                release_lease(store)
            except Exception:
                pass
