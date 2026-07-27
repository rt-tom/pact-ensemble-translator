#!/usr/bin/env python3
"""Immutable final changed-PID lineage and terminal quality policy for v3.1."""
from __future__ import annotations

from copy import deepcopy
import argparse
from pathlib import Path
from typing import Any

from v31_common import add_common_args, load_cfg, load_runtime, read_json, selected_chapters, write_json


TERMINAL = {"complete", "quarantined", "failed"}


def prior_terminal_status(work: Path) -> str | None:
    """Read the authoritative terminal record for this work directory.

    ``state.json`` is authoritative because it is the chapter lifecycle record.
    The quality gate is its runner-facing projection and must agree whenever it
    has a terminal status; disagreement is an execution failure, never a reason
    to silently promote a quarantined chapter.
    """
    state = read_json(work / "state.json", {})
    gate = read_json(work / "v31_quality_gate.json", {})
    state_status = state.get("status") if isinstance(state, dict) else None
    gate_status = gate.get("status") if isinstance(gate, dict) else None
    if state_status not in TERMINAL:
        state_status = None
    if gate_status not in TERMINAL:
        gate_status = None
    # A stale gate is never authoritative.  The active finalizer will replace
    # it from state.json before allowing any terminal transition.
    return state_status


def changed_pids(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Return textual changes only; normalization-only cache rewrites do not count."""
    return [pid for pid in after if " ".join(before.get(pid, "").split()) != " ".join(after[pid].split())]


def append_ledger(ledger: dict[str, Any] | None, before: dict[str, str], after: dict[str, str], stage: str, reason: str) -> dict[str, Any]:
    """Append-only lineage.  Earlier changed PIDs and reasons are never discarded."""
    result = deepcopy(ledger or {"schema": "v3.1-final-ledger/v1", "entries": []})
    entries = result.setdefault("entries", [])
    for pid in changed_pids(before, after):
        entries.append({"pid": pid, "stage": stage, "reason": reason, "before": before.get(pid, ""), "after": after[pid]})
    result["changed_pids"] = sorted({str(row["pid"]) for row in entries})
    return result


def context_pids(blocks: list[dict[str, Any]], targets: list[str], radius: int = 2) -> list[str]:
    positions = {str(block["pid"]): i for i, block in enumerate(blocks)}
    selected: set[str] = set()
    for pid in targets:
        if pid not in positions:
            raise ValueError(f"ledger PID is absent from manifest: {pid}")
        index = positions[pid]
        selected.update(str(row["pid"]) for row in blocks[max(0, index-radius):index+radius+1])
    return [str(row["pid"]) for row in blocks if str(row["pid"]) in selected]


def terminal_status(*, ledger_ok: bool, coverage_ok: bool, verification_ok: bool,
                    smoke_ok: bool, blocking_findings: list[dict[str, Any]],
                    final_repair_rounds: int, prior_status: str | None = None) -> str:
    """Quality findings quarantine; execution and accounting failures fail.

    A terminal quarantine is monotonic: a later stale artifact can never turn it
    into complete.  The runner is allowed exactly one final repair round.
    """
    if not ledger_ok or not coverage_ok or not verification_ok or not smoke_ok:
        return "failed"
    if final_repair_rounds > 1:
        return "failed"
    if prior_status == "failed":
        return "failed"
    if prior_status == "quarantined":
        return "quarantined"
    if blocking_findings:
        return "quarantined"
    return "complete"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser, include_pass=False)
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()
    runtime = load_runtime(args.project_root.resolve())
    cfg = load_cfg(runtime, args.config.resolve())
    for _, work in selected_chapters(runtime, cfg, args.start, args.end):
        ledger_path = work / "v31_final_changed_pid_ledger.json"
        before = read_json(work / args.before, {})
        after = read_json(work / args.after, {})
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise RuntimeError("final changed-PID lineage requires translation objects")
        write_json(ledger_path, append_ledger(read_json(ledger_path, {}), before, after, args.stage, args.reason))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
