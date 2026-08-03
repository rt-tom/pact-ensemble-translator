"""Phase 5: translation-time formatting alignment (the §8.14 span contract).

Backing spec:

  * ``docs/architecture/PACT_RESPONSE_TO_CLAUDE_REVISED_ACCEPTED_PLAN_RU.md``
    §8.14 ("Translation-time formatting contract") and §6.1 ("Blocking
    formatting integrity"): every source inline span receives a mapping
    ``{span_id, translated_text, occurrence}``; the code verifies the
    substring exists, the occurrence is unambiguous, spans do not conflict,
    and every required span is mapped. The main path is deterministic, with
    the fallback cascade exact -> occurrence-aware -> conservative fuzzy ->
    model fallback, all with provenance. An unresolved required span
    blocks ``complete``.
  * ``docs/architecture/V4_FINAL_REVIEW_AND_IMPLEMENTATION_PLAN_RU_v2.md``
    ("Phase 5 — formatting alignment") and ``docs/plans/
    V4_IMPLEMENTATION_ORDER_PLAN_RU.md`` §4 B3.
  * ``docs/architecture/V4_MVP_SPEC_RU.md`` §2 Step 6/8: the formatting
    contract is applied **before** Step 8 so the final integrity check and
    the terminal transition see the same text that goes into ``complete``.

What this module implements is the *restoration* half of the formatting
contract: the inline HTML spans (``em``/``strong``/``i``/``b``/``a``)
extracted at source parse time (``pact_v4.phase0b.source_html``) are
re-located in the translated Russian text and re-wrapped, so the final
chapter text carries the source's emphasis. The *verification* half — PID
coverage / numbers / mixed-script / glossary over the whole chapter — is the
Step 8 deterministic integrity check in ``pact_v4.phase4.repair``
(``run_integrity_check``), which now runs over the formatted text.

Key rules (owner decisions, DECISIONS.md 2026-08-02):

  * Formatting is **wrap-only**: it never rewrites the translated text, it
    only locates fragments and wraps them in the source tags. The visible
    content is therefore identical to the repaired text, so Step 8's
    conditional narrow Qwen smoke (``_needs_qwen_smoke``) cannot be tripped
    by formatting alone.
  * Every span resolution records its tier (``exact`` / ``occurrence_aware``
    / ``fuzzy`` / ``model_fallback``) with the located range — no silent
    fallback anywhere.
  * Every unresolved required span is a blocking incident; the policy limit
    ``max_formatting_incidents`` (production default ``0``) decides whether
    the chapter can be ``complete``. Violating it yields
    ``accepted_degraded`` when the output profile remains structurally valid
    (a valid PID map) or ``failed`` otherwise.
  * The model fallback tier goes through an injected ``FormattingCaller``
    (the strict driver wires ``BackendFormattingCaller`` over the
    coordinator ``CompletionBackend``, never local lifecycle adapters — an
    import-guard test enforces this). A transport failure at that call is
    recorded as an incident with reason ``transport_error`` — debt /
    incomplete, never a semantic terminal status ("transport failure !=
    semantic gate failure").

The module deliberately never imports ``pact_v4.runtime.model_lifecycle`` /
``model_lifecycle_adapters`` / ``ModelRouter`` (dual-mode rule).
"""
from __future__ import annotations

import html
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

from pact_v4.phase0b.source_html import SourceBlock, SourceSpan
from pact_v4.phase1.models import validate_json_complete

LOG = logging.getLogger(__name__)

__all__ = [
    "FORMATTING_POLICY_VERSION",
    "FORMATTING_OUTCOME_SCHEMA",
    "FORMATTING_REPORT_SCHEMA",
    "MAX_FORMATTING_INCIDENTS_DEFAULT",
    "TIER_EXACT",
    "TIER_OCCURRENCE",
    "TIER_FUZZY",
    "TIER_MODEL",
    "FormattingCaller",
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

TIER_EXACT = "exact"
TIER_OCCURRENCE = "occurrence_aware"
TIER_FUZZY = "fuzzy"
TIER_MODEL = "model_fallback"

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
    (unformatted) translated text.
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
        }


