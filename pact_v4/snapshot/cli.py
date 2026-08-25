"""CLI for book-state snapshot store."""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tarfile
import traceback
from pathlib import Path

from .bootstrap import bootstrap
from .errors import HashMismatch, LeaseHeld, SnapshotError, StaleParent, ValidationError
from .lease import check_expired, release_with_audit
from .manifest import CANONICAL_STATE_PATHS
from .promote import promote
from .store import BookStore, _validate_component


def _validate_arg_component(name: str, kind: str) -> None:
    try:
        _validate_component(name, kind)
    except ValueError as e:
        raise ValidationError(str(e)) from e


def _print_json(obj) -> None:
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _handle_snapshot_error(e: SnapshotError, candidate_id=None) -> int:
    err_type = type(e).__name__
    reason_map = {
        "LeaseHeld": "LEASE_HELD",
        "StaleParent": "STALE_PARENT",
        "HashMismatch": "HASH_MISMATCH",
        "ValidationError": "VALIDATION_ERROR",
    }
    reason = reason_map.get(err_type, "REJECTED")
    payload = {
        "status": "REJECTED",
        "reason": reason,
        "error": err_type,
        "message": str(e),
    }
    if candidate_id:
        payload["candidate_id"] = candidate_id
    _print_json(payload)
    return 2


# --- fetch-current / receive-candidate helpers ---

CANONICAL_FILES = ["glossary.json", "book_memory.json", "chapter_index.json", "observations.json"]

def _fetch_current_stream(store: BookStore, out_buffer) -> int:
    """Stream CURRENT.json + manifest.json + four canonical files as tar to out_buffer."""
    # Trust checks precede I/O: book_id already validated by caller
    current = store.read_current()
    if current is None or current.get("revision_id") is None:
        raise ValidationError("CURRENT.json not found or no revision")
    revision_id = current.get("revision_id")
    # Validate CURRENT structure minimal
    if not isinstance(revision_id, str) or not revision_id.startswith("rev-"):
        raise ValidationError(f"Invalid revision_id in CURRENT: {revision_id!r}")
    # Load manifest from snapshot dir
    snap_dir = store.snapshot_dir(revision_id)
    if snap_dir.is_symlink():
        raise ValidationError(f"snapshot dir is symlink: {snap_dir}")
    manifest_path = snap_dir / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValidationError(f"manifest.json missing or symlink: {manifest_path}")
    # Stream tar
    # Use stream mode w| not seekable; write to out_buffer
    # For tests, out_buffer may be BytesIO
    with tarfile.open(fileobj=out_buffer, mode="w|", format=tarfile.PAX_FORMAT) as tar:
        # CURRENT.json at top level
        cur_bytes = json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        ti = tarfile.TarInfo(name="CURRENT.json")
        ti.size = len(cur_bytes)
        ti.mtime = 0
        ti.mode = 0o644
        tar.addfile(ti, io.BytesIO(cur_bytes))
        # manifest.json
        manifest_bytes = manifest_path.read_bytes()
        ti2 = tarfile.TarInfo(name="manifest.json")
        ti2.size = len(manifest_bytes)
        ti2.mtime = 0
        ti2.mode = 0o644
        tar.addfile(ti2, io.BytesIO(manifest_bytes))
        # four canonical files from snapshot state/
        state_dir = snap_dir / "state"
        if state_dir.is_symlink() or not state_dir.is_dir():
            raise ValidationError(f"state dir missing or symlink: {state_dir}")
        for fname in CANONICAL_FILES:
            fpath = state_dir / fname
            if fpath.is_symlink() or not fpath.is_file():
                raise ValidationError(f"state file missing or symlink: {fname}")
            data = fpath.read_bytes()
            ti3 = tarfile.TarInfo(name=f"state/{fname}")
            ti3.size = len(data)
            ti3.mtime = 0
            ti3.mode = 0o644
            tar.addfile(ti3, io.BytesIO(data))
    # tarfile w| closes stream but not out_buffer
    try:
        out_buffer.flush()
    except Exception:
        pass
    return 0

