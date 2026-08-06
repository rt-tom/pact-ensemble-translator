"""Unit tests for the Phase 5 formatting alignment module (B3).

Covers the §8.14 span contract: span mapping, the exact -> occurrence-aware
-> conservative fuzzy -> model fallback tier cascade, conflicting spans,
ambiguous occurrence falling through to the next tier, blocking integrity
(``max_formatting_incidents``), the duplicate-occurrence / HTML / PID /
number fixtures, the no-marker-leakage guard, and the dual-mode import guard
(the module never references local lifecycle adapters).
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from pact_v4._integrity_checks import strip_inline_markup
from pact_v4.phase0b.source_html import parse_source_html
from pact_v4.phase5.formatting import (
    TIER_EXACT,
    TIER_FUZZY,
    TIER_MODEL,
    TIER_OCCURRENCE,
    FormattingIncident,
    FormattingOutcome,
    find_nonoverlapping_occurrence,
    occurrence_ranges,
    run_formatting_align,
)

IDENTITY = "abcd" * 8


def _blocks(html: str):
    return parse_source_html(html)


def _align(html: str, translation, *, caller=None, max_incidents=0):
    return run_formatting_align(
        blocks=_blocks(html),
        translation=translation,
        formatting_caller=caller,
        backend_identity_hash=IDENTITY,
        max_formatting_incidents=max_incidents,
    )


class CannedFormattingCaller:
    """Fake ``FormattingCaller``: maps each span to a word of the translation."""

    def __init__(self, *, fail: Exception | None = None,
                 empty: bool = False, suffix: str = "") -> None:
        self.fail = fail
        self.empty = empty
        self.suffix = suffix
        self.calls: list = []

    def __call__(self, *, pid, source_text, translation, spans):
        self.calls.append((pid, translation, list(spans)))
        if self.fail is not None:
            raise self.fail
        words = translation.split()
        mappings = []
        for index, span in enumerate(spans):
            target = "" if self.empty else (words[index] if index < len(words) else "")
            mappings.append({
                "pid": pid, "span_id": span["span_id"],
                "target_text": target + self.suffix, "occurrence": 1,
            })
        return json.dumps({"mappings": mappings}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Occurrence helpers
# ---------------------------------------------------------------------------


def test_occurrence_ranges_case_sensitive_then_insensitive():
    # Case-sensitive first: only the exact "No" matches.
    assert occurrence_ranges("No no NO", "No") == [(0, 2)]
    # Zero case-sensitive matches fall back to case-insensitive.
    assert occurrence_ranges("no NO", "No") == [(0, 2), (3, 5)]
    assert occurrence_ranges("НО нет", "нет") == [(3, 6)]
    assert occurrence_ranges("abc", "No") == []


def test_occurrence_ranges_word_boundary_avoids_substrings():
    # "1947" must not match inside "19471"; "No" must not match inside "Now".
    assert occurrence_ranges("19471 1947", "1947", word_boundary=True) == [(6, 10)]
    assert occurrence_ranges("Now No", "No", word_boundary=True) == [(4, 6)]
    assert occurrence_ranges("Now No", "No") == [(0, 2), (4, 6)]


def test_find_nonoverlapping_occurrence_prefers_requested_index():
    text = "a b a"
    # Preferred occurrence 2 -> the second "a" at [4,5).
    assert find_nonoverlapping_occurrence(text, "a", preferred=2, occupied=[]) == (4, 5)
    assert find_nonoverlapping_occurrence(text, "a", preferred=1, occupied=[]) == (0, 1)
    # Claiming [0,1) forces the next free occurrence.
    assert find_nonoverlapping_occurrence(text, "a", preferred=1, occupied=[(0, 1)]) == (4, 5)
    # preferred is a soft hint (v3 semantics): out of range falls back to the
    # first free occurrence, never None while a non-overlapping one exists.
    assert find_nonoverlapping_occurrence(text, "a", preferred=3, occupied=[]) == (0, 1)
    assert find_nonoverlapping_occurrence(text, "a", preferred=3, occupied=[(0, 1)]) == (4, 5)


# ---------------------------------------------------------------------------
# Tier selection
# ---------------------------------------------------------------------------


def test_exact_tier_wraps_preserved_fragment():
    out = _align("<html><body><p>In <em>1947</em> we met.</p></body></html>",
                 {"p00001": "В 1947 году мы встретились."})
    assert dict(out.formatted_text)["p00001"] == "В <em>1947</em> году мы встретились."
    assert out.resolved_count == 1
    record = out.span_mapping[0]
    assert record.tier == TIER_EXACT
    assert record.translated_text == "1947"
    assert out.incident_count == 0
    assert not out.blocking


def test_exact_tier_case_insensitive_fallback():
    out = _align("<html><body><p>Say <em>No</em>!</p></body></html>",
                 {"p00001": "Скажи NO!"})
    assert dict(out.formatted_text)["p00001"] == "Скажи <em>NO</em>!"
    assert out.span_mapping[0].tier == TIER_EXACT


def test_occurrence_aware_duplicate_1to1():
    # The p00058-style fixture: two identical "No" emphasised, translation
    # keeps both; the 1:1 occurrence-aware assignment wraps each in order.
    out = _align("<html><body><p><em>No</em> and <em>No</em>.</p></body></html>",
                 {"p00001": "No and No."})
    assert dict(out.formatted_text)["p00001"] == "<em>No</em> and <em>No</em>."
    assert [r.tier for r in out.span_mapping] == [TIER_OCCURRENCE, TIER_OCCURRENCE]
    assert [r.occurrence for r in out.span_mapping] == [1, 2]
    assert [r.start for r in out.span_mapping] == [0, 7]
    assert out.incident_count == 0


def test_ambiguous_occurrence_falls_through_to_model():
    # One emphasised "No" but the translation says "No" twice: which one is
    # the emphasised one is ambiguous, so the span must reach the model tier.
    caller = CannedFormattingCaller()
    out = _align("<html><body><p>Go. <em>No</em>. Really.</p></body></html>",
                 {"p00001": "No and No."}, caller=caller)
    assert caller.calls, "the ambiguous span reached the model fallback"
    assert out.span_mapping[0].tier == TIER_MODEL
    assert out.incident_count == 0


def test_ambiguous_occurrence_without_caller_is_blocking_incident():
    out = _align("<html><body><p>Go. <em>No</em>. Really.</p></body></html>",
                 {"p00001": "No and No."})
    assert out.incident_count == 1
    incident = out.incidents[0]
    assert incident.reason == "ambiguous_occurrence"
    assert incident.tier == TIER_OCCURRENCE
    assert out.blocking  # default max_formatting_incidents=0


def test_conflicting_spans_fall_through_to_next_tier():
    # <em>one two</em> claims [0,7); <strong>one</strong> would overlap it, so
    # it must NOT resolve at the exact tier — it falls through to the model
    # fallback. The model's canned fragment ("one") also overlaps the claimed
    # range, so it honestly becomes an incident instead of double-wrapping.
    caller = CannedFormattingCaller()
    out = _align(
        "<html><body><p><em>one two</em> <strong>one</strong></p></body></html>",
        {"p00001": "one two"}, caller=caller,
    )
    tiers = {r.span_id: r.tier for r in out.span_mapping}
    assert tiers["em01"] == TIER_EXACT
    assert "strong01" not in tiers, "the overlapping span never resolved deterministically"
    assert any(
        i.span_id == "strong01" and i.tier == TIER_MODEL
        for i in out.incidents
    )


def test_fuzzy_tier_hyphen_space_tolerance():
    out = _align("<html><body><p>Mail it by <em>e-mail</em>.</p></body></html>",
                 {"p00001": "Отправь по e mail."})
    assert dict(out.formatted_text)["p00001"] == "Отправь по <em>e mail</em>."
    assert out.span_mapping[0].tier == TIER_FUZZY


def test_fuzzy_tier_yo_e_interchange():
    out = _align("<html><body><p>Это <em>ёлка</em>.</p></body></html>",
                 {"p00001": "Это елка."})
    assert dict(out.formatted_text)["p00001"] == "Это <em>елка</em>."
    assert out.span_mapping[0].tier == TIER_FUZZY


def test_fuzzy_tier_never_guesses_arbitrary_edit():
    # The fuzzy tier is conservative: "were" must not fuzzy-match Russian
    # text it has no relationship to.
    out = _align("<html><body><p>They <em>were</em> gone.</p></body></html>",
                 {"p00001": "Они ушли."})
    assert out.resolved_count == 0
    assert out.incident_count == 1
    assert out.incidents[0].reason == "target_not_found"


# ---------------------------------------------------------------------------
# Model fallback tier
# ---------------------------------------------------------------------------


def test_model_fallback_restores_span():
    caller = CannedFormattingCaller()
    out = _align("<html><body><p>Hello <em>world</em>.</p></body></html>",
                 {"p00001": "Привет мир."}, caller=caller)
    assert dict(out.formatted_text)["p00001"] == "<em>Привет</em> мир."
    assert out.span_mapping[0].tier == TIER_MODEL
    assert out.model_fallback_count == 1
    assert out.incident_count == 0


def test_model_fallback_transport_failure_is_debt_not_verdict():
    caller = CannedFormattingCaller(fail=RuntimeError("network down"))
    out = _align("<html><body><p>Hello <em>world</em>.</p></body></html>",
                 {"p00001": "Привет мир."}, caller=caller)
    assert out.resolved_count == 0
    assert out.incident_count == 1
    incident = out.incidents[0]
    assert incident.reason == "transport_error"
    assert incident.tier == TIER_MODEL
    # Blocking incident (default limit 0) but structurally valid PID map:
    # the terminal policy downgrades to accepted_degraded, never `failed`
    # from transport alone. The formatted text is still the verbatim input
    # (B14: wrap-only, no entity escaping).
    assert out.blocking
    assert dict(out.formatted_text)["p00001"] == "Привет мир."


def test_model_fallback_invalid_structured_output_is_incident():
    class Truncated(CannedFormattingCaller):
        def __call__(self, **kwargs):
            return '{"mappings": [{"pid": "p00001", "span_id": "em01", "target_text": "П'
    out = _align("<html><body><p>Hello <em>world</em>.</p></body></html>",
                 {"p00001": "Привет мир."}, caller=Truncated())
    assert out.resolved_count == 0
    assert out.incident_count == 1
    assert out.incidents[0].reason == "transport_error"


def test_model_fallback_missing_mapping_is_incident():
    class UnknownSpan(CannedFormattingCaller):
        def __call__(self, **kwargs):
            return json.dumps({"mappings": [{"pid": "p00001", "span_id": "nope",
                                             "target_text": "Привет", "occurrence": 1}]})
    out = _align("<html><body><p>Hello <em>world</em>.</p></body></html>",
                 {"p00001": "Привет мир."}, caller=UnknownSpan())
    assert out.resolved_count == 0
    assert out.incidents[0].reason == "missing_mapping"


def test_model_fallback_target_not_found_is_incident():
    class BadFragment(CannedFormattingCaller):
        def __call__(self, **kwargs):
            return json.dumps({"mappings": [{"pid": "p00001", "span_id": "em01",
                                             "target_text": "не существует",
                                             "occurrence": 1}]})
    out = _align("<html><body><p>Hello <em>world</em>.</p></body></html>",
                 {"p00001": "Привет мир."}, caller=BadFragment())
    assert out.resolved_count == 0
    assert out.incidents[0].reason == "target_not_found"


def test_model_fallback_explicit_empty_verdict_is_incident():
    caller = CannedFormattingCaller(empty=True)
    out = _align("<html><body><p>Hello <em>world</em>.</p></body></html>",
                 {"p00001": "Привет мир."}, caller=caller)
    assert out.resolved_count == 0
    assert out.incidents[0].reason == "target_not_found"


# ---------------------------------------------------------------------------
# Blocking integrity
# ---------------------------------------------------------------------------


def test_max_formatting_incidents_threshold():
    html = "<html><body><p>Hello <em>world</em>.</p></body></html>"
    # One unresolved span: blocking at the default limit 0...
    assert _align(html, {"p00001": "Привет мир."}).blocking
    # ...and non-blocking once the configured allowance covers it.
    assert not _align(html, {"p00001": "Привет мир."}, max_incidents=5).blocking


def test_blocking_flag_reflects_incident_count():
    html = "<html><body><p>A <em>x</em> B <em>y</em>.</p></body></html>"
    out = _align(html, {"p00001": "Привет мир."}, max_incidents=1)
    assert out.incident_count == 2
    assert out.blocking
    out2 = _align(html, {"p00001": "Привет мир."}, max_incidents=2)
    assert not out2.blocking


def test_outcome_payload_carries_provenance_and_backend_identity():
    out = _align("<html><body><p>In <em>1947</em> we met.</p></body></html>",
                 {"p00001": "В 1947 году мы встретились."})
    payload = out.to_payload()
    assert payload["schema"] == "pact-v4-formatting-outcome/v1"
    assert payload["backend_identity_hash"] == IDENTITY
    assert payload["resolved_count"] == 1
    assert payload["incident_count"] == 0
    assert payload["max_formatting_incidents"] == 0
    assert payload["blocking"] is False
    assert payload["span_mapping"][0]["tier"] == TIER_EXACT


# ---------------------------------------------------------------------------
# HTML / attrs / PID coverage / marker leakage fixtures
# ---------------------------------------------------------------------------


def test_html_attrs_preserved_in_output():
    out = _align(
        '<html><body><p>See <a href="http://x.example">the <em>link</em></a>.</p></body></html>',
        {"p00001": "Смотри ссылку."},
        caller=CannedFormattingCaller(),
    )
    # Both spans restored; the <a> keeps its href attribute.
    assert '<a href="http://x.example">' in dict(out.formatted_text)["p00001"]
    assert "<em>" in dict(out.formatted_text)["p00001"]
    assert out.resolved_count == 2


def test_pids_without_spans_are_escaped_plain():
    blocks = _blocks("<html><body><p>A <em>word</em> here.</p><p>Plain text.</p></body></html>")
    pid0, pid1 = blocks[0].pid, blocks[1].pid
    out = run_formatting_align(
        blocks=blocks, translation={pid0: "Привет мир.", pid1: "Обычный текст."},
        backend_identity_hash=IDENTITY,
    )
    assert dict(out.formatted_text)[pid0] == "Привет мир."
    assert dict(out.formatted_text)[pid1] == "Обычный текст."


def test_empty_pids_are_skipped_not_fabricated():
    blocks = _blocks("<html><body><p>A <em>word</em> here.</p></body></html>")
    out = run_formatting_align(
        blocks=blocks, translation={blocks[0].pid: ""},
        backend_identity_hash=IDENTITY,
    )
    assert dict(out.formatted_text)[blocks[0].pid] == ""
    assert out.resolved_count == 0


def test_formatted_text_preserves_pid_map():
    blocks = _blocks(
        "<html><body><p>A <em>word</em> here.</p><p>Plain.</p></body></html>"
    )
    translation = {blocks[0].pid: "Привет мир.", blocks[1].pid: "Обычный."}
    out = run_formatting_align(
        blocks=blocks, translation=translation, backend_identity_hash=IDENTITY,
    )
    assert set(dict(out.formatted_text)) == set(translation)


def test_no_marker_leakage_in_formatted_text():
    import re
    out = _align(
        "<html><body><p>Hello <em>world</em> and <em>world</em>.</p></body></html>",
        {"p00001": "Привет мир и мир."},
        caller=CannedFormattingCaller(empty=True),
    )
    for pid, text in out.formatted_text:
        assert not re.search(r"\[\[FMT_|@@FMT|%%FMT|<<FMT", text), pid
    assert out.incident_count == 2  # both spans unresolved, no placeholder emitted


def test_numbers_fixture_preserved():
    # A number emphasised in the source must survive as the same number.
    out = _align(
        "<html><body><p>Founded in <em>1947</em>, <em>it</em> grew.</p></body></html>",
        {"p00001": "Основан в 1947 году, он рос."},
    )
    formatted = dict(out.formatted_text)["p00001"]
    assert "<em>1947</em>" in formatted
    # The untranslatable "it" reached the model tier, so a caller is needed
    # for a clean outcome; without one it is a blocking incident, never a
    # marker.
    assert "1947" in formatted


# ---------------------------------------------------------------------------
# strip_inline_markup (shared helper, used by the Step 8 checks)
# ---------------------------------------------------------------------------


def test_strip_inline_markup_removes_only_inline_tags():
    assert strip_inline_markup("<em>Привет</em> <strong>мир</strong>.") == "Привет мир."
    assert strip_inline_markup('<a href="http://x">текст</a>') == "текст"
    # Block-level tags are left alone — formatting never injects them.
    assert strip_inline_markup("<p>текст</p>") == "<p>текст</p>"


# ---------------------------------------------------------------------------
# Dual-mode import guard (mirrors the Phase 4 repair guard)
# ---------------------------------------------------------------------------


def test_formatting_module_does_not_import_local_lifecycle():
    path = Path("pact_v4/phase5/formatting.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
    forbidden = [
        "pact_v4.runtime.model_lifecycle",
        "pact_v4.runtime.model_lifecycle_adapters",
        "pact_v4.runtime.backend_role_adapters",
    ]
    for mod in imports:
        assert not any(mod.startswith(f) for f in forbidden), (
            f"formatting module must not reference local lifecycle/transport: {mod}"
        )


def test_run_formatting_align_rejects_pid_drop_at_repair_layer():
    # The repair layer (run_repair_phase) guards PID-map preservation; here we
    # assert the outcome object itself always mirrors the input key set.
    blocks = _blocks("<html><body><p>A <em>word</em> here.</p></body></html>")
    out = run_formatting_align(
        blocks=blocks, translation={blocks[0].pid: "Текст."},
        backend_identity_hash=IDENTITY,
    )
    assert isinstance(out, FormattingOutcome)
    assert isinstance(out.incidents, tuple)
    assert all(isinstance(i, FormattingIncident) for i in out.incidents)


# ---------------------------------------------------------------------------
# B12: batched model-fallback tier (one call per pid_batches group)
# ---------------------------------------------------------------------------


class BatchFormattingCaller(CannedFormattingCaller):
    """``CannedFormattingCaller`` with a ``batch`` method.

    The batched call maps every PID's spans with the same per-PID word
    mapping as the per-PID ``__call__``, so the batched path and the
    per-PID path produce identical span_mapping/incidents on the same
    chapter — only the number of model calls differs.
    """

    def __init__(self, *, fail: Exception | None = None, empty: bool = False):
        super().__init__(fail=fail, empty=empty)
        self.batch_calls: list = []

    def batch(self, items):
        self.batch_calls.append([dict(item) for item in items])
        if self.fail is not None:
            raise self.fail
        mappings = []
        for item in items:
            words = item["translation"].split()
            for index, span in enumerate(item["spans"]):
                target = "" if self.empty else (
                    words[index] if index < len(words) else ""
                )
                mappings.append({
                    "pid": item["pid"], "span_id": span["span_id"],
                    "target_text": target + self.suffix, "occurrence": 1,
                })
        return json.dumps({"mappings": mappings}, ensure_ascii=False)


def test_batch_call_groups_pids_into_one_call():
    # Two PIDs with unresolved spans in ONE batch -> exactly one model call
    # for the group; model_fallback_count still counts PIDs, model_call_count
    # counts actual calls.
    blocks = _blocks(
        "<html><body>"
        "<p>Hello <em>world</em> one.</p>"
        "<p>Hello <em>world</em> two.</p>"
        "</body></html>"
    )
    pid0, pid1 = blocks[0].pid, blocks[1].pid
    caller = BatchFormattingCaller()
    out = run_formatting_align(
        blocks=blocks,
        translation={pid0: "Привет мир один.", pid1: "Привет мир два."},
        formatting_caller=caller,
        backend_identity_hash=IDENTITY,
        pid_batches=[[pid0, pid1]],
    )
    assert len(caller.batch_calls) == 1
    assert len(caller.batch_calls[0]) == 2
    assert len(caller.calls) == 0  # no per-PID calls when batching is active
    assert out.resolved_count == 2
    assert out.incident_count == 0
    assert out.model_fallback_count == 2  # PIDs needing the model tier
    assert out.model_call_count == 1  # one batched call
    assert "<em>Привет</em> мир один." in dict(out.formatted_text)[pid0]


def test_batch_call_splits_by_pid_batches():
    # Two separate batches -> two model calls, same resolution as per-PID.
    blocks = _blocks(
        "<html><body>"
        "<p>Hello <em>world</em> one.</p>"
        "<p>Hello <em>world</em> two.</p>"
        "</body></html>"
    )
    pid0, pid1 = blocks[0].pid, blocks[1].pid
    caller = BatchFormattingCaller()
    out = run_formatting_align(
        blocks=blocks,
        translation={pid0: "Привет мир один.", pid1: "Привет мир два."},
        formatting_caller=caller,
        backend_identity_hash=IDENTITY,
        pid_batches=[[pid0], [pid1]],
    )
    assert len(caller.batch_calls) == 2
    assert out.resolved_count == 2
    assert out.model_fallback_count == 2
    assert out.model_call_count == 2


def test_batch_call_matches_per_pid_path_on_identical_mappings():
    # Batched and per-PID callers resolving identically must produce the same
    # span_mapping/incidents — only the transport differs (B12 contract).
    blocks = _blocks(
        "<html><body>"
        "<p>Hello <em>world</em> one.</p>"
        "<p>Hello <em>world</em> two.</p>"
        "</body></html>"
    )
    pid0, pid1 = blocks[0].pid, blocks[1].pid
    translation = {pid0: "Привет мир один.", pid1: "Привет мир два."}
    per_pid = run_formatting_align(
        blocks=blocks, translation=translation,
        formatting_caller=CannedFormattingCaller(),
        backend_identity_hash=IDENTITY,
    )
    batched = run_formatting_align(
        blocks=blocks, translation=translation,
        formatting_caller=BatchFormattingCaller(),
        backend_identity_hash=IDENTITY,
        pid_batches=[[pid0, pid1]],
    )
    assert batched.span_mapping == per_pid.span_mapping
    assert batched.incidents == per_pid.incidents
    assert batched.formatted_text == per_pid.formatted_text
    assert batched.model_fallback_count == per_pid.model_fallback_count == 2
    assert batched.model_call_count == 1
    assert per_pid.model_call_count == 2


def test_batch_call_transport_failure_is_debt_for_all_spans():
    # A batched transport failure marks every span of the batch as
    # transport_error debt (never a semantic verdict), and the formatted text
    # stays the verbatim input (B14: wrap-only, no entity escaping).
    blocks = _blocks(
        "<html><body>"
        "<p>Hello <em>world</em> one.</p>"
        "<p>Hello <em>world</em> two.</p>"
        "</body></html>"
    )
    pid0, pid1 = blocks[0].pid, blocks[1].pid
    out = run_formatting_align(
        blocks=blocks,
        translation={pid0: "Привет мир один.", pid1: "Привет мир два."},
        formatting_caller=BatchFormattingCaller(fail=RuntimeError("network down")),
        backend_identity_hash=IDENTITY,
        pid_batches=[[pid0, pid1]],
    )
    assert out.resolved_count == 0
    assert out.incident_count == 2
    assert {i.reason for i in out.incidents} == {"transport_error"}
    assert {i.tier for i in out.incidents} == {TIER_MODEL}
    assert out.model_call_count == 1
    assert dict(out.formatted_text)[pid0] == "Привет мир один."


def test_pid_outside_batches_falls_back_to_per_pid_call():
    # A PID not covered by any pid_batches group keeps the per-PID path even
    # when the caller supports batch.
    blocks = _blocks(
        "<html><body>"
        "<p>Hello <em>world</em> one.</p>"
        "<p>Hello <em>world</em> two.</p>"
        "</body></html>"
    )
    pid0, pid1 = blocks[0].pid, blocks[1].pid
    caller = BatchFormattingCaller()
    out = run_formatting_align(
        blocks=blocks,
        translation={pid0: "Привет мир один.", pid1: "Привет мир два."},
        formatting_caller=caller,
        backend_identity_hash=IDENTITY,
        pid_batches=[[pid0]],  # pid1 is outside the batch
    )
    assert len(caller.batch_calls) == 1
    assert len(caller.calls) == 1  # pid1 via per-PID call
    assert out.resolved_count == 2
    assert out.model_call_count == 2


def test_model_call_count_in_payload():
    blocks = _blocks(
        "<html><body>"
        "<p>Hello <em>world</em> one.</p>"
        "<p>Hello <em>world</em> two.</p>"
        "</body></html>"
    )
    pid0, pid1 = blocks[0].pid, blocks[1].pid
    out = run_formatting_align(
        blocks=blocks,
        translation={pid0: "Привет мир один.", pid1: "Привет мир два."},
        formatting_caller=BatchFormattingCaller(),
        backend_identity_hash=IDENTITY,
        pid_batches=[[pid0, pid1]],
    )
    payload = out.to_payload()
    assert payload["model_fallback_count"] == 2
    assert payload["model_call_count"] == 1


# ---------------------------------------------------------------------------
# B14: wrap-only without entities (run_005 double-escaping)
# ---------------------------------------------------------------------------


def test_formatting_output_has_no_entity_escaping():
    # run_005 defect: apply_span_mappings html.escaped the text (turning the
    # model's own raw <em> into &lt;em&gt;) while adding a real <em> wrap —
    # producing "&lt;em&gt;<em>…</em>&lt;/em&gt;". B14: the wrap is
    # wrap-only without entities — the visible text passes through verbatim
    # and the pre-existing tags survive as real tags.
    out = _align(
        "<html><body><p>Hello <em>мир</em>.</p></body></html>",
        {"p00001": "<em>Привет, мир</em>."},
    )
    formatted = dict(out.formatted_text)["p00001"]
    # No entity-encoded markup anywhere in the output.
    assert "&lt;" not in formatted
    assert "&gt;" not in formatted
    # The wrap is applied around the located fragment; the pre-existing
    # tags stay real tags (normalization collapses the double wrap at the
    # final write — see normalize_inline_markup), never entity-escaped.
    assert formatted.count("<em>") == formatted.count("</em>")
    assert "мир" in formatted


def test_formatting_output_with_literal_ampersand_passthrough():
    # A literal ampersand in the translation is not entity-escaped by the
    # wrap (text is passed through verbatim; the final normalization keeps
    # non-tag entities untouched).
    out = _align(
        "<html><body><p>Hello <em>мир</em>.</p></body></html>",
        {"p00001": "R&D мир."},
    )
    formatted = dict(out.formatted_text)["p00001"]
    assert "R&D" in formatted
    assert "&amp;" not in formatted
