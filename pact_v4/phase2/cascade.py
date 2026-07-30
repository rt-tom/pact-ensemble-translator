"""Phase 2C: Cascaded selection without a scoring engine.

Lexicographic cascade (V4_MVP_SPEC_RU.md #4, #5):

  1. Qwen semantic pass/fail — fidelity check for every candidate (mandatory).
  2. Deterministic consistency gate — glossary/names/numbers/mixed-script
     (model-free, applied only to candidates that passed Qwen).
  3. Gemma Russian preference — selects the best Russian among candidates that
     passed both Qwen and deterministic, without seeing the source.

Rules:

  - A "passing" candidate has passed BOTH Qwen and deterministic.
  - Only passing candidates are eligible for Gemma preference.
  - C/synthesis is triggered only when:
      * Two passing A/B candidates show documented semantic disagreement, OR
      * No candidate passes (the chunk goes to targeted synthesis/repair).
  - Disagreement A≠B raises scrutiny but is NOT treated as an error.
  - Agreement A=B is NOT proof of correctness.
  - If no candidate passes → quarantine route (no "least bad" selection).
  - Every selection decision is recorded with a decision trace: who
    passed/failed, why, and the final selection outcome.

Explicitly OUT of scope: actual C/synthesis *generation* (Phase 2B/Phase 4),
repair execution, and benchmark measurement.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, Mapping, Optional, Protocol, Sequence, Tuple

from pact_v4._integrity_checks import (
    combine_glossary_terms,
    extract_digits as _extract_digits,
    find_mixed_script as _find_mixed_script,
    missing_numeric_values as _missing_numeric_values,
    source_term_present as _source_term_present,
    target_form_present as _target_form_present,
)
from pact_v4.phase1.models import Candidate, GateResult, SourceArtifact

__all__ = [
    "SelectionResult",
    "QwenEvaluator",
    "GemmaSelector",
    "DeterministicGateData",
    "deterministic_consistency_gate",
    "select_candidate",
    "check_semantic_disagreement",
]


# ---------------------------------------------------------------------------
# Utility helpers (model-free, deterministic)
#
# The actual implementations live in ``pact_v4._integrity_checks`` (shared
# with ``pact_v4.phase3.audit``, which needs the exact same checks applied
# chapter-wide instead of per-candidate) and are imported above under their
# original private names so the rest of this module needs no further
# changes.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Deterministic consistency gate data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeterministicGateData:
    """Frozen data for the model-free deterministic consistency gate.

    All fields can be empty (the gate is a pure check on what IS provided,
    nothing is assumed).
    """

    glossary_terms: Tuple[Tuple[str, str], ...] = ()
    names: Tuple[Tuple[str, str], ...] = ()
    mixed_script_allow: Tuple[str, ...] = ()

    @classmethod
    def empty(cls) -> "DeterministicGateData":
        return cls()


# ---------------------------------------------------------------------------
# Deterministic consistency gate (model-free)
# ---------------------------------------------------------------------------


def deterministic_consistency_gate(
    *,
    candidate: Candidate,
    source: Mapping[str, str],
    data: DeterministicGateData = DeterministicGateData(),
) -> GateResult:
    """Run the deterministic consistency gate on one candidate.

    Checks (all model-free):
      - glossary: known EN terms must use the established RU form.
      - names: known EN names must use the established RU form.
      - numbers: all numeric values from source must be preserved.
      - mixed_script: no unallowed Latin characters in RU text.

    Returns a ``GateResult`` with ``passed=True`` if no errors were found.
    The ``detail`` field contains a JSON-serialised list of errors when
    ``passed=False``.
    """
    import json

    errors: list[dict[str, str]] = []
    translation = dict(candidate.translation)

    all_terms = combine_glossary_terms(data.glossary_terms, data.names)

    for pid in candidate.pid_order():
        en_text = source.get(pid, "")
        target = translation.get(pid, "")
        if not target:
            errors.append({"pid": pid, "category": "missing", "problem": "Translation is empty or missing."})
            continue

        source_digits = _extract_digits(en_text)
        if source_digits:
            missing = _missing_numeric_values(source_digits, target)
            if missing:
                errors.append({
                    "pid": pid,
                    "category": "number",
                    "problem": f"Missing numeric values: {missing}",
                })

        if data.mixed_script_allow is not None:
            mixed = _find_mixed_script(target, data.mixed_script_allow)
            if mixed:
                errors.append({
                    "pid": pid,
                    "category": "mixed_script",
                    "problem": f"Latin or mixed-script token(s): {mixed}",
                })

        for en_term, ru_term in all_terms.items():
            if _source_term_present(en_text, en_term) and not _target_form_present(target, ru_term):
                errors.append({
                    "pid": pid,
                    "category": "glossary_consistency",
                    "problem": f"'{en_term}' should use '{ru_term}'",
                })

    if errors:
        return GateResult(
            gate="deterministic_consistency",
            passed=False,
            detail=json.dumps(errors, ensure_ascii=False),
        )
    return GateResult(
        gate="deterministic_consistency",
        passed=True,
        detail="All deterministic invariants satisfied.",
    )


# ---------------------------------------------------------------------------
# Model caller protocols (injectable; no default HTTP implementation)
# ---------------------------------------------------------------------------


class QwenEvaluator(Protocol):
    """Qwen evaluates fidelity of one candidate against the source.

    Receives the source (PID → EN text mapping) and the candidate's
    translation (PID → RU text mapping). Returns a ``GateResult`` with
    the fidelity verdict.

    The evaluator is expected to return JSON with keys:
      - ``faithful_to_source`` (bool)
      - ``completeness`` (bool)
      - ``introduced_errors`` (bool)
      - ``confidence`` (str: "high"/"medium"/"low")
      - ``reason`` (str)
      - ``passed`` (bool, implied or explicit)

    This protocol knows nothing about HTTP — the production wiring lives
    in the pipeline, not here.
    """

    def __call__(
        self, source: Mapping[str, str], translation: Mapping[str, str]
    ) -> GateResult: ...


class GemmaSelector(Protocol):
    """Gemma selects the best Russian among passing candidates.

    Receives a sequence of ``(candidate_id, PID→RU_text)`` mappings with
    NO English source. Returns a ``GateResult`` whose ``detail`` contains
    the preferred ``candidate_id`` (the ``passed`` field is always
    ``True`` for this gate; a selector that cannot choose is an error).

    Production wiring (prompt, HTTP call, JSON parsing) lives outside this
    module — Phase 2C is the *contract*, not the network client.
    """

    def __call__(
        self, candidates: Sequence[Tuple[str, Mapping[str, str]]]
    ) -> GateResult: ...


# ---------------------------------------------------------------------------
# Semantic disagreement check
# ---------------------------------------------------------------------------


def check_semantic_disagreement(
    candidates: Sequence[Candidate],
    source: Mapping[str, str],
) -> Tuple[bool, str]:
    """Check whether passing A/B candidates semantically disagree.

    Per spec, semantic disagreement is checked only between the primary
    generation candidates (fidelity_first / balanced_literary), not
    against a synthesis candidate that may already be present in the set.

    The check is heuristic and lightweight — this is a cheap escalation
    trigger, NOT a definitive semantic comparison (Phase 2C is NOT a Qwen
    comparison pass; that belongs to a dedicated evaluation call).

    Returns ``(disagreement_detected, reason)``.
    """
    # Only compare fidelity_first vs balanced_literary (A vs B), per spec
    a_b = [c for c in candidates if c.role in ("fidelity_first", "balanced_literary")]
    if len(a_b) < 2:
        return False, ""

    def _normalised_text(candidate: Candidate) -> str:
        parts = [text for _, text in candidate.translation if text]
        return " ".join(parts).casefold()

    texts = {candidate.candidate_id: _normalised_text(candidate) for candidate in a_b}
    ids = list(texts)
    reasons: list[str] = []

    def jaccard(a: str, b: str) -> float:
        set_a = set(a.split())
        set_b = set(b.split())
        if not set_a or not set_b:
            return 0.0
        return len(set_a & set_b) / len(set_a | set_b)

    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            similarity = jaccard(texts[ids[i]], texts[ids[j]])
            if similarity < 0.40:
                reasons.append(
                    f"Low token overlap ({similarity:.2f}) between "
                    f"{ids[i]} and {ids[j]} — possible semantic divergence"
                )

    if reasons:
        return True, "; ".join(reasons)
    return False, ""


# ---------------------------------------------------------------------------
# Selection result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SelectionResult:
    """Result of the Phase 2C cascaded selection for one chunk.

    One of three outcomes applies:
      * ``selected_candidate_id`` is non-``None`` → selection succeeded.
      * ``quarantine`` is ``True`` → no candidate passed; chunk goes to
        targeted synthesis/repair (NOT "least bad" selection).
      * ``needs_synthesis`` is ``True`` → semantic disagreement detected
        between passing A/B candidates; C/synthesis is needed.
    """

    chunk_id: str
    selected_candidate_id: Optional[str] = None
    selected_role: str = ""
    quarantine: bool = False
    quarantine_reason: str = ""
    needs_synthesis: bool = False
    synthesis_reason: str = ""
    disagreement_detected: bool = False
    disagreement_reason: str = ""
    candidates_evaluated: int = 0
    candidates_passed: int = 0
    candidates_failed: int = 0
    decision_trace: Tuple[GateResult, ...] = ()


# ---------------------------------------------------------------------------
# Main cascade entry point
# ---------------------------------------------------------------------------


def select_candidate(
    *,
    chunk_id: str,
    candidates: Sequence[Candidate],
    source: SourceArtifact,
    qwen_evaluator: QwenEvaluator,
    det_data: DeterministicGateData = DeterministicGateData(),
    gemma_selector: Optional[GemmaSelector] = None,
) -> SelectionResult:
    """Run the lexicographic cascaded selection on a set of candidates.

    Algorithm:

      1. Qwen fidelity gate — run on EVERY candidate (mandatory).
      2. Deterministic consistency gate — run only on candidates that
         passed Qwen.
      3. Collect candidates that passed BOTH gates.
      4. If no candidate passed → quarantine.
      5. If one candidate passed → select it.
      6. If multiple passed:
         a. Check for semantic disagreement.
         b. If disagreement and no synthesis candidate → ``needs_synthesis``.
         c. If disagreement and synthesis candidate present → Gemma
            preference among all passed.
         d. If no disagreement → Gemma preference among all passed.
      7. Record decision trace on the selected candidate.

    ``gemma_selector`` is optional: if omitted and multiple candidates
    pass without disagreement, passing candidates are sorted
    lexicographically by role (``fidelity_first`` before
    ``balanced_literary`` before ``synthesis``) and the first is
    selected. This ensures the cascade works even when Gemma is
    unavailable, though this is NOT the intended production path.

    If ``gemma_selector`` returns ``passed=False`` (unable to choose),
    the cascade quarantines the chunk — a selector that cannot choose
    is treated as an error, never silently resolved to a fallback.
    """
    if not candidates:
        return SelectionResult(
            chunk_id=chunk_id,
            quarantine=True,
            quarantine_reason="Empty candidate list — no candidates to evaluate.",
            candidates_evaluated=0,
        )

    source_map: Dict[str, str] = dict(source.source)
    traces: Dict[str, list[GateResult]] = {}
    failed: list[str] = []
    passed: list[Candidate] = []

    # ---- Stage 1: Qwen fidelity (mandatory, every candidate) --------------
    for candidate in candidates:
        translation = dict(candidate.translation)
        qwen_result = qwen_evaluator(source_map, translation)
        gate_trace = [qwen_result]
        if qwen_result.passed:
            traces[candidate.candidate_id] = gate_trace
        else:
            failed.append(candidate.candidate_id)

    # ---- Stage 2: Deterministic consistency (only Qwen-passed candidates) -
    for candidate in candidates:
        if candidate.candidate_id in failed:
            continue
        det_result = deterministic_consistency_gate(
            candidate=candidate,
            source=source_map,
            data=det_data,
        )
        traces[candidate.candidate_id].append(det_result)
        if det_result.passed:
            passed.append(candidate)
        else:
            failed.append(candidate.candidate_id)

    # ---- Decision ----------------------------------------------------------
    num_passed = len(passed)
    num_failed = len(failed)

    # Nobody passed → quarantine
    if num_passed == 0:
        reasons = []
        for cid in failed:
            reason = "Qwen fidelity fail"
            if cid in traces:
                for gate in traces[cid]:
                    if not gate.passed:
                        reason = f"{gate.gate}: {gate.detail[:200]}"
            reasons.append(f"{cid}: {reason}")
        return SelectionResult(
            chunk_id=chunk_id,
            quarantine=True,
            quarantine_reason="No candidate passed both Qwen and deterministic gates. "
            + "; ".join(reasons),
            candidates_evaluated=len(candidates),
            candidates_failed=num_failed,
        )

    # One passed → select directly
    if num_passed == 1:
        winner = passed[0]
        trace = tuple(traces.get(winner.candidate_id, ()))
        return SelectionResult(
            chunk_id=chunk_id,
            selected_candidate_id=winner.candidate_id,
            selected_role=winner.role,
            candidates_evaluated=len(candidates),
            candidates_passed=1,
            candidates_failed=num_failed,
            decision_trace=trace,
        )

    # Multiple passed → check for semantic disagreement
    disagreement, dis_reason = check_semantic_disagreement(passed, source_map)

    # Check if a synthesis candidate (role="synthesis") is already among passed
    has_synthesis = any(c.role == "synthesis" for c in passed)

    if disagreement and not has_synthesis:
        return SelectionResult(
            chunk_id=chunk_id,
            needs_synthesis=True,
            synthesis_reason=f"Semantic disagreement: {dis_reason}",
            disagreement_detected=True,
            disagreement_reason=dis_reason,
            candidates_evaluated=len(candidates),
            candidates_passed=num_passed,
            candidates_failed=num_failed,
        )

    # Multiple passed, no disagreement OR disagreement with synthesis present
    # → Gemma Russian preference (or fallback)
    _ROLE_ORDER = {"fidelity_first": 0, "balanced_literary": 1, "synthesis": 2}
    if gemma_selector is not None:
        gemma_input: list[Tuple[str, Dict[str, str]]] = [
            (candidate.candidate_id, dict(candidate.translation))
            for candidate in passed
        ]
        gemma_result = gemma_selector(gemma_input)
        if not gemma_result.passed:
            return SelectionResult(
                chunk_id=chunk_id,
                quarantine=True,
                quarantine_reason=(
                    "Gemma Russian preference selector could not choose "
                    f"among {num_passed} passing candidates. "
                    f"Reason: {gemma_result.detail}"
                ),
                disagreement_detected=disagreement,
                disagreement_reason=dis_reason,
                candidates_evaluated=len(candidates),
                candidates_passed=num_passed,
                candidates_failed=num_failed,
            )
        preferred_id = gemma_result.detail  # By convention, detail holds preferred_id
    else:
        passed_sorted = sorted(passed, key=lambda c: _ROLE_ORDER.get(c.role, 99))
        preferred_id = passed_sorted[0].candidate_id

    # Find the preferred candidate; if not found, quarantine (corrupted selector)
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
            candidates_evaluated=len(candidates),
            candidates_passed=num_passed,
            candidates_failed=num_failed,
        )
    winner = matching[0]
    trace = tuple(traces.get(winner.candidate_id, ()))

    return SelectionResult(
        chunk_id=chunk_id,
        selected_candidate_id=winner.candidate_id,
        selected_role=winner.role,
        disagreement_detected=disagreement,
        disagreement_reason=dis_reason,
        candidates_evaluated=len(candidates),
        candidates_passed=num_passed,
        candidates_failed=num_failed,
        decision_trace=trace,
    )
