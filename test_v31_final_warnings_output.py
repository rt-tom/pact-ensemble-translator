"""Regression coverage for final output with durable quality warnings."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path


PACKAGE = Path(__file__).parent / "pact_full_pipeline_runner_v1"
sys.path.insert(0, str(PACKAGE))
from v31_final_lifecycle import TERMINAL, terminal_status  # noqa: E402


class FinalWarningsOutputTests(unittest.TestCase):
    def test_blocking_final_finding_allows_output_with_explicit_warning(self):
        self.assertEqual("complete_with_warnings", terminal_status(
            ledger_ok=True, coverage_ok=True, verification_ok=True, smoke_ok=True,
            blocking_findings=[{"pid": "p00001"}], final_repair_rounds=1,
        ))

    def test_legacy_quarantine_is_not_silently_promoted_to_clean_complete(self):
        self.assertEqual("complete_with_warnings", terminal_status(
            ledger_ok=True, coverage_ok=True, verification_ok=True, smoke_ok=True,
            blocking_findings=[], final_repair_rounds=1, prior_status="quarantined",
        ))

    def test_execution_failure_still_blocks_output(self):
        self.assertEqual("failed", terminal_status(
            ledger_ok=True, coverage_ok=False, verification_ok=True, smoke_ok=True,
            blocking_findings=[{"pid": "p00001"}], final_repair_rounds=1,
        ))

    def test_warning_status_is_terminal(self):
        self.assertIn("complete_with_warnings", TERMINAL)

    def test_runner_only_blocks_finalization_for_explicit_quarantine(self):
        runner = (PACKAGE / "run_full_pipeline_v31.ps1").read_text(encoding="utf-8-sig")
        self.assertIn(".status -eq 'quarantined'", runner)


if __name__ == "__main__":
    unittest.main()
