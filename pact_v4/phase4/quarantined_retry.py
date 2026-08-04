"""V4 B6: separate repair cycle for quarantined chunks with repair debt.

Backing card: ``docs/plans/V4_B6_QUARANTINED_RETRY_TASK_RU.md`` (Поток B+,
B6) and ``DECISIONS.md`` (2026-08-03: order B4->B5->B6; B6 — quarantined
chunks). Depends on B4 (JSON resilience, in ``main``) and B5 (mixed_script
policy, in ``main``).

Why this is a separate card: in run_001, four chunks were quarantined
(chunk0001 mixed_script — unlocked by B5; chunk0005/0009/0010 —
``qwen_fidelity``). B2 repair tried to fix them, but the re-gate
systematically failed: when both candidates fell on the same findings
(``qwen_fidelity``), repair cannot close a finding without *new generation* —
the model never saw the disambiguating context (chunk0005 p00095/p00099
skipped sentence + gender; chunk0009 ``grandchild``->``внук`` against a
gender-neutral source; chunk0010 ``well after dark``->``далеко за полночь``
contradicting the next line). The quarantined chunks stay "недовыпущенными":
their best-variant text lands in ``repair_report.final_translation`` but the
repair debt remains and ``complete`` is unreachable.

What this module implements (Variant A of the card, with Variant B as the
fallback):

  1. After B2 repair, identify quarantined chunks that still carry repair
     debt (``quarantined_chunks_with_debt``).
  2. Run a **separate, bounded (1 retry) repair cycle** for them:
     regenerate the chunk's candidates with **look-ahead right_context**
     (the next chunk's English source — ``lookahead_right_context``) and
     re-run the cascade (``run_quarantined_retry``). Phase 1/2, cascade,
     risk and prompts are untouched; the only context change is the
     look-ahead passed to ``generate_for_chunk`` (the card's "кроме
     передачи look-ahead context").
  3. A cascade-selected candidate **replaces the best-variant** in the final
     translation; the changed chunk is re-audited and re-repaired through
     the existing convergence machinery (the driver does this with
     ``pact_v4.phase4.repair``'s ``_reaudit_chunks`` / ``_run_repair_round``),
     so stale debt on the old best-variant is never carried.
  4. A chunk that still fails the cascade is accepted as final with its
     best-variant and explicitly marked ``quarantined_final`` (Variant B
     fallback).

Identity: the retry does not change source/snapshot/chunk_plan identity; its
fresh candidates are merged into ``generation_outcomes.json`` and the retry
history is persisted as ``quarantined_retry.json`` (foreign-identity checked
on resume, like the other cumulative sidecars). Resume reuses a prior
attempt for a chunk instead of re-paying the regeneration.

The module deliberately never imports ``pact_v4.runtime.model_lifecycle`` /
``model_lifecycle_adapters`` / ``ModelRouter`` (dual-mode rule).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from pact_v4._integrity_checks import (
    combine_script_tokens,
    source_derived_allowlist,
)
from pact_v4.phase1.models import (
    Candidate,
    ChunkPlanArtifact,
    ConfigArtifact,
    GateResult,
    Snapshot,
    SourceArtifact,
)
from pact_v4.phase2.cascade import DeterministicGateData, select_candidate
from pact_v4.phase2.generation import GenerationParams, generate_for_chunk
from pact_v4.phase2.risk import GlossaryEntry, RiskAssessment
from pact_v4.pipeline._shared_runner_helpers import (
    _left_ru_for_chunk,
    _serialize_generation_outcome,
)

LOG = logging.getLogger(__name__)

QUARANTINED_RETRY_SCHEMA = "pact-v4-phase4-quarantined-retry/v1"
QUARANTINED_RETRY_POLICY_VERSION = "pact-v4-quarantined-retry/v1"

# The separate repair cycle is bounded: at most one regeneration per
# quarantined chunk with debt (card, acceptance criterion 2). A chunk that
# still fails the cascade after one retry is accepted as final with its
# best-variant.
MAX_QUARANTINED_RETRIES_PER_CHUNK = 1

OUTCOME_SELECTED = "selected"
OUTCOME_QUARANTINED_FINAL = "quarantined_final"
OUTCOME_GENERATION_INCOMPLETE = "generation_incomplete"

# Word-boundary chunk-id matcher for debt strings. ``chunk0005`` never
# matches ``chunk00050`` (a following word character breaks the boundary).
_CHUNK_ID_RE = re.compile(r"(?<!\w)(chunk\d{4})(?!\w)")


def debt_mentions_chunk(debt: str, chunk_id: str) -> bool:
    """Whether a debt-trace string names ``chunk_id`` (word-boundary).

    Debt strings in ``repair_report.json`` are ``"chunk0005: repair ..."``,
    ``"chunk0005: p00095: soft Gemma finding ..."`` etc. — matching on the
    chunk id lets the retry cycle drop a retried-selected chunk's stale debt
    without guessing string formats elsewhere. ``chunk0005`` never matches
    ``chunk00050``.
    """
    return re.search(rf"(?<!\w){re.escape(chunk_id)}(?!\w)", str(debt)) is not None


def debt_mentions_pid(debt: str, pid: str) -> bool:
    """Whether a formatting-incident debt string targets ``pid``.

    Formatting debt strings are ``"formatting:{pid}:{span_id}: ..."``. A
    retried-selected chunk's PIDs are re-formatting, so the old formatting
    incidents on those PIDs must not carry over.
    """
    return re.match(rf"formatting:{re.escape(pid)}:", str(debt)) is not None


def quarantined_chunks_with_debt(
    handoff_chunks: Sequence[Mapping[str, Any]],
    repair_phase_result: Any,
) -> Tuple[str, ...]:
    """Quarantined chunks that still carry repair debt after B2 repair.

    A chunk qualifies when it is ``quarantined`` in the Step 6 handoff **and**
    either the repair debt trace names it or one of its repair records was not
    committed. Chunks quarantined with a clean audit / fully-resolved findings
    have no debt and are not retried (their best-variant is effectively final
    without changing its text).
    """
    quarantined = {
        row["chunk_id"]
        for row in handoff_chunks
        if row.get("status") == "quarantined"
    }
    if not quarantined:
        return ()
    with_debt: set = set()
    for debt in repair_phase_result.debt_trace:
        match = _CHUNK_ID_RE.search(str(debt))
        if match is not None and match.group(1) in quarantined:
            with_debt.add(match.group(1))
    for round_result in repair_phase_result.rounds:
        for record in round_result.records:
            if record.chunk_id in quarantined and not record.committed:
                with_debt.add(record.chunk_id)
    return tuple(sorted(with_debt))


def lookahead_right_context(
    *,
    chunk_id: str,
    chunk_plan: ChunkPlanArtifact,
    source: SourceArtifact,
) -> Tuple[Tuple[str, str], ...]:
    """The next chunk's English source as read-only look-ahead context.

    The original generation saw no following source (``following_blocks=0``
    by default); run_001's quarantined chunks failed precisely because the
    following source disambiguates it. The retry feeds the whole next chunk
    as ``right_context`` (read-only English, never translated). Empty when the
    chunk is the chapter's last — nothing follows, so the retry cannot add
    context and the chunk falls to ``quarantined_final``.
    """
    ids = [chunk.chunk_id for chunk in chunk_plan.chunks]
    try:
        index = ids.index(chunk_id)
    except ValueError:
        return ()
    if index + 1 >= len(ids):
        return ()
    next_chunk = chunk_plan.chunks[index + 1]
    source_map = dict(source.source)
    return tuple(
        (pid, source_map[pid])
        for pid in next_chunk.pids
        if pid in source_map
    )


# ---------------------------------------------------------------------------
# Retry history (persisted as ``quarantined_retry.json``, resume-validated)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuarantinedRetryAttempt:
    """One chunk's bounded (1 retry) regeneration + cascade attempt.

    ``outcome`` is ``selected`` (the retry cascade produced a winner that
    replaces the best-variant), ``quarantined_final`` (the chunk stays
    quarantined — best-variant is accepted as final, Variant B fallback) or
    ``generation_incomplete`` (the look-ahead regeneration failed validation;
    also final with best-variant). ``serialized_candidate`` carries the
    selected winner's full record so resume can reconstruct the candidate
    without re-paying the regeneration. ``reused`` marks an attempt restored
    from a prior session's ``quarantined_retry.json``.
    """

    chunk_id: str
    attempt: int
    outcome: str
    candidate_ids: Tuple[str, ...] = ()
    selected_candidate_id: Optional[str] = None
    selected_role: Optional[str] = None
    quarantine_reason: Optional[str] = None
    decision_trace: Tuple[GateResult, ...] = ()
    reused: bool = False
    serialized_candidate: Optional[Dict[str, Any]] = None

    def to_payload(self) -> Dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "attempt": self.attempt,
            "outcome": self.outcome,
            "candidate_ids": list(self.candidate_ids),
            "selected_candidate_id": self.selected_candidate_id,
            "selected_role": self.selected_role,
            "quarantine_reason": self.quarantine_reason,
            "decision_trace": [
                {"gate": g.gate, "passed": g.passed, "detail": g.detail}
                for g in self.decision_trace
            ],
            "reused": self.reused,
            "serialized_candidate": self.serialized_candidate,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "QuarantinedRetryAttempt":
        return cls(
            chunk_id=str(payload["chunk_id"]),
            attempt=int(payload.get("attempt", 1)),
            outcome=str(payload["outcome"]),
            candidate_ids=tuple(str(x) for x in payload.get("candidate_ids", [])),
            selected_candidate_id=payload.get("selected_candidate_id"),
            selected_role=payload.get("selected_role"),
            quarantine_reason=payload.get("quarantine_reason"),
            decision_trace=tuple(
                GateResult(
                    gate=str(gate["gate"]),
                    passed=bool(gate.get("passed", False)),
                    detail=str(gate.get("detail", "")),
                )
                for gate in payload.get("decision_trace", [])
            ),
            reused=bool(payload.get("reused", False)),
            serialized_candidate=payload.get("serialized_candidate"),
        )


@dataclass(frozen=True)
class QuarantinedRetryResult:
    """Outcome of the separate retry cycle for the quarantined chunk set.

    ``candidates`` maps the chunk ids that became ``selected`` to the new
    winner (the driver replaces the best-variant text with it). Each fresh
    regeneration is also returned as a serialized generation record
    (``generation_records``) so the driver can merge the new candidates into
    ``generation_outcomes.json`` (identity note in the card).
    """

    retried_chunk_ids: Tuple[str, ...]
    attempts: Tuple[QuarantinedRetryAttempt, ...]
    selected_chunk_ids: Tuple[str, ...]
    quarantined_final_chunk_ids: Tuple[str, ...]
    candidates: Tuple[Tuple[str, Candidate], ...] = ()
    generation_records: Tuple[Dict[str, Any], ...] = ()

    @property
    def retry_attempts(self) -> int:
        return len(self.attempts)

    @property
    def quarantined_final(self) -> bool:
        return bool(self.quarantined_final_chunk_ids)


def _candidate_from_retry_record(
    record: Mapping[str, Any],
    *,
    chunk_id: str,
    source: SourceArtifact,
    snapshot: Snapshot,
    chunk_plan: ChunkPlanArtifact,
    config: ConfigArtifact,
) -> Candidate:
    """Reconstruct the retry winner from its persisted record.

    ``Candidate.create`` re-validates every identity against
    source/snapshot/plan/config, so a stale or fabricated record cannot enter
    the assembled chapter. Translation PID order is rebuilt from the chunk
    plan (the record stores a PID->text map).
    """
    chunk = chunk_plan.chunk(chunk_id)
    translation_map = record["translation"]
    translation = tuple((pid, translation_map[pid]) for pid in chunk.pids)
    decision_trace = tuple(
        GateResult(
            gate=str(gate["gate"]),
            passed=bool(gate.get("passed", False)),
            detail=str(gate.get("detail", "")),
        )
        for gate in record.get("decision_trace", [])
    )
    return Candidate.create(
        candidate_id=record["candidate_id"],
        chunk_id=chunk_id,
        role=record["role"],
        translation=translation,
        source=source,
        snapshot=snapshot,
        chunk_plan=chunk_plan,
        config=config,
        decision_trace=decision_trace,
    )


# ---------------------------------------------------------------------------
# One chunk: regenerate with look-ahead + re-run the cascade
# ---------------------------------------------------------------------------


def _chunk_det_data(
    *,
    det_data_base: DeterministicGateData,
    chunk_id: str,
    source: SourceArtifact,
    chunk_plan: ChunkPlanArtifact,
    candidates: Sequence[Candidate],
) -> DeterministicGateData:
    """Chunk-scoped ``DeterministicGateData`` for the retry cascade.

    Mirrors the strict driver's per-chunk cascade allowlist (B5): the static
    allowlist plus the source-derived tokens that appear in *both* the chunk's
    source and the retry candidates' union — so a Latin token in the source
    preserved by the new candidate is legitimate without loosening the gate
    for tokens that never appear in the source.
    """
    source_map = dict(source.source)
    chunk_source_text = " ".join(
        source_map[pid] for pid in chunk_plan.chunk(chunk_id).pids if pid in source_map
    )
    candidate_union_text = " ".join(
        text for cand in candidates for _, text in cand.translation
    )
    return replace(
        det_data_base,
        mixed_script_allow=combine_script_tokens(
            det_data_base.mixed_script_allow,
            source_derived_allowlist(chunk_source_text, candidate_union_text),
        ),
    )


def _with_cascade_trace(
    serialized: Dict[str, Any], winner: Candidate, selection_result: Any
) -> Dict[str, Any]:
    """Attach the retry cascade's decision trace to the serialized winner.

    The persisted generation records of the *original* candidates carry only
    the ``phase2b_prompt_bundle`` gate, so a resumed Step 6's deterministic
    best-variant (gates passed, then role priority) would not prefer the retry
    winner over the original ``fidelity_first``. Adding the passed cascade
    gates makes the retry winner the natural best-variant on a resumed run —
    the diagnostic audit then sees the same text the terminal accepted.
    """
    record = dict(serialized)
    candidates = dict(record["candidates"])
    winner_payload = dict(candidates[winner.role])
    winner_payload["decision_trace"] = [
        {"gate": g.gate, "passed": g.passed, "detail": g.detail}
        for g in winner.decision_trace
    ] + [
        {"gate": g.gate, "passed": g.passed, "detail": g.detail}
        for g in selection_result.decision_trace
    ]
    candidates[winner.role] = winner_payload
    record["candidates"] = candidates
    return record


def _retry_one_chunk(
    *,
    chunk_id: str,
    chunk_plan: ChunkPlanArtifact,
    source: SourceArtifact,
    snapshot: Snapshot,
    config: ConfigArtifact,
    det_data_base: DeterministicGateData,
    risk: RiskAssessment,
    glossary: Tuple[GlossaryEntry, ...],
    left_context: Tuple[Tuple[str, str], ...],
    generation_params: GenerationParams,
    model_caller: Any,
    gen_cache: Any,
    qwen_evaluator: Any,
    gemma_selector: Any,
    prior_attempt: Optional[QuarantinedRetryAttempt],
    bible_text: str = "",
) -> Tuple[QuarantinedRetryAttempt, Optional[Dict[str, Any]]]:
    """Regenerate one chunk with look-ahead and re-run the cascade.

    Returns ``(attempt, serialized_generation_record)``. A prior attempt
    (from ``quarantined_retry.json``) is restored verbatim and reused —
    resume never re-pays the bounded regeneration. ``serialized_generation_record``
    is ``None`` for a reused attempt (the prior session already recorded it)
    and the serialized ``GenerationOutcome`` otherwise.
    """
    if prior_attempt is not None:
        return replace(prior_attempt, reused=True), None

    right_context = lookahead_right_context(
        chunk_id=chunk_id, chunk_plan=chunk_plan, source=source,
    )
    outcome = generate_for_chunk(
        chunk_id=chunk_id,
        risk=risk,
        source=source,
        snapshot=snapshot,
        chunk_plan=chunk_plan,
        left_context=left_context,
        right_context=right_context,
        glossary=glossary,
        style_constraints={},
        bible_text=bible_text,
        config=config,
        params=generation_params,
        model_caller=model_caller,
        cache=gen_cache,
    )
    serialized = _serialize_generation_outcome(outcome)
    candidate_ids = tuple(candidate.candidate_id for candidate in outcome.candidates.values())
    if outcome.status != "complete":
        return QuarantinedRetryAttempt(
            chunk_id=chunk_id,
            attempt=1,
            outcome=OUTCOME_GENERATION_INCOMPLETE,
            candidate_ids=candidate_ids,
            quarantine_reason="; ".join(
                f"{role}={err.detail}" for role, err in outcome.errors.items()
            ),
        ), serialized

    candidates = list(outcome.candidates.values())
    det_data_chunk = _chunk_det_data(
        det_data_base=det_data_base,
        chunk_id=chunk_id,
        source=source,
        chunk_plan=chunk_plan,
        candidates=candidates,
    )
    try:
        result = select_candidate(
            chunk_id=chunk_id,
            candidates=candidates,
            source=source,
            qwen_evaluator=qwen_evaluator,
            det_data=det_data_chunk,
            gemma_selector=gemma_selector,
        )
    except Exception as exc:  # noqa: BLE001 -- a cascade raise is recorded, not fatal
        LOG.exception("quarantined retry: cascade raised for %s", chunk_id)
        return QuarantinedRetryAttempt(
            chunk_id=chunk_id,
            attempt=1,
            outcome=OUTCOME_QUARANTINED_FINAL,
            candidate_ids=candidate_ids,
            quarantine_reason=f"cascade raised during quarantined retry: {exc!r}",
        ), serialized

    if result.selected_candidate_id:
        winner = outcome.candidates[result.selected_role]
        return QuarantinedRetryAttempt(
            chunk_id=chunk_id,
            attempt=1,
            outcome=OUTCOME_SELECTED,
            candidate_ids=candidate_ids,
            selected_candidate_id=winner.candidate_id,
            selected_role=winner.role,
            decision_trace=result.decision_trace,
            serialized_candidate={
                "candidate_id": winner.candidate_id,
                "role": winner.role,
                "translation": dict(winner.translation),
                "decision_trace": [
                    {"gate": g.gate, "passed": g.passed, "detail": g.detail}
                    for g in winner.decision_trace
                ],
            },
        ), _with_cascade_trace(serialized, winner, result)

    reason = result.synthesis_reason if result.needs_synthesis else result.quarantine_reason
    return QuarantinedRetryAttempt(
        chunk_id=chunk_id,
        attempt=1,
        outcome=OUTCOME_QUARANTINED_FINAL,
        candidate_ids=candidate_ids,
        quarantine_reason=reason or "still quarantined after look-ahead retry",
    ), serialized


# ---------------------------------------------------------------------------
# The retry cycle (regeneration + cascade, bounded, resume-aware)
# ---------------------------------------------------------------------------


def run_quarantined_retry(
    *,
    chunk_ids: Sequence[str],
    source: SourceArtifact,
    snapshot: Snapshot,
    chunk_plan: ChunkPlanArtifact,
    config: ConfigArtifact,
    det_data_base: DeterministicGateData,
    risk_by_chunk: Mapping[str, RiskAssessment],
    glossary: Tuple[GlossaryEntry, ...],
    selected_text_by_chunk: Mapping[str, Dict[str, str]],
    generation_params: GenerationParams,
    model_caller: Any,
    gen_cache: Any,
    qwen_evaluator: Any,
    gemma_selector: Any,
    prior_attempts: Optional[Mapping[str, QuarantinedRetryAttempt]] = None,
    bible_text: str = "",
) -> QuarantinedRetryResult:
    """Run the separate bounded retry cycle for the quarantined chunk set.

    ``chunk_ids`` is ``quarantined_chunks_with_debt(...)`` output. Each chunk
    is regenerated with the next chunk's source as look-ahead ``right_context``
    and re-run through the cascade (exactly the gates of Phase 2C — nothing
    here re-derives pass/fail logic). A cascade winner replaces the
    best-variant; a chunk that still fails is accepted as final with its
    best-variant (``quarantined_final``).

    ``prior_attempts`` are the attempts restored from a prior session's
    ``quarantined_retry.json``; a prior attempt is reused instead of re-paying
    the regeneration (a prior ``selected`` attempt's candidate is
    reconstructed through ``Candidate.create``, so a corrupt/foreign record
    falls back to a fresh regeneration rather than being trusted).
    """
    prior = prior_attempts or {}
    attempts: List[QuarantinedRetryAttempt] = []
    candidates: Dict[str, Candidate] = {}
    selected: List[str] = []
    quarantined_final: List[str] = []
    gen_records: List[Dict[str, Any]] = []
    ids = [chunk.chunk_id for chunk in chunk_plan.chunks]

    for chunk_id in sorted(chunk_ids):
        try:
            index = ids.index(chunk_id)
        except ValueError:
            LOG.warning(
                "quarantined retry: %s not in chunk plan; skipping", chunk_id
            )
            continue
        left_context = _left_ru_for_chunk(
            chunk_index=index,
            chunk_plan=chunk_plan,
            selected_text_by_chunk=selected_text_by_chunk,
        )

        prior_attempt = prior.get(chunk_id)
        if (
            prior_attempt is not None
            and prior_attempt.outcome == OUTCOME_SELECTED
            and prior_attempt.serialized_candidate is not None
        ):
            try:
                candidate = _candidate_from_retry_record(
                    prior_attempt.serialized_candidate,
                    chunk_id=chunk_id,
                    source=source,
                    snapshot=snapshot,
                    chunk_plan=chunk_plan,
                    config=config,
                )
            except Exception as exc:  # noqa: BLE001 -- corrupt/foreign record -> fresh retry
                LOG.warning(
                    "quarantined retry: prior selected attempt for %s not "
                    "reconstructible (%s); re-running the bounded retry",
                    chunk_id, exc,
                )
                prior_attempt = None
            else:
                attempts.append(replace(prior_attempt, reused=True))
                selected.append(chunk_id)
                candidates[chunk_id] = candidate
                continue

        attempt, serialized = _retry_one_chunk(
            chunk_id=chunk_id,
            chunk_plan=chunk_plan,
            source=source,
            snapshot=snapshot,
            config=config,
            det_data_base=det_data_base,
            risk=risk_by_chunk[chunk_id],
            glossary=glossary,
            left_context=left_context,
            generation_params=generation_params,
            model_caller=model_caller,
            gen_cache=gen_cache,
            qwen_evaluator=qwen_evaluator,
            gemma_selector=gemma_selector,
            prior_attempt=prior_attempt,
            bible_text=bible_text,
        )
        attempts.append(attempt)
        if serialized is not None:
            gen_records.append(serialized)
        if attempt.outcome == OUTCOME_SELECTED and attempt.serialized_candidate is not None:
            candidate = _candidate_from_retry_record(
                attempt.serialized_candidate,
                chunk_id=chunk_id,
                source=source,
                snapshot=snapshot,
                chunk_plan=chunk_plan,
                config=config,
            )
            candidates[chunk_id] = candidate
            selected.append(chunk_id)
        elif attempt.outcome in (OUTCOME_QUARANTINED_FINAL, OUTCOME_GENERATION_INCOMPLETE):
            # Either the cascade still failed or the look-ahead regeneration
            # itself failed: the chunk is accepted as final with its
            # best-variant (Variant B fallback), explicitly marked.
            quarantined_final.append(chunk_id)

    return QuarantinedRetryResult(
        retried_chunk_ids=tuple(sorted(chunk_ids)),
        attempts=tuple(attempts),
        selected_chunk_ids=tuple(selected),
        quarantined_final_chunk_ids=tuple(quarantined_final),
        candidates=tuple(sorted(candidates.items())),
        generation_records=tuple(gen_records),
    )


def merge_retry_generation_records(
    existing_records: Sequence[Mapping[str, Any]],
    retry_records: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Merge the retry's fresh serialized generation records cumulatively.

    Keeps the existing per-chunk record and unions the retry candidates in
    (a retry candidate with the same role overwrites the original — it is the
    fresh regeneration of that role under the look-ahead context), preserving
    every error trace. Foreign identity is already guarded by the caller's
    ``generation_outcomes.json`` merge.
    """
    by_chunk: Dict[str, Dict[str, Any]] = {
        rec["chunk_id"]: dict(rec) for rec in existing_records if rec.get("chunk_id")
    }
    for rec in retry_records:
        chunk_id = rec.get("chunk_id")
        if not chunk_id:
            continue
        if chunk_id in by_chunk:
            merged_candidates = dict(by_chunk[chunk_id].get("candidates", {}))
            merged_candidates.update(rec.get("candidates", {}))
            merged = dict(by_chunk[chunk_id])
            merged["candidates"] = merged_candidates
            merged["status"] = rec.get("status", merged.get("status"))
            merged["errors"] = {
                **by_chunk[chunk_id].get("errors", {}),
                **rec.get("errors", {}),
            }
            by_chunk[chunk_id] = merged
        else:
            by_chunk[chunk_id] = dict(rec)
    return [by_chunk[chunk_id] for chunk_id in by_chunk]


__all__ = [
    "QUARANTINED_RETRY_SCHEMA",
    "QUARANTINED_RETRY_POLICY_VERSION",
    "MAX_QUARANTINED_RETRIES_PER_CHUNK",
    "OUTCOME_SELECTED",
    "OUTCOME_QUARANTINED_FINAL",
    "OUTCOME_GENERATION_INCOMPLETE",
    "QuarantinedRetryAttempt",
    "QuarantinedRetryResult",
    "debt_mentions_chunk",
    "debt_mentions_pid",
    "quarantined_chunks_with_debt",
    "lookahead_right_context",
    "run_quarantined_retry",
    "merge_retry_generation_records",
]
