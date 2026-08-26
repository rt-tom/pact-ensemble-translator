"""RT-side media client using system ssh/scp (subprocess only).

No SSH library, no network code in the store package. Transport is
injectable for tests: pass a fake transport object that implements
fetch_current / push_candidate / check_expired.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from .store import BookStore, _validate_component

CANONICAL_FILES = ["glossary.json", "book_memory.json", "chapter_index.json", "observations.json"]
CANONICAL_STATE_NAMES = {f"state/{f}" for f in CANONICAL_FILES}

def _validate_book_id(book_id: str) -> None:
    try:
        _validate_component(book_id, "book_id")
    except ValueError as e:
        from .errors import ValidationError
        raise ValidationError(str(e)) from e

def _validate_candidate_id(candidate_id: str) -> None:
    try:
        _validate_component(candidate_id, "candidate_id")
    except ValueError as e:
        from .errors import ValidationError
        raise ValidationError(str(e)) from e

def _check_no_symlink_chain(path: Path) -> None:
    """Reject if path or any existing ancestor is a symlink (hardening)."""
    # Check the path itself and each existing ancestor up to filesystem root.
    candidates = [path] + list(path.parents)
    for anc in candidates:
        try:
            if anc.exists() and anc.is_symlink():
                raise RuntimeError(f"Symlink in path chain rejected: {anc}")
        except OSError as e:
            raise RuntimeError(f"Failed to stat path chain {anc}: {e}") from e


def _is_regular_file(path: Path) -> bool:
    """Return True iff path is a regular file (not FIFO/socket/device/dir/symlink)."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return False
    import stat as _stat
    return _stat.S_ISREG(st.st_mode)


def _validate_local_files(local_dir: Path) -> None:
    _check_no_symlink_chain(local_dir)
    for fname in CANONICAL_FILES:
        p = local_dir / fname
        if p.is_symlink():
            raise ValueError(f"Local file is symlink (rejected): {fname}")
        if not p.is_file() or not _is_regular_file(p):
            raise ValueError(f"Local file missing or not regular: {fname}")
        # Valid JSON
        try:
            with open(p, "r", encoding="utf-8") as f:
                json.load(f)
        except Exception as e:
            raise ValueError(f"Local file not valid JSON: {fname}: {e}") from e

