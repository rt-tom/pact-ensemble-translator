"""Unit tests for the Phase 5 formatting alignment module (B3, card C).

Covers the §8.14 span contract: span mapping, the preserved (already-present
markup) -> exact -> occurrence-aware -> conservative fuzzy deterministic
tier cascade, conflicting spans, ambiguous occurrence falling through to a
blocking incident, blocking integrity (``max_formatting_incidents``), the
duplicate-occurrence / HTML / PID / number fixtures, the no-marker-leakage
guard, the model-free invariant (formatting = 0 model calls), and the
dual-mode import guard (the module never references local lifecycle
adapters or any transport).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from pact_v4._integrity_checks import strip_inline_markup
from pact_v4.phase0b.source_html import parse_source_html
from pact_v4.phase5.formatting import (
    TIER_EXACT,
    TIER_FUZZY,
    TIER_OCCURRENCE,
    TIER_PRESERVED,
    FormattingIncident,
    FormattingOutcome,
    find_nonoverlapping_occurrence,
    occurrence_ranges,
    run_formatting_align,
)

IDENTITY = "abcd" * 8


def _blocks(html: str):
    return parse_source_html(html)


def _align(html: str, translation, *, max_incidents=0):
    return run_formatting_align(
        blocks=_blocks(html),
        translation=translation,
        backend_identity_hash=IDENTITY,
        max_formatting_incidents=max_incidents,
    )


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
# Preserved tier (whole-chapter case: the translation already carries the
# inline markup — card C §11 "whole-chapter перевод держит <em> 101/101")
# ---------------------------------------------------------------------------


def test_preserved_tier_resolves_already_wrapped_fragment():
    # The whole-chapter translation keeps the emphasis inline: the span is
    # already wrapped, so it resolves with 0 model calls and no re-wrap.
    out = _align(
        "<html><body><p>In <em>1947</em> we met.</p></body></html>",
        {"p00001": "В <em>1947</em> году мы встретились."},
    )
    assert dict(out.formatted_text)["p00001"] == "В <em>1947</em> году мы встретились."
    assert out.resolved_count == 1
    record = out.span_mapping[0]
    assert record.tier == TIER_PRESERVED
    assert record.preserved is True
    assert record.translated_text == "1947"
    assert out.incident_count == 0
    assert not out.blocking
    assert out.model_call_count == 0
    assert out.model_fallback_count == 0


def test_preserved_tier_does_not_double_wrap():
    # The fragment is already wrapped in the translation — apply_span_mappings
    # must pass it through verbatim, never wrap it a second time.
    out = _align(
        "<html><body><p>Go <em>now</em>.</p></body></html>",
        {"p00001": "Иди <em>сейчас</em>."},
    )
    formatted = dict(out.formatted_text)["p00001"]
    assert formatted.count("<em>") == 1
    assert formatted.count("</em>") == 1
    assert formatted == "Иди <em>сейчас</em>."


def test_preserved_tier_duplicate_spans_1to1_by_order():
    # Two identical <em> spans, both already wrapped in the translation —
    # the 1:1 order-preserving assignment resolves both.
    out = _align(
        "<html><body><p><em>No</em> and <em>No</em>.</p></body></html>",
        {"p00001": "<em>Нет</em> и <em>Нет</em>."},
    )
    assert dict(out.formatted_text)["p00001"] == "<em>Нет</em> и <em>Нет</em>."
    assert [r.tier for r in out.span_mapping] == [TIER_PRESERVED, TIER_PRESERVED]
    assert [r.translated_text for r in out.span_mapping] == ["Нет", "Нет"]
    assert out.incident_count == 0


def test_preserved_tier_strong_tag():
    # <strong> is part of the inline tag set and is recognized the same way.
    out = _align(
        "<html><body><p>It is <strong>important</strong>.</p></body></html>",
        {"p00001": "Это <strong>важно</strong>."},
    )
    assert dict(out.formatted_text)["p00001"] == "Это <strong>важно</strong>."
    assert out.span_mapping[0].tier == TIER_PRESERVED
    assert out.incident_count == 0


def test_preserved_tier_mixed_tags_sequence_match():
    # em + strong in the same order as the source -> both preserved.
    out = _align(
        "<html><body><p><em>one</em> and <strong>two</strong>.</p></body></html>",
        {"p00001": "<em>раз</em> и <strong>два</strong>."},
    )
    assert [r.tier for r in out.span_mapping] == [TIER_PRESERVED, TIER_PRESERVED]
    assert out.incident_count == 0


def test_preserved_tier_count_mismatch_falls_through_to_incident():
    # The translation has an EXTRA emphasis the source does not: the count
    # differs, so order-based preservation cannot guess which existing tag
    # corresponds to the source span — the span falls through to the text
    # tiers and (with no verbatim fragment) becomes a blocking incident
    # (debt), never a guess.
    out = _align(
        "<html><body><p>She was <em>fair</em>, Peter.</p></body></html>",
        {"p00001": "Она была <em>честна</em> с <em>нами</em>, Питер."},
    )
    assert out.resolved_count == 0
    assert out.incident_count == 1
    assert out.incidents[0].span_id == "em01"
    assert out.blocking
    # The translation's own markup is untouched (wrap-only: nothing claimed).
    assert dict(out.formatted_text)["p00001"] == "Она была <em>честна</em> с <em>нами</em>, Питер."


def test_preserved_tier_missing_tag_is_incident_not_silent():
    # The translation DROPPED the emphasis entirely: no preserved claim, no
    # verbatim fragment -> blocking incident (debt), never a silent loss.
    out = _align(
        "<html><body><p>You <em>rancid</em> cunt.</p></body></html>",
        {"p00001": "Ты протухшая сука."},
    )
    assert out.resolved_count == 0
    assert out.incident_count == 1
    assert out.incidents[0].reason == "target_not_found"
    assert out.blocking


def test_preserved_tier_attrs_kept_in_output():
    # The preserved tier passes the already-wrapped fragment through; the
    # existing <a href> attribute survives untouched.
    out = _align(
        '<html><body><p>See <a href="http://x.example">the link</a>.</p></body></html>',
        {"p00001": 'Смотри <a href="http://x.example">ссылку</a>.'},
    )
    formatted = dict(out.formatted_text)["p00001"]
    assert '<a href="http://x.example">' in formatted
    assert out.span_mapping[0].tier == TIER_PRESERVED
    assert out.incident_count == 0


def test_preserved_tier_unbalanced_translation_tag_not_claimed():
    # A broken/unbalanced tag in the translation must not claim the span —
    # it falls through to the text tiers and becomes a blocking incident.
    out = _align(
        "<html><body><p>Hello <em>world</em>.</p></body></html>",
        {"p00001": "Привет <em>мир."},  # no closing </em>
    )
    assert out.resolved_count == 0
    assert out.incident_count == 1


def test_preserved_count_mismatch_exact_text_is_debt_no_double_wrap():
    # Reviewer finding 1: the translation holds an EXTRA inline tag while the
    # source span text survives verbatim inside the existing markup. The
    # preserved tier sees a count mismatch; the span must NOT fall through to
    # the exact tier (which would claim the verbatim fragment inside the
    # existing markup and add a second wrap with incident_count=0). It is
    # blocking debt, and the translation's own markup stays untouched.
    out = _align(
        "<html><body><p><em>world</em></p></body></html>",
        {"p00001": "<em>world</em> <em>x</em>"},
    )
    assert out.resolved_count == 0
    assert out.incident_count == 1
    incident = out.incidents[0]
    assert incident.reason == "preserved_tag_mismatch"
    assert incident.tier == TIER_PRESERVED
    assert incident.required
    assert out.blocking
    formatted = dict(out.formatted_text)["p00001"]
    assert formatted == "<em>world</em> <em>x</em>", (
        "no double wrap: the translation's existing markup is never claimed "
        f"or re-wrapped, got {formatted!r}"
    )
    assert "<em><em>" not in formatted
    assert out.model_call_count == 0
    assert out.model_fallback_count == 0


def test_preserved_order_mismatch_exact_text_is_debt_no_double_wrap():
    # Reviewer finding 1 (order mismatch): same tag count but the translation
    # REORDERED the emphasis (strong before em). The preserved tier's
    # order-based 1:1 cannot apply; the spans must NOT fall through to exact
    # (which would double-wrap the verbatim fragments) — blocking debt, no
    # re-wrap.
    out = _align(
        "<html><body><p><em>world</em> <strong>good</strong></p></body></html>",
        {"p00001": "<strong>world</strong> <em>good</em>"},
    )
    assert out.resolved_count == 0
    assert out.incident_count == 2
    assert {i.reason for i in out.incidents} == {"preserved_tag_mismatch"}
    assert all(i.tier == TIER_PRESERVED for i in out.incidents)
    assert out.blocking
    formatted = dict(out.formatted_text)["p00001"]
    assert formatted == "<strong>world</strong> <em>good</em>", (
        "no double wrap on order mismatch, got {formatted!r}"
    )
    assert "<strong><em>" not in formatted
    assert "<em><strong>" not in formatted
    assert out.model_call_count == 0


def test_preserved_unbalanced_tag_amid_matching_sequence_is_debt():
    # Unbalanced edge: one tag is balanced and matches the source, but the
    # translation also carries an unbalanced tag. The unbalanced tag must not
    # be claimed and must not silently drop out of the sequence comparison —
    # the count still mismatches (source 2 spans vs 1 balanced + 1 broken
    # tag), so both spans are blocking debt with no claim.
    out = _align(
        "<html><body><p><em>world</em> <strong>good</strong></p></body></html>",
        {"p00001": "<em>мир</em> <strong>важно"},  # second tag unbalanced
    )
    assert out.resolved_count == 0
    assert out.incident_count == 2
    assert {i.reason for i in out.incidents} == {"preserved_tag_mismatch"}
    assert out.blocking
    formatted = dict(out.formatted_text)["p00001"]
    assert formatted == "<em>мир</em> <strong>важно", (
        "translation's own markup untouched, got {formatted!r}"
    )
    assert out.model_call_count == 0


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


def test_ambiguous_occurrence_is_blocking_incident():
    # One emphasised "No" but the translation says "No" twice: which one is
    # the emphasised one is ambiguous, so the span is a blocking incident
    # (debt) — never a guess (card C: there is no model tier to consult).
    out = _align("<html><body><p>Go. <em>No</em>. Really.</p></body></html>",
                 {"p00001": "No and No."})
    assert out.incident_count == 1
    incident = out.incidents[0]
    assert incident.reason == "ambiguous_occurrence"
    assert incident.tier == TIER_OCCURRENCE
    assert out.blocking  # default max_formatting_incidents=0


def test_conflicting_spans_fall_through_to_incident():
    # <em>one two</em> claims [0,7); <strong>one</strong> would overlap it, so
    # it must NOT resolve at the exact tier — with no model tier, it becomes
    # a blocking incident instead of double-wrapping.
    out = _align(
        "<html><body><p><em>one two</em> <strong>one</strong></p></body></html>",
        {"p00001": "one two"},
    )
    tiers = {r.span_id: r.tier for r in out.span_mapping}
    assert tiers["em01"] == TIER_EXACT
    assert "strong01" not in tiers, "the overlapping span never resolved deterministically"
    assert any(i.span_id == "strong01" for i in out.incidents)


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
# Model-free invariant (card C: formatting = 0 model calls)
# ---------------------------------------------------------------------------


def test_outcome_carries_zero_model_counts():
    out = _align("<html><body><p>In <em>1947</em> we met.</p></body></html>",
                 {"p00001": "В 1947 году мы встретились."})
    assert out.model_fallback_count == 0
    assert out.model_call_count == 0
    payload = out.to_payload()
    assert payload["model_fallback_count"] == 0
    assert payload["model_call_count"] == 0


def test_no_formatting_caller_symbol_exists():
    # Card C removed the model-fallback path: the module must not expose a
    # FormattingCaller protocol or a model tier anymore.
    import pact_v4.phase5.formatting as fmt
    assert not hasattr(fmt, "FormattingCaller")
    assert not hasattr(fmt, "TIER_MODEL")
    assert "formatting_caller" not in dir(fmt)
    # run_formatting_align must not accept a caller parameter.
    import inspect
    params = inspect.signature(fmt.run_formatting_align).parameters
    assert "formatting_caller" not in params
    assert "pid_batches" not in params


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
        '<html><body><p>See <a href="http://x.example">the link</a> and <em>it</em>.</p></body></html>',
        {"p00001": "See the link and it."},
    )
    # Both spans restore; the <a> keeps its href attribute.
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
    out = _align(
        "<html><body><p>Hello <em>world</em> and <em>world</em>.</p></body></html>",
        {"p00001": "Привет мир и мир."},
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
    # The untranslatable "it" has no deterministic fragment -> a blocking
    # incident (debt), never a marker.
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


def test_formatting_module_does_not_import_local_lifecycle_or_transport():
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
        "pact_v4.runtime.prompts_runtime",
        "pact_v4.runtime.opencode_backend",
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
# Whole-chapter acceptance (card C §11): a translation that already holds the
# emphasis resolves with 0 unresolved spans and 0 model calls
# ---------------------------------------------------------------------------


def test_whole_chapter_translation_resolves_all_spans_model_free():
    # Acceptance (V4_1_AUDIT_B1_RU.md §11): formatting on chapter 0001 with
    # the whole-chapter translation (which holds <em> inline) -> 0 unresolved
    # spans, 0 model calls. The preserved tier resolves every already-wrapped
    # span; no span needs a model.
    html = (
        "<html><body>"
        "<p>My lingering <em>impressions</em> were banished.</p>"
        "<p>He said <em>no</em>, firmly.</p>"
        "<p>It is <strong>very</strong> cold.</p>"
        "<p>Plain paragraph without markup.</p>"
        "</body></html>"
    )
    blocks = _blocks(html)
    translation = {
        blocks[0].pid: "Мои затянувшиеся <em>впечатления</em> развеялись.",
        blocks[1].pid: "Он твёрдо сказал <em>нет</em>.",
        blocks[2].pid: "Очень <strong>холодно</strong>.",
        blocks[3].pid: "Обычный абзац без разметки.",
    }
    out = run_formatting_align(
        blocks=blocks, translation=translation,
        backend_identity_hash=IDENTITY,
    )
    assert out.resolved_count == 3
    assert out.incident_count == 0
    assert not out.blocking
    assert out.model_call_count == 0
    assert out.model_fallback_count == 0
    # The restored text is exactly the already-marked translation (no
    # double-wrap, no re-location drift).
    for pid, expected in translation.items():
        assert dict(out.formatted_text)[pid] == expected


def test_whole_chapter_translation_with_debt_reports_incidents():
    # A whole-chapter translation that DROPPED one emphasis cannot be
    # restored without guessing: the span becomes a blocking incident (debt),
    # never a silent loss — "0 model calls" alone is not success.
    blocks = _blocks(
        "<html><body><p>You <em>rancid</em> cunt.</p>"
        "<p>Go <em>now</em>.</p></body></html>"
    )
    translation = {
        blocks[0].pid: "Ты протухшая сука.",  # emphasis dropped
        blocks[1].pid: "Иди <em>сейчас</em>.",
    }
    out = run_formatting_align(
        blocks=blocks, translation=translation,
        backend_identity_hash=IDENTITY,
    )
    assert out.resolved_count == 1          # <em>сейчас</em> preserved
    assert out.incident_count == 1          # dropped emphasis -> debt
    assert out.blocking
    assert out.model_call_count == 0
    assert out.incidents[0].span_id == "em01"


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
