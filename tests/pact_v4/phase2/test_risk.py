import dataclasses

import pytest

from pact_v4.phase2.risk import (
    RISK_POLICY,
    GlossaryEntry,
    RiskBand,
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


def test_policy_is_centralized_versioned_and_read_only() -> None:
    assert RISK_POLICY.medium_threshold < RISK_POLICY.high_threshold
    assert set(RISK_POLICY.weights) >= {
        "idiom_or_metaphor", "cultural_reference", "address_t_v_ambiguity",
        "ambiguous_referent", "negation", "numbers", "glossary_conflict",
    }
    with pytest.raises(TypeError):
        RISK_POLICY.weights["numbers"] = 99  # type: ignore[index]
