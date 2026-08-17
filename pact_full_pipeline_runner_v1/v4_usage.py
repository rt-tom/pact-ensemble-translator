#!/usr/bin/env python3
"""CLI: read-only per-call remote-usage report for the strict chapter driver.

Backing task: ``docs/plans/V4_D1_USAGE_RECORD_TASK_RU.md``. Reads the
append-only ``usage.ndjson`` artifact written by
``pact_v4.pipeline.v4_phase12_strict_runner.run_chapter_strict`` (one JSON
line per remote call, success and failure) and renders a human-readable,
read-only report: tokens by model and by role, totals, input/output
throughput (tokens per wall-second, a coarse estimate — wall includes
network/queue, not pure provider decode speed), and reported cost.

Usage::

    python -m pact_full_pipeline_runner_v1.v4_usage \\
        --out-dir "D:/pact/gate_bench_runs/v4_phase12_strict_0001/run_002_remote"

``--watch`` re-renders the report every N seconds. On a catalog without
``usage.ndjson`` (pre-D1 runs such as ``run_001`` / ``run_002_remote``) the
report falls back to the existing ``runtime.remote_calls`` aggregate from
``strict_chapter_trial_record.json`` (count, input/output/cached tokens,
reported cost) — the same summary the record has always carried.

Everything reported here is a *diagnostic* read: token counts and speeds
never imply translation quality, and the aggregator never writes to
``out_dir``, never starts/stops the pipeline or ``llama-server``.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from pact_v4.pipeline.usage_record import USAGE_FILENAME

# Fields that count toward a "call" in the report. The usage dict from the
# provider may omit any of these (plan §9.3: never invented).
TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cached_input_tokens",
    "cached_write_tokens",
)
COST_KEY = "reported_cost"

# V4 Efficiency A1.3: per-call label -> phase mapping for the "by phase"
# breakdown. Labels are the namespaced request labels the role adapters
# emit (``phase2b/...``, ``phase2c/qwen_fidelity``,
# ``phase2c/gemma_russian_preference``, ``phase3/...``, ``phase4/...``,
# ``phase5/...``); legacy labels (``generator``, ``qwen_audit``, ...) from
# pre-namespace runs are matched by exact role name. The runtime adapters'
# default labels are still the legacy hyphen forms
# (``phase2c-qwen-fidelity`` from qwen_evaluator.py,
# ``phase2c-gemma-russian-preference`` from gemma_selector.py), so both the
# slash- and hyphen-spelled phase2c forms map to the same phases. Order
# matters: the phase2c prefix is checked before the generic phase3/4/5
# prefixes would not collide (they are distinct prefixes), but ``phase2c``
# itself must split qwen_fidelity from gemma_russian_preference (both
# spellings) before the fallbacks.
_PHASE_RULES: Tuple[Tuple[str, str], ...] = (
    ("phase2b", "gen"),
    ("phase2c/qwen_fidelity", "qwen_fidelity"),
    ("phase2c/gemma_russian_preference", "gemma_preference"),
    ("phase2c-qwen-fidelity", "qwen_fidelity"),
    ("phase2c-gemma-russian-preference", "gemma_preference"),
    ("phase2c", "(other)"),
    # MONITOR-V2 (1.3): B3-era sub-phases. The B3 stage runs four distinct
    # roles under the phase3 namespace (R_editor, audit, selective repair,
    # re-audit); the monitor's usage column shows them as the same human
    # phases as the Phase block, so these specific prefixes must win over
    # the generic phase3 rule below.
    ("b1.2/entity_extractor", "extraction"),
    ("phase3/russian_editor", "r_editor"),
    ("phase3/reaudit_scope", "reaudit"),
    ("phase3/selective_repair", "repair"),
    ("phase3/qwen_chapter_audit", "audit"),
    ("phase3", "audit"),
    ("phase4", "repair"),
    ("phase5", "formatting"),
)
_PHASE_ROLE_NAMES = {
    "generator": "gen",
    "fidelity_reviewer": "qwen_fidelity",
    "qwen_fidelity": "qwen_fidelity",
    "russian_selector": "gemma_preference",
    "gemma_russian_preference": "gemma_preference",
    "qwen_audit": "audit",
    "gemma_audit": "audit",
    "repair": "repair",
    "formatting": "formatting",
    "formatting_align": "formatting",
}


def phase_for_label(label: Optional[str]) -> str:
    """Map a usage-row ``label`` to its phase (gen / qwen_fidelity /
    gemma_preference / audit / repair / formatting), ``(other)`` otherwise.

    Read-only helper (A1.3) — never changes the pipeline, only how the
    diagnostic report groups rows. Prefix rules are checked first (the
    adapters' namespaced slash labels and their legacy hyphen defaults,
    e.g. ``phase2c/qwen_fidelity`` and ``phase2c-qwen-fidelity``), then
    exact legacy role names.
    """
    if not label:
        return "(other)"
    folded = str(label).strip()
    for prefix, phase in _PHASE_RULES:
        if folded.startswith(prefix):
            return phase
    return _PHASE_ROLE_NAMES.get(folded, "(other)")


# ---------------------------------------------------------------------------
# Read helpers (all read-only)
# ---------------------------------------------------------------------------


def _read_ndjson(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    rows: List[Dict[str, Any]] = []
    for raw_line in raw.splitlines():
        if not raw_line.strip():
            continue
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError:
            # Crash-safe: a crash mid-write can leave a partial trailing
            # line that is also an incomplete multibyte UTF-8 sequence
            # (the writer uses ensure_ascii=False). Skip only the malformed
            # fragment; valid completed records are never altered.
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


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _bucketed(rows: List[Dict[str, Any]], key: str) -> Dict[str, Dict[str, Any]]:
    """Aggregate usage rows into per-``key`` buckets (model_ref / label)."""
    buckets: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        bucket_key = row.get(key) or "(unknown)"
        _bucket_row(buckets, bucket_key, row)
    return buckets


def _bucketed_by_phase(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Aggregate usage rows into per-phase buckets (V4 Efficiency A1.3).

    Phase = ``phase_for_label(row["label"])`` (gen / qwen_fidelity /
    gemma_preference / audit / repair / formatting). Shares the token/cost
    aggregation of ``_bucketed`` so the breakdown is directly comparable to
    the by-model / by-role sections.
    """
    buckets: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        _bucket_row(buckets, phase_for_label(row.get("label")), row)
    return buckets


