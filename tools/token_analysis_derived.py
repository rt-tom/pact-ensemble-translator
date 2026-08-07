#!/usr/bin/env python3
"""Compute derived token-efficiency metrics from the baseline reporter's live JSON.

Read-only, stdlib only. Input: JSON produced by tools/hermes_profile_token_baseline.py.
Output: derived ratios/aggregates per profile (and per source) printed as JSON.

No private data is read: the input JSON is already redacted (sha256-prefix ids,
no message fields). This script only re-combines the published aggregates.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _fmt(x: float) -> float:
    return round(x, 1)


def derived(profile: dict) -> dict:
    out = {}
    for group_name in ("all",):
        g = profile[group_name]
        inp = g["input_tokens"]["sum"]
        out_g = g["output_tokens"]["sum"]
        rea = g["reasoning_tokens"]["sum"]
        cr = g["cache_read_tokens"]["sum"]
        cw = g["cache_write_tokens"]["sum"]
        calls = g["api_calls"]["sum"]
        n = g["n_sessions"]
        out[group_name] = {
            "n_sessions": n,
            "calls_per_session_avg": _fmt(calls / n) if n else None,
            "reasoning_pct_of_input": _fmt(100.0 * rea / inp) if inp else None,
            "cache_read_vs_input_x": _fmt(cr / inp) if inp else None,
            "cache_write_sum": cw,
            "output_pct_of_input": _fmt(100.0 * out_g / inp) if inp else None,
            "visible_text_pct_of_output": _fmt(100.0 * (out_g - rea) / out_g) if out_g else None,
            "visible_text_sum": _fmt(out_g - rea),
        }
    # per-source aggregates for kanban vs non-kanban
    src = {}
    for name, g in profile["by_source"].items():
        inp = g["input_tokens"]["sum"]
        out_g = g["output_tokens"]["sum"]
        rea = g["reasoning_tokens"]["sum"]
        cr = g["cache_read_tokens"]["sum"]
        calls = g["api_calls"]["sum"]
        n = g["n_sessions"]
        src[name] = {
            "n_sessions": n,
            "api_calls": calls,
            "input_tokens": inp,
            "output_tokens": out_g,
            "reasoning_tokens": rea,
            "cache_read_tokens": cr,
            "cache_write_tokens": g["cache_write_tokens"]["sum"],
            "reasoning_pct_of_input": _fmt(100.0 * rea / inp) if inp else None,
            "output_pct_of_input": _fmt(100.0 * out_g / inp) if inp else None,
            "visible_text_sum": _fmt(out_g - rea),
            "calls_per_session_avg": _fmt(calls / n) if n else None,
            "input_p50": g["input_tokens"]["p50"],
            "input_p90": g["input_tokens"]["p90"],
        }
    out["by_source"] = src
    # cross-source share of totals
    tot_inp = profile["all"]["input_tokens"]["sum"]
    tot_calls = profile["all"]["api_calls"]["sum"]
    tot_rea = profile["all"]["reasoning_tokens"]["sum"]
    share = {}
    for name, g in profile["by_source"].items():
        share[name] = {
            "input_pct_of_total": _fmt(100.0 * g["input_tokens"]["sum"] / tot_inp) if tot_inp else None,
            "calls_pct_of_total": _fmt(100.0 * g["api_calls"]["sum"] / tot_calls) if tot_calls else None,
            "reasoning_pct_of_total": _fmt(100.0 * g["reasoning_tokens"]["sum"] / tot_rea) if tot_rea else None,
        }
    out["source_share"] = share
    # model/provider totals
    models = []
    for row in profile["usage_by_model"]:
        if row["billing_provider"] in ("auto", "") and row["billing_mode"] == "":
            continue  # dedup rows (per spec §9.4, sessions table is authoritative for totals)
        models.append(
            {
                "model": row["model"],
                "provider": row["billing_provider"],
                "mode": row["billing_mode"],
                "api_calls": row["api_call_count"],
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "reasoning_tokens": row["reasoning_tokens"],
                "cache_read_tokens": row["cache_read_tokens"],
                "cache_write_tokens": row["cache_write_tokens"],
                "sessions": row["sessions"],
            }
        )
    out["usage_by_model_dedup"] = models
    # finish_reason signals
    out["finish_reason"] = {r["finish_reason"]: r["count"] for r in profile["messages"]["finish_reason"]}
    out["top_tools"] = {r["tool"]: r["count"] for r in profile["messages"]["top_tools"]}
    out["end_reason"] = dict(profile["end_reason_distribution"])
    out["reasoning_effort"] = dict(profile["reasoning_effort_distribution"])
    return out


def main() -> int:
    src_path = Path(sys.argv[1])
    data = json.loads(src_path.read_text(encoding="utf-8"))
    result = {
        "generated_at_utc": data["generated_at_utc"],
        "profiles": {p: derived(prof) for p, prof in data["profiles"].items()},
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
