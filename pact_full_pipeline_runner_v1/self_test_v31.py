#!/usr/bin/env python3
"""Offline contract tests for Pact ensemble pipeline v3.1."""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from v31_common import issue_record, merge_duplicate_issues
import v31_postcheck
import v31_repair
import v31_finalize_verification
import v31_merge_issues


def load_runtime(project_root: Path):
    from v31_common import load_runtime as _load
    return _load(project_root)


def run(project_root: Path) -> None:
    runtime = load_runtime(project_root)

    qwen = issue_record(
        pid="p00001", category="meaning", problem="wrong idiom",
        detector="qwen_semantic_primary", target_span="трудно отложить",
        required_invariant="hard to kill", confidence="high",
    )
    gemma = issue_record(
        pid="p00001", category="calque", problem="broken collocation",
        detector="gemma_russian_primary", target_span="трудно отложить",
        required_invariant="natural Russian", confidence="high",
    )
    merged = merge_duplicate_issues([qwen, gemma])
    assert len(merged) == 2, merged

    same_qwen = issue_record(
        pid="p00002", category="meaning", problem="wrong idiom",
        detector="qwen_semantic_primary", target_span="трудно отложить",
        required_invariant="hard to kill", confidence="high",
    )
    same_gemma = issue_record(
        pid="p00002", category="meaning", problem="wrong idiom",
        detector="gemma_semantic_primary", target_span="трудно отложить",
        required_invariant="hard to kill", confidence="high",
    )
    exact = merge_duplicate_issues([same_qwen, same_gemma])
    assert len(exact) == 1 and exact[0]["agreement_count"] == 2
    preverified, judges = v31_merge_issues.route_issue(exact[0])
    assert preverified and not judges
    assert exact[0]["verification_route"] == "independent_detector_agreement"

    hard = issue_record(
        pid="p00003", category="mixed_script", problem="Mary remains",
        detector="deterministic", target_span="Mary",
        required_invariant="Russian spelling", confidence="deterministic",
    )
    hard["detected_by"].append("gemma_semantic_primary")
    hard["source_issues"] = [dict(hard)]
    preverified, judges = v31_merge_issues.route_issue(hard)
    assert preverified and not judges and hard["verification_route"] == "hard_deterministic"

    assert v31_finalize_verification.consolidate_dual_decisions(
        {"decision": "repair", "confidence": "high"},
        {"decision": "repair", "confidence": "medium"},
    ) == ("repair", "medium")
    assert v31_finalize_verification.consolidate_dual_decisions(
        {"decision": "repair", "confidence": "high"},
        {"decision": "keep", "confidence": "high"},
    ) == ("uncertain", "medium")
    assert v31_finalize_verification.resolve_uncertain_policy(
        "uncertain", "medium", "repair"
    ) == ("repair", "medium", True)

    block = runtime.Block(
        pid="p00001", index=0, tag="p", source_html="<p>x</p>",
        source_text="They are hard to put down.", word_count=6,
        digits=[], inline_spans=[],
    )
    cfg = runtime.merge(runtime.DEFAULTS, {
        "validation": {"strict_digits": False, "english_sequence_min_words": 2},
        "ensemble_v31": {"repair": {"max_changed_ratio_span": 0.35}},
    })
    glossary = runtime.Glossary(cfg)
    book = runtime.BookBible(Path(cfg["paths"]["book_bible_file"]))
    det_block = runtime.Block(
        pid="p00004", index=0, tag="p", source_html="<p>Mary nodded.</p>",
        source_text="Mary nodded.", word_count=2, digits=[], inline_spans=[],
    )
    det_issues = runtime.deterministic_issues(
        [det_block], {"p00004": "Mary кивнула."}, cfg, glossary, {}, book
    )
    assert "mixed_script" in {item.category for item in det_issues}
    clean_issues = runtime.deterministic_issues(
        [det_block], {"p00004": "Мэри кивнула."}, cfg, glossary, {}, book
    )
    assert "mixed_script" not in {item.category for item in clean_issues}

    digit_block = runtime.Block(
        pid="p00005", index=0, tag="p", source_html="<p>3 rules.</p>",
        source_text="3 rules.", word_count=2, digits=["3"], inline_spans=[],
    )
    digit_map = {"p00005": digit_block}
    strict_cfg = runtime.merge(cfg, {"validation": {"strict_digits": True}})
    assert not runtime.validate_single_repair(
        "p00005", "Три правила.", digit_map, strict_cfg, "Три старых правила."
    )
    assert runtime.validate_single_repair(
        "p00005", "Четыре правила.", digit_map, strict_cfg, "Три старых правила."
    )
    assert runtime.validate_single_repair(
        "p00005", "Три правила, 4 исключения.", digit_map, strict_cfg, "Три старых правила."
    )

    candidates = v31_repair.parse_candidates({
        "candidates": [{
            "candidate_id": "A", "action": "replace_span",
            "old": "трудно отложить", "new": "трудно прикончить",
            "text": "", "reason": "idiom", "challenge_reason": "",
        }]
    }, "p00001", "Их трудно отложить.", False, runtime, cfg, {"p00001": block})
    assert candidates[0]["after"] == "Их трудно прикончить."

    qgate = v31_postcheck.parse({
        "verdict": "accept", "confidence": "high", "issue_valid": True,
        "faithful_to_source": True, "all_issues_fixed": True,
        "introduced_new_semantic_error": False, "reason": "ok", "feedback": "",
    }, "qwen_semantic", "replace_span")
    assert qgate["faithful_to_source"] and not qgate["introduced_new_semantic_error"]

    extended = {
        "pid": "p00001", "severity": "minor", "category": "grammar",
        "problem": "x", "repair_instruction": "fix", "issue_id": "v31i00001",
        "detected_by": ["gemma_russian_primary"], "detector_families": ["gemma"],
        "verification_confidence": "high", "verification_reason": "yes",
        "required_invariant": "natural",
    }
    runtime.Issue(**v31_finalize_verification.compatible_issue(extended))

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        for name in ("locked.json", "established.json", "provisional.json", "conflicts.json"):
            (root / name).write_text("{}", encoding="utf-8")
        (root / "book.json").write_text("{}", encoding="utf-8")
        (root / "arcs.json").write_text("{}", encoding="utf-8")
        prompt_cfg = runtime.merge(runtime.DEFAULTS, {
            "paths": {
                "glossary_dir": str(root),
                "book_bible_file": str(root / "book.json"),
                "arc_names_file": str(root / "arcs.json"),
            }
        })
        messages = runtime.translation_messages(
            prompt_cfg, runtime.Glossary(prompt_cfg), runtime.BookBible(root / "book.json"),
            {}, {"by_pid": {"p00001": {"idioms": [{"source_span": "put down", "meaning": "kill"}]}}},
            runtime.Chunk("c1", ["p00001"], 6), {"p00001": block}, [], [], [], {}, None,
        )
        assert "SOURCE_ANALYSIS_DO_NOT_TRANSLATE" in messages[1]["content"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    args = parser.parse_args()
    run(args.project_root.resolve())
    print("Pact v3.1 offline self-tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
