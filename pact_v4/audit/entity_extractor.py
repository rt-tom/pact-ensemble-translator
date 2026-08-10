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
  a cache hit resumes without another model call.
* **Never authorizes repair**: this context is a hint for the auditor
  (fallback evidence level 3); any finding that relies on a semantic
  relation stays Tier B (rule §5.3).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
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
    retry_json_call,
)
from pact_v4.runtime.prompts_runtime import ReviewerPrompt

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
        "with exactly this schema:\n"
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
        "the anchor PID.\n"
        "3. Alias facts: each alias surface must appear verbatim in its own "
        "PID.\n"
        "4. Gender facts: state gender only where the source shows it "
        "(e.g. he/him for male, she/her for female) with the referent in "
        "the same PID.\n"
        "5. Uncertain semantic coreference (e.g. 'bike' later = the "
        "'motorcycle' established earlier) is a 'candidate' relation, not "
        "'verified'.\n"
        "6. If the chapter has no persistent long-range entities, return "
        '{"entities": []}.'
    ),
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
        entities = tuple(
            _entity_from_payload(item) for item in payload.get("entities", [])
        )
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
    """
    src_lines = "\n".join(f"  {pid}: {text}" for pid, text in source.items())
    return (
        f"{ENTITY_EXTRACTION_V1.instructions}\n\n"
        f"CHAPTER: {chapter_id}\n\n"
        f"SOURCE (PID -> English text, whole chapter):\n{src_lines}\n"
    )


# ---------------------------------------------------------------------------
# Model-output parsing
# ---------------------------------------------------------------------------


def _strip_fences(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return text


def parse_model_output(raw: str) -> Dict[str, Any]:
    """Parse the Qwen extraction response into a payload dict.

    Mirrors the audit parser contract: empty/truncated bodies raise the
    retryable ``EmptyResponseError``/``TruncatedJSONError`` (the role
    adapter's B4 retry owns them), a well-formed but wrong-shaped body
    raises ``ValueError`` — never a silent accept.
    """
    if not raw.strip():
        raise EmptyResponseError(
            "empty entity-extraction response body (max_tokens exhausted?)"
        )
    try:
        payload = json.loads(_strip_fences(raw))
    except json.JSONDecodeError as exc:
        raise TruncatedJSONError(
            f"entity-extraction response is not complete JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError(
            f"entity-extraction payload must be a JSON object, got {type(payload).__name__}"
        )
    return payload


# ---------------------------------------------------------------------------
# 8-point code validation (§8.3)
# ---------------------------------------------------------------------------


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ValueError(message)


def _entity_from_payload(item: Any) -> EntityRecord:
    if not isinstance(item, Mapping):
        raise ValueError("entity entry must be a JSON object")
    anchor = item.get("anchor")
    if not isinstance(anchor, Mapping):
        raise ValueError("entity anchor must be an object {pid, span}")
    aliases = tuple(
        AliasRef(
            surface=str(a["surface"]), pid=str(a["pid"]), span=str(a["span"]),
            status=str(a.get("status", STATUS_VERIFIED)),
        )
        for a in item.get("aliases", [])
    )
    claims = tuple(
        EntityClaim(
            kind=str(c["kind"]),
            value=str(c.get("value", "")),
            status=str(c.get("status", STATUS_CANDIDATE)),
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
            status=str(anchor.get("status", STATUS_VERIFIED)),
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
    entries: List[ValidationEntry] = []
    records: List[EntityRecord] = []
    for item in payload.get("entities", []):
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
        # Point 5: canonical type explicitly in anchor evidence.
        if record.canonical_type.lower() not in anchor_text.lower():
            entries.append(ValidationEntry(
                entity=record.entity, claim="canonical_type",
                action="dropped",
                reason=(
                    f"canonical type {record.canonical_type!r} not in anchor "
                    f"evidence {record.anchor.pid}"
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

        records.append(EntityRecord(
            entity=record.entity, canonical_type=record.canonical_type,
            anchor=record.anchor, aliases=tuple(kept_aliases),
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
    """

    def __init__(self) -> None:
        self._store: Dict[str, ChapterEntityContext] = {}

    def get(self, key: str) -> Optional[ChapterEntityContext]:
        return self._store.get(key)

    def put(self, key: str, context: ChapterEntityContext) -> None:
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
            cache.put(str(item["key"]), ChapterEntityContext.from_payload(item["context"]))
        return cache


# ---------------------------------------------------------------------------
# Backend role adapter + orchestrator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackendEntityExtractorConfig:
    """Extraction call settings (source-only, temp=0, deterministic)."""

    max_tokens: int = 4096
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

    def __call__(self, *, chapter_id: str, source: Mapping[str, str]) -> str:
        prompt = render_entity_extraction_prompt(
            chapter_id=chapter_id, source=dict(source)
        )
        request = CompletionRequest(
            model_ref=_model_ref_for(self._backend, "entity_extractor"),
            messages=(Message(role="user", content=prompt),),
            max_output_tokens=self._max_tokens,
            temperature=0.0,
            response_schema=JSON_OBJECT_SCHEMA,
            label=self._config.label,
        )

        def _complete() -> str:
            try:
                return self._backend.complete(request).text
            except CompletionError as exc:
                LOG.error("BackendEntityExtractor: backend failure: %s", exc)
                raise

        return retry_json_call(_complete, self._config.retry, label=self._config.label)


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
) -> EntityExtractionResult:
    """Source-only prepass: cache hit -> reuse; miss -> Qwen -> validate.

    ``extractor`` is duck-typed (any object exposing ``__call__(*,
    chapter_id, source) -> raw str``); the reference implementation is
    ``BackendEntityExtractor`` / ``LifecycleQwenEntityExtractor``. The model
    is called ONCE per chapter when the cache misses; the validated result
    is stored under ``source_hash + extractor_version`` so resume never
    repeats the call.
    """
    source_map = dict(source_artifact.source)
    key = entity_context_cache_key(
        source_hash=source_artifact.source_hash,
        extractor_version=extractor_version,
    )
    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            LOG.info("entity_extractor: cache hit for %s", key[:12])
            return EntityExtractionResult(
                context=cached, validation=ValidationReport(), from_cache=True
            )

    raw = extractor(
        chapter_id=source_artifact.chapter_id, source=source_map
    )
    payload = parse_model_output(raw)
    context, report = validate_entity_context(
        payload,
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
]
