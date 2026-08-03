"""Tests for the read-only run-progress tracker CLI.

Covers the tracker acceptance criteria of the Phase 12 card:

  * fine mode: on a synthetic catalog with ``phase_progress.ndjson`` the
    tracker shows exact unit/region progress matching the source events;
  * coarse mode: without ``phase_progress.ndjson`` (e.g. the pre-Phase-12
    ``run_001``) phase/journal/b2_handoff inference still works;
  * read-only: ``render_report`` / ``main`` never write to ``out_dir`` and
    never touch the pipeline;
  * resume-aware identity (``resumed_from_index`` surfaced from events).

The tracker is pure diagnostics — no subprocess, no HTTP.
"""
from __future__ import annotations

import json
from pathlib import Path

from pact_full_pipeline_runner_v1 import v4_phase_progress as tracker
from pact_v4.pipeline.phase_progress import PHASE_PROGRESS_FILENAME


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_ndjson(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _make_run_dir(tmp_path: Path) -> Path:
    """Synthetic run dir: 2 chunks, all selected, full journal + handoff."""
    out = tmp_path / "run_002"
    _write(out / "chunk_plan.json", {
        "chunks": [{"chunk_id": "c1", "pids": ["p1"]}, {"chunk_id": "c2", "pids": ["p2"]}],
    })
    _write_ndjson(out / "journal.ndjson", [
        {"chunk_id": "c1", "outcome": "selected", "selected_candidate_id": "c1:sel", "selected_role": "fidelity_first"},
        {"chunk_id": "c2", "outcome": "selected", "selected_candidate_id": "c2:sel", "selected_role": "fidelity_first"},
    ])
    _write(out / "b2_handoff.json", {
        "chunks": [
            {"chunk_id": "c1", "status": "audited", "audited_candidate_id": "c1:sel",
             "audit_status": "clean", "committed": True},
            {"chunk_id": "c2", "status": "audited", "audited_candidate_id": "c2:sel",
             "audit_status": "findings_present", "committed": True},
        ],
    })
    return out


def _fine_events() -> list:
    return [
        {"schema": "pact-v4-phase-progress/ndjson/v1", "event": "run_started", "ts": "2026-08-03T10:00:00+00:00",
         "chapter_id": "046", "out_dir": "run_002", "started_at": "2026-08-03T10:00:00+00:00",
         "backend_identity_hash": "h1", "resumed_from_index": 0},
        {"schema": "pact-v4-phase-progress/ndjson/v1", "event": "chunk_started", "ts": "2026-08-03T10:00:01+00:00", "chunk_id": "c1"},
        {"schema": "pact-v4-phase-progress/ndjson/v1", "event": "chunk_done", "ts": "2026-08-03T10:00:02+00:00", "chunk_id": "c1", "outcome": "selected"},
        {"schema": "pact-v4-phase-progress/ndjson/v1", "event": "chunk_started", "ts": "2026-08-03T10:00:03+00:00", "chunk_id": "c2"},
        {"schema": "pact-v4-phase-progress/ndjson/v1", "event": "chunk_done", "ts": "2026-08-03T10:00:04+00:00", "chunk_id": "c2", "outcome": "selected"},
        {"schema": "pact-v4-phase-progress/ndjson/v1", "event": "audit_unit_started", "ts": "2026-08-03T10:00:05+00:00", "chunk_id": "c1", "detector": "qwen_chapter_audit"},
        {"schema": "pact-v4-phase-progress/ndjson/v1", "event": "audit_unit_done", "ts": "2026-08-03T10:00:06+00:00", "chunk_id": "c1", "detector": "qwen_chapter_audit", "status": "ok"},
        {"schema": "pact-v4-phase-progress/ndjson/v1", "event": "audit_unit_started", "ts": "2026-08-03T10:00:07+00:00", "chunk_id": "c1", "detector": "gemma_russian_review"},
        {"schema": "pact-v4-phase-progress/ndjson/v1", "event": "audit_unit_done", "ts": "2026-08-03T10:00:08+00:00", "chunk_id": "c1", "detector": "gemma_russian_review", "status": "ok"},
        {"schema": "pact-v4-phase-progress/ndjson/v1", "event": "audit_done", "ts": "2026-08-03T10:00:09+00:00", "status": "complete"},
        {"schema": "pact-v4-phase-progress/ndjson/v1", "event": "repair_round_started", "ts": "2026-08-03T10:00:10+00:00", "round_number": 1},
        {"schema": "pact-v4-phase-progress/ndjson/v1", "event": "region_started", "ts": "2026-08-03T10:00:11+00:00",
         "chunk_id": "c2", "repair_id": "r1", "target_pids": ["p2"], "action": "region_edit"},
        {"schema": "pact-v4-phase-progress/ndjson/v1", "event": "region_done", "ts": "2026-08-03T10:00:12+00:00",
         "chunk_id": "c2", "repair_id": "r1", "target_pids": ["p2"], "action": "region_edit", "committed": True, "reason": "ok"},
        {"schema": "pact-v4-phase-progress/ndjson/v1", "event": "reaudit_unit_started", "ts": "2026-08-03T10:00:13+00:00", "chunk_id": "c2", "detector": "qwen_chapter_audit"},
        {"schema": "pact-v4-phase-progress/ndjson/v1", "event": "reaudit_unit_done", "ts": "2026-08-03T10:00:14+00:00", "chunk_id": "c2", "detector": "qwen_chapter_audit", "status": "ok"},
        {"schema": "pact-v4-phase-progress/ndjson/v1", "event": "repair_done", "ts": "2026-08-03T10:00:15+00:00", "rounds": 1},
        {"schema": "pact-v4-phase-progress/ndjson/v1", "event": "terminal", "ts": "2026-08-03T10:00:16+00:00", "status": "complete"},
    ]


def test_fine_mode_shows_exact_progress(tmp_path: Path):
    out = _make_run_dir(tmp_path)
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, _fine_events())

    events = tracker._load_events(out)
    identity = tracker._identity(out, events)
    assert identity["alive"] is False
    assert identity["resumed_from_index"] == 0
    assert identity["started_at"] == "2026-08-03T10:00:00+00:00"

    phase, basis = tracker._detect_phase(out, events)
    assert phase == "done"
    assert "terminal event" in basis

    rows = tracker._chunk_table(out, events)
    by_chunk = {row["chunk_id"]: row for row in rows}
    assert by_chunk["c1"]["trial"] == "selected"
    assert by_chunk["c1"]["audit"] == "clean"
    assert by_chunk["c2"]["trial"] == "selected"
    assert by_chunk["c2"]["audit"] == "findings_present"
    # c2 has one committed repair; c1 has none.
    assert by_chunk["c2"]["repair"] == "committed"
    assert by_chunk["c1"]["repair"] == "not_started"

    region_counts = tracker._region_counts(events)
    assert region_counts["planned"] == 1
    assert region_counts["done"] == 1
    assert region_counts["committed"] == 1
    assert region_counts["debt"] == 0

    reaudit = tracker._reaudit_counts(events)
    assert reaudit == {"started": 1, "done": 1}

    terminal = tracker._terminal_counts(out, events)
    assert terminal["status"] == "complete"
    assert terminal["basis"] == "terminal event"


