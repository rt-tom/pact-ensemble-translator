#!/usr/bin/env python3
"""Model-free, machine-readable execution probe for v3.1 model stages.

Exit codes are the contract consumed by the PowerShell runner: 0=REUSED,
20=MODEL_REQUIRED, 21=FAILED.  The JSON line is for logs and offline tools;
the runner deliberately does not parse console text.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from v31_common import compatible_artifact_version

REUSED = 0
MODEL_REQUIRED = 20
MODEL_REQUIRED_INVALID = 22
FAILED = 21


def emit(outcome: str, **detail: object) -> int:
    print(json.dumps({"protocol": "pact-v31-stage-execution/v1", "outcome": outcome, **detail}, ensure_ascii=False))
    if outcome == "MODEL_REQUIRED" and detail.get("reason") == "missing_partial_or_invalid_aggregate" and detail.get("has_invalid"):
        return MODEL_REQUIRED_INVALID
    return {"REUSED": REUSED, "MODEL_REQUIRED": MODEL_REQUIRED, "FAILED": FAILED}[outcome]


def valid_aggregate(path: Path, *, allow_legacy_artifact_version: bool = False) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    # All v3.1 aggregates carry a producer version.  Completion counters are
    # checked when present, so a truncated but parseable aggregate cannot be
    # promoted to REUSED.  Fine-grained cache identity remains validated by the
    # stage itself on the MODEL_REQUIRED execution path.
    if not isinstance(value, dict) or not compatible_artifact_version(
        value.get("version"), allow_legacy=allow_legacy_artifact_version
    ):
        return False
    expected, completed = value.get("expected"), value.get("completed")
    if expected is not None or completed is not None:
        if not isinstance(expected, int) or not isinstance(completed, int) or expected != completed:
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--chapter-stem", action="append", default=[])
    parser.add_argument("--aggregate-relative-path")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--translation", action="store_true")
    parser.add_argument("--allow-legacy-artifact-version", action="store_true",
                        help="Allow only explicitly listed legacy artifact versions to be reused.")
    args = parser.parse_args()
    if not args.chapter_stem:
        return emit("FAILED", reason="empty_chapter_selection")
    if args.translation:
        # Translation has no v3.1 aggregate with a completion identity.  Never
        # infer completion from an output file; a future translation aggregate
        # may tighten this to REUSED after proving every chunk.
        return emit("MODEL_REQUIRED", reason="translation_completion_not_proven")
    if args.force:
        return emit("MODEL_REQUIRED", reason="forced")
    if not args.aggregate_relative_path:
        return emit("FAILED", reason="missing_aggregate_relative_path")
    paths = [args.work_dir / stem / args.aggregate_relative_path for stem in args.chapter_stem]
    missing = [str(path) for path in paths if not path.exists()]
    invalid = [
        str(path) for path in paths
        if path.exists() and not valid_aggregate(
            path, allow_legacy_artifact_version=args.allow_legacy_artifact_version
        )
    ]
    if missing or invalid:
        return emit("MODEL_REQUIRED", reason="missing_partial_or_invalid_aggregate", missing=missing, invalid=invalid, has_invalid=bool(invalid))
    return emit("REUSED", aggregate_count=len(paths))


if __name__ == "__main__":
    raise SystemExit(main())
