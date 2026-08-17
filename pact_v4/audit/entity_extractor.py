"""B1.2 ChapterEntityContext extractor — source-only Qwen prepass.

Produces ``chapter_entity_context.json`` (the CHAPTER ENTITY FACTS block of
the v4 audit prompt, konspekt ``V4_1_AUDIT_B1_RU.md`` §8.3, §10 B1.2): a
compact, source-derived per-chapter record of persistent long-range
entities (objects with aliases, people with roles/names, source-established
gender). Fixes the p00236 class of errors (``bike -> велосипед`` while the
object is established as a ``motorcycle`` at p00007) that a chunked audit
cannot see because the anchor and the alias never share an audit window.

Design rules (konspekt §8.3 + B1.1 review, PROPOSAL reply §1.2/§1.4/§1.5):

* **SOURCE-ONLY**: every fact must be re-confirmed against this chapter's
  source text. book_memory is never evidence here (poisoned-context lesson
  "The Nurse: female").
* **Per-claim schema**: ``entity + claims[]`` where each claim is
  ``{kind, value, status, evidence, evidence_windows}``. Statuses are
  per-part: anchor span -> ``verified``, alias mention span -> ``verified``,
  same_entity relation -> ``candidate`` (a semantic hypothesis, never
  auto-repair).
* **Validation by code (8 points, §8.3)**: schema/version/source_hash/
  chapter_id; every PID exists; every quoted span verbatim in source; no
  translation-derived span; canonical type explicitly in anchor evidence;
  alias surface in its own PID; gender evidence with a verifiable referent
  link (not just "he/him"); unconfirmed coreference -> ``candidate``, never
  silently accepted as ``verified``.
* **Cache per-chapter**: identity = ``source_hash + extractor_version``;
  a cache hit resumes without another model call, but every hit
  re-validates the cached content against the current chapter source
  (anchor/alias/evidence PIDs and spans, alias surfaces,
  canonical-type-in-anchor-span) — tampered/foreign entries fail closed
  and are recomputed, never returned ``from_cache=True``.
* **Never authorizes repair**: this context is a hint for the auditor
  (fallback evidence level 3); any finding that relies on a semantic
  relation stays Tier B (rule §5.3).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from pact_v4.phase1.models import SourceArtifact, canonical_json_hash
from pact_v4.runtime.backend_protocol import (
    JSON_OBJECT_SCHEMA,
    CompletionBackend,
    CompletionError,
    CompletionRequest,
    Message,
)
from pact_v4.runtime.json_resilience import (
    EmptyResponseError,
    JsonRetryPolicy,
    TruncatedJSONError,
    parse_json_response,
    retry_json_call,
)
from pact_v4.runtime.prompts_runtime import ReviewerPrompt
from pact_v4.runtime.reasoning_writer import append_error_marker, open_reasoning_writer

LOG = logging.getLogger(__name__)

# Identity of the validated artifact and of the extractor model call.
ENTITY_CONTEXT_SCHEMA = "pact-v4-chapter-entity-context/v1"
EXTRACTOR_VERSION = "pact-v4-entity-extractor/v1"
# Identity of the validation report written alongside the artifact so a
# drop/downgrade is visible, never silent.
VALIDATION_REPORT_SCHEMA = "pact-v4-entity-extractor-validation/v1"
CACHE_SCHEMA = "pact-v4-entity-context-cache/v1"

CLAIM_KINDS = ("gender", "alias_relation", "object_identity")
STATUS_VERIFIED = "verified"
STATUS_CANDIDATE = "candidate"
STATUSES = (STATUS_VERIFIED, STATUS_CANDIDATE)

# Gendered pronouns used by the gender-evidence check (point 7 of §8.3):
# the claim's value (male/female) must be matched by a pronoun IN THE SAME
# evidence PID as a referent surface — a lone "he" somewhere is not a
# verifiable referent link.
_MALE_PRONOUNS = re.compile(r"\b(he|him|his|himself)\b", re.IGNORECASE)
_FEMALE_PRONOUNS = re.compile(r"\b(she|her|hers|herself)\b", re.IGNORECASE)

# Cyrillic is a strong signal of a translation-derived span (the source is
# English); such a span must be rejected (point 4 of §8.3).
_CYRILLIC = re.compile(r"[\u0400-\u04FF]")


# ---------------------------------------------------------------------------
# Model output schema (per-claim, §8.3)
# ---------------------------------------------------------------------------


ENTITY_EXTRACTION_V1 = ReviewerPrompt(
    role="entity_extractor",
    version="pact-v4-entity-extractor-prompt/v1",
    instructions=(
        "You are a source-only entity extractor for an English fiction "
        "chapter. You are given the FULL SOURCE of one chapter as an ordered "
        "PID map (PID -> English text). Extract persistent long-range "
        "entities and per-claim facts about them that the source itself "
        "establishes. Return STRICT JSON, no markdown fences, no commentary, "
        "with exactly this schema. The response MUST be a single JSON object "
        "with a top-level \"entities\" key — do NOT return a bare JSON array "
        "(e.g. [{...}] is wrong; {\"entities\": [...]} is correct). Do not "
        "add schema, extractor_version, chapter_id, or source_hash; the "
        "harness stamps these from the actual chapter. "
        "Schema:\n"
        "  entities: array of objects, each with:\n"
        "    entity: short canonical display name for the entity\n"
        "    canonical_type: the noun phrase the source uses as the "
        "established type of the entity (e.g. 'motorcycle', 'nurse')\n"
        "    anchor: object with pid (the PID where canonical_type is "
        "explicitly named) and span (the verbatim quoted source fragment "
        "containing canonical_type)\n"
        "    aliases: array of {surface, pid, span} — every other surface "
        "the source uses for the SAME entity, each with its own verbatim "
        "span; omit generic role words unless the source ties them to this "
        "specific person/object\n"
        "    claims: array of objects, each with:\n"
        "      kind: exactly one of\n"
        "        'gender' — the source establishes the gender of a person "
        "(male/female)\n"
        "        'alias_relation' — two source surfaces refer to the same "
        "entity\n"
        "        'object_identity' — a later surface refers to the same "
        "object as canonical_type\n"
        "      value: short string stating the claim\n"
        "      status: 'verified' | 'candidate' — 'verified' ONLY when the "
        "quoted evidence directly establishes the claim (a gendered pronoun "
        "in the same PID as the referent; the canonical type explicitly "
        "named); otherwise 'candidate'\n"
        "      evidence: array of {pid, span} — the PIDs with verbatim "
        "quoted spans that support the claim\n"
        "      evidence_windows: array of inclusive PID ranges [[a,b], ...] "
        "that contain the supporting context\n"
        "Rules:\n"
        "1. SOURCE ONLY: every span must be a verbatim quote from the "
        "source PID it is attached to. Never quote or invent text that is "
        "not in the source. Never use the translation, book memory, or "
        "outside knowledge.\n"
        "2. Anchor facts: the canonical_type must be explicitly named in "
        "the quoted anchor span.\n"
        "3. Alias facts: each alias surface must appear verbatim in its own "
        "PID.\n"
        "4. Gender facts: state gender only where the source shows it "
        "(e.g. he/him for male, she/her for female) with the referent in "
        "the same PID.\n"
        "5. Uncertain semantic coreference (e.g. 'bike' later = the "
        "'motorcycle' established earlier) is a 'candidate' relation, not "
        "'verified'.\n"
        "6. If the chapter has no persistent long-range entities, return "
        '{"entities": []}.\n'
        "7. PIDs are NOT guessable: the only valid PIDs are the ones listed "
        "in the VALID PIDS section below. Every pid you reference (anchor, "
        "alias, evidence, evidence_windows) must come from that list — a "
        "PID that is not in the list does not exist and the claim will be "
        "discarded."
    ),
)


# Valid-PID prompt section. The model repeatedly invented plausible PIDs
# (25/25 claims dropped in book-run 1-3 because every referenced PID was
# dead); the harness lists the chapter's real PIDs explicitly so the model
# can copy instead of guess.
def _valid_pids_section(valid_pids) -> str:
    pids = list(valid_pids or ())
    if not pids:
        return ""
    return "VALID PIDS (use ONLY these; every pid below exists):\n" + "\n".join(
        f"  {pid}" for pid in pids
    )


@dataclass(frozen=True)
class AnchorRef:
    """Canonical-type anchor: one PID + verbatim quoted span."""

    pid: str
    span: str
    status: str = STATUS_VERIFIED

    def to_payload(self) -> Dict[str, Any]:
        return {"pid": self.pid, "span": self.span, "status": self.status}


@dataclass(frozen=True)
class AliasRef:
    """One alias mention: surface + its own PID + verbatim span."""

    surface: str
    pid: str
    span: str
    status: str = STATUS_VERIFIED

    def to_payload(self) -> Dict[str, Any]:
        return {
            "surface": self.surface, "pid": self.pid,
            "span": self.span, "status": self.status,
        }


@dataclass(frozen=True)
class EvidenceRef:
    """One evidence entry: PID + verbatim quoted span."""

    pid: str
    span: str

    def to_payload(self) -> Dict[str, Any]:
        return {"pid": self.pid, "span": self.span}


@dataclass(frozen=True)
class EntityClaim:
    """One per-claim fact (schema §8.3)."""

    kind: str
    value: str
    status: str
    evidence: Tuple[EvidenceRef, ...]
    evidence_windows: Tuple[Tuple[str, str], ...]

    def to_payload(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "value": self.value,
            "status": self.status,
            "evidence": [e.to_payload() for e in self.evidence],
            "evidence_windows": [list(w) for w in self.evidence_windows],
        }


@dataclass(frozen=True)
class EntityRecord:
    """One persistent entity with its anchor, aliases and per-claim facts."""

    entity: str
    canonical_type: str
    anchor: AnchorRef
    aliases: Tuple[AliasRef, ...] = ()
    claims: Tuple[EntityClaim, ...] = ()

    def to_payload(self) -> Dict[str, Any]:
        return {
            "entity": self.entity,
            "canonical_type": self.canonical_type,
            "anchor": self.anchor.to_payload(),
            "aliases": [a.to_payload() for a in self.aliases],
            "claims": [c.to_payload() for c in self.claims],
        }


@dataclass(frozen=True)
class ChapterEntityContext:
    """Validated chapter entity context (cacheable, resumable)."""

    schema: str
    extractor_version: str
    chapter_id: str
    source_hash: str
    entities: Tuple[EntityRecord, ...] = ()

    def to_payload(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "extractor_version": self.extractor_version,
            "chapter_id": self.chapter_id,
            "source_hash": self.source_hash,
            "entities": [e.to_payload() for e in self.entities],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ChapterEntityContext":
        if payload.get("schema") != ENTITY_CONTEXT_SCHEMA:
            raise ValueError(
                f"Foreign identity: entity-context schema={payload.get('schema')!r}"
            )
        entities_raw = payload.get("entities")
        if not isinstance(entities_raw, list):
            raise ValueError(
                "entity-context payload must contain a top-level 'entities' "
                f"array, got {type(entities_raw).__name__}"
            )
        entities = tuple(_entity_from_payload(item) for item in entities_raw)
        return cls(
            schema=payload["schema"],
            extractor_version=str(payload["extractor_version"]),
            chapter_id=str(payload["chapter_id"]),
            source_hash=str(payload["source_hash"]),
            entities=entities,
        )


@dataclass(frozen=True)
class ValidationEntry:
    """One drop/downgrade decision — the 'not silent accept' record."""

    entity: str
    claim: str
    action: str  # "dropped" | "downgraded"
    reason: str

    def to_payload(self) -> Dict[str, Any]:
        return {
            "entity": self.entity,
            "claim": self.claim,
            "action": self.action,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ValidationReport:
    """Every claim the code refused to accept as-is, with the reason."""

    entries: Tuple[ValidationEntry, ...] = ()

    def to_payload(self) -> Dict[str, Any]:
        return {
            "schema": VALIDATION_REPORT_SCHEMA,
            "entries": [e.to_payload() for e in self.entries],
        }

    def is_clean(self) -> bool:
        return not self.entries


# ---------------------------------------------------------------------------
# Prompt rendering (deterministic: whole chapter source, ordered PIDs)
# ---------------------------------------------------------------------------


def render_entity_extraction_prompt(
    *, chapter_id: str, source: Mapping[str, str],
) -> str:
    """Render the source-only extraction request as one user message.

    Deterministic input: the FULL chapter source PID map in PID order — the
    extractor must see the whole chapter (anchor and alias far apart) to
    establish long-range identity. No translation, no bible, no memory.

    The VALID PIDS section lists every PID of the chapter explicitly
    (dead-PID fix, book-run 1-3: the model invented PIDs and 25/25 claims
    were dropped; with the real list the model can copy instead of guess).
    """
    src_lines = "\n".join(f"  {pid}: {text}" for pid, text in source.items())
    pids_block = _valid_pids_section(source)
    return (
        f"{ENTITY_EXTRACTION_V1.instructions}\n\n"
        f"CHAPTER: {chapter_id}\n\n"
        f"SOURCE (PID -> English text, whole chapter):\n{src_lines}\n\n"
        f"{pids_block}\n"
    )


# ---------------------------------------------------------------------------
# Model-output parsing
# ---------------------------------------------------------------------------

def _normalize_bare_array(raw_list: list) -> Dict[str, Any]:
    """Entity-specific boundary: recover a bare JSON array → ``{"entities": list}``.

    The prompt requires a JSON object with a top-level ``entities`` key, but
    models occasionally return a bare array instead.  This helper is the ONLY
    place such recovery is attempted — it is strictly gated:

    * The list must be non-empty.
    * Every element must be a JSON object (dict).
    * No element may be a scalar, null, or non-object container.

    On success the list is wrapped as ``{"entities": list}`` and a diagnostic
    is logged.  On failure a ``ValueError`` is raised — the caller must NOT
    silently accept malformed payloads.
    """
    if not raw_list:
        raise ValueError(
            "bare JSON array is empty — cannot normalize to entity object"
        )
    for idx, item in enumerate(raw_list):
        if not isinstance(item, dict):
            raise ValueError(
                f"bare JSON array element {idx} is not an object: "
                f"{type(item).__name__} — not a recoverable entity payload"
            )
    LOG.info(
        "entity_extractor: bare JSON array (%d items) normalized to "
        "{\"entities\": ...} object", len(raw_list),
    )
    return {"entities": raw_list}


def parse_model_output(raw: str) -> Dict[str, Any]:
    """Parse the Qwen extraction response into a payload dict.

    Mirrors the audit parser contract: empty/truncated bodies raise the
    retryable ``EmptyResponseError``/``TruncatedJSONError`` (the role
    adapter's B4 retry owns them), a well-formed but wrong-shaped body
    raises ``ValueError`` — never a silent accept. RESILIENCE
    (t_406fc48c): fences / BOM / prose are stripped by the shared
    ``parse_json_response`` utility.

    ENTITY-SPECIFIC NORMALIZATION (t_83bab286): when the model returns a
    bare JSON array of objects, this is a recoverable variant — the list
    is wrapped as ``{"entities": list}`` with a diagnostic log.  Non-object
    elements, empty lists, scalars, and malformed payloads are NOT
    recovered and raise ``ValueError``.
    """
    if not raw.strip():
        raise EmptyResponseError(
            "empty entity-extraction response body (max_tokens exhausted?)"
        )
    try:
        payload = parse_json_response(raw)
    except ValueError as exc:
        # parse_json_response raises ValueError for valid JSON that is not
        # a dict (e.g. a bare list).  Attempt entity-specific normalization
        # before giving up.
        if isinstance(exc, (EmptyResponseError, TruncatedJSONError)):
            raise
        # Use the already-parsed value attached by parse_json_response
        # (avoids re-parsing with inconsistent fence-stripping semantics).
        parsed_value = getattr(exc, "parsed_value", None)
        if parsed_value is not None and isinstance(parsed_value, list):
            return _normalize_bare_array(parsed_value)
        raise ValueError(
            f"entity-extraction payload must be a JSON object: {exc}"
        ) from exc
    return payload


def with_entity_context_metadata(
    payload: Mapping[str, Any],
    *,
    chapter_id: str,
    source_hash: str,
    extractor_version: str = EXTRACTOR_VERSION,
) -> Dict[str, Any]:
    """Deterministically stamp the top-level identity onto a model body.

    The model returns ONLY ``entities`` (see ``ENTITY_EXTRACTION_V1``);
    the top-level ``schema``/``extractor_version``/``chapter_id``/
    ``source_hash`` are provenance the harness owns — they are stamped
    here from the caller's real ``SourceArtifact``, never taken from the
    model (a model-supplied value would be a provenance substitution).
    Any model-supplied top-level keys are overwritten by the real values.
    """
    stamped = dict(payload)
    stamped["schema"] = ENTITY_CONTEXT_SCHEMA
    stamped["extractor_version"] = extractor_version
    stamped["chapter_id"] = chapter_id
    stamped["source_hash"] = source_hash
    return stamped


# ---------------------------------------------------------------------------
# 8-point code validation (§8.3)
# ---------------------------------------------------------------------------


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ValueError(message)


def _ref_status(value: Any, *, what: str, default: str = STATUS_VERIFIED) -> str:
    """Model-supplied ref status: legal values only, fail-closed.

    Anchor/alias status is CODE-derived (verified after the §8.3 checks
    pass); the model is not asked for it at all. A supplied value must
    still be one of the legal statuses — anything else is rejected, never
    stored verbatim.
    """
    if value is None:
        return default
    if not isinstance(value, str) or value not in STATUSES:
        raise ValueError(f"{what} status must be one of {STATUSES}, got {value!r}")
    return value


def _entity_from_payload(item: Any) -> EntityRecord:
    if not isinstance(item, Mapping):
        raise ValueError("entity entry must be a JSON object")
    anchor = item.get("anchor")
    if not isinstance(anchor, Mapping):
        raise ValueError("entity anchor must be an object {pid, span}")
    aliases = tuple(
        AliasRef(
            surface=str(a["surface"]), pid=str(a["pid"]), span=str(a["span"]),
            status=_ref_status(a.get("status"), what="alias"),
        )
        for a in item.get("aliases", [])
    )
    claims = tuple(
        EntityClaim(
            kind=str(c["kind"]),
            value=str(c.get("value", "")),
            status=_ref_status(
                c.get("status"), what="claim", default=STATUS_CANDIDATE,
            ),
            evidence=tuple(
                EvidenceRef(pid=str(e["pid"]), span=str(e["span"]))
                for e in c.get("evidence", [])
            ),
            evidence_windows=tuple(
                (str(w[0]), str(w[1])) for w in c.get("evidence_windows", [])
            ),
        )
        for c in item.get("claims", [])
    )
    return EntityRecord(
        entity=str(item["entity"]),
        canonical_type=str(item["canonical_type"]),
        anchor=AnchorRef(
            pid=str(anchor["pid"]), span=str(anchor["span"]),
            status=_ref_status(anchor.get("status"), what="anchor"),
        ),
        aliases=aliases,
        claims=claims,
    )


def _span_verbatim_in_pid(span: str, pid_text: str) -> bool:
    """Whitespace-insensitive verbatim span check (point 3 of §8.3)."""
    span_norm = " ".join(span.split())
    pid_norm = " ".join(pid_text.split())
    return span_norm in pid_norm


def _is_translation_derived(span: str) -> bool:
    """A span with Cyrillic is translation-derived (point 4 of §8.3)."""
    return bool(_CYRILLIC.search(span))


def _gender_value_matches_pronouns(value: str, pid_text: str) -> bool:
    folded = value.strip().lower()
    if folded in ("male", "м"):
        return bool(_MALE_PRONOUNS.search(pid_text))
    if folded in ("female", "ж"):
        return bool(_FEMALE_PRONOUNS.search(pid_text))
    return False


def _referent_surfaces(record: EntityRecord) -> List[str]:
    """Surfaces that can identify the referent in gender evidence."""
    surfaces = [record.entity, record.canonical_type]
    surfaces.extend(alias.surface for alias in record.aliases)
    return [s for s in surfaces if s]


def _validate_claim(
    claim: EntityClaim,
    *,
    record: EntityRecord,
    source_map: Mapping[str, str],
    entries: List[ValidationEntry],
) -> Optional[EntityClaim]:
    """Validate one claim; return the accepted claim or None (dropped)."""
    label = f"{claim.kind} {claim.value!r}"

    # Point 2: every PID exists.
    claim_pids = [e.pid for e in claim.evidence]
    claim_pids += [w[0] for w in claim.evidence_windows]
    claim_pids += [w[1] for w in claim.evidence_windows]
    for pid in claim_pids:
        if pid not in source_map:
            entries.append(ValidationEntry(
                entity=record.entity, claim=label,
                action="dropped", reason=f"dead PID {pid}",
            ))
            return None

    # Points 3/4: every quoted span verbatim in source, none translation-derived.
    for ev in claim.evidence:
        if _is_translation_derived(ev.span):
            entries.append(ValidationEntry(
                entity=record.entity, claim=label,
                action="dropped",
                reason=f"translation-derived span {ev.span!r} (not source)",
            ))
            return None
        if not _span_verbatim_in_pid(ev.span, source_map.get(ev.pid, "")):
            entries.append(ValidationEntry(
                entity=record.entity, claim=label,
                action="dropped",
                reason=f"span {ev.span!r} not verbatim in {ev.pid}",
            ))
            return None

    status = claim.status
    if claim.kind == "gender":
        # Point 7: gender evidence must link a matching gendered pronoun to
        # the referent in the SAME evidence PID — not just "he/him" anywhere.
        referents = _referent_surfaces(record)
        linked = False
        for ev in claim.evidence:
            pid_text = source_map.get(ev.pid, "")
            if not _gender_value_matches_pronouns(claim.value, pid_text):
                continue
            if any(ref and ref.lower() in pid_text.lower() for ref in referents):
                linked = True
                break
        if not linked:
            entries.append(ValidationEntry(
                entity=record.entity, claim=label,
                action="downgraded",
                reason=(
                    "gender evidence lacks a verifiable referent link "
                    "(pronoun and referent in the same PID)"
                ),
            ))
            status = STATUS_CANDIDATE
    else:
        # Point 8 + §8.3: same_entity relations are semantic hypotheses.
        # Code cannot confirm object identity, so the relation is ALWAYS
        # candidate — a model 'verified' here would be silent self-approval.
        if status == STATUS_VERIFIED:
            entries.append(ValidationEntry(
                entity=record.entity, claim=label,
                action="downgraded",
                reason="same_entity relation is semantic; code cannot confirm it",
            ))
        status = STATUS_CANDIDATE

    return EntityClaim(
        kind=claim.kind, value=claim.value, status=status,
        evidence=claim.evidence, evidence_windows=claim.evidence_windows,
    )


def validate_entity_context(
    payload: Mapping[str, Any],
    *,
    chapter_id: str,
    source_hash: str,
    source: Mapping[str, str],
    extractor_version: str = EXTRACTOR_VERSION,
) -> Tuple[ChapterEntityContext, ValidationReport]:
    """Apply the 8-point §8.3 validation to a parsed model output.

    Point 1 (schema/version/source_hash/chapter_id) fails closed with
    ``ValueError`` — a foreign identity must never be cached as this
    chapter's context. Claim-level failures (dead PID, non-verbatim span,
    translation-derived span, unlinked gender evidence, relation claimed
    verified) drop or downgrade the claim and are recorded in the returned
    ``ValidationReport`` — never silently accepted.
    """
    _require(payload.get("schema") == ENTITY_CONTEXT_SCHEMA, (
        f"entity-context schema mismatch: {payload.get('schema')!r}"
    ))
    _require(payload.get("extractor_version") == extractor_version, (
        f"extractor_version mismatch: {payload.get('extractor_version')!r}"
        f" != {extractor_version!r}"
    ))
    _require(payload.get("chapter_id") == chapter_id, (
        f"chapter_id mismatch: {payload.get('chapter_id')!r} != {chapter_id!r}"
    ))
    _require(payload.get("source_hash") == source_hash, (
        f"source_hash mismatch: {payload.get('source_hash')!r} != {source_hash!r}"
    ))

    source_map = dict(source)
    entities_raw = payload.get("entities")
    if not isinstance(entities_raw, list):
        raise ValueError(
            "entity-context payload must contain a top-level 'entities' "
            f"array, got {type(entities_raw).__name__}"
        )
    entries: List[ValidationEntry] = []
    records: List[EntityRecord] = []
    for item in entities_raw:
        record = _entity_from_payload(item)
        label = f"entity {record.entity!r}"

        # Point 2 for the anchor: the anchor PID must exist.
        if record.anchor.pid not in source_map:
            entries.append(ValidationEntry(
                entity=record.entity, claim="anchor",
                action="dropped",
                reason=f"dead PID {record.anchor.pid}",
            ))
            continue
        anchor_text = source_map[record.anchor.pid]
        # Point 3 for the anchor span.
        if not _span_verbatim_in_pid(record.anchor.span, anchor_text):
            entries.append(ValidationEntry(
                entity=record.entity, claim="anchor",
                action="dropped",
                reason=f"anchor span {record.anchor.span!r} not verbatim in "
                       f"{record.anchor.pid}",
            ))
            continue
        # Point 4 for the anchor span.
        if _is_translation_derived(record.anchor.span):
            entries.append(ValidationEntry(
                entity=record.entity, claim="anchor",
                action="dropped",
                reason=f"translation-derived anchor span {record.anchor.span!r}",
            ))
            continue
        # Point 5: canonical type explicitly in the quoted anchor span
        # (normalized), not merely somewhere in the whole anchor PID.
        anchor_span_norm = " ".join(record.anchor.span.lower().split())
        canonical_norm = " ".join(record.canonical_type.lower().split())
        if canonical_norm not in anchor_span_norm:
            entries.append(ValidationEntry(
                entity=record.entity, claim="canonical_type",
                action="dropped",
                reason=(
                    f"canonical type {record.canonical_type!r} not in anchor "
                    f"span {record.anchor.span!r}"
                ),
            ))
            continue

        # Point 6: each alias surface in its own PID.
        kept_aliases: List[AliasRef] = []
        for alias in record.aliases:
            if alias.pid not in source_map:
                entries.append(ValidationEntry(
                    entity=record.entity, claim=f"alias {alias.surface!r}",
                    action="dropped", reason=f"dead PID {alias.pid}",
                ))
                continue
            alias_text = source_map[alias.pid]
            if _is_translation_derived(alias.span) or not _span_verbatim_in_pid(
                alias.span, alias_text
            ):
                entries.append(ValidationEntry(
                    entity=record.entity, claim=f"alias {alias.surface!r}",
                    action="dropped",
                    reason=(
                        f"alias surface {alias.surface!r} not verbatim in "
                        f"{alias.pid}"
                    ),
                ))
                continue
            # Point 6: the alias SURFACE itself must appear in its own PID
            # (a verbatim span that paraphrases the surface is not enough).
            if alias.surface.lower() not in alias_text.lower():
                entries.append(ValidationEntry(
                    entity=record.entity, claim=f"alias {alias.surface!r}",
                    action="dropped",
                    reason=(
                        f"alias surface {alias.surface!r} not present in "
                        f"its own PID {alias.pid}"
                    ),
                ))
                continue
            kept_aliases.append(alias)

        kept_claims: List[EntityClaim] = []
        for claim in record.claims:
            if claim.kind not in CLAIM_KINDS:
                entries.append(ValidationEntry(
                    entity=record.entity, claim=f"claim kind {claim.kind!r}",
                    action="dropped", reason="unknown claim kind",
                ))
                continue
            accepted = _validate_claim(
                claim, record=record, source_map=source_map, entries=entries
            )
            if accepted is not None:
                kept_claims.append(accepted)

        # F3: anchor/alias status is CODE-derived. The model is not asked for
        # a status at all; a span that passed every §8.3 code check IS verified.
        # Any model-supplied status is overridden here (never stored verbatim).
        verified_anchor = AnchorRef(
            pid=record.anchor.pid, span=record.anchor.span,
            status=STATUS_VERIFIED,
        )
        records.append(EntityRecord(
            entity=record.entity, canonical_type=record.canonical_type,
            anchor=verified_anchor,
            aliases=tuple(
                AliasRef(
                    surface=a.surface, pid=a.pid, span=a.span,
                    status=STATUS_VERIFIED,
                )
                for a in kept_aliases
            ),
            claims=tuple(kept_claims),
        ))

    context = ChapterEntityContext(
        schema=ENTITY_CONTEXT_SCHEMA,
        extractor_version=extractor_version,
        chapter_id=chapter_id,
        source_hash=source_hash,
        entities=tuple(records),
    )
    return context, ValidationReport(entries=tuple(entries))


def cached_context_source_valid(
    context: ChapterEntityContext,
    *,
    source: Mapping[str, str],
) -> Optional[str]:
    """Source-bound content check for a cache-hit context.

    Returns ``None`` when the cached context is still fully source-bound
    valid: every PID exists in the current source, every quoted span
    (anchor/alias/evidence) is verbatim in its PID and not
    translation-derived, the canonical type is inside the anchor span, and
    every alias surface appears in its own PID. Returns a human-readable
    reason for the first violation otherwise.

    Unlike ``validate_entity_context`` this is all-or-nothing and does NOT
    re-apply drop/downgrade judgment: the stored context is already
    post-validation (drops/downgrades applied), and the cache key's
    ``source_hash`` guarantees the same chapter source text — so any
    span/PID that fails the source-bound invariants here means the entry
    was tampered/foreign AFTER validation. The caller must fail closed
    (ignore the entry and recompute), never reuse the altered content.
    Statuses are intentionally not re-checked: they are already
    code-derived and legal (``_entity_from_payload``/``from_payload``
    enforce that), and legitimate downgrades (gender/coreference ->
    ``candidate``) must survive a cache hit.
    """
    source_map = dict(source)
    for record in context.entities:
        anchor = record.anchor
        anchor_text = source_map.get(anchor.pid)
        if anchor_text is None:
            return f"anchor PID {anchor.pid} absent from current source"
        if _is_translation_derived(anchor.span):
            return f"translation-derived anchor span {anchor.span!r}"
        if not _span_verbatim_in_pid(anchor.span, anchor_text):
            return f"anchor span {anchor.span!r} not verbatim in {anchor.pid}"
        canonical_norm = " ".join(record.canonical_type.lower().split())
        anchor_span_norm = " ".join(anchor.span.lower().split())
        if canonical_norm not in anchor_span_norm:
            return (
                f"canonical type {record.canonical_type!r} not in "
                f"anchor span {anchor.span!r}"
            )
        for alias in record.aliases:
            alias_text = source_map.get(alias.pid)
            if alias_text is None:
                return f"alias PID {alias.pid} absent from current source"
            if _is_translation_derived(alias.span) or not _span_verbatim_in_pid(
                alias.span, alias_text
            ):
                return (
                    f"alias surface {alias.surface!r} not verbatim in "
                    f"{alias.pid}"
                )
            if alias.surface.lower() not in alias_text.lower():
                return (
                    f"alias surface {alias.surface!r} not present in "
                    f"its own PID {alias.pid}"
                )
        for claim in record.claims:
            if claim.kind not in CLAIM_KINDS:
                return f"unknown claim kind {claim.kind!r}"
            for window in claim.evidence_windows:
                for pid in window:
                    if pid not in source_map:
                        return (
                            f"evidence window PID {pid} absent from "
                            f"current source"
                        )
            for ev in claim.evidence:
                ev_text = source_map.get(ev.pid)
                if ev_text is None:
                    return f"evidence PID {ev.pid} absent from current source"
                if _is_translation_derived(ev.span) or not _span_verbatim_in_pid(
                    ev.span, ev_text
                ):
                    return f"evidence span {ev.span!r} not verbatim in {ev.pid}"
    return None


# ---------------------------------------------------------------------------
# Per-chapter cache (identity = source_hash + extractor_version)
# ---------------------------------------------------------------------------


def entity_context_cache_key(*, source_hash: str, extractor_version: str) -> str:
    """Deterministic cache identity for one chapter's extraction."""
    return canonical_json_hash({
        "artifact": "pact-v4-chapter-entity-context",
        "source_hash": source_hash,
        "extractor_version": extractor_version,
    })


