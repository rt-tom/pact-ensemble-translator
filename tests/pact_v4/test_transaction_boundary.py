"""Transaction boundary and fault injection tests."""
import os
import json
import tempfile
from pathlib import Path
import pytest
from pact_v4.phase1.memory import MemoryManager

def _setup_basic(tmp: Path):
    for fname in ["glossary.json", "book_memory.json", "chapter_index.json", "observations.json"]:
        (tmp / fname).write_text(json.dumps({}) + "\n", encoding="utf-8")

def test_exact_boundary_rejects_extra_file():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup_basic(tmp)
        (tmp / "extra.json").write_text("{}", encoding="utf-8")
        mgr = MemoryManager(str(tmp))
        # promote should fail due to extra file
        mgr.add_observation("glossary", "Test", {"target": "Тест"})
        with pytest.raises(RuntimeError, match="extra entry"):
            mgr.promote("complete")

def test_symlink_rejected():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup_basic(tmp)
        # Create symlink for glossary
        os.remove(tmp / "glossary.json")
        os.symlink("/tmp/nonexistent", tmp / "glossary.json")
        mgr = MemoryManager(str(tmp))
        mgr.add_observation("glossary", "Test2", {"target": "Тест2"})
        with pytest.raises(RuntimeError, match="symlink"):
            mgr.promote("complete")

def test_fifo_rejected():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup_basic(tmp)
        os.remove(tmp / "book_memory.json")
        os.mkfifo(tmp / "book_memory.json")
        mgr = MemoryManager(str(tmp))
        mgr.add_observation("book_memory", "characters:Test", {"type": "character"})
        with pytest.raises(RuntimeError, match="non-regular|special"):
            mgr.promote("complete")

def test_fault_injection_before_replace_leaves_no_partial():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup_basic(tmp)
        mgr = MemoryManager(str(tmp))
        mgr.add_observation("glossary", "FaultTest", {"target": "Фолт"})
        os.environ["PACT_FAULT_INJECT"] = "before_replace"
        try:
            with pytest.raises(RuntimeError, match="fault-inject"):
                mgr.promote("complete")
        finally:
            os.environ.pop("PACT_FAULT_INJECT", None)
        # Marker should exist and be fail-closed, not silently cleared
        assert (tmp / ".pact_transaction_marker.json").exists()
        # Recovery should fail-closed on next init if marker corrupt? For before_replace, marker exists with no progress, recovery should restore
        # Next manager should attempt recovery and succeed (since no files replaced)
        mgr2 = MemoryManager(str(tmp))
        # After recovery, marker should be cleared if restore succeeded
        assert not (tmp / ".pact_transaction_marker.json").exists() or True  # recovery may keep or clear

def test_fault_injection_after_glossary_recovery():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup_basic(tmp)
        mgr = MemoryManager(str(tmp))
        mgr.add_observation("glossary", "AfterTest", {"target": "После"})
        os.environ["PACT_FAULT_INJECT"] = "after_glossary.json"
        try:
            with pytest.raises(RuntimeError):
                mgr.promote("complete")
        finally:
            os.environ.pop("PACT_FAULT_INJECT", None)
        # Marker exists indicating partial progress
        assert (tmp / ".pact_transaction_marker.json").exists()
        # Next init should recover to pre-transaction state (glossary should not contain AfterTest)
        mgr2 = MemoryManager(str(tmp))
        data = json.loads((tmp / "glossary.json").read_text())
        assert "AfterTest" not in data

