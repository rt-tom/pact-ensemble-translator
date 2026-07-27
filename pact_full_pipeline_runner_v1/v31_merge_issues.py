#!/usr/bin/env python3
"""Merge deterministic, Qwen, and Gemma audit findings for Pact v3.1."""
from __future__ import annotations

import argparse
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from v31_common import (
    VERSION, add_common_args, issue_record, load_cfg, load_manifest,
    issue_fingerprint, load_runtime, load_translations, merge_duplicate_issues, norm,
    read_json, selected_chapters, setup_logging, write_json,
)

HARD_DETERMINISTIC = {"missing", "mixed_script"}
QWEN_PREFERRED = {"number", "number_word", "subject", "object", "negation", "modality", "meaning"}


def deterministic_for_chapter(runtime, cfg, work, blocks, translations):
    glossary = runtime.Glossary(cfg)
    book_bible = runtime.BookBible(Path(cfg["paths"]["book_bible_file"]))
    chapter_bible = read_json(work / "chapter_bible.json", {})
    block_objs = runtime.blocks_from_manifest(blocks)
    raw = runtime.deterministic_issues(
        block_objs, translations, cfg, glossary, chapter_bible, book_bible
    )
    result = []
    for item in raw:
        data = asdict(item)
        result.append(issue_record(
            pid=data["pid"],
            severity=data.get("severity", "major"),
            category=data.get("category", "deterministic"),
            problem=data.get("problem", ""),
            detector="deterministic",
            required_invariant=data.get("repair_instruction", ""),
            repair_instruction=data.get("repair_instruction", ""),
            scope="span",
            confidence="deterministic",
            metadata={"legacy_issue": data},
        ))
    return result


def model_families(issue: dict[str, Any]) -> set[str]:
    result = set()
    for detector in issue.get("detected_by") or []:
        name = str(detector).casefold()
        if "qwen" in name:
            result.add("qwen")
        elif "gemma" in name:
            result.add("gemma")
        elif "deterministic" in name:
            result.add("deterministic")
    return result


def family_confidences(issue: dict[str, Any]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {"qwen": set(), "gemma": set(), "deterministic": set()}
    for src in issue.get("source_issues") or []:
        if not isinstance(src, dict):
            continue
        confidence = norm(src.get("detector_confidence")).casefold()
        for family in model_families(src):
            if confidence:
                result.setdefault(family, set()).add(confidence)
    return result


def exact_high_cross_family_agreement(issue: dict[str, Any]) -> bool:
    """True only when high-confidence Qwen and Gemma findings share a fingerprint."""
    fingerprints: dict[str, set[str]] = {"qwen": set(), "gemma": set()}
    for src in issue.get("source_issues") or []:
        if not isinstance(src, dict):
            continue
        if norm(src.get("detector_confidence")).casefold() != "high":
            continue
        fp = issue_fingerprint(src)
        for family in model_families(src):
            if family in fingerprints:
                fingerprints[family].add(fp)
    return bool(fingerprints["qwen"] & fingerprints["gemma"])


def route_issue(issue: dict[str, Any]) -> tuple[bool, list[str]]:
    """Mutate one merged issue with its verification route.

    Returns (preverified, judges_to_queue).
    """
    families = model_families(issue)
    category = norm(issue.get("category")).casefold()
    confidences = family_confidences(issue)
    issue["detector_families"] = sorted(families)
    issue["family_confidences"] = {
        key: sorted(value) for key, value in confidences.items() if value
    }

    if "deterministic" in families and category in HARD_DETERMINISTIC:
        issue.update({
            "verification_route": "hard_deterministic",
            "verification_decision": "repair",
            "verification_confidence": "deterministic",
            "verification_reason": "Hard deterministic invariant failed.",
        })
        return True, []

    if "qwen" in families and "gemma" in families:
        qwen_high = "high" in confidences.get("qwen", set())
        gemma_high = "high" in confidences.get("gemma", set())
        if qwen_high and gemma_high and exact_high_cross_family_agreement(issue):
            issue.update({
                "verification_route": "independent_detector_agreement",
                "verification_decision": "repair",
                "verification_confidence": "high",
                "verification_reason": (
                    "Independent Qwen and Gemma audit families detected the "
                    "same exact issue fingerprint with high confidence."
                ),
                "required_invariant": issue.get("required_invariant") or issue.get("repair_instruction"),
            })
            return True, []
        issue["verification_route"] = "dual_cross_judge"
        return False, ["qwen", "gemma"]

    if "qwen" in families and "gemma" not in families:
        issue["verification_route"] = "gemma_cross_judge"
        return False, ["gemma"]
    if "gemma" in families and "qwen" not in families:
        issue["verification_route"] = "qwen_cross_judge"
        return False, ["qwen"]
    if "deterministic" in families:
        judge = "qwen" if category in QWEN_PREFERRED else "gemma"
        issue["verification_route"] = f"{judge}_deterministic_judge"
        return False, [judge]
    issue["verification_route"] = "gemma_cross_judge"
    return False, ["gemma"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    parser.add_argument("--translations-file")
    args = parser.parse_args()
    setup_logging()
    runtime = load_runtime(args.project_root.resolve())
    cfg = load_cfg(runtime, args.config.resolve())

    for source_path, work in selected_chapters(runtime, cfg, args.start, args.end):
        _, blocks, _ = load_manifest(work)
        translations = load_translations(work, args.pass_name, args.translations_file)
        root = work / "v31" / args.pass_name
        out = root / "merged_issues.json"
        if out.exists() and not args.force:
            logging.info("Reusing %s", out)
            continue

        issues = deterministic_for_chapter(runtime, cfg, work, blocks, translations)
        detectors = ("qwen_semantic", "gemma_semantic", "gemma_russian", "qwen_global_smoke") if args.pass_name == "final" else ("qwen_semantic", "gemma_semantic", "gemma_russian", "gemma_discourse")
        for detector in detectors:
            data = read_json(root / f"{detector}.json", {})
            coverage = data.get("coverage") or {}
            if not coverage.get("ok"):
                raise RuntimeError(f"Missing complete {detector} coverage for {source_path.name}")
            issues.extend(data.get("issues") or [])

        merged = merge_duplicate_issues(issues)
        for index, issue in enumerate(merged, 1):
            issue["issue_id"] = f"v31-{args.pass_name}-{index:05d}"
        queues = {"qwen": [], "gemma": []}
        preverified: list[dict[str, Any]] = []
        for issue in merged:
            is_preverified, judges = route_issue(issue)
            if is_preverified:
                preverified.append(issue)
            else:
                for judge in judges:
                    queues[judge].append(issue)

        root.mkdir(parents=True, exist_ok=True)
        write_json(out, {
            "version": VERSION,
            "chapter": source_path.name,
            "pass": args.pass_name,
            "raw_issue_count": len(issues),
            "merged_issue_count": len(merged),
            "preverified": preverified,
            "queues": {key: value for key, value in queues.items()},
            "issues": merged,
        })
        write_json(root / "verify_queue_qwen.json", queues["qwen"])
        write_json(root / "verify_queue_gemma.json", queues["gemma"])
        logging.info(
            "%s merge: raw=%s merged=%s preverified=%s qwen_queue=%s gemma_queue=%s",
            source_path.name, len(issues), len(merged), len(preverified),
            len(queues["qwen"]), len(queues["gemma"]),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
