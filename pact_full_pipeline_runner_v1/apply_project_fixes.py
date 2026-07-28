#!/usr/bin/env python3
"""Merge glossary additions, sanitize existing bibles, and update safe defaults."""
from __future__ import annotations

import argparse
import copy
import json
import re
from pathlib import Path
from typing import Any

VERSION = "1.1.0"
LATIN_RE = re.compile(r"[A-Za-z]")
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")


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


def load_known(directory: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for filename in ("locked.json", "established.json", "provisional.json"):
        data = read_json(directory / filename, {})
        if not isinstance(data, dict):
            continue
        for source, record in data.items():
            value = target(record)
            if value and source not in result:
                result[source] = value
    return result


def lookup(known: dict[str, str], source: str) -> str | None:
    if source in known:
        return known[source]
    folded = source.casefold()
    for key, value in known.items():
        if key.casefold() == folded:
            return value
    return None


def latin_placeholder(source: str, proposed: str) -> bool:
    proposed = proposed.strip()
    return bool(
        proposed
        and LATIN_RE.search(proposed)
        and not CYRILLIC_RE.search(proposed)
        and LATIN_RE.search(source)
    )


def merge_locked(glossary_dir: Path, additions: dict[str, str]) -> int:
    path = glossary_dir / "locked.json"
    if not glossary_dir.exists() or not path.exists():
        return 0
    data = read_json(path, {})
    changes = 0
    for source, russian in additions.items():
        if data.get(source) != russian:
            data[source] = russian
            changes += 1
    data = dict(sorted(data.items(), key=lambda item: item[0].casefold()))
    write_json(path, data)
    return changes


def sanitize_chapter_bible(path: Path, known: dict[str, str]) -> int:
    data = read_json(path, {})
    if not isinstance(data, dict):
        return 0
    changes = 0
    for section in ("characters", "entities", "terms"):
        items = data.get(section) or []
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source") or item.get("english") or "").strip()
            if not source:
                continue
            field = "russian" if "russian" in item and "target" not in item else "target"
            old = str(item.get(field) or "").strip()
            expected = lookup(known, source)
            if expected and old != expected:
                item[field] = expected
                changes += 1
                forbidden = item.setdefault("forbidden_targets", [])
                if old and old not in forbidden:
                    forbidden.append(old)
            elif not expected and latin_placeholder(source, old):
                item[field] = ""
                changes += 1
                forbidden = item.setdefault("forbidden_targets", [])
                if old and old not in forbidden:
                    forbidden.append(old)
    if changes:
        write_json(path, data)
    return changes


def sanitize_book_bible(path: Path, known: dict[str, str]) -> int:
    data = read_json(path, {})
    if not isinstance(data, dict):
        return 0
    changes = 0
    for section in ("characters", "entities"):
        collection = data.get(section) or {}
        if not isinstance(collection, dict):
            continue
        for source, record in collection.items():
            if not isinstance(record, dict):
                continue
            old = str(record.get("target") or "").strip()
            expected = lookup(known, source)
            if expected and old != expected:
                record["target"] = expected
                changes += 1
                forbidden = record.setdefault("forbidden_targets", [])
                if old and old not in forbidden:
                    forbidden.append(old)
            elif not expected and latin_placeholder(source, old):
                record["target"] = ""
                changes += 1
                forbidden = record.setdefault("forbidden_targets", [])
                if old and old not in forbidden:
                    forbidden.append(old)
            variants = record.get("variants")
            if isinstance(variants, dict):
                cleaned = {
                    value: count for value, count in variants.items()
                    if not latin_placeholder(source, str(value))
                }
                if cleaned != variants:
                    record["variants"] = cleaned
                    changes += 1
    if changes:
        write_json(path, data)
    return changes


def update_config(path: Path) -> bool:
    if not path.exists():
        return False
    cfg = read_json(path, {})
    if not isinstance(cfg, dict):
        return False
    repair = cfg.setdefault("repair", {})
    repair.update({
        "temperature": 0.0,
        "top_p": 1.0,
        "enable_thinking": False,
        "max_pids_per_call": 1,
        "max_tokens": 1200,
        "generation_retries": 3,
        "context_before": 1,
        "context_after": 1,
        "auto_repair_verified_decisions": ["repair"],
        "auto_repair_verifier_confidences": ["high", "deterministic"],
        "retry_on_keep_or_invalid": True,
    })
    formatting = cfg.setdefault("formatting", {})
    formatting.update({
        "temperature": 0.0,
        "enable_thinking": False,
        "retry_unresolved_spans": True,
    })
    cfg.setdefault("post_repair_verifier", {}).update({
        "enabled": True,
        "required": True,
        "temperature": 0.0,
        "top_p": 1.0,
        "enable_thinking": True,
        "reasoning_budget": 128,
        "reject_policy": "revert_to_draft",
        "uncertain_policy": "revert_to_draft",
        "accept_confidences": ["high"],
        "max_repair_rounds": 2,
        "fail_on_unresolved": True,
    })
    cfg.setdefault("verifier", {}).update({
        "fail_on_uncertain": True,
    })
    write_json(path, cfg)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--additions", type=Path)
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args()
    if args.version:
        print(f"apply_project_fixes.py {VERSION}")
        return 0
    if args.project_root is None or args.additions is None:
        parser.error("--project-root and --additions are required")

    project = args.project_root.resolve()
    additions = read_json(args.additions.resolve(), {})
    if not isinstance(additions, dict):
        raise RuntimeError("glossary additions must be an object")

    glossary_dirs = [project / "glossary"]
    pipeline_runs = project / "pipeline_runs"
    if pipeline_runs.exists():
        glossary_dirs.extend(path / "glossary" for path in pipeline_runs.iterdir() if path.is_dir())

    glossary_changes = 0
    for directory in glossary_dirs:
        glossary_changes += merge_locked(directory, additions)

    bible_changes = 0
    main_known = load_known(project / "glossary")
    bible_changes += sanitize_book_bible(project / "book_bible.json", main_known)

    if pipeline_runs.exists():
        for run in pipeline_runs.iterdir():
            if not run.is_dir():
                continue
            known = load_known(run / "glossary") or main_known
            bible_changes += sanitize_book_bible(run / "book_bible.json", known)
            work = run / "work"
            if work.exists():
                for chapter in work.iterdir():
                    if chapter.is_dir():
                        bible_changes += sanitize_chapter_bible(chapter / "chapter_bible.json", known)

    config_updated = update_config(project / "config.v3.json")
    print(
        f"Project fixes applied: glossary_changes={glossary_changes}, "
        f"bible_changes={bible_changes}, config_updated={config_updated}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
