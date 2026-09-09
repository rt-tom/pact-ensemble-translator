"""v2 index scope — presence-based full-memory selection (Rule 1) + fail-soft."""
import json, tempfile
from pathlib import Path
from pact_full_pipeline_runner_v1.build_chapter_index import build_chapter_index, pre_chapter_book_memory

def test_named_entities_present_terms_split():
    book_memory = {
        "schema": "pact-v4-book-memory/v2",
        "book_memory_policy_version": "book-memory-policy/v1",
        "characters": {
            "Blake Thorburn": {"memory_class": "named_character", "chapters": ["0001"], "variants": {}, "field_provenance": {}}
        },
        "entities": {
            "Hillsglade House": {"memory_class": "named_place", "chapters": ["0001"], "variants": {}, "field_provenance": {}},
            "Demesnes": {"memory_class": "world_term", "chapters": ["0001"], "variants": {}, "field_provenance": {}}
        },
        "facts": [],
        "policy": {"approved_terms": ["Demesnes"]},
        "pov": {"source_name": "Narrator"}
    }
    source_text = "Blake Thorburn walked to Hillsglade House and discussed Demesnes."
    entry = build_chapter_index(chapter_id="0002_bonds-1-2", source_text=source_text, book_memory=book_memory, glossary=[])
    assert "Blake Thorburn" in entry["characters"]
    assert "Hillsglade House" in entry["named_entities"]
    assert "Demesnes" in entry["terms"]
    # Ensure not flattened into characters
    assert "Hillsglade House" not in entry["characters"]

def test_later_learned_alias_included_when_source_present():
    # Alias Steph verified in 0002 is eligible for any chapter whose source contains
    # the alias surface, regardless of provenance ordering (Rule 1 presence-based).
    book_memory = {
        "schema": "pact-v4-book-memory/v2",
        "book_memory_policy_version": "book-memory-policy/v1",
        "characters": {
            "Stephanie": {"memory_class": "named_character", "chapters": ["0001"], "variants": {"Steph": {"chapter": "0002_bonds-1-2", "source_pids": ["p00026"]}}, "field_provenance": {}}
        },
        "entities": {},
        "facts": [],
        "policy": {"approved_terms": []}
    }
    source_text = "Steph visited."
    # Even for chapter 0001 (before alias provenance), source containing "Steph" matches
    entry_early = build_chapter_index(chapter_id="0001_bonds-1-1", source_text=source_text, book_memory=book_memory, glossary=[])
    assert "Stephanie" in entry_early["characters"]
    # For chapter 0003, alias also matches
    entry_late = build_chapter_index(chapter_id="0003_bonds-1-3", source_text=source_text, book_memory=book_memory, glossary=[])
    assert "Stephanie" in entry_late["characters"]
    # Absent surface remains excluded (Rule 1)
    entry_absent = build_chapter_index(chapter_id="0001_bonds-1-1", source_text="Blake visited.", book_memory=book_memory, glossary=[])
    assert "Stephanie" not in entry_absent["characters"]

def test_pre_chapter_book_memory_is_non_filtering_shallow_copy():
    book_memory = {
        "schema": "pact-v4-book-memory/v2",
        "book_memory_policy_version": "book-memory-policy/v1",
        "characters": {
            "Blake Thorburn": {"memory_class": "named_character", "chapters": ["0001"], "variants": {}, "field_provenance": {}},
        },
        "entities": {
            "Hillsglade House": {"memory_class": "named_place", "chapters": ["0002"], "variants": {}, "field_provenance": {}},
        },
        "facts": [
            {"fact": "Blake knows Joel", "keys": ["Blake Thorburn", "Joel"], "chapter": "0002_bonds-1-2"},
        ],
        "policy": {"approved_terms": []},
        "pov": {"source_name": "Narrator"},
    }
    copy = pre_chapter_book_memory(book_memory, "0001_bonds-1-1")
    # Distinct top-level mapping but same nested values (shallow)
    assert copy is not book_memory
    assert copy == book_memory
    assert copy["characters"] is book_memory["characters"]
    assert copy["facts"] is book_memory["facts"]
    # All facts/entities remain, including those attributed to target or later chapter
    assert any("Blake knows Joel" in str(f.get("fact", "")) for f in copy["facts"])
    assert "Hillsglade House" in copy["entities"]

