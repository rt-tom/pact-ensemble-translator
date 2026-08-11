"""Phase 5: translation-time formatting alignment (the §8.14 span contract).

Backing spec:

  * ``docs/architecture/PACT_RESPONSE_TO_CLAUDE_REVISED_ACCEPTED_PLAN_RU.md``
    §8.14 ("Translation-time formatting contract") and §6.1 ("Blocking
    formatting integrity"): every source inline span receives a mapping
    ``{span_id, translated_text, occurrence}``; the code verifies the
    substring exists, the occurrence is unambiguous, spans do not conflict,
    and every required span is mapped.
  * ``docs/architecture/V4_FINAL_REVIEW_AND_IMPLEMENTATION_PLAN_RU_v2.md``
    ("Phase 5 — formatting alignment") and ``docs/plans/
    V4_IMPLEMENTATION_ORDER_PLAN_RU.md`` §4 B3.
  * ``docs/architecture/V4_MVP_SPEC_RU.md`` §2 Step 6/8: the formatting
    contract is applied **before** Step 8 so the final integrity check and
    the terminal transition see the same text that goes into ``complete``.
  * ``docs/plans/V4_1_WHOLE_CHAPTER_ARCHITECTURE_PLAN_RU.md`` §8-C and
    ``docs/plans/V4_1_AUDIT_B1_RU.md`` §11 (card C): formatting is
    **model-free** — the rule "formatting = 0 model calls". The model
    fallback tier was removed; only the deterministic tiers remain.

What this module implements is the *restoration* half of the formatting
contract: the inline HTML spans (``em``/``strong``/``i``/``b``/``a``)
extracted at source parse time (``pact_v4.phase0b.source_html``) are
re-located in the translated Russian text and re-wrapped, so the final
chapter text carries the source's emphasis. The *verification* half — PID
coverage / numbers / mixed-script / glossary over the whole chapter — is the
Step 8 deterministic integrity check in ``pact_v4.phase4.repair``
(``run_integrity_check``), which now runs over the formatted text.

Key rules (owner decisions, DECISIONS.md 2026-08-02 / 2026-08-05; card C
2026-08-10):

  * Formatting is **wrap-only**: it never rewrites the translated text, it
    only locates fragments and wraps them in the source tags. The visible
    content is therefore identical to the repaired text, so Step 8's
    conditional narrow Qwen smoke (``_needs_qwen_smoke``) cannot be tripped
    by formatting alone.
  * Formatting is **model-free** (card C): all tiers are deterministic —
    ``preserved`` (the translation already carries the inline markup, the
    whole-chapter case), ``exact``, ``occurrence_aware``, ``fuzzy``. There
    is no ``FormattingCaller`` and no model call anywhere in this module.
  * Every span resolution records its tier with the located range — no
    silent fallback anywhere.
  * Every unresolved required span is a blocking incident; the policy limit
    ``max_formatting_incidents`` (production default ``0``) decides whether
    the chapter can be ``complete``. Violating it yields
    ``accepted_degraded`` when the output profile remains structurally valid
    (a valid PID map) or ``failed`` otherwise. Unresolved spans are debt,
    never a silent loss; "0 model calls" alone is not success when the
    chapter degraded to ``accepted_degraded`` because of formatting debt.

The module deliberately never imports ``pact_v4.runtime.model_lifecycle`` /
``model_lifecycle_adapters`` / ``ModelRouter`` / ``backend_role_adapters``
(dual-mode rule, now trivially satisfied — there is no transport at all).
"""
from __future__ import annotations

import html
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from pact_v4.phase0b.source_html import SourceBlock, SourceSpan

LOG = logging.getLogger(__name__)

__all__ = [
    "FORMATTING_POLICY_VERSION",
    "FORMATTING_OUTCOME_SCHEMA",
    "FORMATTING_REPORT_SCHEMA",
    "MAX_FORMATTING_INCIDENTS_DEFAULT",
    "TIER_PRESERVED",
    "TIER_EXACT",
    "TIER_OCCURRENCE",
    "TIER_FUZZY",
    "FormattingIncident",
    "SpanMappingRecord",
    "FormattingOutcome",
    "occurrence_ranges",
    "find_nonoverlapping_occurrence",
    "apply_span_mappings",
    "run_formatting_align",
]

