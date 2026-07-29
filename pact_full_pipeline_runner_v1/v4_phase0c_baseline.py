#!/usr/bin/env python3
"""v4 Phase 0C — baseline measurement (read-only).

Combines two independent data sources into one versioned result record:

  * Track A — chapter_046 / Phase 0B golden set.  A ``8-12 / 12-20 PID x
    right-context on/off`` grid (4 cells) over the v3 translation stage.
    Only ``verdict.status == "accepted"`` golden records feed numeric
    metrics.  ``needs_review`` records are excluded (documented limitation,
    not design).  ``known_violations`` is empty in every record, therefore
    semantic recall is not measurable this round — only the FP-candidate
    rate over accepted PIDs is.

  * Track B — an already-running/historic v3.1 production run of another
    chapter (run_full_pipeline_v31.ps1, different artifact layout from the
    0A plain-v3 harness).  Internal pipeline metrics (bad-repair, residual,
    deterministic integrity, time/tokens/reloads) are read read-only from
    the run artifacts, never by invoking models.

This module deliberately EXTENDS the Phase 0A measurement harness
(`v4_measurement_harness`) rather than duplicating it: shared helpers
(hashing, JSON read, word counting, run-identity) are imported from there.
No live model/pipeline invocation happens anywhere in this file.

Outputs contain aggregated metrics, hashes and identities only — never full
translation text (same boundary as Phase 0B / ``golden_sets``).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

# Re-use the Phase 0A harness helpers — do not parallel-implement them.
import v4_measurement_harness as h0a

SCHEMA_VERSION = "pact-v4-phase0c-result-record/v1"
TOOL_VERSION = "pact-0c/0.1"
UNKNOWN = h0a.UNKNOWN

PENDING_LIVE_RUN = "pending_live_run"
PENDING_RUN_COMPLETION = "pending_run_completion"
PENDING_DEFINITION = "pending_definition"
NOT_MEASURABLE = "not_measurable"
MEASURED = "measured"
NO_RUN = "no_run"

NEEDS_REVIEW_POLICY = (
    "needs_review records (43) are excluded from numeric Track A metrics. "
    "This is a sample limitation — those records have not reached a final "
    "verdict — not a conscious design choice. If they are later curated, "
    "the baseline must be recomputed."
)

FP_CANDIDATE_DEFINITION = (
    "Among the accepted golden PIDs, count how many v3 draft translation "
    "outputs violate a must_preserve invariant (a number from the source is "
    "absent/changed in the RU output, or a required inline span "
    "(tag,occurrence) is dropped).  No candidate selection exists in v3, so "
    "FP-candidate is operationalised as 'v3 draft fails a golden invariant'. "
    "fp_candidate_rate = violated_accepted_pids / accepted_pids. "
    "semantic recall is not measurable this round because known_violations "
    "is empty in every golden record."
)

_HEX_RE = re.compile(r"^[a-f0-9]{64}$")


GRID_CHUNK_LOW = "8_12"
GRID_CHUNK_HIGH = "12_20"
GRID_RC_ON = "on"
GRID_RC_OFF = "off"

# v3 config.v3.json ``chunking`` section levers.  right-context on/off maps
# to ``following_blocks`` 2 / 0.  chunk-size low/high maps to a scaled
# target/min/max_words band; the actually-achieved PID/chunk is itself a
# reported metric (we do not hard-claim the band is hit).
#
# The word bands below anchor 8-12 / 12-20 PID around the v3 default of ~900
# target words (config.v3.json): low band halves the window (denser chunks,
# ~8-12 PID), high band keeps the default sized window (~12-20 PID).  These
# are starting points for the future live runs; the runner re-derives the
# achieved band from the run manifest.
GRID_CONFIG = {
    (GRID_CHUNK_LOW, GRID_RC_ON): {
        "chunking": {
            "target_words": 450,
            "min_words": 280,
            "max_words": 640,
            "following_blocks": 2,
        }
    },
    (GRID_CHUNK_LOW, GRID_RC_OFF): {
        "chunking": {
            "target_words": 450,
            "min_words": 280,
            "max_words": 640,
            "following_blocks": 0,
        }
    },
    (GRID_CHUNK_HIGH, GRID_RC_ON): {
        "chunking": {
            "target_words": 900,
            "min_words": 550,
            "max_words": 1200,
            "following_blocks": 2,
        }
    },
    (GRID_CHUNK_HIGH, GRID_RC_OFF): {
        "chunking": {
            "target_words": 900,
            "min_words": 550,
            "max_words": 1200,
            "following_blocks": 0,
        }
    },
}

NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")

# --------------------------------------------------------------------------- #
# Golden set (Track A source)
# --------------------------------------------------------------------------- #
def sha256_file(path: Path) -> str:
    return h0a.sha256_file(path)


def load_golden_records(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} is not a golden-set records list")
    return data


def summarize_golden_source(records: list[dict[str, Any]], records_hash: str) -> dict[str, Any]:
    from collections import Counter

    verdict_dist = Counter(r.get("verdict", {}).get("status") for r in records)
    kv_populated = sum(1 for r in records if r.get("known_violations"))
    accepted = [r for r in records if r.get("verdict", {}).get("status") == "accepted"]
    needs_review = verdict_dist.get("needs_review", 0)
    chapter = records[0].get("chapter", UNKNOWN) if records else UNKNOWN
    return {
        "chapter_id": chapter,
        "records_hash_sha256": records_hash,
        "records_count": len(records),
        "accepted_count": len(accepted),
        "needs_review_excluded_count": needs_review,
        "rejected_count": verdict_dist.get("rejected", 0),
        "known_violations_populated_count": kv_populated,
        "semantic_recall": (
            {
                "status": NOT_MEASURABLE
                if kv_populated == 0
                else PENDING_LIVE_RUN,
                "reason": (
                    "known_violations empty in every golden record; "
                    "semantic recall needs known violations to compare "
                    "against the v3 draft detections."
                    if kv_populated == 0
                    else "requires a live v3 run to compare detections."
                ),
            }
        ),
        "needs_review_policy": NEEDS_REVIEW_POLICY,
    }


def accepted_golden_pids(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only verdict.status == 'accepted' records, preserving order."""
    return [r for r in records if r.get("verdict", {}).get("status") == "accepted"]


