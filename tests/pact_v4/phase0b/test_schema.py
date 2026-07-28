from copy import deepcopy

import pytest

from pact_v4.phase0b import SCHEMA_ID
from pact_v4.phase0b.schema import SchemaError, load_schema, validate


def _valid_record() -> dict:
    return {
        "schema": SCHEMA_ID,
        "record_id": "ch044-p00001",
        "chapter": "044",
        "pid": "p00001",
        "source": {
            "language": "en",
            "text": "Sample.",
            "html": "<p>Sample.</p>",
            "structural_role": "paragraph",
            "inline_spans": [],
            "word_count": 1,
        },
        "risk": {"band": "low", "types": [], "signals": {}},
        "invariants": {
            "must_preserve": [],
            "must_not_add": [],
            "formatting_expectation": {"required_spans": []},
        },
        "known_violations": [],
        "reference": {
            "text": "",
            "html": "",
            "source": "human_translation_epub",
            "note": "reference only; not an exact-match ground truth",
            "alignment": {"method": "none", "confidence": 0.0},
        },
        "verdict": {"status": "unreviewed"},
        "provenance": {
            "source_file": "/tmp/en.html",
            "reference_file": "/tmp/ru.epub",
            "source_hash": "0" * 64,
            "reference_hash": "1" * 64,
            "tool_version": "phase0b/1",
            "generated_at": "2026-07-28T12:00:00+00:00",
        },
    }


def test_valid_record_has_no_errors() -> None:
    assert validate(_valid_record()) == []


def test_wrong_schema_const_flagged() -> None:
    r = _valid_record()
    r["schema"] = "pact-v4-golden-record/v99"
    errs = validate(r)
    assert any("expected const" in e for e in errs)


def test_pid_pattern_enforced() -> None:
    r = _valid_record()
    r["pid"] = "PID_001"
    errs = validate(r)
    assert any("does not match pattern" in e for e in errs)


def test_risk_band_enum_enforced() -> None:
    r = _valid_record()
    r["risk"]["band"] = "critical"
    errs = validate(r)
    assert any("enum" in e for e in errs)


def test_missing_required_field_flagged() -> None:
    r = _valid_record()
    del r["provenance"]
    errs = validate(r)
    assert any("provenance: missing required" in e for e in errs)


def test_reference_note_const_enforced() -> None:
    r = _valid_record()
    r["reference"]["note"] = "ground truth"
    errs = validate(r)
    assert any("reference.note" in e for e in errs)


def test_hash_pattern_enforced() -> None:
    r = _valid_record()
    r["provenance"]["source_hash"] = "not_a_hash"
    errs = validate(r)
    assert any("source_hash" in e for e in errs)


def test_boolean_is_not_accepted_as_integer() -> None:
    r = _valid_record()
    r["source"]["word_count"] = True  # type: ignore[assignment]
    errs = validate(r)
    assert any("boolean" in e for e in errs)


def test_unknown_ref_raises_schema_error() -> None:
    schema = load_schema()
    schema["properties"]["source"] = {"$ref": "#/$defs/does_not_exist"}
    with pytest.raises(SchemaError):
        validate(_valid_record(), schema)


def test_alignment_confidence_bounds_enforced() -> None:
    r = _valid_record()
    r["reference"]["alignment"]["confidence"] = 1.5
    errs = validate(r)
    assert any("> maximum" in e for e in errs)


def test_inline_spans_validated_via_defs() -> None:
    r = _valid_record()
    r["source"]["inline_spans"] = [
        {"span_id": "em01", "tag": "em", "text": "x", "occurrence": 0}
    ]
    errs = validate(r)
    assert any("occurrence" in e and "< minimum" in e for e in errs)
