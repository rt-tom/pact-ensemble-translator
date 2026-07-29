"""Regression coverage for compact final Qwen global-smoke responses."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path


PACKAGE = Path(__file__).parent / "pact_full_pipeline_runner_v1"
sys.path.insert(0, str(PACKAGE))
from v31_audit import parse_global_smoke  # noqa: E402


PIDS = ["p00001", "p00002", "p00003"]
ISSUE = {
    "pid": "p00002", "severity": "major", "category": "meaning",
    "source_span": "source", "target_span": "target", "problem": "problem",
    "required_invariant": "invariant", "repair_instruction": "repair",
    "scope": "sentence", "confidence": "high",
}


class GlobalSmokeContractTests(unittest.TestCase):
    def test_compact_issues_only_response_covers_whole_chapter(self):
        issues, coverage = parse_global_smoke({
            "coverage": {"first_pid": "p00001", "last_pid": "p00003", "pid_count": 3},
            "issues": [ISSUE],
        }, PIDS, "qwen_global_smoke_final")
        self.assertEqual(PIDS, coverage)
        self.assertEqual(["p00002"], [issue["pid"] for issue in issues])

    def test_compact_response_rejects_false_or_incomplete_coverage(self):
        with self.assertRaisesRegex(ValueError, "coverage"):
            parse_global_smoke({"coverage": {"pid_count": 3}, "issues": []}, PIDS, "qwen")

    def test_compact_response_rejects_unknown_issue_pid(self):
        unknown = dict(ISSUE, pid="p99999")
        with self.assertRaisesRegex(ValueError, "unexpected PID"):
            parse_global_smoke({
                "coverage": {"first_pid": "p00001", "last_pid": "p00003", "pid_count": 3},
                "issues": [unknown],
            }, PIDS, "qwen")


if __name__ == "__main__":
    unittest.main()
