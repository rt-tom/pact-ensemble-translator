from pact_v4.phase0b.source_html import parse_source_html


def test_leaf_blocks_get_stable_pids(en_html: str) -> None:
    blocks = parse_source_html(en_html)
    assert [b.pid for b in blocks] == [
        "p00001", "p00002", "p00003", "p00004", "p00005", "p00006",
    ]
    assert [b.structural_role for b in blocks] == [
        "heading", "paragraph", "dialogue", "paragraph", "paragraph",
        "blockquote",
    ]


def test_inline_spans_captured_with_occurrence(en_html: str) -> None:
    blocks = parse_source_html(en_html)
    p4 = blocks[3]  # "He walked slowly toward the gate."
    assert p4.pid == "p00004"
    tags = [(s.tag, s.text, s.occurrence, s.span_id) for s in p4.inline_spans]
    assert tags == [
        ("em", "slowly", 1, "em01"),
        ("strong", "gate", 1, "strong01"),
    ]


def test_script_and_style_removed() -> None:
    html = (
        "<p>keep me</p>"
        "<script>alert(1)</script>"
        "<style>p { color: red }</style>"
    )
    blocks = parse_source_html(html)
    assert [b.text for b in blocks] == ["keep me"]


def test_nested_blocks_are_not_double_counted() -> None:
    html = "<blockquote><p>quoted</p></blockquote>"
    blocks = parse_source_html(html)
    # The <p> is the leaf; blockquote must not double-emit.
    assert len(blocks) == 1
    assert blocks[0].tag == "p"
    assert blocks[0].text == "quoted"


def test_dialogue_role_detected_by_quote_char() -> None:
    blocks = parse_source_html('<p>"Hello there."</p>')
    assert blocks[0].structural_role == "dialogue"