def test_fact_presence_based_no_provenance_gate():
    book_memory = {
        "schema": "pact-v4-book-memory/v2",
        "book_memory_policy_version": "book-memory-policy/v1",
        "characters": {
            "Blake Thorburn": {"memory_class": "named_character", "chapters": ["0001"], "variants": {}, "field_provenance": {}},
            "Joel": {"memory_class": "named_character", "chapters": ["0002"], "variants": {}, "field_provenance": {}}
        },
        "entities": {},
        "facts": [
            {"fact": "Blake knows Joel", "keys": ["Blake Thorburn", "Joel"], "chapter": "0002_bonds-1-2"},
            {"fact": "Seed fact", "keys": ["Blake Thorburn"], "chapter": "", "seed": True}
        ],
        "policy": {"approved_terms": []}
    }
    # Fact attributed to N is eligible when its Rule 1 keys are present, regardless of chapter ordering
    entry = build_chapter_index(chapter_id="0002_bonds-1-2", source_text="Blake Thorburn and Joel were there", book_memory=book_memory, glossary=[])
    assert any("Blake knows Joel" in f for f in entry["facts"])
    assert any("Seed fact" in f for f in entry["facts"])
    # Absent keys remain excluded (neither key in source)
    entry_absent = build_chapter_index(chapter_id="0002_bonds-1-2", source_text="Alice walked alone", book_memory=book_memory, glossary=[])
    assert not any("Blake knows Joel" in f for f in entry_absent["facts"])

def test_world_term_first_recorded_in_target_chapter_selected_when_present():
    book_memory = {
        "schema": "pact-v4-book-memory/v2",
        "book_memory_policy_version": "book-memory-policy/v1",
        "characters": {},
        "entities": {
            "Demesnes": {"memory_class": "world_term", "chapters": ["0002_bonds-1-2"], "variants": {}, "field_provenance": {}}
        },
        "facts": [],
        "policy": {"approved_terms": ["Demesnes"]},
        "pov": {"source_name": "Narrator"}
    }
    entry = build_chapter_index(chapter_id="0002_bonds-1-2", source_text="They discussed Demesnes.", book_memory=book_memory, glossary=[])
    assert "Demesnes" in entry["terms"]
    entry_absent = build_chapter_index(chapter_id="0002_bonds-1-2", source_text="They discussed nothing.", book_memory=book_memory, glossary=[])
    assert "Demesnes" not in entry_absent["terms"]

def test_unapproved_stored_world_term_excluded_even_when_present():
    # Policy boundary: a stored world_term not in policy.approved_terms
    # must not enter terms or named_entities even when its surface is
    # present in the chapter source. Only approved world_terms are eligible.
    book_memory = {
        "schema": "pact-v4-book-memory/v2",
        "book_memory_policy_version": "book-memory-policy/v1",
        "characters": {},
        "entities": {
            "Demesnes": {"memory_class": "world_term", "chapters": ["0002"], "variants": {}, "field_provenance": {}},
            "UnapprovedTerm": {"memory_class": "world_term", "chapters": ["0001"], "variants": {}, "field_provenance": {}},
        },
        "facts": [],
        "policy": {"approved_terms": ["Demesnes"]},
        "pov": {"source_name": "Narrator"}
    }
    source = "They discussed Demesnes and UnapprovedTerm."
    entry = build_chapter_index(chapter_id="0002", source_text=source, book_memory=book_memory, glossary=[])
    assert "Demesnes" in entry["terms"]
    assert "UnapprovedTerm" not in entry["terms"]
    assert "UnapprovedTerm" not in entry["named_entities"]
    assert "UnapprovedTerm" not in entry["characters"]
    # Approved-term legacy path still works when term is present but not stored as world_term
    book_memory2 = {
        "schema": "pact-v4-book-memory/v2",
        "book_memory_policy_version": "book-memory-policy/v1",
        "characters": {},
        "entities": {},
        "facts": [],
        "policy": {"approved_terms": ["Demesnes"]},
        "pov": {"source_name": "Narrator"}
    }
    entry2 = build_chapter_index(chapter_id="0002", source_text="They discussed Demesnes.", book_memory=book_memory2, glossary=[])
    assert "Demesnes" in entry2["terms"]

