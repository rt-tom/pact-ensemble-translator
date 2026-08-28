"""Tests for book-memory role views (v4.2): select_relevant + render_book_context.

Covers: single-selector reuse, bounded role views, glossary > canonical_ru
resolution with conflict exclusion/diagnosis, deterministic overflow trimming,
identity-bound hashing, provenance, and the canonical population reducer
(normalized cross-section merge, class routing, verified-claim-only promotion,
explicit outcomes). Boundary-hardening negative matrix for the four-file set.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile

import pytest

from pact_v4.phase1.memory import (
    _attrs_compatible,
    _identity_matches,
    _norm_identity,
    _validate_exact_four_file_set,
    canonical_populate,
)
from pact_v4.phase2.risk import GlossaryEntry
from pact_v4.runtime.book_memory_role_views import (
    ROLE_TOKEN_BUDGET,
    ROLE_VIEW_ROLES,
    AuthoritativeState,
    CanonicalRecord,
    RelevanceResult,
    RoleCardProvenance,
    build_role_provenance,
    render_book_context,
    resolve_canonical_ru,
    select_relevant,
)


def _v2_bm(characters=None, entities=None, facts=None, pov=None):
    bm = {
        "schema": "pact-v4-book-memory/v2",
        "book_memory_policy_version": "book-memory-policy/v1",
        "characters": characters or {},
        "entities": entities or {},
        "facts": facts or [],
    }
    if pov is not None:
        bm["pov"] = pov
    return bm


def _state(bm, glossary, chapter_id="0003"):
    return AuthoritativeState(
        book_memory=bm, glossary=glossary, chapter_id=chapter_id,
        state_hash="s", glossary_hash="g",
    )


def _glossary(pairs):
    return [GlossaryEntry(source_term=s, target_terms=(t,)) for s, t in pairs]


# ---------------------------------------------------------------------------
# select_relevant (task 3.1 / 3.9)
# ---------------------------------------------------------------------------


def test_select_relevant_reuses_causal_logic_and_exceptions():
    bm = _v2_bm(
        characters={
            "Callan": {"gender": "male", "canonical_ru": "Каллан", "memory_class": "named_character"},
            "Paige": {"gender": "female", "canonical_ru": "Пейдж", "memory_class": "named_character"},
        },
        entities={"Riverbend": {"memory_class": "named_place", "canonical_ru": "Ривербенд"}},
        facts=[{"fact": "Blake drives a motorcycle", "seed": True}],
        pov={"source_name": "Blake", "gender": "male"},
    )
    rel = select_relevant(_state(bm, _glossary([])), {"p1": "Callan met Paige at Riverbend."})
    # Causal: only source-present names are selected.
    assert "Callan" in rel.selected_characters
    assert "Paige" in rel.selected_characters
    # Narrator/seed are explicit exceptions (independent of source presence).
    assert any(e.startswith("narrator:") for e in rel.exceptions)
    assert any(e.startswith("seed_fact:") for e in rel.exceptions)
    # The projected records carry attributes used by render_book_context.
    names = {r.name: r for r in rel.selected_records}
    assert names["Callan"].gender == "male"
    assert names["Riverbend"].kind == "entity"


def test_select_relevant_computed_once_reused_by_all_roles():
    bm = _v2_bm(characters={"Callan": {"gender": "male", "canonical_ru": "Каллан", "memory_class": "named_character"}})
    rel = select_relevant(_state(bm, _glossary([])), {"p1": "Callan spoke."})
    # Every role view is a projection of the SAME relevance result.
    views = {role: render_book_context(role, rel, _glossary([])) for role in ROLE_VIEW_ROLES}
    # Determinism: byte-identical text for identical input.
    again = render_book_context("translator", rel, _glossary([]))
    assert views["translator"].text == again.text


def test_later_chapter_fact_excluded_from_earlier_view():
    # A fact attributed to chapter 0005 must not appear in the chapter 0003 view.
    bm = _v2_bm(
        characters={"Callan": {"gender": "male", "canonical_ru": "Каллан", "memory_class": "named_character"}},
        facts=[{"fact": "Callan becomes a detective", "chapter": "0005"}],
    )
    rel = select_relevant(_state(bm, _glossary([]), chapter_id="0003"), {"p1": "Callan walked."})
    assert "Callan becomes a detective" not in rel.selected_facts


# ---------------------------------------------------------------------------
# render_book_context — bounded role views (task 3.2-3.9)
# ---------------------------------------------------------------------------


def test_translator_compose_separate_b12_block():
    bm = _v2_bm(characters={"Callan": {"gender": "male", "canonical_ru": "Каллан", "memory_class": "named_character"}})
    rel = select_relevant(_state(bm, _glossary([])), {"p1": "Callan spoke."})
    rc = render_book_context("translator", rel, _glossary([]), current_b1_2="B1.2 verified block")
    assert "BOOK MEMORY" in rc.text
    assert "CURRENT CHAPTER VERIFIED ENTITY FACTS (B1.2" in rc.text
    assert "B1.2 verified block" in rc.text
    # The B1.2 block is NOT treated as a durable role view (separate labelled section).
    assert rc.text.index("BOOK MEMORY") < rc.text.index("CURRENT CHAPTER VERIFIED")


def test_russian_editor_grammar_only_no_source():
    bm = _v2_bm(
        characters={
            "Callan": {"gender": "male", "canonical_ru": "Каллан", "memory_class": "named_character",
                       "facts": ["Callan is a detective"]},
        },
    )
    rel = select_relevant(_state(bm, _glossary([])), {"p1": "Callan spoke."})
    rc = render_book_context("russian_editor", rel, _glossary([]))
    assert "Каллан" in rc.text
    assert "gender: male" in rc.text
    # No plot/relationship facts leak into the R-editor view.
    assert "detective" not in rc.text


def test_audit_repair_source_prevails_instruction():
    bm = _v2_bm(characters={"Callan": {"gender": "male", "canonical_ru": "Каллан", "memory_class": "named_character"}})
    rel = select_relevant(_state(bm, _glossary([])), {"p1": "Callan spoke."})
    rc = render_book_context("audit_repair", rel, _glossary([]))
    assert "SOURCE PREVAILS" in rc.text


def test_glossary_role_candidate_limited():
    bm = _v2_bm(characters={
        "Callan": {"gender": "male", "canonical_ru": "Каллан", "memory_class": "named_character"},
        "Paige": {"gender": "female", "canonical_ru": "Пейдж", "memory_class": "named_character"},
    })
    rel = select_relevant(_state(bm, _glossary([])), {"p1": "Callan and Paige spoke."})
    # Only the candidate 'Callan' is supplied -> Paige must be excluded.
    rc = render_book_context("glossary", rel, _glossary([]), glossary_candidates=["Callan"])
    assert "Callan" in rc.text
    assert "Paige" not in rc.text


def test_glossary_conflict_excluded_and_diagnosed():
    bm = _v2_bm(characters={"Callan": {"gender": "male", "canonical_ru": "Каллан_WRONG", "memory_class": "named_character"}})
    rel = select_relevant(_state(bm, _glossary([("Callan", "Каллан")])), {"p1": "Callan spoke."})
    rc = render_book_context("glossary", rel, _glossary([("Callan", "Каллан")]), glossary_candidates=["Callan"])
    # Conflicting form excluded; diagnostic recorded.
    assert "Каллан_WRONG" not in rc.text
    assert "Каллан" not in rc.text or rc.conflicts
    assert rc.conflicts and rc.conflicts[0]["name"] == "Callan"
    # resolve_canonical_ru returns None + conflict for a disagreement.
    rec = {"canonical_ru": "Каллан_WRONG"}
    resolved, conflict = resolve_canonical_ru("Callan", rec, _glossary([("Callan", "Каллан")]))
    assert resolved is None and conflict is True


def test_glossary_wins_over_canonical_ru():
    # Glossary is authoritative: when book_memory has NO canonical_ru, the glossary
    # form is used (overrides the absent memory form). When both exist and agree,
    # the glossary form is used. (A disagreement is a conflict, tested separately.)
    bm = _v2_bm(characters={"Callan": {"gender": "male", "memory_class": "named_character"}})  # no canonical_ru
    rel = select_relevant(_state(bm, _glossary([("Callan", "Каллан_NEW")])), {"p1": "Callan spoke."})
    rc = render_book_context("glossary", rel, _glossary([("Callan", "Каллан_NEW")]), glossary_candidates=["Callan"])
    assert "Каллан_NEW" in rc.text
    assert rc.conflicts == ()


def test_overflow_trims_lowest_priority_not_add_calls():
    # Build many source-relevant characters so the audit card must trim.
    chars = {f"Char{i}": {"gender": "male", "canonical_ru": f"Ч{i}", "memory_class": "named_character"}
             for i in range(60)}
    bm = _v2_bm(characters=chars)
    src = " ".join(f"Char{i} spoke." for i in range(60))
    rel = select_relevant(_state(bm, _glossary([])), {"p1": src})
    rc = render_book_context("audit_repair", rel, _glossary([]))
    # Bounded by card budget (no unbounded growth).
    assert len(rc.included_canonical_ids) <= 30
    # Deterministic: highest-priority records retained.
    assert rc.included_canonical_ids


def test_rendered_hash_includes_glossary_slice_and_role():
    bm = _v2_bm(characters={"Callan": {"gender": "male", "canonical_ru": "Каллан", "memory_class": "named_character"}})
    rel = select_relevant(_state(bm, _glossary([])), {"p1": "Callan spoke."})
    a = render_book_context("translator", rel, _glossary([("Callan", "Каллан")]))
    b = render_book_context("translator", rel, _glossary([]))  # different glossary slice
    c = render_book_context("audit_repair", rel, _glossary([("Callan", "Каллан")]))  # different role
    assert a.canonical_hash != b.canonical_hash  # glossary slice changes hash
    assert a.canonical_hash != c.canonical_hash  # role changes hash


def test_provenance_distinguishes_empty_from_disabled():
    bm = _v2_bm()  # no entities, no source hits
    rel = select_relevant(_state(bm, _glossary([])), {"p1": "Nothing relevant here."})
    rc = render_book_context("audit_repair", rel, _glossary([]))
    prov = build_role_provenance("audit_repair", rc, rel)
    assert prov.included_count == 0
    assert prov.empty_reason == "no_relevant"
    assert prov.to_payload()["role"] == "audit_repair"


def test_unknown_role_raises():
    bm = _v2_bm()
    rel = select_relevant(_state(bm, _glossary([])), {"p1": "x"})
    with pytest.raises(ValueError):
        render_book_context("bogus", rel, _glossary([]))


# ---------------------------------------------------------------------------
# Canonical population reducer (task 2)
# ---------------------------------------------------------------------------


def test_create_routes_by_class_not_gender():
    nm, rep = canonical_populate(
        {}, [{"source": "Riverbend", "memory_class": "named_place", "verified": True,
              "canonical_ru": "Ривербенд", "evidence_pids": ["p1"], "chapter_id": "0001"}])
    assert rep[0]["operation"] == "create"
    assert rep[0]["scope"] == "entities"
    assert "Riverbend" in nm["entities"]


def test_pre_existing_cross_section_duplicates_merged():
    mem = {
        "characters": {"Callan": {"memory_class": "named_character", "gender": "male", "canonical_ru": "Каллан", "chapters": ["0001"]}},
        "entities": {"Callan": {"memory_class": "named_character", "gender": "male", "canonical_ru": "Каллан", "chapters": ["0001"]}},
    }
    nm, rep = canonical_populate(mem, [{"source": "Callan", "memory_class": "named_character", "verified": True,
                                       "gender": "male", "canonical_ru": "Каллан", "evidence_pids": ["p9"], "chapter_id": "0002"}])
    assert rep[0]["operation"] == "merge"
    assert rep[0]["reason"] == "merged_pre_existing_duplicates"
    assert "Callan" in nm["characters"] and "Callan" not in nm["entities"]


def test_conflict_incompatible_not_mutated():
    mem = {"characters": {"Peter": {"memory_class": "named_character", "gender": "male", "canonical_ru": "Питер"}}}
    nm, rep = canonical_populate(mem, [{"source": "Peter", "memory_class": "named_character", "verified": True,
                                       "gender": "female", "canonical_ru": "Питер", "evidence_pids": ["p1"], "chapter_id": "0003"}])
    assert rep[0]["operation"] == "conflict"
    assert nm["characters"]["Peter"]["gender"] == "male"  # unchanged


def test_reject_unverified_candidate_coreference():
    nm, rep = canonical_populate({}, [{"source": "the young lady", "memory_class": "chapter_local",
                                      "verified": False, "evidence_pids": ["p2"], "chapter_id": "0001"}])
    assert rep[0]["operation"] == "reject"
    assert nm == {}


def test_merge_appends_provenance_no_overwrite():
    mem = {"characters": {"Paige": {"memory_class": "named_character", "gender": "female", "canonical_ru": "Пейдж",
                                    "provenance_pids": ["p1"], "chapters": ["0001"]}}}
    nm, rep = canonical_populate(mem, [{"source": "Paige", "memory_class": "named_character", "verified": True,
                                       "gender": "female", "canonical_ru": "Пейдж", "evidence_pids": ["p2"], "chapter_id": "0002"}])
    assert rep[0]["operation"] == "merge"
    assert nm["characters"]["Paige"]["provenance_pids"] == ["p1", "p2"]


def test_no_op_when_identical_already_present():
    mem = {"characters": {"Stephanie": {"memory_class": "named_character", "gender": "female", "canonical_ru": "Стефани"}}}
    nm, rep = canonical_populate(mem, [{"source": "Stephanie", "memory_class": "named_character", "verified": True,
                                       "gender": "female", "canonical_ru": "Стефани", "evidence_pids": ["p1"], "chapter_id": "0001"}])
    # Compatible merge with identical established values -> merge (no new key, provenance retained).
    assert rep[0]["operation"] in ("merge", "no_op")
    assert "Stephanie" in nm["characters"]


def test_report_distinguishes_merged_from_created():
    mem = {}
    nm, rep = canonical_populate(mem, [
        {"source": "Callan", "memory_class": "named_character", "verified": True, "gender": "male",
         "canonical_ru": "Каллан", "evidence_pids": ["p1"], "chapter_id": "0001"},
        {"source": "Callan", "memory_class": "named_character", "verified": True, "gender": "male",
         "canonical_ru": "Каллан", "evidence_pids": ["p2"], "chapter_id": "0002"},
    ])
    ops = [r["operation"] for r in rep]
    assert "create" in ops and "merge" in ops
    # The second is distinguishable from the first (target recorded).
    assert rep[1]["target"] == "Callan"


# ---------------------------------------------------------------------------
# Boundary hardening: four-file set negative matrix (project standing rule)
# ---------------------------------------------------------------------------


def _make_four_file_dir(base):
    for fname in ("glossary.json", "book_memory.json", "chapter_index.json", "observations.json"):
        with open(os.path.join(base, fname), "w", encoding="utf-8") as f:
            json.dump({}, f)
    # Valid marker/backup allowed.
    return base


def test_four_file_set_accepts_clean():
    with tempfile.TemporaryDirectory() as d:
        _make_four_file_dir(d)
        assert _validate_exact_four_file_set(d) is None


def test_four_file_set_rejects_extra_top_level_file():
    with tempfile.TemporaryDirectory() as d:
        _make_four_file_dir(d)
        with open(os.path.join(d, "extra.json"), "w") as f:
            f.write("{}")
        assert _validate_exact_four_file_set(d) is not None


def test_four_file_set_rejects_extra_dir():
    with tempfile.TemporaryDirectory() as d:
        _make_four_file_dir(d)
        os.makedirs(os.path.join(d, "sneaky_dir"))
        assert _validate_exact_four_file_set(d) is not None


def test_four_file_set_rejects_symlink_canonical():
    with tempfile.TemporaryDirectory() as d:
        _make_four_file_dir(d)
        os.remove(os.path.join(d, "glossary.json"))
        os.symlink("/etc/hostname", os.path.join(d, "glossary.json"))
        assert _validate_exact_four_file_set(d) is not None


def test_four_file_set_rejects_fifo():
    with tempfile.TemporaryDirectory() as d:
        _make_four_file_dir(d)
        os.remove(os.path.join(d, "observations.json"))
        os.mkfifo(os.path.join(d, "observations.json"))
        assert _validate_exact_four_file_set(d) is not None


def test_four_file_set_rejects_malformed_json():
    with tempfile.TemporaryDirectory() as d:
        _make_four_file_dir(d)
        with open(os.path.join(d, "book_memory.json"), "w") as f:
            f.write("{not valid json")
        assert _validate_exact_four_file_set(d) is not None


def test_four_file_set_rejects_missing_canonical():
    with tempfile.TemporaryDirectory() as d:
        _make_four_file_dir(d)
        os.remove(os.path.join(d, "glossary.json"))
        assert _validate_exact_four_file_set(d) is not None


# ---------------------------------------------------------------------------
# Finding 1 fix: empty glossary_candidates must mean "include none"
# ---------------------------------------------------------------------------

def test_glossary_view_empty_candidates_includes_none_fresh_no_extraction():
    """Fresh run with no extraction: explicit empty candidate list yields no constraints.
    This is the candidate-limited requirement: an explicitly supplied EMPTY list
    must be treated as "include none", not "include all". Only when candidates
    are genuinely absent/unspecified (None) should the view fall back to all-relevant.
    """
    bm = _v2_bm(characters={
        "Callan": {"gender": "male", "canonical_ru": "Каллан", "memory_class": "named_character"},
        "Paige": {"gender": "female", "canonical_ru": "Пейдж", "memory_class": "named_character"},
    })
    rel = select_relevant(_state(bm, _glossary([])), {"p1": "Callan and Paige spoke."})
    # Explicit empty list -> no glossary constraints (empty/restricted)
    rc_empty = render_book_context("glossary", rel, _glossary([]), glossary_candidates=[])
    assert rc_empty.included_canonical_ids == ()
    assert rc_empty.empty_reason == "no_relevant"
    assert "Callan" not in rc_empty.text and "Paige" not in rc_empty.text
    # Genuinely absent (None) -> fallback to all-relevant (includes both)
    rc_none = render_book_context("glossary", rel, _glossary([]), glossary_candidates=None)
    assert set(rc_none.included_canonical_ids) == {"Callan", "Paige"}
    assert "Callan" in rc_none.text


def test_glossary_view_resume_cache_miss_explicit_empty():
    """Resumed run with missing/invalid glossary candidate cache: must be treated
    as explicit empty (restricted, not unrestricted). The glossary card must be
    empty/restricted, not include all source-relevant established forms.
    """
    bm = _v2_bm(characters={
        "Callan": {"gender": "male", "canonical_ru": "Каллан", "memory_class": "named_character"},
        "Paige": {"gender": "female", "canonical_ru": "Пейдж", "memory_class": "named_character"},
    })
    rel = select_relevant(_state(bm, _glossary([])), {"p1": "Callan and Paige spoke."})
    # Simulate resume cache-miss: caller detects missing/invalid cache and
    # passes explicit empty list to the renderer (fail-closed, not fallback).
    cache_miss_candidates: list = []  # explicit empty from cache-miss handling
    rc = render_book_context("glossary", rel, _glossary([]), glossary_candidates=cache_miss_candidates)
    assert rc.included_canonical_ids == ()
    assert rc.empty_reason == "no_relevant"
    # Via compute_role_views as the whole-chapter runner does on resume
    from pact_v4.runtime.book_memory_role_views import compute_role_views
    out = compute_role_views(
        book_memory=bm, glossary=_glossary([]), chapter_id="0003",
        source_map={"p1": "Callan and Paige spoke."},
        glossary_candidates=[],  # explicit empty from invalid cache
    )
    assert out["views"]["glossary"].included_canonical_ids == ()
    assert out["views"]["glossary"].empty_reason == "no_relevant"
    # Ensure None still means unrestricted (genuinely absent)
    out_none = compute_role_views(
        book_memory=bm, glossary=_glossary([]), chapter_id="0003",
        source_map={"p1": "Callan and Paige spoke."},
        glossary_candidates=None,
    )
    assert set(out_none["views"]["glossary"].included_canonical_ids) == {"Callan", "Paige"}


def test_real_colon_observations_preserve_evidence_pids():
    """Real B1.2 colon observations must preserve per-field/per-alias/per-fact evidence.
    A direct promotion of the real shape (field_provenance.gender.source_pids,
    variants.*.source_pids, fact source_pids) must not produce provenance_pids: [].
    """
    import tempfile as _tf, json as _js, os as _os
    from pact_v4.phase1.memory import MemoryManager
    from pact_v4.pipeline.b3_audit_repair import book_memory_observations_from_entity_context
    from pact_v4.audit.entity_extractor import ChapterEntityContext, EntityRecord, AnchorRef, AliasRef, EvidenceRef, EntityClaim
    anchor = AnchorRef(pid="p1", span="Callan appeared")
    gender_claim = EntityClaim(kind="gender", value="male", status="verified", evidence=(EvidenceRef(pid="p7", span="he"),), evidence_windows=())
    occ_claim = EntityClaim(kind="occupation", value="detective", status="verified", evidence=(EvidenceRef(pid="p3", span="detective"), EvidenceRef(pid="p4", span="detective"),), evidence_windows=())
    alias = AliasRef(surface="Cal", pid="p5", span="Cal")
    rec = EntityRecord(entity="Callan", canonical_type="person", anchor=anchor, claims=(gender_claim, occ_claim), aliases=(alias,), memory_class="named_character")
    ctx = ChapterEntityContext(schema="pact-v4-entity-context/v1", extractor_version="v1", chapter_id="0001", source_hash="abc", entities=(rec,))
    obs = book_memory_observations_from_entity_context(ctx, chapter_id="0001")
    # Verify the real shape carries the expected provenance fields
    assert "field_provenance" in obs["book_memory"]["characters:Callan"]
    assert "variants" in obs["book_memory"]["characters:Callan"]
    assert obs["book_memory"]["facts:Callan:0"]["source_pids"] == ["p3", "p4"]
    with _tf.TemporaryDirectory() as d:
        for fname in ("glossary.json","book_memory.json","chapter_index.json","observations.json"):
            with open(_os.path.join(d, fname), "w", encoding="utf-8") as f:
                _js.dump({}, f)
        with open(_os.path.join(d, "chapter_index.json"), "w", encoding="utf-8") as f:
            _js.dump({"$schema":"pact-v4-chapter-index/v2","$book_memory_policy_version":"book-memory-policy/v1"}, f)
        with open(_os.path.join(d, "book_memory.json"), "w", encoding="utf-8") as f:
            _js.dump({"schema":"pact-v4-book-memory/v2","book_memory_policy_version":"book-memory-policy/v1","characters":{},"entities":{},"facts":[]}, f)
        mgr = MemoryManager(d)
        for k,v in obs["book_memory"].items():
            mgr.add_observation("book_memory", k, v)
        mgr.promote("complete", _chapter_id="0001")
        bm = _js.load(open(_os.path.join(d, "book_memory.json"), encoding="utf-8"))
        rec_out = bm.get("characters", {}).get("Callan") or bm.get("entities", {}).get("Callan")
        assert rec_out is not None
        # Provenance must not be empty
        assert rec_out.get("provenance_pids"), "provenance_pids must not be empty for real shape"
        assert "p7" in rec_out["provenance_pids"]
        assert "p5" in rec_out["provenance_pids"]
        # Variants and field_provenance must be preserved with source_pids
        assert rec_out.get("variants", {}).get("Cal", {}).get("source_pids") == ["p5"]
        assert rec_out.get("field_provenance", {}).get("gender", {}).get("source_pids") == ["p7"]
        # Facts must be persisted with evidence, not as bare strings
        assert isinstance(rec_out.get("facts"), list) and rec_out["facts"]
        fact0 = rec_out["facts"][0]
        assert isinstance(fact0, dict)
        assert fact0.get("source_pids") == ["p3", "p4"]
        # Candidate report must carry evidence_pids
        assert bm.get("_candidate_reports")
        rpt = bm["_candidate_reports"][0]["report"][0]
        assert rpt.get("evidence_pids")
        assert "p7" in rpt["evidence_pids"]
