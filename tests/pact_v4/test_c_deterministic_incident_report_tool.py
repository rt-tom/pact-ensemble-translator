"""Smoke test for the portable card-C incident-report tool.

The tool (``tools/c_deterministic_incident_report.py``) must be a portable
CLI: argparse interface for the source/independent/output paths, no
machine- or worktree-specific paths, a ``main()`` guard (importing the
module performs no work), and it must generate the same report
shape/counts as ``run_formatting_align`` on the same inputs.

The tool is exercised with temporary fixtures only (never real
artifacts / machine paths).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TOOLS = REPO / "tools"
TOOL_PATH = TOOLS / "c_deterministic_incident_report.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "c_deterministic_incident_report", TOOL_PATH,
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def tool():
    return _load_tool()


@pytest.fixture()
def fixtures(tmp_path):
    """Tiny chapter 0001-style fixtures: 1 <p> block, 2 inline spans."""
    chapter = tmp_path / "chapter.html"
    chapter.write_text(
        "<html><body><p>In <em>1947</em> we met <strong>them</strong>.</p>"
        "</body></html>",
        encoding="utf-8",
    )
    independent = tmp_path / "independent.html"
    independent.write_text(
        "<html><body><p>В <em>1947</em> году мы встретили <strong>их</strong>."
        "</p></body></html>",
        encoding="utf-8",
    )
    out = tmp_path / "report.md"
    return chapter, independent, out


def test_tool_generates_report_from_temp_fixtures(tool, fixtures, tmp_path):
    chapter, independent, out = fixtures
    rc = tool.main(["--chapter-html", str(chapter), "--independent-html",
                    str(independent), "--out", str(out)])
    assert rc == 0
    assert out.is_file()
    text = out.read_text(encoding="utf-8")
    # Report shape/counts mirror run_formatting_align on the same inputs:
    # both spans resolve via the preserved tier, 0 model calls.
    assert "resolved: **2** / 2 (100.0%)" in text
    assert "incidents (unresolved, blocking, debt): **0**" in text
    assert "model_call_count: **0**" in text
    assert "model_fallback_count: **0**" in text
    assert "тиры: `{'preserved': 2}`" in text


def test_tool_cli_requires_all_three_paths(tool):
    # argparse contract: all three paths are required.
    with pytest.raises(SystemExit):
        tool.main([])
    with pytest.raises(SystemExit):
        tool.main(["--chapter-html", "a.html", "--independent-html", "b.html"])


def test_tool_import_performs_no_work(tmp_path):
    # Importing the module must not write any report (main() guard).
    before = set(tmp_path.iterdir())
    _load_tool()
    after = set(tmp_path.iterdir())
    assert before == after


def test_tool_has_no_hardcoded_machine_paths():
    source = TOOL_PATH.read_text(encoding="utf-8")
    # AGENTS.md: no new machine-specific paths; the repo root is resolved
    # from __file__, never from a D:/ worktree literal.
    assert "D:/" not in source and "D:\\" not in source
    assert ".worktrees" not in source
    assert "__file__" in source
    assert "if __name__ == \"__main__\":" in source
