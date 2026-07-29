"""Regression coverage for non-authoritative Qwen global-smoke diagnostics."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


PACKAGE = Path(__file__).parent / "pact_full_pipeline_runner_v1"
sys.path.insert(0, str(PACKAGE))
from v31_common import JsonGenerationError, complete_json  # noqa: E402


class _Runtime:
    @staticmethod
    def fit_output_budget(_client, _messages, _stage, maximum):
        return maximum

    @staticmethod
    def safe_json_loads(_text):
        raise ValueError("invalid JSON")


class _Client:
    @staticmethod
    def complete(_messages, _stage, _maximum, _label):
        return SimpleNamespace(
            content='{"results":[{"pid":"p00059"',
            finish_reason="stop",
            usage={"completion_tokens": 5000},
            wall_seconds=1.25,
        )


class GlobalSmokeDiagnosticTests(unittest.TestCase):
    def test_rejected_response_is_saved_outside_authoritative_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            diagnostics = Path(temp) / "v31" / "final" / "diagnostics" / "qwen_global_smoke"
            with self.assertRaises(JsonGenerationError):
                complete_json(
                    _Runtime(), _Client(), [], {}, 5000, "qwen_global_smoke_final:chapter:unit1",
                    attempts=1, diagnostics_dir=diagnostics, diagnostics_stem="chapter_unit1",
                )

            raw_path = diagnostics / "chapter_unit1.attempt_01.invalid.json.txt"
            meta_path = diagnostics / "chapter_unit1.attempt_01.meta.json"
            self.assertEqual('{"results":[{"pid":"p00059"', raw_path.read_text(encoding="utf-8"))
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertFalse(metadata["authoritative"])
            self.assertEqual("diagnostic_only", metadata["purpose"])
            self.assertEqual(5000, metadata["max_tokens"])


if __name__ == "__main__":
    unittest.main()
