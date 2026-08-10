"""B1.1 contract tests for pact_v4.audit.hard_filters (Tier A hard filters).

Acceptance (card t_05c04093):
- p00132-type exact adjacent duplicate -> CONFIRMED
- «Две минуты первого = 1:02»-FP -> REJECTED (number/time normalization)
- nurse-issue with explicit current-source fact (Rich male) -> REJECTED
- entity-relation issue (bike=motorcycle) -> NOT Tier A (TIER_B, §5.3)
- compact regression tests; full suite passes
"""
from __future__ import annotations

import pytest

from pact_v4.audit.hard_filters import (
    CONFIRMED,
    REJECTED,
    TIER_B,
    apply_hard_filters,
    find_adjacent_duplicate,
    normalized_numeric_values,
)


def _issue(pid, category, note="", excerpt="", severity="major", confidence="high"):
    return {
        "id": pid,
        "category": category,
        "severity": severity,
        "confidence": confidence,
        "note": note,
        "excerpt": excerpt,
    }


def _verdicts(issues, source, translation, **kwargs):
    return {
        f.issue["id"]: f
        for f in apply_hard_filters(issues, source=source, translation=translation, **kwargs)
    }


# --- acceptance: duplicates -------------------------------------------------


def test_adjacent_duplicate_confirmed_tier_a():
    issue = _issue(
        "p00132", "addition",
        note="в гости в гости — повторение",
        excerpt="в гости в гости",
    )
    result = _verdicts(
        [issue],
        source={"p00132": "They went to visit their aunt."},
        translation={"p00132": "Они пошли в гости в гости к тёте."},
    )
    assert result["p00132"].verdict == CONFIRMED
    assert result["p00132"].filter_name == "adjacent_duplicate"


def test_duplicate_detector_finds_bigram_and_ignores_hyphen_reduplication():
    assert find_adjacent_duplicate("Они пошли в гости в гости.") == ("в", "гости")
    assert find_adjacent_duplicate("он он") == ("он",)
    # hyphenated Russian reduplication is legitimate, not an error
    assert find_adjacent_duplicate("Да-да, конечно.") is None
    assert find_adjacent_duplicate("Нет ошибок.") is None


# --- acceptance: numbers/time normalization ---------------------------------


def test_time_normalization_fp_rejected():
    # «Две минуты первого = 1:02»-FP: source and translation are the same
    # time (00:02); the auditor's numeric claim is a false positive.
    issue = _issue(
        "p00285", "changed_fact",
        note="Two past twelve translated as Две минуты первого (12:58), "
             "materially changing the time from 12:02.",
        excerpt="Две минуты первого",
    )
    result = _verdicts(
        [issue],
        source={"p00285": "It was two past twelve."},
        translation={"p00285": "Было две минуты первого."},
    )
    assert result["p00285"].verdict == REJECTED
    assert result["p00285"].filter_name == "number_time"


def test_number_word_normalization_ru_genitive():
    # девяти/десяти: genitive number words normalize to the same value.
    assert normalized_numeric_values("Десяти.", "ru") == ((), (10,))
    assert normalized_numeric_values("девяти", "ru") == ((), (9,))
    assert normalized_numeric_values("Ten.", "en") == ((), (10,))


def test_time_normalization_equivalence():
    assert normalized_numeric_values("two past twelve", "en") == ((2,), ())
    assert normalized_numeric_values("две минуты первого", "ru") == ((2,), ())
    assert normalized_numeric_values("half past ten", "en") == ((630,), ())
    assert normalized_numeric_values("половина одиннадцатого", "ru") == ((630,), ())
    assert normalized_numeric_values("five to three", "en") == ((175,), ())
    assert normalized_numeric_values("без пяти три", "ru") == ((175,), ())


def test_real_numeric_mismatch_confirmed():
    issue = _issue(
        "p00099", "changed_fact",
        note="source says twelve chairs but translation says десять (10)",
        excerpt="десять стульев",
    )
    result = _verdicts(
        [issue],
        source={"p00099": "There were twelve chairs in the hall."},
        translation={"p00099": "В зале стояло десять стульев."},
    )
    assert result["p00099"].verdict == CONFIRMED
    assert result["p00099"].filter_name == "number_time"


# --- acceptance: direct current-source gender fact ---------------------------


def test_nurse_issue_with_explicit_source_fact_rejected():
    # The auditor (poisoned narrator context "The Nurse: female") flagged the
    # translation's masculine forms; the CURRENT source explicitly establishes
    # Rich as male (he/him/his) — the finding is a deterministic false positive.
    issue = _issue(
        "p00184", "invented_gender",
        note="the nurse is referred to with masculine forms but should be female",
        excerpt="медбрат",
    )
    result = _verdicts(
        [issue],
        source={"p00184": "Rich was a nurse. He had trained at the city hospital. "
                          "His hands were steady."},
        translation={"p00184": "Рич был медбратом. Он учился в городской больнице. "
                               "Его руки не дрожали."},
    )
    assert result["p00184"].verdict == REJECTED
    assert result["p00184"].filter_name == "source_gender"