def _safe_tar_members(tar_bytes: bytes):
    """Validate tar members allow-list and return TarFile. Raise ValidationError on violation."""
    bio = io.BytesIO(tar_bytes)
    # Use r:* to handle both compressed or not; client sends plain tar
    try:
        tar = tarfile.open(fileobj=bio, mode="r|*")
    except tarfile.TarError as e:
        raise ValidationError(f"Invalid tar archive: {e}") from e
    # For streaming mode we need to iterate; but r|* is streaming, r:* random access
    # Switch to r:* by reopening with BytesIO again if needed
    bio2 = io.BytesIO(tar_bytes)
    try:
        tar2 = tarfile.open(fileobj=bio2, mode="r:*")
        members = list(tar2.getmembers())
        tar2.close()
    except tarfile.TarError as e:
        raise ValidationError(f"Invalid tar archive: {e}") from e
    allowed = {"manifest.json", "state/glossary.json", "state/book_memory.json", "state/chapter_index.json", "state/observations.json"}
    seen = set()
    for m in members:
        name = m.name
        # Reject absolute, traversal, empty, .. segments
        if not name or name.startswith("/") or name.startswith("\\"):
            raise ValidationError(f"Tar entry absolute path rejected: {name!r}")
        if "\\" in name:
            raise ValidationError(f"Tar entry backslash rejected: {name!r}")
        parts = name.split("/")
        if any(p in ("", ".", "..") for p in parts):
            raise ValidationError(f"Tar entry traversal rejected: {name!r}")
        # Reject non-regular entries
        if m.issym() or m.islnk():
            raise ValidationError(f"Tar symlink rejected: {name!r}")
        if m.isfifo() or m.ischr() or m.isblk() or m.isdir():
            # Only allow regular files; directories are allowed only for state/ prefix implicit
            # Our archive should contain only files, not explicit dirs
            if m.isdir():
                # Allow state directory entry if present, but not required
                if name in ("state", "state/"):
                    continue
                raise ValidationError(f"Tar directory entry rejected: {name!r}")
            raise ValidationError(f"Tar special file rejected: {name!r}")
        if not m.isreg():
            raise ValidationError(f"Tar non-regular entry rejected: {name!r}")
        if name not in allowed:
            raise ValidationError(f"Tar unexpected entry: {name!r}")
        seen.add(name)
    if seen != allowed:
        missing = allowed - seen
        extra = seen - allowed
        if missing:
            raise ValidationError(f"Tar missing required entries: {sorted(missing)}")
        if extra:
            raise ValidationError(f"Tar extra entries: {sorted(extra)}")
    return members

