"""Transaction interruption/recovery fault injection BEFORE EACH of 4 replacements AND before each post-verify step."""
import os, json, tempfile, hashlib
from pathlib import Path
import pytest
from pact_v4.phase1.memory import MemoryManager

def _setup_basic(tmp: Path):
    for fname in ["glossary.json", "book_memory.json", "chapter_index.json", "observations.json"]:
        (tmp / fname).write_text(json.dumps({fname: "orig"}) + "\n")

def _hash(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

FAUL_POINTS = [
    "before_glossary.json",
    "before_book_memory.json",
    "before_chapter_index.json",
    "before_observations.json",
    "after_glossary.json",
    "after_book_memory.json",
    "after_chapter_index.json",
    "after_observations.json",
    "before_verify",
    "before_verify_glossary.json",
    "before_verify_book_memory.json",
    "after_verify",
]

@pytest.mark.parametrize("fault", FAUL_POINTS)
def test_fault_injection_matrix(fault):
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup_basic(tmp)
        mgr = MemoryManager(str(tmp))
        mgr.add_observation("glossary", "Term", {"target": "Термин"})
        mgr.add_observation("book_memory", "characters:Hero", {"type": "character", "memory_class": "named_character", "first_seen_chapter": "0001", "chapters": ["0001"], "variants": {}, "field_provenance": {}})
        hashes_before = {f: _hash(tmp / f) for f in ["glossary.json","book_memory.json","chapter_index.json","observations.json"]}
        os.environ["PACT_FAULT_INJECT"] = fault
        try:
            with pytest.raises(RuntimeError):
                mgr.promote("complete")
        finally:
            os.environ.pop("PACT_FAULT_INJECT", None)
        # Marker should exist after fault
        assert (tmp / ".pact_transaction_marker.json").exists()
        # Recovery on next init should restore pre-transaction state
        mgr2 = MemoryManager(str(tmp))
        # After recovery, marker cleared or recovery succeeded
        # Hashes should match before (since promotion didn't complete)
        for f in ["glossary.json","book_memory.json","chapter_index.json","observations.json"]:
            assert _hash(tmp / f) == hashes_before[f], f"file {f} not restored after fault {fault}"
        assert not (tmp / ".pact_transaction_marker.json").exists()

def test_success_clears_marker():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup_basic(tmp)
        mgr = MemoryManager(str(tmp))
        mgr.add_observation("glossary", "OkTerm", {"target": "Ок"})
        mgr.promote("complete")
        assert not (tmp / ".pact_transaction_marker.json").exists()
        data = json.loads((tmp / "glossary.json").read_text())
        assert "OkTerm" in data
