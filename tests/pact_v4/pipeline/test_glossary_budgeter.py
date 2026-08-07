"""V4 Efficiency A1.1 — per-chunk glossary budgeter tests.

Covers ``_glossary_entries_for_chunk`` (term-present filter with the
fail-closed ``always_include`` set: narrator_gender name pairs,
glossary_conflict-carrying entries, and entries tied to the chunk's
required risk categories ``number_word``/``tone_profanity``) and
``_narrator_glossary_terms`` (narrator name tokens from book_memory).
"""
from __future__ import annotations

from typing import List, Tuple

from pact_v4.phase2.risk import GlossaryEntry
from pact_v4.pipeline._shared_runner_helpers import (
    _glossary_entries_for_chunk,
    _narrator_glossary_terms,
)

ENTRY = GlossaryEntry


def _run_filter(
    glossary: List[GlossaryEntry],
    *,
    chunk_text: str,
    risk_feature_codes: Tuple[str, ...] = (),
    narrator_gender: str | None = None,
    narrator_source_terms: Tuple[str, ...] = (),
) -> Tuple[List[str], List[str]]:
    kept, dropped = _glossary_entries_for_chunk(
        glossary,
        chunk_text=chunk_text,
        risk_feature_codes=risk_feature_codes,
        narrator_gender=narrator_gender,
        narrator_source_terms=narrator_source_terms,
    )
    return [e.source_term for e in kept], list(dropped)


# ---------------------------------------------------------------------------
# Term-present filter
# ---------------------------------------------------------------------------


def test_present_term_is_kept() -> None:
    kept, dropped = _run_filter(
        [ENTRY("steward", ("стюард",))], chunk_text="The steward entered."
    )
    assert kept == ["steward"]
    assert dropped == []


def test_absent_term_is_dropped_and_reported() -> None:
    kept, dropped = _run_filter(
        [ENTRY("steward", ("стюард",))], chunk_text="The butler entered."
    )
    assert kept == []
    assert dropped == ["steward"]


def test_match_is_case_insensitive() -> None:
    kept, _ = _run_filter(
        [ENTRY("Blake", ("Блэйк",))], chunk_text="blake walked in."
    )
    assert kept == ["Blake"]


def test_match_respects_word_boundaries() -> None:
    # "steward" must not match inside "stewardship" — same (?<!\w)...(?!\w)
    # boundary contract as the risk pre-screen's _term_present.
    kept, dropped = _run_filter(
        [ENTRY("steward", ("стюард",))], chunk_text="His stewardship was praised."
    )
    assert kept == []
    assert dropped == ["steward"]


def test_multi_word_terms_are_supported() -> None:
    kept, _ = _run_filter(
        [ENTRY("Blake Thorburn", ("Блэйк Торбёрн",))],
        chunk_text="Blake Thorburn lit a candle.",
    )
    assert kept == ["Blake Thorburn"]


def test_dropped_uses_stripped_source_term() -> None:
    kept, dropped = _run_filter(
        [ENTRY("  steward  ", ("стюард",))], chunk_text="The butler entered."
    )
    assert kept == []
    assert dropped == ["steward"]


# ---------------------------------------------------------------------------
# always_include fail-closed
# ---------------------------------------------------------------------------


def test_narrator_name_pair_is_kept_even_when_absent() -> None:
    # The narrator's name is a locked chapter-level constraint: never cut
    # from the prompt, even in a chunk that does not mention the narrator.
    kept, dropped = _run_filter(
        [ENTRY("Blake", ("Блэйк",))],
        chunk_text="A stranger entered.",
        narrator_gender="male",
        narrator_source_terms=("Blake", "Thorburn"),
    )
    assert kept == ["Blake"]
    assert dropped == []


def test_narrator_rule_is_inert_without_gender() -> None:
    # No pinned narrator gender -> the name pair is not locked (but the
    # term-present rule still applies; here it is absent so it drops).
    kept, dropped = _run_filter(
        [ENTRY("Blake", ("Блэйк",))],
        chunk_text="A stranger entered.",
        narrator_gender=None,
        narrator_source_terms=("Blake",),
    )
    assert kept == []
    assert dropped == ["Blake"]


def test_narrator_rule_is_inert_without_name_terms() -> None:
    kept, dropped = _run_filter(
        [ENTRY("Blake", ("Блэйк",))],
        chunk_text="A stranger entered.",
        narrator_gender="male",
        narrator_source_terms=(),
    )
    assert kept == []
    assert dropped == ["Blake"]


