#!/usr/bin/env python3
"""Unit/regression tests for v4 Phase 0C baseline (read-only).

Synthetic fixtures only — no book text, no model calls, no live runs.
Mirrors the Phase 0A self_test convention (``import v4_*`` from the runner dir).
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

import v4_phase0c_baseline as m
from v4_phase0c_baseline import (
    MEASURED, NO_RUN, NOT_MEASURABLE, PENDING_DEFINITION,
    PENDING_LIVE_RUN, PENDING_RUN_COMPLETION, SCHEMA_VERSION,
)


def wj(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def golden_record(pid: str, status: str = "accepted",
                  numbers: list[str] | None = None,
                  spans: list[tuple[str, str, int]] | None = None) -> dict[str, Any]:
    inv: list[dict[str, Any]] = []
    for n in numbers or []:
        inv.append({"kind": "number", "value": n})
    return {
        "schema": "pact-v4-golden-record/v1",
        "record_id": f"ch046-{pid}",
        "chapter": "046",
        "pid": pid,
        "source": {"language": "en", "text": f"EN {pid}", "html": f"<p>EN {pid}</p>",
                    "structural_role": "paragraph", "inline_spans": [],
                    "word_count": 3},
        "risk": {"band": "med", "types": ["numbers"], "signals": {}},
        "invariants": {
            "must_preserve": inv,
            "must_not_add": [],
            "formatting_expectation": {
                "required_spans": [
                    {"span_id": sid, "tag": tag, "occurrence": occ}
                    for tag, sid, occ in (spans or [])
                ]
            },
        },
        "known_violations": [],
        "reference": {"text": "RU ref", "source": "human_translation_epub",
                       "note": "reference only; not an exact-match ground truth",
                       "alignment": {"method": "heuristic_length", "confidence": 0.35}},
        "verdict": {"status": status},
        "provenance": {"source_file": "s.html", "reference_file": "r.epub",
                        "source_hash": "a" * 64, "reference_hash": "b" * 64,
                        "tool_version": "x", "generated_at": "2026-01-01T00:00:00+00:00"},
    }


def make_golden(path: Path, accepted: int = 2, needs_review: int = 1) -> None:
    records: list[dict[str, Any]] = []
    for i in range(accepted):
        records.append(golden_record(f"p{i:05d}", "accepted",
                                     numbers=[str(100 + i)] if i % 2 == 0 else None))
    for i in range(accepted, accepted + needs_review):
        records.append(golden_record(f"p{i:05d}", "needs_review", numbers=["999"]))
    wj(path, records)


def v31_run(root: Path, *, primary: bool = True, residual: bool = False,
            covered: int = 2, total_blocks: int = 2,
            lifecycle_statuses: list[str] | None = None,
            gate_fail_pids: list[str] | None = None,
            introduced_pids: list[str] | None = None,
            selected_pids: list[str] | None = None) -> Path:
    """Build a synthetic v31 run_root; returns the root."""
    wj(root / "config.full_pipeline.v31.json", {"artifact_version": "3.1.3"})
    wj(root / "chapter_manifest.v31.json", {"chapter": "0100_x.html"})
    wj(root / "book_bible.json", {"characters": []})
    ch = root / "work" / "0100_x"
    blocks = [{"pid": f"p{i:05d}", "index": i, "tag": "p",
               "source_text": f"EN p{i}", "word_count": 2,
               "digits": [], "inline_spans": []} for i in range(total_blocks)]
    chunks = [{"chunk_id": "c0001", "pids": [b["pid"] for b in blocks]}]
    wj(ch / "manifest.json", {"version": "3.1.3", "chapter": "0100_x.html",
                              "source_sha256": "s" * 64, "blocks": blocks, "chunks": chunks})
    wj(ch / "v31_primary_translations.json",
       {f"p{i:05d}": f"RU p{i}" for i in range(covered)})
    # meta translation with generation.usage so time/tokens is measurable
    meta = ch / "meta"
    wj(meta / "c0001.translation.json", {
        "chunk_id": "c0001",
        "attempts": [{"attempt": 1, "ok": True,
                       "generation": {"usage": {"prompt_tokens": 100, "completion_tokens": 50,
                                                 "total_tokens": 150},
                                       "wall_seconds": 12.5,
                                       "finish_reason": "stop"}}],
    })
    if primary:
        wj(ch / "v31" / "primary" / "status.json",
           {"version": "3.1.3", "pass": "primary", "last_round": 1,
            "resolved": len(lifecycle_statuses or []), "total": len(lifecycle_statuses or [])})
        lc: list[dict[str, Any]] = []
        for idx, st in enumerate(lifecycle_statuses or ["resolved_repair"], start=1):
            lc.append({"issue_id": f"v31-primary-{idx:05d}", "pid": "p00000",
                       "pass": "primary", "round": 1, "status": st})
        wj(ch / "v31" / "primary" / "lifecycle.json", lc)
        wj(ch / "v31" / "primary" / "verification_report.json", {
            "version": "3.1.3", "chapter": "0100_x.html", "pass": "primary",
            "total": len(lc), "repair": len(lc), "keep": 0, "uncertain": 0,
            "decisions": [{"issue_id": f"v31-primary-{i+1:05d}", "pid": "p00000",
                            "decision": "repair", "confidence": "high"} for i in range(len(lc))],
        })
        gate_decisions = []
        for i in range(total_blocks):
            for cand in ("A", "B"):
                pid = f"p{i:05d}"
                passed = pid not in (gate_fail_pids or [])
                gate_decisions.append({"pid": pid, "candidate_id": cand,
                                        "passed": passed, "errors": [],
                                        "introduced_issues": (
                                            [{"category": "x"}] if pid in (introduced_pids or [])
                                            else []),
                                        "remaining_required_categories": []})
        wj(ch / "v31" / "primary" / "post_gate_deterministic_round_01.json", {
            "version": "3.1.3", "chapter": "0100_x.html", "pass": "primary",
            "round": 1, "expected": len(gate_decisions), "completed": len(gate_decisions),
            "decisions": gate_decisions,
        })
        wj(ch / "v31_final_changed_pid_ledger.json", {
            "schema": "v3.1-final-ledger/v1",
            "entries": [{"pid": p, "stage": "primary_repair",
                          "reason": "accepted", "before": "b", "after": "a"} for p in (selected_pids or [])],
            "changed_pids": selected_pids or [],
        })
    if residual:
        wj(ch / "v31" / "residual" / "lifecycle.json", [])
    return root


class GridTests(unittest.TestCase):
    def test_grid_has_four_cells_with_recipe_and_pending(self) -> None:
        grid = m.build_grid("046")
        self.assertEqual(4, len(grid["cells"]))
        ids = {c["cell_id"] for c in grid["cells"]}
        self.assertEqual({"8_12__rc_on", "8_12__rc_off", "12_20__rc_on", "12_20__rc_off"}, ids)
        for c in grid["cells"]:
            self.assertEqual(PENDING_LIVE_RUN, c["status"])
            self.assertIn("chunking", c["config_overrides"])
            self.assertTrue(c["run_command"].startswith("py ./pact_translate_v3.py"))
            self.assertIn("--phase translate", c["run_command"])
        # following_blocks on/off mapping
        on = next(c for c in grid["cells"] if c["right_context"] == "on")
        off = next(c for c in grid["cells"] if c["right_context"] == "off")
        self.assertEqual(2, on["config_overrides"]["chunking"]["following_blocks"])
        self.assertEqual(0, off["config_overrides"]["chunking"]["following_blocks"])
        # chunk-size low vs high differ in target_words
        low = next(c for c in grid["cells"] if c["chunk_size"] == "8_12")
        high = next(c for c in grid["cells"] if c["chunk_size"] == "12_20")
        self.assertLess(low["config_overrides"]["chunking"]["target_words"],
                         high["config_overrides"]["chunking"]["target_words"])

    def test_aggregated_status_pending_until_all_measured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            golden = Path(tmp) / "records.json"
            make_golden(golden, accepted=2, needs_review=1)
            rec = m.build_result_record(golden, None)
            self.assertEqual(PENDING_LIVE_RUN, rec["track_a"]["aggregated"]["status"])
            grid = rec["track_a"]["grid"]
            # 1 measured + 3 pending -> still pending (not all 4 done)
            outs = {grid["cells"][0]["cell_id"]: {"p00000": "RU 0", "p00001": "RU 1"}}
            m.attach_grid_metrics(grid, m.load_golden_records(golden), outs)
            self.assertEqual(PENDING_LIVE_RUN, m._grid_aggregated_status(grid))
            # all 4 measured -> aggregated measured
            for i, cell in enumerate(grid["cells"][1:], start=1):
                outs[cell["cell_id"]] = {f"p{i:05d}": f"RU {i}"}
            m.attach_grid_metrics(grid, m.load_golden_records(golden), outs)
            self.assertEqual(MEASURED, m._grid_aggregated_status(grid))


class NeedsReviewExclusionTests(unittest.TestCase):
    def test_only_accepted_feed_numeric_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            golden = Path(tmp) / "records.json"
            make_golden(golden, accepted=2, needs_review=3)
            records = m.load_golden_records(golden)
            accepted = m.accepted_golden_pids(records)
            self.assertEqual(2, len(accepted))
            self.assertEqual({r["pid"] for r in accepted}, {"p00000", "p00001"})
            src = m.summarize_golden_source(records, m.sha256_file(golden))
            self.assertEqual(2, src["accepted_count"])
            self.assertEqual(3, src["needs_review_excluded_count"])
            self.assertEqual(0, src["known_violations_populated_count"])
            self.assertEqual(NOT_MEASURABLE, src["semantic_recall"]["status"])
            self.assertIn("limitation", src["needs_review_policy"])
            # the number must be the real needs_review count, not a literal placeholder
            self.assertIn("3", src["needs_review_policy"])
            self.assertNotIn("{n}", src["needs_review_policy"])


class GapDetectionTests(unittest.TestCase):
    def test_missing_pid_in_output_is_explicit_gap_not_skip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            golden = Path(tmp) / "records.json"
            make_golden(golden, accepted=3, needs_review=0)
            records = m.load_golden_records(golden)
            # output only has p00000 + p00001; p00002 missing
            out = {"p00000": "RU 0 100", "p00001": "RU 1"}
            res = m.aggregate_track_a_cell(records, out)
            self.assertEqual(MEASURED, res["status"])
            self.assertIn("p00002", res["missing_pids_gaps"])
            gaps = [r for r in res["pid_results"] if r["gap"]]
            self.assertEqual(["p00002"], [r["pid"] for r in gaps])
            self.assertTrue(gaps[0]["violated"])

    def test_invariant_violation_counted_as_fp_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            golden = Path(tmp) / "records.json"
            # p00000 accepted with number invariant "100"
            make_golden(golden, accepted=2, needs_review=0)
            records = m.load_golden_records(golden)
            # p00000 output missing the "100" -> violated; p00001 has no invariant -> clean
            out = {"p00000": "RU output with no number", "p00001": "RU 1"}
            res = m.aggregate_track_a_cell(records, out)
            self.assertEqual(1, res["violated_pids"])
            self.assertGreater(res["fp_candidate_rate"], 0.0)
            self.assertEqual([], res["missing_pids_gaps"])


class PartialTrackBTests(unittest.TestCase):
    def test_partial_run_pending_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = v31_run(Path(tmp), primary=True, residual=False,
                           covered=2, total_blocks=2,
                           lifecycle_statuses=["resolved_repair", "resolved_retry_exhausted"],
                           selected_pids=["p00000"])
            tb = m.import_track_b(root)
            self.assertEqual(PENDING_RUN_COMPLETION, tb["completion"]["status"])
            self.assertTrue(tb["completion"]["primary_pass_complete"])
            self.assertFalse(tb["completion"]["residual_pass_complete"])
            self.assertEqual(MEASURED, tb["metrics"]["pid_coverage"]["status"])
            self.assertEqual("2/2", tb["metrics"]["deterministic_integrity"]["pid_coverage"])
            # residual_errors primary numbers measured but final residual pending
            self.assertEqual(1, tb["metrics"]["residual_errors"]["primary_retry_exhausted"])
            self.assertEqual(PENDING_RUN_COMPLETION, tb["metrics"]["residual_errors"]["final_residual_total"])
            self.assertEqual(NOT_MEASURABLE, tb["metrics"]["russian_rubric"]["status"])
            self.assertEqual(PENDING_DEFINITION, tb["metrics"]["ltcr"]["status"])
            self.assertEqual(MEASURED, tb["metrics"]["time_tokens"]["status"])

    def test_complete_run_measured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = v31_run(Path(tmp), primary=True, residual=True,
                           selected_pids=["p00000"],
                           gate_fail_pids=["p00000"],  # selected pid fails its gate
                           lifecycle_statuses=["resolved_repair"])
            tb = m.import_track_b(root)
            self.assertEqual(MEASURED, tb["completion"]["status"])
            self.assertIn("p00000", tb["metrics"]["bad_repair"]["bad_repair_pids"])
            self.assertGreater(tb["metrics"]["bad_repair"]["bad_repair_rate"], 0.0)

    def test_no_run_returns_no_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tb = m.import_track_b(Path(tmp) / "missing")
        self.assertEqual(NO_RUN, tb["completion"]["status"])


class ResultRecordTests(unittest.TestCase):
    def test_versioning_and_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            golden = Path(tmp) / "records.json"
            make_golden(golden, accepted=2, needs_review=1)
            root = v31_run(Path(tmp) / "run", primary=True, residual=False,
                           selected_pids=["p00000"],
                           lifecycle_statuses=["resolved_repair"])
            rec = m.build_result_record(golden, root)
            self.assertEqual(SCHEMA_VERSION, rec["schema"])
            self.assertEqual("pact-0c/0.2", rec["tool_version"])
            self.assertEqual(PENDING_LIVE_RUN, rec["track_a"]["aggregated"]["status"])
            self.assertEqual(PENDING_RUN_COMPLETION, rec["track_b"]["completion"]["status"])
            out = Path(tmp) / "out" / "result.json"
            m.write_result_record(rec, out)
            loaded = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(SCHEMA_VERSION, loaded["schema"])
            self.assertEqual([], m.validate_result_record(loaded))

    def test_invalid_record_rejected_on_write(self) -> None:
        rec = m.build_result_record(None, None)
        rec["schema"] = "wrong"
        rec["track_b"]["completion"]["status"] = "bogus"
        with self.assertRaises(ValueError):
            m.write_result_record(rec, Path("."))  # won't reach fs; validation first

    def test_no_sources_produce_no_run_pending_statuses(self) -> None:
        rec = m.build_result_record(None, None)
        self.assertEqual(NO_RUN, rec["track_a"]["aggregated"]["status"])
        self.assertEqual(NO_RUN, rec["track_b"]["completion"]["status"])
        self.assertEqual(NO_RUN, rec["track_b"]["metrics"]["pid_coverage"]["status"])

    def test_records_hash_is_real_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            golden = Path(tmp) / "records.json"
            make_golden(golden)
            rec = m.build_result_record(golden, None)
            rh = rec["track_a"]["source"]["records_hash_sha256"]
            self.assertRegex(rh, r"^[a-f0-9]{64}$")
            import hashlib
            self.assertEqual(hashlib.sha256(golden.read_bytes()).hexdigest(), rh)


if __name__ == "__main__":
    unittest.main()