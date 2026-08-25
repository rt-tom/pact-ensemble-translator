"""CLI for book-state snapshot store."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from .bootstrap import bootstrap
from .errors import HashMismatch, LeaseHeld, SnapshotError, StaleParent, ValidationError
from .lease import check_expired, release_with_audit
from .promote import promote
from .store import BookStore


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
            store = BookStore(args.book_id, root=args.root)
            current = store.init_store()
            _print_json({"status": "ACCEPTED", "book_id": args.book_id, "current": current, "root": str(store.book_dir)})
            return 0

        elif args.cmd == "bootstrap":
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

        elif args.cmd == "release-lease":
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
