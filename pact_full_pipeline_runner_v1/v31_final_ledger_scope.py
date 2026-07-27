#!/usr/bin/env python3
"""Resolve one immutable final changed-PID ledger for each canonical chapter."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from v31_common import read_json, write_json
from v31_chapter_resolver import MANIFEST_SCHEMA


SCHEMA = "pact-v31-final-ledger-scope/v1"


def build_scope(manifest_path: Path, work_dir: Path, ledger_name: str) -> dict[str, Any]:
    manifest = read_json(manifest_path, {})
    if manifest.get("schema") != MANIFEST_SCHEMA or not isinstance(manifest.get("chapters"), list):
        raise ValueError(f"Invalid canonical chapter manifest: {manifest_path}")
    entries: list[dict[str, str]] = []
    seen_stems: set[str] = set()
    for record in manifest["chapters"]:
        if not isinstance(record, dict) or not all(record.get(key) for key in ("chapter_id", "source_path", "filename")):
            raise ValueError(f"Invalid canonical chapter record: {record!r}")
        stem = Path(str(record["filename"])).stem
        if stem in seen_stems:
            raise ValueError(f"Canonical chapter records resolve to duplicate work stem: {stem}")
        seen_stems.add(stem)
        ledger = work_dir / stem / ledger_name
        if not ledger.is_file():
            raise FileNotFoundError(f"Missing final changed-PID ledger for canonical chapter {record['chapter_id']}: {ledger}")
        entries.append({
            "chapter_id": str(record["chapter_id"]),
            "source_path": str(record["source_path"]),
            "work_stem": stem,
            "ledger_path": str(ledger.resolve()),
        })
    return {"schema": SCHEMA, "chapters": entries}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--ledger-name", default="v31_final_changed_pid_ledger.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    write_json(args.output, build_scope(args.manifest, args.work_dir, args.ledger_name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
