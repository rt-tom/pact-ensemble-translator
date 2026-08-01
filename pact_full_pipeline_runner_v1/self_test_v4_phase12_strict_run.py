#!/usr/bin/env python3
"""Regression test: chapter args in the strict-driver CLI have no default.

PR #97: --chapter-id/--chapter-html/--memory-dir used to default to
chapter_046's own paths, which made it easy to silently re-run that
chapter again instead of whichever one was actually intended. This test
locks in that they are now required, without needing a real llama-server
or chapter file -- argparse fails at parse time, before any of that is
touched.
"""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr

import v4_phase12_strict_run as m

REQUIRED_FLAGS = ("--chapter-id", "--chapter-html", "--memory-dir")


class RequiredChapterArgsTest(unittest.TestCase):
    def test_no_args_at_all_exits_nonzero(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                m.build_argparser().parse_args([])
        self.assertNotEqual(cm.exception.code, 0)

    def test_missing_any_one_required_flag_exits_nonzero(self):
        base = {
            "--chapter-id": "046_subordination-6-3",
            "--chapter-html": "D:/pact/pact_chapters/0046_subordination-6-3.html",
            "--memory-dir": "D:/pact/pact_chapters",
            "--out-dir": "D:/pact/gate_bench_runs/test_run",
        }
        for missing in REQUIRED_FLAGS:
            argv = [v for k, v in base.items() if k != missing for v in (k, v)]
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    m.build_argparser().parse_args(argv)

    def test_all_required_flags_present_parses_successfully(self):
        args = m.build_argparser().parse_args([
            "--chapter-id", "046_subordination-6-3",
            "--chapter-html", "D:/pact/pact_chapters/0046_subordination-6-3.html",
            "--memory-dir", "D:/pact/pact_chapters",
            "--out-dir", "D:/pact/gate_bench_runs/test_run",
        ])
        self.assertEqual(args.chapter_id, "046_subordination-6-3")
        self.assertEqual(str(args.chapter_html), "D:\\pact\\pact_chapters\\0046_subordination-6-3.html")
        self.assertEqual(str(args.memory_dir), "D:\\pact\\pact_chapters")


if __name__ == "__main__":
    unittest.main()
