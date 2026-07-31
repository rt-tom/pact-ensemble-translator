#!/usr/bin/env python3
"""Two-pass Qwen-then-Gemma shadow re-selection for one existing sequential run.

Measurement Task A (V4_SINGLE_RESIDENT_DRIVER_ARCHITECTURE_RU.md, "Что
измерить до кода"). ``v4_phase12_sequential_run.py --phase select
--use-gemma-selector`` needs Qwen *and* Gemma reachable at once, because
``pact_v4.phase2.cascade.select_candidate`` calls Gemma inline, per chunk,
the moment a chunk has 2+ passing candidates -- there is no built-in point
where all Qwen work finishes before any Gemma work starts. On hardware
that can only keep one model resident at a time, that single-process call
cannot complete.

This script reimplements ``select_candidate``'s decision tree split across
two independent processes, handing off state through an intermediate JSON
file (``qwen_pass_state.json``) -- the same on-disk handoff pattern
``v4_phase12_sequential_runner.py`` itself uses for its generate/select
split. It deliberately does NOT reimplement any of the gate *logic*
(``deterministic_consistency_gate``, ``required_category_gate``,
``check_semantic_disagreement``, the Qwen/Gemma evaluator protocols) --
those are imported from ``pact_v4.phase2.cascade`` unchanged. Only the
*orchestration* (which stage runs in which pass) is duplicated here,
mirroring ``pact_v4.phase2.cascade.select_candidate`` stage-for-stage; if
that function's decision tree changes, this module's ``_qwen_pass_chunk``/
``_gemma_pass_chunk`` pair needs to be re-checked against it.

Usage (operator swaps the model loaded on their single llama-server
between the two invocations)::

    # Stage 1: only Qwen needs to be running.
    python -m pact_full_pipeline_runner_v1.v4_shadow_reselect_two_pass \\
        --stage qwen \\
        --out-dir "D:/pact/gate_bench_runs/v4_phase12_046_seq/shadow_reselect_001" \\
        --qwen-url http://127.0.0.1:8080/v1/chat/completions \\
        --qwen-model qwen-....gguf

    # <operator swaps llama-server's loaded model to Gemma here>

    # Stage 2: only Gemma needs to be running.
    python -m pact_full_pipeline_runner_v1.v4_shadow_reselect_two_pass \\
        --stage gemma \\
        --out-dir "D:/pact/gate_bench_runs/v4_phase12_046_seq/shadow_reselect_001" \\
        --gemma-url http://127.0.0.1:8080/v1/chat/completions \\
        --gemma-model gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf \\
        --run-label v4-shadow-reselect-046

``--out-dir`` must contain ``generation_bundle.json`` (copied from the
run being shadow-re-selected, e.g. ``draft_001``) and must NOT be that
run's own directory -- both stages only ever read/write inside
``--out-dir``, so the source run is never touched.

Stage ``gemma``'s output (``selection_results.json`` / ``translations.json``
/ ``provenance.json``) uses the same schema ``v4_phase12_sequential_run.py
--phase select`` produces, so any tool that reads that output (this
project's ``v4_shadow_reselect_compare.py`` included) needs no changes.
"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from pact_v4.phase1.models import Candidate, GateResult
from pact_v4.phase2.cascade import (
    DeterministicGateData,
    SelectionResult,
    check_semantic_disagreement,
    deterministic_consistency_gate,
    required_category_gate,
)
from pact_v4.pipeline.v4_phase12_sequential_runner import (
    GENERATION_BUNDLE_SCHEMA,
    PROVENANCE_SCHEMA,
    SequentialSelectConfig,
    SEQUENTIAL_MODEL_CAVEAT,
    _deserialize_candidate,
    _deserialize_risk,
    _record_selection,
    _serialize_candidate,
    _write_json,
)
from pact_v4.runtime.api_client import ApiClient, ApiClientConfig
from pact_v4.runtime.gemma_selector import HttpGemmaSelector, HttpGemmaSelectorConfig
from pact_v4.runtime.qwen_evaluator import HttpQwenEvaluator, HttpQwenEvaluatorConfig

LOG = logging.getLogger("v4_shadow_reselect_two_pass")

QWEN_PASS_STATE_SCHEMA = "pact-v4-shadow-reselect-qwen-pass-state/v1"


# ---------------------------------------------------------------------------
# Stage 1: Qwen pass -- stages 1/2/2b of select_candidate, per chunk.
# ---------------------------------------------------------------------------


def _qwen_pass_chunk(
    *,
    chunk_id: str,
    candidates: Sequence[Candidate],
    source_map: Mapping[str, str],
    qwen_evaluator,
    det_data: DeterministicGateData,
    risk,
) -> Dict[str, Any]:
    """Run stages 1 (Qwen fidelity), 2 (deterministic), 2b (required-category).

    Mirrors ``select_candidate``'s first half exactly (same gate calls, same
    failure bookkeeping). Returns a JSON-serialisable record: ``kind`` is
    ``"final"`` if the chunk's outcome is already decided without Gemma
    (quarantine / needs_synthesis / single-candidate select), or
    ``"pending_gemma"`` if 2+ candidates passed and Gemma preference is
    still needed.
    """
    candidates_by_role = {c.role: c for c in candidates}

    if not candidates:
        result = SelectionResult(
            chunk_id=chunk_id,
            quarantine=True,
            quarantine_reason="Empty candidate list — no candidates to evaluate.",
            candidates_evaluated=0,
        )
        return _final_record(result, candidates_by_role)

    traces: Dict[str, List[GateResult]] = {}
    failed: List[str] = []
    passed: List[Candidate] = []

    for candidate in candidates:
        translation = dict(candidate.translation)
        qwen_result = qwen_evaluator(source_map, translation)
        gate_trace = [qwen_result]
        if qwen_result.passed:
            traces[candidate.candidate_id] = gate_trace
        else:
            failed.append(candidate.candidate_id)

    for candidate in candidates:
        if candidate.candidate_id in failed:
            continue
        det_result = deterministic_consistency_gate(
            candidate=candidate, source=source_map, data=det_data,
        )
        traces[candidate.candidate_id].append(det_result)
        if not det_result.passed:
            failed.append(candidate.candidate_id)
            continue
        if risk is not None:
            required_result = required_category_gate(risk=risk, candidate=candidate)
            traces[candidate.candidate_id].append(
                GateResult(
                    gate="required_risk_categories",
                    passed=required_result.clean,
                    detail=(
                        "All required risk categories resolved."
                        if required_result.clean
                        else "Unresolved required categories: "
                        + ", ".join(required_result.unresolved_required)
                    ),
                )
            )
            if not required_result.clean:
                failed.append(candidate.candidate_id)
                continue
        passed.append(candidate)

    num_passed = len(passed)
    num_failed = len(failed)

    if num_passed == 0:
        reasons = []
        for cid in failed:
            reason = "Qwen fidelity fail"
            if cid in traces:
                for gate in traces[cid]:
                    if not gate.passed:
                        reason = f"{gate.gate}: {gate.detail[:200]}"
            reasons.append(f"{cid}: {reason}")
        result = SelectionResult(
            chunk_id=chunk_id,
            quarantine=True,
            quarantine_reason="No candidate passed both Qwen and deterministic gates. "
            + "; ".join(reasons),
            candidates_evaluated=len(candidates),
            candidates_failed=num_failed,
        )
        return _final_record(result, candidates_by_role)

    if num_passed == 1:
        winner = passed[0]
        trace = tuple(traces.get(winner.candidate_id, ()))
        result = SelectionResult(
            chunk_id=chunk_id,
            selected_candidate_id=winner.candidate_id,
            selected_role=winner.role,
            candidates_evaluated=len(candidates),
            candidates_passed=1,
            candidates_failed=num_failed,
            decision_trace=trace,
        )
        return _final_record(result, candidates_by_role)

    disagreement, dis_reason = check_semantic_disagreement(passed, source_map)
    has_synthesis = any(c.role == "synthesis" for c in passed)

    if disagreement and not has_synthesis:
        result = SelectionResult(
            chunk_id=chunk_id,
            needs_synthesis=True,
            synthesis_reason=f"Semantic disagreement: {dis_reason}",
            disagreement_detected=True,
            disagreement_reason=dis_reason,
            candidates_evaluated=len(candidates),
            candidates_passed=num_passed,
            candidates_failed=num_failed,
        )
        return _final_record(result, candidates_by_role)

    # 2+ passed, Gemma preference still needed -- defer to stage 2.
    return {
        "kind": "pending_gemma",
        "chunk_id": chunk_id,
        "disagreement_detected": disagreement,
        "disagreement_reason": dis_reason,
        "candidates_evaluated": len(candidates),
        "candidates_failed": num_failed,
        "passed_candidates": [_serialize_candidate(c) for c in passed],
        "traces": {
            cid: [{"gate": g.gate, "passed": g.passed, "detail": g.detail} for g in gates]
            for cid, gates in traces.items()
            if cid in {c.candidate_id for c in passed}
        },
        "candidates_by_role": {
            role: _serialize_candidate(c) for role, c in candidates_by_role.items()
        },
    }


def _final_record(
    result: SelectionResult, candidates_by_role: Mapping[str, Candidate],
) -> Dict[str, Any]:
    return {
        "kind": "final",
        "chunk_id": result.chunk_id,
        "result": {
            "chunk_id": result.chunk_id,
            "selected_candidate_id": result.selected_candidate_id,
            "selected_role": result.selected_role,
            "quarantine": result.quarantine,
            "quarantine_reason": result.quarantine_reason,
            "needs_synthesis": result.needs_synthesis,
            "synthesis_reason": result.synthesis_reason,
            "disagreement_detected": result.disagreement_detected,
            "disagreement_reason": result.disagreement_reason,
            "candidates_evaluated": result.candidates_evaluated,
            "candidates_passed": result.candidates_passed,
            "candidates_failed": result.candidates_failed,
            "decision_trace": [
                {"gate": g.gate, "passed": g.passed, "detail": g.detail}
                for g in result.decision_trace
            ],
        },
        "candidates_by_role": {
            role: _serialize_candidate(c) for role, c in candidates_by_role.items()
        },
    }


def run_qwen_pass(*, bundle_path: Path, out_dir: Path, qwen_evaluator) -> Path:
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    if bundle.get("schema") != GENERATION_BUNDLE_SCHEMA:
        raise ValueError(
            f"Foreign identity: generation bundle schema {bundle.get('schema')!r}, "
            f"expected {GENERATION_BUNDLE_SCHEMA!r}"
        )
    source_map: Dict[str, str] = dict(bundle["source"])
    det_data = DeterministicGateData()

    chunk_records: List[Dict[str, Any]] = []
    for outcome_record in bundle["outcomes"]:
        chunk_id = outcome_record["chunk_id"]
        risk_band = outcome_record["risk_band"]
        if outcome_record["status"] != "complete":
            chunk_records.append({
                "kind": "incomplete_generation",
                "chunk_id": chunk_id,
                "risk_band": risk_band,
                "expected_roles": list(outcome_record["expected_roles"]),
                "candidates_produced": list(outcome_record["candidates"]),
                "errors": dict(outcome_record.get("errors", {})),
            })
            continue

        candidates_by_role = {
            role: _deserialize_candidate(payload)
            for role, payload in outcome_record["candidates"].items()
        }
        candidates = list(candidates_by_role.values())
        risk_payload = outcome_record.get("risk")
        risk = _deserialize_risk(risk_payload) if risk_payload is not None else None

        try:
            record = _qwen_pass_chunk(
                chunk_id=chunk_id, candidates=candidates, source_map=source_map,
                qwen_evaluator=qwen_evaluator, det_data=det_data, risk=risk,
            )
        except Exception as exc:  # noqa: BLE001 -- mirrors run_select's own handling
            LOG.exception("Qwen pass raised for %s", chunk_id)
            record = {
                "kind": "final",
                "chunk_id": chunk_id,
                "result": {
                    "chunk_id": chunk_id,
                    "selected_candidate_id": None,
                    "selected_role": "",
                    "quarantine": True,
                    "quarantine_reason": f"cascade raised: {exc!r}",
                    "needs_synthesis": False,
                    "synthesis_reason": "",
                    "disagreement_detected": False,
                    "disagreement_reason": "",
                    "candidates_evaluated": len(candidates),
                    "candidates_passed": 0,
                    "candidates_failed": len(candidates),
                    "decision_trace": [],
                },
                "candidates_by_role": {
                    role: _serialize_candidate(c) for role, c in candidates_by_role.items()
                },
            }
        record["risk_band"] = risk_band
        chunk_records.append(record)

    state = {
        "schema": QWEN_PASS_STATE_SCHEMA,
        "generation_bundle_path": str(bundle_path),
        "chapter_id": bundle["chapter_id"],
        "identities": dict(bundle["identities"]),
        "generation_run_label": bundle.get("run_label"),
        "policy_versions": dict(bundle.get("policy_versions", {})),
        "provisional_params": dict(bundle.get("provisional_params", {})),
        "sequential_model_caveat": bundle.get("sequential_model_caveat", SEQUENTIAL_MODEL_CAVEAT),
        "chunks": chunk_records,
    }
    state_path = out_dir / "qwen_pass_state.json"
    _write_json(state_path, state)
    return state_path


# ---------------------------------------------------------------------------
# Stage 2: Gemma pass -- finishes "pending_gemma" chunks, writes final output.
# ---------------------------------------------------------------------------


def _gemma_finish_chunk(record: Mapping[str, Any], *, gemma_selector) -> SelectionResult:
    """Mirrors select_candidate's stage 6 (Gemma preference) exactly."""
    chunk_id = record["chunk_id"]
    passed = [_deserialize_candidate(p) for p in record["passed_candidates"]]
    disagreement = record["disagreement_detected"]
    dis_reason = record["disagreement_reason"]
    num_passed = len(passed)
    num_failed = record["candidates_failed"]
    candidates_evaluated = record["candidates_evaluated"]
    traces = {
        cid: tuple(GateResult(gate=g["gate"], passed=g["passed"], detail=g.get("detail", "")) for g in gates)
        for cid, gates in record["traces"].items()
    }

    gemma_input = [(c.candidate_id, dict(c.translation)) for c in passed]
    gemma_result = gemma_selector(gemma_input)
    if not gemma_result.passed:
        return SelectionResult(
            chunk_id=chunk_id,
            quarantine=True,
            quarantine_reason=(
                "Gemma Russian preference selector could not choose "
                f"among {num_passed} passing candidates. Reason: {gemma_result.detail}"
            ),
            disagreement_detected=disagreement,
            disagreement_reason=dis_reason,
            candidates_evaluated=candidates_evaluated,
            candidates_passed=num_passed,
            candidates_failed=num_failed,
        )
    preferred_id = gemma_result.detail

    matching = [c for c in passed if c.candidate_id == preferred_id]
    if not matching:
        return SelectionResult(
            chunk_id=chunk_id,
            quarantine=True,
            quarantine_reason=(
                f"Gemma-preferred candidate {preferred_id!r} not found "
                f"among the {num_passed} passing candidates."
            ),
            disagreement_detected=disagreement,
            disagreement_reason=dis_reason,
            candidates_evaluated=candidates_evaluated,
            candidates_passed=num_passed,
            candidates_failed=num_failed,
        )
    winner = matching[0]
    trace = traces.get(winner.candidate_id, ())
    return SelectionResult(
        chunk_id=chunk_id,
        selected_candidate_id=winner.candidate_id,
        selected_role=winner.role,
        disagreement_detected=disagreement,
        disagreement_reason=dis_reason,
        candidates_evaluated=candidates_evaluated,
        candidates_passed=num_passed,
        candidates_failed=num_failed,
        decision_trace=trace,
    )


