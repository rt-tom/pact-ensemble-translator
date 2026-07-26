#!/usr/bin/env python3
"""Deterministic gate for every Pact v3.1 repair candidate."""
from __future__ import annotations

import argparse
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from v31_common import (
    VERSION, add_common_args, load_cfg, load_manifest, load_runtime,
    load_translations, norm, read_json, selected_chapters, setup_logging,
    write_json,
)

HARD_CATEGORIES = {"missing", "mixed_script", "english_residue", "number", "number_word", "entity_consistency", "name_consistency", "narrator_gender"}


def by_pid(runtime, cfg, work, blocks_raw, translations, only_pid: str | None = None):
    glossary = runtime.Glossary(cfg)
    book = runtime.BookBible(Path(cfg["paths"]["book_bible_file"]))
    bible = read_json(work / "chapter_bible.json", {})
    block_objs = runtime.blocks_from_manifest(blocks_raw)
    if only_pid is not None:
        block_objs = [block for block in block_objs if block.pid == only_pid]
    issues = runtime.deterministic_issues(block_objs, translations, cfg, glossary, bible, book)
    result: dict[str, list[dict[str, Any]]] = {}
    for issue in issues:
        result.setdefault(issue.pid, []).append(asdict(issue))
    return result


def signature(issue: dict[str, Any]) -> tuple[str, str]:
    return norm(issue.get("category")).casefold(), norm(issue.get("problem")).casefold()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    parser.add_argument("--round", type=int, required=True)
    parser.add_argument("--translations-file")
    args = parser.parse_args()
    setup_logging()
    runtime = load_runtime(args.project_root.resolve())
    cfg = load_cfg(runtime, args.config.resolve())

    for source_path, work in selected_chapters(runtime, cfg, args.start, args.end):
        _, blocks_raw, _ = load_manifest(work)
        translations = load_translations(work, args.pass_name, args.translations_file)
        root = work / "v31" / args.pass_name
        candidate_report = read_json(root / f"repair_candidates_round_{args.round:02d}.json", {})
        base_issues = by_pid(runtime, cfg, work, blocks_raw, translations)
        decisions = []
        total = sum(len(record.get("candidates") or []) for record in candidate_report.get("records") or [])
        done = 0
        for record in candidate_report.get("records") or []:
            pid = record["pid"]
            original_verified = record.get("issues") or []
            original_det_categories = {
                norm(issue.get("category")).casefold()
                for issue in original_verified
                if "deterministic" in (issue.get("detector_families") or [])
                or "deterministic" in (issue.get("detected_by") or [])
            }
            for candidate in record.get("candidates") or []:
                action = candidate["action"]
                errors = list(candidate.get("validation_errors") or [])
                introduced = []
                remaining_required = []
                if action == "challenge_issue":
                    if original_det_categories & HARD_CATEGORIES:
                        errors.append("Cannot challenge a hard deterministic invariant")
                else:
                    test_map = dict(translations)
                    test_map[pid] = candidate["after"]
                    cand_issues = by_pid(runtime, cfg, work, blocks_raw, test_map, pid).get(pid, [])
                    baseline_sigs = {signature(x) for x in base_issues.get(pid, [])}
                    for issue in cand_issues:
                        sig = signature(issue)
                        category = sig[0]
                        if sig not in baseline_sigs and category in HARD_CATEGORIES:
                            introduced.append(issue)
                    cand_categories = {norm(x.get("category")).casefold() for x in cand_issues}
                    for category in original_det_categories:
                        if category in cand_categories:
                            remaining_required.append(category)
                    if introduced:
                        errors.append("Introduced deterministic issue(s): " + ", ".join(sorted({x["category"] for x in introduced})))
                    if remaining_required:
                        errors.append("Did not resolve deterministic category: " + ", ".join(sorted(remaining_required)))
                decisions.append({
                    "version": VERSION,
                    "pass": args.pass_name,
                    "round": args.round,
                    "pid": pid,
                    "candidate_id": candidate["candidate_id"],
                    "action": action,
                    "passed": not errors,
                    "errors": errors,
                    "introduced_issues": introduced,
                    "remaining_required_categories": remaining_required,
                })
                done += 1
                logging.info("deterministic gate %s: %s/%s %s/%s passed=%s", source_path.name, done, total, pid, candidate["candidate_id"], not errors)
        write_json(root / f"post_gate_deterministic_round_{args.round:02d}.json", {
            "version": VERSION,
            "chapter": source_path.name,
            "pass": args.pass_name,
            "round": args.round,
            "expected": total,
            "completed": len(decisions),
            "decisions": decisions,
        })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
