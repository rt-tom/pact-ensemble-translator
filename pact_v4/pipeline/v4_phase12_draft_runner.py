"""End-to-end driver: Phase 1C → 2A → 2B → 2C for one chapter.

The driver consumes:

* EN chapter HTML (parsed via ``pact_v4.phase0b.source_html``).
* A frozen chapter memory (glossary + book_memory; loaded from disk).
* A ``ConfigArtifact`` describing which model profiles, generation
  parameters, and reviewer wiring to use.
* Three injectable HTTP adapters: ``ModelCaller``, ``QwenEvaluator``,
  ``GemmaSelector`` (the production-flavoured implementations live in
  ``pact_v4.runtime``).

It emits, for the chapter:

* ``chunk_plan.json``         — Phase 1C output (the plan).
* ``risk_classification.json``— Phase 2A output (per-PID risk features).
* ``generation_outcomes.json``— Phase 2B output (per-chunk candidates).
* ``selection_results.json``  — Phase 2C output (per-chunk selection).
* ``translations.json``       — final PID -> Russian text map (only PIDs
  whose chunk produced a selected candidate; quarantined chunks are
  explicitly listed in ``selection_results.json``).
* ``provenance.json``         — single bundle of all the run's identities
  and policy versions, including a content-hash of the driver config so
  the future v3/v4 comparison tool can tell which run produced the
  artefacts.

The driver is deliberately pipeline-state-free: it does not own a
``GenerationCache`` across runs (the library's cache is per-call-site
and lives one layer down). For now the cache is per-chapter; that is
sufficient for the v3/v4 A/B gate, which is a single chapter and
re-runs only with deliberate config changes.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from pact_v4.phase0b.source_html import SourceBlock, load_source
from pact_v4.phase1.chunker import (
    DEFAULT_MAX_WORDS,
    DEFAULT_MIN_WORDS,
    DEFAULT_TARGET_WORDS,
    ChunkPlanner,
)
from pact_v4.phase1.models import (
    Candidate,
    ChunkContext,
    ChunkPlanArtifact,
    ConfigArtifact,
    GateResult,
    Snapshot,
    SourceArtifact,
)
from pact_v4.phase2.cascade import (
    DeterministicGateData,
    select_candidate,
)
from pact_v4.phase2.generation import (
    GenerationCache,
    GenerationError,
    GenerationParams,
    ModelCaller,
    generate_for_chunk,
)
from pact_v4.phase2.risk import (
    RiskAssessment,
)
from pact_v4.pipeline._shared_runner_helpers import (
    _glossary_entries,
    _left_ru_for_chunk,
    _record_selection,
    _risk_for_chunk,
    _serialize_generation_outcome,
)
from pact_v4.runtime.snapshot_factory import (
    ChapterMemory,
    build_config_artifact,
    build_snapshot,
    build_source_artifact,
)

LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Run configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PipelineConfig:
    """One end-to-end run configuration.

    * All provisional values are explicit. Temperature, seed, max_tokens
      and risk thresholds are the current placeholders from
      ``pact_v4.phase2.risk.RISK_POLICY`` and
      ``pact_v4.phase2.generation.GenerationParams``; the benchmark gate
      will replace them.
    * ``right_context_pids`` is the count of next-chunk PIDs rendered
      into the prompt as English source (0 by default — same default as
      ``ChunkPlanner.plan(following_blocks=0)``).
    * ``min_chunk_words`` / ``target_chunk_words`` / ``max_chunk_words``
      are passed straight into ``ChunkPlanner``. Defaults are the Phase
      0C Gate's initial/default small chunk profile (word-based, not
      PID-based — see ``pact_v4.phase1.chunker``).
    """

    chapter_id: str
    chapter_html_path: Path
    memory_dir: Path
    out_dir: Path
    # Chunk planner
    min_chunk_words: int = DEFAULT_MIN_WORDS
    target_chunk_words: int = DEFAULT_TARGET_WORDS
    max_chunk_words: int = DEFAULT_MAX_WORDS
    right_context_pids: int = 0
    # Generation (Phase 2B)
    temperature: float = 0.2
    seed: int = 7
    max_tokens: int = 8192
    # Deterministic gate (Phase 2C)
    deterministic_glossary_terms: Tuple[Tuple[str, str], ...] = ()
    deterministic_names: Tuple[Tuple[str, str], ...] = ()
    deterministic_mixed_script_allow: Tuple[str, ...] = ()
    # Driver
    config_version: str = "pact-v4-driver/phase12/draft/v1"
    run_label: str = "v4-phase12-draft"

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


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------


@dataclass
class ChapterRunResult:
    """Aggregated outcome of one end-to-end chapter run."""

    chapter_id: str
    out_dir: Path
    chunk_count: int
    selected_count: int
    quarantined_count: int
    needs_synthesis_count: int
    incomplete_generation_count: int
    selected_role_counts: Dict[str, int]
    translations_path: Path
    chunk_plan_path: Path
    risk_path: Path
    generation_path: Path
    selection_path: Path
    provenance_path: Path
    provenance: Dict[str, Any]


def run_chapter(
    cfg: PipelineConfig,
    *,
    model_caller: ModelCaller,
    qwen_evaluator: Callable[[Mapping[str, str], Mapping[str, str]], GateResult],
    gemma_selector: Optional[Callable[[Sequence[Tuple[str, Mapping[str, str]]]], GateResult]] = None,
    now: Optional[Callable[[], datetime]] = None,
) -> ChapterRunResult:
    """Execute the Phase 1C → 2A → 2B → 2C pipeline for one chapter.

    The function is purely sequential: the chapter is small enough
    (golden-set chapter 046 is one file) that we do not need an
    async/parallel model. **Phase 2B and Phase 2C run interleaved per
    chunk, not in two separate passes** — this matters because chunk
    N+1's left_context must come from chunk N's *cascade winner*, not
    from ``outcome.candidates[expected_roles[0]]`` (which is just the
    first generation role, before the cascade has had a chance to pick
    ``balanced_literary``, to quarantine the chunk, or to flag it for
    synthesis). The cascade's "no least-bad selection" contract
    applies symmetrically here: a chunk that was not selected has no
    established translation, so feeding its draft to the next chunk
    is the same silent fallback the cascade is built to refuse.
    """
    now_fn = now or (lambda: datetime.now(timezone.utc))

    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    started_at = now_fn().isoformat(timespec="seconds")

    # ------------------------------------------------------------------
    # Phase 0B: parse EN source HTML.
    # ------------------------------------------------------------------
    blocks, _raw_sha = load_source(cfg.chapter_html_path)
    if not blocks:
        raise ValueError(f"Chapter {cfg.chapter_id}: no source blocks parsed")
    LOG.info("Parsed %d source blocks from %s", len(blocks), cfg.chapter_html_path)

    # ------------------------------------------------------------------
    # Phase 1A: build SourceArtifact + Snapshot + ConfigArtifact.
    # ------------------------------------------------------------------
    source = build_source_artifact(chapter_id=cfg.chapter_id, blocks=blocks)
    memory = ChapterMemory.from_directory(cfg.memory_dir)
    snapshot = build_snapshot(
        chapter_id=cfg.chapter_id,
        source=source,
        memory=memory,
        context=f"chapter_html={cfg.chapter_html_path};memory_dir={cfg.memory_dir}",
    )
    # The ConfigArtifact model_config record must match the config used;
    # we pass a minimal model_config so Provenance.validate_against can
    # bind it later if needed.
    config = cfg.to_config_artifact(model_profile="gemma-4-26B-A4B-it-UD-Q4_K_XL")

    # ------------------------------------------------------------------
    # Phase 1C: structure-aware chunk plan.
    # ------------------------------------------------------------------
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
    LOG.info(
        "Chunk plan: %d chunks (snapshot_hash=%s, plan_hash=%s)",
        len(plans), snapshot.snapshot_hash[:12], chunk_plan.plan_hash[:12],
    )
    chunk_plan_path = cfg.out_dir / "chunk_plan.json"
    _write_json(chunk_plan_path, chunk_plan.to_payload())

    # ------------------------------------------------------------------
    # Phase 2A: per-chunk deterministic risk pre-screen.
    #
    # Risk is purely source-side and model-free, so it can be computed
    # for every chunk in one pass before the per-chunk generation
    # loop. The risk band decides how many candidates the cascade
    # expects (1 for low, 2 for medium/high).
    # ------------------------------------------------------------------
    glossary = _glossary_entries(memory)
    source_map = dict(source.source)
    risk_records: List[Dict[str, Any]] = []
    risk_by_chunk: Dict[str, RiskAssessment] = {}
    for plan_chunk in chunk_plan.chunks:
        assessment = _risk_for_chunk(
            chunk=plan_chunk, source_map=source_map, glossary=glossary,
        )
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

    # ------------------------------------------------------------------
    # Phase 2B + 2C, interleaved per chunk.
    #
    # We deliberately do NOT split this into two separate "generate
    # everything" / "select everything" passes: chunk N+1's left_context
    # must come from chunk N's *selected* candidate, which the cascade
    # is the only thing that knows. Generating first, selecting second
    # would force us to either (a) feed chunk N+1 the first-role
    # candidate's draft regardless of cascade outcome (the original
    # bug) or (b) record a "stale" first-role draft as left_context
    # and then... do nothing useful with it. Interleaving keeps the
    # invariants tight and matches the docstring's claim.
    # ------------------------------------------------------------------
    generation_params = GenerationParams(
        temperature=cfg.temperature, seed=cfg.seed, max_tokens=cfg.max_tokens,
    )
    gen_cache = GenerationCache()
    generation_records: List[Dict[str, Any]] = []
    selection_records: List[Dict[str, Any]] = []
    final_text_by_pid: Dict[str, str] = {}
    selected_role_counts: Dict[str, int] = {}
    selected_text_by_chunk: Dict[str, Dict[str, str]] = {}
    quarantined_count = 0
    needs_synthesis_count = 0
    incomplete_generation_count = 0
    det_data = DeterministicGateData(
        glossary_terms=cfg.deterministic_glossary_terms,
        names=cfg.deterministic_names,
        mixed_script_allow=cfg.deterministic_mixed_script_allow,
    )

    for index, plan_chunk in enumerate(chunk_plan.chunks):
        risk = risk_by_chunk[plan_chunk.chunk_id]
        left_context = _left_ru_for_chunk(
            chunk_index=index,
            chunk_plan=chunk_plan,
            selected_text_by_chunk=selected_text_by_chunk,
        )
        right_context = tuple(
            (pid, source_map[pid])
            for pid in plan_chunk.context.right_en
            if pid in source_map
        )

        # ---- Phase 2B: generate candidates for this chunk ----------
        # lazy_balanced=False: this benchmark driver keeps the legacy A/B
        # 2-candidate scheme (V4 Efficiency A2 changes only the production
        # strict driver; the draft driver's A/B contract is unchanged).
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
            lazy_balanced=False,
        )
        generation_records.append(_serialize_generation_outcome(outcome))

        # ---- Phase 2C: select a winner for this chunk --------------
        if outcome.status != "complete":
            incomplete_generation_count += 1
            selection_records.append({
                "chunk_id": plan_chunk.chunk_id,
                "status": "incomplete_generation",
                "risk_band": outcome.risk_band,
                "expected_roles": list(outcome.expected_roles),
                "candidates_produced": list(outcome.candidates),
                "errors": {
                    role: {"code": err.code.value, "detail": err.detail}
                    for role, err in outcome.errors.items()
                },
            })
            # No selection → no established translation. The next
            # chunk sees an empty left_context, per the cascade's
            # "no least-bad selection" contract.
            continue

        candidates: List[Candidate] = list(outcome.candidates.values())
        try:
            result = select_candidate(
                chunk_id=plan_chunk.chunk_id,
                candidates=candidates,
                source=source,
                qwen_evaluator=qwen_evaluator,
                det_data=det_data,
                gemma_selector=gemma_selector,
            )
        except Exception as exc:  # noqa: BLE001 — see note below
            # The cascade's contract is that the *gate* (Qwen/det/Gemma)
            # may fail and the result is recorded as a failed gate, not
            # as an exception. A genuine exception here means the
            # evaluator/selector raised something the cascade didn't
            # anticipate (e.g. an HTTP error that escaped the adapter's
            # try/except). We treat it as a quarantine, not a crash, so
            # the run can still be diffed chunk-by-chunk.
            LOG.exception("select_candidate raised for %s", plan_chunk.chunk_id)
            selection_records.append({
                "chunk_id": plan_chunk.chunk_id,
                "status": "quarantined",
                "quarantine_reason": f"cascade raised: {exc!r}",
                "risk_band": outcome.risk_band,
                "candidates_produced": [c.role for c in candidates],
            })
            quarantined_count += 1
            continue

        q_delta, n_delta, selected_text = _record_selection(
            selection_records=selection_records,
            final_text_by_pid=final_text_by_pid,
            selected_role_counts=selected_role_counts,
            result=result,
            outcome=outcome,
        )
        quarantined_count += q_delta
        needs_synthesis_count += n_delta
        # Only the cascade winner is allowed to flow into the next
        # chunk's left_context. If this chunk was quarantined or
        # needs_synthesis, ``selected_text`` is None and the next
        # chunk's left_context is empty by construction.
        if selected_text is not None:
            selected_text_by_chunk[plan_chunk.chunk_id] = selected_text

    LOG.info(
        "Phase 2B/2C: %d selected, %d quarantined, %d needs_synthesis, %d incomplete_generation",
        sum(selected_role_counts.values()), quarantined_count,
        needs_synthesis_count, incomplete_generation_count,
    )
    generation_path = cfg.out_dir / "generation_outcomes.json"
    _write_json(generation_path, {
        "chapter_id": cfg.chapter_id,
        "snapshot_hash": snapshot.snapshot_hash,
        "chunk_plan_hash": chunk_plan.plan_hash,
        "config_identity": config.config_identity,
        "params": {
            "temperature": cfg.temperature,
            "seed": cfg.seed,
            "max_tokens": cfg.max_tokens,
            "reasoning": 0,
        },
        "outcomes": generation_records,
    })
    translations_path = cfg.out_dir / "translations.json"
    _write_json(translations_path, final_text_by_pid)
    selection_path = cfg.out_dir / "selection_results.json"
    _write_json(selection_path, {
        "chapter_id": cfg.chapter_id,
        "snapshot_hash": snapshot.snapshot_hash,
        "chunk_plan_hash": chunk_plan.plan_hash,
        "config_identity": config.config_identity,
        "results": selection_records,
    })

    # ------------------------------------------------------------------
    # Provenance bundle: every identity the future v3/v4 comparison
    # tool will need to tell which run produced the artefacts.
    # ------------------------------------------------------------------
    finished_at = now_fn().isoformat(timespec="seconds")
    provenance: Dict[str, Any] = {
        "schema": "pact-v4-run-provenance/phase12/v1",
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
            "reviewer_qwen_fidelity": "pact-v4-reviewer-qwen-fidelity/v1",
            "reviewer_gemma_russian_preference": "pact-v4-reviewer-gemma-russian-preference/v1",
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
        "counts": {
            "chunks": len(chunk_plan.chunks),
            "selected": sum(selected_role_counts.values()),
            "quarantined": quarantined_count,
            "needs_synthesis": needs_synthesis_count,
            "incomplete_generation": incomplete_generation_count,
            "selected_role_counts": dict(selected_role_counts),
        },
        "artefacts": {
            "chunk_plan": str(chunk_plan_path),
            "risk_classification": str(risk_path),
            "generation_outcomes": str(generation_path),
            "selection_results": str(selection_path),
            "translations": str(translations_path),
        },
    }
    provenance_path = cfg.out_dir / "provenance.json"
    _write_json(provenance_path, provenance)

    return ChapterRunResult(
        chapter_id=cfg.chapter_id,
        out_dir=cfg.out_dir,
        chunk_count=len(chunk_plan.chunks),
        selected_count=sum(selected_role_counts.values()),
        quarantined_count=quarantined_count,
        needs_synthesis_count=needs_synthesis_count,
        incomplete_generation_count=incomplete_generation_count,
        selected_role_counts=dict(selected_role_counts),
        translations_path=translations_path,
        chunk_plan_path=chunk_plan_path,
        risk_path=risk_path,
        generation_path=generation_path,
        selection_path=selection_path,
        provenance_path=provenance_path,
        provenance=provenance,
    )


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False),
        encoding="utf-8",
    )