def _must_preserve_numbers(record: dict[str, Any]) -> set[str]:
    nums: set[str] = set()
    for inv in record.get("invariants", {}).get("must_preserve", []):
        if inv.get("kind") == "number":
            nums.add(str(inv.get("value", "")))
    return nums


def _required_spans(record: dict[str, Any]) -> list[tuple[str, str, int]]:
    spans: list[tuple[str, str, int]] = []
    for s in (
        record.get("invariants", {})
        .get("formatting_expectation", {})
        .get("required_spans", [])
    ):
        spans.append((str(s.get("tag", "")), str(s.get("span_id", "")), int(s.get("occurrence", 1))))
    return spans


def _output_for_pid(pid: str, output_map: dict[str, str]) -> str | None:
    if pid in output_map:
        return output_map[pid]
    return None


def evaluate_pid_against_golden(
    record: dict[str, Any], output_text: str | None
) -> dict[str, Any]:
    """Compute per-PID invariant violations of a v3 output vs one golden record.

    Returns a dict describing which must_preserve invariants the output holds.
    A PID absent from the v3 output is a hard gap, not a quiet skip.
    """
    pid = record["pid"]
    if output_text is None:
        return {
            "pid": pid,
            "present": False,
            "gap": True,
            "number_violations": [],
            "span_violations": [],
            "violated": True,
        }
    present_numbers = set(NUMBER_RE.findall(output_text))
    required_numbers = _must_preserve_numbers(record)
    missing_numbers = sorted(n for n in required_numbers if n not in present_numbers)

    span_violations: list[str] = []
    for tag, span_id, occurrence in _required_spans(record):
        pat = re.compile(
            rf"<{re.escape(tag)}\b[^>]*>.*?</{re.escape(tag)}>",
            re.IGNORECASE | re.DOTALL,
        )
        matches = pat.findall(output_text)
        if len(matches) < occurrence:
            span_violations.append(f"{tag}:{span_id}:occ{occurrence}")

    violated = bool(missing_numbers) or bool(span_violations)
    return {
        "pid": pid,
        "present": True,
        "gap": False,
        "number_violations": missing_numbers,
        "span_violations": span_violations,
        "violated": violated,
    }


