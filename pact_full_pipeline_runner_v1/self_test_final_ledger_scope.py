#!/usr/bin/env python3
"""Offline integration tests for canonical per-chapter final ledger scoping."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
import argparse

from v31_chapter_resolver import MANIFEST_SCHEMA
from v31_final_ledger_scope import SCHEMA, build_scope
from v31_audit import ledger_target_pids, scoped_ledger_path_for_work, scoped_ledger_paths
from v31_common import add_common_args


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    add_common_args(parser)
    assert parser.parse_args(["--project-root", "root", "--config", "config", "--start", "1", "--end", "2", "--pass-name", "final"]).pass_name == "final"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        manifest = root / "chapter_manifest.v31.json"
        write_json(manifest, {"schema": MANIFEST_SCHEMA, "chapters": [
            {"chapter_id": "1", "source_path": "pact_chapters/0001_one.html", "filename": "0001_one.html"},
            {"chapter_id": "2", "source_path": "pact_chapters/0002_two.html", "filename": "0002_two.html"},
        ]})
        work = root / "work"
        write_json(work / "0001_one" / "v31_final_changed_pid_ledger.json", {"changed_pids": ["one-pid"]})
        write_json(work / "0002_two" / "v31_final_changed_pid_ledger.json", {"changed_pids": ["two-pid"]})
        scope = build_scope(manifest, work, "v31_final_changed_pid_ledger.json")
        assert scope["schema"] == SCHEMA
        scope_path = root / "v31_final_changed_pid_ledger_scope.json"
        write_json(scope_path, scope)
        paths = scoped_ledger_paths(scope_path)
        assert json.loads(paths["0001_one"].read_text(encoding="utf-8"))["changed_pids"] == ["one-pid"]
        assert json.loads(paths["0002_two"].read_text(encoding="utf-8"))["changed_pids"] == ["two-pid"]
        assert ledger_target_pids(["one-pid"], paths["0001_one"], "0001_one") == ["one-pid"]
        assert ledger_target_pids(["two-pid"], paths["0002_two"], "0002_two") == ["two-pid"]
        assert scoped_ledger_path_for_work(paths, work / "0002_two") == paths["0002_two"]
        swapped = dict(paths); swapped["0002_two"] = paths["0001_one"]
        try:
            scoped_ledger_path_for_work(swapped, work / "0002_two")
        except ValueError as exc:
            assert "0002_two" in str(exc)
        else:
            raise AssertionError("Scope map must not point one chapter at another chapter's ledger")
        write_json(paths["0002_two"], {"changed_pids": ["one-pid"]})
        try:
            ledger_target_pids(["two-pid"], paths["0002_two"], "0002_two")
        except ValueError as exc:
            assert "0002_two" in str(exc) and "one-pid" in str(exc)
        else:
            raise AssertionError("Mismatched chapter ledger must not use another chapter's PID namespace")
        write_json(paths["0002_two"], {"changed_pids": ["two-pid"]})
        (work / "0002_two" / "v31_final_changed_pid_ledger.json").unlink()
        try:
            build_scope(manifest, work, "v31_final_changed_pid_ledger.json")
        except FileNotFoundError as exc:
            assert "0002_two" in str(exc)
        else:
            raise AssertionError("Missing chapter ledger must not fall back to another chapter")
    print("Pact v3.1 final ledger scope integration test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
