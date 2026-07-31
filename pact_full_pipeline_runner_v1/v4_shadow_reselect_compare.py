#!/usr/bin/env python3
"""Offline comparison: tie-break selection vs. real Qwen->Gemma cascade winner.

Measurement Task A, step 3 (V4_SINGLE_RESIDENT_DRIVER_ARCHITECTURE_RU.md,
"Что измерить до кода"). Purely read-only against both run directories --
writes only its own output report, never touches ``draft_001`` or the
shadow re-select directory's own artefacts.

For each chunk, compares:
  * the source run's ``generation_bundle.json`` -> outcome -> candidates
    -> ``fidelity_first`` PID-map (the DRAFT that generation used as
    unverified left context, per ``SEQUENTIAL_MODEL_CAVEAT``), against
  * the shadow re-select run's ``selection_results.json`` -> selected
    candidate's PID-map (the real Qwen->Gemma cascade winner, or a
    terminal non-selection state).

Usage::

    python -m pact_full_pipeline_runner_v1.v4_shadow_reselect_compare \\
        --draft-dir "D:/pact/gate_bench_runs/v4_phase12_046_seq/draft_001" \\
        --shadow-dir "D:/pact/gate_bench_runs/v4_phase12_046_seq/shadow_reselect_001" \\
        --out "D:/pact/gate_bench_runs/v4_phase12_046_seq/shadow_reselect_001/measurement_record.json"

    # Snapshot draft_001's artefact hashes before/after the shadow re-select
    # run, to verify it was never mutated:
    python -m pact_full_pipeline_runner_v1.v4_shadow_reselect_compare \\
        --hash-only "D:/pact/gate_bench_runs/v4_phase12_046_seq/draft_001"
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

DRAFT_ARTEFACTS = (
    "generation_bundle.json", "selection_results.json",
    "translations.json", "provenance.json",
)


def _sha256_file(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def hash_draft_dir(draft_dir: Path) -> Dict[str, Optional[str]]:
    return {name: _sha256_file(draft_dir / name) for name in DRAFT_ARTEFACTS}


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _chunk_order(bundle: Dict[str, Any]) -> List[str]:
    return [c["chunk_id"] for c in bundle["chunk_plan"]["chunks"]]


def compare(*, draft_dir: Path, shadow_dir: Path) -> Dict[str, Any]:
    draft_bundle = _load_json(draft_dir / "generation_bundle.json")
    draft_provenance = _load_json(draft_dir / "provenance.json")
    shadow_selection = _load_json(shadow_dir / "selection_results.json")
    shadow_provenance = _load_json(shadow_dir / "provenance.json")

    draft_identities = draft_provenance["identities"]
    shadow_identities = shadow_provenance["identities"]
    if draft_identities != shadow_identities:
        raise ValueError(
            "Identity mismatch between draft_dir and shadow_dir runs -- they "
            "do not share the same source/snapshot/chunk-plan/config. "
            f"draft={draft_identities} shadow={shadow_identities}"
        )

    outcomes_by_chunk = {o["chunk_id"]: o for o in draft_bundle["outcomes"]}
    shadow_by_chunk = {r["chunk_id"]: r for r in shadow_selection["results"]}
    order = _chunk_order(draft_bundle)

    rows: List[Dict[str, Any]] = []
    for chunk_id in order:
        outcome = outcomes_by_chunk.get(chunk_id)
        shadow = shadow_by_chunk.get(chunk_id)
        if outcome is None or shadow is None:
            rows.append({
                "chunk_id": chunk_id,
                "error": "missing from one of the two runs — cannot compare",
            })
            continue

        fidelity_first_payload = outcome.get("candidates", {}).get("fidelity_first")
        fidelity_first_available = (
            outcome["status"] == "complete" and fidelity_first_payload is not None
        )
        fidelity_first_map = (
            dict(fidelity_first_payload["translation"]) if fidelity_first_available else None
        )
        if fidelity_first_map is not None:
            fidelity_first_map = {pid: text for pid, text in fidelity_first_payload["translation"]}

        cascade_status = shadow["status"]  # selected | quarantined | needs_synthesis | incomplete_generation
        cascade_selected_role = shadow.get("selected_role") or None

        selected_map: Optional[Dict[str, str]] = None
        if cascade_status == "selected":
            selected_candidate_id = shadow.get("selected_candidate_id")
            for role, payload in outcome.get("candidates", {}).items():
                if payload.get("candidate_id") == selected_candidate_id:
                    selected_map = {pid: text for pid, text in payload["translation"]}
                    break

        role_match: Optional[bool] = None
        pid_map_mismatch: Optional[bool] = None
        if cascade_status == "selected" and fidelity_first_available:
            role_match = cascade_selected_role == "fidelity_first"
            if selected_map is not None and fidelity_first_map is not None:
                all_pids = set(fidelity_first_map) | set(selected_map)
                pid_map_mismatch = any(
                    fidelity_first_map.get(pid) != selected_map.get(pid) for pid in all_pids
                )

        # A chunk is "context-impacting" if the fidelity_first draft actually
        # fed forward as left_context during generation (SEQUENTIAL_MODEL_CAVEAT)
        # is NOT what the real cascade would have produced/validated:
        #   - cascade terminal state is not "selected" at all (quarantined /
        #     needs_synthesis / incomplete_generation) -- the draft used as
        #     context was never a validated winner, or
        #   - cascade selected a different role/text than fidelity_first.
        if cascade_status != "selected":
            context_impacting = fidelity_first_available  # nothing to compare if draft never existed either
        elif not fidelity_first_available:
            context_impacting = True
        else:
            context_impacting = bool(pid_map_mismatch)

        rows.append({
            "chunk_id": chunk_id,
            "fidelity_first_available": fidelity_first_available,
            "cascade_status": cascade_status,
            "cascade_selected_role": cascade_selected_role,
            "role_match": role_match,
            "pid_map_mismatch": pid_map_mismatch,
            "context_impacting": context_impacting,
        })

    total_chunks = len(rows)
    first_mismatch_index: Optional[int] = None
    for idx, row in enumerate(rows):
        if row.get("context_impacting"):
            first_mismatch_index = idx
            break
    suffix_length = (
        total_chunks - (first_mismatch_index + 1) if first_mismatch_index is not None else 0
    )

    selected_count = sum(1 for r in rows if r.get("cascade_status") == "selected")
    quarantined_count = sum(1 for r in rows if r.get("cascade_status") == "quarantined")
    needs_synthesis_count = sum(1 for r in rows if r.get("cascade_status") == "needs_synthesis")
    incomplete_count = sum(1 for r in rows if r.get("cascade_status") == "incomplete_generation")
    context_impacting_count = sum(1 for r in rows if r.get("context_impacting"))

    draft_counts = draft_provenance.get("counts", {})

    return {
        "schema": "pact-v4-shadow-reselect-comparison/v1",
        "chapter_id": draft_bundle["chapter_id"],
        "sources": {
            "draft_dir": str(draft_dir),
            "shadow_dir": str(shadow_dir),
            "draft_provenance": str(draft_dir / "provenance.json"),
            "shadow_provenance": str(shadow_dir / "provenance.json"),
        },
        "identities": dict(draft_identities),
        "per_chunk": rows,
        "first_context_impacting_mismatch": {
            "chunk_index": first_mismatch_index,
            "chunk_id": rows[first_mismatch_index]["chunk_id"] if first_mismatch_index is not None else None,
            "invalidated_suffix_length": suffix_length,
            "total_chunks": total_chunks,
        },
        "aggregate": {
            "shadow_reselect_run": {
                "total_chunks": total_chunks,
                "selected": selected_count,
                "quarantined": quarantined_count,
                "needs_synthesis": needs_synthesis_count,
                "incomplete_generation": incomplete_count,
                "context_impacting_mismatches": context_impacting_count,
                "divergence_rate": context_impacting_count / total_chunks if total_chunks else None,
                "quarantine_rate": quarantined_count / total_chunks if total_chunks else None,
                "needs_synthesis_rate": needs_synthesis_count / total_chunks if total_chunks else None,
            },
            "draft_run_tie_break": {
                "total_chunks": draft_counts.get("chunks"),
                "selected": draft_counts.get("selected"),
                "quarantined": draft_counts.get("quarantined"),
                "needs_synthesis": draft_counts.get("needs_synthesis"),
                "incomplete_generation": draft_counts.get("incomplete_generation"),
                "quarantine_rate": (
                    draft_counts["quarantined"] / draft_counts["chunks"]
                    if draft_counts.get("chunks") else None
                ),
                "needs_synthesis_rate": (
                    draft_counts["needs_synthesis"] / draft_counts["chunks"]
                    if draft_counts.get("chunks") else None
                ),
            },
        },
        "draft_dir_artefact_hashes_at_comparison_time": hash_draft_dir(draft_dir),
        "non_goals": [
            "Does not predict later waves of a speculative/windowed driver — "
            "once a suffix is regenerated with corrected left context, its "
            "own candidates can change again. This measures only the first "
            "wave.",
            "Does not measure lifecycle/reload cost or boundary-rubric quality.",
        ],
    }


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pact_full_pipeline_runner_v1.v4_shadow_reselect_compare",
        description="Offline, read-only comparison of a tie-break run vs. its shadow re-select run.",
    )
    p.add_argument("--draft-dir", type=Path, help="Original sequential run dir (e.g. draft_001). Read-only.")
    p.add_argument("--shadow-dir", type=Path, help="Shadow re-select run dir (e.g. shadow_reselect_001). Read-only.")
    p.add_argument("--out", type=Path, default=None, help="Output report path (default: <shadow-dir>/measurement_record.json).")
    p.add_argument("--hash-only", type=Path, default=None, help="Print artefact hashes for a draft dir and exit (no comparison).")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_argparser().parse_args(argv)

    if args.hash_only is not None:
        print(json.dumps(hash_draft_dir(args.hash_only), indent=2))
        return 0

    if args.draft_dir is None or args.shadow_dir is None:
        raise SystemExit("--draft-dir and --shadow-dir are required (or use --hash-only)")

    report = compare(draft_dir=args.draft_dir, shadow_dir=args.shadow_dir)
    out_path = args.out or (args.shadow_dir / "measurement_record.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    agg = report["aggregate"]["shadow_reselect_run"]
    print(json.dumps({
        "report_path": str(out_path),
        "total_chunks": agg["total_chunks"],
        "selected": agg["selected"],
        "quarantined": agg["quarantined"],
        "needs_synthesis": agg["needs_synthesis"],
        "context_impacting_mismatches": agg["context_impacting_mismatches"],
        "divergence_rate": agg["divergence_rate"],
        "first_context_impacting_mismatch": report["first_context_impacting_mismatch"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
