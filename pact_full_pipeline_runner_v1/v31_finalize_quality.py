#!/usr/bin/env python3
"""Seal Pact v3.1 ensemble results for existing HTML finalization."""
from __future__ import annotations

import argparse
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from v31_common import (
    VERSION, add_common_args, load_cfg, load_manifest, load_runtime,
    norm, read_json, selected_chapters, setup_logging, write_json,
)

DEFAULT_FAIL_CATEGORIES = {
    "missing", "mixed_script", "english_residue", "number", "number_word",
    "entity_consistency", "name_consistency", "narrator_gender",
}


def compatible(issue: dict[str, Any]) -> dict[str, Any]:
    detectors = issue.get("detected_by") or []
    return {
        "pid": issue["pid"],
        "severity": issue.get("severity", "major"),
        "category": issue.get("category", "meaning"),
        "problem": issue.get("problem", ""),
        "repair_instruction": issue.get("repair_instruction", ""),
        "suggested_text": "",
        "source": "v31_ensemble:" + ",".join(detectors),
        "deterministic": "deterministic" in (issue.get("detector_families") or []) or "deterministic" in detectors,
        "status": "verified_repair_v31",
        "issue_id": issue["issue_id"],
        "verifier_decision": "repair",
        "verifier_confidence": issue.get("verification_confidence", "high"),
        "verifier_reason": issue.get("verification_reason", ""),
        "verifier_repair_goal": issue.get("required_invariant") or issue.get("repair_instruction", ""),
    }


