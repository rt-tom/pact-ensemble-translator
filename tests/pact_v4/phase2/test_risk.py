import dataclasses
from types import MappingProxyType

import pytest

from pact_v4.phase2.risk import (
    REQUIRED_RISK_CATEGORIES,
    RISK_POLICY,
    GlossaryEntry,
    RiskBand,
    RiskPolicy,
    assess_source_risk,
)


def _codes(result):
    return [feature.code for feature in result.features]


def test_known_low_risk_plain_source_has_no_raise_explanation() -> None:
    result = assess_source_risk(
        (("p001", "The warm wind crossed the empty field."),), glossary=()
    )
    assert result.band is RiskBand.LOW
    assert result.score == 0
    assert result.policy_version == RISK_POLICY.version
    assert result.features == ()


def test_known_medium_risk_explains_each_raise() -> None:
    result = assess_source_risk(
        (("p001", "You must not open box 7."),), glossary=()
    )
    assert result.band is RiskBand.MEDIUM
    assert _codes(result) == ["address_t_v_ambiguity", "negation", "numbers"]
    assert all(feature.explanation and feature.evidence for feature in result.features)


def test_known_high_risk_explains_source_only_signals_and_glossary_conflict() -> None:
    result = assess_source_risk(
        (
            ("p001", "At Thanksgiving, Alice told Robert to break the ice."),
            ("p002", "She said it would cost 20 dollars."),
        ),
        glossary=(
            GlossaryEntry("ice", ("лёд",)),
            GlossaryEntry("ice", ("холод",)),
        ),
    )
    assert result.band is RiskBand.HIGH
    assert _codes(result) == [
        "idiom_or_metaphor",
        "cultural_reference",
        "ambiguous_referent",
        "numbers",
        "glossary_conflict",
    ]
    assert all(feature.explanation and feature.evidence for feature in result.features)


def test_identical_input_produces_identical_immutable_result() -> None:
    source = (("p001", "You never visit Broadway twice."),)
    first = assess_source_risk(source, glossary=())
    second = assess_source_risk(source, glossary=())
    assert first == second
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.score = 0  # type: ignore[misc]


@pytest.mark.parametrize(
    ("source", "source_complete", "expected"),
    [
        (None, True, "missing_source"),
        ((), True, "incomplete_source"),
        ((("p001", "Known text"),), False, "incomplete_source"),
        ((("p001", "Known"), ("p001", "Duplicate")), True, "incomplete_source"),
    ],
)
def test_unknown_or_incomplete_source_is_explicit_and_high(
    source, source_complete, expected
) -> None:
    result = assess_source_risk(
        source, glossary=(), source_complete=source_complete
    )
    assert result.band is RiskBand.HIGH
    assert expected in _codes(result)


def test_unknown_glossary_is_explicit_and_conservative() -> None:
    result = assess_source_risk(
        (("p001", "The warm wind crossed the field."),), glossary=None
    )
    assert result.band is RiskBand.MEDIUM
    assert _codes(result) == ["unknown_glossary_context"]


def test_malformed_glossary_is_explicit_and_conservative() -> None:
    result = assess_source_risk(
        (("p001", "The warm wind crossed the field."),),
        glossary=(object(),),  # type: ignore[arg-type]
    )
    assert result.band is RiskBand.MEDIUM
    assert _codes(result) == ["incomplete_glossary_context"]


def test_glossary_single_prescribed_form_is_not_a_conflict() -> None:
    result = assess_source_risk(
        (("p001", "The Warden opened the door."),),
        glossary=(GlossaryEntry("warden", ("смотритель",)),),
    )
    assert "glossary_conflict" not in _codes(result)


def test_unambiguous_multi_row_pronoun_is_not_flagged_ambiguous() -> None:
    # Regression: a single-antecedent, three-row scene must stay low risk.
    # ``len(rows) >= 2`` used to be an alternative trigger for
    # ``ambiguous_referent`` and fired on almost any multi-PID chunk that
    # contained a pronoun, defeating the "low risk = majority" design intent.
    result = assess_source_risk(
        (
            ("p001", "Maria opened the door."),
            ("p002", "She smiled and stepped inside."),
            ("p003", "It was cold outside."),
        ),
        glossary=(),
    )
    assert result.band is RiskBand.LOW
    assert "ambiguous_referent" not in _codes(result)


def test_sentence_initial_capitalized_word_is_not_a_name() -> None:
    # Regression: _NAME_RE's exclusion of sentence-initial capitals relied on
    # a preceding ". " and never matched the very first word of the whole
    # text, so it was misdetected as a proper name (e.g. "At").
    result = assess_source_risk(
        (("p001", "At dawn, Alice said it was cold outside."),), glossary=()
    )
    assert "ambiguous_referent" not in _codes(result)


def test_policy_is_centralized_versioned_and_read_only() -> None:
    assert RISK_POLICY.medium_threshold < RISK_POLICY.high_threshold
    assert set(RISK_POLICY.weights) >= {
        "idiom_or_metaphor", "cultural_reference", "address_t_v_ambiguity",
        "ambiguous_referent", "negation", "numbers", "glossary_conflict",
    }
    with pytest.raises(TypeError):
        RISK_POLICY.weights["numbers"] = 99  # type: ignore[index]


# ---------------------------------------------------------------------------
# number_word / tone_profanity — required risk/gate categories
# (Phase 0C Gate policy, docs/plans/V4_PHASE_0C_GATE_NOTE_RU.md §2).
# ---------------------------------------------------------------------------

def test_required_risk_categories_are_number_word_and_tone_profanity() -> None:
    assert REQUIRED_RISK_CATEGORIES == frozenset({"number_word", "tone_profanity"})
    assert REQUIRED_RISK_CATEGORIES <= set(RISK_POLICY.weights)


def _policy_without(category: str) -> dict:
    weights = dict(RISK_POLICY.weights)
    del weights[category]
    return dict(
        version="test-policy-missing-required-category",
        weights=MappingProxyType(weights),
        medium_threshold=RISK_POLICY.medium_threshold,
        high_threshold=RISK_POLICY.high_threshold,
        force_high=RISK_POLICY.force_high,
    )


def test_policy_construction_fails_without_number_word() -> None:
    with pytest.raises(ValueError, match="number_word"):
        RiskPolicy(**_policy_without("number_word"))


def test_policy_construction_fails_without_tone_profanity() -> None:
    with pytest.raises(ValueError, match="tone_profanity"):
        RiskPolicy(**_policy_without("tone_profanity"))


def test_number_word_flags_written_out_numbers() -> None:
    result = assess_source_risk(
        (("p001", "There were twelve guests at the table."),), glossary=()
    )
    assert "number_word" in _codes(result)
    feature = next(f for f in result.features if f.code == "number_word")
    assert "twelve" in feature.evidence


def test_number_word_excludes_pronominal_one_half_quarter() -> None:
    result = assess_source_risk(
        (("p001", "She ate half of one quarter of the pie."),), glossary=()
    )
    assert "number_word" not in _codes(result)


def test_tone_profanity_flags_strong_source_language() -> None:
    result = assess_source_risk(
        (("p001", "He was a real bastard about it."),), glossary=()
    )
    assert "tone_profanity" in _codes(result)
    feature = next(f for f in result.features if f.code == "tone_profanity")
    assert "bastard" in feature.evidence


def test_plain_source_does_not_flag_number_word_or_profanity() -> None:
    result = assess_source_risk(
        (("p001", "The warm wind crossed the empty field."),), glossary=()
    )
    assert "number_word" not in _codes(result)
    assert "tone_profanity" not in _codes(result)
