"""Portability contract for the B9 offline-validation test.

``test_b9_offline_validation`` must never embed machine-specific absolute
paths: it resolves its external artifacts (the run_005_remote directory, the
chapter 0001 source HTML, and the production memory dir) exclusively from the
environment variables ``PACT_B9_RUN005_DIR`` / ``PACT_B9_CHAPTER_HTML`` /
``PACT_B9_MEMORY_DIR`` and skips the whole module when any is unavailable.
These tests pin that resolution/skip contract without needing the private
artifacts (they run on every machine).
"""
from __future__ import annotations

import pytest

from tests.pact_v4 import test_b9_offline_validation as b9val


def _skip_reasons() -> list[str]:
    marks = b9val.pytestmark
    markers = marks if isinstance(marks, list) else [marks]
    return [
        m.kwargs["reason"]
        for m in markers
        if getattr(m, "markname", "") == "skipif" and "reason" in m.kwargs
    ]


def test_resolution_returns_none_when_env_vars_unset(monkeypatch):
    monkeypatch.delenv(b9val.PACT_B9_RUN005_DIR_ENV, raising=False)
    monkeypatch.delenv(b9val.PACT_B9_CHAPTER_HTML_ENV, raising=False)
    monkeypatch.delenv(b9val.PACT_B9_MEMORY_DIR_ENV, raising=False)
    assert b9val._resolve_external_paths() is None


def test_resolution_returns_none_when_paths_do_not_exist(monkeypatch, tmp_path):
    monkeypatch.setenv(b9val.PACT_B9_RUN005_DIR_ENV, str(tmp_path / "no-run"))
    monkeypatch.setenv(b9val.PACT_B9_CHAPTER_HTML_ENV,
                       str(tmp_path / "no-such.html"))
    monkeypatch.setenv(b9val.PACT_B9_MEMORY_DIR_ENV, str(tmp_path / "no-memory"))
    assert b9val._resolve_external_paths() is None


def test_resolution_returns_none_when_memory_files_missing(monkeypatch, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    chapter = tmp_path / "0001.html"
    chapter.write_text("<html><body>x</body></html>", encoding="utf-8")
    memory = tmp_path / "memory"
    memory.mkdir()
    # glossary.json exists, book_memory.json missing -> None.
    (memory / "glossary.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv(b9val.PACT_B9_RUN005_DIR_ENV, str(run_dir))
    monkeypatch.setenv(b9val.PACT_B9_CHAPTER_HTML_ENV, str(chapter))
    monkeypatch.setenv(b9val.PACT_B9_MEMORY_DIR_ENV, str(memory))
    assert b9val._resolve_external_paths() is None


def test_resolution_returns_supplied_paths_when_they_exist(monkeypatch, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    chapter = tmp_path / "0001.html"
    chapter.write_text("<html><body>x</body></html>", encoding="utf-8")
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "glossary.json").write_text("{}", encoding="utf-8")
    (memory / "book_memory.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv(b9val.PACT_B9_RUN005_DIR_ENV, str(run_dir))
    monkeypatch.setenv(b9val.PACT_B9_CHAPTER_HTML_ENV, str(chapter))
    monkeypatch.setenv(b9val.PACT_B9_MEMORY_DIR_ENV, str(memory))
    assert b9val._resolve_external_paths() == (run_dir, chapter, memory)


def test_module_skip_reason_documents_the_env_vars():
    reasons = _skip_reasons()
    assert reasons, "module must carry a skipif marker with a reason"
    text = " ".join(reasons)
    assert b9val.PACT_B9_RUN005_DIR_ENV in text
    assert b9val.PACT_B9_CHAPTER_HTML_ENV in text
    assert b9val.PACT_B9_MEMORY_DIR_ENV in text
