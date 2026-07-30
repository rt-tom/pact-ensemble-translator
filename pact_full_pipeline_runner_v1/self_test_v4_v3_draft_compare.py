#!/usr/bin/env python3
"""Unit/regression tests for the v3-vs-v4 draft comparison tool (read-only).

Synthetic fixtures only -- no book text, no model calls, no live runs.
Mirrors the Phase 0C self_test convention (``import v4_*`` from the runner dir).
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

import v4_v3_draft_compare as m


def wj(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def golden_record(pid: str, status: str = "accepted",
                   numbers: list[str] | None = None,
                   source_hash: str = "a" * 64) -> dict[str, Any]:
    inv = [{"kind": "number", "value": n} for n in (numbers or [])]
    return {
        "schema": "pact-v4-golden-record/v1",
        "record_id": f"ch046-{pid}",
        "chapter": "046",
        "pid": pid,
        "source": {"language": "en", "text": f"EN {pid}", "html": f"<p>EN {pid}</p>",
                    "structural_role": "paragraph", "inline_spans": [], "word_count": 3},
        "risk": {"band": "low", "types": [], "signals": {}},
        "invariants": {
            "must_preserve": inv, "must_not_add": [],
            "formatting_expectation": {"required_spans": []},
        },
        "known_violations": [],
        "reference": {"text": "RU ref", "source": "human_translation_epub",
                       "note": "reference only; not an exact-match ground truth",
                       "alignment": {"method": "heuristic_length", "confidence": 0.9}},
        "verdict": {"status": status},
        "provenance": {"source_file": "0046_subordination-6-3.html", "reference_file": "r.epub",
                        "source_hash": source_hash, "reference_hash": "b" * 64,
                        "tool_version": "x", "generated_at": "2026-01-01T00:00:00+00:00"},
    }


def make_golden(path: Path, accepted_specs: list[tuple[str, list[str]]],
                 source_hash: str = "a" * 64) -> None:
    records = [golden_record(pid, "accepted", numbers=nums, source_hash=source_hash)
               for pid, nums in accepted_specs]
    wj(path, records)


def make_v3_result(path: Path, *, cells: dict[str, dict[str, Any]]) -> None:
    """``cells`` maps cell_id -> {status, fp_candidate_rate, accepted_pids, missing_pids_gaps}."""
    grid_cells = []
    for cell_id, m_ in cells.items():
        chunk_size, right_context = cell_id.split("__rc_")
        grid_cells.append({
            "cell_id": cell_id,
            "chunk_size": chunk_size,
            "right_context": right_context,
            "config_overrides": {"chunking": {"following_blocks": 2 if right_context == "on" else 0}},
            "status": m_["status"],
            "achieved_pid_per_chunk": {"status": "measured", "translated_pids": m_.get("accepted_pids", 0)},
            "metrics": {
                "status": m_["status"],
                "accepted_pids": m_.get("accepted_pids", 0),
                "fp_candidate_rate": m_.get("fp_candidate_rate"),
                "violated_pids": m_.get("violated_pids", 0),
                "missing_pids_gaps": m_.get("missing_pids_gaps", []),
                "pid_results": [],
            },
        })
    wj(path, {
        "schema": "pact-v4-phase0c-result-record/v1",
        "generated_at": "2026-07-30T00:00:00+00:00",
        "tool_version": "pact-0c/0.2",
        "track_a": {
            "source": {"chapter_id": "046", "records_hash_sha256": "c" * 64,
                        "records_count": 60, "accepted_count": 57,
                        "needs_review_excluded_count": 3, "rejected_count": 0,
                        "known_violations_populated_count": 0,
                        "semantic_recall": {"status": "not_measurable", "reason": "x"},
                        "needs_review_policy": "needs_review records (3) are excluded."},
            "grid": {"axes": {"chunk_size": ["8_12", "12_20"], "right_context": ["on", "off"]},
                      "cells": grid_cells},
            "fp_candidate_metric_definition": "def",
            "aggregated": {"status": "measured"},
        },
        "track_b": {"source": {}, "completion": {"status": "no_run"}, "metrics": {}},
    })


def make_v4_out_dir(root: Path, *, translations: dict[str, str],
                     chapter_id: str = "046_subordination-6-3",
                     source_hash: str = "d" * 64,
                     schema: str | None = None) -> Path:
    wj(root / "translations.json", translations)
    wj(root / "provenance.json", {
        "schema": schema or m.V4_PROVENANCE_SCHEMA,
        "run_label": "test-run",
        "chapter_id": chapter_id,
        "identities": {"source_hash": source_hash, "snapshot_hash": "e" * 64,
                        "chunk_plan_hash": "f" * 64, "config_identity": "0" * 64},
        "policy_versions": {"risk_policy": "pact-v4-risk-source-en/v1"},
        "provisional_params": {"temperature": 0.2, "seed": 7},
        "counts": {"chunks": 2, "selected": 2, "quarantined": 0},
        "artefacts": {"translations": str(root / "translations.json")},
    })
    return root


class LoadV4RunOutputsTests(unittest.TestCase):
    def test_reads_translations_and_identity(self) -> None:
        with _tmp() as tmp:
            out_dir = make_v4_out_dir(tmp / "run", translations={"p00001": "RU one 100"})
            result = m.load_v4_run_outputs(out_dir)
            self.assertEqual(result["translations"], {"p00001": "RU one 100"})
            self.assertEqual(result["translations_pid_count"], 1)
            self.assertEqual(result["chapter_id"], "046_subordination-6-3")
            self.assertEqual(result["identities"]["source_hash"], "d" * 64)

    def test_missing_translations_file_raises(self) -> None:
        with _tmp() as tmp:
            out_dir = tmp / "run"
            out_dir.mkdir(parents=True)
            wj(out_dir / "provenance.json", {"schema": m.V4_PROVENANCE_SCHEMA, "chapter_id": "046"})
            with self.assertRaises(ValueError):
                m.load_v4_run_outputs(out_dir)

    def test_wrong_provenance_schema_raises(self) -> None:
        with _tmp() as tmp:
            out_dir = make_v4_out_dir(tmp / "run", translations={"p00001": "x"},
                                       schema="some-other-schema/v1")
            with self.assertRaises(ValueError):
                m.load_v4_run_outputs(out_dir)


class LoadV3CellTests(unittest.TestCase):
    def test_auto_selects_when_unambiguous(self) -> None:
        with _tmp() as tmp:
            path = tmp / "phase0c_result.json"
            make_v3_result(path, cells={
                "8_12__rc_on": {"status": "measured", "fp_candidate_rate": 0.1579, "accepted_pids": 57},
                "8_12__rc_off": {"status": "measured", "fp_candidate_rate": 0.1579, "accepted_pids": 57},
            })
            cell = m.load_v3_cell(path, None)
            self.assertEqual(cell["cell_id"], "8_12__rc_on")
            self.assertIn("unambiguous", cell["cell_selection"])

    def test_disagreement_without_explicit_cell_is_flagged_not_hidden(self) -> None:
        with _tmp() as tmp:
            path = tmp / "phase0c_result.json"
            make_v3_result(path, cells={
                "8_12__rc_on": {"status": "measured", "fp_candidate_rate": 0.10, "accepted_pids": 57},
                "8_12__rc_off": {"status": "measured", "fp_candidate_rate": 0.20, "accepted_pids": 57},
            })
            cell = m.load_v3_cell(path, None)
            self.assertIn("DISAGREE", cell["cell_selection"])

    def test_explicit_cell_id_selects_that_cell(self) -> None:
        with _tmp() as tmp:
            path = tmp / "phase0c_result.json"
            make_v3_result(path, cells={
                "8_12__rc_on": {"status": "measured", "fp_candidate_rate": 0.10, "accepted_pids": 57},
                "8_12__rc_off": {"status": "measured", "fp_candidate_rate": 0.20, "accepted_pids": 57},
            })
            cell = m.load_v3_cell(path, "8_12__rc_off")
            self.assertEqual(cell["cell_id"], "8_12__rc_off")
            self.assertEqual(cell["metrics"]["fp_candidate_rate"], 0.20)

    def test_unknown_explicit_cell_id_raises(self) -> None:
        with _tmp() as tmp:
            path = tmp / "phase0c_result.json"
            make_v3_result(path, cells={
                "8_12__rc_on": {"status": "measured", "fp_candidate_rate": 0.10, "accepted_pids": 57},
            })
            with self.assertRaises(ValueError):
                m.load_v3_cell(path, "12_20__rc_off")

    def test_no_measured_cells_raises(self) -> None:
        with _tmp() as tmp:
            path = tmp / "phase0c_result.json"
            make_v3_result(path, cells={
                "8_12__rc_on": {"status": "pending_live_run", "fp_candidate_rate": None},
            })
            with self.assertRaises(ValueError):
                m.load_v3_cell(path, None)


class BuildComparisonRecordTests(unittest.TestCase):
    def test_end_to_end_with_gap_and_violation(self) -> None:
        with _tmp() as tmp:
            golden_path = tmp / "golden" / "records.json"
            make_golden(golden_path, [
                ("p00001", ["100"]),   # present + number preserved -> ok
                ("p00002", ["200"]),   # present but number dropped -> violated
                ("p00003", ["300"]),   # absent from v4 output -> gap
            ])
            v3_path = tmp / "v3" / "phase0c_result.json"
            make_v3_result(v3_path, cells={
                "8_12__rc_on": {"status": "measured", "fp_candidate_rate": 0.3333,
                                 "accepted_pids": 3, "violated_pids": 1},
            })
            v4_dir = make_v4_out_dir(tmp / "v4", translations={
                "p00001": "RU one with 100",
                "p00002": "RU two with no digits",
            })

            record = m.build_comparison_record(
                golden_path=golden_path,
                v3_result_path=v3_path,
                v4_out_dir=v4_dir,
                v3_cell_id="8_12__rc_on",
            )

            self.assertEqual(record["schema"], m.SCHEMA_VERSION)
            self.assertFalse(m.validate_comparison_record(record))

            v4_metrics = record["v4"]["metrics"]
            self.assertEqual(v4_metrics["accepted_pids"], 3)
            self.assertEqual(v4_metrics["violated_pids"], 1)
            self.assertEqual(v4_metrics["missing_pids_gaps"], ["p00003"])
            self.assertAlmostEqual(v4_metrics["fp_candidate_rate"], 1 / 3, places=4)

            comparison = record["comparison"]
            self.assertEqual(comparison["accepted_pids_golden"], 3)
            self.assertEqual(comparison["fp_candidate_rate_v3"], 0.3333)
            self.assertAlmostEqual(comparison["fp_candidate_rate_v4"], 1 / 3, places=4)
            self.assertEqual(comparison["v4_missing_pids_gaps"], ["p00003"])

            self.assertTrue(any("dry-run" in c.lower() or "stub" in c.lower()
                                  for c in record["caveats"]))

    def test_identity_check_chapter_number_match(self) -> None:
        with _tmp() as tmp:
            golden_path = tmp / "golden" / "records.json"
            make_golden(golden_path, [("p00001", [])])
            v3_path = tmp / "v3" / "phase0c_result.json"
            make_v3_result(v3_path, cells={
                "8_12__rc_on": {"status": "measured", "fp_candidate_rate": 0.0, "accepted_pids": 1},
            })
            v4_dir = make_v4_out_dir(tmp / "v4", translations={"p00001": "RU one"},
                                       chapter_id="046_subordination-6-3")

            record = m.build_comparison_record(
                golden_path=golden_path, v3_result_path=v3_path, v4_out_dir=v4_dir,
            )
            ic = record["identity_check"]
            self.assertTrue(ic["chapter_number_match"])
            self.assertEqual(ic["golden_vs_v3_manifest_source_hash_match"], m.NOT_CHECKED)

    def test_identity_check_chapter_number_mismatch_not_hidden(self) -> None:
        with _tmp() as tmp:
            golden_path = tmp / "golden" / "records.json"
            make_golden(golden_path, [("p00001", [])])
            v3_path = tmp / "v3" / "phase0c_result.json"
            make_v3_result(v3_path, cells={
                "8_12__rc_on": {"status": "measured", "fp_candidate_rate": 0.0, "accepted_pids": 1},
            })
            v4_dir = make_v4_out_dir(tmp / "v4", translations={"p00001": "RU one"},
                                       chapter_id="100_other-chapter")

            record = m.build_comparison_record(
                golden_path=golden_path, v3_result_path=v3_path, v4_out_dir=v4_dir,
            )
            self.assertFalse(record["identity_check"]["chapter_number_match"])

    def test_raw_chapter_html_cross_check(self) -> None:
        with _tmp() as tmp:
            source_hash = "z" * 64
            golden_path = tmp / "golden" / "records.json"
            make_golden(golden_path, [("p00001", [])], source_hash=source_hash)
            v3_path = tmp / "v3" / "phase0c_result.json"
            make_v3_result(v3_path, cells={
                "8_12__rc_on": {"status": "measured", "fp_candidate_rate": 0.0, "accepted_pids": 1},
            })
            v4_dir = make_v4_out_dir(tmp / "v4", translations={"p00001": "RU one"})

            chapter_html = tmp / "chapter.html"
            chapter_html.write_bytes(b"<html>fixture</html>")
            import hashlib
            real_hash = hashlib.sha256(chapter_html.read_bytes()).hexdigest()

            # Point golden's provenance at a hash that won't match the fixture file
            # on purpose, to prove mismatches are reported rather than hidden.
            record = m.build_comparison_record(
                golden_path=golden_path, v3_result_path=v3_path, v4_out_dir=v4_dir,
                chapter_html_path=chapter_html,
            )
            ic = record["identity_check"]
            self.assertEqual(ic["raw_chapter_html_sha256"], real_hash)
            self.assertFalse(ic["golden_vs_raw_chapter_html_hash_match"])

    def test_v3_manifest_cross_check_match(self) -> None:
        with _tmp() as tmp:
            source_hash = "y" * 64
            golden_path = tmp / "golden" / "records.json"
            make_golden(golden_path, [("p00001", [])], source_hash=source_hash)
            v3_path = tmp / "v3" / "phase0c_result.json"
            make_v3_result(v3_path, cells={
                "8_12__rc_on": {"status": "measured", "fp_candidate_rate": 0.0, "accepted_pids": 1},
            })
            v4_dir = make_v4_out_dir(tmp / "v4", translations={"p00001": "RU one"})
            manifest_path = tmp / "v3_manifest.json"
            wj(manifest_path, {"source_sha256": source_hash})

            record = m.build_comparison_record(
                golden_path=golden_path, v3_result_path=v3_path, v4_out_dir=v4_dir,
                v3_manifest_path=manifest_path,
            )
            self.assertTrue(record["identity_check"]["golden_vs_v3_manifest_source_hash_match"])

    def test_write_and_reload_round_trip(self) -> None:
        with _tmp() as tmp:
            golden_path = tmp / "golden" / "records.json"
            make_golden(golden_path, [("p00001", [])])
            v3_path = tmp / "v3" / "phase0c_result.json"
            make_v3_result(v3_path, cells={
                "8_12__rc_on": {"status": "measured", "fp_candidate_rate": 0.0, "accepted_pids": 1},
            })
            v4_dir = make_v4_out_dir(tmp / "v4", translations={"p00001": "RU one"})

            record = m.build_comparison_record(
                golden_path=golden_path, v3_result_path=v3_path, v4_out_dir=v4_dir,
            )
            out_path = tmp / "out" / "comparison.json"
            m.write_comparison_record(record, out_path)
            reloaded = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(reloaded["schema"], m.SCHEMA_VERSION)


def _tmp():
    import tempfile
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        with tempfile.TemporaryDirectory() as d:
            yield Path(d)

    return _ctx()


if __name__ == "__main__":
    unittest.main()
