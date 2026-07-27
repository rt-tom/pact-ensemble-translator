#!/usr/bin/env python3
"""Offline tests for canonical Pact chapter selection and run manifests."""
from __future__ import annotations

import tempfile
from pathlib import Path

from v31_chapter_resolver import ChapterResolutionError, create_or_verify_manifest, resolve_chapters


def write(root: Path, name: str, content: str = "source") -> None:
    (root / name).write_text(content, encoding="utf-8")


def expect_error(fn, expected: str) -> None:
    try:
        fn()
    except ChapterResolutionError as exc:
        assert expected in str(exc), str(exc)
    else:
        raise AssertionError(f"Expected ChapterResolutionError containing {expected!r}")


def run() -> None:
    with tempfile.TemporaryDirectory(prefix="pact_chapter_resolver_") as temp:
        project = Path(temp)
        source = project / "pact_chapters"
        source.mkdir()
        write(source, "0001_prologue.html")
        write(source, "0002_a-strange.filename.html")
        write(source, "0112_execution-13-4.html", "chapter 112")
        records = resolve_chapters(project, source, 1, 2)
        assert [row["chapter_id"] for row in records] == ["1", "2"]
        assert records[1]["filename"] == "0002_a-strange.filename.html"
        assert records[0]["source_path"] == "pact_chapters/0001_prologue.html"
        assert len(resolve_chapters(project, source, 112, 112)) == 1
        expect_error(lambda: resolve_chapters(project, source, 1, 3), "Missing canonical chapters")
        manifest = project / "run" / "chapter_manifest.v31.json"
        first = create_or_verify_manifest(manifest, project, source, 1, 2)
        assert create_or_verify_manifest(manifest, project, source, 1, 2) == first
        write(source, "0002_a-strange.filename.html", "changed source")
        expect_error(lambda: create_or_verify_manifest(manifest, project, source, 1, 2), "Resume is unsafe")
    with tempfile.TemporaryDirectory(prefix="pact_chapter_duplicates_") as temp:
        project = Path(temp)
        source = project / "pact_chapters"
        source.mkdir()
        write(source, "0001_one.html")
        write(source, "1_duplicate.html")
        expect_error(lambda: resolve_chapters(project, source, 1, 1), "Duplicate canonical chapter ids")
        (source / "1_duplicate.html").unlink()
        write(source, "appendix.html")
        expect_error(lambda: resolve_chapters(project, source, 1, 1), "Ambiguous chapter filenames")
    print("chapter resolver self-test: PASS")


if __name__ == "__main__":
    run()