def aggregate_track_a_cell(
    golden: list[dict[str, Any]],
    output_map: dict[str, str] | None,
) -> dict[str, Any]:
    """Build the metrics block for one grid cell.

    ``output_map`` is ``None`` when the cell's live run has not executed.
    """
    accepted = accepted_golden_pids(golden)
    if output_map is None:
        return {
            "status": PENDING_LIVE_RUN,
            "reason": "live v3 translation run for this grid cell not executed yet",
            "accepted_pids": len(accepted),
        }
    per_pid: list[dict[str, Any]] = []
    gaps: list[str] = []
    violated = 0
    for rec in accepted:
        out = _output_for_pid(rec["pid"], output_map)
        res = evaluate_pid_against_golden(rec, out)
        per_pid.append(res)
        if res["gap"]:
            gaps.append(res["pid"])
        if res["violated"] and not res["gap"]:
            violated += 1
    accepted_n = len(accepted)
    rate = (violated / accepted_n) if accepted_n else 0.0
    return {
        "status": MEASURED,
        "accepted_pids": accepted_n,
        "fp_candidate_rate": round(rate, 4),
        "violated_pids": violated,
        "missing_pids_gaps": gaps,
        "pid_results": per_pid,
    }

# --------------------------------------------------------------------------- #
# Grid recipe (Track A)
# --------------------------------------------------------------------------- #
def grid_cell_id(chunk_size: str, right_context: str) -> str:
    return f"{chunk_size}__rc_{right_context}"


def make_run_command(overrides: dict[str, Any], config_path: str, chapter: str) -> str:
    """Render the one-line command for a future live run (NOT executed here)."""
    fb = overrides["chunking"]["following_blocks"]
    tw = overrides["chunking"]["target_words"]
    mw = overrides["chunking"]["min_words"]
    xw = overrides["chunking"]["max_words"]
    return (
        "py ./pact_translate_v3.py"
        f" --config {config_path}"
        f" --phase translate --start {int(chapter)} --end {int(chapter)}"
        f"  # chunking: target_words={tw} min_words={mw} max_words={xw}"
        f" following_blocks={fb}"
    )


def build_grid(chapter_id: str, config_path: str = "config.v3.json") -> dict[str, Any]:
    cells: list[dict[str, Any]] = []
    for (chunk_size, rc), overrides in GRID_CONFIG.items():
        cell = {
            "cell_id": grid_cell_id(chunk_size, rc),
            "chunk_size": chunk_size,
            "right_context": rc,
            "config_overrides": overrides,
            "run_command": make_run_command(overrides, config_path, chapter_id),
            "status": PENDING_LIVE_RUN,
            "metrics": {},
        }
        cells.append(cell)
    return {
        "axes": {
            "chunk_size": [GRID_CHUNK_LOW, GRID_CHUNK_HIGH],
            "right_context": [GRID_RC_ON, GRID_RC_OFF],
        },
        "cells": cells,
    }


def attach_grid_metrics(grid: dict[str, Any], golden: list[dict[str, Any]], run_outputs: dict[str, dict[str, str] | None]) -> None:
    """Attach per-cell metrics.  ``run_outputs`` maps cell_id -> {pid: ru} or None."""
    for cell in grid["cells"]:
        out = run_outputs.get(cell["cell_id"])
        cell["metrics"] = aggregate_track_a_cell(golden, out)
        cell["status"] = cell["metrics"]["status"]
        if out is not None:
            cell["achieved_pid_per_chunk"] = {"status": MEASURED, "pids_in_output": len(out)}
        else:
            cell["achieved_pid_per_chunk"] = {"status": PENDING_LIVE_RUN}