@dataclass(frozen=True)
class FormattingIncident:
    """One unresolved required inline span.

    ``tier`` is the last tier attempted before the span was given up on
    (``model_fallback`` when the model could not map it or the fallback call
    failed to transport). Every incident is blocking (``formatting.required``
    is always ``True`` for the inline spans this module restores); the
    ``max_formatting_incidents`` policy decides whether the chapter can still
    be ``complete``.
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
    per-span provenance required by §8.14; ``model_fallback_count`` records
    how many PIDs needed the model tier (the observable signal for the
    conditional narrow Qwen smoke, which never fires for wrap-only
    formatting). ``blocking`` is ``True`` when
    ``len(incidents) > max_formatting_incidents``.
    """

    formatted_text: Tuple[Tuple[str, str], ...]
    span_mapping: Tuple[SpanMappingRecord, ...]
    incidents: Tuple[FormattingIncident, ...]
    backend_identity_hash: str
    policy_version: str
    max_formatting_incidents: int
    model_fallback_count: int = 0

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
            "max_formatting_incidents": self.max_formatting_incidents,
            "blocking": self.blocking,
        }


# ---------------------------------------------------------------------------
# Model fallback interface (backend-neutral; the driver injects the adapter)
# ---------------------------------------------------------------------------


class FormattingCaller(Protocol):
    """Map a PID's unresolved source spans to translation fragments.

    Receives the PID's English source text, its Russian translation, and the
    serialized unresolved source spans; returns raw text expected to be a
    JSON object ``{"mappings": [{"pid", "span_id", "target_text",
    "occurrence"}]}`` (strict; no markdown, no commentary). This protocol
    knows nothing about HTTP — production wiring lives in the pipeline (the
    strict driver injects ``BackendFormattingCaller`` over the coordinator
    ``CompletionBackend``).
    """

    def __call__(
        self,
        *,
        pid: str,
        source_text: str,
        translation: str,
        spans: Sequence[Mapping[str, Any]],
    ) -> str: ...


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
    bigger tokens). ``text`` may already carry inline markup (model-fallback
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
) -> Tuple[List[SpanMappingRecord], List[SourceSpan], List[SourceSpan]]:
    """Apply the exact -> occurrence-aware -> fuzzy deterministic tiers.

    Returns ``(resolved, fuzzy_candidates, ambiguous)``.

    * ``resolved`` — spans located verbatim (tier ``exact`` for a single
      occurrence, ``occurrence_aware`` for a 1:1 duplicate assignment).
    * ``fuzzy_candidates`` — spans whose source text never appears verbatim;
      the caller tries the conservative fuzzy match next.
    * ``ambiguous`` — spans whose source text appears, but the occurrence
      cannot be unambiguously assigned: the translation's occurrence count
      differs from the number of same-text source spans, or the available
      occurrences collide with an earlier span's claimed range. Per the
      contract ("occurrence неоднозначен"), these go straight to the model
      fallback, never guessed by re-running the exact string search.

    Group rule ("occurrence однозначен"): a group of ``M`` source spans with
    the same folded text resolves deterministically only when the translation
    contains exactly ``M`` occurrences. A count mismatch means the occurrence
    is ambiguous (e.g. one emphasized ``No`` in a translation that says
    "No No" — wrapping the first would be a guess), so the group falls
    through to the next tier.
    """
    resolved: List[SpanMappingRecord] = []
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
        return resolved, fuzzy_candidates, ambiguous

    # Fuzzy tier: only for the spans whose source text never appears
    # verbatim. A conservative normalization match (case/ё-е/quotes/
    # whitespace/hyphen) is the last deterministic recovery; anything else
    # is handed to the model fallback.
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
    return resolved, still_unresolved, ambiguous


# ---------------------------------------------------------------------------
# Model fallback tier (strict parsing; transport failure is debt, not verdict)
# ---------------------------------------------------------------------------


