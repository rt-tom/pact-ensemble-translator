"""Unit tests for the V4 B5 mixed_script-allowlist builders.

The builders live in ``pact_v4._integrity_checks`` (the shared model-free
integrity surface) and produce exactly the token shape
``find_mixed_script`` matches against. These tests pin the four sources of
the combined allowlist — book bible, glossary, source-derived, manual
config — and the tokenization contract that makes an entry like ``"R.D.T."``
unblock the tokens ``R``/``D``/``T``.

B14 also pins the inline-markup normalization helpers: ``strip_inline_markup``
now removes entity-encoded tags (``&lt;em&gt;``) as well as real ones, and
``find_mixed_script`` strips markup before tokenizing so legitimate
formatting never produces mixed-script flags.
"""
from __future__ import annotations

from pact_v4._integrity_checks import (
    bible_script_tokens,
    combine_script_tokens,
    extract_script_tokens,
    find_mixed_script,
    glossary_script_tokens,
    normalize_inline_markup,
    source_derived_allowlist,
    strip_inline_markup,
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


def test_bible_script_tokens_flat_book_memory_shape():
    # V4 book_memory.json is a flat {term: observations} map (V4_MVP_SPEC_RU.md
    # §6 — персонажи/факты/address register/voice notes); the dict keys are
    # the terms.
    book_memory = {
        "R.D.T.": {"target": "Р.Д.Т.", "gender": "male"},
        "Dr.": {"target": "доктор"},
        "GPS": {"target": "GPS"},
    }
    tokens = set(bible_script_tokens(book_memory))
    assert {"R", "D", "T", "Dr", "GPS"} <= tokens


def test_bible_script_tokens_sectioned_v3_shape():
    # The v3 book_bible.json / chapter-bible sectioned shape is also accepted
    # (so the same parser works for the B7 import later).
    bible = {
        "version": 1,
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
    # Sectioned meta keys are not mistaken for terms.
    assert "version" not in {t.casefold() for t in bible_script_tokens(bible)}


def test_bible_script_tokens_ignores_cyrillic_only_terms():
    assert bible_script_tokens({"Мэри": {}}) == ()


def test_bible_script_tokens_tolerates_missing_sections_and_none():
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


# ---------------------------------------------------------------------------
# B14: inline-markup normalization + mixed_script tag exemption
# ---------------------------------------------------------------------------


def test_strip_inline_markup_removes_entity_encoded_tags():
    # B14: strip_inline_markup must remove BOTH normalized tags and (before
    # normalization) the entity-encoded form — the double-escaped output
    # Phase 5's model fallback can produce.
    assert strip_inline_markup("&lt;em&gt;курсив&lt;/em&gt;") == "курсив"
    assert strip_inline_markup("&lt;/em&gt;конец&lt;em&gt;") == "конец"
    assert strip_inline_markup(
        "&lt;a href=&quot;http://x&quot;&gt;ссылка&lt;/a&gt;"
    ) == "ссылка"


def test_normalize_inline_markup_entities_to_clean_tags():
    # Entities of inline tags become clean tags; visible text unchanged.
    assert normalize_inline_markup("&lt;em&gt;курсив&lt;/em&gt;") == "<em>курсив</em>"
    assert normalize_inline_markup("&lt;i&gt;курсив&lt;/i&gt;") == "<i>курсив</i>"
    assert normalize_inline_markup(
        "&lt;strong&gt;важно&lt;/strong&gt;"
    ) == "<strong>важно</strong>"
    # Attribute-bearing tags normalize too.
    assert normalize_inline_markup(
        "&lt;a href=&quot;http://x&quot;&gt;ссылка&lt;/a&gt;"
    ) == '<a href="http://x">ссылка</a>'
    # Text without markup is byte-identical.
    assert normalize_inline_markup("обычный текст") == "обычный текст"
    assert normalize_inline_markup("") == ""


def test_normalize_inline_markup_collapses_double_wraps():
    # run_005 defect: formatting + model tags double the wrap
    # (&lt;em&gt;<em>…</em>&lt;/em&gt;). Normalization collapses to one.
    assert normalize_inline_markup(
        "&lt;em&gt;<em>курсив</em>&lt;/em&gt;"
    ) == "<em>курсив</em>"
    assert normalize_inline_markup("<em><em>курсив</em></em>") == "<em>курсив</em>"
    # Deep nesting collapses fully.
    assert normalize_inline_markup(
        "&lt;em&gt;&lt;em&gt;<em>курсив</em>&lt;/em&gt;&lt;/em&gt;"
    ) == "<em>курсив</em>"


def test_normalize_inline_markup_keeps_other_entities_and_text():
    # Only inline-tag entities are converted; a literal &amp; in the text is
    # not a tag and stays byte-identical ("текст не меняется").
    assert normalize_inline_markup(
        "R&amp;D &lt;em&gt;курсив&lt;/em&gt;"
    ) == "R&amp;D <em>курсив</em>"
    assert normalize_inline_markup("5 &lt; 7") == "5 &lt; 7"


def test_find_mixed_script_ignores_inline_tags():
    # Legitimate markup (both normalized and entity-encoded) produces no
    # mixed-script flags — the run_005 "em" false positive.
    assert find_mixed_script("<em>Будь я проклят</em>") == []
    assert find_mixed_script("&lt;em&gt;Будь я проклят&lt;/em&gt;") == []
    assert find_mixed_script(
        "&lt;em&gt;<em>Будь я проклят</em>&lt;/em&gt;"
    ) == []
    # Latin outside tags is still flagged exactly as before (B5 semantics).
    assert find_mixed_script("Он увидел R.D.T. в дверях.") == ["R", "D", "T"]
    assert find_mixed_script("<em>Он</em> увидел R.D.T. в дверях.") == ["R", "D", "T"]
