from pact_v4.phase0b.risk import assess_risk
from pact_v4.phase0b.source_html import parse_source_html


def _block(html: str):
    return parse_source_html(html)[0]


def test_low_risk_short_prose_gets_low_band() -> None:
    block = _block("<p>the sky was clear and warm.</p>")
    r = assess_risk(block)
    assert r.band == "low"
    assert r.score < 2


def test_numbers_boost_risk() -> None:
    block = _block("<p>He said 3 things, then 4, then 5 more.</p>")
    r = assess_risk(block)
    assert "numbers" in r.types
    assert r.band in {"med", "high"}


def test_dialogue_and_negation_combine() -> None:
    block = _block('<p>"No, never," she said. "Not tomorrow."</p>')
    r = assess_risk(block)
    assert "negation" in r.types
    assert "dialogue" in r.types
    assert "quotation" in r.types
    # negation counted multiple times, quotation counted
    assert r.band in {"med", "high"}


def test_names_recognised_and_not_sentence_start_only() -> None:
    block = _block("<p>Amy called Robert to visit Chicago on Friday.</p>")
    r = assess_risk(block)
    assert r.signals.get("names", 0) >= 1
    assert "temporal" in r.types  # Friday


def test_long_span_flagged() -> None:
    words = " ".join(["word"] * 90)
    block = _block(f"<p>{words}</p>")
    r = assess_risk(block)
    assert "long_span" in r.types