def test_fine_mode_in_progress_phase_and_in_flight(tmp_path: Path):
    out = _make_run_dir(tmp_path)
    # Drop the terminal + repair_done, leave a region_started without done.
    events = [e for e in _fine_events() if e["event"] not in ("repair_done", "terminal")]
    for e in events:
        if e["event"] == "region_done":
            events.remove(e)
            break
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, events)

    phase, basis = tracker._detect_phase(out, tracker._load_events(out))
    assert phase == "step7"
    in_flight = tracker._in_flight_model_activity(tracker._load_events(out))
    assert any("region" in item for item in in_flight)
    assert tracker._current_round(tracker._load_events(out)) == 1


def test_coarse_mode_without_phase_progress(tmp_path: Path):
    out = _make_run_dir(tmp_path)  # no phase_progress.ndjson written
    events = tracker._load_events(out)
    assert events == []

    phase, basis = tracker._detect_phase(out, events)
    assert phase == "step7"  # b2_handoff exists, no repair_report
    assert "b2_handoff.json" in basis

    rows = tracker._chunk_table(out, events)
    by_chunk = {row["chunk_id"]: row for row in rows}
    # Trial + audit statuses come from journal / b2_handoff in coarse mode.
    assert by_chunk["c1"]["trial"] == "selected"
    assert by_chunk["c1"]["audit"] == "clean"
    assert by_chunk["c2"]["audit"] == "findings_present"
    # No repair artifacts -> not_started.
    assert by_chunk["c1"]["repair"] == "not_started"

    # A partial journal (Steps 1-5) still resolves phase correctly.
    out2 = tmp_path / "run_001"
    _write(out2 / "chunk_plan.json", {"chunks": [{"chunk_id": "c1"}, {"chunk_id": "c2"}]})
    _write_ndjson(out2 / "journal.ndjson", [{"chunk_id": "c1", "outcome": "selected"}])
    phase2, basis2 = tracker._detect_phase(out2, [])
    assert phase2 == "steps1-5"
    assert "1/2" in basis2

    # Full journal + no b2_handoff -> Step 6.
    out3 = tmp_path / "run_step6"
    _write(out3 / "chunk_plan.json", {"chunks": [{"chunk_id": "c1"}]})
    _write_ndjson(out3 / "journal.ndjson", [{"chunk_id": "c1", "outcome": "selected"}])
    phase3, basis3 = tracker._detect_phase(out3, [])
    assert phase3 == "step6"
    assert "journal full" in basis3

    # Record present -> done.
    out4 = tmp_path / "run_done"
    _write(out4 / "strict_chapter_trial_record.json", {
        "started_at": "2026-08-03T10:00:00+00:00", "finished_at": "2026-08-03T10:10:00+00:00",
        "resumed_from_index": 0, "step8": {"status": "complete"},
    })
    phase4, basis4 = tracker._detect_phase(out4, [])
    assert phase4 == "done"


def test_tracker_is_read_only(tmp_path: Path):
    out = _make_run_dir(tmp_path)
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, _fine_events())
    before = {p.name: (p.stat().st_mtime_ns, p.stat().st_size) for p in out.iterdir()}
    before_files = sorted(p.name for p in out.iterdir())

    report = tracker.render_report(out)
    assert "V4 run progress" in report
    assert "chunk" in report.lower()

    after_files = sorted(p.name for p in out.iterdir())
    assert after_files == before_files
    for name, (mtime_ns, size) in before.items():
        p = out / name
        assert (p.stat().st_mtime_ns, p.stat().st_size) == (mtime_ns, size)


def test_tracker_cli_main_read_only_and_exit_code(tmp_path: Path):
    out = _make_run_dir(tmp_path)
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, _fine_events())
    before = {p.name for p in out.iterdir()}
    rc = tracker.main(["--out-dir", str(out)])
    assert rc == 0
    after = {p.name for p in out.iterdir()}
    assert after == before


def test_tracker_resume_identity_from_events(tmp_path: Path):
    out = _make_run_dir(tmp_path)
    events = _fine_events()
    events[0]["resumed_from_index"] = 1
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, events)
    identity = tracker._identity(out, tracker._load_events(out))
    assert identity["resumed_from_index"] == 1
    assert identity["backend_identity_hash"] == "h1"
