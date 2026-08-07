#!/usr/bin/env python3
"""V4 Efficiency A1 — read-only dry-run over an existing run's artifacts.

Card: ``docs/plans/V4_EFFICIENCY_A1_TASK_RU.md`` (acceptance: "Dry-run
скрипт на артефактах run_005: отчёт по отброшенным glossary-парам; замер
input_tokens до/после на тех же чанках").

This script REPLAYS the A1.1 glossary budget and the A1.2 prompt ordering
over a completed run directory *without running the pipeline*: it reads the
persisted ``chunk_plan.json`` / ``generation_outcomes.json`` /
``selection_meta.json`` / ``journal.ndjson``, reconstructs each chunk's
owned_source + left_context + right_context exactly as the strict driver
would, recomputes the deterministic risk pre-screen, and renders both the
pre-A1 prompt (full glossary, old block order) and the post-A1 prompt
(filtered glossary, static-first block order). It then reports:

* per-chunk dropped glossary pairs ("отброшено N пар: [термины]");
* input-token estimate before/after on the same chunks (len(text)/4
  heuristic — the repo ships no tokenizer; the delta % is the signal, not
  the absolute count).

Everything here is a read-only diagnostic: nothing is written to the run
directory, no model is called, no server is started. Optional ``--report-out``
writes the JSON payload to a separate path.

Usage::

    python -m pact_full_pipeline_runner_v1.v4_efficiency_a1_dry_run \\
        --out-dir "<RUN_OUT_DIR>" \\
        --chapter-html "<CHAPTER_HTML>" \\
        --memory-dir "<MEMORY_DIR>"

(``<RUN_OUT_DIR>`` is a completed run directory, e.g. ``run_005``;
``<CHAPTER_HTML>`` is the chapter's source HTML; ``<MEMORY_DIR>`` holds
``glossary.json`` / ``book_memory.json``.)
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pact_v4.phase2.prompts import BALANCED_LITERARY_V1, FIDELITY_FIRST_V1, render_prompt
from pact_v4.phase2.risk import REQUIRED_RISK_CATEGORIES, assess_source_risk
from pact_v4.pipeline._shared_runner_helpers import (
    _glossary_entries,
    _glossary_entries_for_chunk,
    _narrator_glossary_terms,
)
from pact_v4.phase0b.source_html import load_source
from pact_v4.runtime.bible_renderer import extract_narrator_gender, render_bible_section
from pact_v4.runtime.snapshot_factory import ChapterMemory, build_source_artifact

# Rough input-token estimate: ~4 characters per token (no tokenizer in the
# repo; the delta between before/after is the signal, not the absolute
# count). Same heuristic applied to both sides, so the ratio is fair.
CHARS_PER_TOKEN = 4.0

_TEMPLATES = {
    "fidelity_first": FIDELITY_FIRST_V1,
    "balanced_literary": BALANCED_LITERARY_V1,
}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


class _RenderBundle:
    """Minimal render_prompt-compatible bundle (only the fields the
    renderer reads; no PromptBundle identity machinery needed for the
    token estimate)."""

    def __init__(self, **fields: Any) -> None:
        for key, value in fields.items():
            setattr(self, key, value)


def _render_for_chunk(
    *,
    chunk_id: str,
    role: str,
    risk: Any,
    owned_source: Tuple[Tuple[str, str], ...],
    left_context: Tuple[Tuple[str, str], ...],
    right_context: Tuple[Tuple[str, str], ...],
    glossary: Tuple[Any, ...],
    bible_text: str,
) -> str:
    required_risk_feature_codes = tuple(
        sorted({feature.code for feature in risk.features} & REQUIRED_RISK_CATEGORIES)
    )
    bundle = _RenderBundle(
        template=_TEMPLATES[role],
        chunk_id=chunk_id,
        risk_band=risk.band.value,
        owned_source=owned_source,
        left_context=left_context,
        right_context=right_context,
        glossary=tuple((entry.source_term, entry.target_terms) for entry in glossary),
        style_constraints=(),
        bible_text=bible_text,
        required_risk_feature_codes=required_risk_feature_codes,
    )
    return render_prompt(bundle)


def _tokens(text: str) -> int:
    return max(1, int(len(text) / CHARS_PER_TOKEN))


def build_dry_run_report(
    *,
    out_dir: Path,
    chapter_html: Path,
    memory_dir: Path,
) -> Dict[str, Any]:
    """Replay the A1 budget over one run directory; return the report dict."""
    out_dir = Path(out_dir)
    chunk_plan_payload = _read_json(out_dir / "chunk_plan.json")
    generation_payload = _read_json(out_dir / "generation_outcomes.json")
    selection_payload = _read_json(out_dir / "selection_meta.json")
    journal = [
        json.loads(line)
        for line in (out_dir / "journal.ndjson").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    blocks, _raw_sha = load_source(chapter_html)
    source = build_source_artifact(
        chapter_id=generation_payload["chapter_id"], blocks=blocks
    )
    source_map = dict(source.source)
    memory = ChapterMemory.from_directory(memory_dir)
    glossary = _glossary_entries(memory)
    bible_text = render_bible_section(memory.book_memory)
    narrator_gender = extract_narrator_gender(memory.book_memory)
    narrator_source_terms = _narrator_glossary_terms(memory.book_memory)

    chunks = chunk_plan_payload["chunks"]
    by_id = {chunk["chunk_id"]: chunk for chunk in chunks}
    selected_role = {
        rec["chunk_id"]: rec.get("selected_role")
        for rec in selection_payload.get("records", [])
    }
    left_kind = {entry["chunk_id"]: entry.get("left_context_kind") for entry in journal}
    outcome_by_chunk = {
        outcome["chunk_id"]: outcome for outcome in generation_payload["outcomes"]
    }

    per_chunk: List[Dict[str, Any]] = []
    totals = {"chunks": 0, "roles": 0, "tokens_before": 0, "tokens_after": 0}

    for chunk in chunks:
        chunk_id = chunk["chunk_id"]
        owned_source = tuple(
            (pid, source_map[pid]) for pid in chunk["pids"] if pid in source_map
        )
        prev = by_id.get(chunks[chunks.index(chunk) - 1]["chunk_id"]) if chunks.index(chunk) > 0 else None
        if left_kind.get(chunk_id) == "selected" and prev is not None:
            prev_outcome = outcome_by_chunk.get(prev["chunk_id"])
            prev_role = selected_role.get(prev["chunk_id"])
            if prev_outcome and prev_role:
                translation = prev_outcome["candidates"].get(prev_role, {}).get("translation", {})
                left_context = tuple(
                    (pid, translation[pid])
                    for pid in prev["pids"]
                    if pid in translation and translation[pid]
                )
            else:
                left_context = ()
        else:
            left_context = ()
        right_context = tuple(
            (pid, source_map[pid]) for pid in chunk["context"].get("right_en", [])
            if pid in source_map
        )
        chunk_text = " ".join(
            [text for _, text in left_context]
            + [text for _, text in right_context]
            + [text for _, text in owned_source]
        )

        outcome = outcome_by_chunk.get(chunk_id)
        roles = tuple(outcome["expected_roles"]) if outcome else ("fidelity_first", "balanced_literary")
        rows = tuple((pid, text) for pid, text in owned_source)
        risk = assess_source_risk(rows, glossary=glossary, source_complete=True)

        filtered, dropped = _glossary_entries_for_chunk(
            glossary,
            chunk_text=chunk_text,
            risk_feature_codes=(feature.code for feature in risk.features),
            narrator_gender=narrator_gender,
            narrator_source_terms=narrator_source_terms,
        )

        chunk_tokens_before = 0
        chunk_tokens_after = 0
        for role in roles:
            if role not in _TEMPLATES:
                continue
            before = _render_for_chunk(
                chunk_id=chunk_id, role=role, risk=risk,
                owned_source=owned_source, left_context=left_context,
                right_context=right_context, glossary=glossary,
                bible_text=bible_text,
            )
            after = _render_for_chunk(
                chunk_id=chunk_id, role=role, risk=risk,
                owned_source=owned_source, left_context=left_context,
                right_context=right_context, glossary=filtered,
                bible_text=bible_text,
            )
            chunk_tokens_before += _tokens(before)
            chunk_tokens_after += _tokens(after)

        per_chunk.append({
            "chunk_id": chunk_id,
            "roles": list(roles),
            "risk_band": risk.band.value,
            "kept": [entry.source_term for entry in filtered],
            "dropped": list(dropped),
            "dropped_count": len(dropped),
            "tokens_before": chunk_tokens_before,
            "tokens_after": chunk_tokens_after,
        })
        totals["chunks"] += 1
        totals["roles"] += len(roles)
        totals["tokens_before"] += chunk_tokens_before
        totals["tokens_after"] += chunk_tokens_after

    total_dropped = sum(row["dropped_count"] for row in per_chunk)
    delta_pct = (
        (totals["tokens_after"] - totals["tokens_before"]) / totals["tokens_before"] * 100
        if totals["tokens_before"]
        else 0.0
    )
    return {
        "schema": "pact-v4-efficiency-a1-dry-run/v1",
        "run_dir": str(out_dir),
        "chapter_id": generation_payload["chapter_id"],
        "glossary_total": len(glossary),
        "narrator_gender": narrator_gender,
        "tokens_before_total": totals["tokens_before"],
        "tokens_after_total": totals["tokens_after"],
        "tokens_delta_pct": round(delta_pct, 2),
        "pairs_dropped_total": total_dropped,
        "chunks": per_chunk,
    }


def render_report_text(report: Dict[str, Any]) -> str:
    lines = [
        f"== V4 Efficiency A1 dry-run: {report['run_dir']} ==",
        f"chapter: {report['chapter_id']} | glossary pairs: {report['glossary_total']}",
        f"narrator_gender: {report['narrator_gender']}",
        f"input tokens (estimate, len/4): before={report['tokens_before_total']} "
        f"after={report['tokens_after_total']} "
        f"delta={report['tokens_delta_pct']}%",
        f"dropped pairs total: {report['pairs_dropped_total']}",
        "",
        "-- per chunk (dropped N pairs: [terms]) --",
    ]
    for row in report["chunks"]:
        dropped = ", ".join(row["dropped"]) if row["dropped"] else "(none)"
        lines.append(
            f"  {row['chunk_id']} [{row['risk_band']}] roles={row['roles']} "
            f"dropped {row['dropped_count']} pair(s): {dropped} "
            f"(tokens {row['tokens_before']} -> {row['tokens_after']})"
        )
    return "\n".join(lines)


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out-dir", type=Path, required=True,
                   help="Run directory (chunk_plan.json / generation_outcomes.json / ...).")
    p.add_argument("--chapter-html", type=Path, required=True,
                   help="Chapter source HTML used for that run (source_map for owned/right context).")
    p.add_argument("--memory-dir", type=Path, required=True,
                   help="Directory with glossary.json + book_memory.json.")
    p.add_argument("--report-out", type=Path, default=None,
                   help="Optional path to write the JSON report (default: stdout text only).")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_argparser().parse_args(argv)
    report = build_dry_run_report(
        out_dir=args.out_dir, chapter_html=args.chapter_html, memory_dir=args.memory_dir,
    )
    print(render_report_text(report))
    if args.report_out is not None:
        args.report_out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nJSON report written to {args.report_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
