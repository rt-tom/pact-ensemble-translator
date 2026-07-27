#!/usr/bin/env python3
"""Canonical chapter selection and immutable per-run source manifest."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


MANIFEST_SCHEMA = "pact-v31-chapter-manifest/v1"
_CHAPTER_PREFIX = re.compile(r"^0*(\d+)(?:[_.\- ]|$)")


class ChapterResolutionError(RuntimeError):
    """A source tree cannot safely be mapped to canonical chapter ids."""


def canonical_chapter_id(filename: str) -> str:
    """Return the numeric leading chapter id used by Pact source filenames."""
    match = _CHAPTER_PREFIX.match(Path(filename).stem)
    if not match:
        raise ChapterResolutionError(f"Ambiguous chapter filename (missing numeric prefix): {filename}")
    return str(int(match.group(1)))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_chapters(project_root: Path, input_dir: Path, start: int | None,
                     end: int | None) -> list[dict[str, str]]:
    project_root = project_root.resolve()
    input_dir = input_dir.resolve()
    indexed: dict[str, list[Path]] = {}
    ambiguous: list[str] = []
    for path in input_dir.glob("*.html"):
        try:
            chapter_id = canonical_chapter_id(path.name)
        except ChapterResolutionError:
            ambiguous.append(path.name)
            continue
        indexed.setdefault(chapter_id, []).append(path)
    if ambiguous:
        raise ChapterResolutionError("Ambiguous chapter filenames: " + ", ".join(sorted(ambiguous)))
    duplicates = {key: paths for key, paths in indexed.items() if len(paths) > 1}
    if duplicates:
        detail = "; ".join(f"{key}: {', '.join(sorted(p.name for p in paths))}" for key, paths in sorted(duplicates.items(), key=lambda item: int(item[0])))
        raise ChapterResolutionError(f"Duplicate canonical chapter ids: {detail}")
    if not indexed:
        raise ChapterResolutionError(f"No HTML chapters in {input_dir}")
    first = min(int(key) for key in indexed) if start is None else int(start)
    last = max(int(key) for key in indexed) if end is None else int(end)
    if first < 1 or last < first:
        raise ChapterResolutionError(f"Invalid chapter range {first}-{last}")
    missing = [str(number) for number in range(first, last + 1) if str(number) not in indexed]
    if missing:
        raise ChapterResolutionError(f"Missing canonical chapters in range {first}-{last}: {', '.join(missing)}")
    records: list[dict[str, str]] = []
    for number in range(first, last + 1):
        path = indexed[str(number)][0]
        try:
            source_path = path.resolve().relative_to(project_root).as_posix()
        except ValueError:
            source_path = path.resolve().as_posix()
        records.append({
            "chapter_id": str(number), "source_path": source_path,
            "filename": path.name, "source_sha256": sha256_file(path),
        })
    return records


def manifest_payload(project_root: Path, input_dir: Path, start: int | None,
                     end: int | None) -> dict[str, Any]:
    records = resolve_chapters(project_root, input_dir, start, end)
    return {
        "schema": MANIFEST_SCHEMA,
        "selection": {"start": int(records[0]["chapter_id"]), "end": int(records[-1]["chapter_id"])},
        "chapters": records,
    }


def create_or_verify_manifest(manifest_path: Path, project_root: Path, input_dir: Path,
                              start: int | None, end: int | None) -> dict[str, Any]:
    expected = manifest_payload(project_root, input_dir, start, end)
    if manifest_path.exists():
        try:
            actual = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ChapterResolutionError(f"Invalid run manifest {manifest_path}: {exc}") from exc
        if actual != expected:
            raise ChapterResolutionError(
                f"Run manifest differs from current chapter selection/source hashes: {manifest_path}. "
                "Resume is unsafe; create a new run rather than rewriting this manifest."
            )
        return actual
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(expected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return expected


def chapters_from_manifest(manifest_path: Path, project_root: Path, input_dir: Path,
                           start: int | None, end: int | None) -> list[Path]:
    manifest = create_or_verify_manifest(manifest_path, project_root, input_dir, start, end)
    return [(project_root / item["source_path"]).resolve() for item in manifest["chapters"]]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--start", type=int)
    parser.add_argument("--end", type=int)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    payload = create_or_verify_manifest(args.manifest, args.project_root, args.input_dir, args.start, args.end)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
