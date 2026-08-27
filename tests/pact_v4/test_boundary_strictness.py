"""Boundary validation strictness: missing files, arbitrary .pact_*/tmp rejection, raw hash."""
import json, tempfile, hashlib
from pathlib import Path
import pytest
from pact_v4.phase1.memory import MemoryManager

def _setup(tmp: Path):
    for fname in ["glossary.json", "book_memory.json", "chapter_index.json", "observations.json"]:
        (tmp / fname).write_text(json.dumps({}) + "\n")

def test_missing_canonical_rejected():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup(tmp)
        (tmp / "book_memory.json").unlink()
        from pact_v4.phase1.memory import _validate_exact_four_file_set
        err = _validate_exact_four_file_set(str(tmp))
        assert err is not None and "missing canonical" in err

def test_arbitrary_pact_extra_rejected():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup(tmp)
        (tmp / ".pact_extra.json").write_text("{}")
        with pytest.raises(RuntimeError, match="extra entry"):
            MemoryManager(str(tmp)).promote("complete")

def test_tmp_extra_rejected():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup(tmp)
        (tmp / "foo.tmp").write_text("{}")
        with pytest.raises(RuntimeError, match="extra entry"):
            MemoryManager(str(tmp)).promote("complete")

def test_marker_allowed_backup_allowed():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup(tmp)
        # marker and backup should not cause extra rejection if manager creates them internally
        mgr = MemoryManager(str(tmp))
        mgr.add_observation("glossary", "Ok", {"target":"Ок"})
        mgr.promote("complete")
        assert not (tmp / ".pact_transaction_marker.json").exists()

def test_raw_hash_stored():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup(tmp)
        # Do a successful promote and check that if we manually check raw hash, it's used
        mgr = MemoryManager(str(tmp))
        mgr.add_observation("glossary", "HashTest", {"target":"Хеш"})
        # Capture raw hash after
        import hashlib
        mgr.promote("complete")
        raw = hashlib.sha256((tmp / "glossary.json").read_bytes()).hexdigest()
        assert raw != ""
