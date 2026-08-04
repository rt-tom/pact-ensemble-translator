#!/usr/bin/env python3
"""CLI: read-only run-progress tracker for the strict chapter driver.

Backing task: ``docs/plans/V4_PHASE12_RUN_PROGRESS_TRACKER_TASK_RU.md``.
Wires the append-only ``phase_progress.ndjson`` artifact written by
``pact_v4.pipeline.v4_phase12_strict_runner.run_chapter_strict`` to a human-
readable, read-only report. This CLI never writes to ``out_dir`` and never
touches the pipeline; it is pure diagnostics.

Usage::

    python -m pact_full_pipeline_runner_v1.v4_phase_progress \
        --out-dir "D:/pact/gate_bench_runs/v4_phase12_strict_0001/run_001" \
        [--watch 10]

``--watch`` re-renders the report every N seconds. Without a
``phase_progress.ndjson`` (pre-Phase-12 runs such as ``run_001``) the tracker
falls back to a coarse inference from the artifacts that do exist
(``journal.ndjson``, ``b2_handoff.json``, ``repair_report.json``,
``strict_chapter_trial_record.json``), with the same phase/status vocabulary.

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

FRESHNESS_WINDOW_SECONDS = 300.0

TRIAL_STATES = (
    "pending", "generated", "gated", "selected", "quarantined",
    "needs_synthesis", "incomplete_generation",
)
AUDIT_STATES = ("not_started", "in_progress", "clean", "findings_present", "unit_failed", "no_candidate")
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
    alive_basis: List[str] = []
    if record is not None:
        alive = False
        alive_basis.append("strict_chapter_trial_record.json exists (run finished)")
    elif terminal is not None:
        alive = False
        alive_basis.append("terminal event written (run finished)")
    else:
        recent_logs = newest_age is not None and newest_age <= FRESHNESS_WINDOW_SECONDS
        recent_event = bool(started) and bool(_recent_event_age(events) <= FRESHNESS_WINDOW_SECONDS)
        alive = bool(recent_logs or recent_event)
        if recent_logs:
            alive_basis.append(f"server_logs newest age {newest_age:.0f}s <= {FRESHNESS_WINDOW_SECONDS:.0f}s")
        if recent_event:
            alive_basis.append(f"last progress event {_recent_event_age(events):.0f}s ago")
        if not alive_basis:
            alive_basis.append("no recent server_logs / progress events (stalled or unknown)")

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
    }


def _recent_event_age(events: List[Dict[str, Any]]) -> float:
    latest_ts = ""
    for event in events:
        ts = event.get("ts") or ""
        if ts > latest_ts:
            latest_ts = ts
    if not latest_ts:
        return float("inf")
    try:
        parsed = datetime.fromisoformat(latest_ts)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (_now() - parsed).total_seconds())
    except ValueError:
        return float("inf")


# ---------------------------------------------------------------------------
# Phase detection
# ---------------------------------------------------------------------------


def _detect_phase(out_dir: Path, events: List[Dict[str, Any]]) -> Tuple[str, str]:
    record = _read_json(out_dir / "strict_chapter_trial_record.json")
    if record is not None:
        return "done", "strict_chapter_trial_record.json exists"
    if _terminal_event(events) is not None:
        return "done", "terminal event written"

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
        return "step7", f"b2_handoff.json exists; repair_report.json absent{round_hint}"
    if total is not None and len(journal) >= total:
        return "step6", "journal full; b2_handoff.json absent"
    if total is not None:
        return "steps1-5", f"journal {len(journal)}/{total} chunks journaled"
    return "unknown", "no chunk_plan.json / journal found"


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
    # In-progress audit unit for this chunk.
    done = {(e.get("chunk_id"), e.get("detector")) for e in events if e.get("event") == "audit_unit_done"}
    for event in events:
        if event.get("event") == "audit_unit_started" and event.get("chunk_id") == chunk_id:
            if (event.get("chunk_id"), event.get("detector")) not in done:
                return "in_progress", f"audit unit started: detector={event.get('detector')}"
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
# Rendering
# ---------------------------------------------------------------------------


def render_report(out_dir: Path) -> str:
    """Read-only text report over one run directory."""
    events = _load_events(out_dir)
    fine = bool(events)
    out_dir = Path(out_dir)
    if not out_dir.is_dir():
        return f"<no such directory: {out_dir}>"

    identity = _identity(out_dir, events)
    phase, phase_basis = _detect_phase(out_dir, events)
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
    lines.append(f"Steps 1-5: journaled {len(journal_by_chunk)}/{total_chunks or 0}"
                 f" (selected={trial_counts['selected']}, quarantined={trial_counts['quarantined']}, "
                 f"needs_synthesis={trial_counts['needs_synthesis']}, "
                 f"incomplete_generation={trial_counts['incomplete_generation']})")
    if fine:
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
    lines.append(f"Step 8: formatting incidents={formatting['incidents']} blocking={formatting['blocking']}"
                 f" ({formatting['basis']}); terminal={terminal['status']} ({terminal['basis']})")

    lines.append("")
    lines.append("-- model activity --")
    if in_flight:
        for item in in_flight:
            lines.append(f"in flight: {item}")
    else:
        lines.append("no *_started without *_done (no model call currently visible)")
    log_count, newest_age = _server_log_freshness(out_dir)
    age_text = f"{newest_age:.0f}s" if newest_age is not None else "n/a"
    lines.append(f"server_logs: {log_count} file(s), newest age {age_text}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--out-dir", type=Path, required=True,
                    help="Run directory (the dir that contains phase_progress.ndjson and the run artifacts).")
    p.add_argument("--watch", type=float, default=None, metavar="SEC",
                    help="Re-render the report every SEC seconds until interrupted.")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_argparser().parse_args(argv)
    while True:
        print(render_report(args.out_dir))
        if args.watch is None or args.watch <= 0:
            break
        time.sleep(args.watch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