def test_multiword_narrator_name_pair_is_locked() -> None:
    # "Blake Thorburn" is the narrator's full name: the multi-word entry is
    # locked too (every token is a narrator token), even when absent.
    kept, dropped = _run_filter(
        [ENTRY("Blake Thorburn", ("Блэйк Торбёрн",))],
        chunk_text="A stranger entered.",
        narrator_gender="male",
        narrator_source_terms=("Blake", "Thorburn"),
    )
    assert kept == ["Blake Thorburn"]
    assert dropped == []


def test_multiword_entry_with_foreign_token_is_not_locked() -> None:
    # "Thorburn Estate" shares the narrator's surname but is a place, not
    # the narrator: not locked, dropped when absent.
    kept, dropped = _run_filter(
        [ENTRY("Thorburn Estate", ("Поместье Торбёрн",))],
        chunk_text="A stranger entered.",
        narrator_gender="male",
        narrator_source_terms=("Blake", "Thorburn"),
    )
    assert kept == []
    assert dropped == ["Thorburn Estate"]


def test_glossary_conflict_entry_is_kept_even_when_absent() -> None:
    # A source term with >1 distinct prescribed target forms carries a
    # glossary_conflict; dropping half of it would silently hide the
    # conflict, so it is always_include.
    kept, dropped = _run_filter(
        [ENTRY("steward", ("стюард", "дворецкий"))],
        chunk_text="The butler entered.",
    )
    assert kept == ["steward"]
    assert dropped == []


def test_single_target_entry_is_not_a_conflict() -> None:
    kept, dropped = _run_filter(
        [ENTRY("steward", ("стюард",))],
        chunk_text="The butler entered.",
    )
    assert kept == []
    assert dropped == ["steward"]


def test_conflict_aggregates_across_duplicate_source_entries() -> None:
    # Two entries with the same source term but different targets are a
    # conflict across the pair (same casefolded aggregation as
    # risk._glossary_conflicts); both halves stay locked.
    kept, dropped = _run_filter(
        [ENTRY("Blake", ("Блэйк",)), ENTRY("Blake", ("Блейк",))],
        chunk_text="A stranger entered.",
    )
    assert kept == ["Blake", "Blake"]
    assert dropped == []


def test_number_word_entry_kept_when_chunk_flagged_number_word() -> None:
    kept, dropped = _run_filter(
        [ENTRY("hundred", ("сотня",))],
        chunk_text="He counted the coins.",
        risk_feature_codes=("number_word",),
    )
    assert kept == ["hundred"]
    assert dropped == []


def test_profanity_entry_kept_when_chunk_flagged_tone_profanity() -> None:
    kept, dropped = _run_filter(
        [ENTRY("fuck", ("ёб",))],
        chunk_text="He frowned at the letter.",
        risk_feature_codes=("tone_profanity",),
    )
    assert kept == ["fuck"]
    assert dropped == []


def test_required_category_entry_dropped_when_chunk_not_flagged() -> None:
    kept, dropped = _run_filter(
        [ENTRY("hundred", ("сотня",)), ENTRY("fuck", ("ёб",))],
        chunk_text="He counted the coins.",
        risk_feature_codes=(),
    )
    assert kept == []
    assert dropped == ["hundred", "fuck"]


def test_present_term_wins_regardless_of_category_flags() -> None:
    # Present beats the budget: a present term is kept even when no
    # required category is flagged for the chunk.
    kept, dropped = _run_filter(
        [ENTRY("hundred", ("сотня",))],
        chunk_text="A hundred candles lit the hall.",
        risk_feature_codes=(),
    )
    assert kept == ["hundred"]
    assert dropped == []


# ---------------------------------------------------------------------------
# _narrator_glossary_terms
# ---------------------------------------------------------------------------


def test_narrator_terms_from_pov_source_name() -> None:
    assert _narrator_glossary_terms(
        {"pov": {"gender": "male", "source_name": "Blake Thorburn"}}
    ) == ("Blake", "Thorburn")


def test_narrator_terms_legacy_keys() -> None:
    assert _narrator_glossary_terms({"narrator_source_name": "Blake"}) == ("Blake",)
    assert _narrator_glossary_terms({"narrator_name": "Blake"}) == ("Blake",)


def test_narrator_terms_absent_returns_empty() -> None:
    assert _narrator_glossary_terms({}) == ()
    assert _narrator_glossary_terms({"pov": {"gender": "male"}}) == ()
    assert _narrator_glossary_terms(None) == ()


def test_narrator_terms_dedupes_tokens() -> None:
    assert _narrator_glossary_terms(
        {"pov": {"source_name": "Blake Blake"}}
    ) == ("Blake",)