def _selection_result_from_final(record: Mapping[str, Any]) -> SelectionResult:
    r = record["result"]
    return SelectionResult(
        chunk_id=r["chunk_id"],
        selected_candidate_id=r["selected_candidate_id"],
        selected_role=r["selected_role"] or "",
        quarantine=r["quarantine"],
        quarantine_reason=r["quarantine_reason"],
        needs_synthesis=r["needs_synthesis"],
        synthesis_reason=r["synthesis_reason"],
        disagreement_detected=r["disagreement_detected"],
        disagreement_reason=r["disagreement_reason"],
        candidates_evaluated=r["candidates_evaluated"],
        candidates_passed=r["candidates_passed"],
        candidates_failed=r["candidates_failed"],
        decision_trace=tuple(
            GateResult(gate=g["gate"], passed=g["passed"], detail=g.get("detail", ""))
            for g in r["decision_trace"]
        ),
    )


@dataclass
class GemmaPassResult:
    chapter_id: str
    out_dir: Path
    chunk_count: int
    selected_count: int
    quarantined_count: int
    needs_synthesis_count: int
    incomplete_generation_count: int
    selected_role_counts: Dict[str, int]
    translations_path: Path
    selection_path: Path
    provenance_path: Path


def run_gemma_pass(
    *, state_path: Path, out_dir: Path, gemma_selector, run_label: str,
    now: Optional[Any] = None,
) -> GemmaPassResult:
    now_fn = now or (lambda: datetime.now(timezone.utc))
    started_at = now_fn().isoformat(timespec="seconds")

    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("schema") != QWEN_PASS_STATE_SCHEMA:
        raise ValueError(
            f"Foreign identity: qwen pass state schema {state.get('schema')!r}, "
            f"expected {QWEN_PASS_STATE_SCHEMA!r}"
        )

    chapter_id = state["chapter_id"]
    identities = state["identities"]

    selection_records: List[Dict[str, Any]] = []
    final_text_by_pid: Dict[str, str] = {}
    selected_role_counts: Dict[str, int] = {}
    quarantined_count = 0
    needs_synthesis_count = 0
    incomplete_generation_count = 0

    for record in state["chunks"]:
        risk_band = record.get("risk_band", "")
        if record["kind"] == "incomplete_generation":
            incomplete_generation_count += 1
            selection_records.append({
                "chunk_id": record["chunk_id"],
                "status": "incomplete_generation",
                "risk_band": risk_band,
                "expected_roles": record["expected_roles"],
                "candidates_produced": record["candidates_produced"],
                "errors": record["errors"],
            })
            continue

        candidates_by_role = {
            role: _deserialize_candidate(payload)
            for role, payload in record["candidates_by_role"].items()
        }

        if record["kind"] == "final":
            result = _selection_result_from_final(record)
        elif record["kind"] == "pending_gemma":
            try:
                result = _gemma_finish_chunk(record, gemma_selector=gemma_selector)
            except Exception as exc:  # noqa: BLE001 -- mirrors run_select's own handling
                LOG.exception("Gemma pass raised for %s", record["chunk_id"])
                result = SelectionResult(
                    chunk_id=record["chunk_id"],
                    quarantine=True,
                    quarantine_reason=f"gemma stage raised: {exc!r}",
                    candidates_evaluated=record["candidates_evaluated"],
                    candidates_failed=record["candidates_failed"],
                )
        else:
            raise ValueError(f"Unknown qwen-pass record kind: {record['kind']!r}")

        q_delta, n_delta = _record_selection(
            selection_records=selection_records,
            final_text_by_pid=final_text_by_pid,
            selected_role_counts=selected_role_counts,
            result=result,
            risk_band=risk_band,
            candidates_by_role=candidates_by_role,
        )
        quarantined_count += q_delta
        needs_synthesis_count += n_delta

    translations_path = out_dir / "translations.json"
    _write_json(translations_path, final_text_by_pid)
    selection_path = out_dir / "selection_results.json"
    _write_json(selection_path, {
        "chapter_id": chapter_id,
        "snapshot_hash": identities["snapshot_hash"],
        "chunk_plan_hash": identities["chunk_plan_hash"],
        "config_identity": identities["config_identity"],
        "results": selection_records,
    })

    finished_at = now_fn().isoformat(timespec="seconds")
    chunk_count = len(state["chunks"])
    select_cfg = SequentialSelectConfig(
        generation_bundle_path=Path(state["generation_bundle_path"]),
        out_dir=out_dir,
        run_label=run_label,
    )
    select_config_artifact = select_cfg.to_config_artifact(
        model_profile="qwen-gemma-shadow-reselect-two-pass"
    )
    policy_versions = dict(state.get("policy_versions", {}))
    policy_versions.setdefault("reviewer_qwen_fidelity", "pact-v4-reviewer-qwen-fidelity/v1")
    policy_versions.setdefault(
        "reviewer_gemma_russian_preference", "pact-v4-reviewer-gemma-russian-preference/v1",
    )

    provenance: Dict[str, Any] = {
        "schema": PROVENANCE_SCHEMA,
        "run_label": run_label,
        "chapter_id": chapter_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "input": {
            "generation_bundle": state["generation_bundle_path"],
            "generation_run_label": state.get("generation_run_label"),
            "qwen_pass_state": str(state_path),
        },
        "identities": dict(identities),
        "select_config_identity": select_config_artifact.config_identity,
        "policy_versions": policy_versions,
        "provisional_params": dict(state.get("provisional_params", {})),
        "counts": {
            "chunks": chunk_count,
            "selected": sum(selected_role_counts.values()),
            "quarantined": quarantined_count,
            "needs_synthesis": needs_synthesis_count,
            "incomplete_generation": incomplete_generation_count,
            "selected_role_counts": dict(selected_role_counts),
        },
        "artefacts": {
            "generation_bundle": state["generation_bundle_path"],
            "qwen_pass_state": str(state_path),
            "selection_results": str(selection_path),
            "translations": str(translations_path),
        },
        "sequential_model_caveat": state.get("sequential_model_caveat", SEQUENTIAL_MODEL_CAVEAT),
        "shadow_reselect_two_pass_note": (
            "Produced by v4_shadow_reselect_two_pass.py, not "
            "v4_phase12_sequential_run.py --phase select. Qwen fidelity/"
            "deterministic/required-category gates ran in a separate "
            "process/pass from the Gemma Russian-preference call, because "
            "this hardware cannot keep both models resident at once. "
            "Selection outcome is equivalent to a single --use-gemma-selector "
            "run (same select_candidate decision tree, split across two "
            "passes with no logic changes) -- see this script's module "
            "docstring."
        ),
    }
    provenance_path = out_dir / "provenance.json"
    _write_json(provenance_path, provenance)

    return GemmaPassResult(
        chapter_id=chapter_id,
        out_dir=out_dir,
        chunk_count=chunk_count,
        selected_count=sum(selected_role_counts.values()),
        quarantined_count=quarantined_count,
        needs_synthesis_count=needs_synthesis_count,
        incomplete_generation_count=incomplete_generation_count,
        selected_role_counts=dict(selected_role_counts),
        translations_path=translations_path,
        selection_path=selection_path,
        provenance_path=provenance_path,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pact_full_pipeline_runner_v1.v4_shadow_reselect_two_pass",
        description=(
            "Two-pass Qwen-then-Gemma shadow re-selection over an existing "
            "generation_bundle.json, for hardware that cannot keep both "
            "models resident during a single select pass."
        ),
    )
    p.add_argument("--stage", choices=("qwen", "gemma"), required=True)
    p.add_argument(
        "--out-dir", type=Path, required=True,
        help="Empty/new directory containing a copy of generation_bundle.json. "
        "Must NOT be the source run's own directory.",
    )
    p.add_argument("--qwen-url", default="http://127.0.0.1:8080/v1/chat/completions")
    p.add_argument("--qwen-model", default="qwen.gguf")
    p.add_argument("--gemma-url", default="http://127.0.0.1:8080/v1/chat/completions")
    p.add_argument("--gemma-model", default="gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf")
    p.add_argument("--run-label", default="v4-shadow-reselect-two-pass")
    p.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_argparser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)

    if args.stage == "qwen":
        bundle_path = args.out_dir / "generation_bundle.json"
        if not bundle_path.exists():
            raise SystemExit(
                f"{bundle_path} not found -- copy generation_bundle.json from the "
                "source run into --out-dir first."
            )
        qwen_api = ApiClient(
            ApiClientConfig(chat_url=args.qwen_url, model=args.qwen_model),
            name="qwen-shadow-reselect-two-pass",
        )
        qwen_evaluator = HttpQwenEvaluator(
            api=qwen_api, config=HttpQwenEvaluatorConfig(api=qwen_api.config, label=qwen_api.name),
        )
        LOG.info("Starting Qwen pass: bundle=%s out=%s", bundle_path, args.out_dir)
        state_path = run_qwen_pass(
            bundle_path=bundle_path, out_dir=args.out_dir, qwen_evaluator=qwen_evaluator,
        )
        pending = sum(
            1 for c in json.loads(state_path.read_text(encoding="utf-8"))["chunks"]
            if c["kind"] == "pending_gemma"
        )
        LOG.info("Qwen pass finished: state=%s pending_gemma=%d", state_path, pending)
        print(json.dumps({
            "stage": "qwen", "out_dir": str(args.out_dir),
            "qwen_pass_state_path": str(state_path), "pending_gemma_chunks": pending,
        }, ensure_ascii=False, indent=2))
        return 0

    state_path = args.out_dir / "qwen_pass_state.json"
    if not state_path.exists():
        raise SystemExit(f"{state_path} not found -- run --stage qwen first.")
    gemma_api = ApiClient(
        ApiClientConfig(chat_url=args.gemma_url, model=args.gemma_model),
        name="gemma-shadow-reselect-two-pass",
    )
    gemma_selector = HttpGemmaSelector(
        api=gemma_api, config=HttpGemmaSelectorConfig(api=gemma_api.config, label=gemma_api.name),
    )
    LOG.info("Starting Gemma pass: state=%s out=%s", state_path, args.out_dir)
    result = run_gemma_pass(
        state_path=state_path, out_dir=args.out_dir, gemma_selector=gemma_selector,
        run_label=args.run_label,
    )
    LOG.info(
        "Gemma pass finished: chunks=%d selected=%d quarantined=%d needs_synthesis=%d "
        "incomplete_generation=%d role_counts=%s",
        result.chunk_count, result.selected_count, result.quarantined_count,
        result.needs_synthesis_count, result.incomplete_generation_count,
        result.selected_role_counts,
    )
    print(json.dumps({
        "stage": "gemma",
        "chapter_id": result.chapter_id,
        "out_dir": str(result.out_dir),
        "chunk_count": result.chunk_count,
        "selected_count": result.selected_count,
        "quarantined_count": result.quarantined_count,
        "needs_synthesis_count": result.needs_synthesis_count,
        "incomplete_generation_count": result.incomplete_generation_count,
        "selected_role_counts": result.selected_role_counts,
        "provenance_path": str(result.provenance_path),
        "translations_path": str(result.translations_path),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