# --------------------------------------------------------------------------- #
# Track B — v31 run import (read-only)
# --------------------------------------------------------------------------- #
def discover_track_b_chapter(run_root: Path) -> Path | None:
    """Find the single chapter work dir (v31 runs are one chapter here)."""
    work = run_root / "work"
    if not work.exists():
        return None
    chapters = sorted(
        [p for p in work.iterdir() if p.is_dir() and (p / "manifest.json").exists()],
        key=lambda p: h0a.natural_key(p.name),
    )
    return chapters[0] if chapters else None


def read_v31_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def v31_run_identity(run_root: Path) -> str:
    parts: dict[str, str] = {}
    for name in ("config.full_pipeline.v31.json", "chapter_manifest.v31.json", "book_bible.json"):
        p = run_root / name
        if p.exists():
            parts[name] = h0a.sha256_file(p)
    return h0a.canonical_json_hash(parts) if parts else UNKNOWN


def load_v31_lifecycle(chapter_dir: Path) -> list[dict[str, Any]]:
    return read_v31_json(chapter_dir / "v31" / "primary" / "lifecycle.json", [])


def load_v31_verification_report(chapter_dir: Path) -> dict[str, Any]:
    return read_v31_json(chapter_dir / "v31" / "primary" / "verification_report.json", {})


def load_v31_post_gate_deterministic(chapter_dir: Path) -> list[dict[str, Any]]:
    primary = chapter_dir / "v31" / "primary"
    decisions: list[dict[str, Any]] = []
    if not primary.exists():
        return decisions
    for path in sorted(primary.glob("post_gate_deterministic_round_*.json")):
        data = read_v31_json(path, {})
        if isinstance(data, dict):
            decs = data.get("decisions") or []
            if isinstance(decs, list):
                for d in decs:
                    if isinstance(d, dict):
                        d = dict(d)
                        d["_round"] = data.get("round")
                        decisions.append(d)
    return decisions


def load_v31_final_ledger(chapter_dir: Path) -> dict[str, Any]:
    return read_v31_json(chapter_dir / "v31_final_changed_pid_ledger.json", {})


def load_meta_translations(chapter_dir: Path) -> list[dict[str, Any]]:
    """Per-chunk translation-stage meta (tokens + timings)."""
    metas: list[dict[str, Any]] = []
    meta_dir = chapter_dir / "meta"
    if not meta_dir.exists():
        return metas
    for path in sorted(meta_dir.glob("*.translation.json")):
        data = read_v31_json(path, {})
        if isinstance(data, dict):
            data = dict(data)
            data["_file"] = path.name
            metas.append(data)
    return metas


def _summarize_meta_tokens(metas: list[dict[str, Any]]) -> dict[str, Any]:
    total_prompt = 0
    total_completion = 0
    total_tokens = 0
    wall = 0.0
    chunks = len(metas)
    attempts_total = 0
    for m in metas:
        for a in m.get("attempts", []) or []:
            if not isinstance(a, dict):
                continue
            attempts_total += 1
            gen = a.get("generation") if isinstance(a.get("generation"), dict) else {}
            usage = gen.get("usage") or a.get("usage") or {}
            total_prompt += int(usage.get("prompt_tokens") or 0)
            total_completion += int(usage.get("completion_tokens") or 0)
            total_tokens += int(usage.get("total_tokens") or 0)
            wall += float(gen.get("wall_seconds") or a.get("wall_seconds") or 0.0)
    return {
        "chunks": chunks,
        "attempts": attempts_total,
        "prompt_tokens": total_prompt,
        "completion_tokens": total_completion,
        "total_tokens": total_tokens,
        "wall_seconds": round(wall, 2),
    }


