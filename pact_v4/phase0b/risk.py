"""Deterministic, source-only risk pre-screen for the golden set.

Model-free; matches v4 spec §4 (main lever for downstream speed) at a
Phase-0B fidelity: we only need enough signal to pick the 50–100 PIDs that
are worth curating, and to auto-flag them ``needs_review`` when banded
``high``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .source_html import SourceBlock

RISK_TYPES: frozenset[str] = frozenset({
    "numbers", "negation", "modality", "names", "idiom", "cultural",
    "dialogue", "ty_vy", "referent", "glossary_conflict", "mixed_script",
    "formatting", "quotation", "temporal", "measurement", "code_switch",
    "wordplay", "long_span", "structural",
})

# --- Deterministic detectors (source-only) ----------------------------------

NUMBER_RE = re.compile(r"\b\d[\d,\.]*\b")
NEGATION_RE = re.compile(
    r"\b(?:no|not|never|nothing|none|nobody|nowhere|neither|nor)\b",
    re.IGNORECASE,
)
MODALITY_RE = re.compile(
    r"\b(?:can|could|may|might|must|shall|should|will|would|ought)\b",
    re.IGNORECASE,
)
QUOTE_CHARS_RE = re.compile(r"[\"“”«»]")
TEMPORAL_RE = re.compile(
    r"\b(?:yesterday|today|tomorrow|morning|evening|midnight|noon|"
    r"minute|hour|second|day|week|month|year|monday|tuesday|"
    r"wednesday|thursday|friday|saturday|sunday|"
    r"january|february|march|april|may|june|july|august|september|"
    r"october|november|december)s?\b",
    re.IGNORECASE,
)
MEASUREMENT_RE = re.compile(
    r"\b\d+\s?(?:cm|mm|km|kg|lb|oz|inch|inches|foot|feet|mile|miles|"
    r"yard|yards|pint|pints|gallon|gallons|litre|litres|liter|liters)\b",
    re.IGNORECASE,
)
PROPER_NAME_RE = re.compile(r"\b(?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})\b")


@dataclass(frozen=True)
class RiskAssessment:
    band: str            # "low" | "med" | "high"
    types: tuple[str, ...]
    signals: dict[str, int]
    score: int


def _count_proper_names(text: str) -> int:
    matches = PROPER_NAME_RE.findall(text)
    # Sentence-start capitalisation is not a reliable name signal; drop the
    # first token if it matches a name-shaped pattern only because it starts
    # the block.
    if matches and text.startswith(matches[0] + " "):
        matches = matches[1:]
    return len(matches)


def assess_risk(block: SourceBlock) -> RiskAssessment:
    text = block.text
    signals: dict[str, int] = {}
    ordered_types: list[str] = []

    def record(key: str, count: int) -> None:
        if count <= 0:
            return
        signals[key] = count
        if key in RISK_TYPES and key not in ordered_types:
            ordered_types.append(key)

    record("numbers", len(NUMBER_RE.findall(text)))
    record("negation", len(NEGATION_RE.findall(text)))
    record("modality", len(MODALITY_RE.findall(text)))
    record("quotation", len(QUOTE_CHARS_RE.findall(text)))
    record("temporal", len(TEMPORAL_RE.findall(text)))
    record("measurement", len(MEASUREMENT_RE.findall(text)))
    record("names", _count_proper_names(text))
    record("formatting", len(block.inline_spans))
    if block.structural_role == "dialogue":
        record("dialogue", 1)
    if block.word_count >= 80:
        record("long_span", block.word_count)

    # Explainable band: weighted-sum but with published integer weights.
    score = (
        signals.get("numbers", 0) * 2
        + signals.get("measurement", 0) * 2
        + signals.get("negation", 0)
        + signals.get("modality", 0)
        + signals.get("names", 0)
        + signals.get("temporal", 0)
        + (1 if signals.get("dialogue") else 0)
        + (2 if signals.get("formatting", 0) >= 2 else 0)
        + (1 if signals.get("long_span") else 0)
    )
    if score >= 6:
        band = "high"
    elif score >= 2:
        band = "med"
    else:
        band = "low"

    return RiskAssessment(
        band=band,
        types=tuple(ordered_types),
        signals=signals,
        score=score,
    )
