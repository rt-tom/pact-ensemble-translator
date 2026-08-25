"""Lease mutex for promote-time single writer."""

from __future__ import annotations

import datetime
import json
import os
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from .errors import LeaseHeld
from .store import BookStore


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def acquire_lease(
    store: BookStore,
    operator: str,
    host: str,
    parent_revision: Optional[str],
    run_id: Optional[str] = None,
    ttl_seconds: int = 3600,
) -> Dict[str, Any]:
    """Atomically acquire lease via O_EXCL; raise LeaseHeld if exists."""
    lease_path = store.lease_path()
    lease_path.parent.mkdir(parents=True, exist_ok=True)

    lease_id = f"lease-{uuid.uuid4().hex[:12]}"
    now = datetime.datetime.now(datetime.timezone.utc)
    expires = now + datetime.timedelta(seconds=ttl_seconds)
    # parent_revision may be None for bootstrap edge, normalize to None
    data: Dict[str, Any] = {
        "book_id": store.book_id,
        "lease_id": lease_id,
        "revision_id": parent_revision,
        "operator": operator,
        "host": host,
        "acquired_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": expires.isoformat().replace("+00:00", "Z"),
        "run_id": run_id,
    }

    # Atomic CAS via O_EXCL
    try:
        fd = os.open(str(lease_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError as e:
        raise LeaseHeld(f"Lease already held: {lease_path}") from e

    try:
        payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return data


def read_lease(store: BookStore) -> Optional[Dict[str, Any]]:
    p = store.lease_path()
    if not p.exists():
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def release_lease(store: BookStore) -> None:
    """Delete lease file, no audit. Used by promote on clean termination."""
    p = store.lease_path()
    if p.exists():
        p.unlink()


def release_with_audit(
    store: BookStore,
    operator: str,
    reason: str,
    prior_staging_reviewed: bool = False,
    recovery_decision: str = "released",
) -> Optional[Dict[str, Any]]:
    """Append JSONL audit record then delete lease. Returns audit record."""
    lease = read_lease(store)
    lease_id = lease.get("lease_id") if lease else None
    revision_id = lease.get("revision_id") if lease else None
    ts = _now_iso()
    record: Dict[str, Any] = {
        "book_id": store.book_id,
        "lease_id": lease_id,
        "revision_id": revision_id,
        "action": "release",
        "reason": reason,
        "operator": operator,
        "ts": ts,
        "prior_staging_reviewed": prior_staging_reviewed,
        "recovery_decision": recovery_decision,
    }
    audit_path = store.lease_audit_path()
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    # Append atomically
    with open(audit_path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    # Delete lease file
    p = store.lease_path()
    if p.exists():
        p.unlink()
    return record


def check_expired(store: BookStore) -> Dict[str, Any]:
    """Read-only report of held leases (no deletion)."""
    lease = read_lease(store)
    if lease is None:
        return {"held": False, "lease": None, "expired": False, "now": _now_iso()}
    # Parse expires_at
    expires_at = lease.get("expires_at")
    expired = False
    if isinstance(expires_at, str):
        try:
            # Parse ISO8601 Z
            dt = datetime.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            now = datetime.datetime.now(datetime.timezone.utc)
            expired = now > dt
        except Exception:
            expired = False
    return {"held": True, "lease": lease, "expired": expired, "now": _now_iso()}
