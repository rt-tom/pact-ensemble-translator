#!/usr/bin/env python3
"""Finalize cross-verification decisions into repairable issues."""
from __future__ import annotations

import argparse
import logging
from typing import Any

from v31_common import (
    VERSION, add_common_args, load_cfg, load_runtime, norm, read_json,
    selected_chapters, setup_logging, write_json,
)


def compatible_issue(issue: dict[str, Any]) -> dict[str, Any]:
    detectors = issue.get("detected_by") or []
    return {
        "pid": issue["pid"],
        "severity": issue.get("severity", "major"),
        "category": issue.get("category", "meaning"),
        "problem": issue.get("problem", ""),
        "repair_instruction": issue.get("repair_instruction", ""),
        "suggested_text": "",
        "source": "v31_ensemble:" + ",".join(detectors),
        "deterministic": "deterministic" in issue.get("detector_families", []),
        "status": "verified_repair_v31",
        "issue_id": issue["issue_id"],
        "verifier_decision": "repair",
        "verifier_confidence": issue.get("verification_confidence", "high"),
        "verifier_reason": issue.get("verification_reason", ""),
        "verifier_repair_goal": issue.get("required_invariant") or issue.get("repair_instruction", ""),
        # v3.1 extended metadata; core Issue ignores only if loaded directly, so
        # root issues.json is used for reporting, while v31 repair reads extended file.
    }



def consolidate_dual_decisions(
    q_record: dict[str, Any], g_record: dict[str, Any]
) -> tuple[str, str]:
    q_decision = norm(q_record.get("decision")).casefold()
    g_decision = norm(g_record.get("decision")).casefold()
    q_confidence = norm(q_record.get("confidence")).casefold()
    g_confidence = norm(g_record.get("confidence")).casefold()
    if q_decision == g_decision and q_decision in {"repair", "keep"}:
        confidence = "high" if q_confidence == g_confidence == "high" else "medium"
        return q_decision, confidence
    return "uncertain", "medium"


