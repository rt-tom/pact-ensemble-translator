"""Structural EN↔RU alignment for the golden-set draft.

Alignment is deliberately simple and explainable:

  * Equal-length lists → 1:1 by structural order (confidence 0.9).
  * Unequal-length lists → proportional index projection
    (``heuristic_length``, confidence 0.35). Every such pair carries a
    ``needs_review`` hint so a human can confirm.
  * No content → confidence 0.0, method ``none``.

Cleverer semantic alignment is out of scope for Phase 0B; the human RU
translation is a *reference*, not a ground truth (v4 spec §8.1, §8.2).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .reference_epub import ReferenceSegment
from .source_html import SourceBlock

STRUCTURAL_CONFIDENCE = 0.9
HEURISTIC_CONFIDENCE = 0.35


@dataclass(frozen=True)
class AlignmentPair:
    pid: str
    source_index: int
    reference_index: int | None
    method: str
    confidence: float
    note: str | None


def align_structural(
    sources: Sequence[SourceBlock],
    references: Sequence[ReferenceSegment],
) -> list[AlignmentPair]:
    pairs: list[AlignmentPair] = []
    n_src = len(sources)
    n_ref = len(references)
    same_len = n_src == n_ref and n_src > 0
    for src in sources:
        ref_idx: int | None = None
        method = "none"
        confidence = 0.0
        note: str | None = None
        if same_len:
            ref_idx = src.index + 1
            method = "structural_order"
            confidence = STRUCTURAL_CONFIDENCE
        elif n_ref > 0:
            denom = max(1, n_src - 1)
            approx = round(src.index * (n_ref - 1) / denom)
            ref_idx = max(1, min(n_ref, approx + 1))
            method = "heuristic_length"
            confidence = HEURISTIC_CONFIDENCE
            note = (
                f"source and reference block counts differ "
                f"({n_src} vs {n_ref}); needs manual verification"
            )
        else:
            note = "no reference segments available"
        pairs.append(AlignmentPair(
            pid=src.pid,
            source_index=src.index,
            reference_index=ref_idx,
            method=method,
            confidence=confidence,
            note=note,
        ))
    return pairs
