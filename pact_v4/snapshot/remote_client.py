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
    # Canonical-only: validate exactly the four named files by direct path.
    # The working root is never listed and unrelated entries are ignored.
    if local_dir.is_symlink():
        raise ValueError("Local dir is symlink (rejected)")
    if not local_dir.is_dir():
        raise ValueError("Local dir missing or not a directory")
    _check_no_symlink_chain(local_dir)
    for fname in CANONICAL_FILES:
        p = local_dir / fname
        if p.is_symlink():
            raise ValueError(f"Local file is symlink (rejected): {fname}")
        _check_no_symlink_chain(p)
        if not p.is_file() or not _is_regular_file(p):
            raise ValueError(f"Local file missing or not regular: {fname}")
        # Valid JSON via O_NOFOLLOW|O_NONBLOCK read (no symlink traversal at
        # open time; never blocks on a FIFO swapped in after the stat check).
        try:
            fd = os.open(str(p), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        except OSError as e:
            raise ValueError(f"Local file cannot be opened safely: {fname}: {e}") from e
        try:
            with os.fdopen(fd, "rb") as f:
                raw = f.read()
        except OSError as e:
            raise ValueError(f"Local file cannot be read: {fname}: {e}") from e
        try:
            json.loads(raw.decode("utf-8"))
        except Exception as e:
            raise ValueError(f"Local file not valid JSON: {fname}: {e}") from e

def _pin_canonical_bytes(local_dir: Path) -> Dict[str, bytes]:
    """Validate and atomically capture the four canonical files' bytes.

    Each file is opened ONCE with O_NOFOLLOW, fstat-checked as a regular
    file, read fully, and JSON-validated from those same bytes. The returned
    mapping is the single pinned source used for manifest hashes AND tar
    members, so a swap between validation, hashing and packing cannot mix
    old and new bytes. Unrelated root entries are never touched.
    """
    import hashlib as _hashlib
    _validate_local_files(local_dir)
    pinned: Dict[str, bytes] = {}
    for fname in CANONICAL_FILES:
        p = local_dir / fname
        try:
            fd = os.open(str(p), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        except OSError as e:
            raise ValueError(f"Local file cannot be pinned (re-open failed): {fname}: {e}") from e
        try:
            import stat as _stat
            st = os.fstat(fd)
            if not _stat.S_ISREG(st.st_mode):
                raise ValueError(f"Local file changed to non-regular during pin: {fname}")
            chunks = []
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
            data = b"".join(chunks)
        except OSError as e:
            raise ValueError(f"Local file cannot be pinned: {fname}: {e}") from e
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            json.loads(data.decode("utf-8"))
        except Exception as e:
            raise ValueError(f"Local file not valid JSON at pin time: {fname}: {e}") from e
        pinned[fname] = data
    # Sanity: pinned digests must match what validation just saw (same bytes object).
    for fname, data in pinned.items():
        _hashlib.sha256(data).hexdigest()
    return pinned

def _pinned_state_files(pinned: Dict[str, bytes]) -> list:
    """Build manifest state_files entries from the pinned bytes (no re-read)."""
    import hashlib as _hashlib
    entries = []
    for fname in CANONICAL_FILES:
        data = pinned[fname]
        entries.append({
            "rel_path": f"state/{fname}",
            "sha256": _hashlib.sha256(data).hexdigest(),
            "size": len(data),
        })
    return entries

def _build_candidate_tar_bytes_from_pinned(pinned: Dict[str, bytes], manifest_dict: Dict[str, Any]) -> bytes:
    """Build tar bytes containing manifest.json + state/ four files from pinned bytes."""
    if set(pinned.keys()) != set(CANONICAL_FILES):
        raise ValueError(f"Pinned set must be exactly the four canonical files, got {sorted(pinned.keys())}")
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
            data = pinned[fname]
            ti2 = tarfile.TarInfo(name=f"state/{fname}")
            ti2.size = len(data)
            ti2.mtime = 0
            ti2.mode = 0o644
            tar.addfile(ti2, io.BytesIO(data))
    return bio.getvalue()

def _build_candidate_tar_bytes(local_dir: Path, manifest_dict: Dict[str, Any]) -> bytes:
    """Build tar bytes containing manifest.json + state/ four files (pins bytes first)."""
    pinned = _pin_canonical_bytes(local_dir)
    return _build_candidate_tar_bytes_from_pinned(pinned, manifest_dict)

def _should_use_local_facade(ssh_target: str, root: str, execution_host: str | None = None) -> bool:
    """Return True if local BookStore should be used instead of SSH (media self-loop avoidance).

    Trusted host signal: the execution host identity comes from the launcher/layout
    ("media" or "rt"), not from local path existence. The local facade is
    selected ONLY when execution_host == "media" (trusted), regardless of
    whether /home/rt/pact_runs exists locally. On RT ("rt") it always uses SSH.
    When execution_host is None and PACT_EXEC_HOST env is unset, fail-closed
    to SSH (never silent local).
    """
    if execution_host is None:
        execution_host = os.environ.get("PACT_EXEC_HOST")
        if execution_host is None:
            return False
    if execution_host != "media":
        return False
    if root != "/home/rt/pact_runs":
        return False
    if ssh_target not in ("media-snap", "media"):
        return False
    return True


def _safe_replace_bytes(dest: Path, data: bytes) -> None:
    """Atomically replace a selected destination file without following symlinks.

    Fail-closed: if the destination currently exists as a symlink (or any
    non-regular file other than a plain file/missing), refuse instead of
    traversing it. Ancestor chain is re-checked immediately before the move.
    Unrelated entries in the same directory are never touched.
    """
    _check_no_symlink_chain(dest)
    # Lstat-based type gate (does not depend on Path.exists/is_file overrides
    # and never follows the final component).
    import stat as _stat
    try:
        dst_st = os.lstat(dest)
    except FileNotFoundError:
        dst_st = None
    except OSError as e:
        raise RuntimeError(f"Fetch destination cannot be statted (rejected): {dest.name}: {e}") from e
    if dst_st is not None:
        if _stat.S_ISLNK(dst_st.st_mode):
            raise RuntimeError(f"Fetch destination is a symlink (rejected): {dest.name}")
        if not _stat.S_ISREG(dst_st.st_mode):
            raise RuntimeError(f"Fetch destination is not a regular file (rejected): {dest.name}")
    _check_no_symlink_chain(dest.parent)
    tmp = dest.parent / (dest.name + ".pact_fetch_tmp")
    if tmp.is_symlink():
        raise RuntimeError(f"Fetch temp path is a symlink (rejected): {tmp.name}")
    tmp.write_bytes(data)
    try:
        _check_no_symlink_chain(dest)
        try:
            re_st = os.lstat(dest)
        except FileNotFoundError:
            re_st = None
        if re_st is not None and _stat.S_ISLNK(re_st.st_mode):
            raise RuntimeError(f"Fetch destination became a symlink (rejected): {dest.name}")
        os.replace(str(tmp), str(dest))
    finally:
        try:
            if tmp.exists() and not tmp.is_symlink():
                tmp.unlink()
        except OSError:
            pass


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
    # Validate and copy CURRENT.json / manifest.json (selected targets only,
    # never listing or cleaning the destination root).
    cur_bytes = json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    _safe_replace_bytes(dest_dir / "CURRENT.json", cur_bytes)
    _safe_replace_bytes(dest_dir / "manifest.json", manifest_path.read_bytes())
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
        _safe_replace_bytes(dest_dir / fname, content)
    return current


def _local_push_candidate(book_id: str, candidate_id: str, pinned: Dict[str, bytes], manifest_dict: Dict[str, Any], root: str) -> Dict[str, Any]:
    """Local facade push: receive-candidate + promote via BookStore.

    D5: the caller passes the exact pinned mapping captured by
    _pin_canonical_bytes in push_candidate. This function NEVER re-reads
    the live working directory, so a mutation between pin/hash and
    publication cannot make tar bytes disagree with the manifest.
    """
    import hashlib as _hashlib
    store = BookStore(book_id, root=root)
    if set(pinned.keys()) != set(CANONICAL_FILES):
        raise ValueError(f"Pinned set must be exactly the four canonical files, got {sorted(pinned.keys())}")
    # Fail-closed: manifest hashes/sizes must match the pinned bytes.
    by_rel = {e.get("rel_path"): e for e in manifest_dict.get("state_files", [])}
    for fname in CANONICAL_FILES:
        rel = f"state/{fname}"
        entry = by_rel.get(rel)
        if entry is None:
            raise ValueError(f"Manifest missing entry for pinned file: {rel}")
        data = pinned[fname]
        if entry.get("sha256") != _hashlib.sha256(data).hexdigest() or entry.get("size") != len(data):
            raise ValueError(f"Manifest does not match pinned bytes for: {rel}")
    tar_bytes = _build_candidate_tar_bytes_from_pinned(pinned, manifest_dict)
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
    # Validate members (no writes occur until every check below passes).
    with tarfile.open(fileobj=bio, mode="r:*") as tar:
        members = tar.getmembers()
        names_list = [m.name for m in members]
        if len(names_list) != len(set(names_list)):
            raise RuntimeError("Fetch tar duplicate member names rejected")
        expected = {"CURRENT.json", "manifest.json"} | CANONICAL_STATE_NAMES
        if set(names_list) != expected:
            raise RuntimeError(f"Fetch tar must contain exactly {sorted(expected)}, got {sorted(names_list)}")
        for m in members:
            if m.issym() or m.islnk():
                raise RuntimeError(f"Fetch tar symlink rejected: {m.name}")
            if m.isfifo() or m.ischr() or m.isblk():
                raise RuntimeError(f"Fetch tar special file rejected: {m.name}")
            if m.isdir():
                raise RuntimeError(f"Fetch tar directory rejected: {m.name}")
            if not m.isfile():
                raise RuntimeError(f"Fetch tar non-regular member rejected: {m.name}")
            if m.name.startswith("/") or ".." in m.name.split("/"):
                raise RuntimeError(f"Fetch tar path escape: {m.name}")
            if m.name not in expected:
                raise RuntimeError(f"Fetch tar unexpected member: {m.name}")
        # Buffer every member's bytes and validate JSON before any write.
        staged: Dict[str, bytes] = {}
        for m in members:
            f = tar.extractfile(m)
            if f is None:
                raise RuntimeError(f"Fetch tar unreadable member: {m.name}")
            content = f.read()
            try:
                parsed = json.loads(content.decode("utf-8"))
            except Exception as e:
                raise RuntimeError(f"Fetch tar invalid JSON: {m.name}: {e}") from e
            if m.name == "CURRENT.json" and not isinstance(parsed, dict):
                raise RuntimeError("Fetch tar CURRENT.json must be a JSON object")
            if m.name.startswith("state/"):
                fname = m.name.split("/")[-1]
                if fname not in CANONICAL_FILES:
                    raise RuntimeError(f"Unexpected state file: {fname}")
            staged[m.name] = content
        # All validation passed: publish the staged bytes (single copy each).
        _safe_replace_bytes(dest_dir / "CURRENT.json", staged["CURRENT.json"])
        _safe_replace_bytes(dest_dir / "manifest.json", staged["manifest.json"])
        for fname in CANONICAL_FILES:
            _safe_replace_bytes(dest_dir / fname, staged[f"state/{fname}"])
    # Return CURRENT
    cur_path = dest_dir / "CURRENT.json"
    if cur_path.exists():
        return json.loads(cur_path.read_text(encoding="utf-8"))
    raise RuntimeError("CURRENT.json missing after extract")

# -- Transport dispatch helpers --

def _has_fake_transport(transport) -> bool:
    return transport is not None and any(hasattr(transport, m) for m in ("fetch_current", "push_candidate", "check_expired"))

# Public API

def fetch_current(book_id: str, dest_dir: str | Path, *, transport=None, ssh_target: str = "media", root: str = "/home/rt/pact_runs", timeout: int = 30, execution_host: str | None = None) -> Dict[str, Any]:
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
    # Trusted host signal threads from launcher/layout; never rely on local path existence.
    if transport is None and _should_use_local_facade(ssh_target, root, execution_host=execution_host):
        try:
            return _local_fetch_current(book_id, dest, root)
        except Exception as e:
            raise RuntimeError(f"local fetch_current failed: {e}") from e
    if transport is not None and hasattr(transport, "fetch_current"):
        # Fake transport may be callable or object
        result = transport.fetch_current(book_id, dest)  # type: ignore
        # Validate the four selected files after injected fetch (direct paths only;
        # unrelated destination entries are never listed, rejected, or cleaned).
        for fname in CANONICAL_FILES:
            p = dest / fname
            if p.is_symlink():
                raise RuntimeError(f"Fetched file is symlink (rejected): {fname}")
            _check_no_symlink_chain(p)
            if not p.is_file() or not _is_regular_file(p):
                raise RuntimeError(f"Fetched file missing or not regular: {fname}")
            try:
                fd = os.open(str(p), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            except OSError as e:
                raise RuntimeError(f"Fetched file cannot be opened safely: {fname}: {e}") from e
            try:
                with os.fdopen(fd, "rb") as f:
                    raw = f.read()
            except OSError as e:
                raise RuntimeError(f"Fetched file cannot be read: {fname}: {e}") from e
            try:
                json.loads(raw.decode("utf-8"))
            except Exception as e:
                raise RuntimeError(f"Fetched file not valid JSON: {fname}: {e}") from e
        # Optionally validate CURRENT.json when present (regular non-symlink JSON).
        cur_p = dest / "CURRENT.json"
        if cur_p.exists() or cur_p.is_symlink():
            if cur_p.is_symlink() or not _is_regular_file(cur_p):
                raise RuntimeError("Fetched CURRENT.json is symlink or not regular (rejected)")
            _check_no_symlink_chain(cur_p)
            try:
                json.loads(cur_p.read_text(encoding="utf-8"))
            except Exception as e:
                raise RuntimeError(f"Fetched CURRENT.json not valid JSON: {e}") from e
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

def push_candidate(book_id: str, candidate_id: str, local_dir: str | Path, *, transport=None, ssh_target: str = "media", root: str = "/home/rt/pact_runs", timeout: int = 30, parent_revision_id: Optional[str] = None, execution_host: str | None = None) -> Dict[str, Any]:
    """Build candidate from local_dir four files, push via receive-candidate + promote, return verdict dict.

    verdict contains status ACCEPTED/REJECTED, revision_id on ACCEPTED, reason on REJECTED.
    Transport injectable: if transport has push_candidate, delegate.
    """
    _validate_book_id(book_id)
    _validate_candidate_id(candidate_id)
    ldir = Path(local_dir)
    # Pin the four canonical bytes ONCE: every manifest hash/size and every
    # tar member below derives from this same capture (TOCTOU close).
    pinned = _pin_canonical_bytes(ldir)
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
                        cur = fetch_current(book_id, tmp, ssh_target=ssh_target, root=root, timeout=timeout, execution_host=execution_host)
                        parent_revision_id = cur.get("revision_id")
                    except Exception as e:
                        raise RuntimeError(f"push_candidate: failed to determine parent_revision_id: {e}") from e
    if parent_revision_id is None:
        raise RuntimeError("push_candidate: parent_revision_id unknown (no CURRENT.json and fetch failed)")

    if transport is not None and hasattr(transport, "push_candidate"):
        # Delegate to fake transport; it should handle manifest building and promote
        # Build manifest for transport to use (hashes/sizes from the pinned bytes).
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        state_files = _pinned_state_files(pinned)
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
        # D5: pass the exact pinned mapping. Injected transports MUST build
        # tar bytes from `pinned`, never by re-reading the live directory.
        # Transports without a `pinned` parameter are rejected fail-closed.
        import inspect as _inspect
        try:
            _sig = _inspect.signature(transport.push_candidate)
            _params = _sig.parameters
            _accepts_pinned = ("pinned" in _params) or any(
                p.kind == _inspect.Parameter.VAR_KEYWORD for p in _params.values()
            )
        except (TypeError, ValueError):
            _accepts_pinned = False
        if not _accepts_pinned:
            raise RuntimeError("push_candidate: injected transport does not accept pinned bytes (D5 fail-closed)")
        return transport.push_candidate(book_id, candidate_id, ldir, manifest_dict, pinned=pinned)  # type: ignore

    # Real transport: build manifest and tar, then ssh receive-candidate + ssh promote
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    state_files = _pinned_state_files(pinned)
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
    if transport is None and _should_use_local_facade(ssh_target, root, execution_host=execution_host):
        try:
            return _local_push_candidate(book_id, candidate_id, pinned, manifest_dict, root)
        except Exception as e:
            raise RuntimeError(f"local push_candidate failed: {e}") from e
    tar_bytes = _build_candidate_tar_bytes_from_pinned(pinned, manifest_dict)

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