def test_invented_gender_without_explicit_source_trace_stays_tier_b():
    # p00010-class: source has no explicit gender marker — needs semantics.
    issue = _issue(
        "p00010", "invented_gender",
        note="source does not specify gender of the wannabe-architect",
        excerpt="девушкой, мечтавшей стать архитектором",
    )
    result = _verdicts(
        [issue],
        source={"p00010": "A wannabe-architect from the neighborhood."},
        translation={"p00010": "Девушкой, мечтавшей стать архитектором, из соседнего квартала."},
    )
    assert result["p00010"].verdict == TIER_B


# --- acceptance: chapter entity context is NEVER Tier A (§5.3) ----------------


def test_entity_relation_issue_never_tier_a():
    # bike=motorcycle depends on chapter entity facts — always Tier B, even
    # though the finding is a changed_fact about an object.
    issue = _issue(
        "p00097", "changed_fact",
        note="bike translated as велосипед (bicycle), but CHAPTER ENTITY FACTS "
             "establish Blake's vehicle as a motorcycle",
        excerpt="Велосипед?",
    )
    result = _verdicts(
        [issue],
        source={"p00097": "He looked at the bike."},
        translation={"p00097": "Он посмотрел на велосипед."},
    )
    assert result["p00097"].verdict == TIER_B
    assert result["p00097"].filter_name == "entity_context"


def test_entity_context_argument_forces_tier_b_by_pid():
    # Even without note markers, an issue whose PID is an anchor/alias of an
    # entity-context claim is forced to Tier B (presence of verified spans
    # does not promote the relation to Tier A).
    issue = _issue(
        "p00097", "changed_fact",
        note="object identity differs",
        excerpt="велосипед",
    )
    entity_context = [
        {
            "entity": "Blake",
            "claims": [
                {
                    "kind": "alias_relation",
                    "value": "bike = motorcycle",
                    "status": "candidate",
                    "evidence_windows": [["p00007", "p00097"]],
                }
            ],
        }
    ]
    result = _verdicts(
        [issue],
        source={"p00097": "He looked at the bike."},
        translation={"p00097": "Он посмотрел на велосипед."},
        entity_context=entity_context,
    )
    assert result["p00097"].verdict == TIER_B
    assert result["p00097"].filter_name == "entity_context"


# --- structure: PID outside chunk / invalid category -> REJECT ----------------


def test_pid_outside_chunk_rejected():
    issue = _issue("p00400", "changed_fact", note="x")
    result = _verdicts(
        [issue],
        source={"p00400": "text"},
        translation={"p00400": "текст"},
        chunk_pids={"p00001", "p00002"},
    )
    assert result["p00400"].verdict == REJECTED
    assert result["p00400"].filter_name == "structure"


def test_invalid_category_rejected():
    issue = _issue("p00001", "stylistic", note="x")
    result = _verdicts(
        [issue],
        source={"p00001": "text"},
        translation={"p00001": "текст"},
    )
    assert result["p00001"].verdict == REJECTED
    assert result["p00001"].filter_name == "structure"


def test_missing_pid_from_source_rejected():
    issue = _issue("p99999", "changed_fact", note="x")
    result = _verdicts(
        [issue],
        source={"p00001": "text"},
        translation={"p00001": "текст"},
    )
    assert result["p99999"].verdict == REJECTED


def test_invalid_severity_rejected():
    issue = _issue("p00001", "changed_fact", note="x", severity="critical")
    result = _verdicts(
        [issue],
        source={"p00001": "text"},
        translation={"p00001": "текст"},
    )
    assert result["p00001"].verdict == REJECTED


# --- default: semantic verification -------------------------------------------


def test_semantic_negation_issue_defaults_to_tier_b():
    # p00093-class (negation scope) — no hard filter decides it.
    issue = _issue(
        "p00093", "negation",
        note="didn't know the story already vs уже не знал",
        excerpt="уже не знал этой истории",
    )
    result = _verdicts(
        [issue],
        source={"p00093": "someone who didn't already know the story"},
        translation={"p00093": "с кем-то, кто уже не знал этой истории"},
    )
    assert result["p00093"].verdict == TIER_B


def test_changed_fact_without_numeric_hint_defaults_to_tier_b():
    # p00013-class (printed -> вышито): no numeric/time content, no duplicate,
    # no gender category — must stay Tier B, never rejected by a coincidental
    # digit in another field.
    issue = _issue(
        "p00013", "changed_fact",
        note="source states the word was printed on the mat",
        excerpt="вышито «добро пожаловать»",
    )
    result = _verdicts(
        [issue],
        source={"p00013": "The word 'welcome' was printed on the mat."},
        translation={"p00013": "На коврике было вышито «добро пожаловать»."},
    )
    assert result["p00013"].verdict == TIER_B


def test_result_order_preserves_input_order():
    issues = [
        _issue("p00001", "changed_fact", note="x"),
        _issue("p00002", "addition", note="в гости в гости"),
    ]
    results = apply_hard_filters(
        issues,
        source={"p00001": "text", "p00002": "They went to visit."},
        translation={"p00001": "текст", "p00002": "Они пошли в гости в гости."},
    )
    assert [r.issue["id"] for r in results] == ["p00001", "p00002"]
    assert results[1].verdict == CONFIRMED
