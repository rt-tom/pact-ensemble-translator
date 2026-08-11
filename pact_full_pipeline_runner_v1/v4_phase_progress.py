#!/usr/bin/env python3
"""CLI: read-only run-progress tracker for the strict chapter driver.

Backing task: ``docs/plans/V4_PHASE12_RUN_PROGRESS_TRACKER_TASK_RU.md``.
Wires the append-only ``phase_progress.ndjson`` artifact written by
``pact_v4.pipeline.v4_phase12_strict_runner.run_chapter_strict`` to a human-
readable, read-only report. This CLI never writes to ``out_dir`` and never
touches the pipeline; it is pure diagnostics.

Usage::

    python -m pact_full_pipeline_runner_v1.v4_phase_progress \\
        --out-dir "D:/pact/gate_bench_runs/v4_phase12_strict_0001/run_001" \\
        [--watch 10]
    python -m pact_full_pipeline_runner_v1.v4_phase_progress \\
        --out-base "D:/pact/gate_bench_runs/v4_book_0001-0002_remote_eff-a1a2" \\
        [--watch 10]

``--watch`` re-renders the report every N seconds. Without a
``phase_progress.ndjson`` (pre-Phase-12 runs such as ``run_001``) the tracker
falls back to a coarse inference from the artifacts that do exist
(``journal.ndjson``, ``b2_handoff.json``, ``repair_report.json``,
``strict_chapter_trial_record.json``), with the same phase/status vocabulary.

``--out-base`` is the multi-chapter (book-run) mode: the monitor discovers
``chapter_*/`` subdirectories that carry ``phase_progress.ndjson`` (chapters
appear dynamically as the book run reaches them), renders a chapters header
table (chunk/journal counts, phase step, terminal status, calls and provider
cost from each chapter's ``usage.ndjson``) and then the full detail report of
the currently active chapter. ``--out-dir`` keeps the single-chapter mode.

The usage-by-step-x-model counters block reuses ``phase_for_label()`` from
``pact_full_pipeline_runner_v1.v4_usage.py`` (V4 Efficiency A1.3, already on
``main``) — the label->phase rules are never duplicated here.

Everything reported here is a *diagnostic* read: "green" progress never
implies translation quality (see ``AGENTS.md`` permanent pipeline rules).
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pact_v4.pipeline.phase_progress import PHASE_PROGRESS_FILENAME
from pact_v4.pipeline.usage_record import USAGE_FILENAME
from pact_full_pipeline_runner_v1.v4_usage import phase_for_label

FRESHNESS_WINDOW_SECONDS = 300.0

# V4.1 M (monitor card): the B3 production audit/repair stage appends its
# own crash-safe journal (``audit_journal.ndjson``, written by
# ``pact_v4.pipeline.b3_audit_repair.AuditJournal``) with fine-grained
# audit-chunk / repair-round events. The monitor reads it READ-ONLY as a
# secondary source so a whole-chapter run shows "AUDIT chunk N/8" and the
# repair round live, without any pipeline change (the B3 events were already
# emitted there — this card only surfaces them).
B3_AUDIT_JOURNAL_FILENAME = "audit_journal.ndjson"

# V4 monitor v2 (owner-approved spec, docs/plans/V4_PHASE12_..._RU.md,
# "Дизайн обновлённого монитора"): usage-label group -> step-group mapping.
# The label -> phase leg is delegated to phase_for_label() from v4_usage.py
# (A1.3) — never duplicated here; this table only maps the *phase* it
# returns to the step-group the monitor displays.
PHASE_TO_STEP_GROUP = {
    "gen": "Steps1-5",
    "qwen_fidelity": "Step2c",
    "gemma_preference": "Step2c",
    "audit": "Step6",
    "repair": "Step7",
    "formatting": "Step8",
}

# Friendly role name shown in the usage-by-step-x-model "label-group" column
# for labels whose phase has no sub-role of its own (phase2b per-chunk
# labels, phase3 audit detectors, phase5 formatting).
PHASE_FRIENDLY_ROLE = {
    "gen": "generation",
    "audit": "audit",
    "formatting": "formatting",
}

# Step-groups that carry a sub-role in the label (phase2c/phase4) keep that
# sub-role in the label-group column (e.g. "phase2c qwen_fidelity",
# "phase4 region_repair"); everything else falls back to PHASE_FRIENDLY_ROLE.
LABEL_GROUP_SUBROLE_PREFIXES = ("phase2c", "phase4")

# Display-only canonical prefix per phase (same design as
# PHASE_TO_STEP_GROUP). Lets legacy hyphen labels ("phase2c-qwen-fidelity")
# render the canonical "phase2c qwen_fidelity" label-group instead of the
# raw "phase2c-qwen-fidelity qwen_fidelity"; the label -> phase leg stays
# fully delegated to phase_for_label() from v4_usage.py, never duplicated.
PHASE_TO_LABEL_PREFIX = {
    "gen": "phase2b",
    "qwen_fidelity": "phase2c",
    "gemma_preference": "phase2c",
    "audit": "phase3",
    "repair": "phase4",
    "formatting": "phase5",
}

TRIAL_STATES = (
    "pending", "generated", "gated", "selected", "quarantined",
    "needs_synthesis", "incomplete_generation",
)
AUDIT_STATES = ("not_started", "in_progress", "done", "clean", "findings_present", "unit_failed", "no_candidate")
REPAIR_STATES = ("not_started", "in_progress", "committed", "debt")

ARTIFACT_NAMES = (
    "chunk_plan.json", "journal.ndjson", "translations.json",
    "b2_handoff.json", "repair_cache.json", "repair_report.json",
    "formatting_report.json", "strict_chapter_trial_record.json",
    "selection_meta.json", "generation_outcomes.json",
)


# ---------------------------------------------------------------------------
# Read helpers (all read-only)
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _read_ndjson(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            # Crash-safe: a partial trailing line (crash mid-write) must not
            # break the read -- skip it.
            continue
    return rows


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _server_log_freshness(out_dir: Path) -> Tuple[int, Optional[float]]:
    """Return ``(log_file_count, age_seconds_of_newest)`` for ``server_logs``."""
    logs = out_dir / "server_logs"
    if not logs.is_dir():
        return 0, None
    ages: List[float] = []
    count = 0
    for child in logs.iterdir():
        if child.is_file():
            count += 1
            try:
                ages.append((_now() - datetime.fromtimestamp(child.stat().st_mtime, timezone.utc)).total_seconds())
            except OSError:
                continue
    newest = min(ages) if ages else None
    return count, newest


def _load_events(out_dir: Path) -> List[Dict[str, Any]]:
    return _read_ndjson(out_dir / PHASE_PROGRESS_FILENAME)


def _load_b3_events(out_dir: Path) -> List[Dict[str, Any]]:
    """B3 audit/repair journal events (read-only, crash-safe).

    The B3 stage (whole-chapter production audit/repair) appends fine-grained
    ``audit_chunk_started``/``audit_chunk_done`` (chunk, total) and
    ``repair_round`` (eligible/committed/passed/debt) events to its own
    journal. Absent when B3 did not run (generation-only, chunked runs) —
    the whole-chapter status then shows generation/validation without the
    B-phase segments.
    """
    return _read_ndjson(out_dir / B3_AUDIT_JOURNAL_FILENAME)


# ---------------------------------------------------------------------------
# Identity / liveness
# ---------------------------------------------------------------------------


def _run_started_event(events: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    started = [e for e in events if e.get("event") == "run_started"]
    if not started:
        return None
    return started[-1]


def _terminal_event(events: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    terminal = [e for e in events if e.get("event") == "terminal"]
    return terminal[-1] if terminal else None


def _last_usage_record(out_dir: Path) -> Optional[Dict[str, Any]]:
    """Last ``usage.ndjson`` row (ts/label/model), or None when absent."""
    rows = _read_usage_rows(out_dir)
    return rows[-1] if rows else None


def _identity(out_dir: Path, events: List[Dict[str, Any]]) -> Dict[str, Any]:
    record = _read_json(out_dir / "strict_chapter_trial_record.json")
    started = _run_started_event(events)
    terminal = _terminal_event(events)

    started_at = (started or {}).get("started_at") or (record or {}).get("started_at") or ""
    resumed = (started or {}).get("resumed_from_index")
    if resumed is None and record is not None:
        resumed = record.get("resumed_from_index", 0)

    elapsed: Optional[float] = None
    if started_at:
        try:
            parsed = datetime.fromisoformat(started_at)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            finished_ts = None
            if record is not None and record.get("finished_at"):
                finished_ts = str(record["finished_at"])
            elif terminal is not None and terminal.get("ts"):
                # No record yet (fine mode terminal event): pin elapsed to the
                # terminal event's own timestamp so --watch archival re-views
                # do not report a growing value for a finished run.
                finished_ts = str(terminal["ts"])
            if finished_ts:
                finished = datetime.fromisoformat(finished_ts)
                if finished.tzinfo is None:
                    finished = finished.replace(tzinfo=timezone.utc)
                elapsed = (finished - parsed).total_seconds()
            else:
                elapsed = (_now() - parsed).total_seconds()
        except ValueError:
            elapsed = None

    log_count, newest_age = _server_log_freshness(out_dir)
    # V4 monitor v2 (owner observation eff-a1a2): server_logs are static
    # after server start on remote runs (opencode_serve_*.log), so they are
    # NOT a liveness indicator. Liveness comes from the last usage.ndjson
    # record (written per remote call) and the last phase_progress event.
    last_usage = _last_usage_record(out_dir)
    usage_age: Optional[float] = None
    if last_usage is not None and last_usage.get("ts"):
        usage_age = _ts_age(str(last_usage["ts"]))
    recent_usage = usage_age is not None and usage_age <= FRESHNESS_WINDOW_SECONDS
    recent_event = bool(started) and bool(_recent_event_age(events) <= FRESHNESS_WINDOW_SECONDS)

    alive_basis: List[str] = []
    if record is not None:
        alive = False
        alive_basis.append("strict_chapter_trial_record.json exists (run finished)")
    elif terminal is not None:
        alive = False
        alive_basis.append("terminal event written (run finished)")
    else:
        alive = bool(recent_usage or recent_event)
        if recent_usage:
            alive_basis.append(f"last usage.ndjson {usage_age:.0f}s ago")
        if recent_event:
            alive_basis.append(f"last progress event {_recent_event_age(events):.0f}s ago")
        if not alive_basis:
            alive_basis.append("no recent usage.ndjson / progress events (stalled or unknown)")

    return {
        "alive": alive,
        "alive_basis": "; ".join(alive_basis),
        "started_at": started_at,
        "elapsed_seconds": elapsed,
        "resumed_from_index": resumed,
        # The runner sets both identity_hash and config_identity_hash from
        # cfg.backend.identity_hash; the event carries only the former, so
        # the record fallback is belt-and-suspenders, not a divergence probe.
        "backend_identity_hash": (started or {}).get("backend_identity_hash") or (record or {}).get("backend", {}).get("identity_hash"),
        "chapter_id": (started or {}).get("chapter_id") or (record or {}).get("chapter_id"),
        "out_dir": str(out_dir),
        "server_log_count": log_count,
        "server_log_newest_age": newest_age,
        "last_usage": last_usage,
        "last_usage_age": usage_age,
    }


def _ts_age(ts: str) -> float:
    """Age in seconds of an ISO timestamp vs now (inf on parse failure)."""
    if not ts:
        return float("inf")
    try:
        parsed = datetime.fromisoformat(ts)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (_now() - parsed).total_seconds())
    except ValueError:
        return float("inf")


def _recent_event_age(events: List[Dict[str, Any]]) -> float:
    latest_ts = ""
    for event in events:
        ts = event.get("ts") or ""
        if ts > latest_ts:
            latest_ts = ts
    if not latest_ts:
        return float("inf")
    return _ts_age(latest_ts)


# ---------------------------------------------------------------------------
# Phase detection
# ---------------------------------------------------------------------------


def _detect_phase(out_dir: Path, events: List[Dict[str, Any]]) -> Tuple[str, str]:
    record = _read_json(out_dir / "strict_chapter_trial_record.json")
    if record is not None:
        return "done", "strict_chapter_trial_record.json exists"
    if _terminal_event(events) is not None:
        return "done", "terminal event written"

    # V4.1 M: whole-chapter path first — a ``wc_generation_started`` event
    # marks a whole-chapter run (one generation call per chapter), whose
    # chunked-model heuristics below (chunk_plan.json + journal) would be
    # misleading: the journal holds ONE whole_chapter entry while the plan
    # holds N chunks, and the 10-minute generation has no journal entry at
    # all yet.
    if _whole_chapter_mode(events):
        return _detect_whole_chapter_phase(out_dir, events)

    b2 = _read_json(out_dir / "b2_handoff.json")
    repair_report = _read_json(out_dir / "repair_report.json")
    chunk_plan = _read_json(out_dir / "chunk_plan.json")
    total = len((chunk_plan or {}).get("chunks", [])) if chunk_plan else None
    journal = _read_ndjson(out_dir / "journal.ndjson")

    if repair_report is not None:
        return "step8", "repair_report.json exists; final record not yet written"
    if b2 is not None:
        round_number = _current_round(events)
        round_hint = f"; round {round_number}" if round_number else ""
        region_hint = _region_progress_hint(events)
        return "step7", (f"b2_handoff.json exists; repair_report.json absent"
                         f"{round_hint}{region_hint}")
    if total is not None and len(journal) >= total:
        return "step6", "journal full; b2_handoff.json absent"
    if total is not None:
        return "steps1-5", f"journal {len(journal)}/{total} chunks journaled"
    return "unknown", "no chunk_plan.json / journal found"


def _whole_chapter_mode(events: List[Dict[str, Any]]) -> bool:
    """True when the run's progress stream is a V4.1 whole-chapter flow."""
    return any(e.get("event") == "wc_generation_started" for e in events)