def import_track_b(run_root: Path) -> dict[str, Any]:
    """Read-only import of a v31 run into Track B metrics."""
    run_root = run_root.resolve()
    if not (run_root / "config.full_pipeline.v31.json").exists() and not (
        run_root / "chapter_manifest.v31.json"
    ).exists():
        return _track_b_no_run()

    chapter_dir = discover_track_b_chapter(run_root)
    if chapter_dir is None:
        return _track_b_no_run(run_identity=v31_run_identity(run_root))

    config = read_v31_json(run_root / "config.full_pipeline.v31.json", {})
    artifact_version = str(
        config.get("artifact_version") if isinstance(config, dict) else UNKNOWN
    ) or UNKNOWN
    monitor = read_v31_json(run_root / "monitor_state.v31.json", {})
    monitor_stage = str(monitor.get("stage") or UNKNOWN) if isinstance(monitor, dict) else UNKNOWN
    monitor_status = str(monitor.get("status") or UNKNOWN) if isinstance(monitor, dict) else UNKNOWN

    primary = chapter_dir / "v31" / "primary"
    primary_complete = (primary / "status.json").exists() and (primary / "lifecycle.json").exists()
    residual_dir = chapter_dir / "v31" / "residual"
    # Residual pass is adjudicated once its lifecycle.json appears; absence -> pending.
    residual_complete = (residual_dir / "lifecycle.json").exists()

    source = {
        "chapter_id": chapter_dir.name.split("_", 1)[0],
        "pipeline": "v31 (run_full_pipeline_v31.ps1)",
        "run_root_name": run_root.name,
        "run_identity": v31_run_identity(run_root),
        "artifact_version": artifact_version,
        "monitor_stage": monitor_stage,
        "monitor_status": monitor_status,
    }

    if primary_complete and residual_complete:
        completion_status = MEASURED
        completion_reason = "both primary and residual passes adjudicated"
    elif primary_complete:
        completion_status = PENDING_RUN_COMPLETION
        completion_reason = "primary pass complete; residual pass not adjudicated yet (lifecycle.json absent)"
    else:
        completion_status = PENDING_RUN_COMPLETION
        completion_reason = "primary pass not adjudicated yet"

    metrics: dict[str, Any] = {}
    # ---- PID coverage ----
    manifest = read_v31_json(chapter_dir / "manifest.json", {})
    blocks = manifest.get("blocks") if isinstance(manifest, dict) else None
    total_pids = len(blocks) if isinstance(blocks, list) else 0
    pt = read_v31_json(chapter_dir / "v31_primary_translations.json", {})
    covered = len(pt) if isinstance(pt, dict) else 0
    missing = (
        sorted(
            set(str(b.get("pid")) for b in blocks if isinstance(b, dict) and b.get("pid"))
            - set(str(k) for k in (pt.keys() if isinstance(pt, dict) else []))
        )
        if isinstance(blocks, list) and isinstance(pt, dict)
        else []
    )
    metrics["pid_coverage"] = {
        "status": MEASURED if total_pids else PENDING_RUN_COMPLETION,
        "covered": covered,
        "total": total_pids,
        "missing": missing,
    }

    # ---- lifecycle-derived residual / repair resolution (primary) ----
    lifecycle = load_v31_lifecycle(chapter_dir)
    verification = load_v31_verification_report(chapter_dir)
    from collections import Counter

    lc_status_dist = Counter(i.get("status") for i in lifecycle if isinstance(i, dict))
    vrep = verification.get("decisions") if isinstance(verification, dict) else None
    decision_dist: Counter[str] = Counter()
    if isinstance(vrep, list):
        for d in vrep:
            if isinstance(d, dict):
                decision_dist[str(d.get("decision"))] += 1
    residual_primary = int(lc_status_dist.get("resolved_retry_exhausted", 0))
    fp_primary = int(lc_status_dist.get("resolved_false_positive", 0))
    repaired_primary = int(lc_status_dist.get("resolved_repair", 0))
    total_lifecycle = len(lifecycle)
    metrics["residual_errors"] = {
        "status": MEASURED if primary_complete else PENDING_RUN_COMPLETION,
        "primary_total_issues": total_lifecycle,
        "primary_resolved_repair": repaired_primary,
        "primary_retry_exhausted": residual_primary,
        "primary_false_positive": fp_primary,
        "primary_keep_decisions": int(decision_dist.get("keep", 0)),
        "final_residual_total": (
            MEASURED if residual_complete else PENDING_RUN_COMPLETION
        ),
        "reason": (
            "final residual count requires the residual pass lifecycle (run ACTIVE)"
            if not residual_complete
            else ""
        ),
    }

    # ---- bad repair (deterministic gate on applied repair candidates) ----
    post_gate = load_v31_post_gate_deterministic(chapter_dir)
    ledger = load_v31_final_ledger(chapter_dir)
    changed_pids: list[str] = []
    if isinstance(ledger, dict):
        changed_pids = [str(p) for p in (ledger.get("changed_pids") or []) if isinstance(p, str)]
    gate_total = len(post_gate)
    gate_failed = 0
    introduced = 0
    failed_pids: set[str] = set()
    introduced_pids: set[str] = set()
    for d in post_gate:
        pid = str(d.get("pid"))
        if not d.get("passed", True):
            gate_failed += 1
            if pid:
                failed_pids.add(pid)
        intro = d.get("introduced_issues")
        if isinstance(intro, list) and intro:
            introduced += len(intro)
            if pid:
                introduced_pids.add(pid)
    # selected candidates = distinct PIDs actually accepted into the repair
    # ledger (stage=primary_repair). bad_repair_pids = those selected PIDs
    # whose deterministic post-gate saw a failed candidate or an introduced
    # issue — a conservative per-PID "repair did not come clean" signal.
    selected = len(changed_pids)
    selected_set = set(changed_pids)
    bad_pids = sorted((failed_pids | introduced_pids) & selected_set)
    bad_repair_rate = (len(bad_pids) / selected) if selected else 0.0
    metrics["bad_repair"] = {
        "status": MEASURED if primary_complete else PENDING_RUN_COMPLETION,
        "selected_repaired_pids": selected,
        "post_gate_decisions": gate_total,
        "post_gate_failed_decisions": gate_failed,
        "introduced_issues": introduced,
        "bad_repair_pids": bad_pids,
        "bad_repair_rate": round(bad_repair_rate, 4),
        "note": (
            "rate = |bad_repair_pids| / |selected_repaired_pids|, where "
            "bad_repair_pids are repaired PIDs whose deterministic post-gate "
            "saw a failed candidate or an introduced issue"
        ),
        "reason": "" if primary_complete else "primary adjudication not finalised",
    }

    # ---- deterministic integrity (PID coverage + post_gate pass aggregation) ----
    integrity_pass = gate_total - gate_failed
    metrics["deterministic_integrity"] = {
        "status": MEASURED if primary_complete else PENDING_RUN_COMPLETION,
        "pid_coverage": f"{covered}/{total_pids}",
        "post_gate_pass": integrity_pass,
        "post_gate_total": gate_total,
        "post_gate_fail": gate_failed,
        "remaining_required_categories": _collect_remaining_categories(post_gate),
    }

    # ---- russian rubric — Track A only by design (needs golden reference) ----
    metrics["russian_rubric"] = {
        "status": NOT_MEASURABLE,
        "reason": (
            "Russian rubric is a Track-A metric scored over accepted golden "
            "records (verdict/known_violations/invariants). Track B has no "
            "independent human reference for chapter 100 and must not be "
            "scored against a v3-reread v3 output (anchoring bias)."
        ),
    }

    # ---- LTCR — spec lists it but defines no numeric formula ----
    metrics["ltcr"] = {
        "status": PENDING_DEFINITION,
        "reason": (
            "V4_MVP_SPEC_RU.md lists LTCR (long-tail consistency / glossary "
            "term consistency) but defines no numeric formula. A structural "
            "proxy over glossary locked-term occurrence consistency is "
            "deferred to a future Phase 0C refinement; Track A variant also "
            "pending live run."
        ),
    }

    # ---- time / tokens / reloads ----
    metas = load_meta_translations(chapter_dir)
    tok = _summarize_meta_tokens(metas)
    # model reloads: not recorded in a single deterministic artifact here;
    # derivable from server_logs per-profile starts, which the harness does
    # not parse to avoid coupling to llama-server internals. Reported as
    # unknown pending a dedicated op-metric source.
    metrics["time_tokens"] = {
        "status": MEASURED if tok["chunks"] else PENDING_RUN_COMPLETION,
        "translation_stage": tok,
        "reloads": {"status": PENDING_DEFINITION, "reason": "requires server-log parsing; no single deterministic artifact"},
        "total_time_seconds": round(tok["wall_seconds"], 2) if tok["chunks"] else UNKNOWN,
        "reason": "" if tok["chunks"] else "translation meta not present",
    }

    return {
        "source": source,
        "completion": {
            "status": completion_status,
            "reason": completion_reason,
            "primary_pass_complete": primary_complete,
            "residual_pass_complete": residual_complete,
        },
        "metrics": metrics,
    }


