"""v41 italics restore — unit / regression tests for formatting model-call + wrap."""

import json
import pytest
from pact_v4.phase0b.source_html import parse_source_html
from pact_v4.phase5.formatting import (
    resolve_format_mappings,
    run_formatting_align,
)
from pact_full_pipeline_runner_v1.v4_book_html import render_chapter_body

IDENTITY = "abcd" * 8


class _Gen:
    def __init__(self, content):
        self.content = content


class _MockClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def complete(self, messages, cfg, max_tokens, label=None):
        self.calls += 1
        content = self.responses.pop(0) if self.responses else '{"mappings":[]}'
        return _Gen(content)


def test_resolve_and_wrap_russian_em():
    html = "<html><body><p>Hello <em>world</em> and <em>again</em>.</p></body></html>"
    blocks = parse_source_html(html)
    translations = {blocks[0].pid: "Привет мир и снова мир."}
    resp = json.dumps({"mappings": [
        {"pid": blocks[0].pid, "span_id": "em01", "target_text": "мир", "occurrence": 1},
        {"pid": blocks[0].pid, "span_id": "em02", "target_text": "мир", "occurrence": 2},
    ]})
    client = _MockClient([resp])
    mappings = resolve_format_mappings(client, {}, blocks, translations)
    assert len(mappings) == 2
    outcome = run_formatting_align(blocks=blocks, translation=translations, backend_identity_hash=IDENTITY, max_formatting_incidents=999, mappings=mappings)
    assert outcome.resolved_count == 2
    assert "<em>мир</em>" in dict(outcome.formatted_text)[blocks[0].pid]
    assert outcome.to_payload()["resolved_count"] > 0


def test_inaccurate_target_is_debt_not_blocking():
    html = "<html><body><p>A <em>world</em> here.</p></body></html>"
    blocks = parse_source_html(html)
    translations = {blocks[0].pid: "Привет."}
    resp = json.dumps({"mappings": [{"pid": blocks[0].pid, "span_id": "em01", "target_text": "не_найдено", "occurrence": 1}]})
    client = _MockClient([resp])
    mappings = resolve_format_mappings(client, {}, blocks, translations)
    outcome = run_formatting_align(blocks=blocks, translation=translations, backend_identity_hash=IDENTITY, max_formatting_incidents=999, mappings=mappings)
    assert outcome.incident_count == 1
    assert not outcome.blocking


def test_no_inline_spans_no_model_call():
    html = "<html><body><p>Plain without em.</p></body></html>"
    blocks = parse_source_html(html)
    translations = {blocks[0].pid: "Просто текст."}
    client = _MockClient(['{"mappings":[]}'])
    mappings = resolve_format_mappings(client, {}, blocks, translations)
    assert client.calls == 0
    assert mappings == {}


def test_legacy_path_preserves_strict_runner():
    html = "<html><body><p>In <em>1947</em> we met.</p></body></html>"
    blocks = parse_source_html(html)
    translations = {blocks[0].pid: "В 1947 году мы встретились."}
    outcome = run_formatting_align(blocks=blocks, translation=translations, backend_identity_hash=IDENTITY, max_formatting_incidents=0, mappings=None)
    assert dict(outcome.formatted_text)[blocks[0].pid] == "В <em>1947</em> году мы встретились."


def test_render_with_em_via_book_html():
    html = "<html><body><p>Hello <em>world</em>.</p></body></html>"
    blocks = parse_source_html(html)
    translations = {blocks[0].pid: "Привет мир."}
    resp = json.dumps({"mappings": [{"pid": blocks[0].pid, "span_id": "em01", "target_text": "мир", "occurrence": 1}]})
    client = _MockClient([resp])
    mappings = resolve_format_mappings(client, {}, blocks, translations)
    outcome = run_formatting_align(blocks=blocks, translation=translations, backend_identity_hash=IDENTITY, max_formatting_incidents=999, mappings=mappings)
    body, _ = render_chapter_body(html, dict(outcome.formatted_text), chapter_id="0001")
    assert "<em>мир</em>" in body
    # chapter without em unchanged
    html2 = "<html><body><p>Plain.</p></body></html>"
    blocks2 = parse_source_html(html2)
    trans2 = {blocks2[0].pid: "Обычный."}
    out2 = run_formatting_align(blocks=blocks2, translation=trans2, backend_identity_hash=IDENTITY, mappings=None)
    body2, _ = render_chapter_body(html2, dict(out2.formatted_text), chapter_id="0002")
    assert "<em>" not in body2
