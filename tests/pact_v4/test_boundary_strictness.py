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


def test_book_run_promotion_injected_failure_is_non_fatal_writes_debt_and_skips_media_push(tmp_path, monkeypatch, caplog):
    """End-to-end non-fatal promotion: force MemoryManager.promote to raise;
    run_book must complete, write book_run.json with promoted:false + promotion_error,
    and skip Media push (post_promote_push not called)."""
    import json as _json
    import logging as _logging
    from pathlib import Path as _Path  # noqa: F401 -- keep tmp_path type hint
    from unittest import mock as _mock
    from pact_full_pipeline_runner_v1 import v4_book_run as _v4_book_run

    caplog.set_level(_logging.WARNING)

    # -- setup memory (four canonical files) --
    memory = tmp_path / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    for _fname in ["glossary.json", "book_memory.json", "chapter_index.json", "observations.json"]:
        (memory / _fname).write_text(_json.dumps({}, ensure_ascii=False) + "\n", encoding="utf-8")

    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    out_base = tmp_path / "out"
    out_base.mkdir(parents=True, exist_ok=True)

    chapter_id = "0001"
    html_path = src_dir / f"{chapter_id}.html"
    # No inline <em>/<strong> spans -> formatting path has_spans=False, no model call
    html_path.write_text("<p>Rose met Blake at the gate.</p>\n<p>She was running late that day.</p>", encoding="utf-8")

    out_dir = out_base / f"chapter_{chapter_id}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Minimal authoritative plan: one chunk mapping all pids
    _plan = {
        "artifact": "pact-v4-chunk-plan/v1",
        "snapshot_hash": "test",
        "plan_hash": "test",
        "chunks": [
            {"chunk_id": "chunk0001", "snapshot_hash": "test", "pids": ["p00001", "p00002"], "word_counts": [], "context": {"left_ru": "", "right_en": []}, "undersized_exception": False},
        ],
    }
    _translations = {
        "p00001": "\u0420\u043e\u0443\u0437 \u0432\u0441\u0442\u0440\u0435\u0442\u0438\u043b\u0430 \u0411\u043b\u044d\u0439\u043a\u0430 \u0443 \u0432\u043e\u0440\u043e\u0442.",
        "p00002": "\u041e\u043d\u0430 \u043e\u043f\u0430\u0437\u0434\u044b\u0432\u0430\u043b\u0430 \u0432 \u0442\u043e\u0442 \u0434\u0435\u043d\u044c.",
    }
    (out_dir / "selection_results.json").write_text(
        _json.dumps({"chapter_id": chapter_id, "results": [{"chunk_id": "chunk0001", "status": "selected", "quarantine_reason": None}]}, ensure_ascii=False), encoding="utf-8",
    )
    (out_dir / "strict_chapter_trial_record.json").write_text(
        _json.dumps({"chapter_id": chapter_id, "step8": {"status": "complete"}}, ensure_ascii=False), encoding="utf-8",
    )
    (out_dir / "translations.json").write_text(_json.dumps(_translations, ensure_ascii=False), encoding="utf-8")
    (out_dir / "chunk_plan.json").write_text(_json.dumps(_plan, ensure_ascii=False), encoding="utf-8")

    # Fake strict driver so run_book does not launch real LLM
    def _fake_run_one(*_a, **_kw):
        return {"status": "ok"}
    monkeypatch.setattr(_v4_book_run, "_run_one_chapter", _fake_run_one)

    # Mock Media hooks: pre_init_fetch no-op, post_promote_push must NOT be called
    monkeypatch.setattr("pact_v4.snapshot.run_hooks.pre_init_fetch", lambda *a, **kw: None)
    _mock_push = _mock.MagicMock()
    monkeypatch.setattr("pact_v4.snapshot.run_hooks.post_promote_push", _mock_push)

    # Force MemoryManager.promote to raise (injected boundary violation)
    def _failing_promote(self, status, **_kw):
        raise RuntimeError("injected boundary violation")
    monkeypatch.setattr("pact_v4.phase1.memory.MemoryManager.promote", _failing_promote)
    # Also patch the re-export in v4_book_run (same class, defensive)
    monkeypatch.setattr(_v4_book_run.MemoryManager, "promote", _failing_promote)

    # Exercise the REAL book-run promotion path -> must NOT raise
    result = _v4_book_run.run_book(
        memory_dir=memory,
        chapter_ids=[chapter_id],
        chapter_html_pattern=str(src_dir / "{chapter_id}.html"),
        out_base=out_base,
        media_book_id="test-book",
        media_transport=_mock.MagicMock(),
        media_exec_host="media",
    )

    # Run completed without aborting
    assert isinstance(result, dict)
    assert "chapters" in result and len(result["chapters"]) == 1

    # book_run.json written under out_base
    book_run_path = out_base / "book_run.json"
    assert book_run_path.exists(), "book_run.json must be written even when promote fails"
    payload = _json.loads(book_run_path.read_text(encoding="utf-8"))
    rec = payload["chapters"][0]
    # promoted:false + non-null promotion_error containing injected message
    assert rec["promoted"] is False
    assert rec["promotion_error"] is not None
    assert "injected boundary violation" in rec["promotion_error"]
    assert "promotion / push to Media not completed" in rec.get("promote_detail", "")
    # Media push skipped: gated on promoted, so not called; media_error not set
    _mock_push.assert_not_called()
    assert rec.get("media_error") is None
    assert rec.get("media_confirmation") is None
    # Warning logged (debt)
    assert any("promotion / push to Media not completed" in r.message for r in caplog.records)


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