def _collect_remaining_categories(post_gate: list[dict[str, Any]]) -> list[str]:
    cats: list[str] = []
    for d in post_gate:
        rem = d.get("remaining_required_categories")
        if isinstance(rem, list):
            cats.extend(str(c) for c in rem)
    return sorted(set(cats))


def _track_b_no_run(run_identity: str = UNKNOWN) -> dict[str, Any]:
    pending = {"status": NO_RUN, "reason": "no v31 run root found"}
    return {
        "source": {
            "chapter_id": UNKNOWN,
            "pipeline": "v31 (run_full_pipeline_v31.ps1)",
            "run_root_name": UNKNOWN,
            "run_identity": run_identity,
            "artifact_version": UNKNOWN,
            "monitor_stage": UNKNOWN,
            "monitor_status": UNKNOWN,
        },
        "completion": {
            "status": NO_RUN,
            "reason": "no v31 run root found",
            "primary_pass_complete": False,
        },
        "metrics": {
            "pid_coverage": dict(pending),
            "bad_repair": dict(pending),
            "residual_errors": dict(pending),
            "deterministic_integrity": dict(pending),
            "russian_rubric": {
                "status": NOT_MEASURABLE,
                "reason": "Track A only; needs golden reference",
            },
            "ltcr": {
                "status": PENDING_DEFINITION,
                "reason": "V4_MVP_SPEC defines no numeric LTCR formula",
            },
            "time_tokens": dict(pending),
        },
    }

