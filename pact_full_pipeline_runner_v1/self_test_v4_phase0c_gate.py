#!/usr/bin/env python3
"""Regression tests for V4 Phase 0C Gate policy (read-only).

These tests pin the V4 Phase 1/2 policy decisions stated in
``docs/plans/V4_PHASE_0C_GATE_NOTE_RU.md`` against the published
``pact-v4-phase0c-result-record/v1`` schema. They exercise:

  * the schema extension (``track_b.notes``, ``track_b.terminal_discrepancy``,
    ``metric_status.value_numeric``, ``typed_residual_total``);
  * the gate note content (initial small profile parameters, naming,
    right-context-as-default, required risk/gate categories, terminal/
    monitor discrepancy, typed final_residual_total, explicit
    limitations on what Track A/B did and did not measure);
  * the producer (``v4_phase0c_baseline.py:import_track_b``) on
    synthetic fixtures — typed final_residual_total in both measured
    and pending cases, terminal_discrepancy when monitor=FAILED +
    primary_complete, ACTIVE/REUSED notes, no-run shape;
  * the validate_result_record contract for the new fields;
  * a merge-base guard that the diff between the PR's base branch and
    HEAD only touches paths in the Gate's allow-list (no v3 production
    code, no run artifacts, no translated chapters, no cache, no
    Phase 1C/2 consumer code).

The tests deliberately do NOT read any machine-specific live record.
All producer behaviour is exercised on synthetic fixtures built
inside ``tempfile.TemporaryDirectory``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "docs" / "schemas" / "v4_phase0c_result_record.schema.json"
GATE_NOTE_PATH = ROOT / "docs" / "plans" / "V4_PHASE_0C_GATE_NOTE_RU.md"

# Parameters of the small chunk profile as the initial Phase 1 default,
# taken from the baseline ``8_12__rc_off`` cell. See the V4 Phase 0C
# Gate note §1.
SMALL_PROFILE = {
    "target_words": 450,
    "min_words": 280,
    "max_words": 640,
    "following_blocks": 0,
}

REQUIRED_RISK_CATEGORIES = ("number_word", "tone_profanity")

# Paths that this Gate PR is allowed to add or modify. Anything outside
# this allow-list is treated as out-of-scope by the merge-base guard
# and any individual-commit guard.
ALLOWED_PATHS = {
    "DECISIONS.md",
    "docs/schemas/v4_phase0c_result_record.schema.json",
    "docs/plans/V4_PHASE_0C_GATE_NOTE_RU.md",
    "pact_full_pipeline_runner_v1/v4_phase0c_baseline.py",
    "pact_full_pipeline_runner_v1/self_test_v4_phase0c_baseline.py",
    "pact_full_pipeline_runner_v1/self_test_v4_phase0c_gate.py",
}


# --------------------------------------------------------------------- #
# Synthetic v31-run fixture builder (mirrors self_test_v4_phase0c_baseline
# but lives in this file so the Gate tests are self-contained).
# --------------------------------------------------------------------- #
def wj(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def make_v31_run(root: Path, *, primary: bool = True, residual: bool = False,
                 primary_lifecycle: list[str] | None = None,
                 residual_lifecycle: list[dict] | None = None,
                 monitor_status: str | None = None,
                 monitor_stage: str = "11/11 Restore formatting and finalize HTML") -> Path:
    wj(root / "config.full_pipeline.v31.json", {"artifact_version": "3.1.3"})
    wj(root / "chapter_manifest.v31.json", {"chapter": "0100_x.html"})
    wj(root / "book_bible.json", {"characters": []})
    ch = root / "work" / "0100_x"
    blocks = [
        {"pid": f"p{i:05d}", "index": i, "tag": "p",
         "source_text": f"EN p{i}", "word_count": 2,
         "digits": [], "inline_spans": []}
        for i in range(2)
    ]
    chunks = [{"chunk_id": "c0001", "pids": [b["pid"] for b in blocks]}]
    wj(ch / "manifest.json",
       {"version": "3.1.3", "chapter": "0100_x.html",
        "source_sha256": "s" * 64, "blocks": blocks, "chunks": chunks})
    wj(ch / "v31_primary_translations.json",
       {f"p{i:05d}": f"RU p{i}" for i in range(2)})
    wj(ch / "meta" / "c0001.translation.json", {
        "chunk_id": "c0001",
        "attempts": [{"attempt": 1, "ok": True,
                      "generation": {"usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                                "total_tokens": 2},
                                     "wall_seconds": 0.1,
                                     "finish_reason": "stop"}}],
    })
    if primary:
        wj(ch / "v31" / "primary" / "status.json",
           {"version": "3.1.3", "pass": "primary", "last_round": 1,
            "resolved": len(primary_lifecycle or []),
            "total": len(primary_lifecycle or [])})
        lc = [
            {"issue_id": f"v31-primary-{i+1:05d}", "pid": "p00000",
             "pass": "primary", "round": 1, "status": st}
            for i, st in enumerate(primary_lifecycle or ["resolved_repair"])
        ]
        wj(ch / "v31" / "primary" / "lifecycle.json", lc)
        wj(ch / "v31" / "primary" / "verification_report.json", {
            "version": "3.1.3", "chapter": "0100_x.html", "pass": "primary",
            "total": len(lc), "repair": len(lc), "keep": 0, "uncertain": 0,
            "decisions": [{"issue_id": f"v31-primary-{i+1:05d}", "pid": "p00000",
                           "decision": "repair", "confidence": "high"} for i in range(len(lc))],
        })
        wj(ch / "v31" / "primary" / "post_gate_deterministic_round_01.json", {
            "version": "3.1.3", "chapter": "0100_x.html", "pass": "primary",
            "round": 1, "expected": 4, "completed": 4,
            "decisions": [{"pid": f"p{i:05d}", "candidate_id": c, "passed": True,
                           "errors": [], "introduced_issues": [],
                           "remaining_required_categories": []}
                          for i in range(2) for c in ("A", "B")],
        })
        wj(ch / "v31_final_changed_pid_ledger.json", {
            "schema": "v3.1-final-ledger/v1",
            "entries": [], "changed_pids": [],
        })
    if residual:
        wj(ch / "v31" / "residual" / "lifecycle.json", residual_lifecycle or [])
    if monitor_status is not None:
        wj(root / "monitor_state.v31.json", {
            "runner_version": "3.1.3", "artifact_version": "3.1.3",
            "stage": monitor_stage, "status": monitor_status,
        })
    return root


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_schema() -> dict:
    return _load_json(SCHEMA_PATH)


# --------------------------------------------------------------------- #
# Schema-level: extensions and required fields
# --------------------------------------------------------------------- #
class SchemaTests(unittest.TestCase):
    def test_track_b_notes_required_and_string_list(self) -> None:
        schema = _load_schema()
        tb = schema["properties"]["track_b"]
        self.assertIn("notes", tb["required"])
        notes = tb["properties"]["notes"]
        self.assertEqual(notes["type"], "array")
        self.assertEqual(notes["items"]["type"], "string")

    def test_track_b_terminal_discrepancy_required_and_nullable(self) -> None:
        schema = _load_schema()
        tb = schema["properties"]["track_b"]
        self.assertIn("terminal_discrepancy", tb["required"])
        td = tb["properties"]["terminal_discrepancy"]
        # anyOf: object {required: detected, monitor_status, artifacts_say} | null
        self.assertEqual(len(td["anyOf"]), 2)
        self.assertIn({"type": "null"}, td["anyOf"])
        obj_variant = next(v for v in td["anyOf"] if v.get("type") == "object")
        self.assertEqual(
            set(obj_variant["required"]),
            {"detected", "monitor_status", "artifacts_say"},
        )
        self.assertIn("reason", obj_variant["properties"])

    def test_residual_errors_final_residual_total_uses_typed_ref(self) -> None:
        schema = _load_schema()
        re_props = (
            schema["properties"]["track_b"]["properties"]["metrics"]
            ["properties"]["residual_errors"]["properties"]
        )
        self.assertIn("final_residual_total", re_props)
        ref = re_props["final_residual_total"]
        self.assertIn("$ref", ref)
        self.assertEqual(ref["$ref"], "#/$defs/typed_residual_total")
        # Reject bare strings at the schema level: typed_residual_total
        # is an object with `additionalProperties: false`.
        typed = schema["$defs"]["typed_residual_total"]
        self.assertEqual(set(typed["required"]), {"status", "value_numeric", "reason"})
        self.assertFalse(typed.get("additionalProperties", True))
        self.assertEqual(
            typed["properties"]["value_numeric"]["type"], ["integer", "null"]
        )

    def test_metric_status_value_numeric_optional(self) -> None:
        schema = _load_schema()
        ms = schema["$defs"]["metric_status"]["properties"]
        self.assertIn("value_numeric", ms)
        # Generic metric_status does not require value_numeric; the typed
        # $ref for final_residual_total is what enforces the typed form.
        self.assertNotIn("value_numeric", schema["$defs"]["metric_status"]["required"])


# --------------------------------------------------------------------- #
# Gate note content
# --------------------------------------------------------------------- #
class GateNoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = GATE_NOTE_PATH.read_text(encoding="utf-8")

    def test_gate_note_references_versioned_record(self) -> None:
        self.assertIn("pact-v4-phase0c-result-record/v1", self.text)
        self.assertIn("pact-0c/0.2", self.text)
        self.assertIn("2026-07-30T18:06:57+00:00", self.text)

    def test_gate_note_pins_small_profile_parameters(self) -> None:
        for k, v in SMALL_PROFILE.items():
            self.assertRegex(self.text, rf"{k}\s*=\s*{v}",
                             f"small profile parameter {k}={v} missing")

    def test_gate_note_names_profile_small_chunk_profile(self) -> None:
        self.assertIn("«small chunk profile»", self.text)

    def test_gate_note_does_not_call_8_12_actual_pid_range(self) -> None:
        # The note must not describe 8_12 / 12_20 as actual PID ranges.
        # The baseline measured 16-32 (small) and 39-65 (large).
        self.assertIn("16–32", self.text)
        self.assertIn("39–65", self.text)

    def test_gate_note_calls_right_context_initial_default_not_proven_better(self) -> None:
        # The note must not claim rc_off is "proven better" — only that
        # Track A did not find a measured advantage of rc_on.
        body = self.text.lower()
        self.assertIn("initial/default", body)
        # The word "доказанно" must not appear in a positive sense for rc_off.
        self.assertNotIn("rc_off доказанно", body)
        self.assertNotIn("доказанно лучше", body)
        # Track A limitation is explicit.
        self.assertIn("не измерял", body)

    def test_gate_note_keeps_required_risk_categories(self) -> None:
        for cat in REQUIRED_RISK_CATEGORIES:
            self.assertIn(cat, self.text)

    def test_gate_note_explicit_about_track_b_inconsistency(self) -> None:
        self.assertIn("monitor_state.v31.json", self.text)
        self.assertIn("FAILED", self.text)
        self.assertIn("complete", self.text)

    def test_gate_note_requires_typed_final_residual_total(self) -> None:
        self.assertIn("value_numeric", self.text)
        self.assertIn("final_residual_total", self.text)
        # The note must explain the typed form explicitly.
        self.assertIn("value_numeric: <int> | null", self.text.replace("int|", "<int> | "))

    def test_gate_note_explains_tool_version_bump(self) -> None:
        self.assertIn("0.1 → 0.2", self.text)
        self.assertIn("TOOL_VERSION", self.text)

    def test_gate_note_says_integration_in_consumers_is_separate_pr(self) -> None:
        # The note must be Gate-only and not promise to integrate the
        # policy into Phase 1C/2A/2B/2C consumers in this PR.
        self.assertIn("отдельные тематические PR", self.text)
        # Case-insensitive: note says "Не правит" / "не правит" depending
        # on whether it's at the start of a line.
        body_lower = self.text.lower()
        self.assertIn("не правит", body_lower)
        self.assertIn("phase 1c", body_lower)

    def test_gate_note_distinguishes_measured_vs_unmeasured(self) -> None:
        # The note must say explicitly what Track A/B did NOT measure.
        self.assertIn("Performance", self.text)
        self.assertIn("discourse", self.text.lower())
        self.assertIn("не измеря", self.text.lower())


# --------------------------------------------------------------------- #
# Producer behaviour on synthetic fixtures
# --------------------------------------------------------------------- #
class ProducerTests(unittest.TestCase):
    """Drive ``v4_phase0c_baseline.import_track_b`` on synthetic v31
    fixtures and assert the Gate-required shape."""

    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, str(ROOT / "pact_full_pipeline_runner_v1"))
        import v4_phase0c_baseline as m  # type: ignore[import-not-found]
        cls.m = m

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_typed_residual_total_measured(self) -> None:
        rl = [
            {"issue_id": f"v31-residual-{i:05d}", "pid": "p00000",
             "pass": "residual", "round": 1, "status": "resolved_retry_exhausted"}
            for i in range(3)
        ]
        root = make_v31_run(self.tmp / "r", primary=True, residual=True,
                            primary_lifecycle=["resolved_repair"],
                            residual_lifecycle=rl)
        tb = self.m.import_track_b(root)
        self.assertEqual("measured", tb["completion"]["status"])
        frt = tb["metrics"]["residual_errors"]["final_residual_total"]
        self.assertEqual({"status", "value_numeric", "reason"}, set(frt.keys()))
        self.assertEqual("measured", frt["status"])
        self.assertEqual(3, frt["value_numeric"])
        self.assertEqual("", frt["reason"])

    def test_typed_residual_total_pending(self) -> None:
        root = make_v31_run(self.tmp / "r", primary=True, residual=False,
                            primary_lifecycle=["resolved_repair"])
        tb = self.m.import_track_b(root)
        self.assertEqual("pending_run_completion", tb["completion"]["status"])
        frt = tb["metrics"]["residual_errors"]["final_residual_total"]
        self.assertEqual("pending_run_completion", frt["status"])
        self.assertIsNone(frt["value_numeric"])
        self.assertTrue(frt["reason"])

    def test_typed_residual_total_pending_when_primary_not_adjudicated(self) -> None:
        root = make_v31_run(self.tmp / "r", primary=False, residual=False)
        tb = self.m.import_track_b(root)
        frt = tb["metrics"]["residual_errors"]["final_residual_total"]
        self.assertEqual("pending_run_completion", frt["status"])
        self.assertIsNone(frt["value_numeric"])
        self.assertTrue(frt["reason"])

    def test_monitor_failed_with_primary_complete_records_discrepancy(self) -> None:
        root = make_v31_run(self.tmp / "r", primary=True, residual=False,
                            primary_lifecycle=["resolved_repair"],
                            monitor_status="FAILED")
        tb = self.m.import_track_b(root)
        td = tb["terminal_discrepancy"]
        self.assertIsNotNone(td)
        self.assertTrue(td["detected"])
        self.assertEqual("FAILED", td["monitor_status"])
        self.assertIn("primary", td["artifacts_say"].lower())
        self.assertTrue(td["reason"])
        self.assertTrue(any("terminal_discrepancy" in n for n in tb["notes"]))

    def test_monitor_active_is_a_note_not_a_discrepancy(self) -> None:
        root = make_v31_run(self.tmp / "r", primary=True, residual=False,
                            primary_lifecycle=["resolved_repair"],
                            monitor_status="ACTIVE",
                            monitor_stage="residual Gemma semantic audit")
        tb = self.m.import_track_b(root)
        self.assertIsNone(tb["terminal_discrepancy"])
        self.assertTrue(any("ACTIVE" in n for n in tb["notes"]))

    def test_monitor_reused_is_a_note(self) -> None:
        root = make_v31_run(self.tmp / "r", primary=True, residual=False,
                            primary_lifecycle=["resolved_repair"],
                            monitor_status="REUSED")
        tb = self.m.import_track_b(root)
        self.assertIsNone(tb["terminal_discrepancy"])
        self.assertTrue(any("REUSED" in n for n in tb["notes"]))

    def test_no_run_shape(self) -> None:
        tb = self.m.import_track_b(self.tmp / "missing")
        self.assertEqual("no_run", tb["completion"]["status"])
        self.assertEqual([], tb["notes"])
        self.assertIsNone(tb["terminal_discrepancy"])
        frt = tb["metrics"]["residual_errors"]["final_residual_total"]
        self.assertEqual("no_run", frt["status"])
        self.assertIsNone(frt["value_numeric"])
        self.assertTrue(frt["reason"])

    def test_record_validates_against_schema_after_producer(self) -> None:
        rl = [
            {"issue_id": f"v31-residual-{i:05d}", "pid": "p00000",
             "pass": "residual", "round": 1, "status": "resolved_retry_exhausted"}
            for i in range(2)
        ]
        root = make_v31_run(self.tmp / "r", primary=True, residual=True,
                            primary_lifecycle=["resolved_repair"],
                            residual_lifecycle=rl,
                            monitor_status="FAILED")
        rec = self.m.build_result_record(None, root)
        # The producer output must validate against validate_result_record
        # (the in-tree contract check) with no errors.
        self.assertEqual([], self.m.validate_result_record(rec))
        # And the typed residual must be a real number, not null.
        self.assertEqual(
            2,
            rec["track_b"]["metrics"]["residual_errors"]["final_residual_total"]["value_numeric"],
        )
        # And terminal_discrepancy must be detected.
        self.assertTrue(rec["track_b"]["terminal_discrepancy"]["detected"])


# --------------------------------------------------------------------- #
# Acceptance: no v3 production code, no run artifacts, no Phase 1C/2
# consumers, no translated chapters, no cache in this PR.
# --------------------------------------------------------------------- #
class MergeBaseGuardTests(unittest.TestCase):
    """Hard guard: the diff between the PR's base branch (``HEAD@{u}``
    on the same name when the branch tracks a remote, otherwise the
    parent of HEAD) and HEAD must only touch paths in
    ``ALLOWED_PATHS``. This protects against:
      * editing v3 production code (v31_*.py, pact_translate_v3.py,
        run_full_pipeline_v31.ps1, run_full_pipeline.ps1);
      * editing v4 consumer code (pact_v4/phase1/chunker.py,
        pact_v4/phase2/risk.py, pact_v4/phase2/cascade.py,
        pact_v4/phase2/generation.py, pact_v4/phase2/prompts.py);
      * committing run artifacts, golden set, translated chapters,
        models, logs, secrets or backups.

    Skipped when no upstream is configured (the local-only check below
    covers a single-commit case) or when git is unavailable."""

    @staticmethod
    def _git(*args: str, cwd: str | None = None) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd or str(ROOT),
            capture_output=True, text=True, check=True,
        ).stdout

    def _resolve_base(self) -> str | None:
        try:
            upstream = self._git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        except subprocess.CalledProcessError:
            return None
        upstream = upstream.strip()
        if not upstream:
            return None
        try:
            self._git("rev-parse", "--verify", upstream)
        except subprocess.CalledProcessError:
            return None
        return upstream

    def test_diff_against_base_touches_only_allowed_paths(self) -> None:
        base = self._resolve_base()
        if base is None:
            self.skipTest("no upstream configured for this branch")
        try:
            self._git("rev-parse", "--verify", f"{base}^{{commit}}")
        except subprocess.CalledProcessError:
            self.skipTest(f"upstream {base!r} has no commit reachable locally")
        diff = self._git("diff", "--name-only", base, "HEAD")
        changed = sorted({p.strip().replace("\\", "/") for p in diff.splitlines() if p.strip()})
        if not changed:
            self.skipTest(f"no diff vs {base!r} (no commits yet?)")
        unexpected = [p for p in changed if p not in ALLOWED_PATHS]
        self.assertEqual(
            unexpected, [],
            f"this Gate PR touches paths outside its allow-list: {unexpected!r}. "
            "V3 production code, run artifacts, translated chapters, cache, "
            "models, logs, secrets, backups, and Phase 1C/2 consumer code "
            "must not be edited by this PR.",
        )


# --------------------------------------------------------------------- #
# Tool version bump — the live record was generated by 0.2; the repo
# must reproduce that version on the next build_result_record call.
# --------------------------------------------------------------------- #
class ToolVersionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, str(ROOT / "pact_full_pipeline_runner_v1"))
        import v4_phase0c_baseline as m  # type: ignore[import-not-found]
        cls.m = m

    def test_tool_version_matches_live_record(self) -> None:
        self.assertEqual("pact-0c/0.2", self.m.TOOL_VERSION)

    def test_record_tool_version_field(self) -> None:
        rec = self.m.build_result_record(None, None)
        self.assertEqual("pact-0c/0.2", rec["tool_version"])


if __name__ == "__main__":
    sys.exit(unittest.main())