def _build_candidate_tar_bytes(local_dir: Path, manifest_dict: Dict[str, Any]) -> bytes:
    """Build tar bytes containing manifest.json + state/ four files."""
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w", format=tarfile.PAX_FORMAT) as tar:
        # manifest.json
        m_bytes = json.dumps(manifest_dict, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        ti = tarfile.TarInfo(name="manifest.json")
        ti.size = len(m_bytes)
        ti.mtime = 0
        ti.mode = 0o644
        tar.addfile(ti, io.BytesIO(m_bytes))
        for fname in CANONICAL_FILES:
            fpath = local_dir / fname
            data = fpath.read_bytes()
            ti2 = tarfile.TarInfo(name=f"state/{fname}")
            ti2.size = len(data)
            ti2.mtime = 0
            ti2.mode = 0o644
            tar.addfile(ti2, io.BytesIO(data))
    return bio.getvalue()

def _should_use_local_facade(ssh_target: str, root: str) -> bool:
    """Return True if local BookStore should be used instead of SSH (media self-loop avoidance)."""
    # Only for the approved media root; on RT (Windows) the Linux path won't exist.
    if root != "/home/rt/pact_runs":
        return False
    # Media host has the store parent directory locally.
    try:
        if Path(root).is_dir() and Path(root).exists():
            # Distinguish media host by Linux directory existence; on RT this path doesn't exist.
            # If ssh_target is the restricted facade alias, use local path when store is locally reachable.
            if ssh_target in ("media-snap", "media"):
                return True
    except Exception:
        return False
    return False


def _local_fetch_current(book_id: str, dest_dir: Path, root: str) -> Dict[str, Any]:
    """Local facade fetch: copy from BookStore snapshots without SSH."""
    store = BookStore(book_id, root=root)
    _check_no_symlink_chain(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    current = store.read_current()
    if current is None or current.get("revision_id") is None:
        raise RuntimeError("CURRENT.json not found or no revision (local facade)")
    revision_id = current.get("revision_id")
    snap_dir = store.snapshot_dir(revision_id)
    if snap_dir.is_symlink():
        raise RuntimeError(f"snapshot dir is symlink: {snap_dir}")
    _check_no_symlink_chain(snap_dir)
    if not snap_dir.is_dir():
        raise RuntimeError(f"snapshot dir missing: {snap_dir}")
    manifest_path = snap_dir / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file() or not _is_regular_file(manifest_path):
        raise RuntimeError(f"manifest.json missing or not regular: {manifest_path}")
    # Validate and copy CURRENT.json / manifest.json
    cur_bytes = json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    (dest_dir / "CURRENT.json").write_bytes(cur_bytes)
    (dest_dir / "manifest.json").write_bytes(manifest_path.read_bytes())
    # Copy four canonical files from snapshot state/
    state_dir = snap_dir / "state"
    if state_dir.is_symlink() or not state_dir.is_dir():
        raise RuntimeError(f"state dir missing or symlink: {state_dir}")
    _check_no_symlink_chain(state_dir)
    for fname in CANONICAL_FILES:
        src = state_dir / fname
        if src.is_symlink() or not src.is_file() or not _is_regular_file(src):
            raise RuntimeError(f"state file missing or not regular: {fname}")
        _check_no_symlink_chain(src)
        content = src.read_bytes()
        # Validate JSON
        json.loads(content.decode("utf-8"))
        (dest_dir / fname).write_bytes(content)
    return current


def _local_push_candidate(book_id: str, candidate_id: str, local_dir: Path, manifest_dict: Dict[str, Any], root: str) -> Dict[str, Any]:
    """Local facade push: receive-candidate + promote via BookStore."""
    store = BookStore(book_id, root=root)
    tar_bytes = _build_candidate_tar_bytes(local_dir, manifest_dict)
    from pact_v4.snapshot.cli import _receive_candidate_stream
    from pact_v4.snapshot.promote import promote as _promote
    from pact_v4.snapshot.errors import SnapshotError
    rc = _receive_candidate_stream(store, candidate_id, tar_bytes)
    if rc != 0:
        raise RuntimeError(f"local receive-candidate failed rc={rc}")
    try:
        verdict = _promote(store, candidate_id, operator="rt", host="RT")
        return verdict
    except SnapshotError as e:
        # Promote already quarantined; return REJECTED verdict dict
        return {"status": "REJECTED", "reason": getattr(e, "code", type(e).__name__), "message": str(e), "candidate_id": candidate_id}


def _extract_fetch_tar(tar_bytes: bytes, dest_dir: Path) -> Dict[str, Any]:
    """Validate and extract fetch-current tar (CURRENT.json, manifest.json, state/*) into dest_dir.

    Returns parsed CURRENT.json dict.
    Validates: exact entries, no symlinks, valid JSON, allowed names.
    """
    if not tar_bytes:
        raise RuntimeError("Empty fetch-current response")
    _check_no_symlink_chain(dest_dir)
    bio = io.BytesIO(tar_bytes)
    # Validate members
    with tarfile.open(fileobj=bio, mode="r:*") as tar:
        members = tar.getmembers()
        names = {m.name for m in members if not m.isdir()}
        expected = {"CURRENT.json", "manifest.json"} | CANONICAL_STATE_NAMES
        if names != expected:
            raise RuntimeError(f"Fetch tar must contain exactly {sorted(expected)}, got {sorted(names)}")
        for m in members:
            if m.issym() or m.islnk():
                raise RuntimeError(f"Fetch tar symlink rejected: {m.name}")
            if m.isfifo() or m.ischr() or m.isblk():
                raise RuntimeError(f"Fetch tar special file rejected: {m.name}")
            if m.name.startswith("/") or ".." in m.name.split("/"):
                raise RuntimeError(f"Fetch tar path escape: {m.name}")
        # Extract
        bio.seek(0)
        with tarfile.open(fileobj=bio, mode="r:*") as tar2:
            for m in tar2.getmembers():
                if m.isdir():
                    continue
                f = tar2.extractfile(m)
                if f is None:
                    continue
                content = f.read()
                if m.name == "CURRENT.json":
                    cur = json.loads(content.decode("utf-8"))
                    # also write CURRENT.json to dest_dir for parent tracking
                    (dest_dir / "CURRENT.json").write_bytes(content)
                    # validate json
                elif m.name == "manifest.json":
                    json.loads(content.decode("utf-8"))
                    (dest_dir / "manifest.json").write_bytes(content)
                elif m.name.startswith("state/"):
                    fname = m.name.split("/")[-1]
                    if fname not in CANONICAL_FILES:
                        raise RuntimeError(f"Unexpected state file: {fname}")
                    # Validate JSON
                    json.loads(content.decode("utf-8"))
                    # Write flat to dest_dir/<fname> only (single copy, no state/ mirror)
                    (dest_dir / fname).write_bytes(content)
    # Return CURRENT
    cur_path = dest_dir / "CURRENT.json"
    if cur_path.exists():
        return json.loads(cur_path.read_text(encoding="utf-8"))
    raise RuntimeError("CURRENT.json missing after extract")

# -- Transport dispatch helpers --

def _has_fake_transport(transport) -> bool:
    return transport is not None and any(hasattr(transport, m) for m in ("fetch_current", "push_candidate", "check_expired"))

# Public API

def fetch_current(book_id: str, dest_dir: str | Path, *, transport=None, ssh_target: str = "media", root: str = "/home/rt/pact_runs", timeout: int = 30) -> Dict[str, Any]:
    """Fetch current state from media and write four canonical files to dest_dir.

    Returns parsed CURRENT.json. Validates files are regular, non-symlink, allowed names, valid JSON.
    Fails fast on media unreachable (raises RuntimeError), no silent fallback.
    If transport is injected, delegates to transport.fetch_current(book_id, dest_dir).
    """
    _validate_book_id(book_id)
    dest = Path(dest_dir)
    _check_no_symlink_chain(dest)
    dest.mkdir(parents=True, exist_ok=True)
    _check_no_symlink_chain(dest)
    # Local facade for media self-loop avoidance (media host): prefer BookStore direct I/O.
    if transport is None and _should_use_local_facade(ssh_target, root):
        try:
            return _local_fetch_current(book_id, dest, root)
        except Exception as e:
            # If local facade fails due to missing store, fall through to SSH attempt?
            # For safety, fail-closed: propagate local error rather than silently falling back.
            raise RuntimeError(f"local fetch_current failed: {e}") from e
    if transport is not None and hasattr(transport, "fetch_current"):
        # Fake transport may be callable or object
        result = transport.fetch_current(book_id, dest)  # type: ignore
        # Validate four files after fake fetch
        for fname in CANONICAL_FILES:
            p = dest / fname
            if p.is_symlink():
                raise RuntimeError(f"Fetched file is symlink (rejected): {fname}")
            if not p.is_file():
                raise RuntimeError(f"Fetched file missing: {fname}")
            try:
                json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:
                raise RuntimeError(f"Fetched file not valid JSON: {fname}: {e}") from e
        # Ensure no extra files beyond four + CURRENT/manifest/state (allow those)
        # For state-only, we check dest contains exactly four canonical files (flat)
        # Extra check: no translation bodies
        extra = [p.name for p in dest.iterdir() if p.is_file() and p.name not in set(CANONICAL_FILES) | {"CURRENT.json", "manifest.json"}]
        # If dest has unexpected top-level files beyond allowed, warn but not fail? Fail-closed: reject extra?
        # But dest may contain other files from prior run; we just ensure four are correct.
        return result if isinstance(result, dict) else {}

    # Real transport: ssh media pact-snapshot fetch-current <book-id>
    cmd = ["ssh", ssh_target, "pact-snapshot", "fetch-current", book_id]
    # Note: root handling if custom root, pass via env? facade uses PACT_SNAPSHOT_ROOT env
    env = os.environ.copy()
    if root != "/home/rt/pact_runs":
        env["PACT_SNAPSHOT_ROOT"] = root
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, env=env)
    except FileNotFoundError as e:
        raise RuntimeError(f"ssh executable not found: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"fetch_current timeout: {e}") from e
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace")[:500]
        out = proc.stdout.decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"fetch_current failed (rc={proc.returncode}): stderr={err} stdout={out}")
    try:
        cur = _extract_fetch_tar(proc.stdout, dest)
    except Exception as e:
        raise RuntimeError(f"fetch_current validation failed: {e}") from e
    # Final validation: regular, non-symlink, allowed names, valid JSON (already done)
    for fname in CANONICAL_FILES:
        p = dest / fname
        if p.is_symlink():
            raise RuntimeError(f"Fetched file is symlink (rejected): {fname}")
        if not p.is_file():
            raise RuntimeError(f"Fetched file missing after extract: {fname}")
    return cur

def push_candidate(book_id: str, candidate_id: str, local_dir: str | Path, *, transport=None, ssh_target: str = "media", root: str = "/home/rt/pact_runs", timeout: int = 30, parent_revision_id: Optional[str] = None) -> Dict[str, Any]:
    """Build candidate from local_dir four files, push via receive-candidate + promote, return verdict dict.

    verdict contains status ACCEPTED/REJECTED, revision_id on ACCEPTED, reason on REJECTED.
    Transport injectable: if transport has push_candidate, delegate.
    """
    _validate_book_id(book_id)
    _validate_candidate_id(candidate_id)
    ldir = Path(local_dir)
    _validate_local_files(ldir)
    # Determine parent_revision_id
    if parent_revision_id is None:
        # Try to read CURRENT.json from ldir (written by fetch_current)
        cur_path = ldir / "CURRENT.json"
        if cur_path.is_file() and not cur_path.is_symlink():
            try:
                cur = json.loads(cur_path.read_text(encoding="utf-8"))
                parent_revision_id = cur.get("revision_id")
            except Exception:
                parent_revision_id = None
        if parent_revision_id is None:
            # Try to fetch current revision via transport or ssh
            if transport is not None and hasattr(transport, "get_current_revision"):
                parent_revision_id = transport.get_current_revision(book_id)  # type: ignore
            elif transport is not None and hasattr(transport, "fetch_current"):
                # Use transport to fetch to temp and read revision
                with tempfile.TemporaryDirectory() as tmp:
                    transport.fetch_current(book_id, Path(tmp))  # type: ignore
                    cp = Path(tmp) / "CURRENT.json"
                    if cp.exists():
                        parent_revision_id = json.loads(cp.read_text(encoding="utf-8")).get("revision_id")
            else:
                # Real ssh: fetch CURRENT via helper (reuse fetch to temp)
                with tempfile.TemporaryDirectory() as tmp:
                    try:
                        cur = fetch_current(book_id, tmp, ssh_target=ssh_target, root=root, timeout=timeout)
                        parent_revision_id = cur.get("revision_id")
                    except Exception as e:
                        raise RuntimeError(f"push_candidate: failed to determine parent_revision_id: {e}") from e
    if parent_revision_id is None:
        raise RuntimeError("push_candidate: parent_revision_id unknown (no CURRENT.json and fetch failed)")

    if transport is not None and hasattr(transport, "push_candidate"):
        # Delegate to fake transport; it should handle manifest building and promote
        # Build manifest for transport to use
        from .manifest import compute_sha256_and_size
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        state_files = []
        for fname in CANONICAL_FILES:
            p = ldir / fname
            # compute via temp state file? Use compute_sha256_and_size directly
            h, sz = compute_sha256_and_size(p)
            # But state_files rel_path expects state/<fname>
            state_files.append({"rel_path": f"state/{fname}", "sha256": h, "size": sz})
        manifest_dict = {
            "schema_version": "1.0.0",
            "book_id": book_id,
            "revision_id": "rev-0000",
            "parent_revision_id": parent_revision_id,
            "created_at": now,
            "published_at": now,
            "terminal_status": "complete",
            "tool_version": "pact-snapshot/0.1.0",
            "source": {"path_on_rt": str(ldir), "operator": "rt", "host": "RT"},
            "state_files": state_files,
            "excludes": [],
            "code_commit": "unknown",
        }
        # Fake transport may expect manifest_dict
        try:
            return transport.push_candidate(book_id, candidate_id, ldir, manifest_dict)  # type: ignore
        except TypeError:
            return transport.push_candidate(book_id, candidate_id, ldir)  # type: ignore

    # Real transport: build manifest and tar, then ssh receive-candidate + ssh promote
    from .manifest import compute_sha256_and_size
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    state_files = []
    for fname in CANONICAL_FILES:
        p = ldir / fname
        h, sz = compute_sha256_and_size(p)
        state_files.append({"rel_path": f"state/{fname}", "sha256": h, "size": sz})
    manifest_dict = {
        "schema_version": "1.0.0",
        "book_id": book_id,
        "revision_id": "rev-0000",
        "parent_revision_id": parent_revision_id,
        "created_at": now,
        "published_at": now,
        "terminal_status": "complete",
        "tool_version": "pact-snapshot/0.1.0",
        "source": {"path_on_rt": str(ldir), "operator": "rt", "host": "RT"},
        "state_files": state_files,
        "excludes": [],
        "code_commit": "unknown",
    }
    # Local facade for media self-loop avoidance: use BookStore directly when on media host.
    if transport is None and _should_use_local_facade(ssh_target, root):
        try:
            return _local_push_candidate(book_id, candidate_id, ldir, manifest_dict, root)
        except Exception as e:
            raise RuntimeError(f"local push_candidate failed: {e}") from e
    tar_bytes = _build_candidate_tar_bytes(ldir, manifest_dict)

    # Step 1: receive-candidate
    cmd_recv = ["ssh", ssh_target, "pact-snapshot", "receive-candidate", book_id, candidate_id]
    env = os.environ.copy()
    if root != "/home/rt/pact_runs":
        env["PACT_SNAPSHOT_ROOT"] = root
    try:
        proc_recv = subprocess.run(cmd_recv, input=tar_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, env=env)
    except FileNotFoundError as e:
        raise RuntimeError(f"ssh not found for receive-candidate: {e}") from e
    if proc_recv.returncode != 0:
        err = proc_recv.stderr.decode("utf-8", errors="replace")[:500]
        out = proc_recv.stdout.decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"receive-candidate failed rc={proc_recv.returncode}: stderr={err} stdout={out}")
    # Step 2: promote
    cmd_prom = ["ssh", ssh_target, "pact-snapshot", "promote", book_id, candidate_id]
    try:
        proc_prom = subprocess.run(cmd_prom, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, env=env)
    except FileNotFoundError as e:
        raise RuntimeError(f"ssh not found for promote: {e}") from e
    # Promote returns JSON verdict on stdout even on REJECTED (exit 2)
    try:
        verdict = json.loads(proc_prom.stdout.decode("utf-8"))
    except Exception:
        err = proc_prom.stderr.decode("utf-8", errors="replace")[:1000]
        out = proc_prom.stdout.decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"promote response not JSON (rc={proc_prom.returncode}): stderr={err} stdout={out}")
    return verdict

def check_expired(book_id: str, *, transport=None, ssh_target: str = "media", root: str = "/home/rt/pact_runs", timeout: int = 30) -> Dict[str, Any]:
    _validate_book_id(book_id)
    if transport is not None and hasattr(transport, "check_expired"):
        return transport.check_expired(book_id)  # type: ignore
    cmd = ["ssh", ssh_target, "pact-snapshot", "release-lease", book_id, "--check-expired"]
    env = os.environ.copy()
    if root != "/home/rt/pact_runs":
        env["PACT_SNAPSHOT_ROOT"] = root
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, env=env)
    except FileNotFoundError as e:
        raise RuntimeError(f"ssh not found: {e}") from e
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"check_expired failed rc={proc.returncode}: {err}")
    try:
        data = json.loads(proc.stdout.decode("utf-8"))
        return data.get("check_expired", data)
    except Exception as e:
        raise RuntimeError(f"check_expired response not JSON: {e}") from e
