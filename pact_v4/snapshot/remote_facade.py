"""Restricted media facade for RT SSH access.

Invoked via ``authorized_keys command=`` wrapper. Parses the original SSH
command and allows ONLY:
  - fetch-current <book-id>
  - receive-candidate <book-id> <candidate-id>
  - promote <book-id> <candidate-id>
  - release-lease <book-id> --check-expired

All other subcommands/arguments/book-ids are rejected without side effects.
Calls into existing store functions (lease, promote, bootstrap store ops).

Trust checks precede any I/O: book-id/candidate-id are validated before any
store path is touched or file read.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import traceback
from pathlib import Path
from typing import List, Optional

from .errors import SnapshotError, ValidationError
from .store import BookStore, _validate_component

ALLOWED_SUBCOMMANDS = {"fetch-current", "receive-candidate", "promote", "release-lease"}

def _validate_book_id(book_id: str) -> None:
    try:
        _validate_component(book_id, "book_id")
    except ValueError as e:
        raise ValidationError(str(e)) from e

def _validate_candidate_id(candidate_id: str) -> None:
    try:
        _validate_component(candidate_id, "candidate_id")
    except ValueError as e:
        raise ValidationError(str(e)) from e

def _print_json(obj) -> None:
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    sys.stdout.flush()

def _print_error(status: str, reason: str, message: str) -> None:
    payload = {"status": status, "reason": reason, "message": message}
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    sys.stdout.flush()

def _parse_original_command(argv: Optional[List[str]] = None) -> List[str]:
    """Return token list for the requested subcommand.

    If SSH_ORIGINAL_COMMAND env var is set (authorized_keys command= mode),
    parse it with shlex. Otherwise use argv[1:] (direct invocation).
    Strips leading wrapper name like 'pact-snapshot' if present.
    """
    if "SSH_ORIGINAL_COMMAND" in os.environ:
        raw = os.environ["SSH_ORIGINAL_COMMAND"] or ""
        # If the wrapper is invoked as `command="/.../remote_facade.py"` the
        # original command is what the client sent, e.g. "pact-snapshot fetch-current ..."
        tokens = shlex.split(raw)
        # Remove leading pact-snapshot / pact_v4.snapshot.cli if present
        if tokens and tokens[0] in ("pact-snapshot", "pact_snapshot", "pact-snapshot-cli"):
            tokens = tokens[1:]
        # Also handle case where client sent "pact-snapshot fetch-current ..."
        # already stripped by sshd command= prepending; we just return tokens
        return tokens
    if argv is None:
        argv = sys.argv[1:]
    else:
        argv = list(argv)
    # Strip wrapper name if present
    if argv and argv[0] in ("pact-snapshot", "remote_facade.py", "remote_facade"):
        argv = argv[1:]
    return argv

def _reject(msg: str, code: int = 2) -> int:
    _print_error("REJECTED", "FACADE_REJECTED", msg)
    return code

def handle_request(tokens: List[str], *, root: str = "/home/rt/pact_runs") -> int:
    """Core facade logic: validate allow-list and dispatch. Returns exit code."""
    if not tokens:
        return _reject("Empty command")
    sub = tokens[0]
    if sub not in ALLOWED_SUBCOMMANDS:
        return _reject(f"Subcommand not allowed: {sub!r}")

    try:
        if sub == "fetch-current":
            if len(tokens) != 2:
                return _reject(f"fetch-current requires exactly 1 arg (book-id), got {tokens[1:]}")
            book_id = tokens[1]
            _validate_book_id(book_id)
            # Trust check done; now dispatch to CLI fetch-current handler
            from .cli import _fetch_current_stream
            store = BookStore(book_id, root=root)
            return _fetch_current_stream(store, sys.stdout.buffer)

        elif sub == "receive-candidate":
            if len(tokens) != 3:
                return _reject(f"receive-candidate requires 2 args (book-id candidate-id), got {tokens[1:]}")
            book_id, candidate_id = tokens[1], tokens[2]
            _validate_book_id(book_id)
            _validate_candidate_id(candidate_id)
            from .cli import _receive_candidate_stream
            store = BookStore(book_id, root=root)
            data = sys.stdin.buffer.read()
            return _receive_candidate_stream(store, candidate_id, data)

        elif sub == "promote":
            if len(tokens) != 3:
                return _reject(f"promote requires 2 args (book-id candidate-id), got {tokens[1:]}")
            book_id, candidate_id = tokens[1], tokens[2]
            _validate_book_id(book_id)
            _validate_candidate_id(candidate_id)
            from .promote import promote
            from .lease import read_lease
            store = BookStore(book_id, root=root)
            try:
                result = promote(store, candidate_id=candidate_id, operator="rt", host="RT")
                json.dump(result, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
                sys.stdout.write("\n")
                sys.stdout.flush()
                return 0
            except SnapshotError as e:
                # promote already quarantined; emit REJECTED JSON
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
                    "candidate_id": candidate_id,
                }
                json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
                sys.stdout.write("\n")
                sys.stdout.flush()
                return 2

        elif sub == "release-lease":
            # Only allow: release-lease <book-id> --check-expired
            if len(tokens) != 3 or tokens[2] != "--check-expired":
                return _reject("release-lease only allows: release-lease <book-id> --check-expired")
            book_id = tokens[1]
            _validate_book_id(book_id)
            from .lease import check_expired
            store = BookStore(book_id, root=root)
            report = check_expired(store)
            json.dump({"status": "OK", "check_expired": report}, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
            sys.stdout.write("\n")
            sys.stdout.flush()
            return 0

        else:
            return _reject(f"Unknown subcommand: {sub}")

    except ValidationError as e:
        return _reject(str(e))
    except SnapshotError as e:
        _print_error("REJECTED", type(e).__name__, str(e))
        return 2
    except Exception as e:
        payload = {
            "status": "ERROR",
            "error": type(e).__name__,
            "message": str(e),
            "traceback": traceback.format_exc(),
        }
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        sys.stdout.flush()
        return 3

def main(argv=None, *, root: str = "/home/rt/pact_runs") -> int:
    # Allow explicit root override via env for tests
    root_env = os.environ.get("PACT_SNAPSHOT_ROOT")
    if root_env:
        root = root_env
    tokens = _parse_original_command(argv)
    return handle_request(tokens, root=root)

if __name__ == "__main__":
    raise SystemExit(main())
