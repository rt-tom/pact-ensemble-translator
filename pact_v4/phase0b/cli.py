"""V4 Phase 0B CLI.

Subcommands:
  extract   parse EN HTML + RU reference -> write draft.json (raw alignment)
  build     turn draft.json into schema-valid golden records
  validate  validate a records file against pact-v4-golden-record/v1
  curate    interactive verdict loop (also scriptable via --input)
  report    print verdict / risk / alignment summary
  sample    print a deterministic subset (helps pick 50–100 curation targets)

All commands are read-mostly. Writes only happen where explicit (draft.json,
records.json). No model calls; no production pipeline access.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from . import SCHEMA_ID
from .alignment import AlignmentPair, align_structural
from .golden_records import build_record, dump_records, load_records
from .reference_epub import (
    ReferenceSegment,
    load_reference_from_epub,
    load_reference_from_path,
)
from .risk import RiskAssessment, assess_risk
from .schema import load_schema, validate as schema_validate
from .source_html import SourceBlock, SourceSpan, load_source


# --- shared helpers ---------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# --- extract ----------------------------------------------------------------

def cmd_extract(args: argparse.Namespace) -> int:
    source_path = Path(args.source_html)
    src_blocks, source_hash = load_source(source_path)

    reference_path = Path(args.reference)
    reference_entry: str | None = args.reference_entry
    references: list[ReferenceSegment]
    if reference_path.suffix.lower() == ".epub":
        if not reference_entry:
            print(
                "error: --reference-entry is required for .epub inputs",
                file=sys.stderr,
            )
            return 2
        references, ref_hash = load_reference_from_epub(
            reference_path, reference_entry,
        )
    else:
        references, ref_hash = load_reference_from_path(reference_path)
        reference_entry = ""

    pairs = align_structural(src_blocks, references)

    source_payload: list[dict[str, Any]] = []
    for b in src_blocks:
        risk = assess_risk(b)
        source_payload.append({
            "pid": b.pid,
            "index": b.index,
            "tag": b.tag,
            "structural_role": b.structural_role,
            "text": b.text,
            "html": b.html,
            "word_count": b.word_count,
            "inline_spans": [
                {
                    "span_id": s.span_id,
                    "tag": s.tag,
                    "text": s.text,
                    "occurrence": s.occurrence,
                    "attrs": s.attrs,
                }
                for s in b.inline_spans
            ],
            "risk": {
                "band": risk.band,
                "types": list(risk.types),
                "signals": risk.signals,
                "score": risk.score,
            },
        })

    draft: dict[str, Any] = {
        "schema_hint": "phase0b/alignment_draft/v1",
        "chapter": args.chapter,
        "generated_at": _now_iso(),
        "provenance": {
            "source_file": str(source_path),
            "reference_file": str(reference_path),
            "reference_entry": reference_entry or "",
            "source_hash": source_hash,
            "reference_hash": ref_hash,
        },
        "source": source_payload,
        "reference": [
            {
                "index": r.index,
                "tag": r.tag,
                "text": r.text,
                "html": r.html,
            }
            for r in references
        ],
        "alignment": [
            {
                "pid": p.pid,
                "source_index": p.source_index,
                "reference_index": p.reference_index,
                "method": p.method,
                "confidence": p.confidence,
                "note": p.note,
            }
            for p in pairs
        ],
    }

    out = Path(args.out_dir) / "draft.json"
    _write_json(out, draft)
    print(
        f"wrote {out}: {len(src_blocks)} source blocks, "
        f"{len(references)} reference segments"
    )
    return 0


# --- build ------------------------------------------------------------------

def _select_pids(
    source: list[dict[str, Any]],
    *,
    max_count: int | None,
) -> list[str]:
    if max_count is None or max_count >= len(source):
        return [entry["pid"] for entry in source]
    # Deterministic selection: keep all high, then all med, then a stride
    # across low so we cover the chapter uniformly.
    highs = [e["pid"] for e in source if e["risk"]["band"] == "high"]
    meds = [e["pid"] for e in source if e["risk"]["band"] == "med"]
    lows = [e["pid"] for e in source if e["risk"]["band"] == "low"]
    chosen: list[str] = []
    for pid in highs:
        if len(chosen) >= max_count:
            break
        chosen.append(pid)
    for pid in meds:
        if len(chosen) >= max_count:
            break
        chosen.append(pid)
    remaining = max_count - len(chosen)
    if remaining > 0 and lows:
        stride = max(1, len(lows) // remaining)
        for i in range(0, len(lows), stride):
            if len(chosen) >= max_count:
                break
            chosen.append(lows[i])
    return chosen[:max_count]


def cmd_build(args: argparse.Namespace) -> int:
    draft = _load_json(Path(args.in_dir) / "draft.json")
    chapter = draft.get("chapter") or args.chapter
    if not chapter:
        print("error: chapter missing in draft and --chapter", file=sys.stderr)
        return 2

    provenance = dict(draft["provenance"])
    reference_entries: dict[int, dict[str, Any]] = {
        r["index"]: r for r in draft["reference"]
    }
    alignment_entries: dict[str, dict[str, Any]] = {
        a["pid"]: a for a in draft["alignment"]
    }

    ordered_source = list(draft["source"])
    keep = set(_select_pids(ordered_source, max_count=args.max_count))

    records: list[dict[str, Any]] = []
    for entry in ordered_source:
        if entry["pid"] not in keep:
            continue
        spans = tuple(
            SourceSpan(
                span_id=s["span_id"],
                tag=s["tag"],
                text=s["text"],
                occurrence=s["occurrence"],
                attrs=dict(s.get("attrs") or {}),
            )
            for s in entry.get("inline_spans", [])
        )
        block = SourceBlock(
            pid=entry["pid"],
            index=entry["index"],
            tag=entry["tag"],
            text=entry["text"],
            html=entry.get("html", ""),
            structural_role=entry["structural_role"],
            inline_spans=spans,
            word_count=entry["word_count"],
        )
        risk_entry = entry["risk"]
        risk = RiskAssessment(
            band=risk_entry["band"],
            types=tuple(risk_entry.get("types", [])),
            signals=dict(risk_entry.get("signals", {})),
            score=risk_entry.get("score", 0),
        )
        align_entry = alignment_entries.get(entry["pid"])
        if align_entry is None:
            pair = AlignmentPair(
                pid=entry["pid"],
                source_index=entry["index"],
                reference_index=None,
                method="none",
                confidence=0.0,
                note="no alignment entry in draft",
            )
        else:
            pair = AlignmentPair(
                pid=align_entry["pid"],
                source_index=align_entry["source_index"],
                reference_index=align_entry["reference_index"],
                method=align_entry["method"],
                confidence=align_entry["confidence"],
                note=align_entry.get("note"),
            )
        ref_seg: ReferenceSegment | None = None
        if pair.reference_index is not None:
            ref_entry = reference_entries.get(pair.reference_index)
            if ref_entry is not None:
                ref_seg = ReferenceSegment(
                    index=ref_entry["index"],
                    tag=ref_entry["tag"],
                    text=ref_entry["text"],
                    html=ref_entry.get("html", ""),
                )
        record = build_record(
            chapter=chapter,
            source=block,
            risk=risk,
            pair=pair,
            reference=ref_seg,
            provenance=provenance,
        )
        records.append(record)

    out_path = Path(args.out or Path(args.in_dir) / "records.json")
    dump_records(records, out_path)
    print(f"wrote {out_path}: {len(records)} golden records")
    return 0


# --- validate ---------------------------------------------------------------

def cmd_validate(args: argparse.Namespace) -> int:
    records = load_records(Path(args.records))
    schema = load_schema()
    ok = True
    for i, record in enumerate(records):
        errs = schema_validate(record, schema)
        if errs:
            ok = False
            print(f"record[{i}] ({record.get('record_id', '?')}):")
            for e in errs:
                print(f"  {e}")
    if ok:
        print(f"OK: {len(records)} records valid against {SCHEMA_ID}")
        return 0
    return 1


# --- report -----------------------------------------------------------------

def cmd_report(args: argparse.Namespace) -> int:
    records = load_records(Path(args.records))
    verdicts: dict[str, int] = {}
    risks: dict[str, int] = {}
    align_methods: dict[str, int] = {}
    for r in records:
        verdicts[r["verdict"]["status"]] = verdicts.get(
            r["verdict"]["status"], 0,
        ) + 1
        risks[r["risk"]["band"]] = risks.get(r["risk"]["band"], 0) + 1
        method = r["reference"]["alignment"]["method"]
        align_methods[method] = align_methods.get(method, 0) + 1
    print(f"total: {len(records)}")
    print("verdict:")
    for k in sorted(verdicts):
        print(f"  {k:14s} {verdicts[k]}")
    print("risk:")
    for k in ("low", "med", "high", "unknown"):
        if k in risks:
            print(f"  {k:14s} {risks[k]}")
    print("alignment method:")
    for k in sorted(align_methods):
        print(f"  {k:18s} {align_methods[k]}")
    return 0


# --- sample -----------------------------------------------------------------

def cmd_sample(args: argparse.Namespace) -> int:
    draft = _load_json(Path(args.in_dir) / "draft.json")
    pids = _select_pids(draft["source"], max_count=args.max_count)
    for pid in pids:
        print(pid)
    return 0


# --- curate -----------------------------------------------------------------

_CURATE_HELP = (
    "verdicts: a=accept  n=needs_review  r=reject  s=skip  q=quit"
)


def _next_iteration(
    records: list[dict[str, Any]],
    *,
    include_accepted: bool,
    include_rejected: bool,
) -> Iterator[int]:
    for i, r in enumerate(records):
        status = r["verdict"]["status"]
        if status == "accepted" and not include_accepted:
            continue
        if status == "rejected" and not include_rejected:
            continue
        yield i


def _apply_verdict(
    record: dict[str, Any], action: str, reviewer: str, note: str,
) -> bool:
    mapping = {"a": "accepted", "n": "needs_review", "r": "rejected"}
    if action not in mapping:
        return False
    record["verdict"]["status"] = mapping[action]
    record["verdict"]["reviewer"] = reviewer
    record["verdict"]["reviewed_at"] = _now_iso()
    if note:
        record["verdict"]["notes"] = note
    return True


def cmd_curate(args: argparse.Namespace) -> int:
    records_path = Path(args.records)
    records = load_records(records_path)
    total = len(records)

    if args.input:
        script: Sequence[str] = Path(args.input).read_text(
            encoding="utf-8",
        ).splitlines()
        script_iter = iter(script)

        def next_action(_prompt: str) -> str:
            try:
                return next(script_iter).strip()
            except StopIteration:
                return "q"
    else:
        def next_action(prompt: str) -> str:
            try:
                return input(prompt).strip()
            except EOFError:
                return "q"

    print(_CURATE_HELP)
    for idx in _next_iteration(
        records,
        include_accepted=args.include_accepted,
        include_rejected=args.include_rejected,
    ):
        r = records[idx]
        src = r["source"]["text"]
        ref = r["reference"]["text"]
        risk = r["risk"]
        conf = r["reference"]["alignment"]["confidence"]
        method = r["reference"]["alignment"]["method"]
        print("-" * 72)
        print(
            f"[{idx + 1}/{total}] {r['record_id']}  "
            f"risk={risk['band']} types={','.join(risk['types']) or '-'}  "
            f"align={method}({conf:.2f})"
        )
        print(f"  EN: {src}")
        print(f"  RU: {ref}")
        inv = r["invariants"]["must_preserve"]
        if inv:
            print("  invariants:")
            for item in inv:
                note = f" ({item['note']})" if item.get("note") else ""
                print(f"    - {item['kind']}={item['value']!r}{note}")
        action = next_action("verdict [a/n/r/s/q] > ").lower()
        if action in {"q", ""}:
            break
        if action == "s":
            continue
        if action not in {"a", "n", "r"}:
            print(f"  (unknown action {action!r}; skipping)")
            continue
        note = ""
        if not args.input and args.ask_notes:
            note = input("  note (optional) > ").strip()
        _apply_verdict(r, action, args.reviewer, note)

    dump_records(records, records_path)
    return 0


# --- parser -----------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pact_v4.phase0b.cli",
        description="V4 Phase 0B golden-set tooling (read-only).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    x = sub.add_parser("extract", help="parse EN + RU into draft.json")
    x.add_argument("--source-html", required=True)
    x.add_argument("--reference", required=True,
                   help=".epub file or .xhtml/.html path")
    x.add_argument("--reference-entry", default=None,
                   help="internal entry (required for .epub)")
    x.add_argument("--chapter", required=True,
                   help="chapter id, e.g. 044")
    x.add_argument("--out-dir", required=True)
    x.set_defaults(func=cmd_extract)

    b = sub.add_parser("build", help="draft.json -> records.json (schema)")
    b.add_argument("--in-dir", required=True)
    b.add_argument("--chapter", default=None)
    b.add_argument("--max-count", type=int, default=100,
                   help="cap number of records (default 100)")
    b.add_argument("--out", default=None)
    b.set_defaults(func=cmd_build)

    v = sub.add_parser("validate", help="check records against schema")
    v.add_argument("--records", required=True)
    v.set_defaults(func=cmd_validate)

    r = sub.add_parser("report", help="verdict/risk/alignment summary")
    r.add_argument("--records", required=True)
    r.set_defaults(func=cmd_report)

    s = sub.add_parser("sample", help="print curation PIDs for a max count")
    s.add_argument("--in-dir", required=True)
    s.add_argument("--max-count", type=int, default=100)
    s.set_defaults(func=cmd_sample)

    c = sub.add_parser("curate", help="interactive verdict loop")
    c.add_argument("--records", required=True)
    c.add_argument("--reviewer", required=True,
                   help="short reviewer id, e.g. initials")
    c.add_argument("--input", default=None,
                   help="scripted actions file (one per line); useful for tests")
    c.add_argument("--include-accepted", action="store_true")
    c.add_argument("--include-rejected", action="store_true")
    c.add_argument("--ask-notes", action="store_true")
    c.set_defaults(func=cmd_curate)

    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
