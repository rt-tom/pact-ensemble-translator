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
from .store import BookStore


def _quarantine_candidate(store: BookStore, candidate_id: str) -> None:
    src = store.incoming_candidate_path(candidate_id)
    if not src.exists():
        return
    dst = store.quarantine_candidate_path(candidate_id)
    dst.parent.mkdir(parents=True, exist_ok=True)
    # If quarantine destination exists, remove it first
    if dst.exists():
        shutil.rmtree(dst)
    os.replace(str(src), str(dst))


def promote(
    store: BookStore,
    candidate_id: str,
    operator: str = "rt",
    host: str = "RT",
    run_id: str | None = None,
) -> Dict[str, Any]:
    """Validate candidate, acquire lease, atomically promote."""
    candidate_dir = store.incoming_candidate_path(candidate_id)
    # We need to ensure quarantine on any failure, preserving CURRENT.
    # Validation phase before lease acquisition is still quarantined on failure.
    lease_acquired = False
    # Store original CURRENT for recovery check (not needed but preserve)
    try:
        # --- Phase 1: validate manifest schema ---
        if not candidate_dir.is_dir():
            raise ValidationError(f"Candidate not found: {candidate_dir}")
        manifest_path = candidate_dir / "manifest.json"
        if not manifest_path.is_file():
            raise ValidationError(f"manifest.json missing in candidate {candidate_id}")

        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            raise ValidationError(f"manifest.json invalid JSON: {e}") from e

        manifest = Manifest.from_dict(raw)

        # Require eligible terminal_status
        # For promote, allow complete, accepted_degraded, bootstrap-seed (per spec)
        # But bootstrap-seed is normally only for rev-0001; allow anyway
        if manifest.terminal_status not in {"complete", "accepted_degraded", "bootstrap-seed"}:
            raise ValidationError(f"Ineligible terminal_status: {manifest.terminal_status}")

        # Book id must match store
        if manifest.book_id != store.book_id:
            raise ValidationError(f"book_id mismatch: manifest {manifest.book_id} vs store {store.book_id}")

        # Verify each state_files entry's actual bytes
        for entry in manifest.state_files:
            file_path = candidate_dir / entry.rel_path
            if not file_path.is_file():
                raise ValidationError(f"state file missing: {entry.rel_path}")
            actual_sha, actual_size = compute_sha256_and_size(file_path)
            if actual_sha != entry.sha256 or actual_size != entry.size:
                raise HashMismatch(
                    f"Hash/size mismatch for {entry.rel_path}: expected {entry.sha256}/{entry.size}, got {actual_sha}/{actual_size}"
                )

        # Require parent_revision_id == current CURRENT revision (fail-closed)
        current = store.read_current()
        if current is None:
            raise ValidationError("CURRENT.json not found; run init-store/bootstrap first")
        current_rev = current.get("revision_id")
        # For bootstrap, current_rev may be None? But promote after bootstrap should have rev-0001. If None, treat as None.
        if manifest.parent_revision_id != current_rev:
            raise StaleParent(
                f"parent_revision_id {manifest.parent_revision_id!r} != current {current_rev!r}"
            )

        # Validate that manifest.revision_id is either next revision or candidate's declared?
        # We will compute next revision and enforce consistency if manifest.revision_id differs from computed?
        # Spec says atomically move candidate to snapshots/<revision-id>. Which revision?
        # We'll compute next and if manifest.revision_id != next, raise ValidationError (strict).
        # However if candidate's revision_id is not next, that's a validation error before lease.
        # Alternatively we could override with computed id, but spec expects monotonic rev-NNNN.
        next_rev = store.compute_next_revision_id()
        # If manifest.revision_id is provided, it should equal next_rev; but to be flexible,
        # if manifest.revision_id != next_rev we will treat manifest's revision as intended but
        # ensure it doesn't collide. Safer to enforce equality.
        if manifest.revision_id != next_rev:
            # Allow if candidate revision is logically next? If mismatch, reject
            raise ValidationError(
                f"manifest revision_id {manifest.revision_id!r} != expected next {next_rev!r}"
            )

        # --- Phase 2: acquire lease mutex bound to current revision ---
        try:
            lease = acquire_lease(store, operator, host, parent_revision=current_rev, run_id=run_id)
            lease_acquired = True
        except LeaseHeld:
            raise

        # --- Phase 3: atomically move bundle to snapshots ---
        snap_dir = store.snapshot_dir(next_rev)
        if snap_dir.exists():
            raise ValidationError(f"Snapshot dir already exists: {snap_dir}")
        # Atomic move via os.replace (candidate dir -> snapshot dir)
        # Ensure snapshots parent exists
        snap_dir.parent.mkdir(parents=True, exist_ok=True)
        # os.replace works for directories on POSIX
        os.replace(str(candidate_dir), str(snap_dir))

        # Re-check lease still references same parent revision
        current_lease = read_lease(store)
        if current_lease is None or current_lease.get("revision_id") != current_rev:
            # Lease lost or changed; this is a failure, but candidate already moved to snapshots
            # We should keep it there but not advance CURRENT. However spec says revert?
            # For safety, preserve prior CURRENT and raise.
            raise LeaseHeld(f"Lease lost or mismatched after move; expected parent {current_rev!r}")

        # Compute manifest hash (now at snap_dir/manifest.json)
        moved_manifest_path = snap_dir / "manifest.json"
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
        # Move candidate to quarantine if still in incoming (has not been moved to snapshots)
        # If candidate was already moved to snapshots and then lease check failed, we keep it in snapshots? But spec says preserve prior CURRENT.
        # For any failure before move, quarantine; after move failure before CURRENT write, we should consider moving snapshot back to quarantine?
        # Simpler: if candidate_dir still exists in incoming, quarantine it.
        # If it was already moved to snapshots/next_rev, move that snapshot dir to quarantine as well to avoid dangling snapshot not referenced by CURRENT.
        try:
            # Check if candidate still in incoming
            if candidate_dir.exists():
                _quarantine_candidate(store, candidate_id)
            else:
                # It may have been moved to snapshots/next_rev; if CURRENT was not advanced, move snapshots dir to quarantine
                # Only if we computed next_rev and snap_dir exists and CURRENT still points to old rev
                try:
                    # Try to find snapshot dir for next_rev if defined
                    if "next_rev" in locals():
                        snap = store.snapshot_dir(next_rev)  # type: ignore[possibly-undefined]
                        if snap.exists():
                            # Verify CURRENT still not advanced
                            cur = store.read_current()
                            if cur is not None and cur.get("revision_id") != next_rev:
                                dst = store.quarantine_candidate_path(candidate_id)
                                # Move snapshot to quarantine (rename)
                                if dst.exists():
                                    shutil.rmtree(dst)
                                os.replace(str(snap), str(dst))
                except Exception:
                    pass
        finally:
            pass
        raise
    except Exception as e:
        # Unexpected error also quarantines if possible
        try:
            if candidate_dir.exists():
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
