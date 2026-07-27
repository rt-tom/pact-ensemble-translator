#!/usr/bin/env python3
"""Explicit artifact dependency plan for v3.1 redo and safe resume.

This module deliberately does not decide cache identity.  Model-stage cache
reuse remains owned by ``v31_common.cache_reuse``; this graph only determines
which authoritative artifacts can no longer be trusted after an explicit redo.
"""
from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass
from pathlib import Path


REUSE, INVALIDATE, RECOMPUTE, BLOCKED = "REUSE", "INVALIDATE", "RECOMPUTE", "BLOCKED"


@dataclass(frozen=True)
class Node:
    name: str
    # Direct data inputs only: ordering in the runner is not a dependency.
    needs: tuple[str, ...]
    artifacts: tuple[str, ...]


NODES = (
    Node("source_analysis", (), ("source_scene_map.json", "v31_source_analysis")),
    Node("translation", ("source_analysis",), ("drafts", "meta", "draft_translations.json", "@run/book_consistency_ledger.json")),
    Node("primary_audit", ("translation",), ("v31/primary", "issues.json", "verified_issues.json")),
    Node("primary_repair", ("primary_audit",), ("repaired_translations.json", "repaired_translations.preverify.json", "repair_records.json", "post_repair_report.json", "issue_lifecycle.json", "v31_primary_translations.json")),
    Node("residual_audit", ("primary_repair",), ("v31/residual",)),
    Node("residual_repair", ("residual_audit",), ("v31_final_translations.json",)),
    # Terminal status remains authoritative for the current run identity across
    # a redo-quality.  Reset creates a new run identity and removes it instead.
    Node("final_quality", ("residual_repair",), ("v31/final", "v31_final_changed_pid_ledger.json", "v31_pre_final_repair_translations.json", "quality_report.json")),
    # review is an independent diagnostic consumer; it is intentionally not a finalization input.
    Node("review", ("final_quality",), ("audit_report.html",)),
    Node("finalization", ("final_quality",), ("output",)),
)
BY_NAME = {node.name: node for node in NODES}
REDO_TARGETS = {
    "source": "source_analysis", "translation": "translation", "quality": "primary_audit",
    "formatting": "finalization",
}


def affected(targets: set[str]) -> set[str]:
    """Return target nodes plus transitive *direct* consumers."""
    result = set(targets)
    changed = True
    while changed:
        changed = False
        for node in NODES:
            if node.name not in result and set(node.needs) & result:
                result.add(node.name)
                changed = True
    return result


def plan(*, redo_source=False, redo_translation=False, redo_quality=False, redo_formatting=False) -> list[dict[str, object]]:
    requested = {
        REDO_TARGETS[key] for key, enabled in {
            "source": redo_source, "translation": redo_translation,
            "quality": redo_quality, "formatting": redo_formatting,
        }.items() if enabled
    }
    invalid = affected(requested)
    rows = []
    for node in NODES:
        if node.name in requested:
            action, reason = INVALIDATE, "explicit_redo"
        elif node.name in invalid:
            parents = sorted(set(node.needs) & invalid)
            action, reason = INVALIDATE, "direct_dependency:" + ",".join(parents)
        else:
            action, reason = REUSE, "no_invalidated_direct_dependency"
        rows.append({"stage": node.name, "action": action, "reason": reason, "artifacts": list(node.artifacts)})
    return rows


def apply(work_dir: Path, output_dir: Path, run_root: Path, rows: list[dict[str, object]]) -> None:
    """Remove only invalidated authoritative artifacts for selected chapters."""
    for work in sorted(path for path in work_dir.iterdir() if path.is_dir()):
        for row in rows:
            if row["action"] != INVALIDATE:
                continue
            for relative in row["artifacts"]:
                path = output_dir if relative == "output" else (run_root / relative[5:] if relative.startswith("@run/") else work / relative)
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.exists():
                    path.unlink()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--redo-source-analysis", action="store_true")
    parser.add_argument("--redo-translation", action="store_true")
    parser.add_argument("--redo-quality", action="store_true")
    parser.add_argument("--redo-formatting", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    rows = plan(redo_source=args.redo_source_analysis, redo_translation=args.redo_translation,
                redo_quality=args.redo_quality, redo_formatting=args.redo_formatting)
    print(json.dumps({"dry_run": not args.apply, "plan": rows}, ensure_ascii=False, indent=2))
    if args.apply:
        apply(args.work_dir, args.output_dir, args.run_root, rows)


if __name__ == "__main__":
    main()