# --------------------------------------------------------------------------- #
# Result record assembly
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_result_record(
    golden_path: Path | None,
    track_b_run_root: Path | None,
    track_a_run_outputs: dict[str, dict[str, str] | None] | None = None,
    chapter_id_for_grid: str = "046",
    config_path: str = "config.v3.json",
) -> dict[str, Any]:
    track_a: dict[str, Any]
    if golden_path is None:
        track_a = {
            "source": {
                "chapter_id": UNKNOWN,
                "records_hash_sha256": UNKNOWN,
                "records_count": 0,
                "accepted_count": 0,
                "needs_review_excluded_count": 0,
                "rejected_count": 0,
                "known_violations_populated_count": 0,
                "semantic_recall": {"status": NO_RUN, "reason": "no golden set provided"},
                "needs_review_policy": NEEDS_REVIEW_POLICY,
            },
            "grid": build_grid(chapter_id_for_grid, config_path),
            "fp_candidate_metric_definition": FP_CANDIDATE_DEFINITION,
            "aggregated": {"status": NO_RUN, "reason": "no golden set provided"},
        }
    else:
        records = load_golden_records(golden_path)
        records_hash = sha256_file(golden_path)
        src = summarize_golden_source(records, records_hash)
        grid = build_grid(src["chapter_id"], config_path)
        if track_a_run_outputs is not None:
            attach_grid_metrics(grid, records, track_a_run_outputs)
        agg_status = _grid_aggregated_status(grid)
        track_a = {
            "source": src,
            "grid": grid,
            "fp_candidate_metric_definition": FP_CANDIDATE_DEFINITION,
            "aggregated": {"status": agg_status},
        }

    track_b = import_track_b(track_b_run_root) if track_b_run_root is not None else _track_b_no_run()

    record = {
        "schema": SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "tool_version": TOOL_VERSION,
        "track_a": track_a,
        "track_b": track_b,
    }
    return record


def _grid_aggregated_status(grid: dict[str, Any]) -> str:
    statuses = {c.get("status") for c in grid.get("cells", [])}
    if statuses == {MEASURED}:
        return MEASURED
    if statuses == {PENDING_LIVE_RUN}:
        return PENDING_LIVE_RUN
    if MEASURED in statuses:
        return MEASURED
    return PENDING_LIVE_RUN