def _bucket_row(
    buckets: Dict[str, Dict[str, Any]], bucket_key: str, row: Dict[str, Any]
) -> None:
    bucket = buckets.setdefault(bucket_key, {
        "calls": 0, "failed": 0, "wall_seconds": 0.0,
    })
    for token_key in TOKEN_KEYS:
        value = row.get(token_key)
        if value is not None:
            bucket[token_key] = bucket.get(token_key, 0) + int(value)
    cost = row.get(COST_KEY)
    if cost is not None:
        bucket[COST_KEY] = bucket.get(COST_KEY, 0.0) + float(cost)
    bucket["calls"] += 1
    if row.get("error_class"):
        bucket["failed"] += 1
    bucket["wall_seconds"] += float(row.get("wall_seconds") or 0.0)


def _per_call_rates(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per-call input/output throughput (tokens per wall-second).

    Coarse estimate: wall_seconds includes network/queue, not pure provider
    decode speed (card §read-only aggregator). A call with no wall time or
    no reported tokens gets ``None`` rates.
    """
    out: List[Dict[str, Any]] = []
    for row in rows:
        wall = float(row.get("wall_seconds") or 0.0)
        input_tokens = row.get("input_tokens")
        output_tokens = row.get("output_tokens")
        out.append({
            "label": row.get("label"),
            "model_ref": row.get("model_ref"),
            "wall_seconds": round(wall, 3),
            "input_tps": (
                round(int(input_tokens) / wall, 2)
                if input_tokens is not None and wall > 0
                else None
            ),
            "output_tps": (
                round(int(output_tokens) / wall, 2)
                if output_tokens is not None and wall > 0
                else None
            ),
            "error_class": row.get("error_class"),
        })
    return out


def _aggregate(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Full report payload from parsed usage.ndjson rows."""
    by_model = _bucketed(rows, key="model_ref")
    by_role = _bucketed(rows, key="label")
    # V4 Efficiency A1.3: per-phase breakdown (gen / qwen_fidelity /
    # gemma_preference / audit / repair / formatting) for the efficiency
    # call/token accounting — read-only grouping of the same rows.
    by_phase = _bucketed_by_phase(rows)

    total_calls = len(rows)
    total_failed = sum(1 for row in rows if row.get("error_class"))
    totals: Dict[str, Any] = {"calls": total_calls, "failed": total_failed}
    for token_key in TOKEN_KEYS:
        total = sum(int(row.get(token_key) or 0) for row in rows)
        if total:
            totals[token_key] = total
    costs = [float(row[COST_KEY]) for row in rows if row.get(COST_KEY) is not None]
    if costs:
        totals[COST_KEY] = round(sum(costs), 6)
    total_wall = sum(float(row.get("wall_seconds") or 0.0) for row in rows)
    totals["wall_seconds"] = round(total_wall, 3)

    # Throughput: tokens per wall-second per call, averaged only over calls
    # that reported both tokens and wall time. Wall includes network/queue —
    # a coarse estimate, not pure provider decode speed (card §read-only
    # aggregator).
    def _avg_tps(token_key: str) -> Optional[float]:
        rates = [
            int(row[token_key]) / float(row["wall_seconds"])
            for row in rows
            if row.get(token_key) is not None
            and float(row.get("wall_seconds") or 0.0) > 0
        ]
        if not rates:
            return None
        return round(sum(rates) / len(rates), 2)

    totals["input_tps_avg"] = _avg_tps("input_tokens")
    totals["output_tps_avg"] = _avg_tps("output_tokens")

    return {
        "totals": totals,
        "by_model": by_model,
        "by_role": by_role,
        "by_phase": by_phase,
        "per_call": _per_call_rates(rows),
    }


def _record_fallback(out_dir: Path) -> Optional[Dict[str, Any]]:
    """``runtime.remote_calls`` aggregate from the run record, or None."""
    record = _read_json(out_dir / "strict_chapter_trial_record.json")
    if record is None:
        return None
    remote = (record.get("runtime") or {}).get("remote_calls")
    if remote is None:
        return None
    return dict(remote)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _fmt_bucket_rows(buckets: Mapping[str, Dict[str, Any]]) -> List[str]:
    lines: List[str] = []
    for bucket_key, bucket in sorted(buckets.items()):
        parts = [
            f"{bucket_key}: {bucket['calls']} call(s)"
            f" (failed={bucket['failed']})",
        ]
        for token_key in TOKEN_KEYS:
            if bucket.get(token_key) is not None:
                parts.append(f"{token_key}={bucket[token_key]}")
        if bucket.get(COST_KEY) is not None:
            parts.append(f"cost={round(bucket[COST_KEY], 6)}")
        if bucket.get("wall_seconds"):
            parts.append(f"wall={round(bucket['wall_seconds'], 2)}s")
        lines.append("  " + "; ".join(parts))
    return lines


def render_usage_report(out_dir: Path) -> str:
    """Read-only text report over one run directory."""
    out_dir = Path(out_dir)
    if not out_dir.is_dir():
        return f"<no such directory: {out_dir}>"

    usage_path = out_dir / USAGE_FILENAME
    rows = _read_ndjson(usage_path)
    lines: List[str] = [f"== V4 remote usage: {out_dir} =="]

    if rows:
        agg = _aggregate(rows)
        totals = agg["totals"]
        lines.append(f"source: {USAGE_FILENAME} ({totals['calls']} call(s), "
                     f"{totals['failed']} failed)")
        lines.append("")
        lines.append("-- totals --")
        lines.append(f"calls: {totals['calls']} (failed={totals['failed']})")
        for token_key in TOKEN_KEYS:
            if totals.get(token_key) is not None:
                lines.append(f"{token_key}: {totals[token_key]}")
        if totals.get(COST_KEY) is not None:
            lines.append(f"reported_cost: {totals[COST_KEY]} (provider-reported)")
        lines.append(f"wall_seconds: {totals['wall_seconds']}")
        lines.append(
            "input tps (avg): "
            + (str(totals["input_tps_avg"]) if totals["input_tps_avg"] is not None else "n/a")
            + "; output tps (avg): "
            + (str(totals["output_tps_avg"]) if totals["output_tps_avg"] is not None else "n/a")
            + "  [coarse: wall includes network/queue, not pure decode speed]"
        )

        lines.append("")
        lines.append("-- by model --")
        lines.extend(_fmt_bucket_rows(agg["by_model"]) or ["  (none)"])

        lines.append("")
        lines.append("-- by role (label) --")
        lines.extend(_fmt_bucket_rows(agg["by_role"]) or ["  (none)"])

        lines.append("")
        lines.append("-- by phase (A1.3 + MONITOR-V2 1.3: extraction / gen / "
                     "qwen_fidelity / gemma_preference / r_editor / audit / "
                     "repair / reaudit / formatting) --")
        lines.extend(_fmt_bucket_rows(agg["by_phase"]) or ["  (none)"])

        lines.append("")
        lines.append("-- per-call rates (input/output tokens per wall-second; "
                     "coarse: wall includes network/queue) --")
        for pc in agg["per_call"]:
            inp = f"{pc['input_tps']}" if pc["input_tps"] is not None else "n/a"
            out = f"{pc['output_tps']}" if pc["output_tps"] is not None else "n/a"
            failed = f" failed={pc['error_class']}" if pc.get("error_class") else ""
            lines.append(
                f"  {pc['label']} {pc['model_ref']} "
                f"in={inp}/s out={out}/s wall={pc['wall_seconds']}s{failed}"
            )
    else:
        fallback = _record_fallback(out_dir)
        if fallback is not None:
            lines.append(f"source: no {USAGE_FILENAME} -> "
                         "strict_chapter_trial_record.json runtime.remote_calls "
                         "(aggregate fallback)")
            lines.append("")
            lines.append("-- record aggregate (no per-call breakdown) --")
            lines.append(f"calls: {fallback.get('count')}")
            for key in ("input_tokens", "output_tokens", "cached_input_tokens"):
                if fallback.get(key) is not None:
                    lines.append(f"{key}: {fallback[key]}")
            if fallback.get("reported_cost") is not None:
                lines.append(f"reported_cost: {fallback['reported_cost']}")
        else:
            lines.append(f"source: no {USAGE_FILENAME} and no record "
                         "runtime.remote_calls -> nothing to report")

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
                    help="Run directory (contains usage.ndjson and the run artifacts).")
    p.add_argument("--watch", type=float, default=None, metavar="SEC",
                    help="Re-render the report every SEC seconds until interrupted.")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_argparser().parse_args(argv)
    while True:
        print(render_usage_report(args.out_dir))
        if args.watch is None or args.watch <= 0:
            break
        time.sleep(args.watch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
