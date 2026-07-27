#!/usr/bin/env python3
"""Offline integration contract tests for the v3.1.3 stage execution protocol."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from v31_common import VERSION

ROOT = Path(__file__).resolve().parent
PROBE = ROOT / "v31_stage_protocol.py"


def probe(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(PROBE), "--work-dir", str(root), *args], text=True, capture_output=True, check=False)


def assert_outcome(result: subprocess.CompletedProcess[str], code: int, outcome: str) -> None:
    assert result.returncode == code, result.stderr + result.stdout
    assert json.loads(result.stdout)["outcome"] == outcome


def aggregate(root: Path, stem: str, relative: str, value: object) -> None:
    path = root / stem / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp); relative = "v31/primary/qwen_semantic.json"
        common = ("--aggregate-relative-path", relative, "--chapter-stem", "one", "--chapter-stem", "two")
        aggregate(root, "one", relative, {"version": VERSION, "expected": 1, "completed": 1}); aggregate(root, "two", relative, {"version": VERSION, "expected": 1, "completed": 1})
        assert_outcome(probe(root, *common), 0, "REUSED")  # aggregate reuse: no model start
        (root / "two" / relative).unlink()
        assert_outcome(probe(root, *common), 20, "MODEL_REQUIRED")  # partial cache
        aggregate(root, "two", relative, [])
        assert_outcome(probe(root, *common), 22, "MODEL_REQUIRED")  # invalid aggregate, retry with force
        aggregate(root, "two", relative, {"version": VERSION, "expected": 2, "completed": 1})
        assert_outcome(probe(root, *common), 22, "MODEL_REQUIRED")  # truncated aggregate
        assert_outcome(probe(root, *common, "--force"), 20, "MODEL_REQUIRED")
        aggregate(root, "two", relative, {"version": "not-compatible", "expected": 1, "completed": 1})
        assert_outcome(probe(root, *common), 22, "MODEL_REQUIRED")  # incompatible version never reuses
        aggregate(root, "two", relative, {"version": "3.1.2j", "expected": 1, "completed": 1})
        assert_outcome(probe(root, *common), 22, "MODEL_REQUIRED")  # legacy is not silently reused
        assert_outcome(probe(root, *common, "--allow-legacy-artifact-version"), 0, "REUSED")
        assert_outcome(probe(root, "--translation", "--chapter-stem", "one"), 20, "MODEL_REQUIRED")
    print("Pact v3.1 stage protocol offline integration tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