def validate_result_record(record: dict[str, Any]) -> list[str]:
    """Focused structural validation (no external dep)."""
    errors: list[str] = []
    if record.get("schema") != SCHEMA_VERSION:
        errors.append(f"schema: expected {SCHEMA_VERSION!r}, got {record.get('schema')!r}")
    for top in ("generated_at", "tool_version", "track_a", "track_b"):
        if top not in record:
            errors.append(f"missing top-level key: {top}")
    # track_a.source required subfields + policy non-empty
    ta = record.get("track_a", {})
    src = ta.get("source", {})
    for k in ("chapter_id", "records_hash_sha256", "records_count",
              "accepted_count", "needs_review_excluded_count",
              "rejected_count", "known_violations_populated_count",
              "semantic_recall", "needs_review_policy"):
        if k not in src:
            errors.append(f"track_a.source missing: {k}")
    if src.get("needs_review_policy", "") == "" and "needs_review_policy" in src:
        errors.append("track_a.source.needs_review_policy must not be empty")
    sr = src.get("semantic_recall", {})
    if not isinstance(sr, dict) or sr.get("status") not in (
        MEASURED, PENDING_LIVE_RUN, PENDING_DEFINITION, NOT_MEASURABLE, NO_RUN
    ):
        errors.append("track_a.source.semantic_recall.status invalid")
    # grid: exactly 4 cells
    grid = ta.get("grid", {})
    cells = grid.get("cells", [])
    if not isinstance(cells, list) or len(cells) != 4:
        errors.append("track_a.grid.cells must have exactly 4 cells")
    else:
        for c in cells:
            if c.get("status") not in (MEASURED, PENDING_LIVE_RUN):
                errors.append(f"grid cell {c.get('cell_id')} bad status {c.get('status')!r}")
    # fp definition present
    if not ta.get("fp_candidate_metric_definition"):
        errors.append("track_a.fp_candidate_metric_definition empty")
    # track_b
    tb = record.get("track_b", {})
    comp = tb.get("completion", {})
    if comp.get("status") not in (MEASURED, PENDING_RUN_COMPLETION, NO_RUN):
        errors.append("track_b.completion.status invalid")
    mtcs = tb.get("metrics", {})
    for k in ("pid_coverage", "bad_repair", "residual_errors",
              "deterministic_integrity", "russian_rubric", "ltcr", "time_tokens"):
        if k not in mtcs:
            errors.append(f"track_b.metrics missing: {k}")
        else:
            st = (mtcs[k] or {}).get("status")
            if st not in (MEASURED, PENDING_LIVE_RUN, PENDING_RUN_COMPLETION,
                          PENDING_DEFINITION, NOT_MEASURABLE, NO_RUN):
                errors.append(f"track_b.metrics.{k}.status invalid: {st!r}")
    # records hash shape when a real golden set is used
    rh = src.get("records_hash_sha256")
    if rh not in (None, UNKNOWN) and not _HEX_RE.match(str(rh)):
        errors.append("track_a.source.records_hash_sha256 not a sha256 hex")
    return errors


def write_result_record(record: dict[str, Any], out_path: Path) -> None:
    errors = validate_result_record(record)
    if errors:
        raise ValueError("result record invalid: " + "; ".join(errors))
    payload = json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=out_path.name + ".", suffix=".tmp", dir=str(out_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(payload)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, out_path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="v4 Phase 0C baseline (read-only)")
    p.add_argument("--golden", type=Path, help="path to golden set records.json (Track A)")
    p.add_argument("--track-b-run-root", type=Path, help="v31 run root to import (Track B)")
    p.add_argument("--out", type=Path, help="write assembled result record here")
    p.add_argument("--chapter", default="046", help="chapter id for grid recipe")
    p.add_argument("--config-path", default="config.v3.json", help="v3 config path for run command recipe")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    record = build_result_record(
        golden_path=args.golden,
        track_b_run_root=args.track_b_run_root,
        track_a_run_outputs=None,
        chapter_id_for_grid=args.chapter,
        config_path=args.config_path,
    )
    if args.out:
        write_result_record(record, args.out)
        print(f"wrote {args.out}")
    else:
        json.dump(record, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())