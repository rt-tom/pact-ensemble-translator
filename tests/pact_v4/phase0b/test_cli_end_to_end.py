"""End-to-end: extract → build → validate → curate → report.

Uses only synthetic fixtures (see conftest.py). No book text.
"""
from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

from pact_v4.phase0b import cli


def _write_source_and_reference(
    tmp_path: Path, en_html: str, ru_xhtml: str,
) -> tuple[Path, Path]:
    src = tmp_path / "0044_subordination-6-1.html"
    src.write_text(en_html, encoding="utf-8")
    epub = tmp_path / "pact_ru.epub"
    with zipfile.ZipFile(epub, "w") as z:
        z.writestr("EPUB/chapter_044.xhtml", ru_xhtml.encode("utf-8"))
    return src, epub


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    rc = cli.main(argv)
    out = capsys.readouterr().out
    return rc, out


def test_extract_then_build_then_validate(
    tmp_path: Path,
    en_html: str,
    ru_xhtml: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    src, epub = _write_source_and_reference(tmp_path, en_html, ru_xhtml)
    out_dir = tmp_path / "gs"
    rc, out = _run([
        "extract",
        "--source-html", str(src),
        "--reference", str(epub),
        "--reference-entry", "EPUB/chapter_044.xhtml",
        "--chapter", "044",
        "--out-dir", str(out_dir),
    ], capsys)
    assert rc == 0
    assert (out_dir / "draft.json").exists()

    rc, out = _run([
        "build", "--in-dir", str(out_dir), "--max-count", "10",
    ], capsys)
    assert rc == 0
    records = json.loads((out_dir / "records.json").read_text(encoding="utf-8"))
    assert 1 <= len(records) <= 10
    for r in records:
        assert r["schema"] == "pact-v4-golden-record/v1"
        assert r["chapter"] == "044"
        assert r["verdict"]["status"] in {"unreviewed", "needs_review"}

    rc, out = _run([
        "validate", "--records", str(out_dir / "records.json"),
    ], capsys)
    assert rc == 0
    assert "OK" in out


def test_extract_epub_requires_entry(
    tmp_path: Path,
    en_html: str,
    ru_xhtml: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    src, epub = _write_source_and_reference(tmp_path, en_html, ru_xhtml)
    out_dir = tmp_path / "gs"
    rc = cli.main([
        "extract",
        "--source-html", str(src),
        "--reference", str(epub),
        "--chapter", "044",
        "--out-dir", str(out_dir),
    ])
    assert rc == 2


def test_extract_plain_xhtml_no_entry_needed(
    tmp_path: Path,
    en_html: str,
    ru_xhtml: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    src, _epub = _write_source_and_reference(tmp_path, en_html, ru_xhtml)
    ref = tmp_path / "chapter_044.xhtml"
    ref.write_text(ru_xhtml, encoding="utf-8")
    out_dir = tmp_path / "gs"
    rc, _ = _run([
        "extract",
        "--source-html", str(src),
        "--reference", str(ref),
        "--chapter", "044",
        "--out-dir", str(out_dir),
    ], capsys)
    assert rc == 0


def test_curate_script_updates_verdicts(
    tmp_path: Path,
    en_html: str,
    ru_xhtml: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    src, epub = _write_source_and_reference(tmp_path, en_html, ru_xhtml)
    out_dir = tmp_path / "gs"
    _run([
        "extract",
        "--source-html", str(src), "--reference", str(epub),
        "--reference-entry", "EPUB/chapter_044.xhtml",
        "--chapter", "044", "--out-dir", str(out_dir),
    ], capsys)
    _run([
        "build", "--in-dir", str(out_dir), "--max-count", "100",
    ], capsys)

    records_path = out_dir / "records.json"
    before = json.loads(records_path.read_text(encoding="utf-8"))
    assert len(before) >= 3
    # Script: accept, needs_review, reject, then quit.
    script = tmp_path / "actions.txt"
    script.write_text("a\nn\nr\nq\n", encoding="utf-8")
    rc, _out = _run([
        "curate", "--records", str(records_path),
        "--reviewer", "test",
        "--input", str(script),
    ], capsys)
    assert rc == 0

    after = json.loads(records_path.read_text(encoding="utf-8"))
    verdicts = [r["verdict"]["status"] for r in after[:3]]
    assert verdicts == ["accepted", "needs_review", "rejected"]
    for r in after[:3]:
        assert r["verdict"]["reviewer"] == "test"
        assert r["verdict"]["reviewed_at"]


def test_report_summarises(
    tmp_path: Path,
    en_html: str,
    ru_xhtml: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    src, epub = _write_source_and_reference(tmp_path, en_html, ru_xhtml)
    out_dir = tmp_path / "gs"
    _run([
        "extract",
        "--source-html", str(src), "--reference", str(epub),
        "--reference-entry", "EPUB/chapter_044.xhtml",
        "--chapter", "044", "--out-dir", str(out_dir),
    ], capsys)
    _run(["build", "--in-dir", str(out_dir)], capsys)
    rc, out = _run([
        "report", "--records", str(out_dir / "records.json"),
    ], capsys)
    assert rc == 0
    assert "total:" in out
    assert "verdict:" in out
    assert "risk:" in out
    assert "alignment method:" in out


def test_sample_prints_pids(
    tmp_path: Path,
    en_html: str,
    ru_xhtml: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    src, epub = _write_source_and_reference(tmp_path, en_html, ru_xhtml)
    out_dir = tmp_path / "gs"
    _run([
        "extract",
        "--source-html", str(src), "--reference", str(epub),
        "--reference-entry", "EPUB/chapter_044.xhtml",
        "--chapter", "044", "--out-dir", str(out_dir),
    ], capsys)
    rc, out = _run([
        "sample", "--in-dir", str(out_dir), "--max-count", "3",
    ], capsys)
    assert rc == 0
    lines = [line for line in out.splitlines() if line.strip()]
    assert 1 <= len(lines) <= 3
    for line in lines:
        assert line.startswith("p") and len(line) == 6
