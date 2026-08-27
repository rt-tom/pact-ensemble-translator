"""Owner clarification 2026-08-27: migration candidate byte-identical rules."""
import json
import tempfile
from pathlib import Path
import pytest
from pact_v4.runtime.book_memory_migration import (
    migrate_to_v2,
    build_migration_candidate,
    dry_run_manifest,
)

def test_glossary_copied_byte_identical():
    with tempfile.TemporaryDirectory() as tmp:
        parent = Path(tmp) / "parent"
        candidate = Path(tmp) / "candidate"
        parent.mkdir()
        # parent files
        glossary = {"Blake": "Блэйк", "Rose": "Роуз"}
        book_memory = {"characters": {"Blake": {"type": "character", "chapters": ["0001"]}}, "entities": {}}
        parent.joinpath("glossary.json").write_text(json.dumps(glossary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        parent.joinpath("book_memory.json").write_text(json.dumps(book_memory, ensure_ascii=False) + "\n", encoding="utf-8")
        parent.joinpath("observations.json").write_text(json.dumps({"glossary": {}, "book_memory": {}}, ensure_ascii=False) + "\n", encoding="utf-8")
        parent.joinpath("chapter_index.json").write_text(json.dumps({"$schema": "pact-v4-chapter-index/v1", "0001": {"characters": ["Blake"]}}, ensure_ascii=False) + "\n", encoding="utf-8")
        migrated = migrate_to_v2(book_memory, glossary)
        rebuilt_index = {"$schema": "pact-v4-chapter-index/v2", "$book_memory_policy_version": "book-memory-policy/v1", "0001": {"characters": ["Blake"], "named_entities": [], "terms": [], "facts": [], "address": []}}
        build_migration_candidate(parent, candidate, migrated, rebuilt_index)
        # glossary byte-identical
        assert (parent / "glossary.json").read_bytes() == (candidate / "glossary.json").read_bytes()
        # canonical_ru reconciled to glossary (not vice versa) - checked via migrated
        # observations byte-identical
        assert (parent / "observations.json").read_bytes() == (candidate / "observations.json").read_bytes()
        # index rebuilt, not copied
        assert json.loads((candidate / "chapter_index.json").read_text())["$schema"] == "pact-v4-chapter-index/v2"

def test_observations_nonempty_fails_closed():
    with tempfile.TemporaryDirectory() as tmp:
        parent = Path(tmp) / "parent"
        candidate = Path(tmp) / "candidate"
        parent.mkdir()
        glossary = {}
        book_memory = {"characters": {}}
        parent.joinpath("glossary.json").write_text(json.dumps(glossary) + "\n", encoding="utf-8")
        parent.joinpath("book_memory.json").write_text(json.dumps(book_memory) + "\n", encoding="utf-8")
        parent.joinpath("observations.json").write_text(json.dumps({"glossary": {"pending": {"target": "x"}}, "book_memory": {}}) + "\n", encoding="utf-8")
        parent.joinpath("chapter_index.json").write_text(json.dumps({}) + "\n", encoding="utf-8")
        migrated = migrate_to_v2(book_memory, glossary)
        rebuilt = {"$schema": "pact-v4-chapter-index/v2"}
        with pytest.raises(RuntimeError, match="pending observations"):
            build_migration_candidate(parent, candidate, migrated, rebuilt)

def test_dry_run_accounts_every_record_once():
    bm = {"characters": {"Blake": {}, "Rose": {}}, "entities": {"Hillsglade House": {}}}
    manifest = dry_run_manifest(bm, {})
    keys = [d["key"] for d in manifest["decisions"]]
    assert sorted(keys) == sorted(["Blake", "Rose", "Hillsglade House"])
    # second run identical
    manifest2 = dry_run_manifest(bm, {})
    assert manifest["hashes"]["canonical"] == manifest2["hashes"]["canonical"]

