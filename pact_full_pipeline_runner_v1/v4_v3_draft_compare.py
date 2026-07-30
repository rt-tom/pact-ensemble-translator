#!/usr/bin/env python3
"""v4 vs v3 single-draft comparison (read-only).

Scores the output of the v4 Phase 1C->2A->2B->2C driver
(``pact_v4/pipeline/v4_phase12_draft_runner.py``, via
``v4_phase12_draft_run.py``) against the same chapter_046 golden-set
rubric already used for the v3 Phase 0C Track A grid, and assembles a
side-by-side result record.

This module does NOT reimplement the FP-candidate metric. The scoring
functions (``accepted_golden_pids``, ``evaluate_pid_against_golden``,
``aggregate_track_a_cell``) are imported unchanged from
``v4_phase0c_baseline`` (itself extending the Phase 0A
``v4_measurement_harness``). Only two things are new here:

  * a loader for the v4 driver's ``translations.json`` /
    ``provenance.json`` output shape (Track A's loader expects a
    different, v3-shaped, artifact layout and does not apply), and
  * assembly of a v3-vs-v4 comparison record around the *existing* v3
    Track A result record (``phase0c_result.json``) rather than a fresh
    v3 run.

No live model/pipeline invocation happens anywhere in this file, and no
full translation text is written to the output record (same boundary as
Track A / ``golden_sets``) -- only aggregated metrics, hashes, PIDs and
identities.

IMPORTANT -- the numbers this tool computes for the v4 side are only
meaningful if ``--v4-out-dir`` was produced by a REAL run of
``v4_phase12_draft_run.py`` against live ``--gemma-url``/``--qwen-url``
llama-server endpoints. The ``dry_run_*`` artifacts under
``gate_bench_runs/v4_phase12_046/`` were produced with stub
ModelCaller/QwenEvaluator/GemmaSelector adapters (no real model calls)
and exist only to exercise this tool's file-format handling -- their
FP-candidate numbers are meaningless and MUST NOT be used for the
benchmark gate decision.
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

# Re-use the Phase 0C baseline's metric functions -- do not parallel-implement them.
import v4_phase0c_baseline as p0c
import v4_measurement_harness as h0a

SCHEMA_VERSION = "pact-v4-v3-draft-comparison/v1"
TOOL_VERSION = "pact-v4v3-compare/0.1"
UNKNOWN = h0a.UNKNOWN

MEASURED = p0c.MEASURED
NOT_CHECKED = "not_checked"

V4_PROVENANCE_SCHEMA = "pact-v4-run-provenance/phase12/v1"
V3_RESULT_SCHEMA = "pact-v4-phase0c-result-record/v1"

DRY_RUN_CAVEAT = (
    "v4 numbers are only meaningful if --v4-out-dir was produced by a REAL "
    "(non-stub) run of v4_phase12_draft_run.py against live --gemma-url/"
    "--qwen-url llama-server endpoints. Artifacts produced with stub "
    "ModelCaller/QwenEvaluator/GemmaSelector adapters (e.g. the "
    "gate_bench_runs/v4_phase12_046/dry_run_* directories) exist only to "
    "exercise this tool's file-format handling; their FP-candidate rate "
    "MUST NOT be used for the benchmark gate decision."
)

SEMANTIC_RECALL_CAVEAT = (
    "semantic recall is not measurable this round because known_violations "
    "is empty in every golden record (same limitation as Track A)."
)

_CHAPTER_NUM_RE = re.compile(r"(\d+)")


# --------------------------------------------------------------------------- #
# v4 driver output loader
# --------------------------------------------------------------------------- #
def load_v4_run_outputs(out_dir: Path) -> dict[str, Any]:
    """Read ``translations.json`` + ``provenance.json`` from a v4 driver run.

    Returns a dict with the raw PID->text translations map plus the
    identity/policy/count fields from provenance, so callers can both
    score the output and report what config it came from.
    """
    out_dir = Path(out_dir)
    if not out_dir.is_dir():
        raise ValueError(f"v4 out-dir is not a directory: {out_dir}")

    translations_path = out_dir / "translations.json"
    provenance_path = out_dir / "provenance.json"
    translations = _read_required_json(translations_path)
    if not isinstance(translations, dict) or not all(
        isinstance(pid, str) and isinstance(text, str)
        for pid, text in translations.items()
    ):
        raise ValueError(
            f"v4 translations.json must be an object mapping PID to text: {translations_path}"
        )
    provenance = _read_required_json(provenance_path)
    if not isinstance(provenance, dict):
        raise ValueError(f"v4 provenance.json must be an object: {provenance_path}")
    schema = provenance.get("schema")
    if schema != V4_PROVENANCE_SCHEMA:
        raise ValueError(
            f"v4 provenance.json has unexpected schema {schema!r} "
            f"(expected {V4_PROVENANCE_SCHEMA!r}): {provenance_path}"
        )

    return {
        "out_dir": str(out_dir),
        "translations_path": str(translations_path),
        "provenance_path": str(provenance_path),
        "translations": translations,
        "translations_pid_count": len(translations),
        "chapter_id": str(provenance.get("chapter_id", UNKNOWN)),
        "identities": provenance.get("identities", {}),
        "policy_versions": provenance.get("policy_versions", {}),
        "provisional_params": provenance.get("provisional_params", {}),
        "counts": provenance.get("counts", {}),
    }


def _read_required_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise ValueError(f"required v4 artifact not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in v4 artifact {path}: {exc}") from exc


# --------------------------------------------------------------------------- #
# v3 side (already-computed Track A result record)
# --------------------------------------------------------------------------- #
def load_v3_cell(
    result_record_path: Path, cell_id: str | None
) -> dict[str, Any]:
    """Pick one grid cell's metrics out of an existing phase0c_result.json.

    ``cell_id`` may be omitted only when every measured cell reports the
    same fp_candidate_rate (the current state as of the 4-cell Track A
    grid) -- in that case the first measured cell is used and the
    ambiguity is recorded, not silently hidden. Otherwise an explicit
    cell_id is required.
    """
    record = _read_required_json(result_record_path)
    if not isinstance(record, dict):
        raise ValueError(f"v3 result record must be an object: {result_record_path}")
    if record.get("schema") != V3_RESULT_SCHEMA:
        raise ValueError(
            f"v3 result record has unexpected schema {record.get('schema')!r} "
            f"(expected {V3_RESULT_SCHEMA!r}): {result_record_path}"
        )
    cells = record.get("track_a", {}).get("grid", {}).get("cells", [])
    if not isinstance(cells, list) or not cells:
        raise ValueError(f"v3 result record has no track_a.grid.cells: {result_record_path}")
    measured = [c for c in cells if c.get("status") == MEASURED]
    if not measured:
        raise ValueError(
            f"no measured Track A cell in {result_record_path}; "
            "run the Track A grid before comparing against v4"
        )

    selection_note: str
    if cell_id is not None:
        chosen = next((c for c in measured if c.get("cell_id") == cell_id), None)
        if chosen is None:
            available = ", ".join(sorted(c.get("cell_id", UNKNOWN) for c in measured))
            raise ValueError(
                f"cell_id {cell_id!r} not found among measured Track A cells: {available}"
            )
        selection_note = f"explicit --v3-cell={cell_id}"
    else:
        rates = {c.get("cell_id"): c.get("metrics", {}).get("fp_candidate_rate") for c in measured}
        distinct_rates = set(rates.values())
        chosen = measured[0]
        if len(distinct_rates) == 1:
            selection_note = (
                f"auto-selected {chosen.get('cell_id')} (unambiguous: all "
                f"{len(measured)} measured cells report the same fp_candidate_rate "
                f"{next(iter(distinct_rates))}); pass --v3-cell to freeze a specific cell explicitly"
            )
        else:
            selection_note = (
                f"auto-selected {chosen.get('cell_id')} but measured cells DISAGREE "
                f"({rates!r}) -- pass --v3-cell explicitly to pick the cell the gate freezes"
            )

    src = record.get("track_a", {}).get("source", {})
    return {
        "result_record_path": str(result_record_path),
        "cell_id": chosen.get("cell_id"),
        "cell_selection": selection_note,
        "chunk_size": chosen.get("chunk_size"),
        "right_context": chosen.get("right_context"),
        "config_overrides": chosen.get("config_overrides"),
        "achieved_pid_per_chunk": chosen.get("achieved_pid_per_chunk"),
        "chapter_id": src.get("chapter_id", UNKNOWN),
        "metrics": chosen.get("metrics", {}),
        "all_measured_cell_ids": sorted(c.get("cell_id", UNKNOWN) for c in measured),
        "all_measured_fp_candidate_rates": {
            c.get("cell_id"): c.get("metrics", {}).get("fp_candidate_rate") for c in measured
        },
    }


# --------------------------------------------------------------------------- #
# Identity check
# --------------------------------------------------------------------------- #
def _chapter_number(chapter_id: str) -> str | None:
    m = _CHAPTER_NUM_RE.search(chapter_id or "")
    return m.group(1).lstrip("0") or "0" if m else None


def _uniform_provenance_source_hash(golden: list[dict[str, Any]]) -> str:
    hashes = {r.get("provenance", {}).get("source_hash") for r in golden}
    hashes.discard(None)
    if len(hashes) == 1:
        return next(iter(hashes))
    if not hashes:
        return UNKNOWN
    return "mixed"


def build_identity_check(
    golden: list[dict[str, Any]],
    golden_chapter_id: str,
    v3_chapter_id: str,
    v4_chapter_id: str,
    v4_identities: dict[str, Any],
    v3_manifest_source_hash: str | None,
    raw_chapter_html_sha256: str | None,
) -> dict[str, Any]:
    golden_source_hash = _uniform_provenance_source_hash(golden)

    v3_num = _chapter_number(v3_chapter_id)
    v4_num = _chapter_number(v4_chapter_id)
    golden_num = _chapter_number(golden_chapter_id)
    chapter_number_match = (
        v3_num is not None and v4_num is not None and golden_num is not None
        and v3_num == v4_num == golden_num
    )

    if v3_manifest_source_hash:
        golden_vs_v3 = golden_source_hash == v3_manifest_source_hash
    else:
        golden_vs_v3 = NOT_CHECKED

    if raw_chapter_html_sha256:
        golden_vs_raw = golden_source_hash == raw_chapter_html_sha256
        v4_vs_raw = v4_identities.get("source_hash") == raw_chapter_html_sha256
    else:
        golden_vs_raw = NOT_CHECKED
        v4_vs_raw = NOT_CHECKED

    return {
        "golden_chapter_id": golden_chapter_id,
        "v3_chapter_id": v3_chapter_id,
        "v4_chapter_id": v4_chapter_id,
        "chapter_number_match": chapter_number_match,
        "golden_source_file_hash_sha256": golden_source_hash,
        "v3_manifest_source_hash_sha256": v3_manifest_source_hash or UNKNOWN,
        "golden_vs_v3_manifest_source_hash_match": golden_vs_v3,
        "v4_declared_source_hash_sha256": v4_identities.get("source_hash", UNKNOWN),
        "v4_source_hash_note": (
            "v4 identities.source_hash is computed by the Phase 1 extraction "
            "pipeline (pact_v4/phase0b) over a different representation than "
            "v3's raw chapter-HTML file hash -- the two are not directly "
            "bit-comparable. Cross-checked here via chapter_number_match and, "
            "if --chapter-html was supplied, an independently recomputed raw "
            "sha256 of that file against both the golden set and v4's "
            "declared hash."
        ),
        "raw_chapter_html_sha256": raw_chapter_html_sha256 or NOT_CHECKED,
        "golden_vs_raw_chapter_html_hash_match": golden_vs_raw,
        "v4_declared_vs_raw_chapter_html_hash_match": v4_vs_raw,
    }


# --------------------------------------------------------------------------- #
# Result record assembly
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_comparison_record(
    golden_path: Path,
    v3_result_path: Path,
    v4_out_dir: Path,
    v3_cell_id: str | None = None,
    v3_manifest_path: Path | None = None,
    chapter_html_path: Path | None = None,
) -> dict[str, Any]:
    golden = p0c.load_golden_records(golden_path)
    golden_records_hash = p0c.sha256_file(golden_path)
    golden_summary = p0c.summarize_golden_source(golden, golden_records_hash)

    v3 = load_v3_cell(v3_result_path, v3_cell_id)

    v4_run = load_v4_run_outputs(v4_out_dir)
    v4_metrics = p0c.aggregate_track_a_cell(golden, v4_run["translations"])

    v3_manifest_source_hash = None
    if v3_manifest_path is not None:
        manifest = _read_required_json(v3_manifest_path)
        if isinstance(manifest, dict):
            v3_manifest_source_hash = manifest.get("source_sha256")

    raw_chapter_html_sha256 = None
    if chapter_html_path is not None:
        raw_chapter_html_sha256 = h0a.sha256_file(Path(chapter_html_path))

    identity_check = build_identity_check(
        golden=golden,
        golden_chapter_id=golden_summary["chapter_id"],
        v3_chapter_id=v3["chapter_id"],
        v4_chapter_id=v4_run["chapter_id"],
        v4_identities=v4_run["identities"],
        v3_manifest_source_hash=v3_manifest_source_hash,
        raw_chapter_html_sha256=raw_chapter_html_sha256,
    )

    v3_rate = v3["metrics"].get("fp_candidate_rate")
    v4_rate = v4_metrics.get("fp_candidate_rate")
    delta = (v4_rate - v3_rate) if isinstance(v3_rate, (int, float)) and isinstance(v4_rate, (int, float)) else UNKNOWN

    accepted = p0c.accepted_golden_pids(golden)

    record = {
        "schema": SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "tool_version": TOOL_VERSION,
        "golden": {
            "path": str(golden_path),
            **golden_summary,
        },
        "v3": {
            "result_record_path": v3["result_record_path"],
            "cell_id": v3["cell_id"],
            "cell_selection": v3["cell_selection"],
            "chunk_size": v3["chunk_size"],
            "right_context": v3["right_context"],
            "config_overrides": v3["config_overrides"],
            "achieved_pid_per_chunk": v3["achieved_pid_per_chunk"],
            "all_measured_cell_ids": v3["all_measured_cell_ids"],
            "all_measured_fp_candidate_rates": v3["all_measured_fp_candidate_rates"],
            "metrics": v3["metrics"],
        },
        "v4": {
            "out_dir": v4_run["out_dir"],
            "translations_path": v4_run["translations_path"],
            "provenance_path": v4_run["provenance_path"],
            "translations_pid_count": v4_run["translations_pid_count"],
            "identities": v4_run["identities"],
            "policy_versions": v4_run["policy_versions"],
            "provisional_params": v4_run["provisional_params"],
            "counts": v4_run["counts"],
            "metrics": v4_metrics,
        },
        "identity_check": identity_check,
        "comparison": {
            "accepted_pids_golden": len(accepted),
            "fp_candidate_rate_v3": v3_rate,
            "fp_candidate_rate_v4": v4_rate,
            "delta_v4_minus_v3": round(delta, 4) if isinstance(delta, (int, float)) else delta,
            "v3_missing_pids_gaps": v3["metrics"].get("missing_pids_gaps", []),
            "v4_missing_pids_gaps": v4_metrics.get("missing_pids_gaps", []),
            "fp_candidate_metric_definition": p0c.FP_CANDIDATE_DEFINITION,
        },
        "caveats": [
            DRY_RUN_CAVEAT,
            SEMANTIC_RECALL_CAVEAT,
            golden_summary["needs_review_policy"],
        ],
    }
    return record


def validate_comparison_record(record: dict[str, Any]) -> list[str]:
    """Focused structural validation (no external dep)."""
    errors: list[str] = []
    if record.get("schema") != SCHEMA_VERSION:
        errors.append(f"schema: expected {SCHEMA_VERSION!r}, got {record.get('schema')!r}")
    for top in ("generated_at", "tool_version", "golden", "v3", "v4", "identity_check", "comparison", "caveats"):
        if top not in record:
            errors.append(f"missing top-level key: {top}")
    v3_metrics = record.get("v3", {}).get("metrics", {})
    if v3_metrics.get("status") != MEASURED:
        errors.append(f"v3.metrics.status must be {MEASURED!r}, got {v3_metrics.get('status')!r}")
    v4_metrics = record.get("v4", {}).get("metrics", {})
    if v4_metrics.get("status") != MEASURED:
        errors.append(f"v4.metrics.status must be {MEASURED!r}, got {v4_metrics.get('status')!r}")
    comparison = record.get("comparison", {})
    for k in ("accepted_pids_golden", "fp_candidate_rate_v3", "fp_candidate_rate_v4",
              "delta_v4_minus_v3", "v3_missing_pids_gaps", "v4_missing_pids_gaps"):
        if k not in comparison:
            errors.append(f"comparison missing: {k}")
    if not record.get("caveats"):
        errors.append("caveats must not be empty")
    return errors


def write_comparison_record(record: dict[str, Any], out_path: Path) -> None:
    errors = validate_comparison_record(record)
    if errors:
        raise ValueError("comparison record invalid: " + "; ".join(errors))
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
    p = argparse.ArgumentParser(
        description="v3 (Track A) vs v4 (Phase 1-2C draft) single-draft comparison (read-only)"
    )
    p.add_argument("--golden", type=Path, required=True, help="path to golden set records.json")
    p.add_argument("--v3-result", type=Path, required=True, help="path to an existing Track A phase0c_result.json")
    p.add_argument("--v3-cell", default=None, help="Track A cell_id to use (required if measured cells disagree)")
    p.add_argument("--v3-manifest", type=Path, default=None,
                    help="optional path to a v3 Track A cell's work/<chapter>/manifest.json, "
                         "used only to cross-check source_sha256 against the golden set")
    p.add_argument("--v4-out-dir", type=Path, required=True,
                    help="v4 driver out-dir containing translations.json + provenance.json")
    p.add_argument("--chapter-html", type=Path, default=None,
                    help="optional path to the raw chapter HTML, used only to independently "
                         "verify the golden set's and v3's recorded source hash")
    p.add_argument("--out", type=Path, help="write assembled comparison record here")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    record = build_comparison_record(
        golden_path=args.golden,
        v3_result_path=args.v3_result,
        v4_out_dir=args.v4_out_dir,
        v3_cell_id=args.v3_cell,
        v3_manifest_path=args.v3_manifest,
        chapter_html_path=args.chapter_html,
    )
    if args.out:
        write_comparison_record(record, args.out)
        print(f"wrote {args.out}")
    else:
        errors = validate_comparison_record(record)
        if errors:
            raise SystemExit("comparison record invalid: " + "; ".join(errors))
        json.dump(record, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
