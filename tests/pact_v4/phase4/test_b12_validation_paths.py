"""Portability contract for the B12 run_004 validation test.

``test_b12_call_optimization_validation`` must never embed machine-specific
absolute paths: it resolves its external artifacts (the run_004_remote
directory + chapter 0001 source HTML) exclusively from the environment
variables ``PACT_B12_RUN004_DIR`` and ``PACT_B12_CHAPTER_HTML`` and skips the
whole module when they are unavailable. These tests pin that resolution/skip
contract without needing the private artifacts (they run on every machine).
"""
from __future__ import annotations

import pytest

from tests.pact_v4.phase4 import test_b12_call_optimization_validation as b12val


def _skip_reasons() -> list[str]:
    marks = b12val.pytestmark
    markers = marks if isinstance(marks, list) else [marks]
    return [
        m.kwargs["reason"]
        for m in markers
        if getattr(m, "markname", "") == "skipif" and "reason" in m.kwargs
    ]


def test_resolution_returns_none_when_env_vars_unset(monkeypatch):
    monkeypatch.delenv(b12val.PACT_B12_RUN004_DIR_ENV, raising=False)
    monkeypatch.delenv(b12val.PACT_B12_CHAPTER_HTML_ENV, raising=False)
    assert b12val._resolve_external_paths() is None


def test_resolution_returns_none_when_paths_do_not_exist(monkeypatch, tmp_path):
    monkeypatch.setenv(b12val.PACT_B12_RUN004_DIR_ENV, str(tmp_path / "no-such-run"))
    monkeypatch.setenv(
        b12val.PACT_B12_CHAPTER_HTML_ENV, str(tmp_path / "no-such.html")
    )
    assert b12val._resolve_external_paths() is None


def test_resolution_returns_none_when_run_dir_is_a_file(monkeypatch, tmp_path):
    run_dir = tmp_path / "run_004_remote"
    run_dir.write_text("not a dir", encoding="utf-8")
    chapter = tmp_path / "0001_bonds-1-1.html"
    chapter.write_text("<html><body>source</body></html>", encoding="utf-8")
    monkeypatch.setenv(b12val.PACT_B12_RUN004_DIR_ENV, str(run_dir))
    monkeypatch.setenv(b12val.PACT_B12_CHAPTER_HTML_ENV, str(chapter))
    assert b12val._resolve_external_paths() is None


def test_resolution_returns_supplied_paths_when_they_exist(monkeypatch, tmp_path):
    run_dir = tmp_path / "run_004_remote"
    run_dir.mkdir()
    chapter = tmp_path / "0001_bonds-1-1.html"
    chapter.write_text("<html><body>source</body></html>", encoding="utf-8")
    monkeypatch.setenv(b12val.PACT_B12_RUN004_DIR_ENV, str(run_dir))
    monkeypatch.setenv(b12val.PACT_B12_CHAPTER_HTML_ENV, str(chapter))
    assert b12val._resolve_external_paths() == (run_dir, chapter)


def test_module_skip_reason_documents_the_env_vars():
    reasons = _skip_reasons()
    assert reasons, "module must carry a skipif marker with a reason"
    text = " ".join(reasons)
    assert b12val.PACT_B12_RUN004_DIR_ENV in text
    assert b12val.PACT_B12_CHAPTER_HTML_ENV in text
