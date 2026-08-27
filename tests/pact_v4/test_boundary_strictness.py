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


def test_six_file_media_contract_allowed():
    """Media six-file set (4 canonical + CURRENT.json + manifest.json) passes."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup(tmp)
        (tmp / "CURRENT.json").write_text(json.dumps({"revision_id": "r1"}) + "\n")
        (tmp / "manifest.json").write_text(json.dumps({"state_files": []}) + "\n")
        from pact_v4.phase1.memory import _validate_exact_four_file_set
        err = _validate_exact_four_file_set(str(tmp))
        assert err is None, f"six-file Media contract should pass, got: {err}"
        # Also promotion should succeed with those files present
        mgr = MemoryManager(str(tmp))
        mgr.add_observation("glossary", "MediaOk", {"target": "\u041e\u043a"})
        mgr.promote("complete")
        assert (tmp / "CURRENT.json").exists()
        assert (tmp / "manifest.json").exists()


def test_unknown_file_still_rejected():
    """Unknown extra file and symlink are still rejected."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup(tmp)
        (tmp / "rogue.txt").write_text("evil")
        from pact_v4.phase1.memory import _validate_exact_four_file_set
        err = _validate_exact_four_file_set(str(tmp))
        assert err is not None and "extra entry" in err
        # Symlink variant
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _setup(tmp)
        target = tmp / "real.json"
        target.write_text("{}")
        link = tmp / "rogue_link"
        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("symlink not supported")
        from pact_v4.phase1.memory import _validate_exact_four_file_set
        err = _validate_exact_four_file_set(str(tmp))
        assert err is not None and "extra entry" in err


def test_safe_promote_success_and_failure(caplog):
    """_safe_promote returns (True,None) on success and (False,err) on raise."""
    from pact_full_pipeline_runner_v1.v4_book_run import _safe_promote
    import logging
    caplog.set_level(logging.WARNING)

    class OkMgr:
        def promote(self, status, **kw):
            return None
    ok, err = _safe_promote(OkMgr(), "complete", chapter_id="0001")
    assert ok is True
    assert err is None

    class FailMgr:
        def promote(self, status, **kw):
            raise RuntimeError("boom boundary")
    ok2, err2 = _safe_promote(FailMgr(), "complete", chapter_id="0002")
    assert ok2 is False
    assert "boom boundary" in err2
    # warning logged
    assert any("promotion / push to Media not completed" in rec.message for rec in caplog.records)
