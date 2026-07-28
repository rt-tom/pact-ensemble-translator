from pathlib import Path

from pact_v4.phase0b.alignment import AlignmentPair, align_structural
from pact_v4.phase0b.golden_records import (
    build_record,
    dump_records,
    load_records,
)
from pact_v4.phase0b.reference_epub import parse_reference_xhtml
from pact_v4.phase0b.risk import assess_risk
from pact_v4.phase0b.schema import validate
from pact_v4.phase0b.source_html import parse_source_html


def _provenance() -> dict:
    return {
        "source_file": "/tmp/en.html",
        "reference_file": "/tmp/ru.xhtml",
        "reference_entry": "",
        "source_hash": "a" * 64,
        "reference_hash": "b" * 64,
    }


def test_build_record_matches_schema(en_html: str, ru_xhtml: str) -> None:
    sources = parse_source_html(en_html)
    references = parse_reference_xhtml(ru_xhtml)
    pairs = align_structural(sources, references)
    for src, pair in zip(sources, pairs):
        ref = references[pair.reference_index - 1]
        record = build_record(
            chapter="044",
            source=src,
            risk=assess_risk(src),
            pair=pair,
            reference=ref,
            provenance=_provenance(),
        )
        errs = validate(record)
        assert errs == [], f"{src.pid} produced errors: {errs}"


def test_auto_verdict_uses_needs_review_on_low_confidence(en_html: str) -> None:
    src = parse_source_html(en_html)[0]
    pair = AlignmentPair(
        pid=src.pid, source_index=src.index, reference_index=1,
        method="heuristic_length", confidence=0.35,
        note="differ",
    )
    r = build_record(
        chapter="044", source=src, risk=assess_risk(src), pair=pair,
        reference=None, provenance=_provenance(),
    )
    assert r["verdict"]["status"] == "needs_review"


def test_auto_verdict_never_accepts(en_html: str) -> None:
    for src in parse_source_html(en_html):
        pair = AlignmentPair(
            pid=src.pid, source_index=src.index, reference_index=1,
            method="structural_order", confidence=0.9, note=None,
        )
        r = build_record(
            chapter="044", source=src, risk=assess_risk(src), pair=pair,
            reference=None, provenance=_provenance(),
        )
        assert r["verdict"]["status"] != "accepted"


def test_dump_and_load_round_trip(tmp_path: Path, en_html: str, ru_xhtml: str) -> None:
    sources = parse_source_html(en_html)
    references = parse_reference_xhtml(ru_xhtml)
    pairs = align_structural(sources, references)
    records = [
        build_record(
            chapter="044", source=s, risk=assess_risk(s), pair=p,
            reference=references[p.reference_index - 1],
            provenance=_provenance(),
        )
        for s, p in zip(sources, pairs)
    ]
    out = tmp_path / "records.json"
    dump_records(records, out)
    loaded = load_records(out)
    assert loaded == records


def test_numbers_promoted_to_must_preserve() -> None:
    src = parse_source_html("<p>He counted 42 chairs.</p>")[0]
    pair = AlignmentPair(
        pid=src.pid, source_index=src.index, reference_index=None,
        method="none", confidence=0.0, note=None,
    )
    record = build_record(
        chapter="044", source=src, risk=assess_risk(src), pair=pair,
        reference=None, provenance=_provenance(),
    )
    kinds = [i["kind"] for i in record["invariants"]["must_preserve"]]
    values = [i["value"] for i in record["invariants"]["must_preserve"]]
    assert "number" in kinds
    assert "42" in values