FORMATTING_POLICY_VERSION = "pact-v4-formatting/v1"
FORMATTING_OUTCOME_SCHEMA = "pact-v4-formatting-outcome/v1"
FORMATTING_REPORT_SCHEMA = "pact-v4-formatting-report/v1"
MAX_FORMATTING_INCIDENTS_DEFAULT = 0

# Deterministic tiers only (card C: formatting = 0 model calls). The former
# ``model_fallback`` tier was removed.
TIER_PRESERVED = "preserved"
TIER_EXACT = "exact"
TIER_OCCURRENCE = "occurrence_aware"
TIER_FUZZY = "fuzzy"

# Word-boundary charset matches ``_SOURCE_BOUNDARY`` in
# ``pact_v4._integrity_checks`` (same convention as the glossary/number
# checks, so a needle is never matched as a substring of a larger token).
_WORD_BOUNDARY = r"A-Za-z0-9_"

# A resolved span's fragment must be non-empty and free of placeholder
# markers. The marker check mirrors v3's "FMT marker leaked into final HTML"
# guard: no placeholder of ours may ever reach the output text.
_MARKER_RE = re.compile(r"\[\[FMT_|@@FMT|%%FMT|<<FMT")

_CURVE_QUOTES = str.maketrans({
    "“": '"', "”": '"', "‘": "'", "’": "'",
})

# Inline tags whose presence in the translated text counts as "already
# restored" for the preserved tier (same set as ``source_html``).
_INLINE_TAG_OPEN_RE = re.compile(r"<(em|strong|i|b|a)\b[^>]*>")


def _fold(text: str) -> str:
    """Conservative normalization used for grouping and fuzzy matching.

    Length-preserving apart from the rare Unicode casefold expansions; this
    is deliberately narrow (no stemming, no phonetic matching, no edit
    distance) so the deterministic tiers never guess a fragment.
    """
    return text.casefold().replace("ё", "е").translate(_CURVE_QUOTES)


# ---------------------------------------------------------------------------
# Span mapping / incident records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpanMappingRecord:
    """One resolved source span -> located translation fragment.

    ``tier`` records which alignment tier produced the mapping; ``start`` /
    ``end`` are the half-open character range of the fragment in the raw
    (unformatted) translated text. ``preserved`` is ``True`` when the
    fragment was already wrapped in the translation (tier
    ``TIER_PRESERVED``): ``apply_span_mappings`` then passes the range
    through verbatim instead of adding another wrap.
    """

    pid: str
    span_id: str
    tag: str
    source_text: str
    translated_text: str
    occurrence: int
    tier: str
    start: int
    end: int
    attrs: Mapping[str, str] = field(default_factory=dict)
    preserved: bool = False

    def to_payload(self) -> Dict[str, Any]:
        return {
            "pid": self.pid,
            "span_id": self.span_id,
            "tag": self.tag,
            "source_text": self.source_text,
            "translated_text": self.translated_text,
            "occurrence": self.occurrence,
            "tier": self.tier,
            "start": self.start,
            "end": self.end,
            "attrs": dict(sorted(self.attrs.items())),
            "preserved": self.preserved,
        }