def _detect_whole_chapter_phase(
    out_dir: Path, events: List[Dict[str, Any]]
) -> Tuple[str, str]:
    """Whole-chapter phase: gen -> step6 (B3 audit) -> step7 (repair) -> step8.

    ``wc_*`` events drive the generation leg; the B3 audit/repair leg comes
    from the B3 stage's own journal (``audit_journal.ndjson``) read-only —
    the audit chunk and repair-round events were already being emitted there,
    this monitor only surfaces them. A generation-only whole-chapter run (no
    B3 journal) jumps from ``gen`` straight to the record/terminal ``done``.
    """
    done = [e for e in events if e.get("event") == "wc_generation_done"]
    if not done:
        gen = _wc_gen_status(events)
        basis = (f"whole-chapter generation in flight; {gen['status']}"
                 f" (думает {gen['thinking_seconds']:.0f} сек)" if gen else
                 "whole-chapter generation started")
        return "gen", basis

    b3 = _load_b3_events(out_dir)
    if not b3:
        return "steps1-5", ("whole-chapter generation done; B3 audit/repair "
                            "not running (generation-only or awaiting stage)")

    b3_names = [e.get("event") for e in b3]
    audit_chunk_started = [e for e in b3 if e.get("event") == "audit_chunk_started"]
    audit_chunk_done = [e for e in b3 if e.get("event") == "audit_chunk_done"]
    repair_rounds = [e for e in b3 if e.get("event") == "repair_round"]
    reaudit = [e for e in b3 if e.get("event") == "reaudit_scope"]
    gate = [e for e in b3 if e.get("event") == "gate"]

    if gate:
        return "step8", ("B3 gate written; final record not yet written "
                         f"(released_as_audited={gate[-1].get('released_as_audited')})")
    if repair_rounds or reaudit:
        detail = _b3_repair_hint(b3)
        return "step7", f"B3 repair in progress{detail}"
    if audit_chunk_started or audit_chunk_done:
        total = audit_chunk_started[-1].get("total") or audit_chunk_done[-1].get("total")
        done_n = len(audit_chunk_done)
        last = (audit_chunk_started or audit_chunk_done)[-1]
        current = last.get("chunk") or done_n or len(audit_chunk_started)
        return "step6", (f"B3 audit chunk {current}/{total or '?'} "
                         f"(done={done_n})")
    if "audit_started" in b3_names or "entity_context" in b3_names:
        return "step6", "B3 audit started"
    return "step6", "whole-chapter generation done; B3 audit pending"


