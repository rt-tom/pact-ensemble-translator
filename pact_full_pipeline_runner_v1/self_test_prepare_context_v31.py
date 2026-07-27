#!/usr/bin/env python3
"""Offline regression coverage for model-free preparation resume."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
sys.path.insert(0, str(PROJECT))

import pact_translate_v3 as runtime
from v31_common import ARTIFACT_VERSION


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def run() -> None:
    with tempfile.TemporaryDirectory(prefix="pact-prepare-") as temp:
        root = Path(temp)
        source_dir, work_dir, glossary_dir = root / "input", root / "work", root / "glossary"
        source_dir.mkdir()
        source = source_dir / "0001_fixture.html"
        source.write_text("<p>One short deterministic fixture.</p>", encoding="utf-8")
        for name in ("locked.json", "established.json", "provisional.json", "conflicts.json"):
            write_json(glossary_dir / name, {})
        write_json(root / "book_bible.json", {})
        cfg = runtime.merge(runtime.DEFAULTS, {
            "paths": {"input_dir": str(source_dir), "work_dir": str(work_dir),
                      "logs_dir": str(root / "logs"), "glossary_dir": str(glossary_dir),
                      "book_bible_file": str(root / "book_bible.json"),
                      "run_glossary_candidate_ledger": str(root / "run-ledger.json"),
                      "book_glossary_candidate_ledger": str(root / "book-ledger.json")},
            "translator_api": {"base_url": "http://127.0.0.1:1/v1/chat/completions",
                               "token_count_url": "http://127.0.0.1:1/input_tokens"},
        })
        runner = runtime.Runner(cfg)
        work, _, blocks, chunks, _ = runner.prepare_chapter(
            source, False, manifest_version=ARTIFACT_VERSION
        )
        manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["version"] == ARTIFACT_VERSION
        assert manifest["source_sha256"]
        original_block_ids = [block.pid for block in blocks]
        write_json(work / "chapter_bible.json", {"terms": [], "facts": []})
        original_token_count = runner.translator.token_count
        runner.translator.token_count = lambda *_: (_ for _ in ()).throw(AssertionError("hidden HTTP/token count"))
        resumed_work, _, resumed_blocks, resumed_chunks, _ = runner.prepare_chapter(
            source, False, manifest_version=ARTIFACT_VERSION
        )
        assert resumed_work == work and len(resumed_blocks) == len(blocks)
        assert [block.pid for block in resumed_blocks] == original_block_ids
        assert [item.__dict__ for item in resumed_chunks] == [item.__dict__ for item in chunks]
        runner.translator.token_count = original_token_count
        manifest["version"] = "3.1.2d"
        write_json(work / "manifest.json", manifest)
        try:
            runner.prepare_chapter(source, False, manifest_version=ARTIFACT_VERSION)
        except runtime.PipelineError as exc:
            assert "incompatible" in str(exc) and "create a new run" in str(exc)
        else:
            raise AssertionError("stale work manifest was silently reused")


if __name__ == "__main__":
    run()
    print("Pact v3.1 prepare-context self-tests passed")