def deterministic(runtime, cfg, work, blocks_raw, translations):
    glossary = runtime.Glossary(cfg)
    book = runtime.BookBible(Path(cfg["paths"]["book_bible_file"]))
    bible = read_json(work / "chapter_bible.json", {})
    block_objs = runtime.blocks_from_manifest(blocks_raw)
    return [asdict(x) for x in runtime.deterministic_issues(block_objs, translations, cfg, glossary, bible, book)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser, include_pass=False)
    args = parser.parse_args()
    setup_logging()
    runtime = load_runtime(args.project_root.resolve())
    cfg = load_cfg(runtime, args.config.resolve())
    fail_categories = {
        norm(x).casefold() for x in cfg.get("ensemble_v31", {}).get("final_quality", {}).get(
            "fail_deterministic_categories", sorted(DEFAULT_FAIL_CATEGORIES)
        )
    }

    for source_path, work in selected_chapters(runtime, cfg, args.start, args.end):
        _, blocks_raw, _ = load_manifest(work)
        expected_pids = [str(block["pid"]) for block in blocks_raw]
        expected_set = set(expected_pids)
        unresolved: list[dict[str, Any]] = []
        coverage: dict[str, Any] = {}

        scene = read_json(work / "source_scene_map.json", {})
        scene_cov = dict(scene.get("coverage") or {})
        scene_cov["statistics"] = scene.get("statistics") or {}
        coverage["source_analysis"] = scene_cov
        if not scene_cov.get("ok") or int(scene_cov.get("completed", -1)) != len(expected_pids):
            unresolved.append({"stage": "source_analysis", "reason": "incomplete coverage", "coverage": scene_cov})

        draft = read_json(work / "draft_translations.json", {})
        if set(draft) != expected_set or any(not norm(draft.get(pid)) for pid in expected_pids):
            unresolved.append({
                "stage": "translation",
                "reason": "draft PID coverage mismatch or empty translation",
                "expected": len(expected_pids),
                "actual": len(draft),
            })

        final_path = work / "v31_final_translations.json"
        if not final_path.exists():
            primary = work / "v31_primary_translations.json"
            if not primary.exists():
                raise FileNotFoundError("No v3.1 translations found")
            translations = read_json(primary, {})
            write_json(final_path, translations)
        translations = read_json(final_path, {})
        if set(translations) != expected_set or any(not norm(translations.get(pid)) for pid in expected_pids):
            unresolved.append({
                "stage": "final_translations",
                "reason": "final PID coverage mismatch or empty translation",
                "expected": len(expected_pids),
                "actual": len(translations),
            })

        lifecycle: list[dict[str, Any]] = []
        verified: list[dict[str, Any]] = []
        detectors = ("qwen_semantic", "gemma_semantic", "gemma_russian", "gemma_discourse")
        for pass_name in ("primary", "residual"):
            root = work / "v31" / pass_name
            pass_verified = read_json(root / "verified_issues.json", [])
            pass_lifecycle = read_json(root / "lifecycle.json", [])
            pass_uncertain = read_json(root / "uncertain_issues.json", [])
            status_path = root / "status.json"
            verification_path = root / "verification_report.json"
            merged_path = root / "merged_issues.json"

            lifecycle.extend(pass_lifecycle)
            verified.extend(pass_verified)

            for detector in detectors:
                path = root / f"{detector}.json"
                report = read_json(path, {})
                cov = report.get("coverage") or {}
                coverage[f"{pass_name}:{detector}"] = cov
                if not path.exists() or not cov.get("ok") or int(cov.get("completed", -1)) != len(expected_pids):
                    unresolved.append({
                        "pass": pass_name,
                        "detector": detector,
                        "reason": "missing or incomplete coverage",
                        "coverage": cov,
                    })

            merged = read_json(merged_path, {})
            verification = read_json(verification_path, {})
            merged_count = int(merged.get("merged_issue_count", -1))
            verification_total = int(verification.get("total", -1))
            decision_total = sum(int(verification.get(key, 0)) for key in ("repair", "keep", "uncertain"))
            if not merged_path.exists() or not verification_path.exists() or merged_count < 0:
                unresolved.append({"pass": pass_name, "stage": "verification", "reason": "required report missing"})
            elif verification_total != merged_count or decision_total != verification_total:
                unresolved.append({
                    "pass": pass_name,
                    "stage": "verification",
                    "reason": "issue accounting mismatch",
                    "merged": merged_count,
                    "verified_total": verification_total,
                    "decision_total": decision_total,
                })

            for judge in ("qwen", "gemma"):
                queue = read_json(root / f"verify_queue_{judge}.json", [])
                report_path = root / f"cross_verify_{judge}.json"
                report = read_json(report_path, {})
                if (
                    not report_path.exists()
                    or int(report.get("expected", -1)) != len(queue)
                    or int(report.get("completed", -1)) != len(queue)
                ):
                    unresolved.append({
                        "pass": pass_name,
                        "stage": f"cross_verify_{judge}",
                        "reason": "queue coverage mismatch",
                        "queue": len(queue),
                        "expected": report.get("expected"),
                        "completed": report.get("completed"),
                    })

            if pass_uncertain:
                unresolved.append({"pass": pass_name, "uncertain": len(pass_uncertain)})

            status = read_json(status_path, {})
            verified_ids = {str(item.get("issue_id")) for item in pass_verified if item.get("issue_id")}
            lifecycle_ids = {str(item.get("issue_id")) for item in pass_lifecycle if item.get("issue_id")}
            unresolved_lifecycle = [
                row for row in pass_lifecycle
                if row.get("status") not in {"resolved_repair", "resolved_false_positive"}
            ]
            if not status_path.exists():
                unresolved.append({"pass": pass_name, "stage": "repair", "reason": "status.json missing"})
            elif (
                int(status.get("retry_required", -1)) != 0
                or int(status.get("total", -1)) != len(verified_ids)
                or int(status.get("resolved", -1)) != len(verified_ids)
                or lifecycle_ids != verified_ids
                or unresolved_lifecycle
            ):
                unresolved.append({
                    "pass": pass_name,
                    "stage": "repair",
                    "reason": "verified issue lifecycle is not fully resolved",
                    "verified_ids": len(verified_ids),
                    "lifecycle_ids": len(lifecycle_ids),
                    "status": status,
                    "unresolved_lifecycle": unresolved_lifecycle,
                })

        final_det = deterministic(runtime, cfg, work, blocks_raw, translations)
        blocking_det = [x for x in final_det if norm(x.get("category")).casefold() in fail_categories]
        if blocking_det:
            unresolved.append({"final_deterministic": blocking_det})

        if unresolved:
            write_json(work / "v31_quality_gate.json", {
                "version": VERSION,
                "chapter": source_path.name,
                "ok": False,
                "unresolved": unresolved,
                "coverage": coverage,
                "final_deterministic_issues": final_det,
            })
            raise RuntimeError(
                f"v3.1 final quality gate failed for {source_path.name}: "
                f"{len(unresolved)} blocking condition(s)"
            )

        compat = [compatible(issue) for issue in verified]
        changed_pids = [pid for pid in expected_pids if draft.get(pid) != translations.get(pid)]
        repair_records = []
        for row in lifecycle:
            if row.get("status") in {"resolved_repair", "resolved_false_positive"}:
                repair_records.append({
                    "pid": row.get("pid"),
                    "issue_ids": [row.get("issue_id")],
                    "action": "replace" if row.get("status") == "resolved_repair" else "keep",
                    "accepted": True,
                    "outcome": row.get("status"),
                    "round": row.get("round"),
                    "pass": row.get("pass"),
                })

        write_json(work / "issues.json", compat)
        write_json(work / "verified_issues.json", compat)
        write_json(work / "repaired_translations.preverify.json", translations)
        write_json(work / "repaired_translations.json", translations)
        write_json(work / "repair_records.json", repair_records)
        write_json(work / "issue_lifecycle.json", lifecycle)
        write_json(work / "post_repair_report.json", {
            "version": VERSION,
            "chapter": source_path.stem,
            "pipeline": "ensemble_v31",
            "changed_candidates": len(changed_pids),
            "accepted": len(changed_pids),
            "reverted": 0,
            "uncertain": 0,
            "no_candidate": 0,
            "retry_required": 0,
            "unresolved_total": 0,
            "resolved_issue_count": len(lifecycle),
            "unresolved_issue_count": 0,
            "coverage": coverage,
            "final_deterministic_issue_count": len(final_det),
            "changed_pids": changed_pids,
        })
        write_json(work / "v31_quality_gate.json", {
            "version": VERSION,
            "chapter": source_path.name,
            "ok": True,
            "coverage": coverage,
            "verified_issue_count": len(verified),
            "lifecycle_count": len(lifecycle),
            "changed_pids": changed_pids,
            "final_deterministic_issues": final_det,
        })
        logging.info(
            "v3.1 quality gate passed %s: verified=%s changed=%s",
            source_path.name, len(verified), len(changed_pids),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