def resolve_uncertain_policy(
    decision: str, confidence: str, policy: str
) -> tuple[str, str, bool]:
    """Resolve an explicit uncertain verdict without faking high confidence."""
    if decision != "uncertain":
        return decision, confidence, False
    policy = norm(policy).casefold()
    if policy == "repair":
        return "repair", "medium", True
    if policy == "keep":
        return "keep", "medium", True
    return "uncertain", confidence or "medium", False

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    parser.add_argument("--translations-file")  # accepted for runner symmetry; verification uses issue reports
    args = parser.parse_args()
    setup_logging()
    runtime = load_runtime(args.project_root.resolve())
    cfg = load_cfg(runtime, args.config.resolve())
    verification_cfg = cfg.get("ensemble_v31", {}).get("verification", {})
    uncertain_policy = norm(verification_cfg.get("uncertain_policy", "fail")).casefold()
    if uncertain_policy not in {"fail", "repair", "keep"}:
        raise ValueError(f"Invalid uncertain_policy={uncertain_policy!r}")

    for source_path, work in selected_chapters(runtime, cfg, args.start, args.end):
        root = work / "v31" / args.pass_name
        merged = read_json(root / "merged_issues.json", {})
        qwen = read_json(root / "cross_verify_qwen.json", {})
        gemma = read_json(root / "cross_verify_gemma.json", {})
        decisions: dict[str, dict[str, dict[str, Any]]] = {}
        for report in (qwen, gemma):
            expected = int(report.get("expected", 0))
            completed = int(report.get("completed", 0))
            judge_name = str(report.get("judge") or "")
            if expected != completed:
                raise RuntimeError(f"Incomplete cross verification: {judge_name} {completed}/{expected}")
            for record in report.get("decisions") or []:
                decisions.setdefault(record["issue_id"], {})[judge_name] = record

        verified: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        uncertain: list[dict[str, Any]] = []
        all_decisions: list[dict[str, Any]] = []
        preverified = {item["issue_id"]: item for item in (merged.get("preverified") or [])}
        for issue in merged.get("issues") or []:
            issue = dict(issue)
            issue_id = issue["issue_id"]
            if issue_id in preverified:
                decision = issue.get("verification_decision", "repair")
                confidence = issue.get("verification_confidence", "high")
                reason = issue.get("verification_reason", "")
                invariant = issue.get("required_invariant") or issue.get("repair_instruction", "")
                scope = issue.get("scope", "span")
                target_span = issue.get("target_span", "")
                forbidden = []
                judge = issue.get("verification_route", "preverified")
            else:
                issue_decisions = decisions.get(issue_id) or {}
                route = issue.get("verification_route", "")
                if route == "dual_cross_judge":
                    q_record = issue_decisions.get("qwen")
                    g_record = issue_decisions.get("gemma")
                    if q_record is None or g_record is None:
                        raise RuntimeError(f"Dual verification decision missing for {issue_id}")
                    decision, confidence = consolidate_dual_decisions(q_record, g_record)
                    reason = f"Qwen: {q_record.get('reason','')} | Gemma: {g_record.get('reason','')}"
                    invariant = (
                        q_record.get("required_invariant")
                        or g_record.get("required_invariant")
                        or issue.get("required_invariant")
                        or issue.get("repair_instruction", "")
                    )
                    scopes = [q_record.get("repair_scope"), g_record.get("repair_scope"), issue.get("scope", "span")]
                    scope = "paragraph" if "paragraph" in scopes else "sentence" if "sentence" in scopes else "span"
                    target_span = q_record.get("target_span") or g_record.get("target_span") or issue.get("target_span", "")
                    forbidden = list(dict.fromkeys(
                        list(q_record.get("forbidden_interpretations") or [])
                        + list(g_record.get("forbidden_interpretations") or [])
                    ))
                    judge = "qwen+gemma"
                else:
                    expected_judge = "qwen" if "qwen" in route else "gemma"
                    record = issue_decisions.get(expected_judge)
                    if record is None:
                        raise RuntimeError(f"No {expected_judge} verification decision for {issue_id}")
                    decision = record["decision"]
                    confidence = record["confidence"]
                    reason = record.get("reason", "")
                    invariant = record.get("required_invariant") or issue.get("required_invariant") or issue.get("repair_instruction", "")
                    scope = record.get("repair_scope") or issue.get("scope", "span")
                    target_span = record.get("target_span") or issue.get("target_span", "")
                    forbidden = record.get("forbidden_interpretations") or []
                    judge = record.get("judge", expected_judge)
            original_decision = decision
            original_confidence = confidence
            decision, confidence, policy_applied = resolve_uncertain_policy(
                decision, confidence, uncertain_policy
            )
            if policy_applied:
                reason = (
                    f"Uncertain policy {uncertain_policy!r} applied. "
                    f"Original: {original_decision}/{original_confidence}. {reason}"
                )
            issue.update({
                "verification_decision": decision,
                "verification_confidence": confidence,
                "verification_reason": reason,
                "required_invariant": invariant,
                "scope": scope,
                "target_span": target_span,
                "forbidden_interpretations": forbidden,
                "verification_judge": judge,
                "verification_policy": uncertain_policy,
                "verification_policy_applied": policy_applied,
                "verification_original_decision": original_decision,
                "verification_original_confidence": original_confidence,
            })
            row = {
                "issue_id": issue_id, "pid": issue["pid"],
                "decision": decision, "confidence": confidence,
                "judge": judge, "reason": reason,
                "policy": uncertain_policy,
                "policy_applied": policy_applied,
                "original_decision": original_decision,
                "original_confidence": original_confidence,
            }
            all_decisions.append(row)
            if decision == "repair" and confidence in {"high", "medium", "deterministic"}:
                verified.append(issue)
            elif decision == "keep" and confidence in {"high", "medium"}:
                rejected.append(issue)
            else:
                uncertain.append(issue)

        write_json(root / "verification_report.json", {
            "version": VERSION,
            "chapter": source_path.name,
            "pass": args.pass_name,
            "total": len(all_decisions),
            "repair": len(verified),
            "keep": len(rejected),
            "uncertain": len(uncertain),
            "uncertain_policy": uncertain_policy,
            "policy_resolved": sum(bool(row.get("policy_applied")) for row in all_decisions),
            "policy_repair": sum(row.get("policy_applied") and row.get("decision") == "repair" for row in all_decisions),
            "policy_keep": sum(row.get("policy_applied") and row.get("decision") == "keep" for row in all_decisions),
            "decisions": all_decisions,
        })
        write_json(root / "verified_issues.json", verified)
        write_json(root / "rejected_issues.json", rejected)
        write_json(root / "uncertain_issues.json", uncertain)
        fail_on_uncertain = bool(verification_cfg.get("fail_on_uncertain", uncertain_policy == "fail"))
        if uncertain and (uncertain_policy == "fail" or fail_on_uncertain):
            raise RuntimeError(f"{len(uncertain)} issue(s) remain uncertain in {args.pass_name} verification")
        logging.info("%s verification: repair=%s keep=%s uncertain=%s", source_path.name, len(verified), len(rejected), len(uncertain))

        # Primary compatible snapshot is useful for existing reports and fallback.
        if args.pass_name == "primary":
            compat = [compatible_issue(issue) for issue in verified]
            write_json(work / "verified_issues.json", compat)
            write_json(work / "issues.json", compat)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
