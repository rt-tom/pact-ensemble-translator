#!/usr/bin/env python3
"""Offline regression tests for the v3.1.3 glossary candidate ledger."""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
import tempfile
from pathlib import Path


HERE = Path(__file__).resolve().parent


def load_runtime(project: Path):
    path = project / "pact_translate_v3.py"
    spec = importlib.util.spec_from_file_location("pact_v3_ledger_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def candidate(target: str, *, pid: str = "p00001", confidence="low"):
    return {
        "english": "Mara", "russian": target, "type": "character",
        "source_pids": [pid], "evidence": "Mara entered.",
        "alternatives": ["Марая"], "confidence": confidence, "model": "test-model",
    }


def run(project: Path) -> None:
    runtime = load_runtime(project)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        run_path, book_path = root / "run.json", root / "book.json"
        ledger = runtime.GlossaryCandidateLedger(run_path, book_path)
        first = ledger.observe_chapter("01.xhtml", "Mara met Mara.", [candidate("Мара")],
                                       stage="chapter_bible", detector="translator")
        assert first["promoted"] == 0 and first["observations"] == 1
        # Identical evidence is idempotent in provenance but remains a repeated sighting.
        ledger.observe_chapter("01.xhtml", "Mara met Mara.", [candidate("Мара")],
                               stage="chapter_bible", detector="translator")
        data = json.loads(run_path.read_text(encoding="utf-8"))
        record = next(iter(data["candidates"].values()))
        assert record["status"] == "candidate"
        assert len(record["observations"]) == 1
        assert record["proposals"]["Мара"]["sightings"] == 2
        observation = record["observations"][0]
        assert observation["confidence"] == "low"
        assert observation["provenance"] == {
            "chapter": "01.xhtml", "pids": ["p00001"], "stage": "chapter_bible",
            "detector": "translator", "model": "test-model", "evidence": "Mara entered.",
            "occurrences": 2,
        }
        # A competing target is retained, not selected or promoted.
        ledger.observe_chapter("02.xhtml", "Mara.", [candidate("Мэрa", pid="p00002")],
                               stage="chapter_bible", detector="translator")
        data = json.loads(book_path.read_text(encoding="utf-8"))
        record = next(iter(data["candidates"].values()))
        assert set(record["proposals"]) == {"Мара", "Мэрa"}
        assert {item["provenance"]["pids"][0] for item in record["observations"]} == {"p00001", "p00002"}
        assert runtime.GlossaryCandidateLedger.candidate_id("Mara", "character") == record["candidate_id"]

        # Atomic write failures leave the previous complete JSON visible.
        before = run_path.read_text(encoding="utf-8")
        original_replace = runtime.os.replace
        runtime.os.replace = lambda _source, _target: (_ for _ in ()).throw(OSError("simulated crash"))
        try:
            try:
                runtime.atomic_json(run_path, {"broken": True})
            except OSError:
                pass
        finally:
            runtime.os.replace = original_replace
        assert run_path.read_text(encoding="utf-8") == before

        glossary = root / "glossary"
        glossary.mkdir()
        for name in ("locked.json", "established.json", "provisional.json", "conflicts.json"):
            (glossary / name).write_text("{}", encoding="utf-8")
        snapshot = {path.name: path.read_bytes() for path in glossary.iterdir()}
        cfg = runtime.merge(runtime.DEFAULTS, {"paths": {
            "glossary_dir": str(glossary), "run_glossary_candidate_ledger": str(root / "run2.json"),
            "book_glossary_candidate_ledger": str(root / "book2.json"),
        }})
        runtime.Glossary(cfg).update("03.xhtml", "Mara.", [candidate("Мара")])
        assert snapshot == {path.name: path.read_bytes() for path in glossary.iterdir()}


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=HERE.parent)
    args = parser.parse_args()
    run(args.project_root.resolve())
    print("Glossary candidate ledger self-tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
