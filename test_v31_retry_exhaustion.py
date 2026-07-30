"""Regression coverage for fail-open repair exhaustion policy."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PACKAGE = Path(__file__).parent / "pact_full_pipeline_runner_v1"
sys.path.insert(0, str(PACKAGE))


def load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, PACKAGE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


adjudicate = load_module("v31_adjudicate")
finalize_quality = load_module("v31_finalize_quality")


class RetryExhaustionTests(unittest.TestCase):
    def test_terminal_round_keeps_current_translation_and_resolves_lifecycle(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp) / "work"
            root = work / "v31" / "primary"
            root.mkdir(parents=True)
            current = "Исправленный вручную текущий перевод."
            candidate = {
                "pid": "p00001", "candidate_id": "c1", "action": "repair",
                "before": current, "after": "Отклонённый кандидат.", "changed_ratio": 0.5,
            }
            adjudicate.write_json(root / "repair_candidates_round_03.json", {
                "records": [{"pid": "p00001", "issues": [{"issue_id": "i1"}], "candidates": [candidate]}],
            })
            failed_semantic = {
                "expected": 1, "completed": 1,
                "decisions": [{"pid": "p00001", "candidate_id": "c1", "confidence": "high", "verdict": "reject"}],
            }
            for name in ("post_gate_qwen_semantic", "post_gate_gemma_semantic"):
                adjudicate.write_json(root / f"{name}_round_03.json", failed_semantic)
            adjudicate.write_json(root / "post_gate_gemma_russian_round_03.json", failed_semantic)
            adjudicate.write_json(root / "post_gate_deterministic_round_03.json", {
                "expected": 1, "completed": 1,
                "decisions": [{"pid": "p00001", "candidate_id": "c1", "passed": True}],
            })

            argv = [
                "v31_adjudicate.py", "--project-root", temp, "--config", str(Path(temp) / "config.json"),
                "--start", "100", "--end", "100", "--round", "3", "--pass-name", "primary",
                "--terminal-round",
            ]
            source = Path(temp) / "chapter.html"
            with patch.object(sys, "argv", argv), \
                 patch.object(adjudicate, "load_runtime", return_value=object()), \
                 patch.object(adjudicate, "load_cfg", return_value={}), \
                 patch.object(adjudicate, "selected_chapters", return_value=[(source, work)]), \
                 patch.object(adjudicate, "load_translations", return_value={"p00001": current}):
                self.assertEqual(0, adjudicate.main())

            self.assertEqual(current, adjudicate.read_json(work / "v31_primary_translations.json")["p00001"])
            self.assertEqual([], adjudicate.read_json(root / "retry_requests_round_03.json"))
            self.assertEqual("kept_after_retry_exhausted", adjudicate.read_json(root / "adjudication_round_03.json")["decisions"][0]["outcome"])
            self.assertEqual("resolved_retry_exhausted", adjudicate.read_json(root / "lifecycle.json")[0]["status"])
            self.assertEqual(0, adjudicate.read_json(root / "status.json")["retry_required"])

    def test_final_quality_accepts_explicit_exhausted_terminal_status(self):
        self.assertIn("resolved_retry_exhausted", finalize_quality.RESOLVED_LIFECYCLE_STATUSES)

    def test_runner_marks_only_last_allowed_round_terminal(self):
        runner = (PACKAGE / "run_full_pipeline_v31.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("if ($round -eq $maxRounds) { $adjudicationArgs += '--terminal-round' }", runner)


if __name__ == "__main__":
    unittest.main()
