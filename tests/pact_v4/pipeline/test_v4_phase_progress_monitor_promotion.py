"""Focused tests for monitor-output-correctness new surfaces."""
from __future__ import annotations

import json
from pathlib import Path

from pact_full_pipeline_runner_v1 import v4_phase_progress as tracker
from pact_v4.pipeline.phase_progress import PHASE_PROGRESS_FILENAME


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_ndjson(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _make_chapter(tmp_path: Path, name: str = "chapter_0001_bonds-1-1") -> Path:
    out = tmp_path / name
    out.mkdir(parents=True, exist_ok=True)
    _write_ndjson(out / PHASE_PROGRESS_FILENAME, [
        {"schema": "pact-v4-phase-progress/ndjson/v1", "event": "run_started",
         "ts": "2026-08-27T06:36:41+00:00", "started_at": "2026-08-27T06:36:41+00:00",
         "chapter_id": "0001_bonds-1-1"},
    ])
    return out


# ---------------------------------------------------------------------------
# _phase_glossary
# ---------------------------------------------------------------------------

def test_phase_glossary_present(tmp_path: Path):
    out = _make_chapter(tmp_path)
    _write(out / "glossary_proposals.json", {"proposals": [{"a": 1}, {"a": 2}, {"a": 3}]})
    report = tracker.render_report(out)
    assert "Glossary: 3 proposals" in report


def test_phase_glossary_absent(tmp_path: Path):
    out = _make_chapter(tmp_path)
    report = tracker.render_report(out)
    assert "Glossary" not in report


# ---------------------------------------------------------------------------
# _phase_formatting
# ---------------------------------------------------------------------------

def test_phase_formatting_present(tmp_path: Path):
    out = _make_chapter(tmp_path)
    _write(out / "formatting_report.json", {"resolved_count": 5, "incident_count": 1})
    report = tracker.render_report(out)
    assert "Formatting: spans 5/6 \u00b7 incidents 1" in report


def test_phase_formatting_absent(tmp_path: Path):
    out = _make_chapter(tmp_path)
    report = tracker.render_report(out)
    assert "Formatting" not in report


# ---------------------------------------------------------------------------
# _book_promotion_summary / render_book_report (book_run.json per-run)
# ---------------------------------------------------------------------------

def test_book_promotion_from_book_run_json(tmp_path: Path):
    base = tmp_path / "book"
    base.mkdir()
    _make_chapter(base, "chapter_0001_bonds-1-1")
    mem = tmp_path / "mem"
    mem.mkdir()
    _write(base / "book_run.json", {
        "schema": "pact-v4-book-run/v1",
        "memory_dir": str(mem),
        "chapters": [{
            "chapter_id": "0001_bonds-1-1",
            "promoted": True,
            "candidates": {"generated": 2, "proposed": 2, "committed": 2, "conflicts": 0},
            "book_memory_candidates": {"generated": 1, "proposed": 1, "committed": 1, "conflicts": 0},
            "book_memory_promotions": [{"name": "Alice"}],
        }],
    })
    report = tracker.render_book_report(base, memory_dir=mem)
    assert "Promoted this run: glossary committed 2 \u00b7 memory committed 1 \u00b7 memory promotions 1 (1 chapters)" in report


def test_book_promotion_none_committed(tmp_path: Path):
    base = tmp_path / "book"
    base.mkdir()
    _make_chapter(base, "chapter_0001_bonds-1-1")
    mem = tmp_path / "mem"
    mem.mkdir()
    _write(base / "book_run.json", {
        "schema": "pact-v4-book-run/v1",
        "memory_dir": str(mem),
        "chapters": [{
            "chapter_id": "0001_bonds-1-1",
            "promoted": True,
            "candidates": {"generated": 0, "proposed": 0, "committed": 0, "conflicts": 0},
            "book_memory_candidates": {"generated": 0, "proposed": 0, "committed": 0, "conflicts": 0},
            "book_memory_promotions": [],
        }],
    })
    report = tracker.render_book_report(base, memory_dir=mem)
    assert "Promoted this run: none (0 glossary / 0 memory)" in report


def test_book_promotion_empty_chapters(tmp_path: Path):
    base = tmp_path / "book"
    base.mkdir()
    _make_chapter(base, "chapter_0001_bonds-1-1")
    mem = tmp_path / "mem"
    mem.mkdir()
    # present book_run.json with empty chapters must NOT fall back to state totals
    _write(base / "book_run.json", {
        "schema": "pact-v4-book-run/v1",
        "chapters": [],
    })
    _write(mem / "glossary.json", {f"k{i}": i for i in range(5)})
    _write(mem / "book_memory.json", {f"k{i}": i for i in range(2)})
    report = tracker.render_book_report(base, memory_dir=mem)
    assert "Promoted this run: none (0 glossary / 0 memory)" in report
    assert "State glossary/memory" not in report


def test_book_promotion_pending_no_book_run_json(tmp_path: Path):
    base = tmp_path / "book"
    base.mkdir()
    _make_chapter(base, "chapter_0001_bonds-1-1")
    mem = tmp_path / "mem"
    mem.mkdir()
    _write(mem / "glossary.json", {"a": 1, "b": 2, "c": 3})
    _write(mem / "book_memory.json", {"k1": 1, "k2": 2})
    report = tracker.render_book_report(base, memory_dir=mem)
    assert "State glossary/memory: 3 / 2 (input+promoted; book_run.json pending)" in report


def test_book_promotion_no_data(tmp_path: Path):
    base = tmp_path / "book"
    base.mkdir()
    _make_chapter(base, "chapter_0001_bonds-1-1")
    mem = tmp_path / "mem2"
    mem.mkdir()
    report = tracker.render_book_report(base, memory_dir=mem)
    assert "Glossary promoted" not in report
    assert "Promoted this run" not in report
    assert "State glossary/memory" not in report
    # also with memory_dir=None
    report2 = tracker.render_book_report(base, memory_dir=None)
    assert "Glossary promoted" not in report2
    assert "Promoted this run" not in report2
    assert "State glossary/memory" not in report2


# ---------------------------------------------------------------------------
# _resolve_state_root
# ---------------------------------------------------------------------------

def test_resolve_state_root_env(monkeypatch):
    monkeypatch.setenv("PACT_V4_STATE_ROOT", "/tmp/custom_state")
    # ensure HOST not interfering
    monkeypatch.delenv("PACT_V4_HOST", raising=False)
    monkeypatch.delenv("PACT_EXEC_HOST", raising=False)
    assert tracker._resolve_state_root() == Path("/tmp/custom_state")


def test_resolve_state_root_host_rt(monkeypatch):
    monkeypatch.delenv("PACT_V4_STATE_ROOT", raising=False)
    monkeypatch.delenv("PACT_EXEC_HOST", raising=False)
    monkeypatch.setenv("PACT_V4_HOST", "rt")
    assert tracker._resolve_state_root() == Path("D:/pact/book_state")


def test_resolve_state_root_win32(monkeypatch):
    monkeypatch.delenv("PACT_V4_STATE_ROOT", raising=False)
    monkeypatch.delenv("PACT_V4_HOST", raising=False)
    monkeypatch.delenv("PACT_EXEC_HOST", raising=False)
    # fallback: directly patch sys.platform via monkeypatch
    import sys as _sys
    monkeypatch.setattr(_sys, "platform", "win32", raising=False)
    assert tracker._resolve_state_root() == Path("D:/pact/book_state")


def test_resolve_state_root_default(monkeypatch):
    monkeypatch.delenv("PACT_V4_STATE_ROOT", raising=False)
    monkeypatch.delenv("PACT_V4_HOST", raising=False)
    monkeypatch.delenv("PACT_EXEC_HOST", raising=False)
    import sys as _sys
    monkeypatch.setattr(_sys, "platform", "linux", raising=False)
    assert tracker._resolve_state_root() == Path("/home/rt/pact_runs/workers/media/book-1/state")


def test_resolve_state_root_exec_host_precedence(monkeypatch):
    """PACT_EXEC_HOST mirrors run host selection with PACT_V4_HOST precedence."""
    import sys as _sys

    # PACT_EXEC_HOST=rt (PACT_V4_HOST unset, non-win32) -> RT dir
    monkeypatch.delenv("PACT_V4_STATE_ROOT", raising=False)
    monkeypatch.delenv("PACT_V4_HOST", raising=False)
    monkeypatch.delenv("PACT_EXEC_HOST", raising=False)
    monkeypatch.setattr(_sys, "platform", "linux", raising=False)
    monkeypatch.setenv("PACT_EXEC_HOST", "rt")
    assert tracker._resolve_state_root() == Path("D:/pact/book_state")

    # PACT_EXEC_HOST=media -> media dir
    monkeypatch.delenv("PACT_V4_STATE_ROOT", raising=False)
    monkeypatch.delenv("PACT_V4_HOST", raising=False)
    monkeypatch.setenv("PACT_EXEC_HOST", "media")
    monkeypatch.setattr(_sys, "platform", "linux", raising=False)
    assert tracker._resolve_state_root() == Path("/home/rt/pact_runs/workers/media/book-1/state")

    # PACT_V4_HOST=rt takes precedence over PACT_EXEC_HOST=media -> RT dir
    monkeypatch.delenv("PACT_V4_STATE_ROOT", raising=False)
    monkeypatch.setenv("PACT_V4_HOST", "rt")
    monkeypatch.setenv("PACT_EXEC_HOST", "media")
    monkeypatch.setattr(_sys, "platform", "linux", raising=False)
    assert tracker._resolve_state_root() == Path("D:/pact/book_state")

    # PACT_V4_STATE_ROOT set -> returns that path regardless of host envs
    monkeypatch.setenv("PACT_V4_STATE_ROOT", "/tmp/custom_state_exec")
    monkeypatch.setenv("PACT_V4_HOST", "media")
    monkeypatch.setenv("PACT_EXEC_HOST", "rt")
    assert tracker._resolve_state_root() == Path("/tmp/custom_state_exec")


# ---------------------------------------------------------------------------
# --memory-dir CLI
# ---------------------------------------------------------------------------

def test_memory_dir_cli_present(tmp_path: Path):
    base = tmp_path / "book"
    base.mkdir()
    mem = tmp_path / "mem"
    mem.mkdir()
    args = tracker.build_argparser().parse_args(["--out-base", str(base), "--memory-dir", str(mem)])
    assert Path(args.memory_dir) == mem


def test_memory_dir_cli_default(tmp_path: Path):
    base = tmp_path / "book"
    base.mkdir()
    args = tracker.build_argparser().parse_args(["--out-base", str(base)])
    assert args.memory_dir is None


# ---------------------------------------------------------------------------
# Promotion debt (book-memory-boundary-allowlist)
# ---------------------------------------------------------------------------

def test_promotion_debt_rendered(tmp_path: Path):
    base = tmp_path / "book"
    base.mkdir()
    _make_chapter(base, "chapter_0001_bonds-1-1")
    mem = tmp_path / "mem"
    mem.mkdir()
    _write(base / "book_run.json", {
        "schema": "pact-v4-book-run/v1",
        "memory_dir": str(mem),
        "chapters": [{
            "chapter_id": "0001_bonds-1-1",
            "promoted": False,
            "promote_detail": "promotion / push to Media not completed: boom",
            "promotion_error": "exact-four-file boundary violation before promotion: extra entry 'rogue.txt'",
            "candidates": {"generated": 0, "proposed": 0, "committed": 0, "conflicts": 0},
            "book_memory_candidates": {"generated": 0, "proposed": 0, "committed": 0, "conflicts": 0},
            "book_memory_promotions": [],
        }],
    })
    report = tracker.render_book_report(base, memory_dir=mem)
    assert "Promotion debt:" in report
    assert "extra entry" in report


def test_promotion_debt_truncated(tmp_path: Path):
    base = tmp_path / "book"
    base.mkdir()
    _make_chapter(base, "chapter_0001_bonds-1-1")
    mem = tmp_path / "mem"
    mem.mkdir()
    long_err = "x" * 500
    _write(base / "book_run.json", {
        "schema": "pact-v4-book-run/v1",
        "chapters": [{
            "chapter_id": "0001",
            "promoted": False,
            "promotion_error": long_err,
            "candidates": {"generated": 0, "proposed": 0, "committed": 0, "conflicts": 0},
            "book_memory_candidates": {"generated": 0, "proposed": 0, "committed": 0, "conflicts": 0},
            "book_memory_promotions": [],
        }],
    })
    report = tracker.render_book_report(base, memory_dir=mem)
    assert "Promotion debt:" in report
    # truncated: full 500-char error must not appear verbatim
    assert long_err not in report
    assert "\u2026" in report or "..." in report


def test_no_promotion_debt_when_success(tmp_path: Path):
    base = tmp_path / "book"
    base.mkdir()
    _make_chapter(base, "chapter_0001_bonds-1-1")
    mem = tmp_path / "mem"
    mem.mkdir()
    _write(base / "book_run.json", {
        "schema": "pact-v4-book-run/v1",
        "chapters": [{
            "chapter_id": "0001",
            "promoted": True,
            "promotion_error": None,
            "candidates": {"generated": 0, "proposed": 0, "committed": 0, "conflicts": 0},
            "book_memory_candidates": {"generated": 0, "proposed": 0, "committed": 0, "conflicts": 0},
            "book_memory_promotions": [],
        }],
    })
    report = tracker.render_book_report(base, memory_dir=mem)
    assert "Promotion debt:" not in report
