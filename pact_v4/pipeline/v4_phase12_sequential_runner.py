"""Sequential-model (two-pass) variant of the Phase 1C→2A→2B→2C driver.

``pact_v4.pipeline.v4_phase12_draft_runner.run_chapter`` interleaves
Phase 2B (Gemma generation) and Phase 2C (Qwen/Gemma cascaded selection)
per chunk, because chunk N+1's ``left_context`` must come from chunk N's
*cascade winner*, not from an unverified draft (see that module's
``run_chapter`` docstring). That is the correct behaviour when Gemma and
Qwen can both be kept resident at once.

On single-GPU hardware where Gemma (~17GB) and Qwen (~22GB) cannot be
served concurrently, true interleaving would mean reloading a model on
every chunk boundary — dozens of reloads per chapter, impractical in
wall-clock time. This module instead splits the chapter run into two
independent passes, with state handed off through a JSON artifact on
disk (the same pattern ``pact_translate_v3.py`` already uses for its
``--phase translate|audit|repair|finalize`` split):

* ``run_generate`` — needs only Gemma. Runs Phase 1C (chunk plan) and
  Phase 2A (risk pre-screen) as before, then Phase 2B chunk-by-chunk.
  Selection does not exist yet at this point, so chunk N+1's
  ``left_context`` is built from chunk N's **``fidelity_first`` DRAFT**
  (the first candidate ``generate_for_chunk`` produces), not a
  cascade-verified winner. This is a deliberate measurement
  approximation for this benchmark gate only — see
  ``SEQUENTIAL_MODEL_CAVEAT`` below and ``run_chapter``'s docstring for
  why it is explicitly NOT the intended v4 production behaviour.

* ``run_select`` — needs only Qwen (Gemma optionally, if it happens to
  still be resident). Reads the ``generate`` pass's self-contained
  ``generation_bundle.json`` (source text, chunk plan, per-chunk
  ``RiskAssessment``, and every candidate ``generate_for_chunk``
  produced) and runs ``pact_v4.phase2.cascade.select_candidate`` per
  chunk -- including its ``risk=`` argument, so the required-risk-
  category resolution gate (``number_word``/``tone_profanity``, see
  ``pact_v4.phase2.cascade.required_category_gate``) actually runs
  here too, not just in the interleaved driver. Selection is provably
  order-independent: ``select_candidate`` takes the static ``source``
  (English text) and the chunk's own candidates, never the previous
  chunk's translation, so chunks can be selected in any order or even
  in parallel.

The output of ``run_select`` (``translations.json`` + ``provenance.json``)
uses the same schema as ``v4_phase12_draft_runner.run_chapter``'s output,
so ``v4_v3_draft_compare.py`` reads it unchanged.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from pact_v4.phase0b.source_html import load_source
from pact_v4.phase1.chunker import (
    DEFAULT_MAX_WORDS,
    DEFAULT_MIN_WORDS,
    DEFAULT_TARGET_WORDS,
    ChunkPlanner,
)
from pact_v4.phase1.models import (
    Candidate,
    ChunkPlanArtifact,
    ConfigArtifact,
    GateResult,
    SourceArtifact,
)
from pact_v4.phase2.cascade import (
    DeterministicGateData,
    SelectionResult,
    select_candidate,
)
from pact_v4.phase2.generation import (
    GenerationCache,
    GenerationParams,
    ModelCaller,
    generate_for_chunk,
)
from pact_v4.phase2.risk import RiskAssessment, RiskBand, RiskFeature, assess_source_risk
from pact_v4.pipeline.v4_phase12_draft_runner import _glossary_entries
from pact_v4.runtime.snapshot_factory import (
    ChapterMemory,
    build_config_artifact,
    build_snapshot,
    build_source_artifact,
)

LOG = logging.getLogger(__name__)

GENERATION_BUNDLE_SCHEMA = "pact-v4-sequential-generation-bundle/v1"
PROVENANCE_SCHEMA = "pact-v4-run-provenance/phase12/v1"

SEQUENTIAL_MODEL_CAVEAT = (
    "left_context during generation used the unverified fidelity_first "
    "draft, not the cascade-verified winner, because Gemma and Qwen "
    "could not be served concurrently on this hardware. This is a "
    "measurement approximation for the v3/v4 benchmark gate only -- NOT "
    "representative of the intended v4 production behavior (see "
    "pact_v4.pipeline.v4_phase12_draft_runner.run_chapter docstring)."
)


# ---------------------------------------------------------------------------
# Configs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SequentialGenerateConfig:
    """Everything ``run_generate`` needs; Gemma-only, no Qwen involved."""

    chapter_id: str
    chapter_html_path: Path
    memory_dir: Path
    out_dir: Path
    min_chunk_words: int = DEFAULT_MIN_WORDS
    target_chunk_words: int = DEFAULT_TARGET_WORDS
    max_chunk_words: int = DEFAULT_MAX_WORDS
    right_context_pids: int = 0
    temperature: float = 0.2
    seed: int = 7
    max_tokens: int = 8192
    config_version: str = "pact-v4-driver/phase12/sequential-generate/v1"
    run_label: str = "v4-phase12-sequential-generate"

    def to_config_artifact(self, *, model_profile: str) -> ConfigArtifact:
        return build_config_artifact(
            version=self.config_version,
            values={
                "chapter_id": self.chapter_id,
                "model_profile": model_profile,
                "chunk_min_words": self.min_chunk_words,
                "chunk_target_words": self.target_chunk_words,
                "chunk_max_words": self.max_chunk_words,
                "right_context_pids": self.right_context_pids,
                "generation": {
                    "temperature": self.temperature,
                    "seed": self.seed,
                    "max_tokens": self.max_tokens,
                    "reasoning": 0,
                },
            },
        )


@dataclass(frozen=True)
class SequentialSelectConfig:
    """Everything ``run_select`` needs; Qwen (+ optional Gemma), no chunker."""

    generation_bundle_path: Path
    out_dir: Path
    deterministic_glossary_terms: Tuple[Tuple[str, str], ...] = ()
    deterministic_names: Tuple[Tuple[str, str], ...] = ()
    deterministic_mixed_script_allow: Tuple[str, ...] = ()
    config_version: str = "pact-v4-driver/phase12/sequential-select/v1"
    run_label: str = "v4-phase12-sequential-select"

    def to_config_artifact(self, *, model_profile: str) -> ConfigArtifact:
        return build_config_artifact(
            version=self.config_version,
            values={
                "model_profile": model_profile,
                "deterministic_glossary_terms": list(self.deterministic_glossary_terms),
                "deterministic_names": list(self.deterministic_names),
                "deterministic_mixed_script_allow": list(self.deterministic_mixed_script_allow),
            },
        )


# ---------------------------------------------------------------------------
# Candidate (de)serialisation -- the hand-off contract between the two passes
# ---------------------------------------------------------------------------


def _serialize_candidate(candidate: Candidate) -> Dict[str, Any]:
    """Render one ``Candidate`` with every field its constructor needs.

    Unlike ``v4_phase12_draft_runner._serialize_generation_outcome`` (which
    only needs enough to describe *what happened* for a human/comparison
    tool reading ``generation_outcomes.json``), this must carry enough to
    reconstruct a real ``Candidate`` object on the ``select`` pass, which
    may run in a different process at a different time.
    """
    return {
        "candidate_id": candidate.candidate_id,
        "chunk_id": candidate.chunk_id,
        "role": candidate.role,
        "translation": [list(pair) for pair in candidate.translation],
        "source_hash": candidate.source_hash,
        "snapshot_hash": candidate.snapshot_hash,
        "chunk_plan_hash": candidate.chunk_plan_hash,
        "config_identity": candidate.config_identity,
        "decision_trace": [
            {"gate": g.gate, "passed": g.passed, "detail": g.detail}
            for g in candidate.decision_trace
        ],
    }


def _deserialize_candidate(payload: Mapping[str, Any]) -> Candidate:
    """Reconstruct a ``Candidate`` from ``_serialize_candidate``'s output.

    This is a plain (validated) construction, not ``Candidate.create``:
    the identity hashes were legitimately produced during ``run_generate``
    and are carried through verbatim, not fabricated here. ``__post_init__``
    still enforces the structural invariants (role, unique PIDs, hash
    shape); it is ``select_candidate`` that actually uses the result, and
    it never calls ``validate_against`` against a live Source/Snapshot/
    ChunkPlan (see ``pact_v4.phase2.cascade.select_candidate``).
    """
    return Candidate(
        candidate_id=payload["candidate_id"],
        chunk_id=payload["chunk_id"],
        role=payload["role"],
        translation=tuple((pid, text) for pid, text in payload["translation"]),
        source_hash=payload["source_hash"],
        snapshot_hash=payload["snapshot_hash"],
        chunk_plan_hash=payload["chunk_plan_hash"],
        config_identity=payload["config_identity"],
        decision_trace=tuple(
            GateResult(gate=g["gate"], passed=g["passed"], detail=g.get("detail", ""))
            for g in payload.get("decision_trace", [])
        ),
    )


def _serialize_risk(risk: RiskAssessment) -> Dict[str, Any]:
    """Render one chunk's ``RiskAssessment`` for the generation bundle.

    Needed so ``run_select`` can pass ``risk=`` into ``select_candidate``
    and get the required-risk-category resolution gate (Phase 2C, gate
    ``required_risk_categories`` -- see ``pact_v4.phase2.cascade.
    required_category_gate``) -- without this, that gate silently no-ops
    (``select_candidate(risk=None)`` skips stage 2b entirely).
    """
    return {
        "policy_version": risk.policy_version,
        "band": risk.band.value,
        "score": risk.score,
        "features": [
            {
                "code": f.code,
                "weight": f.weight,
                "explanation": f.explanation,
                "evidence": list(f.evidence),
            }
            for f in risk.features
        ],
    }


def _deserialize_risk(payload: Mapping[str, Any]) -> RiskAssessment:
    return RiskAssessment(
        policy_version=payload["policy_version"],
        band=RiskBand(payload["band"]),
        score=payload["score"],
        features=tuple(
            RiskFeature(
                code=f["code"],
                weight=f["weight"],
                explanation=f["explanation"],
                evidence=tuple(f.get("evidence", ())),
            )
            for f in payload.get("features", [])
        ),
    )


# ---------------------------------------------------------------------------
# Pass 1: generate (Gemma only)
# ---------------------------------------------------------------------------


def _left_ru_from_draft(
    *,
    chunk_index: int,
    chunk_plan: ChunkPlanArtifact,
    draft_text_by_chunk: Mapping[str, Dict[str, str]],
) -> Tuple[Tuple[str, str], ...]:
    """Sequential-model left_context: previous chunk's fidelity_first DRAFT.

    Deliberately not the cascade winner -- selection has not happened yet
    on this pass. See ``SEQUENTIAL_MODEL_CAVEAT``. Empty when there is no
    previous chunk, or the previous chunk's ``fidelity_first`` role failed
    to generate a valid candidate at all.
    """
    if chunk_index <= 0:
        return ()
    prev_chunk_id = chunk_plan.chunks[chunk_index - 1].chunk_id
    prev_draft = draft_text_by_chunk.get(prev_chunk_id)
    if not prev_draft:
        return ()
    prev_chunk = chunk_plan.chunk(prev_chunk_id)
    return tuple(
        (pid, prev_draft[pid])
        for pid in prev_chunk.pids
        if pid in prev_draft and prev_draft[pid]
    )


@dataclass
class GenerateRunResult:
    chapter_id: str
    out_dir: Path
    chunk_count: int
    chunk_plan_path: Path
    risk_path: Path
    generation_bundle_path: Path
    bundle: Dict[str, Any]


def run_generate(
    cfg: SequentialGenerateConfig,
    *,
    model_caller: ModelCaller,
    now: Optional[Callable[[], datetime]] = None,
) -> GenerateRunResult:
    """Phase 1C -> 2A -> 2B for one chapter; Gemma is the only model needed.

    Writes ``chunk_plan.json`` and ``risk_classification.json`` in the
    same shape ``v4_phase12_draft_runner`` uses (for human inspection),
    plus a self-contained ``generation_bundle.json`` that ``run_select``
    consumes -- it carries the source text, the chunk plan, every
    candidate produced, and the run's identities, so the ``select`` pass
    needs no other input file.
    """
    now_fn = now or (lambda: datetime.now(timezone.utc))
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    started_at = now_fn().isoformat(timespec="seconds")

    blocks, _raw_sha = load_source(cfg.chapter_html_path)
    if not blocks:
        raise ValueError(f"Chapter {cfg.chapter_id}: no source blocks parsed")

    source = build_source_artifact(chapter_id=cfg.chapter_id, blocks=blocks)
    memory = ChapterMemory.from_directory(cfg.memory_dir)
    snapshot = build_snapshot(
        chapter_id=cfg.chapter_id,
        source=source,
        memory=memory,
        context=f"chapter_html={cfg.chapter_html_path};memory_dir={cfg.memory_dir}",
    )
    config = cfg.to_config_artifact(model_profile="gemma-4-26B-A4B-it-UD-Q4_K_XL")

    planner = ChunkPlanner(
        target_words=cfg.target_chunk_words,
        min_words=cfg.min_chunk_words,
        max_words=cfg.max_chunk_words,
    )
    plans = planner.plan(
        blocks,
        snapshot_hash=snapshot.snapshot_hash,
        following_blocks=cfg.right_context_pids,
    )
    if not plans:
        raise ValueError(f"Chapter {cfg.chapter_id}: planner returned no chunks")
    chunk_plan = ChunkPlanArtifact.create(snapshot, tuple(plans))
    chunk_plan_path = cfg.out_dir / "chunk_plan.json"
    _write_json(chunk_plan_path, chunk_plan.to_payload())

    glossary = _glossary_entries(memory)
    source_map = dict(source.source)
    risk_records: List[Dict[str, Any]] = []
    risk_by_chunk: Dict[str, RiskAssessment] = {}
    for plan_chunk in chunk_plan.chunks:
        rows = tuple((pid, source_map.get(pid, "")) for pid in plan_chunk.pids)
        assessment = assess_source_risk(rows, glossary=glossary, source_complete=True)
        risk_by_chunk[plan_chunk.chunk_id] = assessment
        risk_records.append({
            "chunk_id": plan_chunk.chunk_id,
            "pids": list(plan_chunk.pids),
            "policy_version": assessment.policy_version,
            "band": assessment.band.value,
            "score": assessment.score,
            "features": [
                {
                    "code": f.code,
                    "weight": f.weight,
                    "explanation": f.explanation,
                    "evidence": list(f.evidence),
                }
                for f in assessment.features
            ],
        })
    risk_path = cfg.out_dir / "risk_classification.json"
    _write_json(risk_path, {
        "chapter_id": cfg.chapter_id,
        "snapshot_hash": snapshot.snapshot_hash,
        "policy_version": "pact-v4-risk-source-en/v1",
        "thresholds": {"medium": 3, "high": 7},
        "records": risk_records,
    })

    generation_params = GenerationParams(
        temperature=cfg.temperature, seed=cfg.seed, max_tokens=cfg.max_tokens,
    )
    gen_cache = GenerationCache()
    draft_text_by_chunk: Dict[str, Dict[str, str]] = {}
    outcome_records: List[Dict[str, Any]] = []

    for index, plan_chunk in enumerate(chunk_plan.chunks):
        risk = risk_by_chunk[plan_chunk.chunk_id]
        left_context = _left_ru_from_draft(
            chunk_index=index,
            chunk_plan=chunk_plan,
            draft_text_by_chunk=draft_text_by_chunk,
        )
        right_context = tuple(
            (pid, source_map[pid])
            for pid in plan_chunk.context.right_en
            if pid in source_map
        )

        outcome = generate_for_chunk(
            chunk_id=plan_chunk.chunk_id,
            risk=risk,
            source=source,
            snapshot=snapshot,
            chunk_plan=chunk_plan,
            left_context=left_context,
            right_context=right_context,
            glossary=glossary,
            style_constraints={},
            config=config,
            params=generation_params,
            model_caller=model_caller,
            cache=gen_cache,
        )
        outcome_records.append({
            "chunk_id": outcome.chunk_id,
            "risk_band": outcome.risk_band,
            "risk": _serialize_risk(risk),
            "expected_roles": list(outcome.expected_roles),
            "status": outcome.status,
            "candidates": {
                role: _serialize_candidate(cand)
                for role, cand in outcome.candidates.items()
            },
            "errors": {
                role: {"code": err.code.value, "detail": err.detail}
                for role, err in outcome.errors.items()
            },
        })

        fidelity_draft = outcome.candidates.get("fidelity_first")
        if fidelity_draft is not None:
            draft_text_by_chunk[plan_chunk.chunk_id] = fidelity_draft.as_pid_map()

    finished_at = now_fn().isoformat(timespec="seconds")
    bundle: Dict[str, Any] = {
        "schema": GENERATION_BUNDLE_SCHEMA,
        "run_label": cfg.run_label,
        "chapter_id": cfg.chapter_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "input": {
            "chapter_html": str(cfg.chapter_html_path),
            "memory_dir": str(cfg.memory_dir),
        },
        "identities": {
            "source_hash": source.source_hash,
            "snapshot_hash": snapshot.snapshot_hash,
            "chunk_plan_hash": chunk_plan.plan_hash,
            "config_identity": config.config_identity,
        },
        "policy_versions": {
            "risk_policy": "pact-v4-risk-source-en/v1",
            "risk_thresholds": {"medium": 3, "high": 7},
            "prompt_fidelity_first": "pact-v4-prompt-fidelity-first/v2",
            "prompt_balanced_literary": "pact-v4-prompt-balanced-literary/v2",
        },
        "provisional_params": {
            "temperature": cfg.temperature,
            "seed": cfg.seed,
            "max_tokens": cfg.max_tokens,
            "chunk_min_words": cfg.min_chunk_words,
            "chunk_target_words": cfg.target_chunk_words,
            "chunk_max_words": cfg.max_chunk_words,
            "right_context_pids": cfg.right_context_pids,
        },
        "sequential_model_caveat": SEQUENTIAL_MODEL_CAVEAT,
        "source": dict(source.source),
        "chunk_plan": chunk_plan.to_payload(),
        "outcomes": outcome_records,
    }
    generation_bundle_path = cfg.out_dir / "generation_bundle.json"
    _write_json(generation_bundle_path, bundle)

    return GenerateRunResult(
        chapter_id=cfg.chapter_id,
        out_dir=cfg.out_dir,
        chunk_count=len(chunk_plan.chunks),
        chunk_plan_path=chunk_plan_path,
        risk_path=risk_path,
        generation_bundle_path=generation_bundle_path,
        bundle=bundle,
    )


# ---------------------------------------------------------------------------
# Pass 2: select (Qwen, + optional Gemma)
# ---------------------------------------------------------------------------


@dataclass
class SelectRunResult:
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
    provenance: Dict[str, Any]


def _record_selection(
    *,
    selection_records: List[Dict[str, Any]],
    final_text_by_pid: Dict[str, str],
    selected_role_counts: Dict[str, int],
    result: SelectionResult,
    risk_band: str,
    candidates_by_role: Mapping[str, Candidate],
) -> Tuple[int, int]:
    """Mutate run-level state for one chunk's ``SelectionResult``.

    Mirrors ``v4_phase12_draft_runner._record_selection`` but takes the
    candidate-by-role map directly (there is no live ``GenerationOutcome``
    on this pass -- candidates were reconstructed from the bundle).
    Returns ``(quarantined_delta, needs_synthesis_delta)``.
    """
    chunk_id = result.chunk_id
    record: Dict[str, Any] = {
        "chunk_id": chunk_id,
        "status": "selected" if result.selected_candidate_id else (
            "needs_synthesis" if result.needs_synthesis else "quarantined"
        ),
        "risk_band": risk_band,
        "candidates_evaluated": result.candidates_evaluated,
        "candidates_passed": result.candidates_passed,
        "candidates_failed": result.candidates_failed,
        "disagreement_detected": result.disagreement_detected,
        "disagreement_reason": result.disagreement_reason,
        "decision_trace": [
            {"gate": g.gate, "passed": g.passed, "detail": g.detail}
            for g in result.decision_trace
        ],
    }
    quarantined_delta = 0
    needs_synthesis_delta = 0

    if result.quarantine:
        record["quarantine"] = True
        record["quarantine_reason"] = result.quarantine_reason
        quarantined_delta = 1
    elif result.needs_synthesis:
        record["needs_synthesis"] = True
        record["synthesis_reason"] = result.synthesis_reason
        needs_synthesis_delta = 1
    else:
        record["selected_candidate_id"] = result.selected_candidate_id
        record["selected_role"] = result.selected_role
        selected_role_counts[result.selected_role] = (
            selected_role_counts.get(result.selected_role, 0) + 1
        )
        winner = candidates_by_role.get(result.selected_role)
        if winner is not None:
            final_text_by_pid.update(winner.as_pid_map())

    selection_records.append(record)
    return quarantined_delta, needs_synthesis_delta


def run_select(
    cfg: SequentialSelectConfig,
    *,
    qwen_evaluator: Callable[[Mapping[str, str], Mapping[str, str]], GateResult],
    gemma_selector: Optional[Callable[[Sequence[Tuple[str, Mapping[str, str]]]], GateResult]] = None,
    now: Optional[Callable[[], datetime]] = None,
) -> SelectRunResult:
    """Phase 2C for one chapter, reading ``run_generate``'s bundle.

    Needs only ``qwen_evaluator`` (mandatory) and, optionally,
    ``gemma_selector`` -- if Gemma happens to still be resident (e.g. it
    was never unloaded) it can be passed; otherwise ``None`` falls back
    to the cascade's documented deterministic role-order tie-break (see
    ``pact_v4.phase2.cascade.select_candidate``).

    Chunks are processed in the bundle's own order, but the order has no
    bearing on correctness: ``select_candidate`` depends only on the
    static ``source`` and one chunk's own candidates.
    """
    now_fn = now or (lambda: datetime.now(timezone.utc))
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    started_at = now_fn().isoformat(timespec="seconds")

    bundle = json.loads(cfg.generation_bundle_path.read_text(encoding="utf-8"))
    if bundle.get("schema") != GENERATION_BUNDLE_SCHEMA:
        raise ValueError(
            f"Foreign identity: generation bundle schema "
            f"{bundle.get('schema')!r}, expected {GENERATION_BUNDLE_SCHEMA!r}"
        )

    chapter_id = str(bundle["chapter_id"])
    source_pairs = tuple((pid, text) for pid, text in bundle["source"].items())
    source = SourceArtifact(chapter_id=chapter_id, source=source_pairs)
    declared_source_hash = bundle["identities"]["source_hash"]
    if source.source_hash != declared_source_hash:
        raise ValueError(
            f"Foreign identity: reconstructed source_hash {source.source_hash} "
            f"does not match generation bundle's declared {declared_source_hash}"
        )

    select_config = cfg.to_config_artifact(model_profile="qwen-phase2c")
    det_data = DeterministicGateData(
        glossary_terms=cfg.deterministic_glossary_terms,
        names=cfg.deterministic_names,
        mixed_script_allow=cfg.deterministic_mixed_script_allow,
    )

    selection_records: List[Dict[str, Any]] = []
    final_text_by_pid: Dict[str, str] = {}
    selected_role_counts: Dict[str, int] = {}
    quarantined_count = 0
    needs_synthesis_count = 0
    incomplete_generation_count = 0

    for outcome_record in bundle["outcomes"]:
        chunk_id = outcome_record["chunk_id"]
        risk_band = outcome_record["risk_band"]

        if outcome_record["status"] != "complete":
            incomplete_generation_count += 1
            selection_records.append({
                "chunk_id": chunk_id,
                "status": "incomplete_generation",
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
        # Older bundles (written before the required-risk-category gate
        # existed) won't carry "risk" -- degrade to risk=None, which is
        # exactly select_candidate's own documented "skip stage 2b"
        # behaviour, not a fabricated value.
        risk_payload = outcome_record.get("risk")
        risk = _deserialize_risk(risk_payload) if risk_payload is not None else None

        try:
            result = select_candidate(
                chunk_id=chunk_id,
                candidates=candidates,
                source=source,
                qwen_evaluator=qwen_evaluator,
                det_data=det_data,
                gemma_selector=gemma_selector,
                risk=risk,
            )
        except Exception as exc:  # noqa: BLE001 -- see draft_runner's note
            LOG.exception("select_candidate raised for %s", chunk_id)
            selection_records.append({
                "chunk_id": chunk_id,
                "status": "quarantined",
                "quarantine_reason": f"cascade raised: {exc!r}",
                "risk_band": risk_band,
                "candidates_produced": [c.role for c in candidates],
            })
            quarantined_count += 1
            continue

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

    translations_path = cfg.out_dir / "translations.json"
    _write_json(translations_path, final_text_by_pid)
    selection_path = cfg.out_dir / "selection_results.json"
    _write_json(selection_path, {
        "chapter_id": chapter_id,
        "snapshot_hash": bundle["identities"]["snapshot_hash"],
        "chunk_plan_hash": bundle["identities"]["chunk_plan_hash"],
        "config_identity": select_config.config_identity,
        "results": selection_records,
    })

    finished_at = now_fn().isoformat(timespec="seconds")
    chunk_count = len(bundle["outcomes"])
    policy_versions = dict(bundle.get("policy_versions", {}))
    policy_versions.setdefault("reviewer_qwen_fidelity", "pact-v4-reviewer-qwen-fidelity/v1")
    policy_versions.setdefault(
        "reviewer_gemma_russian_preference", "pact-v4-reviewer-gemma-russian-preference/v1",
    )

    provenance: Dict[str, Any] = {
        "schema": PROVENANCE_SCHEMA,
        "run_label": cfg.run_label,
        "chapter_id": chapter_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "input": {
            "generation_bundle": str(cfg.generation_bundle_path),
            "generation_run_label": bundle.get("run_label"),
        },
        "identities": dict(bundle["identities"]),
        "select_config_identity": select_config.config_identity,
        "policy_versions": policy_versions,
        "provisional_params": dict(bundle.get("provisional_params", {})),
        "counts": {
            "chunks": chunk_count,
            "selected": sum(selected_role_counts.values()),
            "quarantined": quarantined_count,
            "needs_synthesis": needs_synthesis_count,
            "incomplete_generation": incomplete_generation_count,
            "selected_role_counts": dict(selected_role_counts),
        },
        "artefacts": {
            "generation_bundle": str(cfg.generation_bundle_path),
            "selection_results": str(selection_path),
            "translations": str(translations_path),
        },
        "sequential_model_caveat": bundle.get(
            "sequential_model_caveat", SEQUENTIAL_MODEL_CAVEAT,
        ),
    }
    provenance_path = cfg.out_dir / "provenance.json"
    _write_json(provenance_path, provenance)

    return SelectRunResult(
        chapter_id=chapter_id,
        out_dir=cfg.out_dir,
        chunk_count=chunk_count,
        selected_count=sum(selected_role_counts.values()),
        quarantined_count=quarantined_count,
        needs_synthesis_count=needs_synthesis_count,
        incomplete_generation_count=incomplete_generation_count,
        selected_role_counts=dict(selected_role_counts),
        translations_path=translations_path,
        selection_path=selection_path,
        provenance_path=provenance_path,
        provenance=provenance,
    )


# ---------------------------------------------------------------------------
# IO helper
# ---------------------------------------------------------------------------


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False),
        encoding="utf-8",
    )
