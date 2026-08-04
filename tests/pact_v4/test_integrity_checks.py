"""Unit tests for the V4 B5 mixed_script-allowlist builders.

The builders live in ``pact_v4._integrity_checks`` (the shared model-free
integrity surface) and produce exactly the token shape
``find_mixed_script`` matches against. These tests pin the four sources of
the combined allowlist — book bible, glossary, source-derived, manual
config — and the tokenization contract that makes an entry like ``"R.D.T."``
unblock the tokens ``R``/``D``/``T``.
"""
from __future__ import annotations

from pact_v4._integrity_checks import (
    bible_script_tokens,
    combine_script_tokens,
    extract_script_tokens,
    find_mixed_script,
    glossary_script_tokens,
    source_derived_allowlist,
)


def test_extract_script_tokens_latin_initials():
    # "R.D.T." is tokenized the same way find_mixed_script tokenizes a
    # translation paragraph: R, D, T are separate tokens (the dot is a
    # separator). The allowlist must therefore carry those tokens, not the
    # whole dotted string.
    assert extract_script_tokens("R.D.T. said hello.") == (
        "R", "D", "T", "said", "hello",
    )


def test_extract_script_tokens_skips_pure_cyrillic_and_dedups():
    assert extract_script_tokens("GPS GPS работает.") == ("GPS",)


def test_extract_script_tokens_strips_urls_and_emails():
    tokens = extract_script_tokens("visit https://example.com now")
    assert "example" not in tokens
    tokens = extract_script_tokens("mail user@example.com please")
    assert "user" not in tokens


def test_bible_script_tokens_characters_and_entities():
    bible = {
        "characters": {
            "R.D.T.": {"target": "Р.Д.Т.", "gender": "male"},
            "Dr.": {"target": "доктор"},
        },
        "entities": {"GPS": {"target": "GPS"}},
        "address_register": [{"source": "Mr. Thorburn"}],
        "facts": [{"source": "The house at Fray"}],
        "chapters": ["0046_subordination-6-3.html"],
    }
    tokens = set(bible_script_tokens(bible))
    assert {"R", "D", "T", "Dr", "GPS", "Mr", "Thorburn", "Fray"} <= tokens


def test_bible_script_tokens_ignores_cyrillic_only_terms():
    assert bible_script_tokens({"characters": {"Мэри": {}}, "entities": {}}) == ()


def test_bible_script_tokens_tolerates_missing_sections():
    assert bible_script_tokens({}) == ()
    assert bible_script_tokens({"characters": {}, "entities": {}}) == ()
    assert bible_script_tokens(None) == ()


def test_glossary_script_tokens_dict_and_list_shapes():
    as_dict = {"R.D.T.": "Р.Д.Т.", "GPS": ["GPS", "Джи-Пи-Эс"]}
    tokens = set(glossary_script_tokens(as_dict))
    assert {"R", "D", "T", "GPS"} <= tokens
    as_list = [
        {"source_term": "Blake", "target_terms": ["Блэйк"]},
        {"source": "Mr.", "target": "мистер"},
    ]
    tokens_list = set(glossary_script_tokens(as_list))
    assert {"Blake", "Mr"} <= tokens_list


def test_glossary_script_tokens_ignores_cyrillic_only():
    assert glossary_script_tokens({"стюард": "стюард"}) == ()


def test_source_derived_allowlist_intersection():
    source = "R.D.T. stood at the window."
    # Token present in BOTH source and translation -> legitimate.
    assert set(source_derived_allowlist(source, "R.D.T. стоял у окна.")) == {
        "R", "D", "T",
    }
    # Latin token in the translation that never appears in the source -> not
    # allowlisted (still caught by find_mixed_script).
    assert source_derived_allowlist(source, "A.B.V. стоял у окна.") == ()


def test_combine_script_tokens_dedup_preserves_order():
    assert combine_script_tokens(("R", "r"), ("D",), ("r", "T")) == ("R", "D", "T")


def test_find_mixed_script_with_combined_allowlist():
    allow = combine_script_tokens(
        bible_script_tokens({"characters": {"R.D.T.": {}}}),
        source_derived_allowlist("R.D.T. spoke.", "R.D.T. сказал."),
    )
    assert find_mixed_script("Он увидел R.D.T. в дверях.", allow) == []
    assert find_mixed_script("Он увидел A.B.V. в дверях.", allow) == ["A", "B", "V"]


def test_manual_config_tokenization_matches_bible_semantics():
    # A manual StrictRunConfig entry like "R.D.T." must unblock the same
    # tokens a bible entry does: the runner tokenizes manual entries with
    # extract_script_tokens before combining.
    manual = extract_script_tokens("R.D.T.")
    assert manual == ("R", "D", "T")
    assert find_mixed_script("R.D.T. подписал.", manual) == []
    assert find_mixed_script("A.B.V. подписал.", manual) == ["A", "B", "V"]
