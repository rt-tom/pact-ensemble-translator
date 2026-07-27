#!/usr/bin/env python3
"""Generate chapter bible, freeze glossary, and enforce known glossary targets."""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

from v31_common import ARTIFACT_VERSION

VERSION = "1.1.0"


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return copy.deepcopy(default)
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def target(record: Any) -> str | None:
    if isinstance(record, str):
        return record
    if isinstance(record, dict) and isinstance(record.get("target"), str):
        return record["target"]
    return None


LATIN_RE = re.compile(r"[A-Za-z]")
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")


def has_latin(value: str) -> bool:
    return bool(LATIN_RE.search(value or ""))


def has_cyrillic(value: str) -> bool:
    return bool(CYRILLIC_RE.search(value or ""))


def known_lookup(known: dict[str, str], source: str) -> str | None:
    if source in known:
        return known[source]
    folded = source.casefold()
    for key, value in known.items():
        if key.casefold() == folded:
            return value
    return None


def latin_placeholder(source: str, proposed: str) -> bool:
    proposed = proposed.strip()
    if not proposed or not has_latin(proposed):
        return False
    if has_cyrillic(proposed):
        return False
    # A Russian-target bible must not normalize an unchanged/Latin-only value.
    return proposed.casefold() == source.strip().casefold() or has_latin(source)


def load_known(directory: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for filename in ("locked.json", "established.json", "provisional.json"):
        data = read_json(directory / filename, {})
        for source, record in data.items():
            value = target(record)
            if value and source not in result:
                result[source] = value
    return result


def sanitize_bible(bible: dict[str, Any], known: dict[str, str]) -> dict[str, Any]:
    result = copy.deepcopy(bible)
    changes: list[dict[str, str]] = []
    for section in ("characters", "entities", "terms"):
        items = result.get(section) or []
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source") or item.get("english") or "").strip()
            if not source:
                continue
            expected = known_lookup(known, source)
            field = "russian" if "russian" in item and "target" not in item else "target"
            old = str(item.get(field) or "").strip()
            if expected:
                if old != expected:
                    changes.append({
                        "section": section, "source": source,
                        "old": old, "new": expected,
                        "action": "glossary_override",
                    })
                item[field] = expected
                if old and old != expected:
                    forbidden = item.setdefault("forbidden_targets", [])
                    if old not in forbidden:
                        forbidden.append(old)
            elif latin_placeholder(source, old):
                changes.append({
                    "section": section, "source": source,
                    "old": old, "new": "",
                    "action": "remove_latin_placeholder",
                })
                item[field] = ""
                forbidden = item.setdefault("forbidden_targets", [])
                if old and old not in forbidden:
                    forbidden.append(old)
                notes = str(item.get("notes") or "").strip()
                warning = (
                    "Russian target was unknown; Latin-only placeholder removed. "
                    "Choose a Cyrillic translation/transliteration during translation."
                )
                item["notes"] = f"{notes} {warning}".strip()
    pov = result.get("pov")
    if isinstance(pov, dict):
        source_name = str(pov.get("source_name") or "").strip()
        expected = known_lookup(known, source_name) if source_name else None
        old = str(pov.get("target_name") or "").strip()
        if expected:
            pov["target_name"] = expected
        elif source_name and latin_placeholder(source_name, old):
            pov["target_name"] = ""
    result["glossary_enforcement"] = {
        "enabled": True,
        "changes": changes,
        "rule": (
            "Known glossary targets override model proposals; Latin-only "
            "Russian-target placeholders are removed instead of becoming norms."
        ),
    }
    return result


def sanitize_book_bible(path: Path, known: dict[str, str]) -> None:
    data = read_json(path, {})
    for section in ("characters", "entities"):
        collection = data.get(section) or {}
        if not isinstance(collection, dict):
            continue
        for source, record in collection.items():
            if not isinstance(record, dict):
                continue
            expected = known_lookup(known, source)
            old = str(record.get("target") or "").strip()
            if expected:
                record["target"] = expected
                if old and old != expected:
                    forbidden = record.setdefault("forbidden_targets", [])
                    if old not in forbidden:
                        forbidden.append(old)
            elif latin_placeholder(source, old):
                record["target"] = ""
                forbidden = record.setdefault("forbidden_targets", [])
                if old and old not in forbidden:
                    forbidden.append(old)
            variants = record.get("variants")
            if isinstance(variants, dict):
                record["variants"] = {
                    value: count for value, count in variants.items()
                    if not latin_placeholder(source, str(value))
                }
    write_json(path, data)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--start", type=int)
    parser.add_argument("--end", type=int)
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args()
    if args.version:
        print(f"prepare_pipeline_context.py {VERSION}")
        return 0
    if args.project_root is None or args.config is None or args.start is None or args.end is None:
        parser.error("--project-root, --config, --start and --end are required")

    project = args.project_root.resolve()
    module_path = project / "pact_translate_v3.py"
    if not module_path.exists():
        raise FileNotFoundError(module_path)
    spec = importlib.util.spec_from_file_location("pact_v3_runtime", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not import pact_translate_v3.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    config_path = args.config.resolve()
    cfg = module.resolve_paths(
        module.merge(module.DEFAULTS, module.read_json(config_path, {})), config_path
    )
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

    glossary_dir = Path(cfg["paths"]["glossary_dir"])
    known = load_known(glossary_dir)
    files = module.select_files(cfg, args.start, args.end)
    runner = module.Runner(cfg)

    for source_path in files:
        # The generic runtime predates v3.1 and carries its own release label.
        # v3.1 work manifests are cache-identity artifacts, so they must use the
        # single semantic version shared by every v3.1 producer.
        work, _, blocks, _, _ = runner.prepare_chapter(
            source_path, False, manifest_version=ARTIFACT_VERSION
        )
        bible_path = work / "chapter_bible.json"
        if bible_path.exists() and not (work / "chapter_bible.raw.json").exists():
            # Existing bible may be from an interrupted run. Preserve then sanitize.
            write_json(work / "chapter_bible.raw.json", read_json(bible_path, {}))
        if not bible_path.exists():
            bible = runner.get_chapter_bible(source_path, work, blocks)
            write_json(work / "chapter_bible.raw.json", bible)
        else:
            bible = read_json(bible_path, {})
        sanitized = sanitize_bible(bible, known)
        write_json(bible_path, sanitized)
        sanitize_book_bible(Path(cfg["paths"]["book_bible_file"]), known)
        print(
            f"Prepared {source_path.name}: glossary overrides="
            f"{len((sanitized.get('glossary_enforcement') or {}).get('changes', []))}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