class EntityContextCache:
    """Exact-match in-memory cache keyed on the chapter extraction identity.

    Mirrors ``pact_v4.phase3.audit.AuditCache``: no disk I/O here —
    persistence across restarts is the caller's job via
    ``to_payload``/``from_payload``. A cache hit returns the validated
    context without another model call.

    Fail-closed provenance (RV t_7e9ab408 finding 2, RV2 fix): every entry
    is stored/restored ONLY under its own identity key
    (``source_hash + extractor_version``). ``put`` and ``from_payload``
    reject a key/context mismatch with ``ValueError`` — a foreign or
    tampered context is never stored under the expected key, and a
    tampered persistent payload is never silently accepted. Structural
    shape is enforced too: a malformed entry (not an object) is rejected
    loudly. Content-vs-source validation happens at the reuse boundary in
    ``extract_entity_context`` (``cached_context_source_valid``), where
    the current ``SourceArtifact`` is available — a persistent round-trip
    cannot bypass it.
    """

    def __init__(self) -> None:
        self._store: Dict[str, ChapterEntityContext] = {}

    def get(self, key: str) -> Optional[ChapterEntityContext]:
        return self._store.get(key)

    def put(self, key: str, context: ChapterEntityContext) -> None:
        expected = entity_context_cache_key(
            source_hash=context.source_hash,
            extractor_version=context.extractor_version,
        )
        if key != expected:
            raise ValueError(
                f"refusing to store context under key {key!r}: identity is "
                f"{expected!r} (source_hash/extractor_version mismatch — "
                f"foreign/tampered context)"
            )
        self._store[key] = context

    def to_payload(self) -> Dict[str, Any]:
        return {
            "schema": CACHE_SCHEMA,
            "entries": [
                {"key": key, "context": context.to_payload()}
                for key, context in sorted(self._store.items())
            ],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "EntityContextCache":
        if payload.get("schema") != CACHE_SCHEMA:
            raise ValueError(
                f"Foreign identity: entity-context-cache schema="
                f"{payload.get('schema')!r}"
            )
        cache = cls()
        for item in payload.get("entries", []):
            if not isinstance(item, Mapping):
                raise ValueError(
                    "cache payload entry must be an object {key, context}, "
                    f"got {type(item).__name__}"
                )
            key = str(item["key"])
            context = ChapterEntityContext.from_payload(item["context"])
            # Fail-closed restore: the stored key must be the identity of
            # the context itself — a tampered/foreign entry is rejected,
            # never silently accepted.
            expected = entity_context_cache_key(
                source_hash=context.source_hash,
                extractor_version=context.extractor_version,
            )
            if key != expected:
                raise ValueError(
                    f"cache payload entry key {key!r} does not match context "
                    f"identity {expected!r} — tampered/foreign entry rejected"
                )
            cache.put(key, context)
        return cache


# ---------------------------------------------------------------------------
# Backend role adapter + orchestrator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackendEntityExtractorConfig:
    """Extraction call settings (source-only, temp=0, deterministic).

    ``max_tokens`` must cover the server's reasoning budget PLUS content
    headroom — the llama-server counts reasoning and content TOGETHER
    against ``max_tokens`` (same contract as the audit's
    ``DEFAULT_MAX_TOKENS``). The 12000 value (= 8192 reasoning + ~3800
    headroom) is enough for audit chunks (3.6k input, short reasoning) but
    NOT for the extractor: its input is the WHOLE chapter (~16k tokens),
    which provokes reasoning to the full 8192 budget, leaving only ~3800
    tokens for the entities JSON — the response was truncated exactly at
    12000 tokens (3/3 retries, run_009 2026-08-11). 20000 = 8192 reasoning
    + ~11800 content headroom (entities JSON for a 400-PID chapter).
    """

    max_tokens: int = 20000
    label: str = "b1.2/entity_extractor"
    retry: JsonRetryPolicy = field(default_factory=JsonRetryPolicy)


class BackendEntityExtractor:
    """Transport-neutral Qwen entity extractor over a ``CompletionBackend``.

    Renders the source-only prompt, sends it at temperature 0 (deterministic
    input/output) with the JSON-object response schema, and returns the raw
    assistant text. B4 JSON resilience: an empty/truncated body is retried
    (bounded, exponential backoff) by re-issuing the identical request;
    transport failures are never retried here and raise ``CompletionError``.
    """

    def __init__(
        self,
        backend: CompletionBackend,
        *,
        config: Optional[BackendEntityExtractorConfig] = None,
    ) -> None:
        self._backend = backend
        self._config = config or BackendEntityExtractorConfig()
        self._max_tokens = int(self._config.max_tokens)

    @property
    def backend(self) -> CompletionBackend:
        return self._backend

    def __call__(
        self,
        *,
        chapter_id: str,
        source: Mapping[str, str],
        out_dir: Optional[Path] = None,
    ) -> str:
        prompt = render_entity_extraction_prompt(
            chapter_id=chapter_id, source=dict(source)
        )
        reasoning_path: Optional[Path] = None
        if out_dir is not None:
            out_dir = Path(out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            reasoning_path = out_dir / "b1.2_entity_reasoning.txt"
        request = CompletionRequest(
            model_ref=_model_ref_for(self._backend, "entity_extractor"),
            messages=(Message(role="user", content=prompt),),
            max_output_tokens=self._max_tokens,
            temperature=0.0,
            response_schema=JSON_OBJECT_SCHEMA,
            label=self._config.label,
            # REASONING-STREAM: the reasoning file is created before the call
            # and grows live (gemma_rewrite_v4 pattern); the final marked
            # write below stays authoritative.
            on_reasoning_chunk=open_reasoning_writer(reasoning_path),
        )
        attempts: List[Tuple[int, str, str]] = []  # (attempt_no, raw, reasoning)

        def _complete() -> str:
            try:
                response = self._backend.complete(request)
            except CompletionError as exc:
                LOG.error("BackendEntityExtractor: backend failure: %s", exc)
                raise
            raw = response.text or ""
            reasoning = str((response.raw_metadata or {}).get("reasoning") or "")
            attempts.append((len(attempts) + 1, raw, reasoning))
            return raw

        try:
            return retry_json_call(
                _complete, self._config.retry, label=self._config.label
            )
        finally:
            # Persist every attempt's raw + reasoning (ATTEMPT N markers when
            # a JSON retry happened) — a parse failure leaves a disk trail.
            self._write_entity_artifacts(out_dir=out_dir, attempts=attempts)

    @staticmethod
    def _write_entity_artifacts(
        *,
        out_dir: Optional[Path],
        attempts: List[Tuple[int, str, str]],
    ) -> None:
        """Persist ``b1.2_entity_reasoning.txt`` + ``b1.2_entity_raw.txt``.

        Single attempt -> plain content; multiple attempts (JSON retry) ->
        ``ATTEMPT N`` sections so each attempt's reasoning/raw is preserved
        for parse diagnostics (spec FIX 1: append with ATTEMPT N marker).
        """
        if out_dir is None or not attempts:
            return
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        if len(attempts) == 1:
            reason_text = attempts[0][2]
            raw_text = attempts[0][1]
        else:
            reason_text = "\n\n".join(
                f"ATTEMPT {n}\n{reasoning}" for n, _, reasoning in attempts
            )
            raw_text = "\n\n".join(
                f"ATTEMPT {n}\n{raw}" for n, raw, _ in attempts
            )
        (out_dir / "b1.2_entity_reasoning.txt").write_text(
            reason_text, encoding="utf-8"
        )
        (out_dir / "b1.2_entity_raw.txt").write_text(raw_text, encoding="utf-8")


def _model_ref_for(backend: CompletionBackend, role: str) -> str:
    """Resolve the role->model binding from the backend descriptor."""
    bindings = backend.descriptor.model_bindings
    ref = bindings.get(role)
    if not ref:
        ref = bindings.get("default")
    if not ref:
        raise ValueError(
            f"no model binding for role {role!r}; "
            f"backend model_bindings={dict(bindings)!r}"
        )
    return ref


@dataclass(frozen=True)
class EntityExtractionResult:
    """One chapter's validated extraction plus its provenance."""

    context: ChapterEntityContext
    validation: ValidationReport
    from_cache: bool = False

    def to_payload(self) -> Dict[str, Any]:
        return {
            "context": self.context.to_payload(),
            "validation": self.validation.to_payload(),
            "from_cache": self.from_cache,
        }


def extract_entity_context(
    *,
    source_artifact: SourceArtifact,
    extractor: Any,
    cache: Optional[EntityContextCache] = None,
    extractor_version: str = EXTRACTOR_VERSION,
    retry: Optional[JsonRetryPolicy] = None,
    out_dir: Optional[Path] = None,
) -> EntityExtractionResult:
    """Source-only prepass: cache hit -> reuse; miss -> Qwen -> validate.

    ``extractor`` is duck-typed (any object exposing ``__call__(*,
    chapter_id, source, out_dir=None) -> raw str``); the reference
    implementation is ``BackendEntityExtractor`` /
    ``LifecycleQwenEntityExtractor``. ``out_dir`` is forwarded to the
    extractor so it can persist its ``b1.2_entity_reasoning.txt`` /
    ``b1.2_entity_raw.txt`` artifacts (REASONING-STREAM; artifact only,
    never part of cache identity). The model is called ONCE per chapter
    when the cache misses; the validated result is stored under
    ``source_hash + extractor_version`` so resume never repeats the call.

    Fail-closed (RV t_7e9ab408 findings 1+2, RV2 fix): the model body is
    stamped with the harness-owned top-level metadata (``schema``/
    ``extractor_version``/``chapter_id``/``source_hash``) BEFORE validation
    — the real model output can pass without the model fabricating
    provenance. A cache hit is trusted only when the cached context's
    ``chapter_id``/``source_hash``/``extractor_version`` match the current
    ``SourceArtifact`` AND its content is still fully source-bound
    (``cached_context_source_valid``: anchor/alias/evidence PIDs and
    spans, alias surfaces, canonical-type-in-anchor-span); a
    foreign/tampered entry — even with intact metadata and key — is
    ignored and recomputed, never returned ``from_cache=True``.
    """
    source_map = dict(source_artifact.source)
    key = entity_context_cache_key(
        source_hash=source_artifact.source_hash,
        extractor_version=extractor_version,
    )
    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            if (
                cached.schema == ENTITY_CONTEXT_SCHEMA
                and cached.chapter_id == source_artifact.chapter_id
                and cached.source_hash == source_artifact.source_hash
                and cached.extractor_version == extractor_version
            ):
                # RV2 HIGH fix: metadata/key identity is NOT enough — the
                # cached content itself must still be source-bound (same
                # source_hash guarantees the same chapter text, so any span
                # that is not verbatim / any dead PID is tampering). Fail
                # closed: ignore and recompute, never from_cache=True.
                content_problem = cached_context_source_valid(
                    cached, source=source_map
                )
                if content_problem is None:
                    LOG.info("entity_extractor: cache hit for %s", key[:12])
                    return EntityExtractionResult(
                        context=cached, validation=ValidationReport(),
                        from_cache=True,
                    )
                LOG.warning(
                    "entity_extractor: cache entry %s has tampered/foreign "
                    "content (%s); recomputing", key[:12], content_problem,
                )
            else:
                LOG.warning(
                    "entity_extractor: cache entry %s has foreign/tampered "
                    "metadata; recomputing", key[:12],
                )

    raw = extractor(
        chapter_id=source_artifact.chapter_id,
        source=source_map,
        out_dir=out_dir,
    )
    payload = parse_model_output(raw)
    stamped = with_entity_context_metadata(
        payload,
        chapter_id=source_artifact.chapter_id,
        source_hash=source_artifact.source_hash,
        extractor_version=extractor_version,
    )
    context, report = validate_entity_context(
        stamped,
        chapter_id=source_artifact.chapter_id,
        source_hash=source_artifact.source_hash,
        source=source_map,
        extractor_version=extractor_version,
    )
    if cache is not None:
        cache.put(key, context)
    return EntityExtractionResult(context=context, validation=report)


__all__ = [
    "ENTITY_CONTEXT_SCHEMA",
    "EXTRACTOR_VERSION",
    "VALIDATION_REPORT_SCHEMA",
    "ENTITY_EXTRACTION_V1",
    "AnchorRef",
    "AliasRef",
    "EvidenceRef",
    "EntityClaim",
    "EntityRecord",
    "ChapterEntityContext",
    "ValidationEntry",
    "ValidationReport",
    "BackendEntityExtractor",
    "BackendEntityExtractorConfig",
    "EntityContextCache",
    "EntityExtractionResult",
    "entity_context_cache_key",
    "extract_entity_context",
    "parse_model_output",
    "render_entity_extraction_prompt",
    "validate_entity_context",
    "cached_context_source_valid",
    "with_entity_context_metadata",
]
