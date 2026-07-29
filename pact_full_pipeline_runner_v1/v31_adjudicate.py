#!/usr/bin/env python3
"""Adjudicate independent repair gates and advance v3.1 translations."""
from __future__ import annotations

import argparse
import logging
from typing import Any

from v31_common import (
    VERSION, add_common_args, load_cfg, load_runtime, load_translations,
    norm, read_json, selected_chapters, setup_logging, write_json,
)


def key(row: dict[str, Any]) -> tuple[str, str]:
    return str(row["pid"]), str(row["candidate_id"])


def semantic_accept(row: dict[str, Any], challenge: bool) -> bool:
    if row.get("confidence") != "high":
        return False
    if challenge:
        return row.get("verdict") == "accept_challenge" and row.get("issue_valid") is False
    return (
        row.get("verdict") == "accept"
        and row.get("faithful_to_source") is True
        and row.get("all_issues_fixed") is True
        and row.get("introduced_new_semantic_error") is False
    )


def russian_accept(row: dict[str, Any], challenge: bool) -> bool:
    if row.get("confidence") != "high":
        return False
    if challenge:
        return row.get("verdict") == "accept_challenge" and row.get("issue_valid") is False
    return (
        row.get("verdict") == "accept"
        and row.get("natural_russian") is True
        and row.get("all_issues_fixed") is True
        and row.get("introduced_new_russian_error") is False
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    parser.add_argument("--round", type=int, required=True)
    parser.add_argument("--translations-file")
    parser.add_argument(
        "--terminal-round",
        action="store_true",
        help="Keep the current translation when no candidate passes on the final allowed round.",
    )
    args = parser.parse_args()
    setup_logging()
    runtime = load_runtime(args.project_root.resolve())
    cfg = load_cfg(runtime, args.config.resolve())

    for source_path, work in selected_chapters(runtime, cfg, args.start, args.end):
        root = work / "v31" / args.pass_name
        translations = load_translations(work, args.pass_name, args.translations_file)
        candidates_report = read_json(root / f"repair_candidates_round_{args.round:02d}.json", {})
        qwen_semantic_report = read_json(root / f"post_gate_qwen_semantic_round_{args.round:02d}.json", {})
        gemma_semantic_report = read_json(root / f"post_gate_gemma_semantic_round_{args.round:02d}.json", {})
        gemma_russian_report = read_json(root / f"post_gate_gemma_russian_round_{args.round:02d}.json", {})
        det_report = read_json(root / f"post_gate_deterministic_round_{args.round:02d}.json", {})

        total = sum(len(r.get("candidates") or []) for r in candidates_report.get("records") or [])
        for report, name in (
            (qwen_semantic_report, "qwen_semantic"),
            (gemma_semantic_report, "gemma_semantic"),
            (gemma_russian_report, "gemma_russian"),
            (det_report, "deterministic"),
        ):
            if int(report.get("expected", -1)) != total or int(report.get("completed", -1)) != total:
                raise RuntimeError(f"Incomplete {name} gate for round {args.round}: {report.get('completed')}/{total}")
        qwen_semantic = {key(x): x for x in qwen_semantic_report.get("decisions") or []}
        gemma_semantic = {key(x): x for x in gemma_semantic_report.get("decisions") or []}
        gemma_russian = {key(x): x for x in gemma_russian_report.get("decisions") or []}
        det = {key(x): x for x in det_report.get("decisions") or []}

        retry_requests = []
        decisions = []
        lifecycle = read_json(root / "lifecycle.json", [])
        lifecycle_by_issue = {row["issue_id"]: row for row in lifecycle if row.get("issue_id")}

        for record in candidates_report.get("records") or []:
            pid = record["pid"]
            accepted_repairs = []
            accepted_challenges = []
            evaluated = []
            for candidate in record.get("candidates") or []:
                k = (pid, candidate["candidate_id"])
                qs = qwen_semantic.get(k)
                gs = gemma_semantic.get(k)
                gr = gemma_russian.get(k)
                dr = det.get(k)
                if qs is None or gs is None or gr is None or dr is None:
                    raise RuntimeError(f"Missing gate decision for {k}")
                challenge = candidate["action"] == "challenge_issue"
                passed = (
                    semantic_accept(qs, challenge)
                    and semantic_accept(gs, challenge)
                    and russian_accept(gr, challenge)
                    and bool(dr.get("passed"))
                )
                row = {
                    "pid": pid,
                    "candidate_id": candidate["candidate_id"],
                    "action": candidate["action"],
                    "passed": passed,
                    "candidate": candidate,
                    "qwen_semantic": qs,
                    "gemma_semantic": gs,
                    "gemma_russian": gr,
                    "deterministic": dr,
                }
                evaluated.append(row)
                if passed:
                    (accepted_challenges if challenge else accepted_repairs).append(row)

            selected = None
            outcome = "retry_required"
            if accepted_repairs:
                # Minimal accepted edit wins. Full-paragraph candidates naturally
                # lose when an equally safe span candidate exists.
                selected = sorted(accepted_repairs, key=lambda r: (float(r["candidate"].get("changed_ratio", 1.0)), r["candidate_id"]))[0]
                translations[pid] = selected["candidate"]["after"]
                outcome = "repair_accepted"
            elif accepted_challenges:
                selected = accepted_challenges[0]
                outcome = "issue_challenge_accepted"
            elif args.terminal_round:
                outcome = "kept_after_retry_exhausted"
            else:
                feedback = []
                for row in evaluated:
                    qs = row["qwen_semantic"]
                    gs = row["gemma_semantic"]
                    gr = row["gemma_russian"]
                    dr = row["deterministic"]
                    pieces = [f"Candidate {row['candidate_id']} ({row['action']}) failed."]
                    if qs.get("feedback") or qs.get("reason"):
                        pieces.append("Qwen semantic: " + (qs.get("feedback") or qs.get("reason")))
                    if gs.get("feedback") or gs.get("reason"):
                        pieces.append("Gemma semantic: " + (gs.get("feedback") or gs.get("reason")))
                    if gr.get("feedback") or gr.get("reason"):
                        pieces.append("Gemma Russian: " + (gr.get("feedback") or gr.get("reason")))
                    if dr.get("errors"):
                        pieces.append("Deterministic: " + "; ".join(dr["errors"]))
                    feedback.append(" ".join(pieces))
                retry_requests.append({
                    "pid": pid,
                    "issue_ids": [issue["issue_id"] for issue in record.get("issues") or []],
                    "feedback": feedback,
                    "round": args.round,
                })

            decisions.append({
                "pid": pid,
                "round": args.round,
                "outcome": outcome,
                "selected_candidate_id": selected["candidate_id"] if selected else None,
                "before": record.get("candidates", [{}])[0].get("before", translations.get(pid, "")),
                "after": translations.get(pid, ""),
                "evaluated": evaluated,
            })
            for issue in record.get("issues") or []:
                lifecycle_by_issue[issue["issue_id"]] = {
                    "issue_id": issue["issue_id"],
                    "pid": pid,
                    "pass": args.pass_name,
                    "round": args.round,
                    "status": (
                        "resolved_repair" if outcome == "repair_accepted"
                        else "resolved_false_positive" if outcome == "issue_challenge_accepted"
                        else "resolved_retry_exhausted" if outcome == "kept_after_retry_exhausted"
                        else "retry_required"
                    ),
                    "detected_by": issue.get("detected_by") or [],
                    "verification_route": issue.get("verification_route"),
                    "verification_confidence": issue.get("verification_confidence"),
                    "selected_candidate_id": selected["candidate_id"] if selected else None,
                }
            logging.info("adjudicate %s %s round %s: %s -> %s", args.pass_name, source_path.name, args.round, pid, outcome)

        output_name = "v31_primary_translations.json" if args.pass_name == "primary" else "v31_final_translations.json"
        write_json(work / output_name, translations)
        write_json(root / f"retry_requests_round_{args.round:02d}.json", retry_requests)
        write_json(root / f"adjudication_round_{args.round:02d}.json", {
            "version": VERSION,
            "chapter": source_path.name,
            "pass": args.pass_name,
            "round": args.round,
            "pid_count": len(decisions),
            "accepted_repairs": sum(1 for x in decisions if x["outcome"] == "repair_accepted"),
            "accepted_challenges": sum(1 for x in decisions if x["outcome"] == "issue_challenge_accepted"),
            "kept_after_retry_exhausted": sum(1 for x in decisions if x["outcome"] == "kept_after_retry_exhausted"),
            "retry_required": len(retry_requests),
            "decisions": decisions,
        })
        write_json(root / "lifecycle.json", list(lifecycle_by_issue.values()))
        write_json(root / "status.json", {
            "version": VERSION,
            "pass": args.pass_name,
            "last_round": args.round,
            "retry_required": len(retry_requests),
            "kept_after_retry_exhausted": sum(
                1 for x in decisions if x["outcome"] == "kept_after_retry_exhausted"
            ),
            "resolved": len(lifecycle_by_issue) - sum(1 for x in lifecycle_by_issue.values() if x["status"] == "retry_required"),
            "total": len(lifecycle_by_issue),
        })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