def _receive_candidate_stream(store: BookStore, candidate_id: str, data: bytes) -> int:
    """Read candidate tar from data bytes and write under incoming/<candidate-id>/."""
    if not data:
        raise ValidationError("Empty candidate archive (no stdin data)")
    # Validate tar allow-list before any write
    _safe_tar_members(data)
    # Prepare incoming dir
    cand_dir = store.incoming_candidate_path(candidate_id)
    # Ensure parent within store and not symlink
    if cand_dir.exists() or cand_dir.is_symlink():
        raise ValidationError(f"Candidate already exists: {candidate_id}")
    # Defense: incoming_dir must be regular dir
    store.incoming_dir.mkdir(parents=True, exist_ok=True)
    if store.incoming_dir.is_symlink():
        raise ValidationError(f"incoming is symlink: {store.incoming_dir}")
    # Extract to temp then move? Simplify: extract directly but validated
    # Use temp dir to avoid partial write on failure
    import tempfile
    tmp_root = Path(tempfile.mkdtemp(dir=str(store.incoming_dir), prefix=".recv-"))
    try:
        bio = io.BytesIO(data)
        with tarfile.open(fileobj=bio, mode="r:*") as tar:
            # Extract only allowed files, flatten to cand structure
            for m in tar.getmembers():
                if m.isdir():
                    continue
                # m.name is already validated
                # Ensure destination within tmp_root
                dest = tmp_root / m.name
                # Validate containment
                try:
                    dest.resolve().relative_to(tmp_root.resolve())
                except ValueError:
                    raise ValidationError(f"Path escape in tar: {m.name!r}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                f = tar.extractfile(m)
                if f is None:
                    raise ValidationError(f"Failed to extract {m.name!r}")
                content = f.read()
                # Validate JSON well-formed for state files and manifest
                if m.name == "manifest.json" or m.name.startswith("state/"):
                    try:
                        json.loads(content.decode("utf-8"))
                    except Exception as e:
                        raise ValidationError(f"Candidate file not valid JSON: {m.name}: {e}") from e
                dest.write_bytes(content)
        # Verify exactly manifest.json + state/ four files exist in tmp
        if not (tmp_root / "manifest.json").is_file():
            raise ValidationError("Candidate missing manifest.json after extract")
        for fname in CANONICAL_FILES:
            if not (tmp_root / "state" / fname).is_file():
                raise ValidationError(f"Candidate missing state/{fname}")
        # Boundary validation (shared validator) before commit — fail-closed
        from .promote import validate_candidate_boundary
        validate_candidate_boundary(tmp_root)
        # Atomic move tmp_root -> cand_dir
        os.replace(str(tmp_root), str(cand_dir))
        # tmp_root no longer exists after replace, avoid cleanup double
        tmp_root = None
        # Success JSON to stdout (for receive-candidate caller)
        _print_json({"status": "ACCEPTED", "candidate_id": candidate_id, "path": str(cand_dir)})
        return 0
    finally:
        if tmp_root is not None and tmp_root.exists():
            import shutil
            shutil.rmtree(tmp_root, ignore_errors=True)



def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="pact-snapshot")
    parser.add_argument("--root", default="/home/rt/pact_runs", help="Store root (default /home/rt/pact_runs)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init-store", help="Create store skeleton")
    p_init.add_argument("book_id", help="Book id")

    p_boot = sub.add_parser("bootstrap", help="Seed first revision from _bootstrap_inbox")
    p_boot.add_argument("book_id", help="Book id")
    p_boot.add_argument("--ts", default=None, help="Inbox timestamp subdir (default latest)")
    p_boot.add_argument("--operator", default="rt")
    p_boot.add_argument("--host", default="RT")
    p_boot.add_argument("--tool-version", default="pact-snapshot/0.1.0")
    p_boot.add_argument("--source-path", default="D:\\pact\\pact_chapters")
    p_boot.add_argument("--code-commit", default="unknown")

    p_prom = sub.add_parser("promote", help="Promote candidate")
    p_prom.add_argument("book_id", help="Book id")
    p_prom.add_argument("candidate_id", help="Candidate id under incoming/")
    p_prom.add_argument("--operator", default="rt")
    p_prom.add_argument("--host", default="RT")
    p_prom.add_argument("--run-id", default=None)

    p_fetch = sub.add_parser("fetch-current", help="Stream current state as tar to stdout")
    p_fetch.add_argument("book_id", help="Book id")

    p_recv = sub.add_parser("receive-candidate", help="Read candidate tar from stdin into incoming/<candidate-id>/")
    p_recv.add_argument("book_id", help="Book id")
    p_recv.add_argument("candidate_id", help="Candidate id")

    p_rel = sub.add_parser("release-lease", help="Release lease with audit or check expired")
    p_rel.add_argument("book_id", help="Book id")
    p_rel.add_argument("--operator", default="rt")
    p_rel.add_argument("--reason", default="manual release")
    p_rel.add_argument("--prior-staging-reviewed", action="store_true", help="Flag that prior staging was reviewed")
    p_rel.add_argument("--recovery-decision", default="released")
    p_rel.add_argument("--check-expired", action="store_true", help="Read-only report, no release")

    args = parser.parse_args(argv)

    try:
        if args.cmd == "init-store":
            _validate_arg_component(args.book_id, "book_id")
            store = BookStore(args.book_id, root=args.root)
            current = store.init_store()
            _print_json({"status": "ACCEPTED", "book_id": args.book_id, "current": current, "root": str(store.book_dir)})
            return 0

        elif args.cmd == "bootstrap":
            _validate_arg_component(args.book_id, "book_id")
            store = BookStore(args.book_id, root=args.root)
            result = bootstrap(
                store,
                ts=args.ts,
                operator=args.operator,
                host=args.host,
                tool_version=args.tool_version,
                source_path=args.source_path,
                code_commit=args.code_commit,
            )
            _print_json(result)
            return 0

        elif args.cmd == "promote":
            _validate_arg_component(args.book_id, "book_id")
            _validate_arg_component(args.candidate_id, "candidate_id")
            store = BookStore(args.book_id, root=args.root)
            try:
                result = promote(
                    store,
                    candidate_id=args.candidate_id,
                    operator=args.operator,
                    host=args.host,
                    run_id=args.run_id,
                )
                _print_json(result)
                return 0
            except SnapshotError as e:
                # promote already quarantined; print REJECTED
                return _handle_snapshot_error(e, candidate_id=args.candidate_id)

        elif args.cmd == "fetch-current":
            _validate_arg_component(args.book_id, "book_id")
            store = BookStore(args.book_id, root=args.root)
            return _fetch_current_stream(store, sys.stdout.buffer)

        elif args.cmd == "receive-candidate":
            _validate_arg_component(args.book_id, "book_id")
            _validate_arg_component(args.candidate_id, "candidate_id")
            store = BookStore(args.book_id, root=args.root)
            data = sys.stdin.buffer.read()
            return _receive_candidate_stream(store, args.candidate_id, data)

        elif args.cmd == "release-lease":
            _validate_arg_component(args.book_id, "book_id")
            store = BookStore(args.book_id, root=args.root)
            if args.check_expired:
                report = check_expired(store)
                _print_json({"status": "OK", "check_expired": report})
                return 0
            # Require prior_staging_reviewed? Not strictly, allow any
            record = release_with_audit(
                store,
                operator=args.operator,
                reason=args.reason,
                prior_staging_reviewed=args.prior_staging_reviewed,
                recovery_decision=args.recovery_decision,
            )
            _print_json({"status": "ACCEPTED", "action": "released", "audit": record})
            return 0

        else:
            parser.print_help()
            return 2

    except SnapshotError as e:
        return _handle_snapshot_error(e)
    except SystemExit:
        raise
    except Exception as e:
        # Unexpected error -> exit 3, print JSON
        payload = {
            "status": "ERROR",
            "error": type(e).__name__,
            "message": str(e),
            "traceback": traceback.format_exc(),
        }
        try:
            _print_json(payload)
        except Exception:
            print(str(payload), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
