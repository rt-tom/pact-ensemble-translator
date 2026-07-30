"""Regression coverage for finalize guard allowing complete_with_warnings."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pact_translate_v3 as runtime


def _make_fixture_cfg(work: Path):
    glossary_dir = work / "glossary"
    glossary_dir.mkdir(parents=True, exist_ok=True)
    for name in ("locked.json", "established.json", "provisional.json", "conflicts.json"):
        (glossary_dir / name).write_text("{}", encoding="utf-8")
    cfg = copy.deepcopy(runtime.DEFAULTS)
    cfg["post_repair_verifier"] = {"enabled": True, "required": True}
    cfg["reviewer_api"]["enabled"] = False
    for key, val in {
        "input_dir": str(work / "input"),
        "output_dir": str(work / "output"),
        "work_dir": str(work / "work"),
        "logs_dir": str(work / "logs"),
        "glossary_dir": str(glossary_dir),
        "run_glossary_candidate_ledger": str(work / "glossary_candidates.run.json"),
        "book_glossary_candidate_ledger": str(work / "glossary_candidates.json"),
        "book_bible_file": str(work / "book_bible.json"),
    }.items():
        cfg["paths"][key] = val
    (work / "book_bible.json").write_text("{}", encoding="utf-8")
    return cfg


def make_blocks(source_html: str):
    _, blocks = runtime.prepare_html(source_html, runtime.DEFAULTS)
    return blocks


class FinalizeGuardTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.work = self.root / "work" / "test"
        self.work.mkdir(parents=True, exist_ok=True)
        self.cfg = _make_fixture_cfg(self.root)
        self.runner = runtime.Runner(self.cfg)

    def tearDown(self):
        self.tempdir.cleanup()

    def _write_state(self, status):
        (self.work / "state.json").write_text(
            json.dumps({"status": status}), encoding="utf-8"
        )

    def _trans(self, blocks):
        return {block.pid: f"Перевод {block.index}" for block in blocks}

    def test_complete_with_warnings_allows_finalize_without_post_repair_report(self):
        blocks = make_blocks("<p>Hello <em>world</em></p>")
        block_map = {block.pid: block for block in blocks}
        trans = self._trans(blocks)
        self._write_state("complete_with_warnings")
        with patch.object(runtime, "run_formatting", return_value=(trans, [])):
            with patch.object(runtime, "build_final_html", return_value="<html></html>"):
                with patch.object(runtime, "final_integrity", return_value={"ok": True, "errors": []}):
                    result = self.runner.finalize(
                        Path("test.html"), self.work, "<html></html>",
                        blocks, block_map, trans, trans,
                        [], [], False,
                    )
        self.assertIsInstance(result, Path)

    def test_missing_state_raises_when_post_repair_report_missing(self):
        blocks = make_blocks("<p>Hello world</p>")
        block_map = {block.pid: block for block in blocks}
        trans = self._trans(blocks)
        with self.assertRaises(runtime.PipelineError) as ctx:
            self.runner.finalize(
                Path("test.html"), self.work, "<html></html>",
                blocks, block_map, trans, trans,
                [], [], False,
            )
        self.assertIn("post-repair verification report is missing", str(ctx.exception))

    def test_complete_state_allows_finalize_without_post_repair_report(self):
        blocks = make_blocks("<p>Test</p>")
        block_map = {block.pid: block for block in blocks}
        trans = self._trans(blocks)
        self._write_state("complete")
        with patch.object(runtime, "run_formatting", return_value=(trans, [])):
            with patch.object(runtime, "build_final_html", return_value="<html></html>"):
                with patch.object(runtime, "final_integrity", return_value={"ok": True, "errors": []}):
                    result = self.runner.finalize(
                        Path("test.html"), self.work, "<html></html>",
                        blocks, block_map, trans, trans,
                        [], [], False,
                    )
        self.assertIsInstance(result, Path)

    def test_failed_state_raises(self):
        blocks = make_blocks("<p>Test</p>")
        block_map = {block.pid: block for block in blocks}
        trans = self._trans(blocks)
        self._write_state("failed")
        with self.assertRaises(runtime.PipelineError) as ctx:
            self.runner.finalize(
                Path("test.html"), self.work, "<html></html>",
                blocks, block_map, trans, trans,
                [], [], False,
            )
        self.assertIn("post-repair verification report is missing", str(ctx.exception))

    def test_quarantined_state_raises(self):
        blocks = make_blocks("<p>Test</p>")
        block_map = {block.pid: block for block in blocks}
        trans = self._trans(blocks)
        self._write_state("quarantined")
        with self.assertRaises(runtime.PipelineError) as ctx:
            self.runner.finalize(
                Path("test.html"), self.work, "<html></html>",
                blocks, block_map, trans, trans,
                [], [], False,
            )
        self.assertIn("post-repair verification report is missing", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
