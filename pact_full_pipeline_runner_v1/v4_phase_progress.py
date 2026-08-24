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
table (chunk/journal counts, phase step, terminal status, calls, per-chapter
input/output/reasoning token sums and provider cost from each chapter's
``usage.ndjson``) and then the full detail report of the currently active
chapter. ``--out-dir`` keeps the single-chapter mode.

MONITOR-V2 additions (backing cards ``V4_BOOK_RUN_MONITOR_TASK_RU.md`` and
the monitor-v2 spec):

* ``-- Phase --`` block between the alive header and the usage block:
  per-pipeline-phase progress (Extraction / Translation / R_editor / Audit /
  Repair / Re-audit) read-only from the B3 artifacts
  (``entity_context_cache.json``, ``translations_raw.json``,
  ``audit_cache_b3.json``);
* ``-- скорость генерации (локальная, из server_logs) --`` block after the
  Phase block, LOCAL runs only: llama-server ``slot print_timing`` eval /
  prompt / live ``tg_3s`` tokens-per-second from the newest local
  ``Gemma_*``/``Qwen_*`` stderr log, plus ``live думание`` growth of the
  freshest ``*_reasoning.txt`` trace (B/s). Remote runs never render a
  speed block;
* the usage-by-step-x-model "label-group" column is renamed to ``фазы`` and
  shows the same human phase names as the Phase block (the label -> phase
  leg still reuses ``phase_for_label()`` — never duplicated);
* ``-- последний вызов (из usage.ndjson) --`` block after the usage block:
  the human phase, model, in/out/reasoning tokens and wall seconds of the
  most recent completed call.

The usage-by-step-x-model counters block reuses ``phase_for_label()`` from
``pact_full_pipeline_runner_v1.v4_usage.py`` (V4 Efficiency A1.3, already on
``main``) — the label->phase rules are never duplicated here.

