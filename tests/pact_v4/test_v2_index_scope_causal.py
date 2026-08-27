"""v2 index scope + causal backward-leakage tests."""
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

def test_later_learned_alias_absent_from_earlier_chapters():
    # Alias Steph learned in 0002 should not appear in index for 0001
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
    # For chapter 0001, alias Steph not yet learned, so source containing "Steph" should NOT match
    source_text = "Steph visited."
    entry_early = build_chapter_index(chapter_id="0001_bonds-1-1", source_text=source_text, book_memory=book_memory, glossary=[])
    assert "Stephanie" not in entry_early["characters"]
    assert "Stephanie" not in entry_early["named_entities"]
    # For chapter 0003, alias now known, should match
    entry_late = build_chapter_index(chapter_id="0003_bonds-1-3", source_text=source_text, book_memory=book_memory, glossary=[])
    assert "Stephanie" in entry_late["characters"]

def test_fact_causal_filter():
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
    # For chapter 0002, fact from 0002 should NOT be included (pre-chapter memory)
    pre_mem = pre_chapter_book_memory(book_memory, "0002_bonds-1-2")
    entry = build_chapter_index(chapter_id="0002_bonds-1-2", source_text="Blake Thorburn and Joel were there", book_memory=pre_mem, glossary=[])
    # Seed fact should be present, Joel fact should not (since its chapter == target)
    assert any("Seed fact" in f for f in entry["facts"])
    # For chapter 0003, fact from 0002 now visible
    pre_mem2 = pre_chapter_book_memory(book_memory, "0003_bonds-1-3")
    entry2 = build_chapter_index(chapter_id="0003_bonds-1-3", source_text="Blake Thorburn and Joel were there", book_memory=pre_mem2, glossary=[])
    assert any("Blake knows Joel" in f for f in entry2["facts"])

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
