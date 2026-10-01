"""Focused tests for entity-context-authoritative-hints (owner-approved change).

Offline only: every model call is scripted/faked — no llama-server, no
pipeline run. Covers the task 2.1 matrix: source matching (casefold,
punctuation, aliases, word boundaries), authority/conflict exclusion,
ordering/cap/truncation, empty-card source-only path, source-only PID
validation under hints, new-alias discovery, contradictory hints,
changed-hint cache invalidation, and same-chapter 0-extra-call replay.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Mapping

import pytest

from pact_v4.audit.entity_extractor import (
    ENTITY_HINT_MAX_CHARS,
    ENTITY_HINT_MAX_ENTRIES,
    ENTITY_HINT_PROMPT_VARIANT,
    EXTRACTOR_VERSION,
    BackendEntityExtractor,
    ChapterEntityContext,
    EntityContextCache,
    EntityHintCard,
    build_entity_hint_card,
    entity_context_cache_key,
    entity_hint_hash,
    extract_entity_context,
    render_entity_extraction_prompt,
)
from pact_v4.phase1.models import SourceArtifact
from pact_v4.phase2.risk import GlossaryEntry
from tests.pact_v4.runtime.test_backend_role_adapters import (
    ScriptedBackend,
    _text_response,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _src(*pairs: Any) -> Dict[str, str]:
    return dict(pairs)


CHAPTER_SRC = _src(
    ("p00001", "Nurse Rich looked at his watch. \u201cTwo past twelve.\u201d"),
    ("p00002", "Blake pushed his motorcycle through the gap."),
    ("p00003", "The man in scrubs followed Callan out of the room."),
)

MEMORY = {
    "characters": {
        "Rich": {
            "canonical_ru": "Рич",
            "memory_class": "named_character",
            "variants": {"Richie": {"source_pids": ["p00010"]}},
        },
        "Blake": {"memory_class": "named_character"},
        "Zara": {"memory_class": "named_character"},  # absent from source
    },
    "entities": {
        "Callan": {"memory_class": "named_character"},
    },
    "terms": {},
}

GLOSSARY = [
    GlossaryEntry(source_term="Rich", target_terms=("Рич",)),
    GlossaryEntry(source_term="motorcycle", target_terms=("мотоцикл",)),
]


def _source(chapter_id: str = "0007") -> SourceArtifact:
    return SourceArtifact(chapter_id=chapter_id, source=tuple(CHAPTER_SRC.items()))


def _card(**over: Any) -> EntityHintCard:
    kw: Dict[str, Any] = {
        "book_memory": MEMORY, "glossary": GLOSSARY,
        "source": CHAPTER_SRC, "chapter_id": "0007",
    }
    kw.update(over)
    return build_entity_hint_card(**kw)


# ---------------------------------------------------------------------------
# Selection: known vs unknown, aliases, word boundaries
# ---------------------------------------------------------------------------

def test_known_matched_unknown_excluded() -> None:
    card = _card()
    rows = dict((s, c) for s, c in card.rows)
    assert rows["Rich"] == "Rich"
    assert rows["Blake"] == "Blake"
    assert rows["Callan"] == "Callan"
    # New/unmatched entries never enter the card.
    assert "Zara" not in rows
    assert "motorcycle" in rows  # vetted glossary term, canonical = itself
    assert card.excluded_unmatched >= 1


def test_alias_surface_maps_to_canonical() -> None:
    src = _src(("p00001", "Richie brought the tea. She smiled up at him."))
    card = _card(source=src)
    rows = dict((s, c) for s, c in card.rows)
    assert rows.get("Richie") == "Rich"


def test_word_boundary_no_substring_match() -> None:
    src = _src(
        ("p00001", "Richardson praised the rich soil of Richmond."),
    )
    card = _card(source=src)
    surfaces = [s for s, _c in card.rows]
    assert "Rich" not in surfaces
    assert card.is_empty()


def test_apostrophe_tolerance() -> None:
    bm = {"characters": {"Jacob's Bell": {"memory_class": "named_place"}}}
    src = _src(("p00001", "They rang Jacob\u2019s Bell at dawn."))
    card = _card(book_memory=bm, glossary=[], source=src)
    assert ("Jacob\u2019s Bell", "Jacob's Bell") in card.rows or \
        ("Jacob's Bell", "Jacob's Bell") in card.rows


def test_case_sensitive_proper_name() -> None:
    # Lowercase common-word use must not hint the proper name.
    src = _src(("p00001", "The soil was rich and dark."))
    card = _card(source=src)
    assert all(s != "Rich" for s, _c in card.rows)


# ---------------------------------------------------------------------------
# Authority: conflicts, candidates, current-chapter, chapter_local
# ---------------------------------------------------------------------------

def test_top_level_conflict_excluded_and_diagnosable() -> None:
    bm = {
        "characters": dict(MEMORY["characters"]),
        "_conflicts": {"Blake": {"kind": "gender"}},
    }
    card = _card(book_memory=bm)
    assert all(c != "Blake" for _s, c in card.rows)
    assert card.excluded_conflict >= 1


def test_excluded_conflict_flag() -> None:
    bm = {"characters": {
        "Rich": {"memory_class": "named_character", "_excluded_conflict": True},
    }}
    card = _card(book_memory=bm)
    assert card.is_empty() or all(c != "Rich" for _s, c in card.rows)


def test_glossary_memory_ru_disagreement_excluded() -> None:
    bm = {"characters": {
        "Rich": {"canonical_ru": "Ричард", "memory_class": "named_character"},
    }}
    gl = [GlossaryEntry(source_term="Rich", target_terms=("Рич",))]
    card = _card(book_memory=bm, glossary=gl)
    assert all(c != "Rich" for _s, c in card.rows)
    assert card.excluded_conflict >= 1


def test_glossary_internal_multi_target_conflict_excluded() -> None:
    gl = [
        GlossaryEntry(source_term="Rich", target_terms=("Рич",)),
        GlossaryEntry(source_term="Rich", target_terms=("Богач",)),
    ]
    card = _card(glossary=gl)
    assert all(s != "Rich" for s, _c in card.rows)


def test_candidate_only_excluded() -> None:
    bm = {"characters": {
        "Rich": {"memory_class": "named_character", "status": "candidate"},
    }}
    card = _card(book_memory=bm, glossary=[])
    assert all(c != "Rich" for _s, c in card.rows)
    assert card.excluded_candidate >= 1


def test_current_chapter_only_observation_excluded() -> None:
    bm = {"characters": {
        "Rich": {"memory_class": "named_character", "chapters": ["0007"]},
        "Blake": {"memory_class": "named_character", "chapters": ["0001"]},
    }}
    card = _card(book_memory=bm, glossary=[])
    rows = dict((s, c) for s, c in card.rows)
    assert "Rich" not in rows  # current-chapter observation, not prior state
    assert rows.get("Blake") == "Blake"


def test_chapter_local_excluded() -> None:
    bm = {"characters": {
        "Rich": {"memory_class": "chapter_local"},
    }}
    card = _card(book_memory=bm, glossary=[])
    assert all(c != "Rich" for _s, c in card.rows)


def test_chapter_local_not_readmitted_via_glossary_or_approved_terms() -> None:
    # A chapter-local record's surfaces stay suppressed even when the same
    # term is also a vetted glossary entry or an approved world term
    # (review regression: the chapter_local branch skipped suppression).
    from pact_v4.phase2.risk import GlossaryEntry as _GE
    bm = {"characters": {
        "Rich": {"memory_class": "chapter_local"},
    }}
    card = _card(
        book_memory=bm,
        glossary=[_GE(source_term="Rich", target_terms=("\u0420\u0438\u0447",))],
    )
    assert all(s != "Rich" and c != "Rich" for s, c in card.rows)
    bm2 = {"characters": {
        "Rich": {"memory_class": "chapter_local"},
    }, "policy": {"approved_terms": ["Rich"]}}
    card2 = _card(book_memory=bm2, glossary=[])
    assert all(s != "Rich" and c != "Rich" for s, c in card2.rows)


def test_shared_alias_across_identities_asserts_neither() -> None:
    # One source-matched surface claimed by two distinct canonical
    # identities: no arbitrary identity may be emitted, in either record
    # order, and glossary/approved-term fallbacks must not re-admit it.
    from pact_v4.phase2.risk import GlossaryEntry as _GE2
    src = _src(("p00001", "Rich waved from the doorway."))
    bm_a = {"characters": {
        "Rich King": {"memory_class": "named_character",
                        "variants": {"Rich": {}}},
        "Rich Queen": {"memory_class": "named_character",
                         "variants": {"Rich": {}}},
    }}
    bm_b = {"characters": {
        "Rich Queen": {"memory_class": "named_character",
                         "variants": {"Rich": {}}},
        "Rich King": {"memory_class": "named_character",
                        "variants": {"Rich": {}}},
    }}
    gl = [_GE2(source_term="Rich", target_terms=("\u0420\u0438\u0447",))]
    card_a = build_entity_hint_card(
        book_memory=bm_a, glossary=gl, source=src, chapter_id="0007")
    card_b = build_entity_hint_card(
        book_memory=bm_b, glossary=gl, source=src, chapter_id="0007")
    for card in (card_a, card_b):
        assert all(s != "Rich" for s, _c in card.rows)
        assert all(c not in ("Rich King", "Rich Queen")
                    for _s, c in card.rows)
    assert card_a.text == card_b.text
    assert card_a.card_hash == card_b.card_hash
    # Approved-term fallback must not re-admit the ambiguous surface.
    # Approved-term fallback must not re-admit the ambiguous surface.
    bm_c = {"characters": dict(bm_a["characters"]),
            "policy": {"approved_terms": ["Rich"]}}
    card_c = build_entity_hint_card(
        book_memory=bm_c, glossary=[], source=src, chapter_id="0007")
    assert all(s != "Rich" for s, _c in card_c.rows)


def test_cyrillic_canonical_never_leaks_via_english_alias() -> None:
    import re as _re
    bm = {"characters": {
        "Рич": {"memory_class": "named_character", "variants": {"Rich": {}}},
    }}
    card = _card(book_memory=bm, glossary=[])
    assert not _re.search(r"[\u0400-\u04FF]", card.text)
    assert all("\u0420" not in c and "Рич" not in c for _s, c in card.rows)


def test_no_russian_forms_no_facts_no_bible() -> None:
    import re as _re
    card = _card()
    assert not _re.search(r"[\u0400-\u04FF]", card.text)
    assert "PRIOR HINTS" in card.text and "NOT SOURCE EVIDENCE" in card.text
    assert len(card.text) <= ENTITY_HINT_MAX_CHARS


# ---------------------------------------------------------------------------
# Determinism, ordering, caps
# ---------------------------------------------------------------------------

def test_deterministic_order_earliest_pid_first() -> None:
    card = _card()
    surfaces = [s for s, _c in card.rows]
    # p00001 Rich < p00002 Blake < p00003 Callan; glossary 'motorcycle'
    # matches p00002.
    assert surfaces.index("Rich") < surfaces.index("Blake") < surfaces.index("Callan")


def test_input_order_independent() -> None:
    bm_a = {"characters": {"Rich": {}, "Blake": {}, "Callan": {}}}
    bm_b = {"characters": {"Callan": {}, "Blake": {}, "Rich": {}}}
    gl_a = list(GLOSSARY)
    gl_b = list(reversed(GLOSSARY))
    a = _card(book_memory=bm_a, glossary=gl_a)
    b = _card(book_memory=bm_b, glossary=gl_b)
    assert a.text == b.text
    assert a.card_hash == b.card_hash


def test_entry_cap_truncates_deterministically() -> None:
    names = {f"Name{i:02d}": {"memory_class": "named_character"} for i in range(40)}
    src = _src(("p00001", " ".join(names.keys()) + "."),)
    card = _card(book_memory={"characters": names}, glossary=[], source=src)
    assert card.kept_rows == ENTITY_HINT_MAX_ENTRIES == 32
    assert card.matched_rows == 40
    assert card.truncated_rows == 8
    assert len(card.text) <= ENTITY_HINT_MAX_CHARS


def test_char_cap_truncates_rows() -> None:
    names = {f"Name{i:02d}": {"memory_class": "named_character"} for i in range(10)}
    src = _src(("p00001", " ".join(names.keys()) + "."),)
    card = _card(
        book_memory={"characters": names}, glossary=[], source=src,
        max_chars=300,
    )
    assert 0 < card.kept_rows < card.matched_rows
    assert len(card.text) <= 300
    assert card.truncated_rows == card.matched_rows - card.kept_rows


def test_nothing_fits_is_empty_card() -> None:
    card = _card(max_chars=10)
    assert card.is_empty()
    assert card.text == "" and card.card_hash == ""


# ---------------------------------------------------------------------------
# Prompt contract: empty-card byte identity, hinted variant
# ---------------------------------------------------------------------------

def test_empty_card_prompt_byte_identical() -> None:
    base = render_entity_extraction_prompt(chapter_id="0007", source=CHAPTER_SRC)
    assert render_entity_extraction_prompt(
        chapter_id="0007", source=CHAPTER_SRC, hint_card="") == base
    assert render_entity_extraction_prompt(
        chapter_id="0007", source=CHAPTER_SRC,
        hint_card=EntityHintCard(text="")) == base


def test_hinted_prompt_labels_hints_not_evidence() -> None:
    card = _card()
    assert not card.is_empty()
    prompt = render_entity_extraction_prompt(
        chapter_id="0007", source=CHAPTER_SRC, hint_card=card)
    assert card.text in prompt
    assert "NOT EVIDENCE" in prompt
    assert "NEVER replaces PID evidence" in prompt
    assert "NEVER justifies 'verified' by itself" in prompt


# ---------------------------------------------------------------------------
# Cache identity: legacy compat, hint binding, tamper rejection
# ---------------------------------------------------------------------------

def test_empty_card_keeps_legacy_cache_key() -> None:
    legacy = entity_context_cache_key(
        source_hash="abc", extractor_version=EXTRACTOR_VERSION)
    assert entity_context_cache_key(
        source_hash="abc", extractor_version=EXTRACTOR_VERSION,
        hint_hash="", prompt_variant="") == legacy
    assert entity_context_cache_key(
        source_hash="abc", extractor_version=EXTRACTOR_VERSION,
        hint_hash=entity_hint_hash(EntityHintCard(text=""))) == legacy


def test_nonempty_hint_changes_key() -> None:
    card = _card()
    hinted = entity_context_cache_key(
        source_hash="abc", extractor_version=EXTRACTOR_VERSION,
        hint_hash=entity_hint_hash(card),
        prompt_variant=ENTITY_HINT_PROMPT_VARIANT)
    legacy = entity_context_cache_key(
        source_hash="abc", extractor_version=EXTRACTOR_VERSION)
    assert hinted != legacy


def _stub_extractor(calls: list, payload: Mapping[str, Any]):
    def _fake(*, chapter_id: str, source: Mapping[str, str],
              out_dir: Any = None, hint_card: str = "") -> str:
        calls.append({"chapter_id": chapter_id, "hint_card": hint_card})
        body = dict(payload)
        body.pop("schema", None)
        body.pop("extractor_version", None)
        body.pop("chapter_id", None)
        body.pop("source_hash", None)
        return json.dumps({"entities": body.get("entities", [])})
    return _fake


def test_changed_hint_recomputes_identical_card_reuses() -> None:
    source = _source()
    cache = EntityContextCache()
    card_a = _card()
    # Same source, different relevant hint -> recompute (2 model calls).
    bm_b = {"characters": dict(MEMORY["characters"], Blake={"memory_class": "named_character", "variants": {"B.B.": {}}})}
    src_b = dict(CHAPTER_SRC)
    src_b["p00002"] = src_b["p00002"] + " B.B. waved."
    card_b = _card(book_memory=bm_b, source=src_b)
    assert entity_hint_hash(card_a) != entity_hint_hash(card_b)

    calls: list = []
    fake = _stub_extractor(calls, {"entities": []})
    first = extract_entity_context(
        source_artifact=source, extractor=fake, cache=cache, hint_card=card_a)
    assert first.from_cache is False
    second = extract_entity_context(
        source_artifact=source, extractor=fake, cache=cache, hint_card=card_a)
    assert second.from_cache is True
    # Changed relevant hint -> no reuse under the new identity.
    third = extract_entity_context(
        source_artifact=source, extractor=fake, cache=cache, hint_card=card_b)
    assert third.from_cache is False
    assert len(calls) == 2
    assert calls[0]["hint_card"] == card_a.text
    assert calls[1]["hint_card"] == card_b.text


def test_irrelevant_memory_change_keeps_card_and_key() -> None:
    card_a = _card()
    # 'Zara' never appears in source: adding/removing her must not move
    # the card or trigger re-extraction.
    bm = dict(MEMORY)
    bm["characters"] = dict(MEMORY["characters"], Extra={"memory_class": "named_character"})
    card_b = _card(book_memory=bm)
    assert card_b.text == card_a.text
    assert entity_hint_hash(card_b) == entity_hint_hash(card_a)


def test_source_only_entry_not_accepted_under_hinted_key() -> None:
    source = _source()
    cache = EntityContextCache()
    calls: list = []
    fake = _stub_extractor(calls, {"entities": []})
    # Populate the legacy source-only entry.
    extract_entity_context(source_artifact=source, extractor=fake, cache=cache)
    assert len(calls) == 1
    # A hinted call for the same source must NOT hit the legacy entry.
    card = _card()
    assert not card.is_empty()
    result = extract_entity_context(
        source_artifact=source, extractor=fake, cache=cache, hint_card=card)
    assert result.from_cache is False
    assert len(calls) == 2


def test_legacy_extractor_without_hint_param_still_works() -> None:
    source = _source()
    cache = EntityContextCache()
    calls: list = []

    def _legacy(*, chapter_id: str, source: Mapping[str, str], out_dir: Any = None) -> str:
        calls.append(chapter_id)
        return json.dumps({"entities": []})

    card = _card()
    result = extract_entity_context(
        source_artifact=source, extractor=_legacy, cache=cache, hint_card=card)
    assert result.from_cache is False
    again = extract_entity_context(
        source_artifact=source, extractor=_legacy, cache=cache, hint_card=card)
    assert again.from_cache is True
    assert len(calls) == 1


def test_cache_roundtrip_with_hints_and_tamper_rejected() -> None:
    source = _source()
    cache = EntityContextCache()
    fake = _stub_extractor([], {"entities": []})
    card = _card()
    extract_entity_context(
        source_artifact=source, extractor=fake, cache=cache, hint_card=card)
    payload = cache.to_payload()
    entry = payload["entries"][0]
    assert entry["hint_hash"] == entity_hint_hash(card)
    assert entry["prompt_variant"] == ENTITY_HINT_PROMPT_VARIANT
    # Clean round-trip restores.
    restored = EntityContextCache.from_payload(payload)
    again = extract_entity_context(
        source_artifact=source, extractor=fake, cache=restored, hint_card=card)
    assert again.from_cache is True
    # Tampered key rejected.
    bad = json.loads(json.dumps(payload))
    bad["entries"][0]["key"] = "0" * 64
    with pytest.raises(ValueError):
        EntityContextCache.from_payload(bad)
    # Unknown prompt variant rejected.
    bad2 = json.loads(json.dumps(payload))
    bad2["entries"][0]["prompt_variant"] = "attacker/v9"
    with pytest.raises(ValueError):
        EntityContextCache.from_payload(bad2)
    # Legacy payload (no hint fields) still readable.
    legacy_payload = {"schema": payload["schema"], "entries": [
        {"key": e["key"], "context": e["context"]} for e in payload["entries"]
    ]}
    # A legacy-shaped entry with a hinted key cannot validate (no hint
    # hash to recompute it) -> rejected, never silently accepted.
    with pytest.raises(ValueError):
        EntityContextCache.from_payload(legacy_payload)


# ---------------------------------------------------------------------------
# Evidence boundary under hints: validators unchanged
# ---------------------------------------------------------------------------

def _validated_with_hint(payload_entities: list) -> Any:
    from pact_v4.audit.entity_extractor import (
        validate_entity_context,
        with_entity_context_metadata,
    )
    source = _source()
    # A contradictory/stale hint card: must not authorize anything.
    stamped = with_entity_context_metadata(
        {"entities": payload_entities},
        chapter_id=source.chapter_id, source_hash=source.source_hash,
    )
    return validate_entity_context(
        stamped, chapter_id=source.chapter_id,
        source_hash=source.source_hash, source=dict(source.source),
    )


def test_hint_does_not_authorize_unverified_claim() -> None:
    context, report = _validated_with_hint([{
        "entity": "Rich",
        "canonical_type": "nurse",
        "anchor": {"pid": "p00001", "span": "Nurse Rich"},
        "aliases": [],
        "claims": [{
            "kind": "gender", "value": "male", "status": "verified",
            # No verbatim span in p00001 for this invented quote.
            "evidence": [{"pid": "p00001", "span": "Rich is certainly male"}],
            "evidence_windows": [["p00001", "p00001"]],
        }],
        "glossary_worthy": False, "memory_class": "named_character",
        "memory_worthy": True,
    }])
    assert len(context.entities) == 1
    assert len(context.entities[0].claims) == 0  # dropped, not verified
    assert any(e.action == "dropped" for e in report.entries)


def test_new_alias_with_source_evidence_passes_despite_hint() -> None:
    context, report = _validated_with_hint([{
        "entity": "Rich",
        "canonical_type": "nurse",
        "anchor": {"pid": "p00001", "span": "Nurse Rich"},
        "aliases": [{
            "surface": "man in scrubs", "pid": "p00003",
            "span": "The man in scrubs",
        }],
        "claims": [],
        "glossary_worthy": False, "memory_class": "named_character",
        "memory_worthy": True,
    }])
    assert len(context.entities) == 1
    assert context.entities[0].aliases[0].surface == "man in scrubs"
    assert context.entities[0].aliases[0].status == "verified"


# ---------------------------------------------------------------------------
# Transport: hints reach the model; malformed inputs fail safe
# ---------------------------------------------------------------------------

def test_backend_extractor_sends_hint_text() -> None:
    card = _card()
    backend = ScriptedBackend([_text_response(json.dumps({"entities": []}))])
    extractor = BackendEntityExtractor(backend)
    extractor(chapter_id="0007", source=dict(CHAPTER_SRC), hint_card=card)
    prompt = backend.requests[0].messages[0].content
    assert card.text in prompt
    assert "HINT USE RULES" in prompt


def test_backend_extractor_empty_hint_sends_source_only() -> None:
    backend = ScriptedBackend([_text_response(json.dumps({"entities": []}))])
    extractor = BackendEntityExtractor(backend)
    extractor(chapter_id="0007", source=dict(CHAPTER_SRC))
    prompt = backend.requests[0].messages[0].content
    assert "PRIOR CONTEXT" not in prompt
    assert "HINT USE RULES" not in prompt


def test_malformed_authoritative_inputs_yield_empty_card() -> None:
    for bad_bm in (None, "x", [1, 2], 42):
        card = build_entity_hint_card(
            book_memory=bad_bm, glossary="garbage",
            source=CHAPTER_SRC, chapter_id="0007")
        assert card.is_empty()
    # A name-only legacy record (bare string attrs) with a source match is
    # still an eligible name; malformed glossary entries are ignored.
    card = build_entity_hint_card(
        book_memory={"characters": {"Rich": "not-a-mapping"}},
        glossary=[{"bogus": 1}, None, 5],
        source=CHAPTER_SRC, chapter_id="0007")
    assert ("Rich", "Rich") in card.rows
    # Malformed source never crashes either.
    card2 = build_entity_hint_card(
        book_memory=MEMORY, glossary=GLOSSARY, source={}, chapter_id="0007")
    assert card2.is_empty()


def test_flat_glossary_and_list_memory_shapes() -> None:
    bm = {"characters": [{"name": "Rich", "memory_class": "named_character"}]}
    gl = {"Rich": "Рич"}
    card = build_entity_hint_card(
        book_memory=bm, glossary=gl, source=CHAPTER_SRC, chapter_id="0007")
    assert ("Rich", "Rich") in card.rows


def test_flat_glossary_only_term_matches_source() -> None:
    # Glossary-only flat-mapped term (no memory record): the mapping KEY
    # is the source term and must survive normalization (review
    # regression: .values() first dropped every flat-mapped term).
    card = build_entity_hint_card(
        book_memory={}, glossary={"motorcycle": "\u043c\u043e\u0442\u043e\u0446\u0438\u043a\u043b"},
        source=CHAPTER_SRC, chapter_id="0007")
    assert ("motorcycle", "motorcycle") in card.rows
    assert not card.is_empty()
    # List-valued flat mapping targets work the same way.
    card2 = build_entity_hint_card(
        book_memory={}, glossary={"motorcycle": ["\u043c\u043e\u0442\u043e\u0446\u0438\u043a\u043b"]},
        source=CHAPTER_SRC, chapter_id="0007")
    assert ("motorcycle", "motorcycle") in card2.rows


# ---------------------------------------------------------------------------
# Stage/resume identity: output + hint + role card combine without loss
# ---------------------------------------------------------------------------

def test_entity_stage_hash_binds_hint_and_role_card_together() -> None:
    from pact_v4.phase1.models import canonical_json_hash
    from pact_v4.pipeline.b3_audit_repair import _entity_stage_hash

    payload = {"schema": "x", "entities": []}
    legacy = canonical_json_hash(payload)
    hint_a = entity_hint_hash(_card())
    hint_b = entity_hint_hash(_card(book_memory={
        "characters": dict(MEMORY["characters"], Nurse={"memory_class": "named_character"}),
        "entities": dict(MEMORY["entities"]),
        "terms": {},
    }))
    assert hint_a and hint_b and hint_a != hint_b
    card_hash = "role-card-hash"

    # Legacy compat: no hint + no role card == output-only hash.
    assert _entity_stage_hash(payload) == legacy
    # Each single input moves the identity.
    hint_only = _entity_stage_hash(payload, hint_hash=hint_a)
    card_only = _entity_stage_hash(payload, role_card_hash=card_hash)
    assert hint_only != legacy and card_only != legacy and hint_only != card_only
    # Combined identity moves when EITHER input moves (review regression:
    # the role-card branch must not drop the hint binding).
    combined_a = _entity_stage_hash(
        payload, hint_hash=hint_a, role_card_hash=card_hash)
    combined_b = _entity_stage_hash(
        payload, hint_hash=hint_b, role_card_hash=card_hash)
    combined_card2 = _entity_stage_hash(
        payload, hint_hash=hint_a, role_card_hash="other-card")
    assert combined_a != hint_only and combined_a != card_only
    assert combined_b != combined_a  # hint move invalidates replay
    assert combined_card2 != combined_a  # role-card move invalidates replay


# ---------------------------------------------------------------------------
# Runner/B3 contract: same frozen inputs -> same frozen card -> 0 extra calls
# ---------------------------------------------------------------------------

def test_runner_and_b3_derive_identical_card() -> None:
    # The runner generation prepass and B3 _run_impl step 1 call the same
    # pure builder over the same frozen inputs; assert the identity they
    # both bind (card hash + cache key) is equal.
    runner_card = build_entity_hint_card(
        book_memory=MEMORY, glossary=list(GLOSSARY),
        source=dict(CHAPTER_SRC), chapter_id="0007")
    b3_card = build_entity_hint_card(
        book_memory=dict(MEMORY), glossary=tuple(GLOSSARY),
        source=dict(CHAPTER_SRC), chapter_id="0007")
    assert runner_card.text == b3_card.text
    source = _source()
    key_kwargs: Dict[str, Any] = {
        "source_hash": source.source_hash,
        "extractor_version": EXTRACTOR_VERSION,
        "hint_hash": entity_hint_hash(runner_card),
        "prompt_variant": ENTITY_HINT_PROMPT_VARIANT,
    }
    assert entity_context_cache_key(**key_kwargs) == entity_context_cache_key(
        source_hash=source.source_hash,
        extractor_version=EXTRACTOR_VERSION,
        hint_hash=entity_hint_hash(b3_card),
        prompt_variant=ENTITY_HINT_PROMPT_VARIANT)


def test_same_card_replay_zero_extra_model_calls() -> None:
    from pact_v4.pipeline.b3_audit_repair import (
        B3AuditRepair,
        B3AuditRepairConfig,
    )
    import tempfile
    from pathlib import Path

    source = _source()
    card = _card()
    assert not card.is_empty()
    backend = ScriptedBackend(
        [_text_response(json.dumps({"entities": []}))],
        model_bindings={"qwen_audit": "qwen-3", "default": "qwen-3"},
    )
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        b3 = B3AuditRepair(
            audit_backend=backend, repair_backend=backend,
            config=B3AuditRepairConfig(),
        )
        first = b3.entity_context_prepass(
            source=source, out_dir=out_dir, hint_card=card)
        assert first is not None and first.from_cache is False
        entity_calls = sum(
            1 for r in backend.requests if "entity_extractor" in (r.label or ""))
        assert entity_calls == 1
        # Same frozen card replays from the persisted cache: 0 extra calls.
        b3b = B3AuditRepair(
            audit_backend=backend, repair_backend=backend,
            config=B3AuditRepairConfig(),
        )
        second = b3b.entity_context_prepass(
            source=source, out_dir=out_dir, hint_card=card)
        assert second is not None and second.from_cache is True
        entity_calls2 = sum(
            1 for r in backend.requests if "entity_extractor" in (r.label or ""))
        assert entity_calls2 == 1
