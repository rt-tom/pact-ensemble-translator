"""Versioned deterministic source-only risk pre-screen (Phase 2A).

This module deliberately knows nothing about candidates, generator confidence,
models, or APIs.  It classifies an ordered chunk/scene source PID-map using a
small, auditable rule policy.  The provisional weights and thresholds are kept
in one immutable policy object so the benchmark gate can replace them by
publishing a new policy version rather than silently changing behaviour.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence


class RiskBand(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# Required for every V4 risk policy (Phase 0C Gate policy record, §2,
# docs/plans/V4_PHASE_0C_GATE_NOTE_RU.md): Track B's deterministic
# consistency gate treats these two as required categories, so the
# source-only risk pre-screen must always be able to flag them too.
# A policy that omits either from its weights fails to construct.
REQUIRED_RISK_CATEGORIES = frozenset({"number_word", "tone_profanity"})


@dataclass(frozen=True)
class RiskPolicy:
    version: str
    weights: Mapping[str, int]
    medium_threshold: int
    high_threshold: int
    force_high: frozenset[str]

    def __post_init__(self) -> None:
        missing = REQUIRED_RISK_CATEGORIES - set(self.weights)
        if missing:
            raise ValueError(
                f"RiskPolicy {self.version}: missing required risk categories "
                f"{sorted(missing)} (Phase 0C Gate policy requires "
                f"{sorted(REQUIRED_RISK_CATEGORIES)} in every policy's weights)"
            )


# Thresholds are intentionally provisional until the Phase-2 benchmark gate.
# MappingProxyType prevents runtime mutation from making equal inputs diverge.
RISK_POLICY = RiskPolicy(
    version="pact-v4-risk-source-en/v1",
    weights=MappingProxyType({
        "idiom_or_metaphor": 3,
        "cultural_reference": 3,
        "address_t_v_ambiguity": 2,
        "ambiguous_referent": 3,
        "negation": 2,
        "numbers": 2,
        "number_word": 3,
        "tone_profanity": 4,
        "glossary_conflict": 5,
        "unknown_glossary_context": 3,
        "incomplete_glossary_context": 3,
        "missing_source": 7,
        "incomplete_source": 7,
    }),
    medium_threshold=3,
    high_threshold=7,
    force_high=frozenset({"missing_source", "incomplete_source"}),
)


@dataclass(frozen=True)
class GlossaryEntry:
    """Frozen source-side glossary constraint available before generation."""

    source_term: str
    target_terms: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.source_term, str) or not self.source_term.strip():
            raise ValueError("source_term must be a non-empty string")
        if not self.target_terms or any(
            not isinstance(term, str) or not term.strip() for term in self.target_terms
        ):
            raise ValueError("target_terms must contain non-empty strings")


@dataclass(frozen=True)
class RiskFeature:
    code: str
    weight: int
    explanation: str
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class RiskAssessment:
    policy_version: str
    band: RiskBand
    score: int
    features: tuple[RiskFeature, ...]


_IDIOM_PATTERNS = (
    r"\b(?:break the ice|spill the beans|piece of cake|hit the nail on the head)\b",
    r"\b(?:under the weather|once in a blue moon|costs? an arm and a leg)\b",
)
_METAPHOR_MARKERS = re.compile(
    r"\b(?:as if|as though|metaphorically|figuratively|symbolically)\b",
    re.IGNORECASE,
)
_CULTURAL_RE = re.compile(
    r"\b(?:Thanksgiving|Halloween|Super Bowl|Ivy League|Downing Street|"
    r"Whitehall|Broadway|the Bible|the Quran|the Koran|Christmas|Easter)\b",
    re.IGNORECASE,
)
_ADDRESS_RE = re.compile(r"\b(?:you|your|yours|yourself|yourselves)\b", re.IGNORECASE)
_THIRD_PERSON_RE = re.compile(
    r"\b(?:he|she|him|her|his|hers|they|them|their|theirs|it|its|this|that)\b",
    re.IGNORECASE,
)
_NAME_RE = re.compile(r"(?<![.!?]\s)\b[A-Z][a-z]+\b")
_NEGATION_RE = re.compile(
    r"\b(?:no|not|never|neither|nor|none|nothing|nobody|nowhere|without|hardly|barely)\b",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(
    r"(?<!\w)(?:[$£€¥]\s*)?[+-]?(?:\d{1,3}(?:[,.]\d{3})+|\d+)(?:[.,]\d+)?%?(?!\w)"
)
# Written-out cardinal number words. "one", "half" and "quarter" are
# deliberately excluded (frequently pronominal or idiomatic, not a number
# that needs exact preservation) -- same exclusion v3's deterministic gate
# applies to its target-side missing_written_number_words check.
_NUMBER_WORD_RE = re.compile(
    r"\b(?:two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
    r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|"
    r"thousand)\b",
    re.IGNORECASE,
)
_PROFANITY_RE = re.compile(
    r"\b(?:fuck(?:ing|ed|er)?|shit(?:ty)?|cunt|bitch|damn(?:ed)?|"
    r"bastard|asshole)\b",
    re.IGNORECASE,
)


def _snippets(pattern: re.Pattern[str], text: str, limit: int = 3) -> tuple[str, ...]:
    return tuple(match.group(0) for match in list(pattern.finditer(text))[:limit])


def _term_present(text: str, term: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, re.IGNORECASE) is not None


def source_term_required_categories(source_term: str) -> frozenset[str]:
    """Required-risk categories a glossary source term is tied to.

    A1.1 (glossary budgeter) keeps every entry whose source term belongs to
    a category the chunk's risk pre-screen flagged as required — an entry
    like ``"fuck"`` is exactly what the ``tone_profanity`` constraint
    prescribes, and ``"hundred"`` what ``number_word`` prescribes, so they
    must never be dropped from the prompt while the chunk carries that
    category. Returns a subset of ``REQUIRED_RISK_CATEGORIES``
    (``{"number_word", "tone_profanity"}``); the same frozen regexes that
    drive the pre-screen are used, so an entry cannot disagree with the
    category it is claimed to protect.
    """
    codes = set()
    if _NUMBER_WORD_RE.search(source_term):
        codes.add("number_word")
    if _PROFANITY_RE.search(source_term):
        codes.add("tone_profanity")
    return frozenset(codes)


def _glossary_conflicts(
    text: str, glossary: Sequence[GlossaryEntry]
) -> tuple[str, ...]:
    by_source: dict[str, set[str]] = {}
    display: dict[str, str] = {}
    for entry in glossary:
        key = entry.source_term.casefold().strip()
        if not _term_present(text, entry.source_term.strip()):
            continue
        display.setdefault(key, entry.source_term.strip())
        by_source.setdefault(key, set()).update(
            target.casefold().strip() for target in entry.target_terms
        )
    return tuple(
        f"{display[key]} -> {len(targets)} target terms"
        for key, targets in sorted(by_source.items())
        if len(targets) > 1
    )


def assess_source_risk(
    source: Iterable[tuple[str, str]] | None,
    *,
    glossary: Sequence[GlossaryEntry] | None,
    source_complete: bool = True,
    policy: RiskPolicy = RISK_POLICY,
) -> RiskAssessment:
    """Classify an ordered chunk/scene source map with stable explanations.

    ``glossary=None`` explicitly means that glossary state is unknown.  Pass an
    empty tuple only when the authoritative snapshot is known to contain no
    entries.  Invalid or partial source is not guessed through: it produces a
    conservative high-risk assessment with an explicit feature.
    """
    features: list[RiskFeature] = []

    def add(code: str, explanation: str, evidence: Iterable[str]) -> None:
        values = tuple(dict.fromkeys(str(item) for item in evidence))
        features.append(RiskFeature(code, policy.weights[code], explanation, values))

    if source is None:
        add("missing_source", "Source PID-map is unavailable.", ("source=None",))
        rows: tuple[tuple[str, str], ...] = ()
    else:
        rows = tuple(source)
        invalid = [
            f"row[{index}]"
            for index, row in enumerate(rows)
            if not isinstance(row, (tuple, list))
            or len(row) != 2
            or not isinstance(row[0], str)
            or not row[0].strip()
            or not isinstance(row[1], str)
            or not row[1].strip()
        ]
        pids = [row[0] for row in rows if isinstance(row, (tuple, list)) and len(row) == 2]
        duplicates = tuple(
            dict.fromkeys(str(pid) for pid in pids if pids.count(pid) > 1)
        )
        if not rows or invalid or duplicates or not source_complete:
            evidence = invalid + [f"duplicate PID: {pid}" for pid in duplicates]
            if not rows:
                evidence.append("empty PID-map")
            if not source_complete:
                evidence.append("source_complete=False")
            add("incomplete_source", "Source PID-map is incomplete or invalid.", evidence)

    valid_texts = [
        row[1] for row in rows
        if isinstance(row, (tuple, list)) and len(row) == 2 and isinstance(row[1], str)
    ]
    text = "\n".join(valid_texts)
    lowered = text.casefold()

    idioms = tuple(
        match.group(0)
        for pattern in _IDIOM_PATTERNS
        for match in re.finditer(pattern, text, re.IGNORECASE)
    ) + _snippets(_METAPHOR_MARKERS, text)
    if idioms:
        add("idiom_or_metaphor", "Idiom or explicit figurative marker needs non-literal handling.", idioms[:3])

    cultural = _snippets(_CULTURAL_RE, text)
    if cultural:
        add("cultural_reference", "Culture-bound reference may need contextual rendering.", cultural)

    address = _snippets(_ADDRESS_RE, text)
    if address:
        add("address_t_v_ambiguity", "English second-person address does not encode Russian ты/вы.", address)

    referents = _snippets(_THIRD_PERSON_RE, text)
    # Prime the lookbehind so the very first word of the whole text is
    # treated as sentence-initial too, not just words after mid-text ". ".
    names = tuple(dict.fromkeys(_NAME_RE.findall(". " + text)))
    if referents and len(names) >= 2:
        add("ambiguous_referent", "Pronoun/demonstrative has multiple plausible discourse referents.", referents + names[:2])

    negations = _snippets(_NEGATION_RE, text)
    if negations:
        add("negation", "Negation or negative-polarity marker must preserve scope.", negations)

    numbers = _snippets(_NUMBER_RE, text)
    if numbers:
        add("numbers", "Numeric value or sign requires exact preservation.", numbers)

    number_words = _snippets(_NUMBER_WORD_RE, text)
    if number_words:
        add(
            "number_word",
            "Written-out number expression requires an exact, non-omitted equivalent.",
            number_words,
        )

    profanity = _snippets(_PROFANITY_RE, text)
    if profanity:
        add(
            "tone_profanity",
            "Strong source profanity/tone must not be softened or omitted.",
            profanity,
        )

    if glossary is None:
        add("unknown_glossary_context", "Authoritative glossary state was not supplied.", ("glossary=None",))
    else:
        glossary_rows = tuple(glossary)
        invalid_glossary = tuple(
            f"glossary[{index}]={type(entry).__name__}"
            for index, entry in enumerate(glossary_rows)
            if not isinstance(entry, GlossaryEntry)
        )
        if invalid_glossary:
            add(
                "incomplete_glossary_context",
                "Glossary context contains entries outside the frozen Phase-2A contract.",
                invalid_glossary,
            )
        conflicts = _glossary_conflicts(
            lowered,
            tuple(entry for entry in glossary_rows if isinstance(entry, GlossaryEntry)),
        )
        if conflicts:
            add("glossary_conflict", "A present source term has multiple prescribed target forms.", conflicts)

    score = sum(feature.weight for feature in features)
    codes = {feature.code for feature in features}
    if codes & policy.force_high or score >= policy.high_threshold:
        band = RiskBand.HIGH
    elif score >= policy.medium_threshold:
        band = RiskBand.MEDIUM
    else:
        band = RiskBand.LOW
    return RiskAssessment(policy.version, band, score, tuple(features))