@dataclass(frozen=True)
class FormattingIncident:
    """One unresolved required inline span.

    ``tier`` is the last deterministic tier attempted before the span was
    given up on. Every incident is blocking (``formatting.required`` is
    always ``True`` for the inline spans this module restores); the
    ``max_formatting_incidents`` policy decides whether the chapter can
    still be ``complete``. Unresolved spans are debt, never a silent loss.
    """

    pid: str
    span_id: str
    tier: str
    reason: str
    required: bool = True
    detail: str = ""

    def to_payload(self) -> Dict[str, Any]:
        return {
            "pid": self.pid,
            "span_id": self.span_id,
            "tier": self.tier,
            "reason": self.reason,
            "required": self.required,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class FormattingOutcome:
    """Result of one ``run_formatting_align`` call over a chapter.

    ``formatted_text`` is the PID -> inner-HTML map (the text Step 8 and the
    terminal transition see). ``span_mapping`` / ``incidents`` carry the
    per-span provenance required by §8.14; ``blocking`` is ``True`` when
    ``len(incidents) > max_formatting_incidents``.

    ``model_fallback_count`` / ``model_call_count`` are always ``0`` (card
    C: formatting is model-free by rule) and are kept only as an explicit
    observable of that invariant for downstream reports.
    """

    formatted_text: Tuple[Tuple[str, str], ...]
    span_mapping: Tuple[SpanMappingRecord, ...]
    incidents: Tuple[FormattingIncident, ...]
    backend_identity_hash: str
    policy_version: str
    max_formatting_incidents: int
    model_fallback_count: int = 0
    model_call_count: int = 0

    @property
    def incident_count(self) -> int:
        return len(self.incidents)

    @property
    def resolved_count(self) -> int:
        return len(self.span_mapping)

    @property
    def blocking(self) -> bool:
        return self.incident_count > self.max_formatting_incidents

    def as_pid_map(self) -> Dict[str, str]:
        return dict(self.formatted_text)

    def to_payload(self) -> Dict[str, Any]:
        return {
            "schema": FORMATTING_OUTCOME_SCHEMA,
            "policy_version": self.policy_version,
            "backend_identity_hash": self.backend_identity_hash,
            "formatted_text": [list(item) for item in self.formatted_text],
            "span_mapping": [record.to_payload() for record in self.span_mapping],
            "incidents": [incident.to_payload() for incident in self.incidents],
            "resolved_count": self.resolved_count,
            "incident_count": self.incident_count,
            "model_fallback_count": self.model_fallback_count,
            "model_call_count": self.model_call_count,
            "max_formatting_incidents": self.max_formatting_incidents,
            "blocking": self.blocking,
        }


# ---------------------------------------------------------------------------
# Occurrence helpers (word-boundary aware for the deterministic tiers)
# ---------------------------------------------------------------------------


def occurrence_ranges(
    text: str, needle: str, *, word_boundary: bool = False
) -> List[Tuple[int, int]]:
    """All half-open occurrence ranges of ``needle`` in ``text``.

    Case-sensitive first; if nothing matches, case-insensitive. With
    ``word_boundary=True`` the needle must not sit inside a larger
    ``[A-Za-z0-9_]`` token (numbers/names are never matched as substrings of
    bigger tokens). ``text`` may already carry inline markup (preserved
    fragments), so matches are reported against the raw string as given.
    """
    if not needle:
        return []
    escaped = re.escape(needle)
    if word_boundary:
        escaped = rf"(?<![{_WORD_BOUNDARY}]){escaped}(?![{_WORD_BOUNDARY}])"
    matches = list(re.finditer(escaped, text))
    if not matches:
        matches = list(re.finditer(escaped, text, flags=re.I))
    return [(match.start(), match.end()) for match in matches]


def find_nonoverlapping_occurrence(
    text: str,
    needle: str,
    preferred: int,
    occupied: Sequence[Tuple[int, int]],
    *,
    word_boundary: bool = False,
) -> Optional[Tuple[int, int]]:
    """Pick one occurrence of ``needle`` that does not overlap ``occupied``.

    ``preferred`` is a 1-based occurrence index; the preferred occurrence is
    tried first, then every other non-overlapping occurrence in index order.
    Returns ``None`` when every occurrence overlaps an already-claimed range.
    """
    ranges = occurrence_ranges(text, needle, word_boundary=word_boundary)
    if not ranges:
        return None
    order = list(range(len(ranges)))
    preferred_index = preferred - 1
    if 0 <= preferred_index < len(ranges):
        order.remove(preferred_index)
        order.insert(0, preferred_index)
    for index in order:
        start, end = ranges[index]
        if not any(not (end <= a or start >= b) for a, b in occupied):
            return start, end
    return None


def _fuzzy_pattern(needle: str) -> str:
    """A conservative, tolerant regex for the source span text.

    Tolerates only what a faithful translation would preserve by accident:
    case, ё/е, curly quotes, whitespace runs, and hyphen/en-dash vs space
    (``e-mail`` -> ``e mail``). Never accepts arbitrary edits, so a wrong
    fragment cannot slip through.
    """
    parts: List[str] = []
    for ch in _fold(needle):
        if ch.isspace():
            parts.append(r"\s+")
        elif ch == "\u0435":  # Cyrillic е (U+0435; ё folded to е) — Russian ё/е interchange
            parts.append("[еЕёЁ]")
        elif ch == "-" or ch in "–—":
            parts.append(r"[-–—\s]+")
        elif ch in "'":
            parts.append(r"['’]")
        elif ch in '"':
            parts.append('["”]')
        else:
            # Latin/other letters match case-insensitively via the re.I flag.
            parts.append(re.escape(ch))
    pattern = "".join(parts)
    if _fold(needle) and (_fold(needle)[0].isalnum() or _fold(needle)[-1].isalnum()):
        pattern = rf"(?<![{_WORD_BOUNDARY}]){pattern}(?![{_WORD_BOUNDARY}])"
    return pattern


# ---------------------------------------------------------------------------
# Preserved-markup tier (whole-chapter case: the translation already carries
# the inline tags — card C §11 "whole-chapter перевод держит <em> 101/101")
# ---------------------------------------------------------------------------


def _existing_inline_tags(
    text: str,
) -> List[Tuple[str, int, int]]:
    """Scan a translated PID text for already-present inline tags.

    Returns ``(tag, inner_start, inner_end)`` in document order (the same
    order ``parse_source_html``'s ``find_all`` produces: nested tags are
    reported by opening-tag order). ``inner_start`` points just after the
    opening tag and ``inner_end`` just before the closing tag, so the inner
    range is the already-wrapped fragment. Unbalanced tags are ignored (a
    broken tag must not claim a span).
    """
    results: List[Tuple[str, int, int]] = []
    for match in _INLINE_TAG_OPEN_RE.finditer(text):
        tag = match.group(1)
        close = re.search(rf"</{tag}>", text[match.end():])
        if close is None:
            continue
        inner_end = match.end() + close.start()
        results.append((tag, match.end(), inner_end))
    return results


def _resolve_preserved(
    *,
    pid: str,
    translation: str,
    spans: Sequence[SourceSpan],
) -> Tuple[List[SpanMappingRecord], List[SourceSpan], List[SourceSpan]]:
    """Resolve spans whose markup is ALREADY present in the translation.

    When the translation's inline tag sequence matches the source span tag
    sequence exactly (same tags, same order, same count), the emphasis was
    already restored by the translator — each source span maps 1:1 (in
    order) to the existing ``<tag>…</tag>`` range with tier
    ``TIER_PRESERVED`` and ``preserved=True`` (no re-wrap). This is the
    whole-chapter case (§11: "whole-chapter перевод держит ``<em>``
    101/101"), resolved with 0 model calls.

    Returns ``(resolved, remaining, mismatched)``:

    * ``resolved`` — the 1:1 preserved records;
    * ``remaining`` — spans to try in the text tiers (``exact`` ->
      ``occurrence_aware`` -> ``fuzzy``). This happens ONLY when the
      translation carries no inline markup at all (the valid exact path,
      e.g. source ``<em>1947</em>`` with a tag-free translation);
    * ``mismatched`` — spans whose translation ALREADY carries inline
      markup that does not match the source span sequence (count or order
      mismatch). A count/order mismatch (the translation added, dropped or
      reordered an emphasis) is NOT guessed and must NOT fall through to
      the text tiers: the source text often survives verbatim *inside* the
      existing markup, and an ``exact`` claim there would double-wrap the
      fragment with no incident. These spans become blocking incidents
      (debt) directly.
    """
    if not spans:
        return [], [], []
    src_seq = [span.tag for span in spans]
    existing = _existing_inline_tags(translation)
    if not existing:
        # No inline markup in the translation at all — the valid exact path.
        return [], list(spans), []
    if len(existing) != len(src_seq) or any(
        tag != expected for (tag, _s, _e), expected in zip(existing, src_seq)
    ):
        # Existing markup sequence mismatches the source span sequence:
        # never claim it and never fall through to the text tiers (a
        # verbatim fragment inside the existing markup would be
        # double-wrapped). The spans are blocking debt.
        return [], [], list(spans)
    resolved: List[SpanMappingRecord] = []
    for span, (tag, start, end) in zip(spans, existing):
        resolved.append(SpanMappingRecord(
            pid=pid,
            span_id=span.span_id,
            tag=tag,
            source_text=span.text,
            translated_text=translation[start:end],
            occurrence=span.occurrence,
            tier=TIER_PRESERVED,
            start=start,
            end=end,
            attrs=dict(span.attrs),
            preserved=True,
        ))
    return resolved, [], []


# ---------------------------------------------------------------------------
# Deterministic tier resolution (exact / occurrence-aware / fuzzy)
# ---------------------------------------------------------------------------


def _group_spans(spans: Sequence[SourceSpan]) -> Dict[str, List[SourceSpan]]:
    """Group a block's spans by folded source text (source order preserved)."""
    groups: Dict[str, List[SourceSpan]] = defaultdict(list)
    for span in spans:
        groups[_fold(span.text)].append(span)
    return dict(groups)


def _resolve_deterministic(
    *,
    pid: str,
    translation: str,
    spans: Sequence[SourceSpan],
    occupied: List[Tuple[int, int]],
) -> Tuple[List[SpanMappingRecord], List[SourceSpan], List[SourceSpan], List[SourceSpan]]:
    """Apply the preserved -> exact -> occurrence-aware -> fuzzy tiers.

    Returns ``(resolved, fuzzy_candidates, ambiguous, preserved_mismatch)``.

    * ``resolved`` — spans already wrapped in the translation (tier
      ``preserved``) or located verbatim (tier ``exact`` for a single
      occurrence, ``occurrence_aware`` for a 1:1 duplicate assignment).
    * ``fuzzy_candidates`` — spans whose source text never appears verbatim;
      the caller tries the conservative fuzzy match next.
    * ``ambiguous`` — spans whose source text appears, but the occurrence
      cannot be unambiguously assigned: the translation's occurrence count
      differs from the number of same-text source spans, or the available
      occurrences collide with an earlier span's claimed range. Per the
      contract ("occurrence неоднозначен"), these become blocking incidents,
      never guessed by re-running the exact string search.
    * ``preserved_mismatch`` — spans whose translation ALREADY carries
      inline markup whose sequence (count or order) differs from the source
      span sequence. These must become blocking incidents (debt) directly
      and must NOT run the text tiers: the source text often survives
      verbatim *inside* the existing markup, and an ``exact`` claim there
      would double-wrap the fragment with no incident. ``preserved_mismatch``
      is mutually exclusive with ``fuzzy_candidates``/``ambiguous`` — when it
      is non-empty the text tiers are skipped entirely for the PID.

    Group rule ("occurrence однозначен"): a group of ``M`` source spans with
    the same folded text resolves deterministically only when the translation
    contains exactly ``M`` occurrences. A count mismatch means the occurrence
    is ambiguous (e.g. one emphasized ``No`` in a translation that says
    "No No" — wrapping the first would be a guess), so the group falls
    through to the next tier.
    """
    preserved, preserved_remaining, preserved_mismatch = _resolve_preserved(
        pid=pid, translation=translation, spans=spans,
    )
    resolved: List[SpanMappingRecord] = list(preserved)
    if preserved_mismatch:
        # The translation already carries inline markup whose sequence does
        # not match the source spans: the spans are blocking debt (never
        # claimed, never run through the text tiers — a verbatim fragment
        # inside the existing markup would be double-wrapped).
        return resolved, [], [], preserved_mismatch
    if not preserved_remaining:
        return resolved, [], [], []
    spans = preserved_remaining

    fuzzy_candidates: List[SourceSpan] = []
    ambiguous: List[SourceSpan] = []
    for group in _group_spans(spans).values():
        needle = group[0].text
        ranges = occurrence_ranges(translation, needle, word_boundary=True)
        if not ranges:
            fuzzy_candidates.extend(group)
            continue
        if len(ranges) != len(group):
            ambiguous.extend(group)
            continue
        if any(
            any(not (end <= a or start >= b) for a, b in occupied)
            for start, end in ranges
        ):
            ambiguous.extend(group)
            continue
        tier = TIER_EXACT if len(group) == 1 else TIER_OCCURRENCE
        for index, span in enumerate(group):
            start, end = ranges[index]
            occupied.append((start, end))
            resolved.append(SpanMappingRecord(
                pid=pid,
                span_id=span.span_id,
                tag=span.tag,
                source_text=span.text,
                translated_text=translation[start:end],
                occurrence=index + 1,
                tier=tier,
                start=start,
                end=end,
                attrs=dict(span.attrs),
            ))

    if not fuzzy_candidates:
        return resolved, fuzzy_candidates, ambiguous, []

    # Fuzzy tier: only for the spans whose source text never appears
    # verbatim. A conservative normalization match (case/ё-е/quotes/
    # whitespace/hyphen) is the last deterministic recovery; anything else
    # becomes a blocking incident (debt).
    still_unresolved: List[SourceSpan] = []
    for span in fuzzy_candidates:
        pattern = _fuzzy_pattern(span.text)
        location = None
        for match in re.finditer(pattern, translation):
            start, end = match.span()
            if not any(not (end <= a or start >= b) for a, b in occupied):
                location = (start, end)
                break
        if location is None:
            still_unresolved.append(span)
            continue
        start, end = location
        occupied.append((start, end))
        resolved.append(SpanMappingRecord(
            pid=pid,
            span_id=span.span_id,
            tag=span.tag,
            source_text=span.text,
            translated_text=translation[start:end],
            occurrence=1,
            tier=TIER_FUZZY,
            start=start,
            end=end,
            attrs=dict(span.attrs),
        ))
    return resolved, still_unresolved, ambiguous, []


# ---------------------------------------------------------------------------
# Markup application
# ---------------------------------------------------------------------------


def apply_span_mappings(
    text: str, records: Sequence[SpanMappingRecord]
) -> str:
    """Wrap the located fragments in their source tags, wrap-only (B14).

    ``records`` must be the resolved mappings for one PID; ranges are applied
    left-to-right with every non-fragment slice passed through verbatim and
    every fragment wrapped in ``<tag attrs...>fragment</tag>``. A record whose
    range overlaps an already-applied one is skipped defensively (the
    alignment tiers already guarantee non-overlap). A ``preserved`` record
    (tier ``TIER_PRESERVED``) is passed through **without** a new wrap — the
    fragment is already wrapped in the translation.

    B14 (owner decision 2026-08-05): the wrap is **wrap-only without
    entities** — the translated text is no longer HTML-escaped while real
    tags are added around the fragment. Escaping the text was what produced
    run_005's double-escaping (``&lt;em&gt;<em>…</em>&lt;/em&gt;``: the
    model's own raw ``<em>`` got escaped into an entity while the wrap added
    a real tag). The final chapter text is normalized to clean tags when it
    is written to ``translations.json`` (``normalize_inline_markup``); the
    visible text is otherwise unchanged.
    """
    parts: List[str] = []
    cursor = 0
    for record in sorted(records, key=lambda r: r.start):
        if record.start < cursor:
            continue
        parts.append(text[cursor:record.start])
        if record.preserved:
            # Already wrapped in the translation — pass the range through
            # verbatim (the tags around it survive in the neighbouring
            # slices).
            parts.append(text[record.start:record.end])
            cursor = record.end
            continue
        attrs = "".join(
            f' {html.escape(str(key), quote=True)}="'
            f'{html.escape(str(value), quote=True)}"'
            for key, value in sorted(record.attrs.items())
        )
        parts.append(f"<{record.tag}{attrs}>")
        parts.append(text[record.start:record.end])
        parts.append(f"</{record.tag}>")
        cursor = record.end
    parts.append(text[cursor:])
    return "".join(parts)


# ---------------------------------------------------------------------------
# Chapter-level alignment
# ---------------------------------------------------------------------------


def run_formatting_align(
    *,
    blocks: Sequence[SourceBlock],
    translation: Mapping[str, str],
    backend_identity_hash: str,
    policy_version: str = FORMATTING_POLICY_VERSION,
    max_formatting_incidents: int = MAX_FORMATTING_INCIDENTS_DEFAULT,
) -> FormattingOutcome:
    """Run the Phase 5 formatting alignment over one chapter.

    ``blocks`` are the parsed source blocks (``pact_v4.phase0b.source_html``)
    carrying the inline spans; ``translation`` is the repaired chapter PID
    map produced by Phase 4 convergence. The output ``formatted_text`` covers
    every PID of ``translation`` (the visible text passed through verbatim
    with the restored inline tags — B14: wrap-only without entities); it is
    the text the Step 8 final integrity check and the terminal transition
    must see.

    Tier cascade per PID with a span contract (deterministic only — card C:
    formatting = 0 model calls):

      1. ``preserved`` — the translation already carries the inline tags
         (whole-chapter case: "whole-chapter перевод держит ``<em>``
         101/101"): the span's markup is already restored, resolved with no
         re-wrap. The preserved tier verifies the translation's tag sequence
         against the source spans (same tags, same order, same count); a
         count/order mismatch is NOT guessed and does NOT fall through to
         the text tiers — those spans become blocking incidents (debt)
         directly, because the source text often survives verbatim *inside*
         the existing markup and an ``exact`` claim would double-wrap it;
      2. ``exact`` — the source text survives verbatim, a single occurrence
         (only reached when the translation carries no inline markup at
         all);
      3. ``occurrence_aware`` — ``M`` identical source spans map 1:1 to ``M``
         occurrences (duplicate-``No``-style recovery);
      4. ``fuzzy`` — conservative normalization match (case/ё-е/quotes/
         whitespace/hyphen).

    Every unresolved required span becomes a blocking ``FormattingIncident``;
    ``blocking`` on the outcome is ``incident_count > max_formatting_incidents``
    (production default ``0``). Unresolved spans are debt, never a silent
    loss — "0 model calls" is not success when the chapter degraded to
    ``accepted_degraded`` because of formatting debt.
    """
    span_map: Dict[str, Tuple[SourceSpan, ...]] = {
        block.pid: tuple(block.inline_spans)
        for block in blocks
        if block.inline_spans
    }
    # B14: wrap-only without entities — the translated text is passed through
    # verbatim (no html.escape); only the restored inline tags are added.
    # See ``apply_span_mappings`` for the run_005 double-escaping rationale.
    formatted: Dict[str, str] = {
        pid: text for pid, text in translation.items()
    }
    span_mapping: List[SpanMappingRecord] = []
    incidents: List[FormattingIncident] = []

    for pid, spans in span_map.items():
        text = translation.get(pid, "")
        if not text:
            continue
        occupied: List[Tuple[int, int]] = []
        resolved, fuzzy_candidates, ambiguous, preserved_mismatch = _resolve_deterministic(
            pid=pid, translation=text, spans=spans, occupied=occupied,
        )
        span_mapping.extend(resolved)
        unresolved = fuzzy_candidates + ambiguous + preserved_mismatch
        fuzzy_ids = {span.span_id for span in fuzzy_candidates}
        mismatch_ids = {span.span_id for span in preserved_mismatch}

        def _last_tier(span: SourceSpan) -> str:
            if span.span_id in mismatch_ids:
                return TIER_PRESERVED
            if span.span_id in fuzzy_ids:
                return TIER_FUZZY
            return TIER_OCCURRENCE

        def _reason(span: SourceSpan) -> str:
            if span.span_id in mismatch_ids:
                return "preserved_tag_mismatch"
            if span.span_id in fuzzy_ids:
                return "target_not_found"
            return "ambiguous_occurrence"

        def _detail(span: SourceSpan) -> str:
            if span.span_id in mismatch_ids:
                return (
                    "translation already carries inline markup whose tag "
                    "sequence (count/order) does not match the source spans; "
                    "never claimed, never re-wrapped (formatting is model-free "
                    "by rule — unresolved spans are debt)"
                )
            return (
                "no deterministic fragment found (formatting is "
                "model-free by rule — unresolved spans are debt)"
            )

        if unresolved:
            incidents.extend(
                FormattingIncident(
                    pid=pid, span_id=span.span_id, tier=_last_tier(span),
                    reason=_reason(span),
                    detail=_detail(span),
                )
                for span in unresolved
            )

    for pid, spans in span_map.items():
        text = translation.get(pid, "")
        records_for_pid = [r for r in span_mapping if r.pid == pid]
        formatted[pid] = apply_span_mappings(text, records_for_pid)

    # Marker-leakage guard (v3 "FMT marker leaked into final HTML"): the
    # formatted text must never contain a placeholder of ours.
    for pid, text in formatted.items():
        if _MARKER_RE.search(text):
            raise AssertionError(
                f"Formatting marker leaked into PID {pid}: {text!r}"
            )

    return FormattingOutcome(
        formatted_text=tuple((pid, formatted.get(pid, "")) for pid in translation),
        span_mapping=tuple(span_mapping),
        incidents=tuple(incidents),
        backend_identity_hash=backend_identity_hash,
        policy_version=policy_version,
        max_formatting_incidents=max_formatting_incidents,
        model_fallback_count=0,
        model_call_count=0,
    )
