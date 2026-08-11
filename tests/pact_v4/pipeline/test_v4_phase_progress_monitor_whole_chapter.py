"""V4.1 M card: whole-chapter monitor support tests.

Covers ``docs/plans/V4_1_AUDIT_B1_RU.md`` §13 (card M — whole-chapter events
+ live B-phase):

* ``PhaseProgressWriter`` gains the ``wc_*`` event helpers
  (wc_generation_started / wc_retry_attempt / wc_generation_done /
  wc_validated) and writes them with the expected fields;
* ``generate_whole_chapter`` fires the diagnostics ``on_retry`` hook with
  the reason vocabulary (malformed/missing_pid/truncated/abort) — the
  monitor's "GEN attempt N/M (reason)" source;
* the monitor (``v4_phase_progress``) detects the whole-chapter path from
  ``wc_*`` events (phase ``gen`` while the 10-minute generation runs, then
  ``step6``/``step7``/``step8`` from the B3 audit journal, then ``done``);
* the one-line status renders ``GEN attempt N/M (reason)`` live, ``AUDIT
  chunk N/8`` and ``REPAIR regions done/committed/debt`` from the B3 journal
  events, and ``DONE`` with the wc_validated PID flags after the run;
* chunked mode is NOT broken: old chunk_started/audit_unit/region events
  still drive the same phases as before (backward compatibility).

Pure diagnostics — synthetic ndjson only, no subprocess / HTTP / llama-server.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pact_full_pipeline_runner_v1 import v4_phase_progress as tracker
from pact_v4.pipeline.phase_progress import (
    PHASE_PROGRESS_FILENAME,
    PHASE_PROGRESS_SCHEMA,
    PhaseProgressWriter,
)

from tests.pact_v4.phase2.test_generation_whole_chapter import (
    _EchoCaller,
    _ScriptedCaller,
    _artifacts,
    _params,
)
from pact_v4.phase2.generation import (
    GenerationCache,
    WholeChapterRetryPolicy,
    generate_whole_chapter,
)


def _iso(seconds_ago: float) -> str:
    ts = datetime.now(timezone.utc).timestamp() - seconds_ago
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


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


def _wc_event(event: str, seconds_ago: float, **fields) -> dict:
    return {
        "schema": PHASE_PROGRESS_SCHEMA,
        "event": event,
        "ts": _iso(seconds_ago),
        **fields,
    }


def _b3_event(event: str, seconds_ago: float, **fields) -> dict:
    return {
        "schema": "pact-v4-b3-audit-journal/v1",
        "event": event,
        "ts": _iso(seconds_ago),
        **fields,
    }


def _wc_run_dir(tmp_path: Path, *, with_b3: bool = True) -> Path:
    """Whole-chapter run dir: run_started + wc generation events."""
    out = tmp_path / "chapter_0001"
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        _wc_event("run_started", 600, chapter_id="0001", out_dir=str(out),
                  started_at=_iso(600), backend_identity_hash="h", resumed_from_index=0),
        _wc_event("wc_generation_started", 550, pid_count=120,
                  reasoning_budget=3, model="gemma-4-26b", max_attempts=3),
    ])
    if with_b3:
        _write_ndjson(out / "audit_journal.ndjson", [])
    return out


# ---------------------------------------------------------------------------
# PhaseProgressWriter: wc_* event helpers
# ---------------------------------------------------------------------------


def test_writer_emits_whole_chapter_events(tmp_path: Path):
    writer = PhaseProgressWriter(tmp_path)
    writer.run_started(chapter_id="0001", out_dir=tmp_path,
                       started_at="2026-08-10T12:00:00+00:00",
                       backend_identity_hash="h", resumed_from_index=0)
    writer.wc_generation_started(pid_count=120, reasoning_budget=3,
                                 model="gemma-4-26b", max_attempts=3)
    writer.wc_retry_attempt(attempt=1, reason="malformed")
    writer.wc_retry_attempt(attempt=2, reason="malformed")
    writer.wc_generation_done(finish_reason="complete", pid_count=120,
                              duration=602.5)
    writer.wc_validated(json_ok=True, pids_ok=True, order_ok=True)
    writer.close()

    events = tracker._load_events(tmp_path)
    names = [e["event"] for e in events]
    assert names == [
        "run_started", "wc_generation_started", "wc_retry_attempt",
        "wc_retry_attempt", "wc_generation_done", "wc_validated",
    ]
    started = events[1]
    assert started["pid_count"] == 120
    assert started["reasoning_budget"] == 3
    assert started["model"] == "gemma-4-26b"
    assert started["max_attempts"] == 3
    assert events[2]["reason"] == "malformed"
    assert events[4]["finish_reason"] == "complete"
    assert events[4]["duration"] == 602.5
    assert events[5]["json_ok"] is True and events[5]["pids_ok"] is True


def test_writer_wc_events_crash_safe_partial_tail(tmp_path: Path):
    writer = PhaseProgressWriter(tmp_path)
    writer.wc_generation_started(pid_count=10, reasoning_budget=1,
                                 model="m", max_attempts=3)
    writer.close()
    with open(tmp_path / PHASE_PROGRESS_FILENAME, "a", encoding="utf-8") as handle:
        handle.write('{"schema": "pact-v4-phase-progress/ndjson/v1", "event": "wc_ret')
    events = tracker._load_events(tmp_path)
    assert [e["event"] for e in events] == ["wc_generation_started"]


# ---------------------------------------------------------------------------
# generate_whole_chapter: on_retry diagnostics hook (reason vocabulary)
# ---------------------------------------------------------------------------


def test_generate_whole_chapter_on_retry_reports_malformed(tmp_path):
    source, snapshot, chunk_plan, config = _artifacts(tmp_path, n=8)
    pid_map = _pid_map(chunk_plan, snapshot)
    caller = _ScriptedCaller(["not json", "not json", "not json"])
    retries: list = []

    outcome = generate_whole_chapter(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, pid_map=pid_map,
        glossary=(), bible_text="", config=config, params=_params(),
        model_caller=caller, cache=GenerationCache(),
        retry=WholeChapterRetryPolicy(max_attempts=3, base_delay_seconds=0),
        on_retry=lambda attempt, reason: retries.append((attempt, reason)),
    )
    assert outcome.status == "incomplete"
    assert retries == [(1, "malformed"), (2, "malformed"), (3, "malformed")]


def test_generate_whole_chapter_on_retry_missing_pid(tmp_path):
    source, snapshot, chunk_plan, config = _artifacts(tmp_path, n=8)
    pid_map = _pid_map(chunk_plan, snapshot)
    pids = list(pid_map.pids)
    dropped = json.dumps({p: "Текст" for p in pids[1:]}, ensure_ascii=False)
    caller = _ScriptedCaller([dropped, dropped, dropped])
    retries: list = []

    outcome = generate_whole_chapter(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, pid_map=pid_map,
        glossary=(), bible_text="", config=config, params=_params(),
        model_caller=caller, cache=GenerationCache(),
        retry=WholeChapterRetryPolicy(max_attempts=3, base_delay_seconds=0),
        on_retry=lambda attempt, reason: retries.append((attempt, reason)),
    )
    assert outcome.status == "incomplete"
    assert retries == [(1, "missing_pid"), (2, "missing_pid"), (3, "missing_pid")]


def test_generate_whole_chapter_on_retry_abort_then_success(tmp_path):
    # Session abort on the first call, success on the second: the hook fires
    # for the abort with "abort" and then the run completes (no further retry).
    source, snapshot, chunk_plan, config = _artifacts(tmp_path, n=8)
    pid_map = _pid_map(chunk_plan, snapshot)
    caller = _EchoCaller(abort_then_succeed=1)
    retries: list = []

    outcome = generate_whole_chapter(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, pid_map=pid_map,
        glossary=(), bible_text="", config=config, params=_params(),
        model_caller=caller, cache=GenerationCache(),
        retry=WholeChapterRetryPolicy(max_attempts=3, base_delay_seconds=0),
        on_retry=lambda attempt, reason: retries.append((attempt, reason)),
    )
    assert outcome.status == "complete"
    assert retries == [(1, "abort")]


def test_generate_whole_chapter_on_retry_hook_never_breaks_generation(tmp_path):
    # A raising hook must be swallowed: generation still completes.
    source, snapshot, chunk_plan, config = _artifacts(tmp_path, n=8)
    pid_map = _pid_map(chunk_plan, snapshot)
    caller = _EchoCaller()

    def _boom(attempt, reason):
        raise RuntimeError("hook exploded")

    outcome = generate_whole_chapter(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, pid_map=pid_map,
        glossary=(), bible_text="", config=config, params=_params(),
        model_caller=caller, cache=GenerationCache(),
        retry=WholeChapterRetryPolicy(max_attempts=3, base_delay_seconds=0),
        on_retry=_boom,
    )
    assert outcome.status == "complete"


def _pid_map(chunk_plan, snapshot):
    from pact_v4.phase1.models import WholeChapterPidMap
    return WholeChapterPidMap.derive(chunk_plan, snapshot)


# ---------------------------------------------------------------------------
# Monitor: whole-chapter phase detection
# ---------------------------------------------------------------------------


def test_monitor_whole_chapter_generation_phase_live(tmp_path: Path):
    out = _wc_run_dir(tmp_path)
    events = tracker._load_events(out)
    phase, basis = tracker._detect_phase(out, events)
    assert phase == "gen"
    assert "attempt 1/3" in basis
    assert "думает" in basis  # live thinking-time signal


def test_monitor_whole_chapter_phase_with_retry_reason(tmp_path: Path):
    out = _wc_run_dir(tmp_path)
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        _wc_event("wc_retry_attempt", 500, attempt=1, reason="malformed"),
        _wc_event("wc_retry_attempt", 490, attempt=2, reason="malformed"),
    ])
    events = tracker._load_events(out)
    phase, basis = tracker._detect_phase(out, events)
    assert phase == "gen"
    assert "attempt 2/3" in basis
    assert "reason: malformed" in basis


def test_monitor_whole_chapter_audit_chunk_phase(tmp_path: Path):
    out = _wc_run_dir(tmp_path)
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        _wc_event("wc_generation_done", 480, finish_reason="complete",
                  pid_count=120, duration=70.0),
        _wc_event("wc_validated", 479, json_ok=True, pids_ok=True, order_ok=True),
    ])
    _write_ndjson(out / "audit_journal.ndjson", [
        _b3_event("audit_started", 460),
        _b3_event("audit_chunk_started", 450, chunk=1, total=8),
        _b3_event("audit_chunk_done", 440, chunk=1, total=8, status="ok"),
        _b3_event("audit_chunk_started", 430, chunk=2, total=8),
        _b3_event("audit_chunk_done", 420, chunk=2, total=8, status="ok"),
    ])
    events = tracker._load_events(out)
    phase, basis = tracker._detect_phase(out, events)
    assert phase == "step6"
    assert "chunk 2/8" in basis


def test_monitor_whole_chapter_repair_phase(tmp_path: Path):
    out = _wc_run_dir(tmp_path)
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        _wc_event("wc_generation_done", 480, finish_reason="complete",
                  pid_count=120, duration=70.0),
        _wc_event("wc_validated", 479, json_ok=True, pids_ok=True, order_ok=True),
    ])
    _write_ndjson(out / "audit_journal.ndjson", [
        _b3_event("audit_started", 460),
        _b3_event("audit_chunk_started", 450, chunk=1, total=8),
        _b3_event("audit_chunk_done", 440, chunk=1, total=8, status="ok"),
        _b3_event("audit_complete", 430, audit_complete=True, issue_count=3),
        _b3_event("repair_round", 400, round=1, eligible_count=3,
                  committed_pids=["p00001"], passed_pids=["p00002"],
                  debt_trace=["pid p00003: not eligible"], repair_complete=True),
        _b3_event("reaudit_scope", 390, scope_pids=["p00001"], full=False,
                  issue_count=0, failed=False),
    ])
    events = tracker._load_events(out)
    phase, basis = tracker._detect_phase(out, events)
    assert phase == "step7"
    assert "committed=1" in basis and "passed=1" in basis and "debt=1" in basis


def test_monitor_whole_chapter_gate_then_done(tmp_path: Path):
    out = _wc_run_dir(tmp_path)
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        _wc_event("wc_generation_done", 480, finish_reason="complete",
                  pid_count=120, duration=70.0),
        _wc_event("wc_validated", 479, json_ok=True, pids_ok=True, order_ok=True),
    ])
    _write_ndjson(out / "audit_journal.ndjson", [
        _b3_event("audit_started", 460),
        _b3_event("audit_chunk_started", 450, chunk=1, total=8),
        _b3_event("audit_chunk_done", 440, chunk=1, total=8, status="ok"),
        _b3_event("audit_complete", 430, audit_complete=True, issue_count=0),
        _b3_event("gate", 400, audit_complete=True, released_as_audited=True),
    ])
    events = tracker._load_events(out)
    phase, basis = tracker._detect_phase(out, events)
    assert phase == "step8"
    assert "released_as_audited=True" in basis

    # Record present -> done (final status with the PID validation flags).
    _write(out / "strict_chapter_trial_record.json", {"finished_at": _iso(10)})
    phase2, _ = tracker._detect_phase(out, events)
    assert phase2 == "done"


def test_monitor_whole_chapter_generation_only_no_b3(tmp_path: Path):
    # Generation-only whole-chapter run (B3 absent): after wc_generation_done
    # the monitor must NOT claim step6/7 — it waits for the record.
    out = _wc_run_dir(tmp_path, with_b3=False)
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        _wc_event("wc_generation_done", 480, finish_reason="complete",
                  pid_count=120, duration=70.0),
        _wc_event("wc_validated", 479, json_ok=True, pids_ok=True, order_ok=True),
    ])
    events = tracker._load_events(out)
    phase, basis = tracker._detect_phase(out, events)
    assert phase == "steps1-5"
    assert "generation done" in basis


# ---------------------------------------------------------------------------
# Monitor: one-line status
# ---------------------------------------------------------------------------


def test_status_line_whole_chapter_live_generation(tmp_path: Path):
    out = _wc_run_dir(tmp_path)
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        _wc_event("wc_retry_attempt", 100, attempt=1, reason="truncated"),
    ])
    events = tracker._load_events(out)
    line = tracker._status_line(out, events, "gen")
    assert line.startswith("[0001] gen")
    assert "GEN attempt 1/3 (reason: truncated)" in line


def test_status_line_whole_chapter_audit_and_repair(tmp_path: Path):
    out = _wc_run_dir(tmp_path)
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        _wc_event("wc_generation_done", 480, finish_reason="complete",
                  pid_count=120, duration=70.0),
        _wc_event("wc_validated", 479, json_ok=True, pids_ok=True, order_ok=True),
    ])
    _write_ndjson(out / "audit_journal.ndjson", [
        _b3_event("audit_chunk_started", 450, chunk=5, total=8),
        _b3_event("audit_chunk_done", 440, chunk=5, total=8, status="ok"),
        _b3_event("repair_round", 400, round=1, eligible_count=3,
                  committed_pids=["p00001"], passed_pids=["p00002"],
                  debt_trace=["x"], repair_complete=False),
    ])
    events = tracker._load_events(out)
    line = tracker._status_line(out, events, "step7")
    assert "[0001] step7" in line
    assert "AUDIT chunk 5/8" in line
    assert "committed=1" in line and "debt=1" in line


def test_status_line_whole_chapter_done_with_validation(tmp_path: Path):
    out = _wc_run_dir(tmp_path)
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        _wc_event("wc_generation_done", 480, finish_reason="complete",
                  pid_count=120, duration=70.0),
        _wc_event("wc_validated", 479, json_ok=True, pids_ok=True, order_ok=True),
        _wc_event("terminal", 400, status="complete"),
    ])
    events = tracker._load_events(out)
    line = tracker._status_line(out, events, "done")
    assert line.endswith("DONE (complete)")
    assert "GEN attempt 1/3 done finish_reason=complete" in line


def test_status_line_chunked_mode_backward_compat(tmp_path: Path):
    # A chunked run (no wc_* events) still renders: GEN chunks + REPAIR
    # regions from the old region events.
    out = tmp_path / "run_chunked"
    _write(out / "chunk_plan.json", {"chunks": [{"chunk_id": "c1"}, {"chunk_id": "c2"}]})
    _write(out / "b2_handoff.json", {"chunks": [
        {"chunk_id": "c1", "audit_status": "findings_present"},
        {"chunk_id": "c2", "audit_status": "clean"},
    ]})
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        _wc_event("run_started", 600, chapter_id="0001", out_dir=str(out),
                  started_at=_iso(600), resumed_from_index=0),
        _wc_event("repair_round_started", 300, round_number=1),
        _wc_event("region_started", 290, chunk_id="c1", repair_id="r1",
                  target_pids=["p1"], action="repair"),
        _wc_event("region_done", 280, chunk_id="c1", repair_id="r1",
                  target_pids=["p1"], action="repair", committed=True, reason="ok"),
    ])
    events = tracker._load_events(out)
    phase, _ = tracker._detect_phase(out, events)
    assert phase == "step7"
    line = tracker._status_line(out, events, phase)
    assert "REPAIR regions done=1 committed=1 debt=0" in line


# ---------------------------------------------------------------------------
# Monitor: full report integration (render_report) + read-only guarantee
# ---------------------------------------------------------------------------


def test_render_report_whole_chapter_shows_generation_and_validation(tmp_path: Path):
    out = _wc_run_dir(tmp_path)
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        _wc_event("wc_retry_attempt", 500, attempt=1, reason="malformed"),
        _wc_event("wc_generation_done", 480, finish_reason="complete",
                  pid_count=120, duration=70.0),
        _wc_event("wc_validated", 479, json_ok=True, pids_ok=True, order_ok=True),
    ])
    _write_ndjson(out / "audit_journal.ndjson", [
        _b3_event("audit_chunk_started", 450, chunk=5, total=8),
        _b3_event("audit_chunk_done", 440, chunk=5, total=8, status="ok"),
    ])
    report = tracker.render_report(out)
    assert "status: [0001] step6" in report
    assert "GEN attempt 1/3 done finish_reason=complete" in report
    assert "AUDIT chunk 5/8" in report
    assert "PID validation: json_ok=True pids_ok=True order_ok=True" in report
    assert "Step 8: not started" in report


def test_render_report_whole_chapter_final_done(tmp_path: Path):
    out = _wc_run_dir(tmp_path)
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        _wc_event("wc_generation_done", 480, finish_reason="complete",
                  pid_count=120, duration=70.0),
        _wc_event("wc_validated", 479, json_ok=True, pids_ok=True, order_ok=True),
        _wc_event("terminal", 400, status="complete"),
    ])
    _write_ndjson(out / "audit_journal.ndjson", [
        _b3_event("audit_started", 460),
        _b3_event("audit_chunk_started", 450, chunk=1, total=8),
        _b3_event("audit_chunk_done", 440, chunk=1, total=8, status="ok"),
        _b3_event("audit_complete", 430, audit_complete=True, issue_count=0),
        _b3_event("gate", 400, audit_complete=True, released_as_audited=True),
    ])
    report = tracker.render_report(out)
    assert "DONE (complete)" in report
    assert "PID validation: json_ok=True pids_ok=True order_ok=True" in report


def test_render_report_whole_chapter_incomplete_validation_flags(tmp_path: Path):
    # Honest incomplete: wc_validated carries the failed PID flags and the
    # chunk row shows incomplete_generation, never a fabricated success.
    out = _wc_run_dir(tmp_path)
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        _wc_event("wc_retry_attempt", 500, attempt=1, reason="missing_pid"),
        _wc_event("wc_retry_attempt", 490, attempt=2, reason="missing_pid"),
        _wc_event("wc_retry_attempt", 480, attempt=3, reason="missing_pid"),
        _wc_event("wc_generation_done", 470, finish_reason="incomplete",
                  pid_count=120, duration=70.0),
        _wc_event("wc_validated", 469, json_ok=True, pids_ok=False, order_ok=False),
    ])
    report = tracker.render_report(out)
    assert "PID validation: json_ok=True pids_ok=False order_ok=False" in report
    assert "incomplete_generation" in report
    assert "GEN attempt 3/3 done finish_reason=incomplete" in report


def test_render_report_whole_chapter_is_read_only(tmp_path: Path):
    out = _wc_run_dir(tmp_path)
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        _wc_event("wc_generation_done", 480, finish_reason="complete",
                  pid_count=120, duration=70.0),
        _wc_event("wc_validated", 479, json_ok=True, pids_ok=True, order_ok=True),
    ])
    _write_ndjson(out / "audit_journal.ndjson", [
        _b3_event("audit_chunk_started", 450, chunk=1, total=8),
        _b3_event("audit_chunk_done", 440, chunk=1, total=8, status="ok"),
    ])
    before = {
        p.relative_to(out).as_posix(): (p.stat().st_mtime_ns, p.stat().st_size)
        for p in sorted(out.rglob("*")) if p.is_file()
    }
    tracker.render_report(out)
    after = {
        p.relative_to(out).as_posix(): (p.stat().st_mtime_ns, p.stat().st_size)
        for p in sorted(out.rglob("*")) if p.is_file()
    }
    assert after == before


# ---------------------------------------------------------------------------
# Book mode: whole-chapter chapter summary row
# ---------------------------------------------------------------------------


def test_chapters_table_whole_chapter_gen_status(tmp_path: Path):
    base = tmp_path / "book"
    base.mkdir()
    out = _wc_run_dir(base, with_b3=False)
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        _wc_event("wc_retry_attempt", 500, attempt=2, reason="malformed"),
    ])
    report = tracker.render_book_report(base)
    assert "-- chapters (1)" in report
    assert "gen 3/3" in report or "gen 2/3" in report