def test_approved_stored_world_term_glossary_conflict_lock_without_source():
    # Regression for remove-pre-chapter-filter review finding (high):
    # an approved stored world_term whose glossary source has conflicting
    # targets must remain locked (included) even when its surface is absent
    # from the chapter source. Unapproved world_terms must remain excluded
    # even if conflicted.
    from pact_v4.phase2.risk import GlossaryEntry
    glossary_conflict = [GlossaryEntry(source_term="Demesnes", target_terms=("Домены", "Владения"))]
    book_memory = {
        "schema": "pact-v4-book-memory/v2",
        "book_memory_policy_version": "book-memory-policy/v1",
        "characters": {},
        "entities": {
            "Demesnes": {"memory_class": "world_term", "chapters": ["0002"], "variants": {}, "field_provenance": {}}
        },
        "facts": [],
        "policy": {"approved_terms": ["Demesnes"]},
        "pov": {"source_name": "Narrator"}
    }
    # No source surface for Demesnes, but glossary conflict -> locked
    entry = build_chapter_index(chapter_id="0001", source_text="Blake walked alone.", book_memory=book_memory, glossary=glossary_conflict)
    assert "Demesnes" in entry["terms"]
    # Unapproved stored world_term with same conflict must remain excluded even when present
    book_memory_unapproved = {
        "schema": "pact-v4-book-memory/v2",
        "book_memory_policy_version": "book-memory-policy/v1",
        "characters": {},
        "entities": {
            "UnapprovedTerm": {"memory_class": "world_term", "chapters": ["0001"], "variants": {}, "field_provenance": {}}
        },
        "facts": [],
        "policy": {"approved_terms": ["Demesnes"]},
        "pov": {"source_name": "Narrator"}
    }
    glossary_conflict_unapproved = [GlossaryEntry(source_term="UnapprovedTerm", target_terms=("A", "B"))]
    entry_unapproved = build_chapter_index(chapter_id="0001", source_text="UnapprovedTerm appears.", book_memory=book_memory_unapproved, glossary=glossary_conflict_unapproved)
    assert "UnapprovedTerm" not in entry_unapproved["terms"]
    assert "UnapprovedTerm" not in entry_unapproved["named_entities"]
    assert "UnapprovedTerm" not in entry_unapproved["characters"]
    # Approved non-conflict term absent must remain excluded (presence still required)
    glossary_no_conflict = [GlossaryEntry(source_term="Demesnes", target_terms=("Домены",))]
    entry_no_conflict = build_chapter_index(chapter_id="0001", source_text="Blake walked alone.", book_memory=book_memory, glossary=glossary_no_conflict)
    assert "Demesnes" not in entry_no_conflict["terms"]

def test_fail_soft_to_narrator_seed_on_unknown_schema():
    book_memory = {
        "schema": "pact-v4-book-memory/v999",
        "book_memory_policy_version": "book-memory-policy/v1",
        "characters": {"Blake": {"memory_class": "named_character", "chapters": ["0001"], "variants": {}, "field_provenance": {}}},
        "entities": {"Hillsglade House": {"memory_class": "named_place", "chapters": ["0001"], "variants": {}, "field_provenance": {}}},
        "facts": [{"fact": "Some fact", "keys": ["Blake"], "chapter": "0001"}],
        "pov": {"source_name": "Narrator"}
    }
    entry = build_chapter_index(chapter_id="0002", source_text="Blake and Hillsglade House", book_memory=book_memory, glossary=[])
    assert entry["named_entities"] == []
    assert entry["terms"] == []
    # Should contain only narrator
    assert "Narrator" in entry["characters"]
    assert "Blake" not in entry["characters"]

def test_fail_soft_missing_schema():
    # FINDING 3: missing $schema should fail soft to narrator+seed only, not legacy fallback
    book_memory = {
        # schema missing intentionally
        "book_memory_policy_version": "book-memory-policy/v1",
        "characters": {"Blake": {"memory_class": "named_character", "chapters": ["0001"], "variants": {}, "field_provenance": {}}},
        "entities": {"Hillsglade House": {"memory_class": "named_place", "chapters": ["0001"], "variants": {}, "field_provenance": {}}},
        "facts": [{"fact": "Seed fact", "seed": True, "keys": ["Narrator"], "chapter": ""}, {"fact": "Some fact", "keys": ["Blake"], "chapter": "0001"}],
        "pov": {"source_name": "Narrator"}
    }
    entry = build_chapter_index(chapter_id="0002", source_text="Blake and Hillsglade House", book_memory=book_memory, glossary=[])
    assert entry["named_entities"] == []
    assert entry["terms"] == []
    assert "Narrator" in entry["characters"]
    assert "Blake" not in entry["characters"]
    # Should contain seed fact only
    assert any("Seed fact" in f for f in entry["facts"])
    assert not any("Some fact" in f for f in entry["facts"])

def test_fail_soft_unknown_policy_version():
    # FINDING 3: unknown $book_memory_policy_version should fail soft to narrator+seed only
    book_memory = {
        "schema": "pact-v4-book-memory/v2",
        "book_memory_policy_version": "book-memory-policy/v999",
        "characters": {"Blake": {"memory_class": "named_character", "chapters": ["0001"], "variants": {}, "field_provenance": {}}},
        "entities": {"Hillsglade House": {"memory_class": "named_place", "chapters": ["0001"], "variants": {}, "field_provenance": {}}},
        "facts": [{"fact": "Seed fact", "seed": True, "keys": ["Narrator"], "chapter": ""}],
        "pov": {"source_name": "Narrator"}
    }
    entry = build_chapter_index(chapter_id="0002", source_text="Blake and Hillsglade House", book_memory=book_memory, glossary=[])
    assert entry["named_entities"] == []
    assert entry["terms"] == []
    assert "Narrator" in entry["characters"]
    assert "Blake" not in entry["characters"]

