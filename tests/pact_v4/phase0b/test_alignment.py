from pact_v4.phase0b.alignment import (
    HEURISTIC_CONFIDENCE,
    STRUCTURAL_CONFIDENCE,
    align_structural,
)
from pact_v4.phase0b.reference_epub import parse_reference_xhtml
from pact_v4.phase0b.source_html import parse_source_html


def test_equal_lengths_align_1_to_1(en_html: str, ru_xhtml: str) -> None:
    src = parse_source_html(en_html)
    ref = parse_reference_xhtml(ru_xhtml)
    assert len(src) == len(ref)
    pairs = align_structural(src, ref)
    for i, pair in enumerate(pairs):
        assert pair.source_index == i
        assert pair.reference_index == i + 1
        assert pair.method == "structural_order"
        assert pair.confidence == STRUCTURAL_CONFIDENCE
        assert pair.note is None


def test_unequal_lengths_flag_heuristic(en_html: str, ru_xhtml_uneven: str) -> None:
    src = parse_source_html(en_html)
    ref = parse_reference_xhtml(ru_xhtml_uneven)
    pairs = align_structural(src, ref)
    assert all(p.method == "heuristic_length" for p in pairs)
    assert all(p.confidence == HEURISTIC_CONFIDENCE for p in pairs)
    assert all(p.note and "manual verification" in p.note for p in pairs)
    # Reference indexes still bounded by the reference length.
    for p in pairs:
        assert p.reference_index is not None
        assert 1 <= p.reference_index <= len(ref)


def test_no_reference_marks_none(en_html: str) -> None:
    src = parse_source_html(en_html)
    pairs = align_structural(src, [])
    assert all(p.reference_index is None for p in pairs)
    assert all(p.method == "none" for p in pairs)
    assert all(p.confidence == 0.0 for p in pairs)
