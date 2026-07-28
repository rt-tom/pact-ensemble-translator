#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

import v4_measurement_harness as harness


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def make_run(root: Path, label: str, translated: str = "Русский текст.") -> Path:
    run = root / label
    chapter = run / "work" / "0001_sample"
    write_json(run / "config.full_pipeline.v31.json", {"artifact_version": "3.1.3"})
    write_json(run / "book_bible.json", {"characters": []})
    write_json(chapter / "manifest.json", {
        "version": "3.1.3",
        "chapter": "0001_sample.html",
        "source_sha256": "source-id",
        "blocks": [
            {"pid": "p00001", "index": 0, "tag": "p", "source_text": "English text."},
            {"pid": "p00002", "index": 1, "tag": "p", "source_text": "Second line."},
        ],
        "chunks": [{"chunk_id": "c0001", "pids": ["p00001", "p00002"]}],
    })
    write_json(chapter / "repaired_translations.json", {
        "p00001": translated,
        "p00002": "Вторая строка.",
    })
    write_json(chapter / "verified_issues.json", [
        {"pid": "p00001", "severity": "major", "category": "meaning"},
    ])
    write_json(chapter / "repair_records.json", [
        {"pid": "p00001", "action": "replace", "accepted": True},
    ])
    write_json(chapter / "quality_report.json", {
        "integrity": {
            "ok": True,
            "formatting_incident_counts": {"unresolved_required": 0},
        },
    })
    return run


class V4MeasurementHarnessTests(unittest.TestCase):
    def test_import_rerun_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = make_run(Path(tmp), "run-a")
            first = harness.records_to_payload(harness.import_run(run, "baseline", "v3"))
            second = harness.records_to_payload(harness.import_run(run, "baseline", "v3"))
            self.assertEqual(first, second)
            self.assertEqual(2, first["record_count"])

    def test_missing_data_remains_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run-minimal"
            chapter = run / "work" / "0001_minimal"
            write_json(chapter / "manifest.json", {
                "blocks": [{"pid": "p00001", "index": 0, "tag": "p"}],
                "chunks": [],
            })
            records = harness.import_run(run, "minimal", "v4")
            self.assertEqual(1, len(records))
            record = records[0]
            self.assertEqual(harness.UNKNOWN, record.output_text_sha256)
            self.assertEqual(harness.UNKNOWN, record.raw_issue_count)
            self.assertEqual(harness.UNKNOWN, record.repair_accepted)
            self.assertEqual(harness.UNKNOWN, record.source_text_sha256)

    def test_json_and_csv_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = make_run(root, "run-a")
            json_out = root / "exports" / "records.json"
            csv_out = root / "exports" / "records.csv"
            rc = harness.main([
                "import",
                "--run-root", str(run),
                "--label", "baseline",
                "--pipeline", "v3",
                "--json-out", str(json_out),
                "--csv-out", str(csv_out),
            ])
            self.assertEqual(0, rc)
            payload = json.loads(json_out.read_text(encoding="utf-8"))
            self.assertEqual(harness.SCHEMA_VERSION, payload["schema_version"])
            self.assertEqual(2, payload["record_count"])
            with csv_out.open("r", encoding="utf-8", newline="") as file:
                rows = list(csv.DictReader(file))
            self.assertEqual(2, len(rows))
            self.assertEqual("baseline", rows[0]["run_label"])
            self.assertIn("output_text_sha256", rows[0])

    def test_compare_exports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            left = make_run(root, "left", "Один.")
            right = make_run(root, "right", "Другой.")
            left_json = root / "left.json"
            right_json = root / "right.json"
            compare_json = root / "compare.json"
            harness.write_json(left_json, harness.records_to_payload(harness.import_run(left, "v3", "v3")))
            harness.write_json(right_json, harness.records_to_payload(harness.import_run(right, "v4", "v4")))
            rc = harness.main([
                "compare",
                "--left", str(left_json),
                "--right", str(right_json),
                "--json-out", str(compare_json),
            ])
            self.assertEqual(0, rc)
            payload = json.loads(compare_json.read_text(encoding="utf-8"))
            first = payload["records"][0]
            self.assertFalse(first["output_text_same"])
            self.assertEqual(0, first["confirmed_issue_delta"])

    def test_import_refuses_to_write_inside_run_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = make_run(Path(tmp), "run-a")
            with self.assertRaises(ValueError):
                harness.main([
                    "import",
                    "--run-root", str(run),
                    "--json-out", str(run / "measurement.json"),
                ])


if __name__ == "__main__":
    unittest.main()