def _wc_gen_status(events: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Live whole-chapter generation state from ``wc_*`` events.

    Returns the current attempt (1-based), the retry budget, the last retry
    reason, and how many seconds the model has been thinking since the last
    generation event. ``None`` when no whole-chapter generation is in flight.
    """
    started = [e for e in events if e.get("event") == "wc_generation_started"]
    if not started:
        return None
    retries = [e for e in events if e.get("event") == "wc_retry_attempt"]
    done = [e for e in events if e.get("event") == "wc_generation_done"]

    last_started = started[-1]
    max_attempts = int(last_started.get("max_attempts") or 1)
    attempt = (int(retries[-1].get("attempt")) if retries else 1)
    reason = (retries[-1].get("reason") if retries else None)
    # Thinking time = age of the newest wc event (started or last retry).
    latest_ts = last_started.get("ts") or ""
    for retry in retries:
        ts = retry.get("ts") or ""
        if ts > latest_ts:
            latest_ts = ts
    thinking = _ts_age(latest_ts)

    if done:
        last_done = done[-1]
        status = (f"attempt {attempt}/{max_attempts} done "
                  f"finish_reason={last_done.get('finish_reason')}")
    else:
        status = f"attempt {attempt}/{max_attempts}"
        if reason:
            status += f" (reason: {reason})"
    return {
        "attempt": attempt,
        "max_attempts": max_attempts,
        "reason": reason,
        "thinking_seconds": thinking,
        "status": status,
        "pid_count": last_started.get("pid_count"),
        "model": last_started.get("model"),
        "reasoning_budget": last_started.get("reasoning_budget"),
        "validated": [e for e in events if e.get("event") == "wc_validated"],
    }


def _b3_repair_hint(b3: List[Dict[str, Any]]) -> str:
    """Live repair hint from the B3 journal's ``repair_round`` event."""
    for event in reversed(b3):
        if event.get("event") != "repair_round":
            continue
        committed = len(event.get("committed_pids") or [])
        passed = len(event.get("passed_pids") or [])
        debt = len(event.get("debt_trace") or [])
        hint = (f"; round {event.get('round')} "
                f"committed={committed} passed={passed} debt={debt}")
        if event.get("repair_complete") is True:
            hint += " (repair complete)"
        return hint
    return ""


def _region_progress_hint(events: List[Dict[str, Any]]) -> str:
    """Live repair progress from ``region_done``/``region_*`` events.

    ``repair_report.json`` is written only at the end of Step 7, so its
    absence does *not* mean "repair not started": the committed/debt counts
    come from the region events the ProgressTracker emits during the round
    (owner observation eff-a1a2: committed=47 debt=26 with no report yet).
    """
    region_counts = _region_counts(events)
    if not region_counts["done"] and not region_counts["in_progress"]:
        return ""
    return (f"; regions done={region_counts['done']} "
            f"committed={region_counts['committed']} "
            f"debt={region_counts['debt']} "
            f"in_progress={region_counts['in_progress']}")


def _current_round(events: List[Dict[str, Any]]) -> Optional[int]:
    round_started = [e for e in events if e.get("event") == "repair_round_started"]
    round_done = [e for e in events if e.get("event") == "repair_done"]
    if not round_started:
        return None
    if round_done:
        return None
    return int(round_started[-1].get("round_number", 0)) or None


# ---------------------------------------------------------------------------
# Per-chunk state
# ---------------------------------------------------------------------------


def _journal_by_chunk(out_dir: Path) -> Dict[str, Dict[str, Any]]:
    return {
        entry.get("chunk_id"): entry
        for entry in _read_ndjson(out_dir / "journal.ndjson")
        if entry.get("chunk_id")
    }


def _handoff_by_chunk(out_dir: Path) -> Dict[str, Dict[str, Any]]:
    payload = _read_json(out_dir / "b2_handoff.json")
    if not payload:
        return {}
    return {
        row.get("chunk_id"): row
        for row in payload.get("chunks", [])
        if row.get("chunk_id")
    }


def _repair_state_by_chunk(out_dir: Path, events: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Repair state per chunk: ``committed``/``debt``/``in_progress`` from
    ``repair_report.json`` + ``repair_cache.json`` + region events.

    A committed record anywhere wins over debt; ``in_progress`` (a
    ``region_started`` without its ``region_done``) wins over a stale
    artifact state, since the run is still writing.
    """
    states: Dict[str, Dict[str, Any]] = {}

    report = _read_json(out_dir / "repair_report.json")
    if report:
        for round_payload in report.get("rounds", []):
            for rec in round_payload.get("records", []):
                chunk_id = rec.get("chunk_id")
                if not chunk_id:
                    continue
                current = states.setdefault(chunk_id, {"committed": False, "debt": False, "records": 0})
                current["records"] += 1
                if rec.get("committed"):
                    current["committed"] = True
                else:
                    current["debt"] = True

    cache = _read_json(out_dir / "repair_cache.json")
    if cache:
        units = (cache.get("cache") or {}).get("units", [])
        for unit in units:
            rec = (unit or {}).get("record", {})
            chunk_id = rec.get("chunk_id")
            if not chunk_id:
                continue
            current = states.setdefault(chunk_id, {"committed": False, "debt": False, "records": 0})
            current["records"] += 1
            if rec.get("committed"):
                current["committed"] = True
            else:
                current["debt"] = True

    # Region events (fine mode) carry the live committed/debt state; in
    # coarse mode they are absent and the artifact reads below stand alone.
    for event in events:
        if event.get("event") == "region_done":
            chunk_id = event.get("chunk_id")
            if not chunk_id:
                continue
            current = states.setdefault(chunk_id, {"committed": False, "debt": False, "records": 0})
            current["records"] += 1
            if event.get("committed"):
                current["committed"] = True
            else:
                current["debt"] = True

    # In-progress region (started without done) wins over stale artifact state.
    region_done = set()
    for event in events:
        if event.get("event") == "region_done":
            region_done.add(event.get("repair_id"))
    for event in events:
        if event.get("event") == "region_started" and event.get("repair_id") not in region_done:
            chunk_id = event.get("chunk_id")
            if chunk_id:
                states.setdefault(chunk_id, {"committed": False, "debt": False, "records": 0})
                states[chunk_id]["in_progress"] = True

    return states


def _trial_status(chunk_id: str, journal_by_chunk: Dict[str, Any], events: List[Dict[str, Any]]) -> Tuple[str, str]:
    entry = journal_by_chunk.get(chunk_id)
    if entry is not None:
        outcome = entry.get("outcome") or "pending"
        return outcome, f"journal outcome={outcome}"
    # In-progress chunk: chunk_started without chunk_done.
    for event in reversed(events):
        if event.get("event") == "chunk_started" and event.get("chunk_id") == chunk_id:
            return "generated", "chunk_started without chunk_done"
    return "pending", "no journal entry"


def _audit_status(chunk_id: str, handoff_by_chunk: Dict[str, Any], events: List[Dict[str, Any]]) -> Tuple[str, str]:
    row = handoff_by_chunk.get(chunk_id)
    if row is not None:
        status = row.get("audit_status") or "no_candidate"
        candidate = row.get("audited_candidate_id")
        detail = f"b2_handoff audit_status={status}"
        if candidate:
            detail += f" candidate={candidate}"
        return status, detail
    # No b2_handoff row yet (Step 6 still running): derive from audit unit
    # events written by the ProgressTracker into phase_progress.ndjson.
    started = {(e.get("chunk_id"), e.get("detector")) for e in events
               if e.get("event") == "audit_unit_started" and e.get("chunk_id") == chunk_id}
    if started:
        done = {(e.get("chunk_id"), e.get("detector")) for e in events
                if e.get("event") == "audit_unit_done" and e.get("chunk_id") == chunk_id}
        if started <= done:
            detectors = ", ".join(sorted(detector for _, detector in started))
            return "done", f"audit units done: {detectors}"
        pending = next((detector for chunk, detector in sorted(started - done)), None)
        return "in_progress", f"audit unit started: detector={pending}"
    return "not_started", "no b2_handoff / audit unit event"


def _repair_status(chunk_id: str, repair_state: Dict[str, Any]) -> Tuple[str, str]:
    state = repair_state.get(chunk_id)
    if state is None:
        return "not_started", "no repair records"
    if state.get("in_progress"):
        return "in_progress", "region_started without region_done"
    if state.get("committed"):
        return "committed", f"{state['records']} record(s), at least one committed"
    if state.get("debt"):
        return "debt", f"{state['records']} record(s), none committed"
    return "not_started", "no repair records"


def _chunk_table(out_dir: Path, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    chunk_plan = _read_json(out_dir / "chunk_plan.json")
    journal_by_chunk = _journal_by_chunk(out_dir)
    handoff_by_chunk = _handoff_by_chunk(out_dir)
    repair_state = _repair_state_by_chunk(out_dir, events)

    # V4.1 whole-chapter mode: ONE processing unit (whole_chapter), not the
    # planner's N chunks — the journal holds a single whole_chapter entry and
    # the plan rows would all read "pending". Show the unit's own status from
    # the wc_*/B3 events instead.
    if _whole_chapter_mode(events):
        return [_whole_chapter_chunk_row(out_dir, events)]

    chunk_ids = [row.get("chunk_id") for row in (chunk_plan or {}).get("chunks", []) if row.get("chunk_id")]
    rows: List[Dict[str, Any]] = []
    for chunk_id in chunk_ids:
        trial, trial_basis = _trial_status(chunk_id, journal_by_chunk, events)
        audit, audit_basis = _audit_status(chunk_id, handoff_by_chunk, events)
        repair, repair_basis = _repair_status(chunk_id, repair_state)
        rows.append({
            "chunk_id": chunk_id,
            "trial": trial,
            "trial_basis": trial_basis,
            "audit": audit,
            "audit_basis": audit_basis,
            "repair": repair,
            "repair_basis": repair_basis,
        })
    return rows


def _whole_chapter_chunk_row(out_dir: Path, events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Single-row chunk table for whole-chapter runs (generation -> B3)."""
    gen = _wc_gen_status(events)
    if gen is None:
        return {
            "chunk_id": "whole_chapter",
            "trial": "pending", "trial_basis": "no wc_generation_started event",
            "audit": "not_started", "audit_basis": "no B3 audit events",
            "repair": "not_started", "repair_basis": "no B3 repair events",
        }
    if gen["validated"]:
        flags = gen["validated"][-1]
        trial = ("generated" if flags.get("json_ok") and flags.get("pids_ok")
                 and flags.get("order_ok") else "incomplete_generation")
        trial_basis = (f"wc_validated json_ok={flags.get('json_ok')} "
                       f"pids_ok={flags.get('pids_ok')} order_ok={flags.get('order_ok')}")
    else:
        trial = "generating" if not any(
            e.get("event") == "wc_generation_done" for e in events
        ) else "generated"
        trial_basis = "wc_generation in flight / done (no wc_validated yet)"

    b3 = _load_b3_events(out_dir)
    if not b3:
        return {
            "chunk_id": "whole_chapter",
            "trial": trial, "trial_basis": trial_basis,
            "audit": "not_started", "audit_basis": "no B3 audit journal",
            "repair": "not_started", "repair_basis": "no B3 audit journal",
        }
    audit_started = [e for e in b3 if e.get("event") == "audit_chunk_started"]
    audit_done = [e for e in b3 if e.get("event") == "audit_chunk_done"]
    audit = ("done" if audit_started and len(audit_done) >= len(audit_started)
             else ("in_progress" if audit_started else "not_started"))
    audit_total = (
        (audit_done or audit_started)[-1].get("total")
        or len(audit_started)
    )
    audit_basis = f"B3 audit chunks done={len(audit_done)}/{audit_total}"

    repair_rounds = [e for e in b3 if e.get("event") == "repair_round"]
    if repair_rounds and repair_rounds[-1].get("repair_complete") is True:
        repair = "committed"
    elif repair_rounds:
        repair = "debt" if repair_rounds[-1].get("debt_trace") else "in_progress"
    else:
        repair = "not_started"
    repair_basis = (_b3_repair_hint(b3) or "no repair_round event yet").lstrip("; ")
    return {
        "chunk_id": "whole_chapter",
        "trial": trial, "trial_basis": trial_basis,
        "audit": audit, "audit_basis": audit_basis,
        "repair": repair, "repair_basis": repair_basis,
    }


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------


def _trial_counts(journal_by_chunk: Dict[str, Any]) -> Dict[str, int]:
    counts = {state: 0 for state in TRIAL_STATES}
    for entry in journal_by_chunk.values():
        outcome = entry.get("outcome")
        if outcome in counts:
            counts[outcome] += 1
    return counts


def _audit_unit_counts(events: List[Dict[str, Any]], total_chunks: int) -> Dict[str, int]:
    started = {(e.get("chunk_id"), e.get("detector")) for e in events if e.get("event") == "audit_unit_started"}
    done = {(e.get("chunk_id"), e.get("detector")) for e in events if e.get("event") == "audit_unit_done"}
    return {
        "expected": 2 * total_chunks,
        "started": len(started),
        "done": len(done),
    }


def _region_counts(events: List[Dict[str, Any]]) -> Dict[str, int]:
    started = [e for e in events if e.get("event") == "region_started"]
    done = [e for e in events if e.get("event") == "region_done"]
    committed = sum(1 for e in done if e.get("committed"))
    debt = len(done) - committed
    return {
        "planned": len(started),
        "done": len(done),
        "committed": committed,
        "debt": debt,
        "in_progress": len(started) - len(done),
    }


def _reaudit_counts(events: List[Dict[str, Any]]) -> Dict[str, int]:
    started = {(e.get("chunk_id"), e.get("detector")) for e in events if e.get("event") == "reaudit_unit_started"}
    done = {(e.get("chunk_id"), e.get("detector")) for e in events if e.get("event") == "reaudit_unit_done"}
    return {"started": len(started), "done": len(done)}


def _formatting_counts(out_dir: Path, events: List[Dict[str, Any]]) -> Dict[str, Any]:
    fmt_events = [e for e in events if e.get("event") == "formatting_done"]
    if fmt_events:
        last = fmt_events[-1]
        return {
            "incidents": last.get("incidents"),
            "blocking": last.get("blocking"),
            "basis": "formatting_done event",
        }
    report = _read_json(out_dir / "formatting_report.json")
    if report and isinstance(report.get("outcome"), dict):
        outcome = report["outcome"]
        return {
            "incidents": outcome.get("incident_count"),
            "blocking": outcome.get("blocking"),
            "basis": "formatting_report.json",
        }
    return {"incidents": None, "blocking": None, "basis": "no formatting artifacts"}


def _terminal_counts(out_dir: Path, events: List[Dict[str, Any]]) -> Dict[str, Any]:
    terminal = _terminal_event(events)
    if terminal is not None:
        return {"status": terminal.get("status"), "basis": "terminal event"}
    record = _read_json(out_dir / "strict_chapter_trial_record.json")
    if record is not None:
        return {"status": record.get("step8", {}).get("status"), "basis": "strict_chapter_trial_record.json"}
    report = _read_json(out_dir / "repair_report.json")
    if report is not None:
        return {"status": report.get("status"), "basis": "repair_report.json status"}
    return {"status": None, "basis": "no terminal artifact yet"}


def _in_flight_model_activity(events: List[Dict[str, Any]]) -> List[str]:
    """Current ``*_started`` without its ``*_done``, the "now on X" signal."""
    in_flight: List[str] = []

    # V4.1 whole-chapter generation: wc_generation_started without
    # wc_generation_done is the single in-flight model call.
    if _whole_chapter_mode(events) and not any(
        e.get("event") == "wc_generation_done" for e in events
    ):
        gen = _wc_gen_status(events)
        if gen is not None:
            in_flight.append(
                f"whole-chapter generation ({gen['status']}, "
                f"думает {gen['thinking_seconds']:.0f} сек)"
            )

    chunk_done = {e.get("chunk_id") for e in events if e.get("event") == "chunk_done"}
    for event in events:
        if event.get("event") == "chunk_started" and event.get("chunk_id") not in chunk_done:
            in_flight.append(f"chunk {event.get('chunk_id')} (Steps 1-5)")

    audit_done = {(e.get("chunk_id"), e.get("detector")) for e in events if e.get("event") == "audit_unit_done"}
    for event in events:
        if event.get("event") == "audit_unit_started" and (event.get("chunk_id"), event.get("detector")) not in audit_done:
            in_flight.append(f"audit {event.get('chunk_id')}:{event.get('detector')}")

    region_done = {e.get("repair_id") for e in events if e.get("event") == "region_done"}
    for event in events:
        if event.get("event") == "region_started" and event.get("repair_id") not in region_done:
            in_flight.append(f"region {event.get('repair_id')} ({event.get('chunk_id')})")

    reaudit_done = {(e.get("chunk_id"), e.get("detector")) for e in events if e.get("event") == "reaudit_unit_done"}
    for event in events:
        if event.get("event") == "reaudit_unit_started" and (event.get("chunk_id"), event.get("detector")) not in reaudit_done:
            in_flight.append(f"reaudit {event.get('chunk_id')}:{event.get('detector')}")

    return in_flight


# ---------------------------------------------------------------------------
# Usage by step x model (V4 monitor v2)
# ---------------------------------------------------------------------------


def _read_usage_rows(out_dir: Path) -> List[Dict[str, Any]]:
    """Parse ``usage.ndjson`` rows (crash-safe, same reader as v4_usage)."""
    return _read_ndjson(out_dir / USAGE_FILENAME)


def _label_group(label: Optional[str]) -> str:
    """Human label-group for the usage-by-step-x-model column.

    ``phase2b/...`` -> ``phase2b generation``, ``phase3/...`` -> ``phase3
    audit``, ``phase5/...`` -> ``phase5 formatting`` (friendly phase role);
    ``phase2c/<sub>`` and ``phase4/<sub>`` keep their label sub-role
    (``phase2c qwen_fidelity``, ``phase4 region_repair``,
    ``phase4 region_fidelity_gate``). Uses ``phase_for_label()`` for the
    phase leg — the same label->phase rules as v4_usage, never duplicated.
    """
    parts = (label or "").split("/")
    phase = phase_for_label(label)
    # Canonical prefix from the phase (handles legacy hyphen labels such as
    # "phase2c-qwen-fidelity" -> "phase2c"); fall back to the raw token for
    # labels phase_for_label does not recognize.
    prefix = PHASE_TO_LABEL_PREFIX.get(phase, parts[0] if parts and parts[0] else "(unknown)")
    if prefix in LABEL_GROUP_SUBROLE_PREFIXES and len(parts) > 1 and parts[1]:
        role = parts[1]
    else:
        role = PHASE_FRIENDLY_ROLE.get(phase, phase)
    return f"{prefix} {role}"


def _usage_group_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Aggregate usage rows into per-(label-group, model) buckets.

    Each bucket carries the step-group (from ``PHASE_TO_STEP_GROUP`` via
    ``phase_for_label``), the label-group, the model, calls, summed
    input/output/reasoning/cached tokens and summed ``reported_cost``.
    """
    buckets: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        label = row.get("label")
        phase = phase_for_label(label)
        group = _label_group(label)
        # The spec table shows the short model name (``qwen3.7-plus``,
        # ``deepseek-v4-flash``); fall back to the full model_ref.
        model = row.get("model") or row.get("model_ref") or "(unknown)"
        key = (group, str(model))
        bucket = buckets.setdefault(key, {
            "step": PHASE_TO_STEP_GROUP.get(phase, phase),
            "label_group": group,
            "model": model,
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "cached_input_tokens": 0,
            "cached_write_tokens": 0,
            "reported_cost": 0.0,
            "has_cost": False,
        })
        bucket["calls"] += 1
        for token_key in ("input_tokens", "output_tokens", "reasoning_tokens",
                          "cached_input_tokens", "cached_write_tokens"):
            value = row.get(token_key)
            if value is not None:
                bucket[token_key] += int(value)
        cost = row.get("reported_cost")
        if cost is not None:
            bucket["reported_cost"] += float(cost)
            bucket["has_cost"] = True
    return list(buckets.values())


def _usage_totals(groups: List[Dict[str, Any]]) -> Dict[str, Any]:
    total_calls = sum(g["calls"] for g in groups)
    total_cost = sum(g["reported_cost"] for g in groups if g["has_cost"])
    return {
        "calls": total_calls,
        "reported_cost": total_cost,
        "any_cost": any(g["has_cost"] for g in groups),
    }


def _fmt_tokens(value: int) -> str:
    """Compact token display: ``108`` stays ``108``, ``42100`` -> ``42.1k``."""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def _fmt_cost(value: float) -> str:
    return f"${value:.4f}"


def _fmt_cost_short(value: float) -> str:
    """Header-table cost (2 decimals, e.g. ``$1.11`` per the spec sketch)."""
    return f"${value:.2f}"


_SHORT_STATUS = {
    "accepted_degraded": "accepted_degr.",
    "complete": "complete",
    "failed": "failed",
}


def _usage_block_lines(out_dir: Path) -> List[str]:
    """``-- usage by step x model --`` block (from usage.ndjson)."""
    rows = _read_usage_rows(out_dir)
    if not rows:
        return ["-- usage by step x model --", "  (no usage.ndjson yet)"]
    groups = _usage_group_rows(rows)
    totals = _usage_totals(groups)
    show_cost = totals["any_cost"] and totals["reported_cost"] != 0.0
    show_reasoning = any(g["reasoning_tokens"] for g in groups)
    show_cached = any(
        g["cached_input_tokens"] or g["cached_write_tokens"] for g in groups
    )

    lines = ["-- usage by step x model (из usage.ndjson) --"]
    header = (f"{'step':<9} {'label-group':<25} {'model':<18}"
              f"{'calls':>6}{'input':>9}{'output':>9}")
    if show_reasoning:
        header += f"{'reasoning':>10}"
    if show_cached:
        header += f"{'cached':>9}"
    if show_cost:
        header += f"{'cost':>11}"
    lines.append(header)

    step_order = {"Steps1-5": 0, "Step2c": 1, "Step6": 2, "Step7": 3, "Step8": 4}
    for g in sorted(
        groups,
        key=lambda g: (step_order.get(g["step"], 99), g["label_group"], g["model"]),
    ):
        row = (f"{g['step']:<9} {g['label_group']:<25} {str(g['model'])[:18]:<18}"
               f"{g['calls']:>6}{_fmt_tokens(g['input_tokens']):>9}"
               f"{_fmt_tokens(g['output_tokens']):>9}")
        if show_reasoning:
            row += f"{_fmt_tokens(g['reasoning_tokens']):>10}"
        if show_cached:
            row += f"{_fmt_tokens(g['cached_input_tokens'] + g['cached_write_tokens']):>9}"
        if show_cost:
            row += f"{_fmt_cost(g['reported_cost']):>11}"
        lines.append(row)

    total_row = (f"{'TOTAL':<9} {'':<25} {'':<18}"
                 f"{totals['calls']:>6}{'~':>9}{'~':>9}")
    if show_reasoning:
        total_row += f"{'~':>10}"
    if show_cached:
        total_row += f"{'~':>9}"
    if show_cost:
        total_row += f"{_fmt_cost(totals['reported_cost']):>11}"
    lines.append(total_row)
    return lines


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _status_line(out_dir: Path, events: List[Dict[str, Any]], phase: str) -> str:
    """Compact one-line status (V4.1 M card)::

        [0001] gen | GEN attempt 2/3 (reason: malformed) | AUDIT chunk 3/8 |
        REPAIR regions done=2 committed=1 debt=1 | DONE

    Segments appear only when the corresponding events exist; ``DONE`` only
    at a terminal state. Works for both whole-chapter (wc_* + B3 journal)
    and chunked runs (region events), so the book-run chapters table and the
    single-chapter report share one vocabulary.
    """
    started = _run_started_event(events)
    chapter = (started or {}).get("chapter_id") or out_dir.name
    segments = [f"[{chapter}] {phase}"]

    gen = _wc_gen_status(events)
    if gen is not None:
        segments.append(f"GEN {gen['status']}")
    elif phase not in ("gen", "steps1-5", "unknown"):
        # Chunked runs: show the chunked generation progress (journal) as the
        # GEN segment so the status line stays meaningful outside whole-chapter.
        journal = _read_ndjson(out_dir / "journal.ndjson")
        chunk_plan = _read_json(out_dir / "chunk_plan.json")
        total = len((chunk_plan or {}).get("chunks", [])) if chunk_plan else 0
        if total:
            segments.append(f"GEN chunks {len(journal)}/{total}")

    b3 = _load_b3_events(out_dir)
    audit_chunks = [e for e in b3 if e.get("event") in ("audit_chunk_started", "audit_chunk_done")]
    if audit_chunks:
        last = audit_chunks[-1]
        total = last.get("total")
        # The card's "AUDIT chunk N/8" is the CURRENT chunk number, i.e. the
        # newest event's chunk (the one being processed / just finished), not
        # a count of events.
        current = last.get("chunk") or sum(
            1 for e in audit_chunks if e.get("event") == "audit_chunk_started"
        )
        segments.append(f"AUDIT chunk {current}/{total or '?'}")

    region_counts = _region_counts(events)
    repair_hint = _b3_repair_hint(b3)
    if repair_hint or region_counts["done"]:
        if repair_hint:
            segments.append(f"REPAIR {repair_hint.lstrip('; ')}")
        else:
            segments.append(
                f"REPAIR regions done={region_counts['done']} "
                f"committed={region_counts['committed']} "
                f"debt={region_counts['debt']}"
            )

    if phase == "done":
        terminal = _terminal_counts(out_dir, events)
        segments.append(f"DONE ({terminal['status']})" if terminal["status"] else "DONE")
    return " | ".join(segments)


def render_report(out_dir: Path) -> str:
    """Read-only text report over one run directory."""
    events = _load_events(out_dir)
    fine = bool(events)
    out_dir = Path(out_dir)
    if not out_dir.is_dir():
        return f"<no such directory: {out_dir}>"

    identity = _identity(out_dir, events)
    phase, phase_basis = _detect_phase(out_dir, events)
    wc_mode = _whole_chapter_mode(events)
    rows = _chunk_table(out_dir, events)
    chunk_plan = _read_json(out_dir / "chunk_plan.json")
    total_chunks = len((chunk_plan or {}).get("chunks", [])) if chunk_plan else 0
    journal_by_chunk = _journal_by_chunk(out_dir)
    trial_counts = _trial_counts(journal_by_chunk)
    audit_counts = _audit_unit_counts(events, total_chunks) if fine else {}
    region_counts = _region_counts(events)
    reaudit_counts = _reaudit_counts(events)
    formatting = _formatting_counts(out_dir, events)
    terminal = _terminal_counts(out_dir, events)
    in_flight = _in_flight_model_activity(events)
    gen = _wc_gen_status(events) if wc_mode else None
    b3_events = _load_b3_events(out_dir)

    lines: List[str] = []
    lines.append(f"== V4 run progress: {out_dir} ==")
    lines.append(f"mode: {'fine (phase_progress.ndjson)' if fine else 'coarse (artifact inference)'}")
    lines.append(f"alive: {'yes' if identity['alive'] else 'no'} -- {identity['alive_basis']}")
    if identity["started_at"]:
        lines.append(f"started_at: {identity['started_at']}")
    if identity["elapsed_seconds"] is not None:
        lines.append(f"elapsed: {identity['elapsed_seconds']:.0f}s")
    if identity["resumed_from_index"] is not None:
        lines.append(f"resumed_from_index: {identity['resumed_from_index']}")
    lines.append(f"status: {_status_line(out_dir, events, phase)}")
    lines.append(f"phase: {phase} -- {phase_basis}")

    lines.append("")
    lines.append("-- chunks (trial -> audit -> repair) --")
    if rows:
        lines.append(f"{'chunk_id':<18} {'trial':<22} {'audit':<18} {'repair':<12}")
        for row in rows:
            lines.append(
                f"{row['chunk_id']:<18} {row['trial']:<22} {row['audit']:<18} {row['repair']:<12}"
            )
    else:
        lines.append("(no chunk_plan.json / no chunks)")

    lines.append("")
    lines.append("-- counters --")
    if wc_mode and gen is not None:
        lines.append(
            f"GEN: attempt {gen['attempt']}/{gen['max_attempts']} "
            f"pid_count={gen['pid_count']} reasoning_budget={gen['reasoning_budget']} "
            f"model={gen['model']}"
        )
        if gen["validated"]:
            flags = gen["validated"][-1]
            lines.append(
                f"PID validation: json_ok={flags.get('json_ok')} "
                f"pids_ok={flags.get('pids_ok')} order_ok={flags.get('order_ok')} "
                "(wc_validated)"
            )
        else:
            lines.append("PID validation: pending (wc_validated not written yet)")
    else:
        lines.append(f"Steps 1-5: journaled {len(journal_by_chunk)}/{total_chunks or 0}"
                     f" (selected={trial_counts['selected']}, quarantined={trial_counts['quarantined']}, "
                     f"needs_synthesis={trial_counts['needs_synthesis']}, "
                     f"incomplete_generation={trial_counts['incomplete_generation']})")
    if wc_mode:
        b3_audit_chunks = [e for e in b3_events
                           if e.get("event") in ("audit_chunk_started", "audit_chunk_done")]
        if b3_audit_chunks:
            total = b3_audit_chunks[-1].get("total")
            started_n = sum(1 for e in b3_audit_chunks if e.get("event") == "audit_chunk_started")
            done_n = sum(1 for e in b3_audit_chunks if e.get("event") == "audit_chunk_done")
            lines.append(f"Step 6 (B3): audit chunks started={started_n}/{total or '?'} "
                         f"done={done_n} (audit_journal.ndjson)")
        else:
            lines.append("Step 6 (B3): no audit chunk events yet")
        repair_hint = _b3_repair_hint(b3_events)
        if repair_hint:
            lines.append(f"Step 7 (B3): {repair_hint.lstrip('; ')}")
        else:
            lines.append("Step 7 (B3): repair not started")
    elif fine:
        lines.append(
            f"Step 6: audit units done={audit_counts['done']}/{audit_counts['expected']} "
            f"(started={audit_counts['started']})"
        )
        lines.append(
            f"Step 7: regions planned={region_counts['planned']} done={region_counts['done']} "
            f"committed={region_counts['committed']} debt={region_counts['debt']} "
            f"in_progress={region_counts['in_progress']}; "
            f"re-audit units done={reaudit_counts['done']}/{reaudit_counts['started']}"
        )
    else:
        lines.append("Step 6/7 detail: not available (coarse mode, no phase_progress.ndjson)")
    # V4 monitor v2 (owner observation eff-a1a2): before Step 8 starts the
    # old text read as "formatting incidents=None ... terminal=None" — a
    # broken-looking Step 8 block. Show an explicit "not started" instead.
    if phase in ("steps1-5", "step6", "step7", "unknown", "gen"):
        lines.append("Step 8: not started (ожидание formatting/terminal)")
    else:
        lines.append(f"Step 8: formatting incidents={formatting['incidents']} blocking={formatting['blocking']}"
                     f" ({formatting['basis']}); terminal={terminal['status']} ({terminal['basis']})")
    lines.append("")
    lines.extend(_usage_block_lines(out_dir))

    lines.append("")
    lines.append("-- model activity --")
    # V4 monitor v2: primary liveness is the last usage.ndjson record
    # (ts/label/model) plus the last phase_progress event; server_logs are
    # shown separately as age since server start (static on remote runs).
    last_usage = identity["last_usage"]
    if last_usage is not None:
        age_text = f" ({identity['last_usage_age']:.0f}s ago)" if identity["last_usage_age"] is not None else ""
        lines.append(
            f"last usage.ndjson: {last_usage.get('ts')} label={last_usage.get('label')} "
            f"model={last_usage.get('model_ref')}{age_text}"
        )
    if in_flight:
        for item in in_flight:
            lines.append(f"in flight: {item}")
    if not last_usage and not in_flight:
        lines.append("no usage.ndjson record and no *_started without *_done")
    log_count, newest_age = _server_log_freshness(out_dir)
    age_text = f"{newest_age:.0f}s" if newest_age is not None else "n/a"
    lines.append(f"server_logs: {log_count} file(s), age since server start {age_text}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Multi-chapter (book-run) mode: --out-base
# ---------------------------------------------------------------------------


def _discover_chapters(out_base: Path) -> List[Path]:
    """``chapter_*/`` subdirs of ``out_base`` carrying phase_progress.ndjson.

    Chapters appear dynamically: book_run creates each chapter dir in order,
    so a re-render (--watch) picks up a newly started chapter on its own.
    """
    out_base = Path(out_base)
    if not out_base.is_dir():
        return []
    chapters: List[Path] = []
    for child in sorted(out_base.iterdir()):
        if child.is_dir() and child.name.startswith("chapter_"):
            if (child / PHASE_PROGRESS_FILENAME).exists():
                chapters.append(child)
    return chapters


def _chapter_summary_row(chapter_dir: Path) -> Dict[str, Any]:
    """One row of the chapters table: id, chunks, step, status, calls, cost."""
    events = _load_events(chapter_dir)
    phase, _ = _detect_phase(chapter_dir, events)

    chunk_plan = _read_json(chapter_dir / "chunk_plan.json")
    total = len((chunk_plan or {}).get("chunks", [])) if chunk_plan else 0
    journal = _journal_by_chunk(chapter_dir)

    usage = _read_usage_rows(chapter_dir)
    calls = len(usage)
    costs = [float(row["reported_cost"]) for row in usage
             if row.get("reported_cost") is not None]
    cost = sum(costs)

    if phase == "done":
        step = "8"
        terminal = _terminal_counts(chapter_dir, events)
        status = terminal["status"] or "done"
    elif phase == "step8":
        step = "8"
        status = "step8"
    elif phase == "step7":
        step = "7"
        round_number = _current_round(events)
        status = f"repair r{round_number}" if round_number else "step7"
    elif phase == "step6":
        step = "6"
        status = "step6"
    elif phase == "gen":
        step = "1-5"
        gen = _wc_gen_status(events)
        status = f"gen {gen['attempt']}/{gen['max_attempts']}" if gen else "gen"
    elif phase == "steps1-5":
        step = "1-5"
        status = "steps1-5"
    else:
        step = "?"
        status = phase

    return {
        "chapter_id": chapter_dir.name,
        "chunks": f"{len(journal)}/{total}",
        "step": step,
        "status": status,
        "calls": calls,
        "cost": cost,
        # Present only when at least one usage row actually reported a
        # non-zero provider cost: all 0/None -> hide the column gracefully
        # (spec), not "$0.00" noise.
        "cost_present": any(c != 0 for c in costs),
        "events": events,
    }


def _render_chapters_table(chapters: List[Path]) -> List[str]:
    rows = [_chapter_summary_row(ch) for ch in chapters]
    total_calls = sum(r["calls"] for r in rows)
    total_cost = sum(r["cost"] for r in rows)
    # Cost column only when at least one chapter actually reported a cost
    # (all 0/None -> hide gracefully, no "$0.00" noise).
    show_cost = any(r["cost_present"] for r in rows)

    lines = [f"-- chapters ({len(chapters)}) {'-' * 56}"]
    header = (f"{'chapter_id':<24} {'chunks':>7} {'step':>5} {'status':<15}"
              f"{'calls':>7}")
    if show_cost:
        header += f" {'cost(prov.)':>10}"
    lines.append(header)
    for row in rows:
        status = _SHORT_STATUS.get(row["status"], row["status"])[:15]
        line = (f"{row['chapter_id']:<24} {row['chunks']:>7} {row['step']:>5}"
                f" {status:<15}{row['calls']:>7}")
        if show_cost:
            line += f" {_fmt_cost_short(row['cost']):>10}"
        lines.append(line)
    total_line = (f"{'TOTAL':<24} {'':>7} {'':>5} {'':<15}{total_calls:>7}")
    if show_cost:
        total_line += f" {_fmt_cost_short(total_cost):>10}"
    lines.append(total_line)
    return lines


def _active_chapter(chapters: List[Path]) -> Optional[Path]:
    """The chapter being processed now: the newest activity among those
    that have not reached a terminal state; fall back to the newest overall.
    """
    def _activity_ts(chapter_dir: Path) -> str:
        events = _load_events(chapter_dir)
        latest = ""
        for event in events:
            ts = event.get("ts") or ""
            if ts > latest:
                latest = ts
        usage = _read_usage_rows(chapter_dir)
        for row in usage:
            ts = row.get("ts") or ""
            if ts > latest:
                latest = ts
        return latest

    def _not_terminal(chapter_dir: Path) -> bool:
        events = _load_events(chapter_dir)
        record = _read_json(chapter_dir / "strict_chapter_trial_record.json")
        return record is None and _terminal_event(events) is None

    active = [c for c in chapters if _not_terminal(c)]
    if not active:
        active = chapters
    return max(active, key=_activity_ts) if active else None


def render_book_report(out_base: Path) -> str:
    """Read-only multi-chapter (book-run) report over ``--out-base``."""
    out_base = Path(out_base)
    if not out_base.is_dir():
        return f"<no such directory: {out_base}>"

    chapters = _discover_chapters(out_base)
    lines: List[str] = [f"== V4 book progress: {out_base} =="]
    if not chapters:
        lines.append("(no chapter_*/ with phase_progress.ndjson found yet)")
        return "\n".join(lines)

    lines.extend(_render_chapters_table(chapters))

    active = _active_chapter(chapters)
    if active is not None:
        lines.append("")
        lines.append(f"-- active chapter: {active.name} --")
        lines.append(render_report(active))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--out-dir", type=Path, default=None,
                        help="Run directory (contains phase_progress.ndjson and the run artifacts).")
    target.add_argument("--out-base", type=Path, default=None,
                        help="Book-run directory (contains chapter_*/ with phase_progress.ndjson).")
    p.add_argument("--watch", type=float, default=None, metavar="SEC",
                    help="Re-render the report every SEC seconds until interrupted.")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_argparser().parse_args(argv)
    while True:
        if args.out_base is not None:
            print(render_book_report(args.out_base))
        else:
            print(render_report(args.out_dir))
        if args.watch is None or args.watch <= 0:
            break
        time.sleep(args.watch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
