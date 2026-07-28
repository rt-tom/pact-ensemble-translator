"""Golden-record assembly and IO for ``pact-v4-golden-record/v1``.

Auto-verdict policy (never sets ``accepted`` automatically):

  * risk band ``high`` → ``needs_review``
  * alignment confidence < 0.8 → ``needs_review``
  * otherwise → ``unreviewed``

Atomic write: uses temp-file + ``os.replace`` so a crash mid-write does not
leave a truncated records file on disk.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import SCHEMA_ID, TOOL_VERSION
from .alignment import AlignmentPair
from .reference_epub import ReferenceSegment
from .risk import NUMBER_RE, RiskAssessment
from .source_html import SourceBlock


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _build_invariants(block: SourceBlock) -> list[dict[str, Any]]:
    invariants: list[dict[str, Any]] = []
    for number in NUMBER_RE.findall(block.text):
        invariants.append({"kind": "number", "value": number})
    for span in block.inline_spans:
        invariants.append({
            "kind": "inline_span",
            "value": span.text,
            "note": f"tag=<{span.tag}> occurrence={span.occurrence}",
        })
    return invariants


def _auto_verdict(risk: RiskAssessment, pair: AlignmentPair) -> str:
    if risk.band == "high":
        return "needs_review"
    if pair.confidence < 0.8:
        return "needs_review"
    return "unreviewed"


def build_record(
    *,
    chapter: str,
    source: SourceBlock,
    risk: RiskAssessment,
    pair: AlignmentPair,
    reference: ReferenceSegment | None,
    provenance: dict[str, str],
) -> dict[str, Any]:
    alignment: dict[str, Any] = {
        "method": pair.method,
        "confidence": pair.confidence,
    }
    if pair.reference_index is not None:
        alignment["reference_index"] = pair.reference_index
    if pair.note:
        alignment["notes"] = pair.note

    reference_payload: dict[str, Any] = {
        "text": reference.text if reference else "",
        "html": reference.html if reference else "",
        "source": "human_translation_epub",
        "note": "reference only; not an exact-match ground truth",
        "alignment": alignment,
    }

    record: dict[str, Any] = {
        "schema": SCHEMA_ID,
        "record_id": f"ch{chapter}-{source.pid}",
        "chapter": chapter,
        "pid": source.pid,
        "source": {
            "language": "en",
            "text": source.text,
            "html": source.html,
            "structural_role": source.structural_role,
            "inline_spans": [
                {
                    "span_id": s.span_id,
                    "tag": s.tag,
                    "text": s.text,
                    "occurrence": s.occurrence,
                    "attrs": s.attrs,
                }
                for s in source.inline_spans
            ],
            "word_count": source.word_count,
        },
        "risk": {
            "band": risk.band,
            "types": list(risk.types),
            "signals": dict(risk.signals),
        },
        "invariants": {
            "must_preserve": _build_invariants(source),
            "must_not_add": [],
            "formatting_expectation": {
                "required_spans": [
                    {
                        "span_id": s.span_id,
                        "tag": s.tag,
                        "occurrence": s.occurrence,
                    }
                    for s in source.inline_spans
                ],
            },
        },
        "known_violations": [],
        "reference": reference_payload,
        "verdict": {"status": _auto_verdict(risk, pair)},
        "provenance": {
            "source_file": provenance["source_file"],
            "reference_file": provenance["reference_file"],
            "reference_entry": provenance.get("reference_entry", ""),
            "source_hash": provenance["source_hash"],
            "reference_hash": provenance["reference_hash"],
            "tool_version": TOOL_VERSION,
            "generated_at": _now_iso(),
        },
    }
    return record


def dump_records(records: Iterable[dict[str, Any]], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(list(records), ensure_ascii=False, indent=2)
    # Atomic write: same directory so os.replace stays on one filesystem.
    fd, tmp_name = tempfile.mkstemp(
        prefix=out_path.name + ".", suffix=".tmp", dir=str(out_path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, out_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def load_records(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))
