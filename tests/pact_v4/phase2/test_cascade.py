"""Tests for Phase 2C cascaded selection (pact_v4.phase2.cascade).

All model calls go through injectable evaluators/selectors — no real
llama-server, no production pipeline, no network access.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Sequence, Tuple

import pytest

from pact_v4.phase1.models import Candidate, GateResult, SourceArtifact, canonical_json_hash
from pact_v4.phase2.cascade import (
    DeterministicGateData,
    SelectionResult,
    check_semantic_disagreement,
    deterministic_consistency_gate,
    select_candidate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hash(seed: str) -> str:
    return canonical_json_hash({"seed": seed})


def make_source(
    chapter_id: str = "ch1", texts: Tuple[Tuple[str, str], ...] | None = None
) -> SourceArtifact:
    if texts is None:
        texts = (
            ("p0", "The steward opened the heavy oak door."),
            ("p1", "3 figures stood waiting in the cold rain."),
            ("p2", "She counted 20 silver coins on the table."),
        )
    return SourceArtifact(chapter_id=chapter_id, source=texts)


def make_candidate(
    candidate_id: str,
    chunk_id: str,
    role: str = "fidelity_first",
    translation: Tuple[Tuple[str, str], ...] | None = None,
    source: SourceArtifact | None = None,
) -> Candidate:
    if source is None:
        source = make_source()
    if translation is None:
        translation = (
            ("p0", "Стюард открыл тяжёлую дубовую дверь."),
            ("p1", "Три фигуры стояли в ожидании под холодным дождём."),
            ("p2", "Она пересчитала двадцать серебряных монет на столе."),
        )
    snapshot_hash = _hash("snap")
    chunk_plan_hash = _hash("plan")
    from pact_v4.phase1.models import ConfigArtifact, Snapshot, ChunkPlan, ChunkPlanArtifact

    snapshot = Snapshot(
        chapter_id=source.chapter_id,
        pids=tuple(pid for pid, _ in source.source),
        context="test-context",
        glossary_hash=_hash("glossary"),
        book_memory_hash=_hash("book"),
        chapter_memory_hash=_hash("chapter"),
    )
    pids = tuple(pid for pid, _ in translation)
    chunk = ChunkPlan(
        chunk_id=chunk_id,
        snapshot_hash=snapshot.snapshot_hash,
        pids=pids,
        undersized_exception=len(pids) < ChunkPlan.MIN_PIDS,
    )
    chunk_plan = ChunkPlanArtifact.create(snapshot, (chunk,))
    config = ConfigArtifact(version="v1", values={"model": "mock"})

    return Candidate.create(
        candidate_id=candidate_id,
        chunk_id=chunk_id,
        role=role,
        translation=translation,
        source=source,
        snapshot=snapshot,
        chunk_plan=chunk_plan,
        config=config,
    )


# ---------------------------------------------------------------------------
# Fixtures for model-call stubs
# ---------------------------------------------------------------------------

class StubQwen:
    """Returns a GateResult mimicking Qwen fidelity evaluation."""

    def __init__(self, passed: bool = True, reason: str = "OK"):
        self._passed = passed
        self._reason = reason
        self.calls: list[tuple] = []

    def __call__(self, source: Mapping[str, str], translation: Mapping[str, str]) -> GateResult:
        self.calls.append((source, translation))
        return GateResult(
            gate="qwen_fidelity",
            passed=self._passed,
            detail=self._reason,
        )


class StubGemma:
    """Returns a GateResult mimicking Gemma Russian preference."""

    def __init__(self, preferred_id: str = "", reason: str = "Better Russian"):
        self._preferred = preferred_id
        self._reason = reason
        self.calls: list[list] = []

    def __call__(
        self, candidates: Sequence[Tuple[str, Mapping[str, str]]]
    ) -> GateResult:
        self.calls.append(list(candidates))
        cid = self._preferred or (candidates[0][0] if candidates else "")
        return GateResult(
            gate="gemma_russian_preference",
            passed=True,
            detail=cid,
        )


# ---------------------------------------------------------------------------
# Tests: deterministic_consistency_gate
# ---------------------------------------------------------------------------


def test_det_gate_empty_candidate_fails():
    source = make_source()
    candidate = make_candidate("c1", "chunk1", translation=(("p0", ""), ("p1", ""), ("p2", "")))
    result = deterministic_consistency_gate(candidate=candidate, source=dict(source.source))
    assert not result.passed
    parsed = json.loads(result.detail)
    missing_pids = {e["pid"] for e in parsed if e["category"] == "missing"}
    assert missing_pids == {"p0", "p1", "p2"}


def test_det_gate_clean_candidate_passes():
    source = make_source()
    candidate = make_candidate("c1", "chunk1")
    result = deterministic_consistency_gate(candidate=candidate, source=dict(source.source))
    assert result.passed


def test_det_gate_glossary_violation():
    source = make_source()
    candidate = make_candidate("c1", "chunk1")
    det_data = DeterministicGateData(
        glossary_terms=(("steward", "дворецкий"),),  # candidate uses "стюард"
    )
    result = deterministic_consistency_gate(
        candidate=candidate, source=dict(source.source), data=det_data,
    )
    assert not result.passed
    parsed = json.loads(result.detail)
    glossary_errors = [e for e in parsed if e["category"] == "glossary_consistency"]
    assert len(glossary_errors) >= 1
    assert any("steward" in e["problem"] for e in glossary_errors)


def test_det_gate_glossary_satisfied():
    source = make_source()
    candidate = make_candidate("c1", "chunk1")
    det_data = DeterministicGateData(
        glossary_terms=(("steward", "стюард"),),  # candidate uses "стюард" ✓
    )
    result = deterministic_consistency_gate(
        candidate=candidate, source=dict(source.source), data=det_data,
    )
    assert result.passed


def test_det_gate_number_violation():
    source = make_source()
    candidate = make_candidate(
        "c1", "chunk1",
        translation=(
            ("p0", "Стюард открыл тяжёлую дубовую дверь."),
            ("p1", "Фигуры стояли в ожидании под холодным дождём."),  # "three" missing
            ("p2", "Она пересчитала монеты на столе."),  # "twenty" missing
        ),
    )
    result = deterministic_consistency_gate(candidate=candidate, source=dict(source.source))
    assert not result.passed
    parsed = json.loads(result.detail)
    number_errors = [e for e in parsed if e["category"] == "number"]
    assert len(number_errors) >= 2


def test_det_gate_mixed_script():
    source = make_source()
    candidate = make_candidate(
        "c1", "chunk1",
        translation=(
            ("p0", "The steward открыл тяжёлую дубовую дверь."),  # English residue
            ("p1", "Три фигуры стояли в ожидании под холодным дождём."),
            ("p2", "Она пересчитала двадцать серебряных монет на столе."),
        ),
    )
    result = deterministic_consistency_gate(candidate=candidate, source=dict(source.source))
    assert not result.passed
    parsed = json.loads(result.detail)
    mixed_errors = [e for e in parsed if e["category"] == "mixed_script"]
    assert len(mixed_errors) >= 1


def test_det_gate_mixed_script_allowlisted():
    source = make_source()
    candidate = make_candidate(
        "c1", "chunk1",
        translation=(
            ("p0", "Blake открыл тяжёлую дубовую дверь."),
            ("p1", "Три фигуры стояли в ожидании под холодным дождём."),
            ("p2", "Она пересчитала двадцать серебряных монет на столе."),
        ),
    )
    det_data = DeterministicGateData(mixed_script_allow=("Blake",))
    result = deterministic_consistency_gate(
        candidate=candidate, source=dict(source.source), data=det_data,
    )
    assert result.passed


def test_det_gate_empty_data_defaults_to_pass():
    source = make_source()
    candidate = make_candidate("c1", "chunk1")
    result = deterministic_consistency_gate(
        candidate=candidate, source=dict(source.source), data=DeterministicGateData(),
    )
    assert result.passed


# ---------------------------------------------------------------------------
# Tests: check_semantic_disagreement
# ---------------------------------------------------------------------------


def test_disagreement_near_identical_texts():
    source = make_source()
    a = make_candidate("A", "c1", role="fidelity_first")
    b = make_candidate(
        "B", "c1", role="balanced_literary",
        translation=(
            ("p0", "Стюард открыл тяжёлую дубовую дверь."),
            ("p1", "Три фигуры стояли под холодным дождём."),
            ("p2", "Она пересчитала двадцать серебряных монет на столе."),
        ),
    )
    disagree, reason = check_semantic_disagreement([a, b], dict(source.source))
    assert not disagree


def test_disagreement_very_different_texts():
    source = make_source()
    a = make_candidate("A", "c1", role="fidelity_first")
    b = make_candidate(
        "B", "c1", role="balanced_literary",
        translation=(
            ("p0", "Управляющий распахнул тяжёлую дубовую дверь."),  # completely different
            ("p1", "Под проливным дождём застыли три силуэта."),
            ("p2", "На столе она насчитала два десятка серебряных монет."),
        ),
    )
    disagree, reason = check_semantic_disagreement([a, b], dict(source.source))
    assert disagree


def test_disagreement_single_candidate_no_disagreement():
    source = make_source()
    a = make_candidate("A", "c1")
    disagree, reason = check_semantic_disagreement([a], dict(source.source))
    assert not disagree


def test_disagreement_empty_list_no_disagreement():
    source = make_source()
    disagree, reason = check_semantic_disagreement([], dict(source.source))
    assert not disagree


# ---------------------------------------------------------------------------
# Tests: select_candidate — empty / single candidate
# ---------------------------------------------------------------------------


def test_select_empty_candidates_quarantines():
    source = make_source()
    qwen = StubQwen()
    result = select_candidate(
        chunk_id="c1",
        candidates=[],
        source=source,
        qwen_evaluator=qwen,
    )
    assert result.quarantine
    assert "Empty candidate list" in result.quarantine_reason
    assert result.candidates_evaluated == 0


def test_select_single_candidate_passes_all():
    source = make_source()
    candidate = make_candidate("A", "c1", role="fidelity_first")
    qwen = StubQwen(passed=True)
    result = select_candidate(
        chunk_id="c1",
        candidates=[candidate],
        source=source,
        qwen_evaluator=qwen,
    )
    assert result.selected_candidate_id == "A"
    assert result.selected_role == "fidelity_first"
    assert not result.quarantine
    assert not result.needs_synthesis
    assert result.candidates_passed == 1
    assert result.candidates_failed == 0
    assert len(result.decision_trace) == 2  # qwen_fidelity + deterministic_consistency


def test_select_single_candidate_fails_qwen():
    source = make_source()
    candidate = make_candidate("A", "c1")
    qwen = StubQwen(passed=False, reason="Meaning not preserved")
    result = select_candidate(
        chunk_id="c1",
        candidates=[candidate],
        source=source,
        qwen_evaluator=qwen,
    )
    assert result.quarantine
    assert "Qwen fidelity fail" in result.quarantine_reason
    assert result.selected_candidate_id is None


def test_select_single_candidate_passes_qwen_fails_deterministic():
    source = make_source()
    candidate = make_candidate("c1", "chunk1", translation=(("p0", ""), ("p1", ""), ("p2", "")))
    qwen = StubQwen(passed=True)
    result = select_candidate(
        chunk_id="chunk1",
        candidates=[candidate],
        source=source,
        qwen_evaluator=qwen,
    )
    assert result.quarantine
    assert "deterministic_consistency" in result.quarantine_reason
    assert result.candidates_failed == 1


# ---------------------------------------------------------------------------
# Tests: select_candidate — multiple candidates
# ---------------------------------------------------------------------------


def test_select_two_both_pass_no_disagreement():
    source = make_source()
    a = make_candidate("A", "c1", role="fidelity_first")
    b = make_candidate("B", "c1", role="balanced_literary")
    qwen = StubQwen(passed=True)
    gemma = StubGemma(preferred_id="B", reason="Better Russian flow")
    result = select_candidate(
        chunk_id="c1",
        candidates=[a, b],
        source=source,
        qwen_evaluator=qwen,
        gemma_selector=gemma,
    )
    assert result.selected_candidate_id == "B"
    assert result.selected_role == "balanced_literary"
    assert not result.quarantine
    assert not result.needs_synthesis
    assert result.candidates_passed == 2
    assert result.candidates_failed == 0


def test_select_two_both_pass_semantic_disagreement():
    source = make_source()
    a = make_candidate("A", "c1", role="fidelity_first")
    b = make_candidate(
        "B", "c1", role="balanced_literary",
        translation=(
            ("p0", "Управляющий распахнул тяжёлую дубовую дверь."),
            ("p1", "Под проливным дождём застыли три силуэта."),
            ("p2", "На столе она насчитала двадцать серебряных монет."),
        ),
    )
    qwen = StubQwen(passed=True)
    result = select_candidate(
        chunk_id="c1",
        candidates=[a, b],
        source=source,
        qwen_evaluator=qwen,
    )
    assert result.needs_synthesis
    assert result.disagreement_detected
    assert result.synthesis_reason != ""
    assert result.selected_candidate_id is None
    assert not result.quarantine


def test_select_two_both_pass_disagreement_with_synthesis_candidate():
    source = make_source()
    a = make_candidate("A", "c1", role="fidelity_first")
    b = make_candidate(
        "B", "c1", role="balanced_literary",
        translation=(
            ("p0", "Управляющий распахнул тяжёлую дубовую дверь."),
            ("p1", "Под проливным дождём застыли три силуэта."),
            ("p2", "На столе она насчитала двадцать серебряных монет."),
        ),
    )
    c_synth = make_candidate(
        "C", "c1", role="synthesis",
        translation=(
            ("p0", "Стюард открыл тяжёлую дубовую дверь."),
            ("p1", "Три фигуры ждали под холодным дождём."),
            ("p2", "Она пересчитала двадцать серебряных монет на столе."),
        ),
    )
    qwen = StubQwen(passed=True)
    gemma = StubGemma(preferred_id="C", reason="Synthesis resolves disagreement")
    result = select_candidate(
        chunk_id="c1",
        candidates=[a, b, c_synth],
        source=source,
        qwen_evaluator=qwen,
        gemma_selector=gemma,
    )
    assert result.selected_candidate_id == "C"
    assert result.disagreement_detected
    assert not result.needs_synthesis
    assert result.candidates_passed == 3


def test_select_a_passes_b_fails_qwen():
    source = make_source()

    class SelectiveQwen(StubQwen):
        def __call__(self, source, translation):
            self.calls.append((source, translation))
            # B candidate has "управляющий" in p0 → synthetic Qwen fail for demonstration
            p0 = list(translation.values())[0] if translation else ""
            passed = "управляющий" not in p0.casefold()
            return GateResult(
                gate="qwen_fidelity",
                passed=passed,
                detail="OK" if passed else "Possible meaning drift",
            )

    a = make_candidate("A", "c1", role="fidelity_first")
    b = make_candidate(
        "B", "c1", role="balanced_literary",
        translation=(
            ("p0", "Управляющий открыл тяжёлую дубовую дверь."),
            ("p1", "Три фигуры стояли в ожидании под холодным дождём."),
            ("p2", "Она пересчитала двадцать серебряных монет на столе."),
        ),
    )
    qwen = SelectiveQwen()
    result = select_candidate(
        chunk_id="c1",
        candidates=[a, b],
        source=source,
        qwen_evaluator=qwen,
    )
    assert result.selected_candidate_id == "A"
    assert result.candidates_passed == 1
    assert result.candidates_failed == 1


def test_select_nobody_passes_quarantines_no_least_bad():
    source = make_source()
    a = make_candidate("A", "c1")
    b = make_candidate("B", "c1", role="balanced_literary")
    qwen = StubQwen(passed=False, reason="Semantic errors in both")
    result = select_candidate(
        chunk_id="c1",
        candidates=[a, b],
        source=source,
        qwen_evaluator=qwen,
    )
    assert result.quarantine
    assert result.selected_candidate_id is None
    assert result.candidates_failed == 2
    assert result.candidates_passed == 0
    assert "A:" in result.quarantine_reason
    assert "B:" in result.quarantine_reason


# ---------------------------------------------------------------------------
# Tests: decision trace
# ---------------------------------------------------------------------------


def test_decision_trace_is_recorded_on_selected_candidate():
    source = make_source()
    candidate = make_candidate("A", "c1", role="fidelity_first")
    qwen = StubQwen(passed=True, reason="Faithful to source")
    result = select_candidate(
        chunk_id="c1",
        candidates=[candidate],
        source=source,
        qwen_evaluator=qwen,
    )
    assert len(result.decision_trace) == 2
    gates = [g.gate for g in result.decision_trace]
    assert "qwen_fidelity" in gates
    assert "deterministic_consistency" in gates
    assert all(g.passed for g in result.decision_trace)


def test_decision_trace_preserves_fail_reasons():
    source = make_source()
    candidate = make_candidate("A", "c1", translation=(("p0", ""), ("p1", ""), ("p2", "")))
    qwen = StubQwen(passed=True)
    result = select_candidate(
        chunk_id="c1",
        candidates=[candidate],
        source=source,
        qwen_evaluator=qwen,
    )
    assert result.quarantine
    assert "deterministic_consistency" in result.quarantine_reason


# ---------------------------------------------------------------------------
# Tests: adversarial cases
# ---------------------------------------------------------------------------


def test_false_agreement_deterministic_catches_what_qwen_misses():
    """Both candidates pass Qwen (false agreement), but one has an injected
    number error that deterministic gate catches."""
    source = make_source(texts=(
        ("p0", "The steward opened the heavy oak door."),
        ("p1", "3 figures stood waiting in the cold rain."),
        ("p2", "She counted 20 silver coins on the table."),
    ))
    a = make_candidate("A", "c1", role="fidelity_first",
        translation=(
            ("p0", "Стюард открыл тяжёлую дубовую дверь."),
            ("p1", "Три фигуры стояли в ожидании под холодным дождём."),
            ("p2", "Она пересчитала 20 серебряных монет на столе."),
        ),
        source=source)  # clean
    b = make_candidate(
        "B", "c1", role="balanced_literary",
        translation=(
            ("p0", "Стюард открыл тяжёлую дубовую дверь."),
            ("p1", "Три фигуры стояли в ожидании под холодным дождём."),
            ("p2", "Она пересчитала монеты на столе."),  # "20" missing!
        ),
        source=source)
    qwen = StubQwen(passed=True, reason="Both look OK semantically")
    result = select_candidate(
        chunk_id="c1",
        candidates=[a, b],
        source=source,
        qwen_evaluator=qwen,
    )
    # Only A should have passed deterministic → A wins
    assert result.selected_candidate_id == "A"
    assert result.candidates_passed == 1
    assert result.candidates_failed == 1


def test_false_disagreement_both_pass_all_gates():
    """Two candidates have very different wording but both pass Qwen and
    deterministic → Gemma picks the best Russian."""
    source = make_source()
    a = make_candidate(
        "A", "c1", role="fidelity_first",
        translation=(
            ("p0", "Стюард открыл тяжёлую дубовую дверь."),
            ("p1", "Три фигуры стояли в ожидании под холодным дождём."),
            ("p2", "Она пересчитала двадцать серебряных монет на столе."),
        ),
    )
    b = make_candidate(
        "B", "c1", role="balanced_literary",
        translation=(
            ("p0", "Управляющий распахнул тяжёлую дубовую дверь."),
            ("p1", "Под проливным дождём застыли три силуэта."),
            ("p2", "На столе она насчитала двадцать серебряных монет."),
        ),
    )
    qwen = StubQwen(passed=True)
    gemma = StubGemma(preferred_id="B")
    result = select_candidate(
        chunk_id="c1",
        candidates=[a, b],
        source=source,
        qwen_evaluator=qwen,
        gemma_selector=gemma,
    )
    assert result.disagreement_detected
    assert result.needs_synthesis
    # Without synthesis candidate present, disagreement → needs_synthesis
    assert result.selected_candidate_id is None


def test_nobody_passes_never_selects_least_bad():
    """Even when Qwen evaluates all candidates and provides confidence levels,
    the cascade must NOT select the 'least bad' — it must quarantine."""
    source = make_source()
    a = make_candidate("A", "c1")
    b = make_candidate("B", "c1", role="balanced_literary")
    c = make_candidate("C", "c1", role="synthesis")
    qwen = StubQwen(passed=False, reason="All have semantic issues")
    result = select_candidate(
        chunk_id="c1",
        candidates=[a, b, c],
        source=source,
        qwen_evaluator=qwen,
    )
    assert result.quarantine
    assert result.selected_candidate_id is None
    assert result.candidates_passed == 0
    assert result.candidates_failed == 3


def test_gemma_not_available_fallback():
    """When Gemma is not provided, the first passing candidate is selected
    (lexicographically by role: fidelity_first before balanced_literary)."""
    source = make_source()
    a = make_candidate("A", "c1", role="fidelity_first")
    b = make_candidate("B", "c1", role="balanced_literary")
    qwen = StubQwen(passed=True)
    result = select_candidate(
        chunk_id="c1",
        candidates=[a, b],
        source=source,
        qwen_evaluator=qwen,
        gemma_selector=None,  # No Gemma
    )
    # Both pass, no disagreement → fallback picks first (fidelity_first)
    assert result.selected_candidate_id == "A"
    assert result.candidates_passed == 2


# ---------------------------------------------------------------------------
# Tests: deterministic_consistency_gate — additional edge cases
# ---------------------------------------------------------------------------


def test_det_gate_name_consistency():
    source = make_source(texts=(
        ("p0", "Blake walked to the door."),
        ("p1", "Rose followed him quietly."),
    ))
    candidate = make_candidate(
        "A", "c1",
        translation=(
            ("p0", "Блейк подошёл к двери."),
            ("p1", "Роуз тихо последовала за ним."),
        ),
        source=source,
    )
    det_data = DeterministicGateData(
        names=(("Blake", "Блейк"), ("Rose", "Роуз")),
    )
    result = deterministic_consistency_gate(
        candidate=candidate, source=dict(source.source), data=det_data,
    )
    # Candidate uses "Роуз" which matches — should pass
    assert result.passed


def test_det_gate_name_violation():
    source = make_source(texts=(
        ("p0", "Blake walked to the door."),
        ("p1", "Rose followed him quietly."),
    ))
    candidate = make_candidate(
        "A", "c1",
        translation=(
            ("p0", "Блейк подошёл к двери."),
            ("p1", "Роза тихо последовала за ним."),  # Should be "Роуз"
        ),
        source=source,
    )
    det_data = DeterministicGateData(
        names=(("Blake", "Блейк"), ("Rose", "Роуз")),
    )
    result = deterministic_consistency_gate(
        candidate=candidate, source=dict(source.source), data=det_data,
    )
    assert not result.passed
    parsed = json.loads(result.detail)
    name_errors = [e for e in parsed if e["category"] == "glossary_consistency"]
    assert any("Rose" in e["problem"] for e in name_errors)


def test_det_gate_multiple_numbers():
    source = make_source(texts=(
        ("p0", "There were 3 dogs, 7 cats, and 12 birds."),
    ))
    candidate = make_candidate(
        "A", "c1",
        translation=(
            ("p0", "Там было три собаки, семь кошек и двенадцать птиц."),
        ),
        source=source,
    )
    result = deterministic_consistency_gate(candidate=candidate, source=dict(source.source))
    assert result.passed


def test_det_gate_numbers_as_digits_in_russian():
    source = make_source(texts=(
        ("p0", "There were 3 dogs and 12 birds."),
    ))
    candidate = make_candidate(
        "A", "c1",
        translation=(
            ("p0", "Там было 3 собаки и 12 птиц."),
        ),
        source=source,
    )
    result = deterministic_consistency_gate(candidate=candidate, source=dict(source.source))
    assert result.passed


# ---------------------------------------------------------------------------
# Tests: SelectionResult immutability
# ---------------------------------------------------------------------------


def test_selection_result_is_immutable():
    result = SelectionResult(
        chunk_id="c1",
        selected_candidate_id="A",
        selected_role="fidelity_first",
        candidates_evaluated=1,
        candidates_passed=1,
    )
    with pytest.raises(Exception):
        result.selected_candidate_id = "B"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Tests: select_candidate respects risk_gated rule — all candidates MUST go
#         through Qwen; no "skipping because risk is low"
# ---------------------------------------------------------------------------


def test_all_candidates_go_through_qwen_even_low_risk_single():
    """Even with a single candidate (low risk), Qwen fidelity is mandatory."""
    source = make_source()
    candidate = make_candidate("A", "c1")
    qwen = StubQwen(passed=True)
    result = select_candidate(
        chunk_id="c1",
        candidates=[candidate],
        source=source,
        qwen_evaluator=qwen,
    )
    assert len(qwen.calls) == 1
    assert result.selected_candidate_id == "A"