def _parse_format_mappings(
    raw: str,
    *,
    allowed: Mapping[Tuple[str, str], SourceSpan],
    pid: str,
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Parse a formatting response into ``{(pid, span_id): mapping}``.

    Strict contract: well-formed complete JSON object with exactly
    ``{"mappings": [...]}``; each entry has ``pid`` / ``span_id`` /
    ``target_text`` / ``occurrence``, and ``(pid, span_id)`` must be one of
    ``allowed``. Any truncation / malformed JSON / wrong shape raises
    ``ValueError`` — the caller treats that as an incident (debt), never a
    silent fallback. An explicitly empty ``target_text`` is kept verbatim so
    the caller can record the model's "no correspondence" verdict as an
    incident instead of guessing.
    """
    payload = validate_json_complete(raw)
    raw_mappings = payload.get("mappings")
    if not isinstance(raw_mappings, list):
        raise ValueError(
            f"Formatting response for {pid}: 'mappings' must be a JSON array"
        )
    result: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for index, item in enumerate(raw_mappings):
        if not isinstance(item, dict):
            raise ValueError(
                f"Formatting response for {pid}: mappings[{index}] must be a JSON object"
            )
        key = (str(item.get("pid") or ""), str(item.get("span_id") or ""))
        if key not in allowed or key in result:
            continue
        target_text = str(item.get("target_text") or "")
        try:
            occurrence = max(1, int(item.get("occurrence") or 1))
        except (TypeError, ValueError):
            occurrence = 1
        result[key] = {"target_text": target_text, "occurrence": occurrence}
    return result


def _apply_model_mappings(
    *,
    pid: str,
    translation: str,
    spans: Sequence[SourceSpan],
    mappings: Mapping[Tuple[str, str], Dict[str, Any]],
    occupied: List[Tuple[int, int]],
) -> Tuple[List[SpanMappingRecord], List[FormattingIncident]]:
    """Apply model-provided mappings, verifying each fragment exists.

    Each mapping's ``target_text`` must be a non-empty substring of the
    translation at a non-overlapping occurrence; otherwise the span stays an
    incident (``target_not_found`` / ``missing_mapping``). A mapping that
    explicitly says "no fragment" (empty ``target_text``) is recorded as an
    incident too — the model's honest verdict, never a silent fallback.
    """
    resolved: List[SpanMappingRecord] = []
    incidents: List[FormattingIncident] = []
    by_span = {span.span_id: span for span in spans}
    for span in spans:
        mapping = mappings.get((pid, span.span_id))
        if mapping is None:
            incidents.append(FormattingIncident(
                pid=pid, span_id=span.span_id, tier=TIER_MODEL,
                reason="missing_mapping",
                detail="model fallback returned no mapping for this span",
            ))
            continue
        target_text = mapping["target_text"]
        if not target_text:
            incidents.append(FormattingIncident(
                pid=pid, span_id=span.span_id, tier=TIER_MODEL,
                reason="target_not_found",
                detail="model fallback reported no corresponding fragment",
            ))
            continue
        location = find_nonoverlapping_occurrence(
            translation, target_text, preferred=mapping["occurrence"],
            occupied=occupied,
        )
        if location is None:
            incidents.append(FormattingIncident(
                pid=pid, span_id=span.span_id, tier=TIER_MODEL,
                reason="target_not_found",
                detail=(
                    f"model-provided fragment {target_text!r} is not a "
                    "non-overlapping substring of the translation"
                ),
            ))
            continue
        start, end = location
        occupied.append((start, end))
        resolved.append(SpanMappingRecord(
            pid=pid,
            span_id=span.span_id,
            tag=by_span[span.span_id].tag,
            source_text=by_span[span.span_id].text,
            translated_text=target_text,
            occurrence=mapping["occurrence"],
            tier=TIER_MODEL,
            start=start,
            end=end,
            attrs=dict(by_span[span.span_id].attrs),
        ))
    return resolved, incidents


# ---------------------------------------------------------------------------
# Markup application
# ---------------------------------------------------------------------------


def apply_span_mappings(
    text: str, records: Sequence[SpanMappingRecord]
) -> str:
    """Wrap the located fragments in their source tags, HTML-escaping all text.

    ``records`` must be the resolved mappings for one PID; ranges are applied
    left-to-right with every non-fragment slice HTML-escaped and every
    fragment wrapped in ``<tag attrs...>fragment</tag>``. A record whose
    range overlaps an already-applied one is skipped defensively (the
    alignment tiers already guarantee non-overlap). The result is inner HTML
    — the final chapter text that Step 8 and the terminal transition see.
    """
    parts: List[str] = []
    cursor = 0
    for record in sorted(records, key=lambda r: r.start):
        if record.start < cursor:
            continue
        parts.append(html.escape(text[cursor:record.start]))
        attrs = "".join(
            f' {html.escape(str(key), quote=True)}="'
            f'{html.escape(str(value), quote=True)}"'
            for key, value in sorted(record.attrs.items())
        )
        parts.append(f"<{record.tag}{attrs}>")
        parts.append(html.escape(text[record.start:record.end]))
        parts.append(f"</{record.tag}>")
        cursor = record.end
    parts.append(html.escape(text[cursor:]))
    return "".join(parts)


# ---------------------------------------------------------------------------
# Chapter-level alignment
# ---------------------------------------------------------------------------


def _span_payload(span: SourceSpan) -> Dict[str, Any]:
    return {
        "span_id": span.span_id,
        "tag": span.tag,
        "text": span.text,
        "occurrence": span.occurrence,
        "attrs": dict(sorted(span.attrs.items())),
    }


def run_formatting_align(
    *,
    blocks: Sequence[SourceBlock],
    translation: Mapping[str, str],
    formatting_caller: Optional[FormattingCaller] = None,
    backend_identity_hash: str,
    policy_version: str = FORMATTING_POLICY_VERSION,
    max_formatting_incidents: int = MAX_FORMATTING_INCIDENTS_DEFAULT,
) -> FormattingOutcome:
    """Run the Phase 5 formatting alignment over one chapter.

    ``blocks`` are the parsed source blocks (``pact_v4.phase0b.source_html``)
    carrying the inline spans; ``translation`` is the repaired chapter PID
    map produced by Phase 4 convergence. The output ``formatted_text`` covers
    every PID of ``translation`` (inner HTML with the restored inline
    markup); it is the text the Step 8 final integrity check and the
    terminal transition must see.

    Tier cascade per PID with a span contract:

      1. ``exact`` — the source text survives verbatim, a single occurrence;
      2. ``occurrence_aware`` — ``M`` identical source spans map 1:1 to ``M``
         occurrences (duplicate-``No``-style recovery);
      3. ``fuzzy`` — conservative normalization match (case/ё-е/quotes/
         whitespace/hyphen);
      4. ``model_fallback`` — the injected ``FormattingCaller`` maps the
         remaining spans, only when one is configured.

    Every unresolved required span becomes a blocking ``FormattingIncident``;
    ``blocking`` on the outcome is ``incident_count > max_formatting_incidents``
    (production default ``0``). A transport failure at the model fallback is
    recorded as incidents with reason ``transport_error`` — debt, never a
    semantic verdict.
    """
    span_map: Dict[str, Tuple[SourceSpan, ...]] = {
        block.pid: tuple(block.inline_spans)
        for block in blocks
        if block.inline_spans
    }
    source_by_pid: Dict[str, str] = {block.pid: block.text for block in blocks}
    formatted: Dict[str, str] = {
        pid: html.escape(text) for pid, text in translation.items()
    }
    span_mapping: List[SpanMappingRecord] = []
    incidents: List[FormattingIncident] = []
    model_fallback_count = 0

    for pid, spans in span_map.items():
        text = translation.get(pid, "")
        if not text:
            continue
        occupied: List[Tuple[int, int]] = []
        resolved, fuzzy_candidates, ambiguous = _resolve_deterministic(
            pid=pid, translation=text, spans=spans, occupied=occupied,
        )
        span_mapping.extend(resolved)
        unresolved = fuzzy_candidates + ambiguous
        fuzzy_ids = {span.span_id for span in fuzzy_candidates}

        def _last_tier(span: SourceSpan) -> str:
            return TIER_FUZZY if span.span_id in fuzzy_ids else TIER_OCCURRENCE

        def _no_caller_reason(span: SourceSpan) -> str:
            if span.span_id in fuzzy_ids:
                return "target_not_found"
            return "ambiguous_occurrence"

        if unresolved:
            if formatting_caller is None:
                incidents.extend(
                    FormattingIncident(
                        pid=pid, span_id=span.span_id, tier=_last_tier(span),
                        reason=_no_caller_reason(span),
                        detail=(
                            "no deterministic fragment and no model fallback "
                            "configured"
                        ),
                    )
                    for span in unresolved
                )
            else:
                allowed = {(pid, span.span_id): span for span in unresolved}
                model_fallback_count += 1
                try:
                    raw = formatting_caller(
                        pid=pid,
                        source_text=source_by_pid.get(pid, ""),
                        translation=text,
                        spans=[_span_payload(span) for span in unresolved],
                    )
                    mappings = _parse_format_mappings(
                        raw, allowed=allowed, pid=pid,
                    )
                except Exception as exc:  # transport or invalid structured output
                    LOG.warning(
                        "Formatting model fallback failed for %s: %s "
                        "(recorded as debt, not a semantic verdict)",
                        pid, exc,
                    )
                    incidents.extend(
                        FormattingIncident(
                            pid=pid, span_id=span.span_id, tier=TIER_MODEL,
                            reason="transport_error",
                            detail=f"model fallback call failed: {exc!r}",
                        )
                        for span in unresolved
                    )
                else:
                    model_resolved, model_incidents = _apply_model_mappings(
                        pid=pid, translation=text, spans=unresolved,
                        mappings=mappings, occupied=occupied,
                    )
                    span_mapping.extend(model_resolved)
                    incidents.extend(model_incidents)

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
        model_fallback_count=model_fallback_count,
    )
