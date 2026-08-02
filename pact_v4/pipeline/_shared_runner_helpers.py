"""Topology-agnostic helpers shared by the v4 chapter runners.

With the adoption of the strict single-resident driver as the production
v4 architecture (``DECISIONS.md``, 2026-08-01),
``v4_phase12_draft_runner`` loses its production-driver role and becomes a
reference/fixture (it assumes Gemma and Qwen are resident at once, which
the current hardware cannot do). Its private helpers are, however,
topology-agnostic — left_context assembly, glossary parsing, risk
pre-screen, selection recording and generation serialization do not
depend on how the models are hosted — so they live here, in a module that
outlives the draft runner, instead of being imported from an archived
fixture file.

The five functions are moved verbatim from
``v4_phase12_draft_runner`` without any change to their signatures or
behaviour. ``v4_phase12_strict_runner`` and
``v4_phase12_sequential_runner`` import them from here.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from pact_v4.phase1.models import ChunkPlan, ChunkPlanArtifact
from pact_v4.phase2.cascade import SelectionResult
from pact_v4.phase2.generation import GenerationOutcome
from pact_v4.phase2.risk import GlossaryEntry, RiskAssessment, assess_source_risk
from pact_v4.runtime.snapshot_factory import ChapterMemory

# ---------------------------------------------------------------------------
# Per-chunk context assembly (left_ru)
# ---------------------------------------------------------------------------


def _left_ru_for_chunk(
    *,
    chunk_index: int,
    chunk_plan: ChunkPlanArtifact,
    selected_text_by_chunk: Mapping[str, Dict[str, str]],
) -> Tuple[Tuple[str, str], ...]:
    """Resolve the read-only Russian left_context for one chunk.

    The plan is generated before any translation exists, so the static
    ``ChunkPlan.context.left_ru`` is empty (per
    ``pact_v4.phase1.chunker``). At generation time the driver looks up
    the *previous* chunk's **selected** translation and returns its PIDs
    and Russian text in the same order they appear in the source.

    **Empty-tuple contract:** ``left_context`` is empty when the
    previous chunk was quarantined, flagged for synthesis, or never
    reached a selection. A chunk that did not produce a selected
    candidate has no established translation, and feeding its
    ``fidelity_first`` draft into the next chunk would be the same
    silent "least-bad" fallback the cascade refuses to do at the
    selection stage. The first chunk of the chapter also gets an
    empty ``left_context`` by construction.
    """
    if chunk_index <= 0:
        return ()
    prev_chunk_id = chunk_plan.chunks[chunk_index - 1].chunk_id
    prev_translation = selected_text_by_chunk.get(prev_chunk_id)
    if not prev_translation:
        return ()
    prev_chunk = chunk_plan.chunk(prev_chunk_id)
    return tuple(
        (pid, prev_translation[pid])
        for pid in prev_chunk.pids
        if pid in prev_translation and prev_translation[pid]
    )


# ---------------------------------------------------------------------------
# Risk classification (per-PID + per-chunk)
# ---------------------------------------------------------------------------


def _glossary_entries(memory: ChapterMemory) -> Tuple[GlossaryEntry, ...]:
    """Convert the on-disk glossary JSON into ``GlossaryEntry`` tuples.

    The on-disk format varies in practice; we accept either:

    * a list of ``{"source_term": ..., "target_terms": [...]}`` objects, or
    * a dict ``{source_term: target_term | [target_term, ...]}``.

    Anything unparseable is dropped — the risk policy treats an empty
    glossary as the conservative "no constraints" baseline, not as an
    error.
    """
    entries: List[GlossaryEntry] = []
    raw = memory.glossary
    if isinstance(raw, dict):
        items = list(raw.items())
    elif isinstance(raw, list):
        items = []
        for entry in raw:
            if isinstance(entry, dict):
                src = entry.get("source_term") or entry.get("source")
                tgt = entry.get("target_terms") or entry.get("target")
                if src is not None and tgt is not None:
                    items.append((src, tgt))
    else:
        items = []

    for source_term, target in items:
        if isinstance(target, str):
            targets = (target,)
        elif isinstance(target, (list, tuple)):
            targets = tuple(str(t) for t in target if t)
        else:
            continue
        if not targets:
            continue
        try:
            entries.append(GlossaryEntry(source_term=str(source_term), target_terms=targets))
        except ValueError:
            continue
    return tuple(entries)


def _risk_for_chunk(
    *,
    chunk: ChunkPlan,
    source_map: Dict[str, str],
    glossary: Sequence[GlossaryEntry],
) -> RiskAssessment:
    """Risk pre-screen for one chunk's owned PIDs.

    Wraps ``assess_source_risk`` with the source_complete=True sentinel
    (a chunk plan with no missing PIDs is the *definition* of a complete
    source) and the frozen glossary from the chapter memory snapshot.
    """
    rows = tuple((pid, source_map.get(pid, "")) for pid in chunk.pids)
    return assess_source_risk(rows, glossary=glossary, source_complete=True)


# ---------------------------------------------------------------------------
# Selection recording helper
# ---------------------------------------------------------------------------


def _serialize_generation_outcome(outcome: GenerationOutcome) -> Dict[str, Any]:
    """Render one Phase 2B outcome to the JSON-friendly form used in
    ``generation_outcomes.json``.

    Centralised so the on-disk schema lives in exactly one place and
    the per-chunk driver loop stays readable.
    """
    return {
        "chunk_id": outcome.chunk_id,
        "risk_band": outcome.risk_band,
        "expected_roles": list(outcome.expected_roles),
        "status": outcome.status,
        "candidates": {
            role: {
                "candidate_id": cand.candidate_id,
                "role": cand.role,
                "translation": dict(cand.translation),
                "decision_trace": [
                    {"gate": g.gate, "passed": g.passed, "detail": g.detail}
                    for g in cand.decision_trace
                ],
            }
            for role, cand in outcome.candidates.items()
        },
        "errors": {
            role: {"code": err.code.value, "detail": err.detail}
            for role, err in outcome.errors.items()
        },
    }


def _record_selection(
    *,
    selection_records: List[Dict[str, Any]],
    final_text_by_pid: Dict[str, str],
    selected_role_counts: Dict[str, int],
    result: SelectionResult,
    outcome: GenerationOutcome,
) -> Tuple[int, int, Optional[Dict[str, str]]]:
    """Mutate the run-level state for one chunk's SelectionResult.

    Returns ``(quarantined_delta, needs_synthesis_delta,
    selected_translation)``: 0 or 1 for the first two, and the cascade
    winner's PID->text map (or ``None`` if the chunk was quarantined or
    needs_synthesis) for the third. The caller uses the third return
    value to populate ``selected_text_by_chunk`` — the table that
    feeds the next chunk's ``left_context`` — so only the cascade
    winner is ever allowed to flow forward, never an unselected draft.

    The helper also appends one record to ``selection_records`` and
    may update ``final_text_by_pid`` / ``selected_role_counts`` (only
    for selected chunks).
    """
    chunk_id = result.chunk_id
    record: Dict[str, Any] = {
        "chunk_id": chunk_id,
        "status": "selected" if result.selected_candidate_id else (
            "needs_synthesis" if result.needs_synthesis else "quarantined"
        ),
        "risk_band": outcome.risk_band,
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
    selected_translation: Optional[Dict[str, str]] = None

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
        winner = outcome.candidates.get(result.selected_role)
        if winner is not None:
            selected_translation = dict(winner.as_pid_map())
            final_text_by_pid.update(selected_translation)

    selection_records.append(record)
    return quarantined_delta, needs_synthesis_delta, selected_translation
