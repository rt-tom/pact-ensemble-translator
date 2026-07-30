#!/usr/bin/env python3
"""Prepare isolated, operator-run Track A cells for V4 Phase 0C.

This tool never starts a model or pipeline.  It snapshots mutable supporting
state into four cell directories, writes cell-specific configs, and emits a
PowerShell command file that the operator may run explicitly.
"""
from __future__ import annotations

import argparse
import copy
import json
import shutil
from pathlib import Path
from typing import Any

from v4_phase0c_baseline import GRID_CONFIG, grid_cell_id


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise ValueError(f"required file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _assert_new_or_empty(path: Path) -> None:
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ValueError(
            f"bench root must be new or empty; refusing to overwrite caches: {path}"
        )


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def prepare_gate_bench(
    project_root: Path,
    source_root: Path,
    bench_root: Path,
    base_config: Path | None = None,
) -> list[Path]:
    project_root = project_root.resolve()
    source_root = source_root.resolve()
    bench_root = bench_root.resolve()
    config_path = (base_config or (source_root / "config.v3.json")).resolve()
    translator = project_root / "pact_translate_v3.py"
    required = [
        translator,
        config_path,
        source_root / "pact_chapters",
        source_root / "glossary",
        source_root / "book_bible.json",
        source_root / "arc_names.json",
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise ValueError("required Gate Bench inputs missing: " + ", ".join(missing))
    if bench_root in (project_root, source_root):
        raise ValueError("bench root must not be the project or source root")
    _assert_new_or_empty(bench_root)
    bench_root.mkdir(parents=True, exist_ok=True)

    base = _load_object(config_path)
    configs: list[Path] = []
    commands: list[str] = [
        "$ErrorActionPreference = 'Stop'",
        f"Set-Location -LiteralPath '{project_root}'",
        "",
    ]
    for (chunk_size, rc), overrides in GRID_CONFIG.items():
        cell_id = grid_cell_id(chunk_size, rc)
        cell_root = bench_root / cell_id
        for name in ("work", "output", "logs"):
            (cell_root / name).mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_root / "glossary", cell_root / "glossary")
        shutil.copy2(source_root / "book_bible.json", cell_root / "book_bible.json")
        shutil.copy2(source_root / "arc_names.json", cell_root / "arc_names.json")

        cfg = copy.deepcopy(base)
        paths = cfg.setdefault("paths", {})
        paths.update({
            "input_dir": str((source_root / "pact_chapters").resolve()),
            "output_dir": str((cell_root / "output").resolve()),
            "work_dir": str((cell_root / "work").resolve()),
            "logs_dir": str((cell_root / "logs").resolve()),
            "glossary_dir": str((cell_root / "glossary").resolve()),
            "book_bible_file": str((cell_root / "book_bible.json").resolve()),
            "arc_names_file": str((cell_root / "arc_names.json").resolve()),
            "run_glossary_candidate_ledger": str((cell_root / "glossary_candidates.run.json").resolve()),
            "book_glossary_candidate_ledger": str((cell_root / "glossary_candidates.book.json").resolve()),
        })
        cfg.setdefault("chunking", {}).update(overrides["chunking"])
        cell_config = cell_root / "config.json"
        _write_json(cell_config, cfg)
        configs.append(cell_config)
        commands.extend([
            f"py .\\pact_translate_v3.py --config '{cell_config}' --phase translate --start 46 --end 46",
            "if ($LASTEXITCODE -ne 0) { throw 'Track A cell failed' }",
            "",
        ])

    (bench_root / "run_track_a.ps1").write_text(
        "\n".join(commands), encoding="utf-8"
    )
    _write_json(bench_root / "bench_manifest.json", {
        "schema": "pact-v4-phase0c-gate-bench/v1",
        "project_root": str(project_root),
        "source_root": str(source_root),
        "cells": [path.parent.name for path in configs],
    })
    return configs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="prepare isolated V4 Phase 0C Track A cells")
    parser.add_argument("--project-root", type=Path, required=True,
                        help="development worktree containing pact_translate_v3.py")
    parser.add_argument("--source-root", type=Path, required=True,
                        help="read-only root containing config, chapters, glossary and bible")
    parser.add_argument("--bench-root", type=Path, required=True,
                        help="new or empty output directory for four isolated cells")
    parser.add_argument("--base-config", type=Path,
                        help="optional base config; defaults to SOURCE_ROOT/config.v3.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configs = prepare_gate_bench(
        args.project_root, args.source_root, args.bench_root, args.base_config
    )
    print(f"prepared {len(configs)} Track A cells in {args.bench_root.resolve()}")
    print(f"operator command file: {(args.bench_root / 'run_track_a.ps1').resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