Everything reported here is a *diagnostic* read: "green" progress never
implies translation quality (see ``AGENTS.md`` permanent pipeline rules).
"""
from __future__ import annotations

import argparse
import json
import re
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

# MONITOR-V2 (1.3): the usage-by-step-x-model "label-group" column is
# renamed to "фазы" and shows the same human phase names as the Phase block.
# MONITOR-V2 whole-chapter vocabulary: all seven user-facing phase names are
# the canonical single source of truth (no second mapping table).
PHASE_HUMAN_NAME = {
    "extraction": "Entity extraction",
    "gen": "Whole-chapter translation",
    "r_editor": "R-editor",
    "audit": "Chapter audit",
    "repair": "Selective repair",
    "reaudit": "Re-audit scope",
    "formatting": "Formatting",
    "qwen_fidelity": "qwen_fidelity",
    "gemma_preference": "gemma_preference",
    "(other)": "(other)",
}

# MONITOR-V2 finding 6: PHASE_TO_STEP_GROUP is derived from PHASE_HUMAN_NAME
# (the single source of truth) — never hard-codes duplicate display strings.
PHASE_TO_STEP_GROUP = {k: v for k, v in PHASE_HUMAN_NAME.items()
                        if k != "(other)"}

# MONITOR-V2 finding 5: internal phase codes -> canonical user-facing names.
# Used by render_report (phase:/status: lines) and _chapter_summary_row
# (step/status columns) so that no raw Step N / GEN / trial labels appear
# in normal output.
_PHASE_DISPLAY = {
    "gen": "Whole-chapter translation",
    "steps1-5": "Entity extraction",
    "step6": "Chapter audit",
    "step7": "Selective repair",
    "step8": "Formatting",
    "done": "Complete",
    "unknown": "Unknown",
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
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            # Crash-safe: a partial trailing line (crash mid-write) must not
            # break the read -- skip it.
            continue
        # MEDIUM (RV t_c9f9ea90): a structurally valid JSON line that is not
        # an object (e.g. ``[1]``) must not reach a caller that does
        # ``row.get(...)`` — skip it, the artifact stays diagnostic.
        if isinstance(row, dict):
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Snapshot (v41-runtime-efficiency 2.1): one read per render cycle
# ---------------------------------------------------------------------------

def _read_snapshot(out_dir: Path, incremental: bool = False) -> Dict[str, Any]:
    """Read the snapshot of the run artifacts for one render cycle.

    One pass per render instead of repeated ``read_json``/``read_ndjson``
    for ``chunk_plan``/``journal``/``usage``/audit cache/events. The dict
    is threaded through render helpers so the same file is never read
    twice within a single ``render_report`` / ``render_book_report`` cycle.
    A fresh snapshot is built on every ``render`` call so ``--watch`` never
    shows stale data.
    When ``incremental`` is True (watch mode) NDJSON files are read via
    ``_read_ndjson_incremental`` so large ``phase_progress.ndjson`` /
    ``usage.ndjson`` tails do not re-read the whole file each poll; if
    the file was truncated the incremental cache falls back to a full read.
    """
    out_dir = Path(out_dir)
    _ndjson = _read_ndjson_incremental if incremental else _read_ndjson
    return {
        "chunk_plan": _read_json(out_dir / "chunk_plan.json"),
        "journal": _read_ndjson(out_dir / "journal.ndjson"),
        "usage": _ndjson(out_dir / USAGE_FILENAME),
        "events": _ndjson(out_dir / PHASE_PROGRESS_FILENAME),
        "b3_events": _ndjson(out_dir / B3_AUDIT_JOURNAL_FILENAME),
        "audit_cache": _read_audit_cache(out_dir),
        "b2_handoff": _read_json(out_dir / "b2_handoff.json"),
        "repair_report": _read_json(out_dir / "repair_report.json"),
        "strict_record": _read_json(out_dir / "strict_chapter_trial_record.json"),
        "entity_context": _read_json(out_dir / "entity_context_cache.json"),
        "translations_raw": _read_json(out_dir / "translations_raw.json"),
        "formatting_report": _read_json(out_dir / "formatting_report.json"),
    }


# ---------------------------------------------------------------------------
# Incremental NDJSON cache for watch mode (v41-runtime-efficiency 5.1)
# ---------------------------------------------------------------------------

_NDJSON_WATCH_CACHE: Dict[str, Dict[str, Any]] = {}


def _ndjson_offset_for_bytes(raw: bytes) -> int:
    """Bytes consumed by complete NDJSON lines (partial trailing preserved)."""
    if not raw:
        return 0
    if raw.endswith(b"\n"):
        return len(raw)
    # No trailing newline — check if last line is valid JSON dict.
    parts = raw.splitlines()
    if not parts:
        return 0
    last = parts[-1].strip()
    if not last:
        # trailing whitespace-only line -> consider complete
        return len(raw)
    try:
        obj = json.loads(last.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, ValueError):
        last_valid = False
    else:
        last_valid = isinstance(obj, dict)
    if last_valid:
        return len(raw)
    # Incomplete trailing line — preserve its bytes for next poll.
    nl = raw.rfind(b"\n")
    return nl + 1 if nl != -1 else 0


def _read_ndjson_incremental(path: Path) -> List[Dict[str, Any]]:
    """Incremental tail read for watch mode keyed by path/size/mtime/inode.

    Tracks ``offset`` (bytes of complete lines consumed) plus ``size``/
    ``mtime``/``inode`` per file; on each poll only the new tail is parsed
    and appended.  If the file was truncated (``size < offset``), its
    ``mtime`` regressed, or its ``inode`` changed (rotation), the cache is
    invalidated and a full re-read is done.  A trailing incomplete line
    (no newline + invalid JSON) does not advance ``offset`` so the bytes
    are re-read once the line completes on the next poll.
    Crash-safety (partial trailing line, non-object JSON) matches
    ``_read_ndjson``.  Diagnostics-only: never raises, never aborts.
    """
    key = str(path.resolve()) if path.exists() else str(path)
    try:
        stat = path.stat()
        size = int(stat.st_size)
        mtime = float(stat.st_mtime)
        inode = int(getattr(stat, "st_ino", 0) or 0) or None
    except OSError:
        _NDJSON_WATCH_CACHE.pop(key, None)
        return []
    cached = _NDJSON_WATCH_CACHE.get(key)
    if cached is not None:
        prev_offset = int(cached.get("offset", cached.get("size", 0)))
        prev_size = int(cached.get("size", prev_offset))
        prev_mtime = float(cached.get("mtime", 0.0))
        prev_inode = cached.get("inode")
        prev_rows: List[Dict[str, Any]] = cached.get("rows", [])
        # Rotation / truncate detection: inode change, size shrink, or mtime regress.
        if inode is not None and prev_inode is not None and inode != prev_inode:
            _NDJSON_WATCH_CACHE.pop(key, None)
        elif size < prev_offset or mtime < prev_mtime - 1e-6:
            _NDJSON_WATCH_CACHE.pop(key, None)
        elif size == prev_size and abs(mtime - prev_mtime) > 1e-6:
            # Same-size rewrite: content may have changed (stale cache).
            # mtime forward (or any drift) with identical size must
            # invalidate — otherwise a same-size rewrite stays stale.
            _NDJSON_WATCH_CACHE.pop(key, None)
        elif size == prev_size and prev_offset == prev_size:
            return list(prev_rows)
        elif size == prev_size:
            # Size unchanged but offset < size means a partial trailing line
            # is still pending — no new bytes to consume (mtime unchanged).
            return list(prev_rows)
        else:
            # Append-only growth: read only the new tail from prev_offset.
            try:
                with path.open("rb") as f:
                    f.seek(prev_offset)
                    tail_bytes = f.read()
            except OSError:
                _NDJSON_WATCH_CACHE.pop(key, None)
                return _read_ndjson(path)
            tail = tail_bytes.decode("utf-8", errors="replace")
            new_rows: List[Dict[str, Any]] = []
            for line in tail.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    new_rows.append(row)
            # Determine how many bytes of the tail were complete.
            if tail_bytes and not tail_bytes.endswith(b"\n"):
                # Check if last line was valid -> complete, else partial.
                parts = tail_bytes.splitlines()
                last_raw = parts[-1].strip() if parts else b""
                try:
                    last_obj = json.loads(last_raw.decode("utf-8", errors="replace")) if last_raw else None
                    last_valid = isinstance(last_obj, dict)
                except (json.JSONDecodeError, ValueError):
                    last_valid = False
                if not last_valid:
                    # Last line incomplete — only advance to last newline.
                    nl = tail_bytes.rfind(b"\n")
                    complete_len = nl + 1 if nl != -1 else 0
                    # Filter new_rows to only those from the complete prefix:
                    # new_rows already excludes the invalid last line, so keep it.
                    merged = list(prev_rows) + new_rows
                    next_offset = prev_offset + complete_len
                    _NDJSON_WATCH_CACHE[key] = {"offset": next_offset, "size": size, "mtime": mtime, "inode": inode, "rows": merged}
                    return list(merged)
            merged = list(prev_rows) + new_rows
            next_offset = prev_offset + len(tail_bytes)
            _NDJSON_WATCH_CACHE[key] = {"offset": next_offset, "size": size, "mtime": mtime, "inode": inode, "rows": merged}
            return list(merged)
    # Cache miss or invalidated: full read and seed cache with offset adjusted for partial tail.
    rows = _read_ndjson(path)
    try:
        raw = path.read_bytes()
        offset = _ndjson_offset_for_bytes(raw)
        stat2 = path.stat()
        _NDJSON_WATCH_CACHE[key] = {"offset": offset, "size": int(stat2.st_size), "mtime": float(stat2.st_mtime), "inode": int(getattr(stat2, "st_ino", 0) or 0) or None, "rows": list(rows)}
    except OSError:
        pass
    return rows


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        # MEDIUM (RV t_c9f9ea90): invalid UTF-8 (errors="replace" above
        # already degrades the bytes) and malformed JSON render an
        # unavailable artifact, never an abort.
        return None
    return payload if isinstance(payload, dict) else None


# Sentinel for a present-but-malformed audit_cache_b3.json.  The audit
# cache is cache-authoritative: a present file that is empty, non-JSON,
# non-object, or otherwise unreadable must fail closed (render an explicit
# error) rather than silently falling through to journal/missing-artifact
# fallback.
_MALFORMED_SENTINEL: Dict[str, Any] = {"__malformed": True}


def _read_audit_cache(out_dir: Path) -> Optional[Dict[str, Any]]:
    """Read ``audit_cache_b3.json`` with cache-authoritative semantics.

    Returns:
    * ``None`` when the file does not exist (no B3 artifact).
    * ``_MALFORMED_SENTINEL`` (a dict with ``__malformed=True``) when the
      file exists but is empty, non-JSON, non-object, or otherwise
      unreadable — callers MUST render an explicit error message.
    * The parsed dict payload when the file is valid.
    """
    path = out_dir / "audit_cache_b3.json"
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return dict(_MALFORMED_SENTINEL)
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return dict(_MALFORMED_SENTINEL)
    if not isinstance(payload, dict):
        return dict(_MALFORMED_SENTINEL)
    return payload


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


def _identity(out_dir: Path, events: List[Dict[str, Any]], snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if snapshot is not None and "strict_record" in snapshot:
        record = snapshot.get("strict_record")
    else:
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
    if snapshot is not None and "usage" in snapshot:
        usage_rows = snapshot.get("usage") or []
        last_usage = usage_rows[-1] if usage_rows else None
    else:
        last_usage = _last_usage_record(out_dir)
    usage_age: Optional[float] = None
    if last_usage is not None and last_usage.get("ts"):
        usage_age = _ts_age(str(last_usage["ts"]))
    recent_usage = usage_age is not None and usage_age <= FRESHNESS_WINDOW_SECONDS
    recent_event = bool(started) and bool(_recent_event_age(events) <= FRESHNESS_WINDOW_SECONDS)

    # V4.1 M (RV fix): local whole-chapter B3 audit/repair calls never write
    # usage.ndjson rows and do not update phase_progress per chunk/repair
    # call — the only fresh activity during a long B3 pass is the audit
    # journal's own events (audit_chunk_started/done, repair_round, ...).
    # Read it read-only: a fresh journal event keeps the run alive, nothing
    # more (no writing, no gating).
    if snapshot is not None and "b3_events" in snapshot:
        b3_events = snapshot.get("b3_events") or []
    else:
        b3_events = _load_b3_events(out_dir)
    b3_age = _recent_event_age(b3_events)
    recent_b3 = b3_age <= FRESHNESS_WINDOW_SECONDS

    # MONITOR-V2 whole-chapter (task req. 6): fresh local llama timing
    # activity (Gemma_/Qwen_ tg_3s) is an additional liveness signal for
    # local runs during long whole-chapter calls. Remote server logs (static
    # opencode_serve_*.log) NEVER produce alive=yes. Basis is explicit.
    local_log_age = _local_log_freshness(out_dir)
    recent_local_log = (local_log_age is not None
                        and local_log_age <= FRESHNESS_WINDOW_SECONDS)

    alive_basis: List[str] = []
    if record is not None:
        alive = False
        alive_basis.append("strict_chapter_trial_record.json exists (run finished)")
    elif terminal is not None:
        alive = False
        alive_basis.append("terminal event written (run finished)")
    else:
        alive = bool(recent_usage or recent_event or recent_b3 or recent_local_log)
        if recent_usage:
            alive_basis.append(f"last usage.ndjson {usage_age:.0f}s ago")
        if recent_event:
            alive_basis.append(f"last progress event {_recent_event_age(events):.0f}s ago")
        if recent_b3:
            alive_basis.append(f"last audit_journal event {b3_age:.0f}s ago")
        if recent_local_log:
            alive_basis.append(f"fresh local llama timing log ({local_log_age:.0f}s ago)")
        if not alive_basis:
            alive_basis.append("no recent usage.ndjson / progress / audit_journal events (stalled or unknown)")

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


def _local_log_freshness(out_dir: Path) -> Optional[float]:
    """Age of the newest local Gemma/Qwen stderr log (seconds, inf if absent).

    MONITOR-V2 whole-chapter (task req. 6): fresh local llama timing
    activity (Gemma_/Qwen_ tg_3s) is an additional liveness signal.
    Remote server logs (opencode_serve_*.log) are NOT checked — they are
    static after server start and never produce alive=yes.
    """
    logs_dir = out_dir / "server_logs"
    if not logs_dir.is_dir():
        return float("inf")
    candidates = [
        p for p in logs_dir.glob("*_stderr.log")
        if p.name.split("_", 1)[0] in ("Gemma", "Qwen")
    ]
    if not candidates:
        return float("inf")
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    try:
        age = (_now() - datetime.fromtimestamp(newest.stat().st_mtime, timezone.utc)).total_seconds()
    except OSError:
        return float("inf")
    return age


# ---------------------------------------------------------------------------
# Phase detection
# ---------------------------------------------------------------------------


def _detect_phase(out_dir: Path, events: List[Dict[str, Any]], snapshot: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
    if snapshot is not None and "strict_record" in snapshot:
        record = snapshot.get("strict_record")
    else:
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
        return _detect_whole_chapter_phase(out_dir, events, snapshot)

    if snapshot is not None:
        b2 = snapshot.get("b2_handoff")
        repair_report = snapshot.get("repair_report")
        chunk_plan = snapshot.get("chunk_plan")
        journal = snapshot.get("journal") or []
    else:
        b2 = _read_json(out_dir / "b2_handoff.json")
        repair_report = _read_json(out_dir / "repair_report.json")
        chunk_plan = _read_json(out_dir / "chunk_plan.json")
        journal = _read_ndjson(out_dir / "journal.ndjson")
    total = len((chunk_plan or {}).get("chunks", [])) if chunk_plan else None

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
    out_dir: Path, events: List[Dict[str, Any]], snapshot: Optional[Dict[str, Any]] = None
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

    if snapshot is not None and "b3_events" in snapshot:
        b3 = snapshot.get("b3_events") or []
    else:
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

    # RV fix: after ``audit_complete`` (audit=True) the repair model call may
    # already be in flight while the repair_round/reaudit_scope event is not
    # yet appended — the monitor must NOT keep reporting step6
    # ``AUDIT chunk N/8`` for a finished audit. Transition to step7 until the
    # repair_round/terminal gate lands. An incomplete audit (audit=False,
    # gate not yet written — transient crash window) stays fail-closed on
    # step6; step8 is shown only after the gate event.
    audit_complete_events = [e for e in b3 if e.get("event") == "audit_complete"]
    if audit_complete_events:
        if audit_complete_events[-1].get("audit_complete") is True:
            return "step7", ("B3 audit complete; repair in flight "
                             "(awaiting repair_round/gate)")
        return "step6", "B3 audit incomplete (fail-closed); awaiting gate"
    if audit_chunk_started or audit_chunk_done:
        total = ((audit_chunk_started[-1].get("total")
                  if audit_chunk_started else None)
                 or (audit_chunk_done[-1].get("total")
                     if audit_chunk_done else None))
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


def _repair_state_by_chunk(out_dir: Path, events: List[Dict[str, Any]], snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
    """Repair state per chunk: ``committed``/``debt``/``in_progress`` from
    ``repair_report.json`` + ``repair_cache.json`` + region events.

    A committed record anywhere wins over debt; ``in_progress`` (a
    ``region_started`` without its ``region_done``) wins over a stale
    artifact state, since the run is still writing.
    """
    states: Dict[str, Dict[str, Any]] = {}

    if snapshot is not None and "repair_report" in snapshot:
        report = snapshot.get("repair_report")
    else:
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


def _chunk_table(out_dir: Path, events: List[Dict[str, Any]], snapshot: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    if snapshot is not None and "chunk_plan" in snapshot:
        chunk_plan = snapshot.get("chunk_plan")
        journal = snapshot.get("journal") or []
        journal_by_chunk = {e.get("chunk_id"): e for e in journal if e.get("chunk_id")}
        b2 = snapshot.get("b2_handoff")
        handoff_by_chunk = {r.get("chunk_id"): r for r in (b2 or {}).get("chunks", []) if r.get("chunk_id")} if b2 else {}
        # repair_state still needs events for region events; read report/cache from snapshot when available
        repair_state = _repair_state_by_chunk(out_dir, events, snapshot)
    else:
        chunk_plan = _read_json(out_dir / "chunk_plan.json")
        journal_by_chunk = _journal_by_chunk(out_dir)
        handoff_by_chunk = _handoff_by_chunk(out_dir)
        repair_state = _repair_state_by_chunk(out_dir, events)

    # V4.1 whole-chapter mode: ONE generation unit (whole_chapter) plus
    # per-chunk audit/repair visibility. The design requires both branches
    # from the same snapshot, not hiding chunk tables. The journal holds a
    # single whole_chapter entry, so per-chunk trial columns stay pending
    # while audit/repair per chunk comes from the same snapshot sources.
    if _whole_chapter_mode(events):
        wc_row = _whole_chapter_chunk_row(out_dir, events, snapshot)
        # Build per-chunk rows from the same snapshot for audit/repair visibility.
        chunk_ids = [row.get("chunk_id") for row in (chunk_plan or {}).get("chunks", []) if row.get("chunk_id")]
        chunk_rows: List[Dict[str, Any]] = []
        for chunk_id in chunk_ids:
            trial, trial_basis = _trial_status(chunk_id, journal_by_chunk, events)
            audit, audit_basis = _audit_status(chunk_id, handoff_by_chunk, events)
            repair, repair_basis = _repair_status(chunk_id, repair_state)
            chunk_rows.append({
                "chunk_id": chunk_id,
                "trial": trial,
                "trial_basis": trial_basis,
                "audit": audit,
                "audit_basis": audit_basis,
                "repair": repair,
                "repair_basis": repair_basis,
            })
        return [wc_row] + chunk_rows

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


def _whole_chapter_chunk_row(out_dir: Path, events: List[Dict[str, Any]], snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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

    if snapshot is not None and "b3_events" in snapshot:
        b3 = snapshot.get("b3_events") or []
    else:
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
        if (audit_done or audit_started) else 0
    ) or len(audit_started)
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


def _formatting_counts(out_dir: Path, events: List[Dict[str, Any]], snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    fmt_events = [e for e in events if e.get("event") == "formatting_done"]
    if fmt_events:
        last = fmt_events[-1]
        return {
            "incidents": last.get("incidents"),
            "blocking": last.get("blocking"),
            "basis": "formatting_done event",
        }
    if snapshot is not None and "formatting_report" in snapshot:
        report = snapshot.get("formatting_report")
    else:
        report = _read_json(out_dir / "formatting_report.json")
    if report and isinstance(report.get("outcome"), dict):
        outcome = report["outcome"]
        return {
            "incidents": outcome.get("incident_count"),
            "blocking": outcome.get("blocking"),
            "basis": "formatting_report.json",
        }
    return {"incidents": None, "blocking": None, "basis": "no formatting artifacts"}


def _terminal_counts(out_dir: Path, events: List[Dict[str, Any]], snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    terminal = _terminal_event(events)
    if terminal is not None:
        return {"status": terminal.get("status"), "basis": "terminal event"}
    if snapshot is not None and "strict_record" in snapshot:
        record = snapshot.get("strict_record")
    else:
        record = _read_json(out_dir / "strict_chapter_trial_record.json")
    if record is not None:
        return {"status": record.get("step8", {}).get("status"), "basis": "strict_chapter_trial_record.json"}
    if snapshot is not None and "repair_report" in snapshot:
        report = snapshot.get("repair_report")
    else:
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
            in_flight.append(f"chunk {event.get('chunk_id')} (Entity extraction)")

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
    """Human phase name for the usage-by-step-x-model "фазы" column.

    MONITOR-V2 (1.3): the column is renamed from ``label-group`` to
    ``фазы`` and shows the same human phase names as the Phase block
    (Extraction / Translation / R_editor / Audit / Repair / Re-audit).
    Uses ``phase_for_label()`` for the label -> phase leg — the same
    rules as v4_usage, never duplicated; ``PHASE_HUMAN_NAME`` only maps
    the returned phase to its human display name.
    """
    phase = phase_for_label(label)
    return PHASE_HUMAN_NAME.get(phase, phase)


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
                # MEDIUM (RV t_c9f9ea90): a non-numeric token value in a
                # structurally-valid row (e.g. ``"input_tokens": "abc"``)
                # counts as 0 — the aggregate stays readable, never aborts.
                bucket[token_key] += _as_int(value)
        cost = row.get("reported_cost")
        if cost is not None:
            bucket["reported_cost"] += _as_float(cost)
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


def _usage_block_lines(out_dir: Path, snapshot: Optional[Dict[str, Any]] = None) -> List[str]:
    """``-- usage by step x model --`` block (from usage.ndjson)."""
    if snapshot is not None and "usage" in snapshot:
        rows = snapshot.get("usage") or []
    else:
        rows = _read_usage_rows(out_dir)
    if not rows:
        # MEDIUM (RV t_c9f9ea90): distinguish "never written" from
        # "exists but corrupt/unreadable" — both render a diagnostic, never
        # an abort.
        if (out_dir / USAGE_FILENAME).exists():
            return ["-- usage by step x model (из usage.ndjson) --",
                    "  (usage.ndjson exists but no readable rows — corrupt/invalid)"]
        return ["-- usage by step x model --", "  (no usage.ndjson yet)"]
    groups = _usage_group_rows(rows)
    totals = _usage_totals(groups)
    show_cost = totals["any_cost"] and totals["reported_cost"] != 0.0
    show_reasoning = any(g["reasoning_tokens"] for g in groups)
    show_cached = any(
        g["cached_input_tokens"] or g["cached_write_tokens"] for g in groups
    )

    lines = ["-- usage by step x model (из usage.ndjson) --"]
    header = (f"{'step':<9} {'фазы':<25} {'model':<18}"
              f"{'calls':>6}{'input':>9}{'output':>9}")
    if show_reasoning:
        header += f"{'reasoning':>10}"
    if show_cached:
        header += f"{'cached':>9}"
    if show_cost:
        header += f"{'cost':>11}"
    lines.append(header)

    step_order = {"Entity extraction": 0, "qwen_fidelity": 1,
                  "gemma_preference": 1, "Whole-chapter translation": 2,
                  "R-editor": 3, "Chapter audit": 4,
                  "Selective repair": 5, "Re-audit scope": 6,
                  "Formatting": 7}
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
# Phase block (MONITOR-V2 1.1) — per-pipeline-phase progress from artifacts
# ---------------------------------------------------------------------------


def _stage_progress_slice(cache: Dict[str, Any], stage: str) -> Optional[Dict[str, Any]]:
    """The KILL-SAFE-INCREMENTAL ``stage_progress`` slice for one B3 stage.

    ``audit_cache_b3.json`` has two shapes:
    * final cache: per-stage results at TOP level (``r_editor`` /
      ``chunks`` / ``repair`` / ``repair.reaudit``) — the historical shape;
    * incremental (KILL-SAFE-INCREMENTAL, t_2d16962c): live per-stage
      payloads under ``stage_progress.<stage>`` (r_editor / audit / repair /
      reaudit), rewritten after every chunk/batch.

    Returns the incremental slice (validated as an object) or ``None`` when
    the cache is a final cache (or the stage never ran). The Phase builders
    prefer the final top-level fields and fall back to this slice so an
    active incremental run shows live Audit/Repair/Re-audit progress
    (RV t_c9f9ea90 HIGH #1).
    """
    stage_progress = cache.get("stage_progress")
    if not isinstance(stage_progress, dict):
        return None
    slice_ = stage_progress.get(stage)
    return slice_ if isinstance(slice_, dict) else None


def _as_int(value: Any, default: int = 0) -> int:
    """Tolerant int for monitor counters: ``None``/non-numeric render 0."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    """Tolerant float for monitor counters (wall seconds, costs)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _phase_extraction(out_dir: Path, snapshot: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Extraction line from ``entity_context_cache.json`` (read-only)."""
    if snapshot is not None and "entity_context" in snapshot:
        payload = snapshot.get("entity_context")
    else:
        payload = _read_json(out_dir / "entity_context_cache.json")
    if not payload:
        return None
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return None
    entities = [
        ent
        for entry in entries
        if isinstance(entry, dict)
        for ent in (
            (lambda c: (c.get("entities")
                        if isinstance(c.get("entities"), list) else [])
             if isinstance(c, dict) else [])(entry.get("context"))
        )
    ]
    if not entities:
        return None
    verified = 0
    candidate = 0
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        anchor = ent.get("anchor") or {}
        if not isinstance(anchor, dict):
            anchor = {}
        if anchor.get("status") == "verified":
            verified += 1
        elif anchor.get("status") == "candidate":
            candidate += 1
        aliases = ent.get("aliases")
        if isinstance(aliases, list):
            for alias in aliases:
                if isinstance(alias, dict):
                    if alias.get("status") == "verified":
                        verified += 1
                    elif alias.get("status") == "candidate":
                        candidate += 1
        claims = ent.get("claims")
        if isinstance(claims, list):
            for claim in claims:
                if isinstance(claim, dict):
                    if claim.get("status") == "verified":
                        verified += 1
                    elif claim.get("status") == "candidate":
                        candidate += 1
    return (f"Entity extraction: сущностей: {len(entities)} "
            f"| claims: verified {verified} / candidate {candidate}")


def _phase_translation(out_dir: Path, events: List[Dict[str, Any]], snapshot: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Translation line from ``translations_raw.json`` + wc events."""
    if snapshot is not None and "translations_raw" in snapshot:
        raw = snapshot.get("translations_raw")
    else:
        raw = _read_json(out_dir / "translations_raw.json")
    if raw is None:
        return None
    if snapshot is not None and "chunk_plan" in snapshot:
        plan = snapshot.get("chunk_plan")
    else:
        plan = _read_json(out_dir / "chunk_plan.json")
    source_words = 0
    if plan:
        chunks = plan.get("chunks")
        if isinstance(chunks, list):
            for chunk in chunks:
                if not isinstance(chunk, dict):
                    continue
                word_counts = chunk.get("word_counts")
                if isinstance(word_counts, list):
                    for w in word_counts:
                        source_words += _as_int(w)
    translation_words = sum(
        len(str(value).split()) for value in raw.values()
    )
    gen = _wc_gen_status(events)
    attempt = f"attempt {gen['attempt']}/{gen['max_attempts']} | " if gen else ""
    return (f"Whole-chapter translation: {attempt}source {source_words} слов → "
            f"перевод {translation_words} слов")


def _phase_r_editor(out_dir: Path, snapshot: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """R_editor line from ``audit_cache_b3.json``.

    Final cache: top-level ``r_editor.outcome`` (chunk_count /
    successful_chunks / applied / candidates). Incremental cache:
    ``stage_progress.r_editor`` (status / done_chunks / outcome.chunks with
    per-chunk edits) — the live fallback (RV t_c9f9ea90 HIGH #1).

    FINDING 1: cache-authoritative — a present but malformed cache renders
    an explicit error, never falls through silently.
    FINDING 2: journal completion validates ``successful_chunks`` and
    ``chunk_count`` as coherent non-negative integers.
    FINDING 3: ``applied_count`` / ``candidate_count`` from the production
    cache are preserved when available (not recomputed from lists).
    """
    if snapshot is not None and "audit_cache" in snapshot:
        cache = snapshot.get("audit_cache")
    else:
        cache = _read_audit_cache(out_dir)
    if cache is None:
        return None
    if cache.get("__malformed"):
        return ("R-editor: audit_cache_b3.json present but malformed "
                "(fail-closed)")
    r_editor = cache.get("r_editor")
    if isinstance(r_editor, dict):
        outcome = r_editor.get("outcome")
        if isinstance(outcome, dict):
            raw_cc = outcome.get("chunk_count")
            raw_sc = outcome.get("successful_chunks")
            # FINDING 2: validate chunk_count and successful_chunks as
            # coherent non-negative ints.  bool/float/string/negative/
            # mismatch -> fall through to incremental path (no crash).
            chunk_count: Optional[int] = None
            if isinstance(raw_cc, int) and not isinstance(raw_cc, bool) and raw_cc >= 0:
                chunk_count = raw_cc
            done: Optional[int] = None
            if isinstance(raw_sc, int) and not isinstance(raw_sc, bool) and raw_sc >= 0:
                done = raw_sc
            # FIX 2 (RV HIGH #1): once cache is present, all invalid
            # nested R-editor shapes must render explicit fail-closed
            # diagnostic.  No GOOD-chunk inference — require explicit
            # valid integer successful_chunks.
            if chunk_count is None and done is not None:
                return ("R-editor: audit_cache_b3.json present but "
                        "r_editor.outcome has invalid chunk_count "
                        "(fail-closed)")
            if done is None and chunk_count is not None:
                # Absent/invalid successful_chunks with valid chunk_count
                # — fall through to incremental path (no GOOD inference).
                pass
            elif chunk_count is not None and done is not None:
                # Coherence: successful_chunks must equal chunk_count.
                if done != chunk_count:
                    # Conflicting evidence — fall through to incremental
                    # path rather than rendering wrong numbers.
                    pass
                else:
                    # FINDING 3: prefer production applied_count /
                    # candidate_count when present; else compute from the
                    # raw lists/scalars (MEDIUM RV t_52f8e9f7: the raw field
                    # may be a scalar int instead of a list — treat scalar
                    # as a count).
                    applied = outcome.get("applied_count")
                    if not isinstance(applied, int) or isinstance(applied, bool):
                        _applied_raw = outcome.get("applied")
                        applied = (len(_applied_raw)
                                   if isinstance(_applied_raw, list)
                                   else _as_int(_applied_raw))
                    candidates = outcome.get("candidate_count")
                    if not isinstance(candidates, int) or isinstance(candidates, bool):
                        _candidates_raw = outcome.get("candidates")
                        candidates = (len(_candidates_raw)
                                      if isinstance(_candidates_raw, list)
                                      else _as_int(_candidates_raw))
                    return (f"R-editor: chunks done={done}/{chunk_count} "
                            f"| safe (применено)={applied} | review (предложено)={candidates}")
            else:
                # Cache present, r_editor is a dict, outcome is a dict,
                # but both chunk_count and successful_chunks are
                # absent/invalid — fail-closed (no GOOD inference).
                return ("R-editor: audit_cache_b3.json present but "
                        "r_editor.outcome has no valid completion data "
                        "(fail-closed)")
        else:
            # Cache present, r_editor is a dict but outcome is not a dict
            # — malformed nested shape, fail-closed.
            return ("R-editor: audit_cache_b3.json present but "
                    "r_editor.outcome is not a valid object "
                    "(fail-closed)")
    elif "r_editor" in cache and cache["r_editor"] is not None:
        # r_editor is present but not a dict — malformed.
        return ("R-editor: audit_cache_b3.json present but "
                "r_editor is not a valid object (fail-closed)")
    # KILL-SAFE-INCREMENTAL fallback: live done_chunks + per-chunk edits.
    stage = _stage_progress_slice(cache, "r_editor")
    if stage is None:
        return None
    done_chunks = stage.get("done_chunks")
    if not isinstance(done_chunks, list) or not done_chunks:
        return None
    done = len(done_chunks)
    # The incremental payload does not persist the planned chunk_count (only
    # chunk_size + the done chunk records), so the live line renders the
    # exact done count without a fabricated denominator.
    applied = 0
    candidates = 0
    outcome = stage.get("outcome")
    if isinstance(outcome, dict):
        chunks = outcome.get("chunks")
        if isinstance(chunks, list):
            # Lazy import: the class sets live in the audit module (single
            # source of truth — never duplicated here); pulled only when the
            # incremental R fallback actually needs them.
            from pact_v4.audit.russian_editor import REVIEW_CLASSES, SAFE_CLASSES
            for chunk in chunks:
                if not isinstance(chunk, dict):
                    continue
                edits = chunk.get("edits")
                if not isinstance(edits, list):
                    continue
                for edit in edits:
                    if not isinstance(edit, dict):
                        continue
                    edit_cls = edit.get("class")
                    if not isinstance(edit_cls, str):
                        continue
                    if edit_cls in SAFE_CLASSES:
                        applied += 1
                    elif edit_cls in REVIEW_CLASSES:
                        candidates += 1
    return (f"R-editor: chunks done={done} "
            f"| safe (применено)={applied} | review (предложено)={candidates}")


def _phase_audit(out_dir: Path, snapshot: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Audit line from ``audit_cache_b3.json``.

    Final cache: top-level ``chunks`` + ``issue_count``. Incremental cache:
    ``stage_progress.audit`` (done_chunks / chunks / issues) — live fallback.

    FINDING 1: cache-authoritative — a present but malformed cache renders
    an explicit error.
    """
    if snapshot is not None and "audit_cache" in snapshot:
        cache = snapshot.get("audit_cache")
    else:
        cache = _read_audit_cache(out_dir)
    if cache is None:
        return None
    if cache.get("__malformed"):
        return ("Chapter audit: audit_cache_b3.json present but malformed "
                "(fail-closed)")
    chunks = cache.get("chunks")
    if isinstance(chunks, list) and chunks:
        total = _as_int(cache.get("issue_count"))
        findings: List[int] = []
        for c in chunks:
            if isinstance(c, dict):
                findings.append(_as_int(c.get("issue_count")))
        return (f"Chapter audit: chunks done={len(chunks)}/{len(chunks)} "
                f"| findings per chunk: {findings} | всего {total}")
    stage = _stage_progress_slice(cache, "audit")
    if stage is None:
        return None
    done_chunks = stage.get("done_chunks")
    if not isinstance(done_chunks, list) or not done_chunks:
        return None
    stage_chunks = stage.get("chunks")
    findings = []
    if isinstance(stage_chunks, list):
        for c in stage_chunks:
            if isinstance(c, dict):
                findings.append(_as_int(c.get("issue_count")))
    issues = stage.get("issues")
    total = len(issues) if isinstance(issues, list) else sum(findings)
    return (f"Chapter audit: chunks done={len(done_chunks)} "
            f"| findings per chunk: {findings} | всего {total}")


def _phase_repair(out_dir: Path, snapshot: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Repair line from ``audit_cache_b3.json``.

    Final cache: top-level ``repair.batches`` (+ committed / eligible_count).
    Incremental cache: ``stage_progress.repair`` (done_batches / committed /
    outcome.batches / outcome.batch_count) — live fallback.

    MONITOR-V2 whole-chapter (task req. 2): show "findings eligible: N"
    and "PID edits committed: M" as separate units (not a ratio).

    FINDING 1: cache-authoritative — a present but malformed cache renders
    an explicit error.
    FINDING 3: preserve production ``applied_count`` / ``candidate_count``
    when available (not recomputed from lists).
    """
    if snapshot is not None and "audit_cache" in snapshot:
        cache = snapshot.get("audit_cache")
    else:
        cache = _read_audit_cache(out_dir)
    if cache is None:
        return None
    if cache.get("__malformed"):
        return ("Selective repair: audit_cache_b3.json present but malformed "
                "(fail-closed)")
    repair = cache.get("repair")
    if isinstance(repair, dict):
        batches = repair.get("batches")
        if isinstance(batches, list) and batches:
            per_batch = []
            for batch in batches:
                if not isinstance(batch, dict):
                    continue
                # MEDIUM (RV t_52f8e9f7): findings/results may be scalar
                # ints instead of lists — treat scalar findings as a count
                # and skip iteration on scalar results.
                _findings_raw = batch.get("findings")
                findings = (len(_findings_raw)
                            if isinstance(_findings_raw, list)
                            else _as_int(_findings_raw))
                _results_raw = batch.get("results")
                _results_iter = _results_raw if isinstance(_results_raw, list) else ()
                repaired = sum(
                    1 for r in _results_iter
                    if isinstance(r, dict)
                    and isinstance(r.get("decision"), str)
                    and r.get("decision").lower() == "repair"
                )
                per_batch.append(f"{repaired}/{findings}")
            # MEDIUM (RV t_52f8e9f7): committed may be a scalar int.
            _committed_raw = repair.get("committed")
            committed = (len(_committed_raw)
                         if isinstance(_committed_raw, (list, dict))
                         else _as_int(_committed_raw))
            eligible = _as_int(repair.get("eligible_count"))
            return (f"Selective repair: batches done={len(batches)}/{len(batches)} "
                    f"| repaired per batch: [{', '.join(per_batch)}] "
                    f"| findings eligible: {eligible} | PID edits committed: {committed}")
    stage = _stage_progress_slice(cache, "repair")
    if stage is None:
        return None
    done_batches = stage.get("done_batches")
    if not isinstance(done_batches, list) or not done_batches:
        return None
    done = len(done_batches)
    outcome = stage.get("outcome")
    batch_count: Optional[int] = None
    batches: List[Any] = []
    if isinstance(outcome, dict):
        if isinstance(outcome.get("batch_count"), int):
            batch_count = outcome["batch_count"]
        if isinstance(outcome.get("batches"), list):
            batches = outcome["batches"]
    per_batch = []
    for batch in batches:
        if not isinstance(batch, dict):
            continue
        # MEDIUM (RV t_52f8e9f7): scalar findings/results guard.
        _findings_raw = batch.get("findings")
        findings = (len(_findings_raw)
                    if isinstance(_findings_raw, list)
                    else _as_int(_findings_raw))
        _results_raw = batch.get("results")
        _results_iter = _results_raw if isinstance(_results_raw, list) else ()
        repaired = sum(
            1 for r in _results_iter
            if isinstance(r, dict)
            and isinstance(r.get("decision"), str)
            and r.get("decision").lower() == "repair"
        )
        per_batch.append(f"{repaired}/{findings}")
    committed = stage.get("committed")
    if isinstance(committed, dict):
        committed_count = len(committed)
    elif isinstance(committed, list):
        committed_count = len(committed)
    else:
        committed_count = _as_int(committed)
    # Count eligible findings from batch outcome.
    eligible_count = 0
    for batch in batches:
        if not isinstance(batch, dict):
            continue
        # MEDIUM (RV t_52f8e9f7): findings may be a scalar int instead of a
        # list — treat scalar as a count.
        _eligible_raw = batch.get("findings")
        eligible_count += (len(_eligible_raw)
                           if isinstance(_eligible_raw, list)
                           else _as_int(_eligible_raw))
    done_txt = f"{done}/{batch_count}" if batch_count else f"{done}"
    return (f"Selective repair: batches done={done_txt} "
            f"| repaired per batch: [{', '.join(per_batch)}] "
            f"| findings eligible: {eligible_count} | PID edits committed: {committed_count}")


def _phase_reaudit(out_dir: Path, snapshot: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Re-audit line from ``audit_cache_b3.json``.

    Final cache: ``repair.reaudit`` (complete/failed/issues); done = persisted
    reaudit chunk raw artifacts, residual = remaining issues, debt = 1 when
    the pass failed. Incremental cache: ``stage_progress.reaudit``
    (status / done_chunks / issues) — live fallback.

    FINDING 1: cache-authoritative — a present but malformed cache renders
    an explicit error.
    """
    if snapshot is not None and "audit_cache" in snapshot:
        cache = snapshot.get("audit_cache")
    else:
        cache = _read_audit_cache(out_dir)
    if cache is None:
        return None
    if cache.get("__malformed"):
        return ("Re-audit scope: audit_cache_b3.json present but malformed "
                "(fail-closed)")
    repair = cache.get("repair")
    if isinstance(repair, dict):
        reaudit = repair.get("reaudit")
        if isinstance(reaudit, dict):
            chunk_files = sorted(out_dir.glob("b3_repair_reaudit_chunk*_raw.txt"))
            total = len(chunk_files)
            done = total if reaudit.get("complete") else 0
            issues = reaudit.get("issues")
            residual = len(issues) if isinstance(issues, list) else 0
            # MONITOR-V2 finding 4: execution debt is independent of
            # residual quality debt.  Both final failure AND incomplete
            # execution (complete=False, no explicit failed) count as
            # execution debt.
            debt = 1 if reaudit.get("failed") or not reaudit.get("complete") else 0
            return (f"Re-audit scope: chunks done={done}/{total} "
                    f"| residual: {residual} | debt: {debt}")
    stage = _stage_progress_slice(cache, "reaudit")
    if stage is None:
        return None
    done_chunks = stage.get("done_chunks")
    if not isinstance(done_chunks, list) or not done_chunks:
        return None
    issues = stage.get("issues")
    residual = len(issues) if isinstance(issues, list) else 0
    # KILL-SAFE-INCREMENTAL (RV5 t_f82ed9ad): a done record marked failed
    # (or a stage status "failed") means the pass did NOT complete — debt 1.
    # MONITOR-V2 finding 4: incomplete/non-terminal execution (chunks exist
    # but stage is not in a terminal state) is also execution debt.
    failed = stage.get("status") == "failed" or any(
        isinstance(c, dict) and c.get("failed") is True for c in done_chunks
    )
    stage_status = stage.get("status")
    incomplete = not failed and stage_status not in ("complete", "failed")
    debt = 1 if failed or incomplete else 0
    return (f"Re-audit scope: chunks done={len(done_chunks)} "
            f"| residual: {residual} | debt: {debt}")


def _phase_block_lines(out_dir: Path, events: List[Dict[str, Any]], snapshot: Optional[Dict[str, Any]] = None) -> List[str]:
    """``-- Phase --`` block: per-pipeline-phase progress (MONITOR-V2 1.1).

    One line per phase whose artifact exists (Extraction / Translation /
    R_editor / Audit / Repair / Re-audit). Absent artifacts are skipped so
    generation-only or pre-B3 runs do not render dead phase lines.

    FINDING 1: the audit cache is read once and the malformed sentinel is
    rendered as a single error line (not duplicated per phase).
    """
    lines = ["-- Phase --"]

    # Pre-read the audit cache once so the malformed-sentinel error is
    # rendered exactly once, not once per cache-reading phase function.
    if snapshot is not None and "audit_cache" in snapshot:
        audit_cache = snapshot.get("audit_cache")
    else:
        audit_cache = _read_audit_cache(out_dir)
    _cache_malformed = (
        isinstance(audit_cache, dict) and audit_cache.get("__malformed")
    )

    def _r_editor(d: Path) -> Optional[str]:
        if _cache_malformed:
            return ("R-editor: audit_cache_b3.json present but malformed "
                    "(fail-closed)")
        return _phase_r_editor(d, snapshot)

    def _audit(d: Path) -> Optional[str]:
        if _cache_malformed:
            return ("Chapter audit: audit_cache_b3.json present but malformed "
                    "(fail-closed)")
        return _phase_audit(d, snapshot)

    def _repair(d: Path) -> Optional[str]:
        if _cache_malformed:
            return ("Selective repair: audit_cache_b3.json present but "
                    "malformed (fail-closed)")
        return _phase_repair(d, snapshot)

    def _reaudit(d: Path) -> Optional[str]:
        if _cache_malformed:
            return ("Re-audit scope: audit_cache_b3.json present but "
                    "malformed (fail-closed)")
        return _phase_reaudit(d, snapshot)

    line_builders = [
        lambda d: _phase_extraction(d, snapshot),
        lambda d: _phase_translation(d, events, snapshot),
        _r_editor,
        _audit,
        _repair,
        _reaudit,
    ]
    rendered = [line for b in line_builders if (line := b(out_dir)) is not None]
    if not rendered:
        lines.append("  (нет Phase-артефактов: entity_context_cache.json / "
                     "translations_raw.json / audit_cache_b3.json)")
    else:
        lines.extend(f"  {line}" for line in rendered)
    return lines


# ---------------------------------------------------------------------------
# Local generation speed (MONITOR-V2 1.2) — from server_logs, local only
# ---------------------------------------------------------------------------


def _llama_ts_raw(prefix: str) -> str:
    """Return the llama.cpp log timestamp prefix as-is.

    llama.cpp monotonic prefixes (e.g. ``14.51.578.231``) are raw counters,
    NOT wall-clock — interpreting them as HH:MM:SS is incorrect and can
    produce impossible values like ``35:59:86``.  Display the raw string
    verbatim so the user sees the monotonic counter without misinterpretation.
    """
    return prefix


def _server_speed_lines(out_dir: Path) -> List[str]:
    """``-- скорость генерации (локальная) --`` block from server_logs.

    MONITOR-V2 (1.2): local-only. Reads the newest ``Gemma_*`` / ``Qwen_*``
    ``_stderr.log`` and parses llama-server ``slot print_timing`` lines
    (eval / prompt tokens-per-second, live ``tg_3s``). Remote runs have no
    local llama-server logs, so the block is not rendered at all.
    """
    logs_dir = out_dir / "server_logs"
    if not logs_dir.is_dir():
        return []
    candidates = [
        p for p in logs_dir.glob("*_stderr.log")
        if p.name.split("_", 1)[0] in ("Gemma", "Qwen")
    ]
    if not candidates:
        return []
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    model = newest.name.split("_", 1)[0].lower()  # gemma / qwen

    eval_tps: Optional[float] = None
    eval_n_decoded: Optional[int] = None
    eval_ms: Optional[float] = None
    eval_ts = ""
    prompt_tps: Optional[float] = None
    live_tg3s: Optional[float] = None
    try:
        for line in newest.read_text(encoding="utf-8", errors="replace").splitlines():
            if "slot print_timing" not in line:
                continue
            ts_match = line.split(" I ", 1)[0] if " I " in line else ""
            m = re.search(r"n_decoded =\s*(\d+), tg =\s*([\d.]+) t/s, tg_3s =\s*([\d.]+) t/s", line)
            if m:
                live_tg3s = float(m.group(3))
                eval_n_decoded = int(m.group(1))
                continue
            m = re.search(r"prompt eval time =\s*([\d.]+) ms / \s*(\d+) tokens \(\s*[\d.]+\s*ms per token,\s*([\d.]+) tokens per second\)", line)
            if m:
                prompt_tps = float(m.group(3))
                continue
            m = re.search(r"eval time =\s*([\d.]+) ms / \s*(\d+) tokens \(\s*[\d.]+\s*ms per token,\s*([\d.]+) tokens per second\)", line)
            if m:
                eval_ms = float(m.group(1))
                eval_n_decoded = int(m.group(2))
                eval_tps = float(m.group(3))
                eval_ts = ts_match
    except OSError:
        return []

    lines = ["-- скорость генерации (локальная, из server_logs) --"]
    if eval_tps is None and live_tg3s is not None:
        # Live tg_3s available from slot print_timing even though no final
        # eval line has been written yet — show it immediately (task req. 5).
        prompt_txt = f"{prompt_tps:.1f}" if prompt_tps is not None else "n/a"
        n_dec_txt = f", n_decoded={eval_n_decoded}" if eval_n_decoded is not None else ""
        lines.append(
            f"  {model}: live tg_3s {live_tg3s:.2f} t/s | prompt {prompt_txt} t/s"
            f" | eval in progress{n_dec_txt}"
        )
        return lines
    if eval_tps is None:
        lines.append(f"  {model}: (нет завершённых eval в server_logs)")
        return lines
    prompt_txt = f"{prompt_tps:.1f}" if prompt_tps is not None else "n/a"
    live_txt = f"{live_tg3s:.2f}" if live_tg3s is not None else "n/a"
    lines.append(
        f"  {model}: eval {eval_tps:.2f} t/s | prompt {prompt_txt} t/s "
        f"| live tg_3s {live_txt} t/s"
    )
    eval_seconds = eval_ms / 1000.0
    request_ts = _llama_ts_raw(eval_ts) if eval_ts else "?"
    lines.append(
        f"    n_decoded={eval_n_decoded}, eval {eval_seconds:.1f}s "
        f"(raw: {request_ts})"
    )
    return lines


def _reasoning_growth(out_dir: Path, window: float = 1.0) -> Optional[Tuple[float, str]]:
    """Live growth of the freshest ``*_reasoning.txt`` file (B/s) + label.

    Read-only: samples the file size twice ``window`` seconds apart. Only
    files modified within FRESHNESS_WINDOW_SECONDS count (live думание);
    returns ``None`` when no reasoning file is being written.
    """
    now = time.time()
    fresh = []
    for p in out_dir.glob("*_reasoning.txt"):
        try:
            age = now - p.stat().st_mtime
        except OSError:
            continue
        if age <= FRESHNESS_WINDOW_SECONDS:
            fresh.append(p)
    if not fresh:
        return None
    newest = max(fresh, key=lambda p: p.stat().st_mtime)
    try:
        size0 = newest.stat().st_size
        time.sleep(window)
        size1 = newest.stat().st_size
    except OSError:
        return None
    rate = (size1 - size0) / window if window > 0 else 0.0
    if rate <= 0:
        return None
    name = newest.name
    label = name
    for marker, human in (
        ("b3_audit_chunk", "audit chunk "),
        ("r_editor_chunk", "R_editor chunk "),
        ("b3_repair_batch", "repair batch "),
        ("b3_repair_reaudit_chunk", "re-audit chunk "),
        ("whole_chapter", "whole chapter"),
        ("b1.2_entity", "entity extraction"),
    ):
        if marker in name:
            tail = name.split(marker, 1)[1].replace("_reasoning.txt", "")
            label = human + tail.lstrip("_")
            break
    return rate, label


def _local_thinking_lines(out_dir: Path) -> List[str]:
    """``live думание`` line: growth of the reasoning trace (local only)."""
    growth = _reasoning_growth(out_dir)
    if growth is None:
        return []
    rate, label = growth
    return [f"  live думание: reasoning.txt растёт {rate:.0f} B/s ({label})"]


# ---------------------------------------------------------------------------
# Last call (MONITOR-V2 1.5) — from usage.ndjson
# ---------------------------------------------------------------------------


def _last_call_block_lines(out_dir: Path, snapshot: Optional[Dict[str, Any]] = None) -> List[str]:
    """``-- последний вызов (из usage.ndjson) --`` block (MONITOR-V2 1.5).

    One line with the human phase, model, in/out/reasoning tokens and wall
    seconds of the most recent usage row. Absent usage.ndjson -> no block.
    """
    if snapshot is not None and "usage" in snapshot:
        rows = snapshot.get("usage") or []
    else:
        rows = _read_usage_rows(out_dir)
    if not rows:
        return []
    row = rows[-1]
    label = row.get("label")
    group = _label_group(label)
    model = row.get("model") or row.get("model_ref") or "?"
    inp = _as_int(row.get("input_tokens"))
    out = _as_int(row.get("output_tokens"))
    reas = _as_int(row.get("reasoning_tokens"))
    wall = row.get("wall_seconds")
    wall_txt = f"{_as_float(wall):.0f}" if wall is not None else "?"
    return [
        "-- последний вызов (из usage.ndjson) --",
        f"  {group} | {model} | in={inp} out={out} reas={reas} | wall={wall_txt}s",
    ]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _status_line(out_dir: Path, events: List[Dict[str, Any]], phase: str, snapshot: Optional[Dict[str, Any]] = None) -> str:
    """Compact one-line status (V4.1 M card)::

        [0001] Whole-chapter translation | attempt 2/3 (reason: malformed) | Chapter audit chunk 3/8 |
        Selective repair committed=2 debt=1 | DONE

    Segments appear only when the corresponding events exist; ``DONE`` only
    at a terminal state. Works for both whole-chapter (wc_* + B3 journal)
    and chunked runs (region events), so the book-run chapters table and the
    single-chapter report share one vocabulary.
    """
    started = _run_started_event(events)
    chapter = (started or {}).get("chapter_id") or out_dir.name
    # MONITOR-V2 finding 5: use canonical phase name in status line.
    segments = [f"[{chapter}] {_PHASE_DISPLAY.get(phase, phase)}"]

    gen = _wc_gen_status(events)
    if gen is not None:
        # MONITOR-V2 whole-chapter canonical: "GEN" -> "Whole-chapter translation"
        segments.append(f"Whole-chapter translation {gen['status']}")
    elif phase not in ("gen", "steps1-5", "unknown"):
        # Chunked runs: show the chunked generation progress (journal) as the
        # GEN segment so the status line stays meaningful outside whole-chapter.
        if snapshot is not None and "journal" in snapshot and "chunk_plan" in snapshot:
            journal = snapshot.get("journal") or []
            chunk_plan = snapshot.get("chunk_plan")
        else:
            journal = _read_ndjson(out_dir / "journal.ndjson")
            chunk_plan = _read_json(out_dir / "chunk_plan.json")
        total = len((chunk_plan or {}).get("chunks", [])) if chunk_plan else 0
        if total:
            segments.append(f"Entity extraction chunks {len(journal)}/{total}")

    if snapshot is not None and "b3_events" in snapshot:
        b3 = snapshot.get("b3_events") or []
    else:
        b3 = _load_b3_events(out_dir)
    audit_chunks = [e for e in b3 if e.get("event") in ("audit_chunk_started", "audit_chunk_done")]
    if audit_chunks:
        last = audit_chunks[-1]
        total = last.get("total")
        # The card's audit chunk N/8 is the CURRENT chunk number, i.e. the
        # newest event's chunk (the one being processed / just finished), not
        # a count of events.
        current = last.get("chunk") or sum(
            1 for e in audit_chunks if e.get("event") == "audit_chunk_started"
        )
        # MONITOR-V2 whole-chapter canonical: "AUDIT" -> "Chapter audit"
        segments.append(f"Chapter audit chunk {current}/{total or '?'}")

    region_counts = _region_counts(events)
    repair_hint = _b3_repair_hint(b3)
    if repair_hint or region_counts["done"]:
        # MONITOR-V2 whole-chapter canonical: "REPAIR" -> "Selective repair"
        if repair_hint:
            segments.append(f"Selective repair {repair_hint.lstrip('; ')}")
        else:
            segments.append(
                f"Selective repair regions done={region_counts['done']} "
                f"committed={region_counts['committed']} "
                f"debt={region_counts['debt']}"
            )

    if phase == "done":
        terminal = _terminal_counts(out_dir, events, snapshot)
        segments.append(f"DONE ({terminal['status']})" if terminal["status"] else "DONE")
    return " | ".join(segments)


def render_report(out_dir: Path, snapshot: Optional[Dict[str, Any]] = None) -> str:
    """Read-only text report over one run directory."""
    out_dir = Path(out_dir)
    if not out_dir.is_dir():
        return f"<no such directory: {out_dir}>"
    # v41 snapshot: one read per cycle, threaded through all helpers so
    # chunk_plan/journal/usage are never read twice; rebuilt every render
    # cycle so watch never stalls. Incremental NDJSON is handled inside
    # _read_snapshot when caller sets incremental=True (watch main loop).
    if snapshot is None:
        snapshot = _read_snapshot(out_dir)
    events = snapshot.get("events") or []
    fine = bool(events)

    identity = _identity(out_dir, events, snapshot)
    phase, phase_basis = _detect_phase(out_dir, events, snapshot)
    wc_mode = _whole_chapter_mode(events)
    rows = _chunk_table(out_dir, events, snapshot)
    chunk_plan = snapshot.get("chunk_plan")
    total_chunks = len((chunk_plan or {}).get("chunks", [])) if chunk_plan else 0
    journal = snapshot.get("journal") or []
    journal_by_chunk = {e.get("chunk_id"): e for e in journal if e.get("chunk_id")}
    trial_counts = _trial_counts(journal_by_chunk)
    audit_counts = _audit_unit_counts(events, total_chunks) if fine else {}
    region_counts = _region_counts(events)
    reaudit_counts = _reaudit_counts(events)
    formatting = _formatting_counts(out_dir, events, snapshot)
    terminal = _terminal_counts(out_dir, events, snapshot)
    in_flight = _in_flight_model_activity(events)
    gen = _wc_gen_status(events) if wc_mode else None
    b3_events = snapshot.get("b3_events") or []

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
    lines.append(f"status: {_status_line(out_dir, events, phase, snapshot)}")
    # MONITOR-V2 finding 5: show canonical phase name, not raw internal code.
    lines.append(f"phase: {_PHASE_DISPLAY.get(phase, phase)} -- {phase_basis}")

    # MONITOR-V2 (1.1/1.2): Phase block (per-pipeline-phase progress) right
    # after the alive header, then the local generation-speed block (local
    # runs only — remote runs never render a speed block).
    lines.append("")
    lines.extend(_phase_block_lines(out_dir, events, snapshot))
    speed_lines = _server_speed_lines(out_dir)
    if speed_lines:
        lines.append("")
        lines.extend(speed_lines)
        # MONITOR-V2 (1.2): live reasoning-trace growth, local only.
        lines.extend(_local_thinking_lines(out_dir))
    lines.append("")
    lines.append("-- chunks (Entity extraction -> Chapter audit -> Selective repair) --")
    if rows:
        lines.append(f"{'chunk_id':<18} {'Entity extraction':<22} {'Chapter audit':<18} {'Selective repair':<13}")
        for row in rows:
            lines.append(
                f"{row['chunk_id']:<18} {row['trial']:<22} {row['audit']:<18} {row['repair']:<13}"
            )
    else:
        lines.append("(no chunk_plan.json / no chunks)")

    lines.append("")
    lines.append("-- counters --")
    if wc_mode and gen is not None:
        # MONITOR-V2 whole-chapter canonical lifecycle: show the current
        # phase with attempt/max, PID count, reasoning budget/model and
        # pending validation; subsequent stages as not started/complete.
        lines.append(
            f"Whole-chapter translation: attempt {gen['attempt']}/{gen['max_attempts']} "
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
        lines.append(f"Entity extraction: journaled {len(journal_by_chunk)}/{total_chunks or 0}"
                     f" (selected={trial_counts['selected']}, quarantined={trial_counts['quarantined']}, "
                     f"needs_synthesis={trial_counts['needs_synthesis']}, "
                     f"incomplete_generation={trial_counts['incomplete_generation']})")
    if wc_mode:
        # MONITOR-V2 whole-chapter canonical lifecycle: subsequent stages
        # show lifecycle status, not legacy Step N names.
        gen_done = any(e.get("event") == "wc_generation_done" for e in events)
        # MONITOR-V2 finding 3: R-editor lifecycle is derived from its own
        # B3 journal/cache events, NOT from generation completion.
        r_editor_done_events = [e for e in b3_events
                                if e.get("event") == "r_editor_done"]
        r_editor_started_events = [e for e in b3_events
                                   if e.get("event") == "r_editor_started"]
        if r_editor_done_events:
            lines.append("R-editor: complete")
        elif r_editor_started_events:
            total_re = r_editor_done_events[-1].get("total") if r_editor_done_events else None
            done_re = len(r_editor_done_events)
            total_hint = f"/{total_re}" if total_re else ""
            lines.append(f"R-editor: in progress, chunks done={done_re}{total_hint}")
        else:
            lines.append("R-editor: not started")
        b3_audit_chunks = [e for e in b3_events
                           if e.get("event") in ("audit_chunk_started", "audit_chunk_done")]
        if b3_audit_chunks:
            total = b3_audit_chunks[-1].get("total")
            started_n = sum(1 for e in b3_audit_chunks if e.get("event") == "audit_chunk_started")
            done_n = sum(1 for e in b3_audit_chunks if e.get("event") == "audit_chunk_done")
            lines.append(f"Chapter audit: in progress, chunks started={started_n}/{total or '?'} "
                         f"done={done_n} (audit_journal.ndjson)")
        elif gen_done:
            lines.append("Chapter audit: not started")
        else:
            lines.append("Chapter audit: not started (awaiting generation)")
        repair_hint = _b3_repair_hint(b3_events)
        if repair_hint:
            lines.append(f"Selective repair: {repair_hint.lstrip('; ')}")
        elif gen_done:
            lines.append("Selective repair: not started")
        else:
            lines.append("Selective repair: not started (awaiting generation)")
        # Re-audit and Formatting: lifecycle status for whole-chapter.
        reaudit_events = [e for e in b3_events if e.get("event") == "reaudit_scope"]
        if reaudit_events:
            lines.append("Re-audit scope: in progress")
        elif gen_done:
            lines.append("Re-audit scope: not started")
        else:
            lines.append("Re-audit scope: not started (awaiting generation)")
        lines.append("Formatting: not applicable (whole-chapter)")
    elif fine:
        lines.append(
            f"Chapter audit: audit units done={audit_counts['done']}/{audit_counts['expected']} "
            f"(started={audit_counts['started']})"
        )
        lines.append(
            f"Selective repair: regions planned={region_counts['planned']} done={region_counts['done']} "
            f"committed={region_counts['committed']} debt={region_counts['debt']} "
            f"in_progress={region_counts['in_progress']}; "
            f"Re-audit scope: units done={reaudit_counts['done']}/{reaudit_counts['started']}"
        )
    else:
        lines.append("Chapter audit / Selective repair: not available (coarse mode, no phase_progress.ndjson)")
    # V4 monitor v2 (owner observation eff-a1a2): before Step 8 starts the
    # old text read as "formatting incidents=None ... terminal=None" — a
    # broken-looking Step 8 block. Show an explicit "not started" instead.
    # MONITOR-V2 whole-chapter: Formatting is already shown as "not
    # applicable" in the counters block — skip the redundant Step 8 line.
    if not wc_mode:
        if phase in ("steps1-5", "step6", "step7", "unknown", "gen"):
            lines.append("Formatting: not started (ожидание formatting/terminal)")
        else:
            lines.append(f"Formatting: incidents={formatting['incidents']} blocking={formatting['blocking']}"
                         f" ({formatting['basis']}); terminal={terminal['status']} ({terminal['basis']})")
    lines.append("")
    lines.extend(_usage_block_lines(out_dir, snapshot))

    # MONITOR-V2 (1.5): last completed call block (only when usage.ndjson
    # exists), placed right after the usage block.
    lines.append("")
    lines.extend(_last_call_block_lines(out_dir, snapshot))

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


def _chapter_summary_row(chapter_dir: Path, snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One row of the chapters table: id, mode/unit, step, status, calls, tokens, cost."""
    if snapshot is None:
        snapshot = _read_snapshot(chapter_dir)
    events = snapshot.get("events") or []
    phase, _ = _detect_phase(chapter_dir, events, snapshot)

    chunk_plan = snapshot.get("chunk_plan")
    total = len((chunk_plan or {}).get("chunks", [])) if chunk_plan else 0
    journal = {e.get("chunk_id"): e for e in (snapshot.get("journal") or []) if e.get("chunk_id")}

    usage = snapshot.get("usage") or []
    calls = len(usage)
    costs = [_as_float(row.get("reported_cost")) for row in usage
             if row.get("reported_cost") is not None]
    cost = sum(costs)
    # MONITOR-V2 (1.4): per-chapter token sums from usage.ndjson.
    # MEDIUM (RV t_c9f9ea90): non-numeric token values count as 0 — a
    # malformed-but-valid usage row never aborts the book table.
    input_tokens = sum(_as_int(row.get("input_tokens")) for row in usage)
    output_tokens = sum(_as_int(row.get("output_tokens")) for row in usage)
    reasoning_tokens = sum(_as_int(row.get("reasoning_tokens")) for row in usage)

    # MONITOR-V2 whole-chapter book table (task req. 4): whole-chapter runs
    # show "1/1" (one generation unit), chunked runs show "N/M" (journal
    # chunks / planned chunks). Never mix generation and audit progress.
    wc_mode = _whole_chapter_mode(events)
    if wc_mode:
        mode_unit = "1/1"
    else:
        mode_unit = f"{len(journal)}/{total}"

    if phase == "done":
        step = "Complete"
        terminal = _terminal_counts(chapter_dir, events)
        status = terminal["status"] or "done"
    elif phase == "step8":
        step = "Formatting"
        status = "Formatting"
    elif phase == "step7":
        step = "Selective repair"
        round_number = _current_round(events)
        status = f"repair r{round_number}" if round_number else "Selective repair"
    elif phase == "step6":
        step = "Chapter audit"
        status = "Chapter audit"
    elif phase == "gen":
        step = "Whole-chapter translation"
        gen = _wc_gen_status(events)
        status = f"Whole-chapter translation {gen['attempt']}/{gen['max_attempts']}" if gen else "Whole-chapter translation"
    elif phase == "steps1-5":
        step = "Entity extraction"
        status = "Entity extraction"
    else:
        step = "?"
        status = _PHASE_DISPLAY.get(phase, phase)

    return {
        "chapter_id": chapter_dir.name,
        "mode_unit": mode_unit,
        "step": step,
        "status": status,
        "calls": calls,
        "cost": cost,
        # Present only when at least one usage row actually reported a
        # non-zero provider cost: all 0/None -> hide the column gracefully
        # (spec), not "$0.00" noise.
        "cost_present": any(c != 0 for c in costs),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "events": events,
    }


def _render_chapters_table(chapters: List[Path], incremental: bool = False, snapshots: Optional[Dict[str, Dict[str, Any]]] = None) -> List[str]:
    # v41: each chapter's row is built from a single snapshot per chapter
    # so the table does not re-read chunk_plan/journal/usage multiple times.
    # When snapshots dict is provided (book watch), reuse it to avoid extra reads.
    def _snap(ch: Path) -> Dict[str, Any]:
        if snapshots is not None and str(ch) in snapshots:
            return snapshots[str(ch)]
        return _read_snapshot(ch, incremental=incremental)
    rows = [_chapter_summary_row(ch, _snap(ch)) for ch in chapters]
    total_calls = sum(r["calls"] for r in rows)
    total_cost = sum(r["cost"] for r in rows)
    total_input = sum(r["input_tokens"] for r in rows)
    total_output = sum(r["output_tokens"] for r in rows)
    total_reasoning = sum(r["reasoning_tokens"] for r in rows)
    # Cost column only when at least one chapter actually reported a cost
    # (all 0/None -> hide gracefully, no "$0.00" noise).
    show_cost = any(r["cost_present"] for r in rows)
    # MONITOR-V2 (1.4): token columns follow the same graceful rule — shown
    # only when at least one chapter reported usage tokens.
    show_input = any(r["input_tokens"] for r in rows)
    show_output = any(r["output_tokens"] for r in rows)
    show_reasoning = any(r["reasoning_tokens"] for r in rows)

    lines = [f"-- chapters ({len(chapters)}) {'-' * 56}"]
    # MONITOR-V2 whole-chapter book table (task req. 4): column renamed
    # from "chunks" to "mode/unit" — whole-chapter runs show "1/1",
    # chunked runs show "N/M" (journal/planned).
    header = (f"{'chapter_id':<24} {'mode/unit':>9} {'step':>25} {'status':<30}"
              f"{'calls':>7}")
    if show_input:
        header += f"{'input':>9}"
    if show_output:
        header += f"{'output':>9}"
    if show_reasoning:
        header += f"{'reasoning':>10}"
    if show_cost:
        header += f" {'cost(prov.)':>10}"
    lines.append(header)
    for row in rows:
        status = _SHORT_STATUS.get(row["status"], row["status"])[:30]
        line = (f"{row['chapter_id']:<24} {row['mode_unit']:>9} {row['step']:>25}"
                f" {status:<30}{row['calls']:>7}")
        if show_input:
            line += f"{_fmt_tokens(row['input_tokens']):>9}"
        if show_output:
            line += f"{_fmt_tokens(row['output_tokens']):>9}"
        if show_reasoning:
            line += f"{_fmt_tokens(row['reasoning_tokens']):>10}"
        if show_cost:
            line += f" {_fmt_cost_short(row['cost']):>10}"
        lines.append(line)
    total_line = (f"{'TOTAL':<24} {'':>9} {'':>25} {'':<30}{total_calls:>7}")
    if show_input:
        total_line += f"{_fmt_tokens(total_input):>9}"
    if show_output:
        total_line += f"{_fmt_tokens(total_output):>9}"
    if show_reasoning:
        total_line += f"{_fmt_tokens(total_reasoning):>10}"
    if show_cost:
        total_line += f" {_fmt_cost_short(total_cost):>10}"
    lines.append(total_line)
    return lines


def _active_chapter(chapters: List[Path], snapshots: Optional[Dict[str, Dict[str, Any]]] = None) -> Optional[Path]:
    """The chapter being processed now: the newest activity among those
    that have not reached a terminal state; fall back to the newest overall.

    When snapshots are provided (book watch), activity/terminal checks reuse
    the same snapshots instead of re-reading events/usage per chapter.
    """
    def _snapshot_for(chapter_dir: Path) -> Optional[Dict[str, Any]]:
        if snapshots is not None and str(chapter_dir) in snapshots:
            return snapshots[str(chapter_dir)]
        return None

    def _activity_ts(chapter_dir: Path) -> str:
        snap = _snapshot_for(chapter_dir)
        if snap is not None:
            events = snap.get("events") or []
            usage = snap.get("usage") or []
        else:
            events = _load_events(chapter_dir)
            usage = _read_usage_rows(chapter_dir)
        latest = ""
        for event in events:
            ts = event.get("ts") or ""
            if ts > latest:
                latest = ts
        for row in usage:
            ts = row.get("ts") or ""
            if ts > latest:
                latest = ts
        return latest

    def _not_terminal(chapter_dir: Path) -> bool:
        snap = _snapshot_for(chapter_dir)
        if snap is not None:
            events = snap.get("events") or []
            record = snap.get("strict_record")
        else:
            events = _load_events(chapter_dir)
            record = _read_json(chapter_dir / "strict_chapter_trial_record.json")
        return record is None and _terminal_event(events) is None

    active = [c for c in chapters if _not_terminal(c)]
    if not active:
        active = chapters
    return max(active, key=_activity_ts) if active else None


def render_book_report(out_base: Path, incremental: bool = False) -> str:
    """Read-only multi-chapter (book-run) report over ``--out-base``."""
    out_base = Path(out_base)
    if not out_base.is_dir():
        return f"<no such directory: {out_base}>"

    chapters = _discover_chapters(out_base)
    lines: List[str] = [f"== V4 book progress: {out_base} =="]
    if not chapters:
        lines.append("(no chapter_*/ with phase_progress.ndjson found yet)")
        return "\n".join(lines)

    # v41 6.1/2.2: build one snapshot per chapter and thread it through
    # both the chapters table and the active-chapter selection/detail so
    # watch does not re-read events/usage per chapter multiple times.
    snapshots: Dict[str, Dict[str, Any]] = {str(ch): _read_snapshot(ch, incremental=incremental) for ch in chapters}
    lines.extend(_render_chapters_table(chapters, incremental=incremental, snapshots=snapshots))

    active = _active_chapter(chapters, snapshots=snapshots)
    if active is not None:
        lines.append("")
        lines.append(f"-- active chapter: {active.name} --")
        active_snap = snapshots.get(str(active))
        if active_snap is None:
            active_snap = _read_snapshot(active, incremental=incremental)
        lines.append(render_report(active, active_snap))
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
    watch_incremental = bool(args.watch is not None and args.watch > 0)
    while True:
        if args.out_base is not None:
            print(render_book_report(args.out_base, incremental=watch_incremental))
        else:
            if watch_incremental:
                print(render_report(args.out_dir, _read_snapshot(args.out_dir, incremental=True)))
            else:
                print(render_report(args.out_dir))
        if args.watch is None or args.watch <= 0:
            break
        time.sleep(args.watch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
