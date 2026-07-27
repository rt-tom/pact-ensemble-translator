#!/usr/bin/env python3
"""Offline integration contract tests for the v3.1.3 stage execution protocol."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from v31_common import VERSION
import v31_stage_protocol as protocol

ROOT = Path(__file__).resolve().parent
PROBE = ROOT / "v31_stage_protocol.py"


def probe(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(PROBE), "--work-dir", str(root), *args], text=True, capture_output=True, check=False)


def assert_outcome(result: subprocess.CompletedProcess[str], code: int, outcome: str) -> None:
    assert result.returncode == code, result.stderr + result.stdout
    assert json.loads(result.stdout)["outcome"] == outcome


def response(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    return json.loads(result.stdout)


def aggregate(root: Path, stem: str, relative: str, value: object) -> None:
    path = root / stem / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp); relative = "v31/primary/qwen_semantic.json"
        common = ("--aggregate-relative-path", relative, "--chapter-stem", "one", "--chapter-stem", "two")
        provenance_path = root / "v31" / "legacy_reuse_provenance.json"
        aggregate(root, "one", relative, {"version": VERSION, "expected": 1, "completed": 1}); aggregate(root, "two", relative, {"version": VERSION, "expected": 1, "completed": 1})
        strict = probe(root, *common)
        assert_outcome(strict, 0, "REUSED")  # aggregate reuse: no model start
        assert response(strict)["provenance"] == {"compatibility_policy": "strict-semantic-version", "reuse_decision": "semantic-version-reused"}
        assert not provenance_path.exists()  # current artifacts never receive legacy state
        (root / "two" / relative).unlink()
        assert_outcome(probe(root, *common), 20, "MODEL_REQUIRED")  # partial cache
        aggregate(root, "two", relative, [])
        assert_outcome(probe(root, *common), 22, "MODEL_REQUIRED")  # invalid aggregate, retry with force
        aggregate(root, "two", relative, {"version": VERSION, "expected": 2, "completed": 1})
        assert_outcome(probe(root, *common), 22, "MODEL_REQUIRED")  # truncated aggregate
        assert_outcome(probe(root, *common, "--force"), 20, "MODEL_REQUIRED")
        aggregate(root, "two", relative, {"version": "not-compatible", "expected": 1, "completed": 1})
        assert_outcome(probe(root, *common), 22, "MODEL_REQUIRED")  # incompatible version never reuses
        assert not provenance_path.exists()
        aggregate(root, "two", relative, {"version": "3.1.2j", "expected": 1, "completed": 1})
        assert_outcome(probe(root, *common), 22, "MODEL_REQUIRED")  # legacy is not silently reused
        legacy_args = (*common, "--allow-legacy-artifact-version", "--stage", "primary-qwen", "--legacy-provenance-path", str(provenance_path))
        legacy = probe(root, *legacy_args)
        assert_outcome(legacy, 0, "REUSED")
        legacy_provenance = response(legacy)["provenance"]
        assert legacy_provenance["legacy_version"] == ["3.1.2j"]
        assert legacy_provenance["reuse_decision"] == "legacy-compatible-reused"
        durable = json.loads(provenance_path.read_text(encoding="utf-8"))
        assert len(durable["records"]) == 1
        record = durable["records"][0]
        assert {"chapter", "stage", "artifact_path", "artifact_version", "expected_semantic_version", "compatibility_policy", "reuse_decision", "timestamp", "artifact_hash"} <= record.keys()
        assert record["artifact_version"] == "3.1.2j" and record["stage"] == "primary-qwen"
        assert_outcome(probe(root, *legacy_args), 0, "REUSED")
        assert len(json.loads(provenance_path.read_text(encoding="utf-8"))["records"]) == 1  # idempotent resume
        aggregate(root, "two", relative, {"version": "3.1.2j", "expected": 2, "completed": 1})
        assert_outcome(probe(root, *legacy_args), 22, "MODEL_REQUIRED")  # stale legacy aggregate
        aggregate(root, "two", relative, [])
        assert_outcome(probe(root, *legacy_args), 22, "MODEL_REQUIRED")  # malformed legacy aggregate
        assert len(json.loads(provenance_path.read_text(encoding="utf-8"))["records"]) == 1
        preserved = provenance_path.read_text(encoding="utf-8")
        original_replace = protocol.os.replace
        protocol.os.replace = lambda *_: (_ for _ in ()).throw(OSError("synthetic atomic failure"))
        try:
            try: protocol.write_json_atomic(provenance_path, {"records": []})
            except OSError: pass
            else: raise AssertionError("Atomic write failure was not raised")
        finally:
            protocol.os.replace = original_replace
        assert provenance_path.read_text(encoding="utf-8") == preserved
        assert_outcome(probe(root, "--translation", "--chapter-stem", "one"), 20, "MODEL_REQUIRED")
    print("Pact v3.1 stage protocol offline integration tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
