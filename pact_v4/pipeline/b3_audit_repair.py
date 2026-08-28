"""B3: production audit/repair pipeline (concept §10 B3 + §9.4).

Wires the B-series components into one orchestrator the strict runner can
call after whole-chapter generation:

    1. entity context (B1.2, optional, ``entity_context_enabled``) —
       ``extract_entity_context(source)`` -> per-chapter cache
       (``source_hash + extractor_version``), rendered into the audit
       prompt's ``CHAPTER ENTITY FACTS`` block and passed to the hard
       filters (entity-PID issues are forced to TIER_B, §5.3);
    2. chunked audit (B1) — ``ChunkedAuditEvaluator`` over the whole
       chapter pairs (narrator + entity blocks), replacing the old
       ``gemma_russian_review`` / ``qwen_fidelity`` gates;
    3. gate: ``audit_complete == False`` -> FAIL-CLOSED (no repair, the
       chapter is never released as passed audit);
    4. hard filters (B1.1) — ``apply_hard_filters`` -> CONFIRMED /
       REJECTED / TIER_B;
    5. selective repair (B2) — ``SelectiveRepairEvaluator`` (Tier A
       direct, Tier B verify-before-repair) with its single post-repair
       re-audit of the changed PIDs;
    6. ``translations_repaired`` = raw map + committed repairs.

Provenance / cache contract:

* ``audit_journal.ndjson`` (schema ``pact-v4-b3-audit-journal/v1``) —
  append-only events ``audit_started`` / ``audit_chunk_started`` /
  ``audit_chunk_done`` / ``audit_complete`` / ``audit_failed`` (terminal
  pre/model-call evaluator failure — always followed by a fail-closed
  ``gate``) / ``finding`` / ``repair_round`` / ``reaudit_scope`` /
  ``gate``. This is a SEPARATE
  file from the generation ``journal.ndjson``: the whole-chapter resume
  contract requires exactly one generation entry, so audit events can
  never share that file.
* ``audit_cache_b3.json`` (schema ``pact-v4-b3-audit-cache/v1``) — audit
  cache identity = ``snapshot_hash + translation_hash + config_identity +
  backend_identity_hash + prompt_version + harness_version +
  entity_context_hash`` (the card's identity, plus the exact translation
  content — the audit outcome is a function of both source and translation;
  entity hash present only when entity context is enabled). A full cache
  hit reuses the stored outcome (0 model calls). A cached
  ``audit_complete=False`` with an intact identity is a PARTIAL hit
  (PARTIAL-RESUME t_a58dd881): GOOD/GOOD_RETRIED audit chunks and GOOD
  R-editor chunks are replayed with 0 model calls (their stored issues /
  parse-validated edits reused verbatim), the TRANSPORT_ERROR/EMPTY/FAILED
  chunks are re-run; the fail-closed gate stays — the chapter is never
  released as passed audit until EVERY chunk has a status, a cache is never
  downgraded, and an identity mismatch is still a full miss.
* ``entity_context_cache.json`` (schema from ``entity_extractor``) — the
  B1.2 per-chapter entity cache (``source_hash + extractor_version``).
* ``entity_context_validation_report.json`` (schema
  ``pact-v4-entity-extractor-validation/v1``) — B3-DIAG transparency:
  what the model PROPOSED vs what the code ACCEPTED. Written next to the
  entity cache whenever a FRESH extraction ran validation (drop/downgrade
  decisions with reasons from ``EntityExtractionResult.validation``). A
  cache hit reuses the previously validated context and never clobbers the
  original report; an extractor failure (never reaching validation) writes
  neither the cache nor the report.

Transport: audit/repair/entity calls go through ``CompletionBackend``
(``build_role_backend``), so the same pipeline serves local, remote and
composite profiles. Remote audit through ``opencode serve`` is a
CONTRACT, NOT tested yet (owner decision: test remote audit after the
B-phase; the AUDIT evaluators never emit ``request_options`` — the audit
reasoning budget is a server arg). The REPAIR evaluator is the one
exception (REPAIR-ROBUST, card t_b6fd6cbd): its per-batch reasoning
effort (default 1 = low) travels via request_options ONLY to remote
transports that support it (opencode ``reasoningEffort``); local
llama-server transports receive the budget from their server args and
reject request_options, so the local path is untouched.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple, AbstractSet

from pact_v4.audit.chunked_audit import (
    AUDIT_V4_CATEGORIES,
    AUDIT_V4_CONFIDENCES,
    AUDIT_V4_SEVERITIES,
    DEFAULT_MAX_INPUT_TOKENS,
    DEFAULT_MAX_TOKENS,
    DEFAULT_OVERLAP_TOKENS,
    DEFAULT_REASONING_BUDGET,
    DEFAULT_TRANSPORT_MAX_RETRIES,
    DEFAULT_TRANSPORT_BASE_DELAY_SECONDS,
    HARNESS_VERSION,
    PROMPT_VERSION,
    AuditPair,
    ChunkedAuditConfig,
    ChunkedAuditEvaluator,
    ChunkedAuditOutcome,
    audit_model_ref,
    build_narrator_context,
    pairs_from_maps,
)
from pact_v4.audit.entity_extractor import (
    EXTRACTOR_VERSION,
    STATUS_VERIFIED,
    AliasRef,
    BackendEntityExtractor,
    BackendEntityExtractorConfig,
    ChapterEntityContext,
    EntityContextCache,
    EntityExtractionResult,
    EntityRecord,
    extract_entity_context,
)
from pact_v4.audit.hard_filters import FilteredIssue, apply_hard_filters
from pact_v4.audit.russian_editor import (
    RUSSIAN_EDITOR_HARNESS_VERSION,
    RUSSIAN_EDITOR_PROMPT_VERSION,
    SAFE_CLASSES,
    ALL_CLASSES,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_MAX_TOKENS as RUSSIAN_EDITOR_DEFAULT_MAX_TOKENS,
    DEFAULT_OVERLAP_PAIRS,
    MAX_EDITS_PER_PID,
    DEFAULT_RETRY_MAX_RETRIES,
    DEFAULT_RETRY_BASE_DELAY_SECONDS,
    ReviewCandidate,
    RussianEditorConfig,
    RussianEditorEvaluator,
    RussianEditorOutcome,
)
from pact_v4.phase1.models import SourceArtifact, canonical_json_hash
from pact_v4.pipeline.glossary_resolver import (
    RESOLVER_VERSION as GLOSSARY_RESOLVER_VERSION,
    PROMPT_VERSION as GLOSSARY_PROMPT_VERSION,
    RESPONSE_SCHEMA as GLOSSARY_RESPONSE_SCHEMA,
    GLOSSARY_PROPOSAL_SCHEMA,
    compute_allowed_evidence_pids,
    candidate_input_hash as glossary_candidate_input_hash,
    translation_hash as glossary_translation_hash,
    semantic_translation_hash,
    sidecar_path as glossary_sidecar_path,
    atomic_write_sidecar,
    load_and_validate_sidecar,
    validate_sidecar_payload,
    GlossaryResolver,
    build_sidecar_payload,
)
from pact_v4.repair.selective_repair import (
    DEFAULT_REAUDIT_BASE_DELAY_SECONDS,
    DEFAULT_REAUDIT_MAX_INPUT_TOKENS,
    DEFAULT_REAUDIT_MAX_OVERLAP_PAIRS,
    DEFAULT_REAUDIT_MAX_RETRIES,
    DEFAULT_REAUDIT_MAX_TOKENS,
    DEFAULT_REAUDIT_MIN_OVERLAP_PAIRS,
    DEFAULT_REAUDIT_NEIGHBOUR_WINDOW,
    DEFAULT_REAUDIT_OVERLAP_TOKENS,
    DEFAULT_REPAIR_CONTEXT_WINDOW,
    DEFAULT_REPAIR_CONTEXT_WINDOW_BY_CATEGORY,
    DEFAULT_REPAIR_MAX_TOKENS,
    DEFAULT_REPAIR_REASONING,
    REAUDIT_DELTA_FORMAT,
    MICROBATCH_TARGET,
    MICROBATCH_TRIGGER,
    REPAIR_FINDINGS_CAP,
    REPAIR_HARNESS_VERSION,
    REPAIR_PROMPT_VERSION,
    SelectiveRepairConfig,
    SelectiveRepairEvaluator,
    SelectiveRepairOutcome,
)
from pact_v4.runtime.backend_protocol import (
    BackendDescriptor,
    CompletionBackend,
    CompletionRequest,
)
from pact_v4.runtime.json_resilience import JsonRetryPolicy

LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Artifact schemas (identity-bearing, never reused across a schema change)
# ---------------------------------------------------------------------------

B3_AUDIT_CACHE_SCHEMA = "pact-v4-b3-audit-cache/v1"
B3_AUDIT_JOURNAL_SCHEMA = "pact-v4-b3-audit-journal/v1"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Atomic write (write temp, fsync, replace) with a UTF-8 JSON payload.

    KILL-SAFE-INCREMENTAL (t_2d16962c): the incremental audit-cache
    rewrites must survive a kill at ANY point — the temp file is flushed
    and fsynced to disk BEFORE the rename, so a torn/truncated
    ``audit_cache_b3.json`` can never be left behind (a bare
    write-then-rename can publish unflushed data after a crash).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2)
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Entity-context block renderer (deterministic, source-derived)
# ---------------------------------------------------------------------------


def _alias_is_source_apposed(record: EntityRecord, alias: AliasRef) -> bool:
    """Code-proven source-established alias (RV3 HIGH, 2026-08-14).

    A same-entity alias is a semantic coreference hypothesis the code
    cannot confirm (point 8 of §8.3 downgrades every alias_relation /
    object_identity claim to candidate). The ONLY alias whose coreference
    is provable is one the source itself apposes to the canonical type:
    the alias surface appears INSIDE the anchor span that names the
    canonical type (same PID), e.g. ``"the woman, Rose"``. Any other
    alias may reach the audit block but never the generation prompt.

    RV finding 2 (SAFE-MEMORY, 2026-08-14): the apposition check must be
    BOUNDARY-SAFE — ``"Rose"`` inside ``"the woman, Rosemary"`` is NOT an
    apposition, only a substring of the anchor's own word. The old
    ``surface_norm in anchor_norm`` substring test let a short form
    (surface ``Rose``, span ``Rosemary``) leak into the verified-only
    Translator block. The surface must occur as an independent word
    (``(?<!\\w)`` / ``(?!\\w)``) in the anchor text.
    """
    if alias.pid != record.anchor.pid:
        return False
    anchor_norm = " ".join(record.anchor.span.lower().split())
    surface_norm = " ".join(alias.surface.lower().split())
    if not surface_norm:
        return False
    return (
        re.search(rf"(?<!\w){re.escape(surface_norm)}(?!\w)", anchor_norm)
        is not None
    )


def render_entity_context_block(
    context: ChapterEntityContext,
    *,
    verified_only: bool = False,
    role_view_card: Optional[str] = None,
) -> str:
    """Render a validated ``ChapterEntityContext`` into the audit prompt's
    ``CHAPTER ENTITY FACTS - SOURCE-DERIVED`` block.

    Deterministic (sorted by entity name); the block is data for the
    auditor (evidence level 3: source > adjacent > chapter facts), never
    an instruction. Empty context -> empty string (caller omits the
    block).

    ``role_view_card`` (v4.2 book-memory role views): an OPTIONAL bounded
    ``audit_repair`` consistency card from the frozen pre-chapter canonical
    state. When provided it is appended after the source-derived block. It is
    disabled by default (``None``) so the v4.1 B3 prompt is byte-identical.

    ``verified_only=True`` (generation-prompt variant, owner decision
    2026-08-14): only claims whose status is ``verified`` are rendered —
    candidate claims are semantic hypotheses and go ONLY to the audit
    block, never to the translator's prompt. The anchor is code-verified
    by construction and always shown. Aliases are filtered the same way
    (RV3 HIGH): a same-entity alias is a semantic hypothesis unless the
    source apposes it to the canonical type inside the anchor span, so
    only ``_alias_is_source_apposed`` aliases are rendered here; every
    other alias stays in the full audit block.
    """
    if not context.entities:
        block = ""
    else:
        lines: list = []
        for record in sorted(context.entities, key=lambda r: r.entity):
            claims = (
                [c for c in record.claims if c.status == STATUS_VERIFIED]
                if verified_only
                else list(record.claims)
            )
            aliases = record.aliases
            if verified_only:
                aliases = tuple(
                    a for a in record.aliases if _alias_is_source_apposed(record, a)
                )
            lines.append(f"- entity: {record.entity}")
            lines.append(f"  established_type: {record.canonical_type}")
            anchor = record.anchor
            lines.append(
                f"  anchor: \"{anchor.span}\" (pid {anchor.pid}, {anchor.status})"
            )
            for alias in aliases:
                lines.append(
                    f"  alias: \"{alias.surface}\" (pid {alias.pid}, {alias.status})"
                )
            for claim in claims:
                evidence = ", ".join(
                    f"{ev.pid} \"{ev.span}\"" for ev in claim.evidence
                )
                windows = ", ".join(
                    f"[{a}-{b}]" for a, b in claim.evidence_windows
                )
                detail = f"evidence: {evidence}" if evidence else ""
                if windows:
                    detail += f" windows: {windows}"
                lines.append(
                    f"  claim: {claim.kind}={claim.value!r} ({claim.status})"
                    + (f" {detail}" if detail else "")
                )
    if context.entities:
        block = "\n".join(lines) + "\n"
    return _append_role_view_card(block, role_view_card)


def _append_role_view_card(block: str, role_view_card: Optional[str]) -> str:
    """Append an OPTIONAL bounded role-view card to a prompt block (no-op when
    ``None``). Keeps the v4.1 path byte-identical when no card is supplied."""
    if not role_view_card:
        return block
    sep = "" if block.endswith("\n") else "\n"
    return f"{block}{sep}\n{role_view_card}"


def render_entity_context_to_hard_filters(
    context: ChapterEntityContext,
) -> Mapping[str, Any]:
    """Payload form for ``apply_hard_filters`` (the ``entities`` list)."""
    return context.to_payload()


def book_memory_observations_from_entity_context(
    context: ChapterEntityContext,
    *,
    chapter_id: str,
) -> Dict[str, Any]:
    """Convert a validated entity context into MemoryManager observations.

    P0 owner decision 2026-08-14 (verified → book_memory promote): after an
    ACCEPTED chapter the run promotes the source-verified entity facts into
    ``book_memory.json`` — no reviewer round-trip, no extra model calls.

    Rules (conservative, fail-closed):

    * **Entity identity** — every entity whose anchor survived the 8-point
      validation is source-verified (its ``canonical_type`` is named
      verbatim in the chapter source at the anchor PID). It becomes an
      ``entities:<name>`` observation carrying ``type`` (from
      ``canonical_type``) and the evidence PIDs.
    * **Person → characters** — an entity with a VERIFIED ``gender`` claim
      is a person: it promotes to ``characters:<name>`` with that gender.
      Gender is high-precision by construction — the extractor's validation
      only marks a gender claim ``verified`` when a matching gendered
      pronoun and the referent surface appear in the SAME evidence PID
      (extreme-conservative rule: unknown is safer than wrong; Rosalyn →
      male and English → female poison classes are impossible here).
    * **Verified claims → facts** — each verified claim becomes a
      ``facts:<name>:<kind>`` observation (source-bound text with the
      evidence PIDs).
    * **Candidate claims are NEVER promoted** — they are semantic
      hypotheses that live only in the audit (TIER_B); promotion of a
      candidate would be self-approval.

    Returns ``{"book_memory": {obs_key: obs_value}}`` — empty when the
    context has no promoted content. The caller feeds this into
    ``MemoryManager.add_observation`` before ``promote``.
    """
    observations: Dict[str, Any] = {}
    for record in sorted(context.entities, key=lambda r: r.entity):
        if not record.entity:
            continue
        verified_gender = ""
        gender_evidence_pids: List[str] = []
        facts: List[Dict[str, Any]] = []
        for claim in record.claims:
            if claim.status != STATUS_VERIFIED:
                continue
            evidence_pids = sorted({ev.pid for ev in claim.evidence})
            if claim.kind == "gender":
                verified_gender = str(claim.value)
                gender_evidence_pids = evidence_pids
                continue
            facts.append({
                "fact": (
                    f"{record.entity}: {claim.value} "
                    f"(source-derived, {claim.kind})"
                ),
                "source_pids": evidence_pids,
                "chapter": chapter_id,
                "keys": [record.entity],
                "status": "verified",
            })
        # Build per-alias provenance (variants as provenance objects)
        variants: Dict[str, Any] = {}
        for alias in record.aliases:
            # Only include alias surfaces that are verified (status verified is code-derived)
            if alias.surface and alias.surface != record.entity:
                variants[alias.surface] = {"chapter": chapter_id, "source_pids": [alias.pid]}
        mc = getattr(record, "memory_class", "chapter_local")
        section = "characters" if verified_gender else "entities"
        identity: Dict[str, Any] = {
            "type": record.canonical_type or "object",
            "memory_class": mc,
            "first_seen_chapter": chapter_id,
            "chapters": [chapter_id],
            "variants": variants,
            "field_provenance": {},
            "forbidden_targets": [],
        }
        if verified_gender:
            identity["gender"] = verified_gender
            identity["field_provenance"]["gender"] = {"chapter": chapter_id, "source_pids": gender_evidence_pids}
        # Ensure field_provenance is present even if empty for v2 readers
        observations[f"{section}:{record.entity}"] = identity
        if facts:
            for idx, fact in enumerate(facts):
                observations[f"facts:{record.entity}:{idx}"] = fact
    return {"book_memory": observations}


# ---------------------------------------------------------------------------
# GLOSSARY-FROM-ENTITY (owner decision 2026-08-15, variant B): verified
# entities -> glossary candidates with targets aligned from the translation.
# ---------------------------------------------------------------------------


def _is_proper_noun_entity_name(name: str) -> bool:
    """Glossary-worthiness filter: the entity name is a proper noun.

    Only person/place/world-term entities belong in the glossary; everyday
    objects (pocketwatch, upstairs bathroom mirror, Joel's car) do not. The
    extractor names persons/places by their proper-noun display name (Rose,
    Blake Thorburn, Hillsglade House) and objects by their common noun
    (pocketwatch, mirror, car), so a deterministic title-case test on the
    entity name separates the two without a prompt change or an LLM call:
    every whitespace-separated word must start with an uppercase letter
    (''Joel's car'' fails because ''car'' is lowercase). A single-word
    lowercase name (''pocketwatch'') fails too.
    """
    words = [w for w in name.split() if w]
    if not words:
        return False
    return all(w[0].isupper() for w in words)


def _flat_glossary_target(value: Any) -> Optional[str]:
    """Flat glossary target for an on-disk entry (str or ``{target: ...}``)."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("target"), str):
        return value["target"]
    return None


def _norm_source_key(value: str) -> str:
    """Apostrophe-normalized, casefolded key for glossary/book_memory lookups.

    APOSTROPHE-NORM (acceptance, Jacob's Bell): straight ``'`` and curly
    ``’`` are the same source surface (the chapter source may use either);
    an established glossary entry keyed ``Jacob's Bell`` must match an
    extractor entity named ``Jacob’s Bell`` (and vice versa).
    """
    return value.replace("\u2019", "'").casefold()


def _canonical_ru_from_book_memory(
    book_memory: Mapping[str, Any], name: str,
) -> Optional[str]:
    """Established ``canonical_ru`` for an entity in book_memory (if any).

    Variant B fallback: the memory fact wins over a fresh chapter alignment
    (acceptance regression bike->НЕ велосипед). An entity whose name — or a
    ``source_alias`` of it — matches an established ``canonical_ru`` in
    ``book_memory.json`` (characters/entities sections) uses that
    ``canonical_ru`` as the target instead of aligning (e.g. entity
    ``bike``/``motorcycle`` resolves to the seeded vehicle's canonical_ru
    ``мотоцикл``, never to the model's ``велосипед``). This is also the
    multi-word fallback (card Q2): a multi-word entity name (Joel's car,
    Hillsglade House) cannot be aligned word-by-word, so the target comes
    from the established ``canonical_ru`` when present; otherwise the
    candidate carries no target and is never promoted. Apostrophe-normalized
    + casefolded (APOSTROPHE-NORM): ``Jacob’s Bell`` matches an established
    ``Jacob's Bell`` entry; ``Blake's Vehicle`` matches ``Blake's vehicle``.
    """
    wanted = _norm_source_key(name)
    for section in ("characters", "entities"):
        for key, entry in (book_memory.get(section) or {}).items():
            if not isinstance(entry, dict):
                continue
            if _norm_source_key(str(key)) == wanted:
                if entry.get("canonical_ru"):
                    return str(entry["canonical_ru"])
            for alias in entry.get("source_aliases") or []:
                if _norm_source_key(str(alias)) == wanted:
                    if entry.get("canonical_ru"):
                        return str(entry["canonical_ru"])
    return None


def _established_glossary_target(
    glossary: Mapping[str, Any], name: str,
) -> Optional[str]:
    """Flat target of an established glossary entry for ``name``.

    Apostrophe-normalized + casefolded key match (APOSTROPHE-NORM), so an
    extractor entity named ``Jacob’s Bell`` (curly apostrophe) is recognized
    as already established by the on-disk ``Jacob's Bell`` entry — the
    conflict/no-op logic keys off the SOURCE SURFACE, not the exact
    apostrophe variant.
    """
    wanted = _norm_source_key(name)
    for key, value in glossary.items():
        if _norm_source_key(str(key)) == wanted:
            return _flat_glossary_target(value)
    return None


def glossary_observations_from_entity_context(
    context: ChapterEntityContext,
    *,
    chapter_id: str,
    source_by_pid: Mapping[str, str],
    translations: Mapping[str, str],
    glossary: Mapping[str, Any],
    book_memory: Mapping[str, Any],
    consensus_ratio: float = 0.8,
    pid_to_chunk: Optional[Mapping[str, str]] = None,
    _allow_proper_name_align: bool = False,
) -> Dict[str, Any]:
    """GLOSSARY-FROM-ENTITY (variant B): verified entities -> glossary targets.

    Owner decision 2026-08-15 (variant B): the source-only entity extractor
    gives verified candidates ``{source, canonical_type, anchor}`` but does
    NOT know the translation; the deterministic ``align_candidates`` script
    (0 model calls) extracts the ACTUAL Russian target from the finished
    chapter translation, so the glossary entry is exactly what the model
    wrote (consistent with the text).

    Rules (conservative, fail-closed):

    * **Proper-noun filter** — only entities whose name is a proper noun
      (title-case, ``_is_proper_noun_entity_name``) become glossary
      candidates: persons and places. Objects (pocketwatch, upstairs
      bathroom mirror, Joel's car, motorcycle, cat) are named by common
      nouns and never reach the glossary.
    * **Single-word names** — the entity name is aligned against the
      chapter translation with the existing ``align_candidates``
      (``consensus_ratio``); the dominant capitalized-Russian variant is
      the target.
    * **Multi-word names** (Joel's car, Hillsglade House) — word-level
      alignment cannot join words (card Q2); the target falls back to the
      entity's established ``canonical_ru`` in book_memory when present,
      otherwise the candidate carries no target and is NOT promoted.
    * **Established glossary conflict** — an existing glossary entry with a
      DIFFERENT target is a conflict (never overwritten); the same target
      is a no-op.
    * **canonical_ru** — the same aligned target fills ``canonical_ru`` in
      book_memory entities (the caller merges it into the entity
      observations built by ``book_memory_observations_from_entity_context``).

    ``source_by_pid`` is the chapter source ``{pid: text}`` (from
    ``parse_source_html``), ``translations`` the finished
    ``translations.json`` ``{pid: text}``, ``glossary`` the live
    ``glossary.json`` and ``book_memory`` the live ``book_memory.json`` in
    the memory dir. ``pid_to_chunk`` (optional, from ``chunk_plan.json``)
    maps the anchor pid to a chunk so the observation carries a
    ``chunk_id`` for the B7 quarantined-chunk filter.

    Returns ``{"glossary": {source: {target, type, chunk_id}},
    "canonical_ru": {entity: target}, "proposed": [...], "conflicts": [...]}``
    — the glossary observations to feed into
    ``MemoryManager.add_observation("glossary", ...)``, the canonical_ru
    map to merge into book_memory entity observations, and the aligned
    candidate records (proposed vs blocked) for the book-run
    ``candidates`` block / ledger. Deterministic; zero model calls.
    """
    # Deprecated: proper_name production path disabled; term only library (task 4.1)
    if not _allow_proper_name_align:
        return {"glossary": {}, "canonical_ru": {}, "proposed": [], "conflicts": []}
    from pact_v4.phase1.glossary_candidates import align_candidates

    observations: Dict[str, Any] = {}
    canonical_ru: Dict[str, str] = {}
    proposed: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    for record in sorted(context.entities, key=lambda r: r.entity):
        name = str(record.entity or "")
        if not name or not _is_proper_noun_entity_name(name):
            continue
        chunk_ids: List[str] = []
        if pid_to_chunk and record.anchor.pid in pid_to_chunk:
            chunk_ids = [str(pid_to_chunk[record.anchor.pid])]
        candidate: Dict[str, Any] = {
            "source": name,
            "kind": "proper_name",
            "occurrences": 1,
            "chunk_ids": chunk_ids,
            "context": record.anchor.span,
        }
        if " " in name:
            # Multi-word (card Q2): alignment by capitalized RU words cannot
            # join the phrase — fall back to the established canonical_ru,
            # then to the already-established glossary entry (apostrophe-
            # normalized match => the source surface is already promoted, a
            # no-op); else record the candidate WITHOUT a target (never
            # promoted).
            target = _canonical_ru_from_book_memory(book_memory, name)
            if not target:
                target = _established_glossary_target(glossary, name)
            aligned: Dict[str, Any] = {
                **candidate,
                "variants": {target: 1} if target else {},
                "target": target,
                "consensus_share": 1.0 if target else 0.0,
                "conflicts": [],
            }
        else:
            # Single-word: the established canonical_ru fact (if any) wins
            # over a fresh chapter alignment (bike->НЕ велосипед regression:
            # the memory says the vehicle is мотоцикл, so "motorcycle" must
            # never be promoted as the model's "велосипед"). Otherwise align
            # the entity name against the finished translation.
            established = _canonical_ru_from_book_memory(book_memory, name)
            if established:
                aligned = {
                    **candidate,
                    "variants": {established: 1},
                    "target": established,
                    "consensus_share": 1.0,
                    "conflicts": [],
                }
            else:
                aligned_list = align_candidates(
                    [candidate], source_by_pid, translations,
                    consensus_ratio=consensus_ratio, glossary=glossary,
                )
                aligned = aligned_list[0] if aligned_list else {**candidate}
        target = aligned.get("target")
        if not target:
            conflicts.append(dict(aligned))
            continue
        existing_target = _established_glossary_target(glossary, name)
        if existing_target is not None and existing_target != target:
            # Established glossary entry with a different target: conflict,
            # never overwrite (card Q5).
            conflicts.append({**dict(aligned), "established_target": existing_target})
            continue
        if existing_target == target:
            continue  # already established with the same target — no-op
        observations[name] = {
            "target": str(target),
            "type": record.canonical_type or "proper_name",
            "chunk_id": chunk_ids[0] if chunk_ids else "",
        }
        canonical_ru[name] = str(target)
        proposed.append(dict(aligned))
    return {
        "glossary": observations,
        "canonical_ru": canonical_ru,
        "proposed": proposed,
        "conflicts": conflicts,
    }


# ---------------------------------------------------------------------------
# Audit journal (append-only, one event per line, crash-safe)
# ---------------------------------------------------------------------------


class AuditJournal:
    """Append-only audit provenance journal (write-only, one line/event).

    Deliberately separate from the generation ``journal.ndjson``: the
    whole-chapter resume contract requires exactly one generation entry,
    so audit events are recorded here. A write failure disables the
    writer and is logged, never raised — provenance must not break a run.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._handle: Optional[Any] = None
        self._disabled = False

    def _ensure_open(self) -> None:
        if self._handle is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = open(self.path, "a", encoding="utf-8")

    def emit(self, event: str, **fields: Any) -> None:
        if self._disabled:
            return
        record = {
            "schema": B3_AUDIT_JOURNAL_SCHEMA,
            "event": event,
            "ts": _now_iso(),
            **fields,
        }
        try:
            self._ensure_open()
            assert self._handle is not None
            self._handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._handle.flush()
        except OSError as exc:
            LOG.warning("B3 audit journal write failed (%s); disabling", exc)
            self._disabled = True

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            finally:
                self._handle = None


# ---------------------------------------------------------------------------
# Config / result shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class B3AuditRepairConfig:
    """Settings for one B3 audit/repair pass (frozen contract of the run).

    ``entity_context_enabled`` (runtime config, default True — owner
    decision 2026-08-10, B1.3 gate pending): True runs the source-only
    entity prepass and feeds both the auditor and the hard filters; False
    audits without the entity block. The audit input params
    (``max_input_tokens``/``max_tokens``/``overlap_tokens``) are part of
    the run config identity via ``StrictRunConfig`` and of the audit
    cache identity via the stored payload.
    """

    entity_context_enabled: bool = True
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS
    max_tokens: int = DEFAULT_MAX_TOKENS
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS
    reasoning_budget: int = DEFAULT_REASONING_BUDGET
    # R-RETRY (t_8ab8ab35, operator extension 2026-08-13): the chunk-level
    # TRANSPORT_ERROR bounded retry policy (NEW session per attempt). It is
    # part of the run config identity (StrictRunConfig.to_config_artifact)
    # and is wired into ChunkedAuditConfig below — a cache written under a
    # different transport-retry policy must never replay a failed chunk.
    audit_transport_max_retries: int = DEFAULT_TRANSPORT_MAX_RETRIES
    audit_transport_base_delay_seconds: float = DEFAULT_TRANSPORT_BASE_DELAY_SECONDS
    repair_findings_cap: int = REPAIR_FINDINGS_CAP
    repair_microbatch_trigger: int = MICROBATCH_TRIGGER
    repair_microbatch_target: int = MICROBATCH_TARGET
    # REPAIR-CTX (card t_97b31f81, F5): the repair-batch local context
    # window (±N neighbour pairs around each finding PID) is part of the run
    # config identity (StrictRunConfig.to_config_artifact) and is wired into
    # SelectiveRepairConfig below — a window change must invalidate a stale
    # cached repaired map (old full-chapter batches can never replay under a
    # local-context prompt).
    repair_context_window: int = DEFAULT_REPAIR_CONTEXT_WINDOW
    # REPAIR-2 (card t_768537b9, F5): the per-category window overrides
    # ({category: window}; categories not in the map fall back to
    # ``repair_context_window`` — invented_gender/referent/omission default
    # ±10, changed_fact/addition stay ±3). Part of the run config identity
    # and wired into SelectiveRepairConfig below — a window change must
    # invalidate a stale cached repaired map.
    repair_context_window_by_category: Mapping[str, int] = field(
        default_factory=lambda: dict(DEFAULT_REPAIR_CONTEXT_WINDOW_BY_CATEGORY)
    )
    repair_reaudit_neighbour_window: int = DEFAULT_REAUDIT_NEIGHBOUR_WINDOW
    # REPAIR-CTX (t_97b31f81, owner decision 2026-08-12): the re-audit is a
    # CHUNKED audit over the affected region (changed PIDs + neighbours) —
    # the whole-chapter re-audit mode (old full_threshold) is CANCELLED. The
    # re-audit chunk/overlap settings and the REPAIRED CHANGES delta format
    # are identity-bearing (F5): a change invalidates a stale cached repaired
    # map (an old full-chapter re-audit can never replay under the chunked
    # local-context prompt).
    repair_reaudit_max_input_tokens: int = DEFAULT_REAUDIT_MAX_INPUT_TOKENS
    repair_reaudit_overlap_tokens: int = DEFAULT_REAUDIT_OVERLAP_TOKENS
    repair_reaudit_min_overlap_pairs: int = DEFAULT_REAUDIT_MIN_OVERLAP_PAIRS
    repair_reaudit_max_overlap_pairs: int = DEFAULT_REAUDIT_MAX_OVERLAP_PAIRS
    repair_reaudit_delta_format: str = REAUDIT_DELTA_FORMAT
    # The re-audit output budget and its bounded B4 JSON retry policy are
    # part of the run config identity (StrictRunConfig.to_config_artifact)
    # and are wired into SelectiveRepairConfig below (RV 71b7cbc fix, F5) —
    # without them the re-audit would silently fall back to module defaults
    # and a cache written under a different budget/policy could replay.
    repair_reaudit_max_tokens: int = DEFAULT_REAUDIT_MAX_TOKENS
    # REPAIR-MAX-TOKENS (owner decision 2026-08-15): the per-batch repair
    # OUTPUT budget (not the re-audit one) — 4000 exhausted by deepseek
    # reasoning in run_0004-0005 → empty JSON → 5/6 batches failed.
    # Identity-bearing (F5): a budget change must invalidate a stale
    # cached repaired map, so it rides the run config identity and is
    # wired into SelectiveRepairConfig below.
    repair_max_tokens: int = DEFAULT_REPAIR_MAX_TOKENS
    # REPAIR-ROBUST (card t_b6fd6cbd, run_0005): the per-batch repair
    # reasoning effort (0=off, 1=low, 2=medium, 3=high) for REMOTE
    # transports only — default 1 (low): deepseek high burned 32k reasoning
    # tokens on a repair batch and exhausted max_tokens before content
    # (run_0005 batch1: raw=0, finish=length). Identity-bearing (F5): wired
    # into SelectiveRepairConfig below, so a change invalidates a stale
    # cached repaired map. Inert locally — the local llama-server receives
    # its reasoning budget from the server args (--reasoning-budget) and
    # LocalOpenAIBackend rejects request_options (owner rule: local servers
    # always run with the same args).
    repair_reasoning: Optional[int] = DEFAULT_REPAIR_REASONING
    repair_reaudit_max_retries: int = DEFAULT_REAUDIT_MAX_RETRIES
    repair_reaudit_base_delay_seconds: float = DEFAULT_REAUDIT_BASE_DELAY_SECONDS
    prompt_version: str = PROMPT_VERSION
    harness_version: str = HARNESS_VERSION
    extractor_version: str = EXTRACTOR_VERSION
    # CANDIDATE-MERGE (t_0ffe56e1, RV2 HIGH finding): the REPAIR prompt
    # version (REPAIR_AS_VERIFIER_V1 v4 — the source_stage/merge contract)
    # is identity-bearing and wired into SelectiveRepairConfig below (F5) —
    # a cache written under a different repair prompt must never replay the
    # repaired map. Defaults mirror the module constants and are overridden
    # by _build_b3_audit_repair from StrictRunConfig.audit_repair_prompt_version.
    repair_prompt_version: str = REPAIR_PROMPT_VERSION
    repair_harness_version: str = REPAIR_HARNESS_VERSION
    # V4.2 R (card t_4707e6e5): Russian-only editor stage BEFORE the audit.
    # ``russian_editor_enabled`` is True by default (owner decision
    # 2026-08-11 — R is on unless the CLI disables it with
    # ``--no-russian-editor``, scheme 4.1). The R settings are identity-
    # bearing via ``StrictRunConfig`` (F5 lesson): russian_editor_version +
    # chunk settings + class threshold are part of the run config identity,
    # so flipping any of them invalidates the repaired cache — an old
    # repaired map can never replay under a different editor policy.
    russian_editor_enabled: bool = True
    russian_editor_version: str = RUSSIAN_EDITOR_PROMPT_VERSION
    russian_editor_harness_version: str = RUSSIAN_EDITOR_HARNESS_VERSION
    russian_editor_chunk_size: int = DEFAULT_CHUNK_SIZE
    russian_editor_overlap_pairs: int = DEFAULT_OVERLAP_PAIRS
    russian_editor_max_tokens: int = RUSSIAN_EDITOR_DEFAULT_MAX_TOKENS
    # Class threshold: classes in this frozenset are SAFE (auto-applied with
    # the diff-gate); every other known class routes to REVIEW candidates.
    russian_editor_safe_classes: frozenset = frozenset(SAFE_CLASSES)
    # R-RETRY (t_8ab8ab35, F5): the per-pid edit cap (duplicate pid is NOT
    # an error — up to this many edits per pid; 11th+ drops per-edit with a
    # WARNING) and the bounded retry policy (transport + empty/truncated
    # JSON) are identity-bearing — a cache written under a different
    # cap/retry policy must never replay the edited map.
    russian_editor_max_edits_per_pid: int = MAX_EDITS_PER_PID
    russian_editor_retry_max_retries: int = DEFAULT_RETRY_MAX_RETRIES
    russian_editor_retry_base_delay_seconds: float = DEFAULT_RETRY_BASE_DELAY_SECONDS
    glossary_resolver_mode: str = "promote"
    glossary_resolver_cache_miss_policy: str = "recompute"

    def to_payload(self) -> Dict[str, Any]:
        return {
            "entity_context_enabled": self.entity_context_enabled,
            "max_input_tokens": self.max_input_tokens,
            "max_tokens": self.max_tokens,
            "overlap_tokens": self.overlap_tokens,
            "reasoning_budget": self.reasoning_budget,
            "audit_transport_retry": {
                "max_retries": self.audit_transport_max_retries,
                "base_delay_seconds": self.audit_transport_base_delay_seconds,
            },
            "repair_findings_cap": self.repair_findings_cap,
            "repair_microbatch_trigger": self.repair_microbatch_trigger,
            "repair_microbatch_target": self.repair_microbatch_target,
            "repair_context_window": self.repair_context_window,
            "repair_context_window_by_category": dict(
                self.repair_context_window_by_category
            ),
            "repair_reaudit_neighbour_window": self.repair_reaudit_neighbour_window,
            "repair_reaudit_chunk": {
                "max_input_tokens": self.repair_reaudit_max_input_tokens,
                "overlap_tokens": self.repair_reaudit_overlap_tokens,
                "min_overlap_pairs": self.repair_reaudit_min_overlap_pairs,
                "max_overlap_pairs": self.repair_reaudit_max_overlap_pairs,
                "delta_format": self.repair_reaudit_delta_format,
            },
            "repair_reaudit_max_tokens": self.repair_reaudit_max_tokens,
            "repair_max_tokens": self.repair_max_tokens,
            # REPAIR-ROBUST (t_b6fd6cbd): the per-batch repair reasoning
            # effort is identity-bearing (F5) — the payload/report carries
            # it so the record reflects what the repair stage ran with.
            "repair_reasoning": self.repair_reasoning,
            "repair_reaudit_retry": {
                "max_retries": self.repair_reaudit_max_retries,
                "base_delay_seconds": self.repair_reaudit_base_delay_seconds,
            },
            "prompt_version": self.prompt_version,
            "harness_version": self.harness_version,
            "extractor_version": self.extractor_version,
            # CANDIDATE-MERGE (t_0ffe56e1, RV2 HIGH finding): the REPAIR
            # prompt version rides the payload/report like every other
            # version field (F5 — the repaired map is a function of it).
            "repair_prompt_version": self.repair_prompt_version,
            "repair_harness_version": self.repair_harness_version,
            "russian_editor": {
                "enabled": self.russian_editor_enabled,
                "version": self.russian_editor_version,
                "harness_version": self.russian_editor_harness_version,
                "chunk_size": self.russian_editor_chunk_size,
                "overlap_pairs": self.russian_editor_overlap_pairs,
                "max_tokens": self.russian_editor_max_tokens,
                "safe_classes": sorted(self.russian_editor_safe_classes),
                "max_edits_per_pid": self.russian_editor_max_edits_per_pid,
                "r_editor_retry": {
                    "max_retries": self.russian_editor_retry_max_retries,
                    "base_delay_seconds": self.russian_editor_retry_base_delay_seconds,
                },
            },
            "glossary_resolver": {
                "mode": self.glossary_resolver_mode,
                "cache_miss_policy": self.glossary_resolver_cache_miss_policy,
            },
        }


@dataclass(frozen=True)
class B3AuditRepairResult:
    """Aggregated result of one B3 pass (feeds the run record)."""

    step6: Dict[str, Any]  # audit summary
    step7: Dict[str, Any]  # repair summary
    step8: Dict[str, Any]  # gate / terminal
    translations_repaired: Dict[str, str]
    audit_complete: bool
    from_cache: bool
    entity_context_hash: Optional[str]
    audit_cache_path: Path
    journal_path: Path
    # V4.2 R: Russian-editor stage report (None when R is disabled) — the
    # runner records it in the trial record (edit_candidates + accept/reject
    # journal); on a cache hit it is restored from the audit cache payload.
    r_editor: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Entity-extractor backend view (role resolution without identity change)
# ---------------------------------------------------------------------------


class _EntityRoleView:
    """CompletionBackend view presenting an ``entity_extractor`` binding.

    The local/remote descriptors do not carry an ``entity_extractor``
    role; ``BackendEntityExtractor`` needs one to resolve its model ref.
    This view adds the role pointing at the audit (Qwen) ref WITHOUT
    touching the real backend's identity — the descriptor is only used
    for role resolution, never for cache/resume identity (the run records
    ``cfg.backend.identity_hash``).
    """

    def __init__(self, backend: CompletionBackend, entity_ref: str) -> None:
        self._backend = backend
        self._entity_ref = entity_ref

    @property
    def descriptor(self) -> BackendDescriptor:
        base = self._backend.descriptor
        bindings = dict(base.model_bindings)
        bindings["entity_extractor"] = self._entity_ref
        bindings.setdefault("default", self._entity_ref)
        return replace(base, model_bindings=bindings)

    def complete(self, request: CompletionRequest) -> Any:
        return self._backend.complete(request)

    def close(self) -> None:
        self._backend.close()

    def call_records(self) -> Sequence[Any]:
        return self._backend.call_records()


# ---------------------------------------------------------------------------
# Audit cache (resume identity)
# ---------------------------------------------------------------------------


def _audit_cache_path(out_dir: Path) -> Path:
    return out_dir / "audit_cache_b3.json"


def _r_editor_report_path(out_dir: Path) -> Path:
    """Standalone R-report artifact (FAIL-PATH R-CACHE, 2026-08-15).

    Written when an audit exception drops the normal cache-write path, so
    a resume still sees the completed R stage (GOOD chunks reused instead
    of re-running R from scratch). Same payload the cache would carry.
    """
    return out_dir / "r_editor_report.json"


def _entity_cache_path(out_dir: Path) -> Path:
    return out_dir / "entity_context_cache.json"


def _entity_validation_report_path(out_dir: Path) -> Path:
    return out_dir / "entity_context_validation_report.json"


def _journal_path(out_dir: Path) -> Path:
    return out_dir / "audit_journal.ndjson"


def _load_entity_cache(out_dir: Path) -> EntityContextCache:
    path = _entity_cache_path(out_dir)
    if not path.exists():
        return EntityContextCache()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return EntityContextCache.from_payload(payload)
    except Exception as exc:  # noqa: BLE001
        # F6 (B3 review): a structurally corrupt dependent cache (JSON
        # list/object with missing keys, wrong entry shape, foreign schema)
        # can raise KeyError/TypeError/AttributeError deep inside
        # from_payload — NOT just OSError/ValueError. Any such failure is a
        # cache MISS (fail-closed): discard and recompute, never abort B3.
        LOG.warning(
            "B3: entity_context_cache.json unreadable/foreign (%s: %s); "
            "starting a fresh cache",
            type(exc).__name__, exc,
        )
        return EntityContextCache()


def _save_entity_cache(out_dir: Path, cache: EntityContextCache) -> None:
    _atomic_write_json(_entity_cache_path(out_dir), cache.to_payload())


def _save_entity_validation_report(
    out_dir: Path, report: Mapping[str, Any]
) -> None:
    """Persist the extractor's drop/downgrade decisions (B3-DIAG).

    Written next to ``entity_context_cache.json`` so a run shows what the
    model PROPOSED vs what the code ACCEPTED (dead PID / non-verbatim span /
    translation-derived / gender without referent link / relation
    verified->candidate). Only written when a fresh validation actually ran
    — a cache hit reuses the previously validated context, so the report of
    the original extraction is preserved, never clobbered with an empty one.
    """
    _atomic_write_json(
        _entity_validation_report_path(out_dir), report
    )


# ---------------------------------------------------------------------------
# PARTIAL-RESUME integrity (t_ec6bb8bc): the partial cache replay payload
# (chunks / issues / r_editor) is validated AND bound to a canonical
# integrity hash BEFORE any resume plan is built. A cache can preserve
# identity + translations_repaired_hash while its GOOD-chunk evidence or
# R edits are altered; without this gate resume would publish tampered
# payloads with 0 model calls. Any malformed / missing / duplicate /
# extra / mismatched partial payload is a FULL cache miss (recompute) —
# never a partial replay (fail-closed).
# ---------------------------------------------------------------------------


# Statuses the chunked audit can persist per chunk (classify_chunk + the
# TRANSPORT_ERROR / retry-shrink paths — see chunked_audit.py).
_AUDIT_CHUNK_STATUSES = frozenset({
    "GOOD", "GOOD_RETRIED", "FAILED_RETRIED", "TRANSPORT_ERROR",
    "LENGTH", "EMPTY", "SPILL", "INVALID_JSON",
})

# Statuses the Russian editor can persist per chunk (russian_editor.py).
_R_EDITOR_CHUNK_STATUSES = frozenset({"GOOD", "FAILED"})

# Exact persisted key set of an audit issue record (chunked_audit.py
# collection + ``_attach_debug_and_dedupe``). A record with a missing or
# extra key is a schema violation — never tolerated (RV2-A t_c84b6f13).
_ISSUE_KEYS = frozenset({
    "id", "category", "severity", "confidence", "note", "excerpt", "_debug",
})

# Report-level statuses produced by _build_r_editor_report (b3_audit_repair.py).
_R_EDITOR_REPORT_STATUSES = frozenset({
    "disabled", "failed", "partial", "incomplete", "complete",
})

# Statuses under which the R stage actually RAN (outcome REQUIRED).
_R_EDITOR_RAN_STATUSES = frozenset({"partial", "incomplete", "complete"})

# Exact persisted key sets (RussianEditorOutcome chunk records / edit
# payloads). A record with a foreign key is a schema violation — never
# tolerated, never coerced.
_R_CHUNK_KEYS = frozenset({"chunk", "first_pid", "last_pid", "status", "edits"})
_R_EDIT_KEYS = frozenset({"pid", "original", "rewritten", "reason", "class"})

# KILL-SAFE-INCREMENTAL (t_2d16962c, RV2 fix t_d996bbf7): EXACT key sets of
# the incremental ``stage_progress`` payloads written by the per-stage
# progress hooks in ``_run_impl``. The resume validator enforces these
# verbatim — a record with a missing or extra key is a schema violation and
# a FULL cache miss (the replay path copies these slices verbatim into the
# evaluators, so a foreign-schema payload must never be replayed).
_STAGE_PROGRESS_KEYS = frozenset({"r_editor", "audit", "repair", "reaudit"})
_R_EDITOR_STAGE_KEYS = frozenset(
    {"status", "enabled", "done_chunks", "failed_chunks", "outcome"}
)
_R_EDITOR_OUTCOME_KEYS = frozenset({"chunk_size", "chunks"})
_AUDIT_STAGE_KEYS = frozenset(
    {"status", "done_chunks", "failed_chunks", "chunks", "issues"}
)
# ChunkMeta.to_payload() (chunked_audit.py) — the audit slice payload.
_AUDIT_CHUNK_KEYS = frozenset({
    "chunk", "first_pid", "last_pid", "pair_count", "context_count",
    "status", "finish_reason", "reasoning_chars", "reasoning_file",
    "issue_count", "dropped_count",
})
_REPAIR_STAGE_KEYS = frozenset(
    {"status", "done_batches", "committed", "passed", "outcome"}
)
_REPAIR_OUTCOME_KEYS = frozenset({"batches", "batch_count"})
# _repair_batches_payload() (selective_repair.py) — the per-batch slice.
_REPAIR_BATCH_KEYS = frozenset({
    "batch_index", "status", "findings", "results", "error", "warnings",
    "missing_indices",
})
_REPAIR_FINDING_KEYS = frozenset({
    "index", "pid", "tier", "category", "severity", "confidence",
    "source_stage", "sources",
})
_REPAIR_RESULT_KEYS = frozenset({
    "index", "decision", "pid", "repaired_translation", "reason",
})
_REAUDIT_STAGE_KEYS = frozenset({"status", "done_chunks", "issues"})
_REAUDIT_CHUNK_KEYS = frozenset({
    "chunk", "first_pid", "last_pid", "issues", "failed", "dropped",
})

# Repair decisions the fresh batch parser accepts (parse_repair_batch).
_REPAIR_DECISIONS = frozenset({"pass", "repair"})
# Repair batch statuses _repair_batches_payload can persist.
_REPAIR_BATCH_STATUSES = frozenset({"GOOD", "PARTIAL", "FAILED"})

def _validate_partial_payload(
    payload: Mapping[str, Any],
    *,
    expected_pids: Optional[Sequence[str]],
    current_text: Optional[Mapping[str, str]],
    r_editor_enabled: bool,
) -> Optional[str]:
    """Validate the complete PARTIAL-RESUME replay payload.

    Runs inside ``B3AuditCache.load`` BEFORE either resume plan is
    constructed (audit_resume_plan / r_editor_resume_plan). Returns None
    when the payload is valid, or a reason string naming the first
    violation. Checks, fail-closed:

    * exact audit chunk coverage/order (contiguous 1..N, no duplicate /
      missing / extra indices) and a known status per chunk;
    * audit chunk boundaries (first_pid/last_pid strings) and pair counts
      (+ the int fields the evaluator replays verbatim);
    * issue schema (exact key set, non-empty string id/note/excerpt,
      category/severity/confidence vocab, PID membership in the
      translation map), ``_debug.chunk`` attribution inside the covered
      chunk indices, and PID membership inside the ACTUAL covered chunk
      span of the attributed chunk (first_pid..last_pid boundaries);
    * R report schema: when the R stage RAN (status partial/incomplete/
      complete) the ``outcome`` is REQUIRED with a non-empty, coherently
      tiled ``chunks`` list (contiguous 1..M, chunk_size-boundary
      coverage); when R was disabled/failed the ``outcome`` must be null —
      a malformed/missing R outcome is a full miss, never a partial replay
      while audit issues stay reusable; FIX RV2-B (t_a4f8f2b2): when the
      CURRENT run enables R the report itself is REQUIRED — ``r_editor``
      None/missing (an enabled run always persists a report dict, even for
      a failed stage) is a full miss BEFORE either resume plan is built;
      ``r_editor`` None is legitimate only when R is disabled (save()
      writes null for the disabled stage); FIX RV2-B (t_aa3ee032): the
      report ``enabled`` flag and ``status`` must be mutually consistent —
      status ``disabled`` is written ONLY by a disabled R config, so
      ``enabled=True`` with status ``disabled`` (or ``enabled=False`` with
      a ran/failed status) is an impossible combination (tampered) and a
      full miss;
    * every cached R edit: object shape, exact string types, PID
      membership (in the translation map AND inside the chunk's own
      boundary span), known class, and (when ``current_text`` is
      available) the verbatim current-text substring constraint.

    Malformed fields are NEVER stringified/coerced — a violation is a full
    cache miss. ``expected_pids`` / ``current_text`` may be None only when
    the caller has no map (PID-membership / text-substring checks are then
    skipped; the canonical hash below still binds the whole payload).
    """
    chunks = payload.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        return "chunks is missing, not a list, or empty"
    expected_pid_set = frozenset(expected_pids) if expected_pids is not None else None
    for position, item in enumerate(chunks, start=1):
        if not isinstance(item, dict):
            return f"chunk at position {position}: not an object"
        chunk_index = item.get("chunk")
        if chunk_index != position:
            return (
                f"chunk coverage/order mismatch: stored index {chunk_index!r} "
                f"at position {position} (expected contiguous 1..N)"
            )
        status = item.get("status")
        if not isinstance(status, str) or status not in _AUDIT_CHUNK_STATUSES:
            return f"chunk {chunk_index}: unknown status {status!r}"
        first_pid = item.get("first_pid")
        last_pid = item.get("last_pid")
        if not isinstance(first_pid, str) or not first_pid:
            return f"chunk {chunk_index}: first_pid is missing or not a string"
        if not isinstance(last_pid, str) or not last_pid:
            return f"chunk {chunk_index}: last_pid is missing or not a string"
        if expected_pid_set is not None and (
            first_pid not in expected_pid_set or last_pid not in expected_pid_set
        ):
            return (
                f"chunk {chunk_index}: boundary pids {first_pid!r}/{last_pid!r} "
                f"are not in the translation map"
            )
        pair_count = item.get("pair_count")
        if not isinstance(pair_count, int) or isinstance(pair_count, bool) or pair_count < 1:
            return f"chunk {chunk_index}: pair_count {pair_count!r} is not a positive int"
        context_count = item.get("context_count")
        if not isinstance(context_count, int) or isinstance(context_count, bool) or context_count < 0:
            return f"chunk {chunk_index}: context_count {context_count!r} is not a non-negative int"
        reasoning_chars = item.get("reasoning_chars")
        if not isinstance(reasoning_chars, int) or isinstance(reasoning_chars, bool) or reasoning_chars < 0:
            return (
                f"chunk {chunk_index}: reasoning_chars {reasoning_chars!r} "
                f"is not a non-negative int"
            )
        if not isinstance(item.get("reasoning_file"), str):
            return f"chunk {chunk_index}: reasoning_file is not a string"
        dropped_count = item.get("dropped_count")
        if dropped_count is not None and (
            not isinstance(dropped_count, int)
            or isinstance(dropped_count, bool)
            or dropped_count < 0
        ):
            return (
                f"chunk {chunk_index}: dropped_count {dropped_count!r} "
                f"is not a non-negative int"
            )
        finish_reason = item.get("finish_reason")
        if finish_reason is not None and not isinstance(finish_reason, str):
            return f"chunk {chunk_index}: finish_reason is not a string or null"

    issues = payload.get("issues")
    if not isinstance(issues, list):
        return "issues is not a list"
    chunk_count = len(chunks)
    # RV2-A (t_c84b6f13): a position map over the ordered PID list lets the
    # validator enforce that every issue sits inside the ACTUAL pid span of
    # the chunk it claims (_debug.chunk), not merely inside the global
    # translation map.
    pid_positions = None
    if expected_pids is not None:
        pid_positions = {pid: position for position, pid in enumerate(expected_pids)}
    for issue_index, issue in enumerate(issues):
        if not isinstance(issue, dict):
            return f"issue {issue_index}: not an object"
        if set(issue) != _ISSUE_KEYS:
            return (
                f"issue {issue_index}: key set mismatch "
                f"(expected {sorted(_ISSUE_KEYS)}, got {sorted(issue)})"
            )
        pid = issue.get("id")
        if not isinstance(pid, str) or not pid:
            return f"issue {issue_index}: id is missing or not a string"
        if expected_pid_set is not None and pid not in expected_pid_set:
            return f"issue {issue_index}: id {pid!r} is not in the translation map"
        if issue.get("category") not in AUDIT_V4_CATEGORIES:
            return f"issue {pid}: invalid category {issue.get('category')!r}"
        if issue.get("severity") not in AUDIT_V4_SEVERITIES:
            return f"issue {pid}: invalid severity {issue.get('severity')!r}"
        if issue.get("confidence") not in AUDIT_V4_CONFIDENCES:
            return f"issue {pid}: invalid confidence {issue.get('confidence')!r}"
        note = issue.get("note")
        if not isinstance(note, str) or not note.strip():
            return f"issue {pid}: note is missing, not a string, or empty"
        excerpt = issue.get("excerpt")
        if not isinstance(excerpt, str) or not excerpt.strip():
            return f"issue {pid}: excerpt is missing, not a string, or empty"
        debug = issue.get("_debug")
        if not isinstance(debug, dict):
            return f"issue {pid}: _debug is missing or not an object"
        chunk = debug.get("chunk")
        if not isinstance(chunk, int) or isinstance(chunk, bool) or not (1 <= chunk <= chunk_count):
            return (
                f"issue {pid}: _debug.chunk {chunk!r} is not inside the "
                f"covered chunk indices 1..{chunk_count}"
            )
        if not isinstance(debug.get("reasoning_file"), str):
            return f"issue {pid}: _debug.reasoning_file is not a string"
        if pid_positions is not None:
            chunk_record = chunks[chunk - 1]
            first_pos = pid_positions.get(chunk_record.get("first_pid"))
            last_pos = pid_positions.get(chunk_record.get("last_pid"))
            pid_pos = pid_positions.get(pid)
            if (
                first_pos is None or last_pos is None or pid_pos is None
                or not (first_pos <= pid_pos <= last_pos)
            ):
                return (
                    f"issue {pid}: id is not inside the actual pid span of "
                    f"chunk {chunk} ({chunk_record.get('first_pid')!r}.."
                    f"{chunk_record.get('last_pid')!r})"
                )

    r_editor = payload.get("r_editor")
    # FIX RV2-B (t_a4f8f2b2): an enabled-R partial cache ALWAYS carries a
    # report dict (_build_r_editor_report persists one on every enabled run,
    # including a failed stage), so r_editor None/missing means the report
    # was deleted or is a foreign-schema payload — a FULL miss BEFORE either
    # resume plan is built, never a partial replay while the audit GOOD
    # chunks stay reusable. When the CURRENT run has R disabled, r_editor
    # None is the legitimate disabled representation (save() writes null)
    # and is accepted by design.
    if r_editor is None and r_editor_enabled:
        return (
            "r_editor is required when the R stage is enabled "
            "(missing/None report)"
        )
    if r_editor is not None and not isinstance(r_editor, dict):
        return "r_editor is not an object or null"
    if isinstance(r_editor, dict):
        # FIX RV2-B (t_aa3ee032): the R report is validated COMPLETELY before
        # either resume plan is built. When the R stage RAN (status partial/
        # incomplete/complete) the outcome is REQUIRED with a non-empty,
        # coherently tiled chunks list; when R was disabled/failed the
        # outcome must be null. A malformed/missing R outcome is a FULL
        # cache miss, never a partial replay while audit issues stay
        # reusable.
        report_status = r_editor.get("status")
        if (
            not isinstance(report_status, str)
            or report_status not in _R_EDITOR_REPORT_STATUSES
        ):
            return f"r_editor.status is missing or unknown {report_status!r}"
        if not isinstance(r_editor.get("enabled"), bool):
            return "r_editor.enabled is not a bool"
        # FIX RV2-B (t_a4f8f2b2): an enabled-R run always persists a report
        # with enabled=True (_build_r_editor_report records the config flag
        # verbatim) — a stored disabled report inside an enabled-R partial
        # cache is internally inconsistent (tampered) and fails closed.
        if r_editor_enabled and not r_editor.get("enabled"):
            return (
                "r_editor.enabled must be true when the R stage is enabled "
                "(stored disabled report in an enabled-R partial cache)"
            )
        # FIX RV2-B (t_aa3ee032): the report's own enabled flag and status
        # must agree — _build_r_editor_report writes status "disabled" ONLY
        # when the stage was disabled (enabled=False); an enabled stage
        # always persists failed/partial/incomplete/complete. A report
        # claiming enabled=True with status "disabled" (or enabled=False
        # with a ran/failed status) is an impossible combination (tampered)
        # and fails closed BEFORE either resume plan is built — never a
        # partial replay while GOOD audit chunks stay reusable.
        enabled = r_editor.get("enabled")
        if enabled and report_status == "disabled":
            return (
                "r_editor.status 'disabled' contradicts enabled=True — a "
                "disabled status is only written by a disabled R config"
            )
        if not enabled and report_status != "disabled":
            return (
                f"r_editor.status {report_status!r} contradicts "
                "enabled=False — a disabled R config never runs the stage"
            )
        outcome = r_editor.get("outcome")
        if report_status in _R_EDITOR_RAN_STATUSES:
            # R ran: outcome REQUIRED with a non-empty chunks list.
            if not isinstance(outcome, dict):
                return (
                    f"r_editor.outcome is required when status is "
                    f"{report_status!r} (got {type(outcome).__name__})"
                )
            r_chunks = outcome.get("chunks")
            if not isinstance(r_chunks, list) or not r_chunks:
                return "r_editor.outcome.chunks is missing, not a list, or empty"
            chunk_size = r_editor.get("chunk_size")
            if (
                not isinstance(chunk_size, int)
                or isinstance(chunk_size, bool)
                or chunk_size < 1
            ):
                return f"r_editor.chunk_size {chunk_size!r} is not a positive int"
            if expected_pids is not None:
                expected_count = (
                    len(expected_pids) + chunk_size - 1
                ) // chunk_size
                if len(r_chunks) != expected_count:
                    return (
                        f"r_editor chunk coverage mismatch: {len(r_chunks)} "
                        f"chunk(s) persisted for {len(expected_pids)} pids at "
                        f"chunk_size {chunk_size} (expected {expected_count})"
                    )
            for position, item in enumerate(r_chunks, start=1):
                if not isinstance(item, dict):
                    return f"r_editor chunk at position {position}: not an object"
                if set(item) != _R_CHUNK_KEYS:
                    return (
                        f"r_editor chunk at position {position}: foreign key "
                        f"set {sorted(item)!r} (expected {sorted(_R_CHUNK_KEYS)})"
                    )
                chunk_index = item.get("chunk")
                if chunk_index != position:
                    return (
                        f"r_editor chunk coverage/order mismatch: stored index "
                        f"{chunk_index!r} at position {position} (expected "
                        f"contiguous 1..M)"
                    )
                status = item.get("status")
                if not isinstance(status, str) or status not in _R_EDITOR_CHUNK_STATUSES:
                    return f"r_editor chunk {chunk_index}: unknown status {status!r}"
                first_pid = item.get("first_pid")
                last_pid = item.get("last_pid")
                if not isinstance(first_pid, str) or not first_pid:
                    return f"r_editor chunk {chunk_index}: first_pid is missing or not a string"
                if not isinstance(last_pid, str) or not last_pid:
                    return f"r_editor chunk {chunk_index}: last_pid is missing or not a string"
                if expected_pids is not None:
                    # Coherent tiling: chunk k must cover exactly the k-th
                    # chunk_size span of the ordered translation map — chunk 1
                    # starts at map[0], chunk k+1 starts right after chunk k
                    # ends, the last chunk ends at map[-1]. A duplicate /
                    # missing / extra / foreign chunk record breaks the
                    # tiling and is a full miss.
                    start = (position - 1) * chunk_size
                    end = min(position * chunk_size, len(expected_pids))
                    if (
                        first_pid != expected_pids[start]
                        or last_pid != expected_pids[end - 1]
                    ):
                        return (
                            f"r_editor chunk {chunk_index}: boundary span "
                            f"{first_pid!r}/{last_pid!r} does not tile the "
                            f"translation map (expected "
                            f"{expected_pids[start]!r}/{expected_pids[end - 1]!r} "
                            f"at positions {start}..{end - 1})"
                        )
                edits = item.get("edits")
                if not isinstance(edits, list):
                    return f"r_editor chunk {chunk_index}: edits is not a list"
                chunk_pids = None
                if expected_pids is not None:
                    start = (position - 1) * chunk_size
                    end = min(position * chunk_size, len(expected_pids))
                    chunk_pids = frozenset(expected_pids[start:end])
                for edit_index, edit in enumerate(edits):
                    if not isinstance(edit, dict):
                        return (
                            f"r_editor chunk {chunk_index} edit {edit_index}: "
                            f"not an object"
                        )
                    if set(edit) != _R_EDIT_KEYS:
                        return (
                            f"r_editor chunk {chunk_index} edit {edit_index}: "
                            f"foreign key set {sorted(edit)!r} "
                            f"(expected {sorted(_R_EDIT_KEYS)})"
                        )
                    edit_pid = edit.get("pid")
                    if not isinstance(edit_pid, str) or not edit_pid:
                        return (
                            f"r_editor chunk {chunk_index} edit {edit_index}: "
                            f"pid is missing or not a string"
                        )
                    if expected_pid_set is not None and edit_pid not in expected_pid_set:
                        return (
                            f"r_editor chunk {chunk_index} edit {edit_index}: "
                            f"pid {edit_pid!r} is not in the translation map"
                        )
                    if chunk_pids is not None and edit_pid not in chunk_pids:
                        return (
                            f"r_editor chunk {chunk_index} edit {edit_index}: "
                            f"pid {edit_pid!r} is outside the chunk boundary "
                            f"span {first_pid!r}..{last_pid!r}"
                        )
                    original = edit.get("original")
                    if not isinstance(original, str) or not original:
                        return (
                            f"r_editor chunk {chunk_index} edit {edit_index} "
                            f"pid {edit_pid}: original is missing or not a string"
                        )
                    rewritten = edit.get("rewritten")
                    if not isinstance(rewritten, str) or not rewritten.strip():
                        return (
                            f"r_editor chunk {chunk_index} edit {edit_index} "
                            f"pid {edit_pid}: rewritten is missing or not a string"
                        )
                    if not isinstance(edit.get("reason"), str) or not edit.get("reason").strip():
                        return (
                            f"r_editor chunk {chunk_index} edit {edit_index} "
                            f"pid {edit_pid}: reason is missing or not a string"
                        )
                    klass = edit.get("class")
                    if not isinstance(klass, str) or klass not in ALL_CLASSES:
                        return (
                            f"r_editor chunk {chunk_index} edit {edit_index} "
                            f"pid {edit_pid}: unknown edit class {klass!r} "
                            f"(allowed: {sorted(ALL_CLASSES)})"
                        )
                    if current_text is not None and original not in str(
                        current_text.get(edit_pid, "")
                    ):
                        return (
                            f"r_editor chunk {chunk_index} edit {edit_index} "
                            f"pid {edit_pid}: original is not a substring of the "
                            f"current text (tampered cache edit)"
                        )
        elif outcome is not None:
            # R disabled/failed: a non-null outcome is a schema violation.
            return (
                f"r_editor: outcome must be null when status is "
                f"{report_status!r} (got {type(outcome).__name__})"
            )

    # Canonical integrity binding: the whole partial replay payload must
    # match the hash persisted at save() time. A missing field (old
    # schema), any tamper, reorder, duplicate, or extra element — even
    # while identity and translations_repaired_hash are untouched — is a
    # full cache miss, never a partial replay.
    stored_hash = payload.get("partial_resume_hash")
    computed_hash = canonical_json_hash({
        "chunks": chunks, "issues": issues, "r_editor": r_editor,
    })
    if not isinstance(stored_hash, str) or stored_hash != computed_hash:
        return (
            "partial_resume_hash mismatch (old schema or tampered partial "
            "payload; stored=%r computed=%r)"
        ) % (stored_hash, computed_hash)
    return None


def _validate_stage_progress(
    stage_progress: Any,
    *,
    expected_pids: Optional[Sequence[str]],
    stored_hash: Any,
    r_editor_enabled: bool = False,
    # FIX RV2 (t_d996bbf7): the CURRENT raw translation text binds every
    # cached R edit (``original`` must be a verbatim substring of the
    # current text of the edit's pid) and the R-EDITED map (the payload's
    # own ``translations_repaired`` at the last incremental save) binds the
    # cached repair results (no-op / truncation gates mirror the fresh
    # ``parse_repair_batch`` path). Either may be None only when the caller
    # has no map — the text-dependent gates are then skipped (the exact
    # schema / coverage / hash checks below still bind the whole payload).
    current_text: Optional[Mapping[str, str]] = None,
    edited_text: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """Validate the KILL-SAFE-INCREMENTAL ``stage_progress`` payload (t_2d16962c).

    The incremental cache rewrites ``audit_cache_b3.json`` after EVERY chunk /
    batch on all chunked stages (R, audit, repair, reaudit). The payload's
    ``stage_progress`` records, per stage, which chunks/batches finished with
    which status and the accumulated slices (chunk payloads / issues /
    committed / passed / residual issues) needed to resume. Same fail-closed
    policy as the top-level partial payload: any tamper, malformed value,
    coverage gap, or foreign schema is a full cache miss (load returns None,
    the stage re-runs from scratch) — never a partial replay of a payload the
    validator cannot fully trust.

    ``stored_hash`` is the top-level ``partial_resume_hash`` persisted at the
    last save — recomputed over the four stage blocks at every incremental
    rewrite, so any tamper (even with identity and translations_repaired_hash
    preserved) is a full miss.

    FIX RV2 (t_d996bbf7) — the replay payloads are validated with the SAME
    strictness as the fresh paths, so a recomputed hash cannot smuggle
    unauthorized content into the evaluators:

    * repair: EXACT stage/outcome/batch/finding/result key sets; contiguous
      batch index coverage (1..k prefix of ``batch_count``); findings with
      contiguous per-batch indices 1..M and PIDs inside the translation map;
      every result bound to its finding (unknown/duplicate index = miss),
      ``decision`` in {pass, repair}, a repair result naming the EXACT pid of
      its finding (index/PID contract), non-empty ``repaired_translation``,
      and the same no-op / <40%-preserved truncation gates the fresh
      ``parse_repair_batch`` applies (against ``edited_text``); a GOOD batch
      must answer EVERY finding (the fresh coverage gate); ``done_batches``
      must equal the GOOD batch indices and ``committed``/``passed`` must
      equal exactly what replaying the cached batches would produce;
    * reaudit: EXACT stage/chunk key sets; contiguous chunk coverage 1..N;
      every cached issue validated with the same strict validator as a fresh
      re-audit (``validate_chunk_json`` semantics: object, non-empty string
      id, category/severity/confidence vocab) AND bound to the pid span of
      the chunk it lives in; the aggregate ``issues`` list must equal the
      order-sensitive concatenation of the per-chunk issue lists;
    * R: every cached edit bound to the CURRENT text (``original`` is a
      verbatim substring of ``current_text[pid]``) and to the chunk's own
      boundary span, like the legacy ``_validate_partial_payload``;
    * every stage record and every nested record is checked against its
      EXACT key set — an unknown key is a schema violation (fail-closed),
      never silently ignored.

    Returns None when valid, else a reason string naming the first violation.
    """
    if not isinstance(stage_progress, dict):
        return "stage_progress is not an object"
    if set(stage_progress) != _STAGE_PROGRESS_KEYS:
        return (
            f"stage_progress foreign key set {sorted(stage_progress)!r} "
            f"(expected {sorted(_STAGE_PROGRESS_KEYS)})"
        )
    expected_pid_set = frozenset(expected_pids) if expected_pids is not None else None

    # ------------------------------------------------------------------
    # r_editor
    # ------------------------------------------------------------------
    r_editor = stage_progress.get("r_editor")
    if r_editor is None:
        return "stage_progress.r_editor is missing"
    if not isinstance(r_editor, dict):
        return "stage_progress.r_editor is not an object"
    if set(r_editor) != _R_EDITOR_STAGE_KEYS:
        return (
            f"stage_progress.r_editor foreign key set {sorted(r_editor)!r} "
            f"(expected {sorted(_R_EDITOR_STAGE_KEYS)})"
        )
    r_status = r_editor.get("status")
    if not isinstance(r_status, str) or r_status not in _R_EDITOR_REPORT_STATUSES:
        return f"stage_progress.r_editor.status is missing or unknown {r_status!r}"
    if not isinstance(r_editor.get("enabled"), bool):
        return "stage_progress.r_editor.enabled is not a bool"
    # FIX RV2-B (t_aa3ee032, mirrored from the top-level R report): the
    # stored enabled flag must agree with the CURRENT run's R enablement
    # (save() writes the config flag verbatim) — a stored disabled stage in
    # an enabled-R incremental cache (or a ran stage in a disabled-R cache)
    # is an impossible combination (tampered) and fails closed.
    if r_editor.get("enabled") != r_editor_enabled:
        return (
            f"stage_progress.r_editor.enabled {r_editor.get('enabled')!r} "
            f"contradicts the current run's R enablement {r_editor_enabled!r} "
            "(tampered stage_progress)"
        )
    if r_editor.get("enabled") and r_status == "disabled":
        return (
            "stage_progress.r_editor.status 'disabled' contradicts "
            "enabled=True — a disabled status is only written by a disabled "
            "R config"
        )
    if not r_editor.get("enabled") and r_status != "disabled":
        return (
            f"stage_progress.r_editor.status {r_status!r} contradicts "
            "enabled=False — a disabled R config never runs the stage"
        )
    done_chunks = r_editor.get("done_chunks")
    if not isinstance(done_chunks, list) or any(
        not isinstance(c, int) or isinstance(c, bool) for c in done_chunks
    ):
        return "stage_progress.r_editor.done_chunks is not a list of ints"
    if done_chunks != sorted(set(done_chunks)):
        return "stage_progress.r_editor.done_chunks contains duplicates"
    if done_chunks != list(range(1, len(done_chunks) + 1)):
        return (
            "stage_progress.r_editor.done_chunks is not contiguous 1..N "
            "(coverage gap — a kill between chunks must never look complete)"
        )
    failed_chunks = r_editor.get("failed_chunks")
    if not isinstance(failed_chunks, list) or any(
        not isinstance(c, int) or isinstance(c, bool) for c in failed_chunks
    ):
        return "stage_progress.r_editor.failed_chunks is not a list of ints"
    if any(c not in done_chunks for c in failed_chunks):
        return "stage_progress.r_editor.failed_chunks is not a subset of done_chunks"
    r_outcome = r_editor.get("outcome")
    if r_status in _R_EDITOR_RAN_STATUSES:
        # R ran: outcome REQUIRED with a non-empty, prefix-coherent chunks list.
        if not isinstance(r_outcome, dict):
            return (
                f"stage_progress.r_editor.outcome is required when status is "
                f"{r_status!r} (got {type(r_outcome).__name__})"
            )
        r_chunks = r_outcome.get("chunks")
        if not isinstance(r_chunks, list) or not r_chunks:
            return "stage_progress.r_editor.outcome.chunks is missing, not a list, or empty"
        if set(r_outcome) != _R_EDITOR_OUTCOME_KEYS:
            return (
                f"stage_progress.r_editor.outcome foreign key set "
                f"{sorted(r_outcome)!r} (expected {sorted(_R_EDITOR_OUTCOME_KEYS)})"
            )
        chunk_size = r_outcome.get("chunk_size")
        if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or chunk_size < 1:
            return f"stage_progress.r_editor.outcome.chunk_size {chunk_size!r} is not a positive int"
        # FIX RV2 (t_d996bbf7): the persisted chunk records are exactly the
        # processed chunks the progress hook journaled — their indices must
        # equal done_chunks (all processed chunks, GOOD and FAILED alike).
        # FIX RV2-findings (t_006f3a79): the records are type-validated
        # BEFORE field access — a malformed chunk record (e.g. `[ [ ] ]`)
        # must fail closed as a clean miss (None), never an AttributeError
        # escaping the validator.
        if any(not isinstance(c, dict) for c in r_chunks):
            return (
                "stage_progress.r_editor.outcome.chunks contains a "
                "non-object record"
            )
        if [c.get("chunk") for c in r_chunks] != done_chunks:
            return (
                "stage_progress.r_editor.outcome.chunks indices do not match "
                "done_chunks (coverage mismatch)"
            )
        for position, item in enumerate(r_chunks, start=1):
            if not isinstance(item, dict):
                return f"stage_progress.r_editor chunk at position {position}: not an object"
            if set(item) != _R_CHUNK_KEYS:
                return (
                    f"stage_progress.r_editor chunk at position {position}: "
                    f"foreign key set {sorted(item)!r} (expected {sorted(_R_CHUNK_KEYS)})"
                )
            chunk_index = item.get("chunk")
            if chunk_index != position:
                return (
                    f"stage_progress.r_editor chunk coverage/order mismatch: stored "
                    f"index {chunk_index!r} at position {position} (expected 1..N)"
                )
            status = item.get("status")
            if not isinstance(status, str) or status not in _R_EDITOR_CHUNK_STATUSES:
                return f"stage_progress.r_editor chunk {chunk_index}: unknown status {status!r}"
            first_pid = item.get("first_pid")
            last_pid = item.get("last_pid")
            if not isinstance(first_pid, str) or not first_pid:
                return f"stage_progress.r_editor chunk {chunk_index}: first_pid is missing or not a string"
            if not isinstance(last_pid, str) or not last_pid:
                return f"stage_progress.r_editor chunk {chunk_index}: last_pid is missing or not a string"
            if expected_pid_set is not None and (
                first_pid not in expected_pid_set or last_pid not in expected_pid_set
            ):
                return (
                    f"stage_progress.r_editor chunk {chunk_index}: boundary pids "
                    f"{first_pid!r}/{last_pid!r} are not in the translation map"
                )
            if expected_pids is not None:
                # Coherent prefix tiling: chunk k covers exactly the k-th
                # chunk_size span of the ordered translation map.
                start = (position - 1) * chunk_size
                end = min(position * chunk_size, len(expected_pids))
                if start >= len(expected_pids):
                    return (
                        f"stage_progress.r_editor chunk {chunk_index}: boundary span "
                        f"starts beyond the translation map (position {position}, "
                        f"chunk_size {chunk_size})"
                    )
                if (
                    first_pid != expected_pids[start]
                    or last_pid != expected_pids[end - 1]
                ):
                    return (
                        f"stage_progress.r_editor chunk {chunk_index}: boundary span "
                        f"{first_pid!r}/{last_pid!r} does not tile the translation "
                        f"map prefix (expected {expected_pids[start]!r}/"
                        f"{expected_pids[end - 1]!r} at positions {start}..{end - 1})"
                    )
            # FIX RV2 (t_d996bbf7): the pid span the chunk actually covers —
            # every edit must name a pid INSIDE it (legacy validator parity).
            chunk_pids = (
                frozenset(expected_pids[start:end])
                if expected_pids is not None
                else None
            )
            edits = item.get("edits")
            if not isinstance(edits, list):
                return f"stage_progress.r_editor chunk {chunk_index}: edits is not a list"
            for edit_index, edit in enumerate(edits):
                if not isinstance(edit, dict):
                    return (
                        f"stage_progress.r_editor chunk {chunk_index} edit "
                        f"{edit_index}: not an object"
                    )
                if set(edit) != _R_EDIT_KEYS:
                    return (
                        f"stage_progress.r_editor chunk {chunk_index} edit "
                        f"{edit_index}: foreign key set {sorted(edit)!r} "
                        f"(expected {sorted(_R_EDIT_KEYS)})"
                    )
                edit_pid = edit.get("pid")
                if not isinstance(edit_pid, str) or not edit_pid:
                    return (
                        f"stage_progress.r_editor chunk {chunk_index} edit "
                        f"{edit_index}: pid is missing or not a string"
                    )
                if expected_pid_set is not None and edit_pid not in expected_pid_set:
                    return (
                        f"stage_progress.r_editor chunk {chunk_index} edit "
                        f"{edit_index}: pid {edit_pid!r} is not in the translation map"
                    )
                if chunk_pids is not None and edit_pid not in chunk_pids:
                    return (
                        f"stage_progress.r_editor chunk {chunk_index} edit "
                        f"{edit_index}: pid {edit_pid!r} is outside the chunk "
                        f"boundary span {first_pid!r}..{last_pid!r}"
                    )
                if not isinstance(edit.get("original"), str) or not edit.get("original"):
                    return (
                        f"stage_progress.r_editor chunk {chunk_index} edit "
                        f"{edit_index} pid {edit_pid}: original is missing or not a string"
                    )
                # FIX RV2 (t_d996bbf7): the R editor rewrites the CURRENT raw
                # text — ``original`` must be a verbatim substring of it.
                # Without this binding a recomputed hash could replay an edit
                # whose original never existed in the text the model saw.
                if current_text is not None and edit.get("original") not in str(
                    current_text.get(edit_pid, "")
                ):
                    return (
                        f"stage_progress.r_editor chunk {chunk_index} edit "
                        f"{edit_index} pid {edit_pid}: original is not a substring "
                        f"of the current text (tampered cache edit)"
                    )
                if not isinstance(edit.get("rewritten"), str) or not edit.get("rewritten").strip():
                    return (
                        f"stage_progress.r_editor chunk {chunk_index} edit "
                        f"{edit_index} pid {edit_pid}: rewritten is missing or not a string"
                    )
                if not isinstance(edit.get("reason"), str) or not edit.get("reason").strip():
                    return (
                        f"stage_progress.r_editor chunk {chunk_index} edit "
                        f"{edit_index} pid {edit_pid}: reason is missing or not a string"
                    )
                klass = edit.get("class")
                if not isinstance(klass, str) or klass not in ALL_CLASSES:
                    return (
                        f"stage_progress.r_editor chunk {chunk_index} edit "
                        f"{edit_index} pid {edit_pid}: unknown edit class {klass!r} "
                        f"(allowed: {sorted(ALL_CLASSES)})"
                    )
    elif r_outcome is not None:
        # pending/disabled/failed: a non-null outcome is a schema violation.
        return (
            f"stage_progress.r_editor: outcome must be null when status is "
            f"{r_status!r} (got {type(r_outcome).__name__})"
        )

    # ------------------------------------------------------------------
    # audit
    # ------------------------------------------------------------------
    audit = stage_progress.get("audit")
    if audit is None:
        return "stage_progress.audit is missing"
    if not isinstance(audit, dict):
        return "stage_progress.audit is not an object"
    if set(audit) != _AUDIT_STAGE_KEYS:
        return (
            f"stage_progress.audit foreign key set {sorted(audit)!r} "
            f"(expected {sorted(_AUDIT_STAGE_KEYS)})"
        )
    a_status = audit.get("status")
    if not isinstance(a_status, str) or a_status not in ("pending", "partial", "complete"):
        return f"stage_progress.audit.status is missing or unknown {a_status!r}"
    a_chunks = audit.get("chunks")
    if not isinstance(a_chunks, list):
        return "stage_progress.audit.chunks is not a list"
    a_issues = audit.get("issues")
    if not isinstance(a_issues, list):
        return "stage_progress.audit.issues is not a list"
    if a_status == "pending" and a_chunks:
        return "stage_progress.audit is pending but carries chunk payloads"
    if a_status == "pending" and a_issues:
        return "stage_progress.audit is pending but carries issues"
    done = audit.get("done_chunks")
    if not isinstance(done, list) or any(
        not isinstance(c, int) or isinstance(c, bool) for c in done
    ):
        return "stage_progress.audit.done_chunks is not a list of ints"
    if done != sorted(set(done)):
        return "stage_progress.audit.done_chunks contains duplicates"
    if done != list(range(1, len(done) + 1)):
        return (
            "stage_progress.audit.done_chunks is not contiguous 1..N "
            "(coverage gap — a kill between chunks must never look complete)"
        )
    failed = audit.get("failed_chunks")
    if not isinstance(failed, list) or any(
        not isinstance(c, int) or isinstance(c, bool) for c in failed
    ):
        return "stage_progress.audit.failed_chunks is not a list of ints"
    if any(c not in done for c in failed):
        return "stage_progress.audit.failed_chunks is not a subset of done_chunks"
    # FIX RV2-findings (t_006f3a79): the persisted chunk records are
    # type-validated BEFORE field access — a malformed chunk record (e.g.
    # `[ [ ] ]`) must fail closed as a clean miss (None), never an
    # AttributeError escaping the validator. The chunk-index coverage
    # comparison against done_chunks is enforced UNCONDITIONALLY (not only
    # under ``if done``): ``done_chunks=[]`` with a carried GOOD chunk
    # record is an unmarked chunk that must never enter a resume plan.
    if any(not isinstance(c, dict) for c in a_chunks):
        return "stage_progress.audit.chunks contains a non-object record"
    if [c.get("chunk") for c in a_chunks] != done:
        return (
            "stage_progress.audit.chunks indices do not match done_chunks "
            "(coverage mismatch)"
        )
    pid_positions = (
        {pid: position for position, pid in enumerate(expected_pids)}
        if expected_pids is not None
        else None
    )
    for position, item in enumerate(a_chunks, start=1):
        if not isinstance(item, dict):
            return f"stage_progress.audit chunk at position {position}: not an object"
        if not set(item) <= _AUDIT_CHUNK_KEYS:
            return (
                f"stage_progress.audit chunk at position {position}: foreign key "
                f"set {sorted(item)!r} (allowed {sorted(_AUDIT_CHUNK_KEYS)})"
            )
        chunk_index = item.get("chunk")
        if chunk_index != position:
            return (
                f"stage_progress.audit chunk coverage/order mismatch: stored "
                f"index {chunk_index!r} at position {position} (expected 1..N)"
            )
        status = item.get("status")
        if not isinstance(status, str) or status not in _AUDIT_CHUNK_STATUSES:
            return f"stage_progress.audit chunk {chunk_index}: unknown status {status!r}"
        first_pid = item.get("first_pid")
        last_pid = item.get("last_pid")
        if not isinstance(first_pid, str) or not first_pid:
            return f"stage_progress.audit chunk {chunk_index}: first_pid is missing or not a string"
        if not isinstance(last_pid, str) or not last_pid:
            return f"stage_progress.audit chunk {chunk_index}: last_pid is missing or not a string"
        if expected_pid_set is not None and (
            first_pid not in expected_pid_set or last_pid not in expected_pid_set
        ):
            return (
                f"stage_progress.audit chunk {chunk_index}: boundary pids "
                f"{first_pid!r}/{last_pid!r} are not in the translation map"
            )
        if not isinstance(item.get("pair_count"), int) or isinstance(item.get("pair_count"), bool) or item.get("pair_count") < 1:
            return f"stage_progress.audit chunk {chunk_index}: pair_count is not a positive int"
        if not isinstance(item.get("context_count"), int) or isinstance(item.get("context_count"), bool) or item.get("context_count") < 0:
            return f"stage_progress.audit chunk {chunk_index}: context_count is not a non-negative int"
        if not isinstance(item.get("reasoning_chars"), int) or isinstance(item.get("reasoning_chars"), bool) or item.get("reasoning_chars") < 0:
            return f"stage_progress.audit chunk {chunk_index}: reasoning_chars is not a non-negative int"
        if not isinstance(item.get("reasoning_file"), str):
            return f"stage_progress.audit chunk {chunk_index}: reasoning_file is not a string"
        # FIX RV2 (t_d996bbf7): legacy validator parity — the remaining
        # ChunkMeta.to_payload fields are type-bound too.
        finish_reason = item.get("finish_reason")
        if finish_reason is not None and not isinstance(finish_reason, str):
            return f"stage_progress.audit chunk {chunk_index}: finish_reason is not a string or null"
        if not isinstance(item.get("issue_count"), int) or isinstance(item.get("issue_count"), bool) or item.get("issue_count") < 0:
            return f"stage_progress.audit chunk {chunk_index}: issue_count is not a non-negative int"
        # CONTEXT-PID-DROP: the persisted dropped warning count is part of the
        # incremental cache contract — a malformed/negative value is a full
        # miss (never a trusted replay with a fabricated count).
        dropped_count = item.get("dropped_count")
        if dropped_count is not None and (
            not isinstance(dropped_count, int)
            or isinstance(dropped_count, bool)
            or dropped_count < 0
        ):
            return (
                f"stage_progress.audit chunk {chunk_index}: dropped_count "
                f"{dropped_count!r} is not a non-negative int"
            )
    for issue_index, issue in enumerate(a_issues):
        if not isinstance(issue, dict):
            return f"stage_progress.audit issue {issue_index}: not an object"
        if set(issue) != _ISSUE_KEYS:
            return (
                f"stage_progress.audit issue {issue_index}: key set mismatch "
                f"(expected {sorted(_ISSUE_KEYS)}, got {sorted(issue)})"
            )
        pid = issue.get("id")
        if not isinstance(pid, str) or not pid:
            return f"stage_progress.audit issue {issue_index}: id is missing or not a string"
        if expected_pid_set is not None and pid not in expected_pid_set:
            return f"stage_progress.audit issue {issue_index}: id {pid!r} is not in the translation map"
        if issue.get("category") not in AUDIT_V4_CATEGORIES:
            return f"stage_progress.audit issue {pid}: invalid category {issue.get('category')!r}"
        if issue.get("severity") not in AUDIT_V4_SEVERITIES:
            return f"stage_progress.audit issue {pid}: invalid severity {issue.get('severity')!r}"
        if issue.get("confidence") not in AUDIT_V4_CONFIDENCES:
            return f"stage_progress.audit issue {pid}: invalid confidence {issue.get('confidence')!r}"
        if not isinstance(issue.get("note"), str) or not issue.get("note").strip():
            return f"stage_progress.audit issue {pid}: note is missing, not a string, or empty"
        if not isinstance(issue.get("excerpt"), str) or not issue.get("excerpt").strip():
            return f"stage_progress.audit issue {pid}: excerpt is missing, not a string, or empty"
        debug = issue.get("_debug")
        if not isinstance(debug, dict):
            return f"stage_progress.audit issue {pid}: _debug is missing or not an object"
        chunk = debug.get("chunk")
        if not isinstance(chunk, int) or isinstance(chunk, bool) or not (1 <= chunk <= len(a_chunks)):
            return (
                f"stage_progress.audit issue {pid}: _debug.chunk {chunk!r} is not "
                f"inside the covered chunk indices 1..{len(a_chunks)}"
            )
        if pid_positions is not None:
            chunk_record = a_chunks[chunk - 1]
            first_pos = pid_positions.get(chunk_record.get("first_pid"))
            last_pos = pid_positions.get(chunk_record.get("last_pid"))
            pid_pos = pid_positions.get(pid)
            if (
                first_pos is None or last_pos is None or pid_pos is None
                or not (first_pos <= pid_pos <= last_pos)
            ):
                return (
                    f"stage_progress.audit issue {pid}: id is not inside the actual "
                    f"pid span of chunk {chunk} ({chunk_record.get('first_pid')!r}.."
                    f"{chunk_record.get('last_pid')!r})"
                )

    # ------------------------------------------------------------------
    # repair
    # ------------------------------------------------------------------
    repair = stage_progress.get("repair")
    if repair is None:
        return "stage_progress.repair is missing"
    if not isinstance(repair, dict):
        return "stage_progress.repair is not an object"
    if set(repair) != _REPAIR_STAGE_KEYS:
        return (
            f"stage_progress.repair foreign key set {sorted(repair)!r} "
            f"(expected {sorted(_REPAIR_STAGE_KEYS)})"
        )
    rp_status = repair.get("status")
    if not isinstance(rp_status, str) or rp_status not in ("pending", "partial", "complete", "skipped", "failed"):
        return f"stage_progress.repair.status is missing or unknown {rp_status!r}"
    done_batches = repair.get("done_batches")
    if not isinstance(done_batches, list) or any(
        not isinstance(c, int) or isinstance(c, bool) for c in done_batches
    ):
        return "stage_progress.repair.done_batches is not a list of ints"
    if done_batches != sorted(set(done_batches)):
        return "stage_progress.repair.done_batches contains duplicates"
    if any(c < 1 for c in done_batches):
        return "stage_progress.repair.done_batches contains a non-positive index"
    committed = repair.get("committed")
    if committed is None:
        return "stage_progress.repair.committed is missing"
    if not isinstance(committed, dict):
        return "stage_progress.repair.committed is not an object"
    for pid, text in committed.items():
        if expected_pid_set is not None and pid not in expected_pid_set:
            return f"stage_progress.repair.committed pid {pid!r} is not in the translation map"
        if not isinstance(text, str) or not text:
            return f"stage_progress.repair.committed pid {pid!r}: text is missing or not a non-empty string"
    passed = repair.get("passed")
    if passed is None:
        return "stage_progress.repair.passed is missing"
    if not isinstance(passed, list):
        return "stage_progress.repair.passed is not a list"
    for pid in passed:
        if not isinstance(pid, str) or not pid:
            return f"stage_progress.repair.passed contains a non-string entry {pid!r}"
        if expected_pid_set is not None and pid not in expected_pid_set:
            return f"stage_progress.repair.passed pid {pid!r} is not in the translation map"
    rp_outcome = repair.get("outcome")
    if rp_status in ("pending", "skipped", "failed"):
        # FIX RV2 (t_d996bbf7): a non-null outcome for a status that never
        # ran is a schema violation (legacy validator parity) — and a
        # pending/skipped/failed stage must carry no replayable slices.
        if rp_outcome is not None:
            return (
                f"stage_progress.repair.outcome must be null when status is "
                f"{rp_status!r} (got {type(rp_outcome).__name__})"
            )
        if done_batches or committed or passed:
            return (
                f"stage_progress.repair carries done_batches/committed/passed "
                f"while status is {rp_status!r} (a stage that never ran has "
                f"nothing replayable)"
            )
    else:
        # partial / complete: outcome REQUIRED and validated in full — the
        # repair_resume_plan() copies batches/results VERBATIM into the
        # SelectiveRepairEvaluator, so every record is checked with the same
        # strictness as the fresh parse_repair_batch path before replay.
        if not isinstance(rp_outcome, dict):
            return (
                f"stage_progress.repair.outcome is required when status is "
                f"{rp_status!r} (got {type(rp_outcome).__name__})"
            )
        if set(rp_outcome) != _REPAIR_OUTCOME_KEYS:
            return (
                f"stage_progress.repair.outcome foreign key set "
                f"{sorted(rp_outcome)!r} (expected {sorted(_REPAIR_OUTCOME_KEYS)})"
            )
        batches = rp_outcome.get("batches")
        if not isinstance(batches, list) or not batches:
            return (
                "stage_progress.repair.outcome.batches is missing, not a "
                "list, or empty"
            )
        batch_count = rp_outcome.get("batch_count")
        if (
            not isinstance(batch_count, int)
            or isinstance(batch_count, bool)
            or batch_count < 1
        ):
            return (
                f"stage_progress.repair.outcome.batch_count {batch_count!r} "
                f"is not a positive int"
            )
        if len(batches) > batch_count:
            return (
                f"stage_progress.repair batch coverage mismatch: "
                f"{len(batches)} batch(es) persisted for batch_count "
                f"{batch_count} — a kill can only have processed a PREFIX of "
                f"the planned batches"
            )
        if any(c > batch_count for c in done_batches):
            return (
                f"stage_progress.repair.done_batches index {c!r} is beyond "
                f"batch_count {batch_count}"
            )
        # Recompute what replaying the cached batches would commit/pass —
        # the stored committed/passed must equal it EXACTLY (a tampered
        # result text/pid/decision is caught here even when the hash was
        # recomputed).
        expected_committed: Dict[str, str] = {}
        expected_passed: List[str] = []
        good_indices: List[int] = []
        for position, batch in enumerate(batches, start=1):
            if not isinstance(batch, dict):
                return (
                    f"stage_progress.repair batch at position {position}: "
                    f"not an object"
                )
            if set(batch) != _REPAIR_BATCH_KEYS:
                return (
                    f"stage_progress.repair batch at position {position}: "
                    f"foreign key set {sorted(batch)!r} "
                    f"(expected {sorted(_REPAIR_BATCH_KEYS)})"
                )
            batch_index = batch.get("batch_index")
            if batch_index != position:
                return (
                    f"stage_progress.repair batch coverage/order mismatch: "
                    f"stored index {batch_index!r} at position {position} "
                    f"(expected contiguous 1..k of the processed prefix)"
                )
            b_status = batch.get("status")
            if not isinstance(b_status, str) or b_status not in _REPAIR_BATCH_STATUSES:
                return (
                    f"stage_progress.repair batch {batch_index}: unknown "
                    f"status {b_status!r} (allowed: "
                    f"{sorted(_REPAIR_BATCH_STATUSES)})"
                )
            findings = batch.get("findings")
            if not isinstance(findings, list) or not findings:
                return (
                    f"stage_progress.repair batch {batch_index}: findings is "
                    f"missing, not a list, or empty"
                )
            findings_by_index: Dict[int, Dict[str, Any]] = {}
            for finding_position, finding in enumerate(findings, start=1):
                if not isinstance(finding, dict):
                    return (
                        f"stage_progress.repair batch {batch_index} finding "
                        f"{finding_position}: not an object"
                    )
                if set(finding) != _REPAIR_FINDING_KEYS:
                    return (
                        f"stage_progress.repair batch {batch_index} finding "
                        f"{finding_position}: foreign key set {sorted(finding)!r} "
                        f"(expected {sorted(_REPAIR_FINDING_KEYS)})"
                    )
                finding_index = finding.get("index")
                if (
                    not isinstance(finding_index, int)
                    or isinstance(finding_index, bool)
                    or finding_index < 1
                ):
                    return (
                        f"stage_progress.repair batch {batch_index} finding "
                        f"{finding_position}: index {finding_index!r} is not "
                        f"a positive int"
                    )
                # make_microbatches re-numbers every batch 1..M contiguous.
                if finding_index != finding_position:
                    return (
                        f"stage_progress.repair batch {batch_index} finding "
                        f"index coverage/order mismatch: stored "
                        f"{finding_index!r} at position {finding_position} "
                        f"(expected contiguous 1..M within the batch)"
                    )
                finding_pid = finding.get("pid")
                if not isinstance(finding_pid, str) or not finding_pid:
                    return (
                        f"stage_progress.repair batch {batch_index} finding "
                        f"{finding_index}: pid is missing or not a string"
                    )
                if expected_pid_set is not None and finding_pid not in expected_pid_set:
                    return (
                        f"stage_progress.repair batch {batch_index} finding "
                        f"{finding_index}: pid {finding_pid!r} is not in the "
                        f"translation map"
                    )
                for field in ("tier", "category", "severity", "confidence", "source_stage"):
                    if not isinstance(finding.get(field), str):
                        return (
                            f"stage_progress.repair batch {batch_index} finding "
                            f"{finding_index}: {field} is not a string"
                        )
                sources = finding.get("sources")
                if not isinstance(sources, list) or any(
                    not isinstance(s, dict) for s in sources
                ):
                    return (
                        f"stage_progress.repair batch {batch_index} finding "
                        f"{finding_index}: sources is not a list of objects"
                    )
                findings_by_index[finding_index] = finding
            results = batch.get("results")
            if not isinstance(results, list):
                return (
                    f"stage_progress.repair batch {batch_index}: results is "
                    f"not a list"
                )
            seen_indices: set = set()
            for result_position, result in enumerate(results):
                if not isinstance(result, dict):
                    return (
                        f"stage_progress.repair batch {batch_index} result "
                        f"{result_position}: not an object"
                    )
                if set(result) != _REPAIR_RESULT_KEYS:
                    return (
                        f"stage_progress.repair batch {batch_index} result "
                        f"{result_position}: foreign key set {sorted(result)!r} "
                        f"(expected {sorted(_REPAIR_RESULT_KEYS)})"
                    )
                result_index = result.get("index")
                if not isinstance(result_index, int) or isinstance(result_index, bool):
                    return (
                        f"stage_progress.repair batch {batch_index} result "
                        f"{result_position}: index {result_index!r} is not an int"
                    )
                finding = findings_by_index.get(result_index)
                if finding is None:
                    return (
                        f"stage_progress.repair batch {batch_index}: result "
                        f"index {result_index} does not match any finding "
                        f"(unknown index)"
                    )
                if result_index in seen_indices:
                    return (
                        f"stage_progress.repair batch {batch_index}: duplicate "
                        f"result index {result_index}"
                    )
                seen_indices.add(result_index)
                decision = result.get("decision")
                if decision not in _REPAIR_DECISIONS:
                    return (
                        f"stage_progress.repair batch {batch_index} index "
                        f"{result_index}: invalid decision {decision!r} "
                        f"(allowed: {sorted(_REPAIR_DECISIONS)})"
                    )
                if not isinstance(result.get("reason"), str):
                    return (
                        f"stage_progress.repair batch {batch_index} index "
                        f"{result_index}: reason is not a string"
                    )
                if decision == "repair":
                    result_pid = result.get("pid")
                    if (
                        not isinstance(result_pid, str)
                        or result_pid != finding.get("pid")
                    ):
                        return (
                            f"stage_progress.repair batch {batch_index} index "
                            f"{result_index}: repair pid {result_pid!r} does "
                            f"not match finding pid {finding.get('pid')!r} "
                            f"(index/PID contract)"
                        )
                    repaired = result.get("repaired_translation")
                    if not isinstance(repaired, str) or not repaired.strip():
                        return (
                            f"stage_progress.repair batch {batch_index} index "
                            f"{result_index}: repair has empty "
                            f"repaired_translation"
                        )
                    # FIX RV2 (t_d996bbf7): the SAME text gates as the fresh
                    # parse_repair_batch path — a cached GOOD batch can never
                    # contain a no-op repair (converted to pass at fresh
                    # time) or a truncated one (batch-killing error).
                    if edited_text is not None:
                        current_pid_text = str(edited_text.get(result_pid, ""))
                        if repaired.strip() == current_pid_text.strip():
                            return (
                                f"stage_progress.repair batch {batch_index} "
                                f"index {result_index}: no-op repair "
                                f"(repaired_translation equals the current "
                                f"text for pid {result_pid}) — impossible in "
                                f"a cached batch"
                            )
                        if (
                            current_pid_text.strip()
                            and len(repaired.strip())
                            < 0.4 * len(current_pid_text.strip())
                        ):
                            return (
                                f"stage_progress.repair batch {batch_index} "
                                f"index {result_index}: truncated repair — "
                                f"repaired_translation is {len(repaired.strip())} "
                                f"chars vs {len(current_pid_text.strip())} chars "
                                f"current text (<40% preserved; the FULL "
                                f"corrected PID text must be returned)"
                            )
                    expected_committed[result_pid] = repaired
                else:  # pass — the model answers by index, the pid is either
                    # empty or the finding pid (no-op conversion, fresh path).
                    result_pid = result.get("pid")
                    if result_pid not in ("", finding.get("pid")):
                        return (
                            f"stage_progress.repair batch {batch_index} index "
                            f"{result_index}: pass result pid {result_pid!r} "
                            f"does not match finding pid {finding.get('pid')!r}"
                        )
                    if result.get("repaired_translation") != "":
                        return (
                            f"stage_progress.repair batch {batch_index} index "
                            f"{result_index}: pass result carries a "
                            f"repaired_translation (must be empty)"
                        )
                    expected_passed.append(finding.get("pid"))
            if b_status == "GOOD":
                missing_answers = sorted(set(findings_by_index) - seen_indices)
                if missing_answers:
                    return (
                        f"stage_progress.repair batch {batch_index}: missing "
                        f"answer(s) for finding index(es) {missing_answers} — "
                        f"a GOOD batch must answer every finding (coverage gate)"
                    )
                good_indices.append(batch_index)
            # error/warnings/missing_indices are informational journal
            # slices — only their container types are bound (exact key set
            # above), never their content.
            if not isinstance(batch.get("error"), str):
                return (
                    f"stage_progress.repair batch {batch_index}: error is not "
                    f"a string"
                )
            if not isinstance(batch.get("warnings"), list) or any(
                not isinstance(w, str) for w in batch.get("warnings")
            ):
                return (
                    f"stage_progress.repair batch {batch_index}: warnings is "
                    f"not a list of strings"
                )
            if not isinstance(batch.get("missing_indices"), list) or any(
                not isinstance(m, int) or isinstance(m, bool)
                for m in batch.get("missing_indices")
            ):
                return (
                    f"stage_progress.repair batch {batch_index}: "
                    f"missing_indices is not a list of ints"
                )
        # FIX RV2 (t_d996bbf7): done_batches must equal the GOOD batch
        # indices exactly (the progress hook computes it that way) and the
        # stored committed/passed must equal what replaying the cached
        # batches would produce — any divergence is a tamper.
        if list(done_batches) != good_indices:
            return (
                f"stage_progress.repair.done_batches {done_batches!r} does not "
                f"match the GOOD batch indices {good_indices!r}"
            )
        if dict(committed) != expected_committed:
            return (
                "stage_progress.repair.committed does not match the cached "
                f"batch results (stored={sorted(committed)!r} "
                f"expected={sorted(expected_committed)!r})"
            )
        if list(passed) != expected_passed:
            return (
                "stage_progress.repair.passed does not match the cached batch "
                f"results (stored={passed!r} expected={expected_passed!r})"
            )

    # ------------------------------------------------------------------
    # reaudit
    # ------------------------------------------------------------------
    reaudit = stage_progress.get("reaudit")
    if reaudit is None:
        return "stage_progress.reaudit is missing"
    if not isinstance(reaudit, dict):
        return "stage_progress.reaudit is not an object"
    if set(reaudit) != _REAUDIT_STAGE_KEYS:
        return (
            f"stage_progress.reaudit foreign key set {sorted(reaudit)!r} "
            f"(expected {sorted(_REAUDIT_STAGE_KEYS)})"
        )
    ra_status = reaudit.get("status")
    if not isinstance(ra_status, str) or ra_status not in ("pending", "partial", "complete", "failed"):
        return f"stage_progress.reaudit.status is missing or unknown {ra_status!r}"
    ra_done = reaudit.get("done_chunks")
    if not isinstance(ra_done, list):
        return "stage_progress.reaudit.done_chunks is not a list"
    ra_issues = reaudit.get("issues")
    if not isinstance(ra_issues, list):
        return "stage_progress.reaudit.issues is not a list"
    if ra_status == "pending" and (ra_done or ra_issues):
        return (
            "stage_progress.reaudit is pending but carries done_chunks/issues"
        )
    # FIX RV2 (t_d996bbf7): reaudit_resume_plan()/_run_reaudit() copy the
    # cached per-chunk issues VERBATIM (0 model calls) — every issue is
    # validated with the same strictness as a fresh re-audit chunk
    # (validate_chunk_json semantics) AND bound to the pid span of the chunk
    # it lives in; the aggregate ``issues`` list must equal the order-
    # sensitive concatenation of the per-chunk lists.
    expected_aggregate: List[Dict[str, Any]] = []
    pid_positions = (
        {pid: position for position, pid in enumerate(expected_pids)}
        if expected_pids is not None
        else None
    )
    for position, record in enumerate(ra_done, start=1):
        if not isinstance(record, dict):
            return f"stage_progress.reaudit done_chunks record at position {position}: not an object"
        if not set(record) <= _REAUDIT_CHUNK_KEYS:
            return (
                f"stage_progress.reaudit done_chunks record at position "
                f"{position}: foreign key set {sorted(record)!r} "
                f"(allowed {sorted(_REAUDIT_CHUNK_KEYS)})"
            )
        chunk_index = record.get("chunk")
        if chunk_index != position:
            return (
                f"stage_progress.reaudit done_chunks coverage/order mismatch: stored "
                f"index {chunk_index!r} at position {position} (expected 1..N)"
            )
        # CONTEXT-PID-DROP (RV5 t_f82ed9ad): the done record MUST carry an
        # explicit ``failed`` bool — a missing/non-bool marker (a cache
        # written before the failed marker existed, or a tampered one) is a
        # full miss, NEVER a trusted replay: without the marker a chunk that
        # failed fresh (malformed dropped diagnostics) could be replayed as
        # complete with 0 model calls, silently losing the diagnostic/debt.
        failed_marker = record.get("failed")
        if not isinstance(failed_marker, bool):
            return (
                f"stage_progress.reaudit chunk {chunk_index}: failed is "
                f"missing or not a bool"
            )
        first_pid = record.get("first_pid")
        last_pid = record.get("last_pid")
        if not isinstance(first_pid, str) or not first_pid:
            return f"stage_progress.reaudit chunk {chunk_index}: first_pid is missing or not a string"
        if not isinstance(last_pid, str) or not last_pid:
            return f"stage_progress.reaudit chunk {chunk_index}: last_pid is missing or not a string"
        if expected_pid_set is not None and (
            first_pid not in expected_pid_set or last_pid not in expected_pid_set
        ):
            return (
                f"stage_progress.reaudit chunk {chunk_index}: boundary pids "
                f"{first_pid!r}/{last_pid!r} are not in the translation map"
            )
        first_pos = pid_positions.get(first_pid) if pid_positions is not None else None
        last_pos = pid_positions.get(last_pid) if pid_positions is not None else None
        if pid_positions is not None and (
            first_pos is None or last_pos is None or first_pos > last_pos
        ):
            return (
                f"stage_progress.reaudit chunk {chunk_index}: invalid pid "
                f"span {first_pid!r}..{last_pid!r}"
            )
        issues = record.get("issues")
        if not isinstance(issues, list):
            return f"stage_progress.reaudit chunk {chunk_index}: issues is not a list"
        for issue_index, issue in enumerate(issues):
            if not isinstance(issue, dict):
                return (
                    f"stage_progress.reaudit chunk {chunk_index} issue "
                    f"{issue_index}: not an object"
                )
            pid = issue.get("id")
            if not isinstance(pid, str) or not pid:
                return (
                    f"stage_progress.reaudit chunk {chunk_index} issue "
                    f"{issue_index}: id is missing or not a string"
                )
            if issue.get("category") not in AUDIT_V4_CATEGORIES:
                return (
                    f"stage_progress.reaudit chunk {chunk_index} issue {pid}: "
                    f"invalid category {issue.get('category')!r}"
                )
            if issue.get("severity") not in AUDIT_V4_SEVERITIES:
                return (
                    f"stage_progress.reaudit chunk {chunk_index} issue {pid}: "
                    f"invalid severity {issue.get('severity')!r}"
                )
            if issue.get("confidence") not in AUDIT_V4_CONFIDENCES:
                return (
                    f"stage_progress.reaudit chunk {chunk_index} issue {pid}: "
                    f"invalid confidence {issue.get('confidence')!r}"
                )
            if expected_pid_set is not None and pid not in expected_pid_set:
                return (
                    f"stage_progress.reaudit chunk {chunk_index} issue {pid}: "
                    f"id is not in the translation map"
                )
            if pid_positions is not None:
                pid_pos = pid_positions.get(pid)
                if pid_pos is None or not (first_pos <= pid_pos <= last_pos):
                    return (
                        f"stage_progress.reaudit chunk {chunk_index} issue "
                        f"{pid}: id is not inside the actual pid span "
                        f"{first_pid!r}..{last_pid!r}"
                    )
            expected_aggregate.append(dict(issue))
        # CONTEXT-PID-DROP (RV2 t_61af1bb2): dropped context/foreign issue
        # objects are persisted reaudit diagnostics and are validated with
        # the SAME complete well-formed issue contract as cached audit
        # issues — exact _ISSUE_KEYS, non-empty id/note/excerpt strings,
        # valid category/severity/confidence vocab, and a harness _debug
        # {chunk, reasoning_file} attributing the drop to the journaling
        # chunk (the fresh _run_reaudit attaches it exactly like the audit's
        # _with_debug). Any malformed/missing/extra/invalid field is a FULL
        # miss BEFORE either resume plan is built — never a filtered/coerced
        # replay. The PID boundary check is the safe equivalent for
        # context/foreign dropped IDs: a dropped issue's id is by
        # construction NOT in the chunk that journaled it (a context pid
        # lies outside the chunk's span; a foreign/fabricated pid is not in
        # the translation map at all), so the id must NOT lie inside the
        # record's own first_pid..last_pid span — an in-span id would have
        # been a valid issue, never a drop (tampered cache).
        dropped = record.get("dropped")
        if dropped is not None:
            if not isinstance(dropped, list):
                return (
                    f"stage_progress.reaudit chunk {chunk_index}: "
                    f"dropped is not a list"
                )
            for dropped_index, dropped_issue in enumerate(dropped):
                if not isinstance(dropped_issue, dict):
                    return (
                        f"stage_progress.reaudit chunk {chunk_index} dropped "
                        f"issue {dropped_index}: not an object"
                    )
                if set(dropped_issue) != _ISSUE_KEYS:
                    return (
                        f"stage_progress.reaudit chunk {chunk_index} dropped "
                        f"issue {dropped_index}: key set mismatch "
                        f"(expected {sorted(_ISSUE_KEYS)}, got "
                        f"{sorted(dropped_issue)})"
                    )
                pid = dropped_issue.get("id")
                if not isinstance(pid, str) or not pid:
                    return (
                        f"stage_progress.reaudit chunk {chunk_index} dropped "
                        f"issue {dropped_index}: id is missing or not a string"
                    )
                if dropped_issue.get("category") not in AUDIT_V4_CATEGORIES:
                    return (
                        f"stage_progress.reaudit chunk {chunk_index} dropped "
                        f"issue {pid}: invalid category "
                        f"{dropped_issue.get('category')!r}"
                    )
                if dropped_issue.get("severity") not in AUDIT_V4_SEVERITIES:
                    return (
                        f"stage_progress.reaudit chunk {chunk_index} dropped "
                        f"issue {pid}: invalid severity "
                        f"{dropped_issue.get('severity')!r}"
                    )
                if dropped_issue.get("confidence") not in AUDIT_V4_CONFIDENCES:
                    return (
                        f"stage_progress.reaudit chunk {chunk_index} dropped "
                        f"issue {pid}: invalid confidence "
                        f"{dropped_issue.get('confidence')!r}"
                    )
                note = dropped_issue.get("note")
                if not isinstance(note, str) or not note.strip():
                    return (
                        f"stage_progress.reaudit chunk {chunk_index} dropped "
                        f"issue {pid}: note is missing, not a string, or empty"
                    )
                excerpt = dropped_issue.get("excerpt")
                if not isinstance(excerpt, str) or not excerpt.strip():
                    return (
                        f"stage_progress.reaudit chunk {chunk_index} dropped "
                        f"issue {pid}: excerpt is missing, not a string, or "
                        "empty"
                    )
                debug = dropped_issue.get("_debug")
                if not isinstance(debug, dict):
                    return (
                        f"stage_progress.reaudit chunk {chunk_index} dropped "
                        f"issue {pid}: _debug is missing or not an object"
                    )
                if set(debug) != {"chunk", "reasoning_file"}:
                    return (
                        f"stage_progress.reaudit chunk {chunk_index} dropped "
                        f"issue {pid}: _debug key set mismatch "
                        f"(expected ['chunk', 'reasoning_file'], got "
                        f"{sorted(debug)})"
                    )
                debug_chunk = debug.get("chunk")
                if (
                    not isinstance(debug_chunk, int)
                    or isinstance(debug_chunk, bool)
                    or debug_chunk != chunk_index
                ):
                    return (
                        f"stage_progress.reaudit chunk {chunk_index} dropped "
                        f"issue {pid}: _debug.chunk {debug_chunk!r} does not "
                        f"match the journaling chunk {chunk_index}"
                    )
                if not isinstance(debug.get("reasoning_file"), str):
                    return (
                        f"stage_progress.reaudit chunk {chunk_index} dropped "
                        f"issue {pid}: _debug.reasoning_file is not a string"
                    )
                # Safe PID equivalent for dropped IDs: the id must NOT lie
                # inside the journaling record's own pid span. A dropped
                # issue is by construction outside the chunk (context pid
                # from the overlap, or a foreign/fabricated pid absent from
                # the map), so an in-span id is an impossible drop (tamper).
                if pid_positions is not None:
                    first_pos = pid_positions.get(record.get("first_pid"))
                    last_pos = pid_positions.get(record.get("last_pid"))
                    pid_pos = pid_positions.get(pid)
                    if (
                        first_pos is not None
                        and last_pos is not None
                        and pid_pos is not None
                        and first_pos <= pid_pos <= last_pos
                    ):
                        return (
                            f"stage_progress.reaudit chunk {chunk_index} "
                            f"dropped issue {pid}: id is inside the chunk's "
                            f"own pid span "
                            f"({record.get('first_pid')!r}.."
                            f"{record.get('last_pid')!r}) — a dropped issue "
                            f"is by definition outside the chunk (tampered)"
                        )
    ra_issues = reaudit.get("issues")
    if not isinstance(ra_issues, list):
        return "stage_progress.reaudit.issues is not a list"
    for issue_index, issue in enumerate(ra_issues):
        if not isinstance(issue, dict):
            return f"stage_progress.reaudit issue {issue_index}: not an object"
        pid = issue.get("id")
        if not isinstance(pid, str) or not pid:
            return f"stage_progress.reaudit issue {issue_index}: id is missing or not a string"
        if expected_pid_set is not None and pid not in expected_pid_set:
            return f"stage_progress.reaudit issue {issue_index}: id {pid!r} is not in the translation map"
    if list(ra_issues) != expected_aggregate:
        return (
            "stage_progress.reaudit.issues does not match the order-sensitive "
            "concatenation of the per-chunk issue lists (aggregate "
            "inconsistency)"
        )

    # Canonical integrity binding: the whole stage_progress payload must
    # match the hash persisted at save() time — any tamper (even with
    # identity and translations_repaired_hash preserved) is a full miss.
    computed_hash = canonical_json_hash({
        "r_editor": r_editor,
        "audit": audit,
        "repair": repair,
        "reaudit": reaudit,
    })
    if not isinstance(stored_hash, str) or stored_hash != computed_hash:
        return (
            "stage_progress partial_resume_hash mismatch (tampered or "
            "malformed stage_progress; stored=%r computed=%r)"
        ) % (stored_hash, computed_hash)
    return None


def _r_editor_resume_plan_from_report(
    report: Any,
) -> Dict[int, Dict[str, Any]]:
    """Per-chunk R reuse plan from an R report payload (module-level).

    Shared by ``B3AuditCache.r_editor_resume_plan`` (payload from the
    audit cache) and the FAIL-PATH R-CACHE fallback (payload from the
    standalone ``r_editor_report.json`` written when an audit exception
    dropped the cache-write path, 2026-08-15). Same fail-closed guards:
    only GOOD chunks with a list ``edits`` are replayable; a non-list
    edits field is never coerced (chunk re-run).
    """
    if not isinstance(report, dict):
        return {}
    outcome = report.get("outcome")
    if not isinstance(outcome, dict):
        return {}
    plan: Dict[int, Dict[str, Any]] = {}
    for chunk_payload in outcome.get("chunks") or ():
        if not isinstance(chunk_payload, dict):
            continue
        if chunk_payload.get("status") != "GOOD":
            continue
        chunk_index = chunk_payload.get("chunk")
        if not isinstance(chunk_index, int):
            continue
        # PARTIAL-RESUME integrity (t_ec6bb8bc): never coerce a
        # malformed edits field — a non-list is a payload violation and
        # the chunk is re-run (fail-closed per chunk), not stringified
        # into a replayable plan. The authoritative rejection happens in
        # B3AuditCache.load (full miss); this guard only keeps the plan
        # method safe when called on an unvalidated cache/report.
        edits = chunk_payload.get("edits")
        if not isinstance(edits, list):
            LOG.warning(
                "B3: partial r_editor chunk %d edits is not a list (%r) — "
                "chunk re-run (fail-closed)",
                chunk_index, type(edits).__name__,
            )
            continue
        plan[chunk_index] = {
            "status": "GOOD",
            "first_pid": chunk_payload.get("first_pid"),
            "edits": list(edits),
        }
    return plan


def _r_editor_report_identity_mismatch(
    report: Any,
    *,
    translation_hash: str,
    pids: Sequence[str],
    harness_version: str,
    config_identity: str,
    chunk_size: int,
    overlap_pairs: int,
    enabled: bool,
    version: str,
    safe_classes: Sequence[str],
) -> Optional[str]:
    """Fail-closed identity gate for the FAIL-PATH R-CACHE fallback.

    The standalone ``r_editor_report.json`` is read when the audit cache is
    absent (a previous run died inside the audit with an exception). It is
    replayable ONLY when it was produced by the exact same run: same raw
    translation content (hash), same PID set/order, same R harness/prompt
    versions, same safe-class policy, same run config identity, and same
    chunking parameters. A stale report (old harness version, changed chunk
    size, changed text) or a report missing the identity fields
    (pre-identity schema) is rejected with a reason string — the caller then
    re-runs R from scratch instead of replaying GOOD chunks that were
    computed against different input.

    Returns None when the report's persisted identity matches the expected
    values exactly; otherwise a human-readable reason.
    """
    if not isinstance(report, dict):
        return f"report is not an object ({type(report).__name__})"
    expected = {
        "enabled": enabled,
        "version": version,
        "harness_version": harness_version,
        "chunk_size": chunk_size,
        "overlap_pairs": overlap_pairs,
        "safe_classes": sorted(safe_classes),
        "translation_hash": translation_hash,
        "pids": list(pids),
        "config_identity": config_identity,
    }
    for key, wanted in expected.items():
        got = report.get(key)
        if got != wanted:
            return (
                f"identity {key} mismatch "
                f"(report={got!r}, expected={wanted!r})"
            )
    return None


class B3AuditCache:
    """Persistent audit cache with resume identity (card §10 B3 item 3).

    Identity = snapshot_hash + translation_hash + config_identity +
    backend_identity_hash + prompt_version + harness_version +
    entity_context_hash (present only when entity context is enabled).
    ``audit_complete=True`` is a FULL hit (the stored repaired map is
    replayed, 0 model calls). ``audit_complete=False`` with an intact
    identity is a PARTIAL hit (PARTIAL-RESUME t_a58dd881): GOOD chunks are
    replayed per-chunk, failed chunks re-run — never a full replay of an
    incomplete audit (fail-closed), and never a replay across an identity
    mismatch (full miss, as before).
    """

    def __init__(self, path: Path, payload: Optional[Mapping[str, Any]] = None) -> None:
        self.path = Path(path)
        self._payload: Optional[Dict[str, Any]] = (
            dict(payload) if payload is not None else None
        )

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        snapshot_hash: str,
        translation_hash: str,
        config_identity: str,
        backend_identity_hash: str,
        prompt_version: str,
        harness_version: str,
        entity_context_hash: Optional[str],
        entity_context_enabled: bool,
        # FIX RV2-B (t_a4f8f2b2): the CURRENT run's R enablement decides
        # whether a missing/None stored r_editor is a schema violation
        # (R enabled — the report is REQUIRED) or the legitimate disabled
        # representation (R disabled — save() writes null).
        r_editor_enabled: bool,
        expected_pids: Optional[Sequence[str]] = None,
        current_text: Optional[Mapping[str, str]] = None,
    ) -> Optional["B3AuditCache"]:
        if not Path(path).exists():
            return None
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            LOG.warning("B3: audit cache unreadable (%s); re-running audit", exc)
            return None
        if not isinstance(payload, dict):
            LOG.warning("B3: audit cache is not an object; re-running audit")
            return None
        expected = {
            "snapshot_hash": snapshot_hash,
            "translation_hash": translation_hash,
            "config_identity": config_identity,
            "backend_identity_hash": backend_identity_hash,
            "prompt_version": prompt_version,
            "harness_version": harness_version,
            "entity_context_enabled": entity_context_enabled,
            "entity_context_hash": entity_context_hash,
        }
        for key, value in expected.items():
            if payload.get(key) != value:
                LOG.info(
                    "B3: audit cache identity mismatch on %s "
                    "(stored=%r expected=%r); re-running audit",
                    key, payload.get(key), value,
                )
                return None
        if payload.get("schema") != B3_AUDIT_CACHE_SCHEMA:
            LOG.info("B3: audit cache schema mismatch; re-running audit")
            return None
        # F4 (B3 review): the cached repaired map is validated before it can
        # ever be reused/publicized. A structurally tampered cache (extra /
        # missing / reordered PIDs, non-string values, or a repaired-map hash
        # that does not bind to the stored map) is a MISS — the audit re-runs
        # and the tampered map is never published. Old-schema caches (no
        # ``translations_repaired_hash`` field) also miss. The check also
        # guards the PARTIAL path: the edited-map fail-closed guard reads
        # ``stored_translations_repaired``, so a tampered map must be a miss
        # there too.
        repaired = payload.get("translations_repaired")
        if not isinstance(repaired, dict):
            LOG.warning(
                "B3: audit cache translations_repaired is not an object; "
                "re-running audit"
            )
            return None
        if expected_pids is not None:
            stored_pids = list(repaired.keys())
            if stored_pids != list(expected_pids):
                LOG.warning(
                    "B3: audit cache translations_repaired PID set mismatch "
                    "(stored=%r expected=%r); re-running audit",
                    stored_pids, list(expected_pids),
                )
                return None
        if any(not isinstance(value, str) for value in repaired.values()):
            LOG.warning(
                "B3: audit cache translations_repaired contains non-string "
                "values; re-running audit"
            )
            return None
        stored_hash = payload.get("translations_repaired_hash")
        computed_hash = canonical_json_hash(dict(sorted(repaired.items())))
        if not isinstance(stored_hash, str) or stored_hash != computed_hash:
            LOG.warning(
                "B3: audit cache translations_repaired hash mismatch "
                "(stored=%r computed=%r) — old schema or tampered cache; "
                "re-running audit",
                stored_hash, computed_hash,
            )
            return None
        if payload.get("audit_complete") is not True:
            # PARTIAL-RESUME (t_a58dd881): identity matched but the cached
            # audit did not complete. NOT a full miss anymore — the cache is
            # returned as a PARTIAL hit: GOOD chunks (status GOOD/GOOD_RETRIED,
            # valid JSON confirmed at write) and their top-level issues are
            # reused; TRANSPORT_ERROR/EMPTY/FAILED chunks are marked for
            # re-run. Fail-closed is preserved: the cached repaired map is
            # never replayed, the gate stays at audit_complete=False until
            # EVERY chunk has a status, and a cache is never downgraded.
            # PARTIAL-RESUME integrity (t_ec6bb8bc): BEFORE either resume
            # plan is constructed the complete replay payload (chunks /
            # issues / r_editor) is structurally validated and bound to its
            # persisted canonical hash — any malformed, missing, duplicate,
            # extra, or mismatched partial payload is a FULL cache miss
            # (the audit re-runs), never a partial replay. A tampered
            # payload can no longer publish unauthorized issues/edits with
            # 0 model calls while identity and translations_repaired_hash
            # stay intact. FIX RV2-B (t_a4f8f2b2): for an enabled-R cache
            # the R report itself is part of that required payload —
            # a missing/None stored r_editor is rejected here (full miss)
            # before either resume plan is built.
            # KILL-SAFE-INCREMENTAL (t_2d16962c): when the payload carries a
            # ``stage_progress`` block (incremental rewrite after every
            # chunk/batch), the VALIDATION TARGET switches to that block —
            # the same fail-closed policy (any tamper/coverage gap/foreign
            # schema = full miss, never a partial replay of a payload the
            # validator cannot fully trust).
            if payload.get("stage_progress") is not None:
                reason = _validate_stage_progress(
                    payload.get("stage_progress"),
                    expected_pids=expected_pids,
                    stored_hash=payload.get("partial_resume_hash"),
                    r_editor_enabled=r_editor_enabled,
                    # FIX RV2 (t_d996bbf7): the current RAW text binds every
                    # cached R edit (original-substring check) and the
                    # payload's OWN translations_repaired (the R-edited map
                    # at the last incremental save) binds the cached repair
                    # results (no-op / truncation gates).
                    current_text=current_text,
                    edited_text=payload.get("translations_repaired"),
                )
            else:
                reason = _validate_partial_payload(
                    payload,
                    expected_pids=expected_pids,
                    current_text=current_text,
                    r_editor_enabled=r_editor_enabled,
                )
            if reason is not None:
                LOG.warning(
                    "B3: audit cache partial payload rejected (%s); "
                    "re-running the full audit (fail-closed)",
                    reason,
                )
                return None
            LOG.info(
                "B3: cached audit is incomplete (audit_complete=%r); "
                "PARTIAL resume — GOOD chunks reused, failed chunks re-run",
                payload.get("audit_complete"),
            )
            return cls(path, payload)
        return cls(path, payload)

    def is_hit(self) -> bool:
        return self._payload is not None

    def audit_complete(self) -> bool:
        return bool(self._payload and self._payload.get("audit_complete") is True)

    def entity_context_hash(self) -> Optional[str]:
        if not self._payload:
            return None
        return self._payload.get("entity_context_hash")

    def stored_issues(self) -> Tuple[Dict[str, Any], ...]:
        if not self._payload:
            return ()
        return tuple(self._payload.get("issues") or ())

    def stored_filtered(self) -> Tuple[FilteredIssue, ...]:
        if not self._payload:
            return ()
        filtered: list = []
        for item in self._payload.get("filtered") or ():
            filtered.append(FilteredIssue(**item))
        return tuple(filtered)

    def stored_repair(self) -> Optional[Dict[str, Any]]:
        if not self._payload:
            return None
        return self._payload.get("repair")

    def stored_translations_repaired(self) -> Optional[Dict[str, str]]:
        if not self._payload:
            return None
        value = self._payload.get("translations_repaired")
        # F4: values are validated at load() (non-string values reject the
        # cache), so no stringification is applied here — a tampered non-
        # string value must never be silently coerced into publication.
        if not isinstance(value, dict):
            return None
        return dict(value)

    def stored_r_editor(self) -> Optional[Dict[str, Any]]:
        """The stored V4.2 R-stage report (None when the cache predates R or
        the stage was disabled — the caller falls back to a disabled
        report)."""
        if not self._payload:
            return None
        value = self._payload.get("r_editor")
        if not isinstance(value, dict):
            return None
        return dict(value)

    # ------------------------------------------------------------------
    # PARTIAL-RESUME (t_a58dd881): per-chunk reuse plans
    # ------------------------------------------------------------------

    _GOOD_STATUSES = ("GOOD", "GOOD_RETRIED")

    def is_partial(self) -> bool:
        """True when the cached audit is a PARTIAL hit (identity matched,
        audit did not complete) — GOOD chunks reusable, failed re-run."""
        return bool(self._payload and self._payload.get("audit_complete") is not True)

    def audit_resume_plan(self) -> Dict[int, Dict[str, Any]]:
        """Per-chunk audit reuse plan for a partial cache.

        Returns ``{chunk_index: {status, first_pid, last_pid, pair_count,
        context_count, reasoning_chars, reasoning_file, finish_reason,
        issues}}`` for every cached chunk whose status is GOOD/GOOD_RETRIED
        (valid JSON was confirmed at write time — never re-validated here).
        ``issues`` are the cached issues attributed to this chunk via their
        ``_debug.chunk`` (the deduped list is the authoritative issue set; a
        GOOD_RETRIED chunk's sub-issues carry the parent chunk index). Empty
        when the cache is not a partial hit or has no reusable chunks.

        KILL-SAFE-INCREMENTAL (t_2d16962c): reads the accumulated audit
        slices from ``stage_progress.audit`` (chunks + issues) when the
        payload carries an incremental ``stage_progress`` block; legacy
        caches read the top-level ``chunks``/``issues`` as before.
        """
        if not self._payload:
            return {}
        stage_progress = self._payload.get("stage_progress")
        if isinstance(stage_progress, dict):
            audit_stage = stage_progress.get("audit")
            if isinstance(audit_stage, dict):
                chunk_payloads = audit_stage.get("chunks")
                issues = audit_stage.get("issues")
                if isinstance(chunk_payloads, list):
                    return self._audit_resume_plan_from_slices(
                        chunk_payloads, issues if isinstance(issues, list) else ()
                    )
        chunk_payloads = self._payload.get("chunks")
        if not isinstance(chunk_payloads, list):
            return {}
        return self._audit_resume_plan_from_slices(
            chunk_payloads, self._payload.get("issues") or ()
        )

    @staticmethod
    def _audit_resume_plan_from_slices(
        chunk_payloads: Sequence[Mapping[str, Any]],
        issues: Sequence[Mapping[str, Any]],
    ) -> Dict[int, Dict[str, Any]]:
        issues_by_chunk: Dict[int, List[Dict[str, Any]]] = {}
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            debug = issue.get("_debug")
            chunk = debug.get("chunk") if isinstance(debug, dict) else None
            if isinstance(chunk, int):
                issues_by_chunk.setdefault(chunk, []).append(dict(issue))
        plan: Dict[int, Dict[str, Any]] = {}
        for chunk_payload in chunk_payloads:
            if not isinstance(chunk_payload, dict):
                continue
            if chunk_payload.get("status") not in B3AuditCache._GOOD_STATUSES:
                continue
            chunk_index = chunk_payload.get("chunk")
            if not isinstance(chunk_index, int):
                continue
            plan[chunk_index] = {
                "status": str(chunk_payload.get("status")),
                "first_pid": chunk_payload.get("first_pid"),
                "last_pid": chunk_payload.get("last_pid"),
                "pair_count": chunk_payload.get("pair_count"),
                "context_count": chunk_payload.get("context_count"),
                "reasoning_chars": chunk_payload.get("reasoning_chars", 0),
                "reasoning_file": chunk_payload.get("reasoning_file", ""),
                "finish_reason": chunk_payload.get("finish_reason"),
                # CONTEXT-PID-DROP: the persisted dropped warning count rides
                # the resume plan so a replayed GOOD chunk re-emits the exact
                # audit_chunk_done dropped_count (validated at load time).
                "dropped_count": chunk_payload.get("dropped_count", 0),
                "issues": issues_by_chunk.get(chunk_index, []),
            }
        return plan

    def r_editor_resume_plan(self) -> Dict[int, Dict[str, Any]]:
        """Per-chunk Russian-editor reuse plan for a partial cache.

        Returns ``{chunk_index: {status, first_pid, edits}}`` for every
        cached R chunk whose status is GOOD — ``edits`` are the parse-
        validated per-chunk edit payloads stored in the R report (replayed
        with 0 model calls on resume; the caller re-runs only the failed
        chunks). Empty when R was disabled / the cache predates R / the R
        stage had no GOOD chunks.

        KILL-SAFE-INCREMENTAL (t_2d16962c): reads the accumulated R outcome
        from ``stage_progress.r_editor.outcome`` when the payload carries an
        incremental ``stage_progress`` block; legacy caches read the
        top-level ``r_editor`` report as before.
        """
        if not self._payload:
            return {}
        stage_progress = self._payload.get("stage_progress")
        if isinstance(stage_progress, dict):
            r_editor_stage = stage_progress.get("r_editor")
            if isinstance(r_editor_stage, dict):
                return _r_editor_resume_plan_from_report({
                    "outcome": r_editor_stage.get("outcome"),
                })
        return _r_editor_resume_plan_from_report(
            self._payload.get("r_editor")
        )

    # ------------------------------------------------------------------
    # KILL-SAFE-INCREMENTAL (t_2d16962c): repair / reaudit resume plans
    # ------------------------------------------------------------------

    def repair_resume_plan(self) -> Optional[Dict[int, Dict[str, Any]]]:
        """Per-batch repair reuse plan for an incremental partial cache.

        Returns ``{batch_index: {status, findings_pids, results}}`` for
        every cached GOOD repair batch (from ``stage_progress.repair``) —
        the repair evaluator replays a batch with 0 model calls only when
        its finding pids EXACTLY match the current batch (fail-closed on
        mismatch). None when the payload has no incremental repair state
        (legacy caches / repair not started).
        """
        if not self._payload:
            return None
        stage_progress = self._payload.get("stage_progress")
        if not isinstance(stage_progress, dict):
            return None
        repair_stage = stage_progress.get("repair")
        if not isinstance(repair_stage, dict):
            return None
        outcome = repair_stage.get("outcome")
        if not isinstance(outcome, dict):
            return None
        plan: Dict[int, Dict[str, Any]] = {}
        for batch in outcome.get("batches") or ():
            if not isinstance(batch, dict):
                continue
            if batch.get("status") != "GOOD":
                continue
            batch_index = batch.get("batch_index")
            if not isinstance(batch_index, int):
                continue
            plan[batch_index] = {
                "status": "GOOD",
                "findings_pids": [
                    str(f.get("pid"))
                    for f in (batch.get("findings") or ())
                    if isinstance(f, dict)
                ],
                "results": [
                    dict(r) for r in (batch.get("results") or ())
                    if isinstance(r, dict)
                ],
            }
        return plan or None

    def reaudit_resume_plan(self) -> Dict[int, Dict[str, Any]]:
        """Per-chunk reaudit reuse plan for an incremental partial cache.

        Returns ``{chunk_index: {first_pid, last_pid, issues}}`` for every
        cached reaudit chunk (from ``stage_progress.reaudit``) — the reaudit
        loop replays a chunk with 0 model calls only when its boundaries
        match the current chunk (fail-closed on mismatch). CONTEXT-PID-DROP
        (RV5 t_f82ed9ad): a chunk whose done record is marked ``failed``
        (invalid chunk JSON or malformed dropped diagnostics at journal
        time) is NEVER included — the resume re-runs it fail-closed, so the
        malformed input's diagnostic/debt is preserved instead of a 0-call
        replay silently upgrading the chunk to complete. Empty when the
        payload has no incremental reaudit state.
        """
        if not self._payload:
            return {}
        stage_progress = self._payload.get("stage_progress")
        if not isinstance(stage_progress, dict):
            return {}
        reaudit_stage = stage_progress.get("reaudit")
        if not isinstance(reaudit_stage, dict):
            return {}
        plan: Dict[int, Dict[str, Any]] = {}
        for record in reaudit_stage.get("done_chunks") or ():
            if not isinstance(record, dict):
                continue
            # CONTEXT-PID-DROP (RV5 t_f82ed9ad): a failed chunk is never
            # replayable — exclude it from the plan so the next run re-runs
            # it (fail-closed) instead of replaying it as complete.
            if record.get("failed") is True:
                continue
            chunk_index = record.get("chunk")
            if not isinstance(chunk_index, int):
                continue
            plan[chunk_index] = {
                "first_pid": record.get("first_pid"),
                "last_pid": record.get("last_pid"),
                "issues": [
                    dict(issue) for issue in (record.get("issues") or ())
                    if isinstance(issue, dict)
                ],
                # CONTEXT-PID-DROP: the dropped context/foreign issue objects
                # ride the resume plan so a replayed re-audit chunk keeps its
                # journaled diagnostics (validated at load time).
                "dropped": [
                    dict(issue) for issue in (record.get("dropped") or ())
                    if isinstance(issue, dict)
                ],
            }
        return plan

    def save(
        self,
        *,
        snapshot_hash: str,
        translation_hash: str,
        config_identity: str,
        backend_identity_hash: str,
        entity_context_hash: Optional[str],
        entity_context_enabled: bool,
        outcome: Optional[ChunkedAuditOutcome] = None,
        filtered: Sequence[FilteredIssue] = (),
        repair: Optional[SelectiveRepairOutcome] = None,
        translations_repaired: Mapping[str, str],
        r_editor: Optional[Mapping[str, Any]] = None,
        stage_progress: Optional[Mapping[str, Any]] = None,
        # KILL-SAFE-INCREMENTAL (t_2d16962c): explicit versions for the
        # incremental branch, where ``outcome`` is None (R stage) — the
        # identity check on load() compares these, so an incremental cache
        # must carry the CURRENT run's prompt/harness versions.
        prompt_version: Optional[str] = None,
        harness_version: Optional[str] = None,
    ) -> None:
        # PARTIAL-RESUME integrity (t_ec6bb8bc): the replay payload slices
        # are computed ONCE and both written and bound to a canonical hash.
        # load() recomputes the hash over the same slices before any resume
        # plan is built, so a tampered chunks/issues/r_editor payload — even
        # with identity and translations_repaired_hash preserved — is a full
        # cache miss, never a partial replay.
        # KILL-SAFE-INCREMENTAL (t_2d16962c): when ``stage_progress`` is
        # provided the cache is rewritten INCREMENTALLY after every chunk /
        # batch (R, audit, repair, reaudit). The payload carries the identity
        # fields plus the accumulated ``stage_progress`` block; the final
        # save at B3 completion keeps the legacy full payload (no
        # stage_progress) so a completed chapter is a normal full-hit cache.
        if stage_progress is not None:
            payload = {
                "schema": B3_AUDIT_CACHE_SCHEMA,
                "snapshot_hash": snapshot_hash,
                "translation_hash": translation_hash,
                "config_identity": config_identity,
                "backend_identity_hash": backend_identity_hash,
                "prompt_version": (
                    outcome.prompt_version if outcome is not None else (
                        prompt_version if prompt_version is not None else ""
                    )
                ),
                "harness_version": (
                    outcome.harness_version if outcome is not None else (
                        harness_version if harness_version is not None else ""
                    )
                ),
                "entity_context_enabled": entity_context_enabled,
                "entity_context_hash": entity_context_hash,
                "audit_complete": False,
                "translations_repaired": dict(translations_repaired),
                "translations_repaired_hash": canonical_json_hash(
                    dict(sorted(translations_repaired.items()))
                ),
                "stage_progress": dict(stage_progress),
                "partial_resume_hash": canonical_json_hash({
                    "r_editor": stage_progress.get("r_editor"),
                    "audit": stage_progress.get("audit"),
                    "repair": stage_progress.get("repair"),
                    "reaudit": stage_progress.get("reaudit"),
                }),
            }
            _atomic_write_json(self.path, payload)
            self._payload = payload
            return
        chunks_payload = outcome.to_payload()["chunks"]
        issues_payload = [dict(issue) for issue in outcome.issues]
        r_editor_payload = dict(r_editor) if r_editor is not None else None
        payload = {
            "schema": B3_AUDIT_CACHE_SCHEMA,
            "snapshot_hash": snapshot_hash,
            "translation_hash": translation_hash,
            "config_identity": config_identity,
            "backend_identity_hash": backend_identity_hash,
            "prompt_version": outcome.prompt_version,
            "harness_version": outcome.harness_version,
            "entity_context_enabled": entity_context_enabled,
            "entity_context_hash": entity_context_hash,
            "audit_complete": outcome.audit_complete,
            "issue_count": outcome.issue_count,
            "issues": issues_payload,
            "chunks": chunks_payload,
            "filtered": [
                {
                    "issue": dict(f.issue),
                    "verdict": f.verdict,
                    "filter_name": f.filter_name,
                    "reason": f.reason,
                    # CANDIDATE-MERGE (t_0ffe56e1): the source stage is part
                    # of the cached filtered round-trip (old caches without
                    # the key restore the fidelity_auditor default).
                    "source_stage": f.source_stage,
                }
                for f in filtered
            ],
            "repair": repair.to_payload() if repair is not None else None,
            "translations_repaired": dict(translations_repaired),
            # V4.2 R: the Russian-editor report rides the audit cache so a
            # full cache hit restores the R outcome (edit_candidates +
            # accept/reject journal) with 0 model calls.
            "r_editor": r_editor_payload,
            # F4: canonical hash of the repaired map binds the map to this
            # cache record. load() recomputes it and rejects a mismatch (old
            # schema / tampered map), so a structurally tampered
            # translations_repaired can never be replayed or publicized.
            "translations_repaired_hash": canonical_json_hash(
                dict(sorted(translations_repaired.items()))
            ),
            # PARTIAL-RESUME integrity (t_ec6bb8bc): canonical hash of the
            # partial replay payload (chunks + issues + r_editor). load()
            # recomputes it on the partial branch and rejects a mismatch —
            # a tampered GOOD-chunk evidence/edit set is a full miss.
            "partial_resume_hash": canonical_json_hash({
                "chunks": chunks_payload,
                "issues": issues_payload,
                "r_editor": r_editor_payload,
            }),
        }
        _atomic_write_json(self.path, payload)
        self._payload = payload


# ---------------------------------------------------------------------------
# Orchestrator bundle
# ---------------------------------------------------------------------------


class B3AuditRepair:
    """Production B3 pipeline bundle (transport-neutral, injectable).

    Usage (CLI wires it from ``build_role_backend``)::

        b3 = B3AuditRepair(
            audit_backend=completion_backend,      # Qwen (qwen_audit role)
            repair_backend=completion_backend,     # generator role (Gemma)
            config=B3AuditRepairConfig(entity_context_enabled=cfg.entity_context_enabled),
        )
        result = b3.run(
            chapter_id=cfg.chapter_id,
            source=source,                          # SourceArtifact
            translation=dict(final_text_by_pid),    # raw generator map
            book_memory=memory.book_memory,
            out_dir=cfg.out_dir,
            config_identity=config.config_identity,
            backend_identity_hash=cfg.backend.identity_hash,
        )

    ``audit_backend`` must serve the ``qwen_audit`` (or ``default``)
    role; ``repair_backend`` the generator role (Kocmi-safe: auditor !=
    repairer). Entity extraction runs on the audit model.
    """

    def __init__(
        self,
        *,
        audit_backend: CompletionBackend,
        repair_backend: Optional[CompletionBackend] = None,
        entity_backend: Optional[CompletionBackend] = None,
        config: Optional[B3AuditRepairConfig] = None,
        progress: Optional[Any] = None,
    ) -> None:
        self._audit_backend = audit_backend
        self._repair_backend = repair_backend or audit_backend
        self._config = config or B3AuditRepairConfig()
        # Entity extraction runs on the audit (Qwen) model; the view adds
        # the missing entity_extractor role without touching backend
        # identity (role resolution only).
        from pact_v4.audit.chunked_audit import audit_model_ref

        entity_ref = audit_model_ref(audit_backend)
        self._entity_backend = entity_backend or _EntityRoleView(
            audit_backend, entity_ref
        )
        self._progress = progress

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------

    def run(
        self,
        *,
        chapter_id: str,
        source: SourceArtifact,
        snapshot_hash: str,
        translation: Mapping[str, str],
        book_memory: Mapping[str, Any],
        glossary: Sequence[Any] = (),
        out_dir: Path,
        config_identity: str,
        backend_identity_hash: str,
        quarantined_pids: Optional[set] = None,
        book_memory_role_views: Optional[Mapping[str, Any]] = None,
    ) -> B3AuditRepairResult:
        cfg = self._config
        cache_path = _audit_cache_path(out_dir)
        journal = AuditJournal(_journal_path(out_dir))
        try:
            return self._run_impl(
                chapter_id=chapter_id,
                source=source,
                snapshot_hash=snapshot_hash,
                translation=translation,
                book_memory=book_memory,
                glossary=glossary,
                out_dir=out_dir,
                config_identity=config_identity,
                backend_identity_hash=backend_identity_hash,
                cache_path=cache_path,
                journal=journal,
                quarantined_pids=quarantined_pids,
                book_memory_role_views=book_memory_role_views,
            )
        finally:
            journal.close()

    def entity_context_prepass(
        self,
        *,
        source: SourceArtifact,
        out_dir: Path,
    ) -> EntityExtractionResult:
        """Run the source-only entity prepass (B1.2), cache-aware.

        P0 owner decision 2026-08-14 (entity_extractor ДО перевода): the
        runner invokes this BEFORE whole-chapter generation so the verified
        claims reach the translator's prompt as the CHAPTER ENTITY FACTS
        block; B3's own ``_run_impl`` step 1 calls the same method again
        AFTER generation, which then hits the persisted
        ``entity_context_cache.json`` (identity = source_hash +
        extractor_version) with 0 extra model calls.

        Fail-closed: a failed extraction raises ``RuntimeError`` (never a
        silent skip). The cache and the validation report are persisted by
        this method, so a resume that skips generation still has the
        validated context for the audit.

        Returns ``None`` when the machinery's own config disables the
        entity context (``entity_context_enabled=False``) — the runner
        then renders the generation prompt without the entity block.
        """
        cfg = self._config
        if not cfg.entity_context_enabled:
            return None
        entity_cache = _load_entity_cache(out_dir)
        try:
            extraction = extract_entity_context(
                source_artifact=source,
                extractor=BackendEntityExtractor(
                    self._entity_backend,
                    config=BackendEntityExtractorConfig(),
                ),
                cache=entity_cache,
                extractor_version=cfg.extractor_version,
                out_dir=out_dir,
            )
        except Exception as exc:  # noqa: BLE001 — fail-closed, never silent skip
            LOG.exception(
                "B3: entity context extraction failed for %s",
                getattr(source, "chapter_id", "?"),
            )
            raise RuntimeError(
                f"B3 entity context extraction failed: {exc}"
            ) from exc
        _save_entity_cache(out_dir, entity_cache)
        # B3-DIAG transparency: what the model proposed vs what the code
        # accepted. A fresh extraction's validation report is persisted next
        # to the cache; a cache hit reuses the previously validated context
        # (validation report empty), so the original report is kept, never
        # overwritten with an empty one.
        if not extraction.from_cache:
            _save_entity_validation_report(
                out_dir, extraction.validation.to_payload()
            )
        return extraction

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _run_russian_editor(
        self,
        *,
        chapter_id: str,
        translation: Mapping[str, str],
        journal: AuditJournal,
        out_dir: Optional[Path],
        cached_chunks: Optional[Mapping[int, Mapping[str, Any]]] = None,
        on_progress: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        book_memory_role_card: Optional[str] = None,
    ) -> Tuple[Dict[str, str], Tuple[ReviewCandidate, ...], Optional[RussianEditorOutcome]]:
        """V4.2 R stage: run the Russian-only editor over the raw map.

        Returns ``(edited_map, review_candidates, outcome)``:
        * edited_map = raw + SAFE edits (diff-gated) from SUCCESSFUL
          chunks, raw when the stage is disabled or failed;
        * review_candidates = REVIEW-classed edits (never auto-applied)
          from SUCCESSFUL chunks, empty when disabled/failed;
        * outcome = the evaluator outcome (None when disabled).

        Partial-apply (RESILIENCE t_406fc48c, run_remote_001): R edits are
        per-chunk independent, so a failed chunk discards ONLY its own
        edits — the successful chunks' edits are applied and their
        candidates forwarded. Fail-closed is preserved PER-CHUNK: the
        evaluator routes no edits for a failed chunk (``outcome.edits`` /
        ``applied`` / ``candidates`` already contain only GOOD-chunk
        content), and the journal records ``r_editor_done`` with
        ``partial=true`` + ``applied_count`` + ``failed_chunks``. The
        failure is debt (journal + outcome), exactly like a failed repair
        batch: the audit still protects the chapter.

        PARTIAL-RESUME (t_a58dd881): ``cached_chunks`` (from the audit
        cache's r_editor report) replays GOOD chunks with 0 model calls —
        their parse-validated edits are re-applied to the raw map
        (idempotent: R applies edits to the RAW map at run time, so a
        replayed GOOD chunk reproduces the exact same edited text); the
        failed chunks are re-run.
        """
        cfg = self._config
        if not cfg.russian_editor_enabled:
            return dict(translation), (), None

        def _journal_r_editor_chunk_event(kind: str, fields: Dict[str, Any]) -> None:
            # A2 (run_011): per-chunk causality in the append-only journal —
            # started BEFORE the model call, terminal done AFTER it, with the
            # FAILED reason (parse/transport) so a failed R chunk is
            # diagnosable even without reading the raw artifact.
            if kind == "started":
                journal.emit(
                    "r_editor_chunk_started",
                    chunk=fields.get("chunk"),
                    total=fields.get("total"),
                )
                self._emit_progress("r_editor_chunk_started", chunk=fields.get("chunk"), total=fields.get("total"))
            elif kind == "retry":
                # R-RETRY (t_8ab8ab35): a bounded retry attempt (transport
                # or invalid JSON/empty body) is journaled so retry
                # causality is visible (acceptance: retry attempts in the
                # journal).
                journal.emit(
                    "r_editor_chunk_retry",
                    chunk=fields.get("chunk"),
                    attempt=fields.get("attempt"),
                    total=fields.get("total"),
                    error=fields.get("error"),
                    delay=fields.get("delay"),
                )
                self._emit_progress("r_editor_chunk_retry", chunk=fields.get("chunk"), attempt=fields.get("attempt"), total=fields.get("total"), error=fields.get("error"), delay=fields.get("delay"))
            else:
                journal.emit(
                    "r_editor_chunk_done",
                    chunk=fields.get("chunk"),
                    total=fields.get("total"),
                    status=fields.get("status"),
                    edit_count=fields.get("edit_count", 0),
                    warning_count=fields.get("warning_count", 0),
                    error=fields.get("error"),
                    # PARTIAL-RESUME: the chunk was replayed from the partial
                    # cache (0 model calls), not freshly edited.
                    reused=fields.get("reused"),
                )
                self._emit_progress("r_editor_chunk_done", chunk=fields.get("chunk"), total=fields.get("total"), status=fields.get("status"), edit_count=fields.get("edit_count", 0), warning_count=fields.get("warning_count", 0), error=fields.get("error"), reused=fields.get("reused"))

        evaluator = RussianEditorEvaluator(
            self._audit_backend,
            config=RussianEditorConfig(
                chunk_size=cfg.russian_editor_chunk_size,
                overlap_pairs=cfg.russian_editor_overlap_pairs,
                max_tokens=cfg.russian_editor_max_tokens,
                safe_classes=cfg.russian_editor_safe_classes,
                harness_version=cfg.russian_editor_harness_version,
                prompt_version=cfg.russian_editor_version,
                max_edits_per_pid=cfg.russian_editor_max_edits_per_pid,
                retry_max_retries=cfg.russian_editor_retry_max_retries,
                retry_base_delay_seconds=cfg.russian_editor_retry_base_delay_seconds,
            ),
            on_chunk_event=_journal_r_editor_chunk_event,
            on_progress=on_progress,
            book_memory_role_card=book_memory_role_card,
        )
        journal.emit(
            "r_editor_started",
            enabled=True,
            chunk_size=cfg.russian_editor_chunk_size,
            overlap_pairs=cfg.russian_editor_overlap_pairs,
            safe_classes=sorted(cfg.russian_editor_safe_classes),
            prompt_version=cfg.russian_editor_version,
        )
        try:
            outcome = evaluator(
                chapter_id=chapter_id, translation=translation,
                out_dir=out_dir, out_base="r_editor",
                cached_chunks=cached_chunks,
            )
        except Exception as exc:  # noqa: BLE001 — R failure is debt, never a crash
            LOG.exception("B3: russian_editor failed for %s", chapter_id)
            journal.emit(
                "r_editor_done",
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            return dict(translation), (), None
        if not outcome.complete:
            # RESILIENCE (t_406fc48c, run_remote_001): partial-apply — R
            # edits are per-chunk independent; a failed chunk discards ONLY
            # its own edits, never the successful chunks' work. The
            # evaluator's outcome.applied/edits/candidates already contain
            # only GOOD-chunk content (fail-closed per chunk), so applying
            # them is safe. run_remote_001: 17 edits from 5 GOOD chunks
            # were dropped because 3 chunks failed (incl. the p00070 typo
            # 'Не важно'→'Неважно').
            edited_map = {**dict(translation), **dict(outcome.applied)}
            LOG.warning(
                "B3: russian_editor partial for %s (failed chunks %s) — "
                "applied %d edit(s) from %d successful chunk(s), failed "
                "chunk edits skipped (per-chunk fail-closed); audit "
                "proceeds on the partially edited map",
                chapter_id, list(outcome.failed_chunks),
                len(outcome.applied), outcome.successful_chunks,
            )
            journal.emit(
                "r_editor_done",
                status="partial",
                partial=True,
                failed_chunks=list(outcome.failed_chunks),
                successful_chunks=outcome.successful_chunks,
                chunk_count=outcome.chunk_count,
                edit_count=len(outcome.edits),
                applied_count=len(outcome.applied),
                candidate_count=len(outcome.candidates),
                dropped=outcome.dropped,
                warning_count=outcome.warning_count,
            )
            self._emit_progress(
                "r_editor_done",
                complete=False,
                partial=True,
                applied_count=len(outcome.applied),
                candidate_count=len(outcome.candidates),
            )
            return edited_map, outcome.candidates, outcome
        edited_map = {**dict(translation), **dict(outcome.applied)}
        journal.emit(
            "r_editor_done",
            status="complete",
            chunk_count=outcome.chunk_count,
            successful_chunks=outcome.successful_chunks,
            edit_count=len(outcome.edits),
            applied_count=len(outcome.applied),
            candidate_count=len(outcome.candidates),
            dropped=outcome.dropped,
            warning_count=outcome.warning_count,
        )
        self._emit_progress(
            "r_editor_done",
            complete=True,
            applied_count=len(outcome.applied),
            candidate_count=len(outcome.candidates),
        )
        return edited_map, outcome.candidates, outcome

    def _run_impl(
        self,
        *,
        chapter_id: str,
        source: SourceArtifact,
        snapshot_hash: str,
        translation: Mapping[str, str],
        book_memory: Mapping[str, Any],
        glossary: Sequence[Any] = (),
        out_dir: Path,
        config_identity: str,
        backend_identity_hash: str,
        cache_path: Path,
        journal: AuditJournal,
        quarantined_pids: Optional[set] = None,
        book_memory_role_views: Optional[Mapping[str, Any]] = None,
    ) -> B3AuditRepairResult:
        cfg = self._config
        source_map = dict(source.source)
        translation_map = dict(translation)
        # v4.2 helper: extract role card as plain text (RenderedContext.text when present)
        def _card_text(key: str) -> Optional[str]:
            if not book_memory_role_views:
                return None
            val = book_memory_role_views.get(key)
            if val is None:
                return None
            if hasattr(val, "text"):
                return val.text  # type: ignore[attr-defined]
            return str(val)
        # The audit outcome is a function of BOTH the source and the
        # translation being audited, so the audit cache identity binds to
        # the exact translation content too — a regenerated/tampered raw
        # map with the same snapshot hash must never be a stale cache hit.
        # V4.2 R: the identity binds the RAW map hash plus the R config
        # keys (config_identity carries russian_editor_version + chunk
        # settings + class threshold), so a cache produced under a different
        # editor policy never replays (F5 lesson) and R itself runs ONLY on
        # a cache miss — a full hit restores the stored R report and the
        # repaired map with 0 model calls (the resume contract).
        translation_hash = canonical_json_hash(dict(sorted(translation_map.items())))
        # F4: exact PID set/order of the RAW map — captured here (before the
        # R stage replaces ``translation_map`` with the edited map) because
        # it is part of the R fallback identity (the standalone
        # r_editor_report.json must be replayable only against the same
        # raw PID coverage) and of the audit-cache repaired-map validation.
        expected_pids = tuple(translation_map)

        # ------------------------------------------------------------------
        # 1. Entity context prepass (B1.2), when enabled.
        # ------------------------------------------------------------------
        entity_context: str = ""
        entity_hash: Optional[str] = None
        entity_payload: Optional[Mapping[str, Any]] = None
        entity_from_cache = False
        if cfg.entity_context_enabled:
            extraction = self.entity_context_prepass(
                source=source, out_dir=out_dir,
            )
            entity_from_cache = extraction.from_cache
            entity_payload = extraction.context.to_payload()
            _audit_card = _card_text("audit_repair")
            entity_hash = canonical_json_hash(entity_payload)
            # When a role card is present, combine its hash into the entity
            # hash so a card change invalidates replay (finding 5). Keep the
            # source-only hash separately for provenance.
            _source_entity_hash = entity_hash
            if _audit_card:
                import hashlib, json as _js2
                _audit_card_hash = hashlib.sha256(_audit_card.encode("utf-8")).hexdigest()
                entity_hash = canonical_json_hash({"source": entity_payload, "role_card_hash": _audit_card_hash})
            entity_context = render_entity_context_block(
                extraction.context,
                role_view_card=_audit_card,
            )
            # Persist role provenance/diagnostics and bind hashes to artifacts.
            if _audit_card and book_memory_role_views is not None:
                _prov = {}
                for _k, _v in book_memory_role_views.items():
                    if hasattr(_v, "canonical_hash"):
                        _prov[_k] = {"hash": _v.canonical_hash, "schema": getattr(_v, "schema_version", "")}
                    elif isinstance(_v, str):
                        _prov[_k] = {"hash": hashlib.sha256(_v.encode("utf-8")).hexdigest()}
                journal.emit("role_view_provenance", provenance=_prov, source_entity_hash=_source_entity_hash, combined_entity_hash=entity_hash)
            journal.emit(
                "entity_context",
                enabled=True,
                from_cache=entity_from_cache,
                entity_count=len(extraction.context.entities),
                entity_context_hash=entity_hash,
            )
            self._emit_progress(
                "entity_context_done",
                from_cache=entity_from_cache,
                entity_count=len(extraction.context.entities),
            )

        # ------------------------------------------------------------------
        # 2. Audit cache: FULL hit -> reuse (0 model calls); PARTIAL hit
        #    (identity matched, audit incomplete) -> GOOD chunks reused,
        #    failed chunks re-run (PARTIAL-RESUME t_a58dd881).
        # ------------------------------------------------------------------
        cache = B3AuditCache.load(
            cache_path,
            snapshot_hash=snapshot_hash,
            translation_hash=translation_hash,
            config_identity=config_identity,
            backend_identity_hash=backend_identity_hash,
            prompt_version=cfg.prompt_version,
            harness_version=cfg.harness_version,
            entity_context_hash=entity_hash,
            entity_context_enabled=cfg.entity_context_enabled,
            # FIX RV2-B (t_a4f8f2b2): the current run's R enablement decides
            # whether a missing stored r_editor is a full miss (R enabled —
            # the report is REQUIRED) or the legitimate disabled null (R
            # disabled, save() writes null).
            r_editor_enabled=cfg.russian_editor_enabled,
            # F4: exact PID set/order validation — a cache whose
            # translations_repaired has missing/extra/reordered PIDs is a miss.
            expected_pids=expected_pids,
            # PARTIAL-RESUME integrity (t_ec6bb8bc): the current (raw)
            # translation text lets the partial-payload validator enforce the
            # verbatim current-text substring constraint on every cached R
            # edit before any resume plan is built.
            current_text=translation_map,
        )
        # PARTIAL-RESUME: build the per-chunk reuse plans from a partial
        # cache (identity matched, audit_complete=False). GOOD audit chunks
        # and GOOD R chunks are replayed with 0 model calls; the failed ones
        # are re-run. The plans are consumed by the R stage and the audit
        # evaluator below.
        partial_cache = (
            cache if (cache is not None and cache.is_hit() and not cache.audit_complete())
            else None
        )
        audit_resume: Dict[int, Dict[str, Any]] = {}
        r_editor_resume: Dict[int, Dict[str, Any]] = {}
        # KILL-SAFE-INCREMENTAL (t_2d16962c): per-batch repair and per-chunk
        # reaudit resume plans from an incremental stage_progress cache —
        # GOOD batches / reaudit chunks are replayed with 0 model calls.
        repair_resume: Optional[Dict[int, Dict[str, Any]]] = None
        reaudit_resume: Dict[int, Dict[str, Any]] = {}
        # PARTIAL-RESUME: PIDs whose edited text changed in the R re-run vs
        # the cached edited map — cached audit chunks over those PIDs are
        # re-audited (per-chunk fail-closed). None when no partial reuse.
        resume_changed_pids: Optional[AbstractSet[str]] = None
        if partial_cache is not None:
            audit_resume = partial_cache.audit_resume_plan()
            r_editor_resume = partial_cache.r_editor_resume_plan()
            repair_resume = partial_cache.repair_resume_plan()
            reaudit_resume = partial_cache.reaudit_resume_plan()
            LOG.info(
                "B3: partial audit cache for %s — reusing %d GOOD audit "
                "chunk(s) + %d GOOD R chunk(s) + %d GOOD repair batch(es) + "
                "%d reaudit chunk(s), re-running the failed ones",
                chapter_id, len(audit_resume), len(r_editor_resume),
                len(repair_resume or {}), len(reaudit_resume),
            )
        elif not r_editor_resume:
            # FAIL-PATH R-CACHE (2026-08-15): no partial audit cache (the
            # previous run died inside the audit with an exception before
            # the cache was written), but a standalone R report survived —
            # reuse its GOOD chunks so R is not re-run from scratch.
            fallback_path = _r_editor_report_path(out_dir)
            if fallback_path.exists():
                try:
                    fallback_report = json.loads(
                        fallback_path.read_text(encoding="utf-8")
                    )
                except (OSError, ValueError) as exc:
                    LOG.warning(
                        "B3: standalone r_editor_report.json unreadable (%s); "
                        "R re-run", exc,
                    )
                else:
                    # REVIEW b55e940 HIGH: the standalone report is reused
                    # ONLY when its persisted identity matches the current
                    # run exactly (raw translation hash, PID set/order, R
                    # harness/prompt versions, config identity, chunking
                    # parameters). A stale report (old harness version,
                    # changed chunk size/text) or a pre-identity report is
                    # rejected fail-closed and R re-runs from scratch —
                    # GOOD chunks computed against different input are never
                    # replayed. Never rely on the first_pid check alone.
                    identity_mismatch = _r_editor_report_identity_mismatch(
                        fallback_report,
                        translation_hash=translation_hash,
                        pids=expected_pids,
                        harness_version=cfg.russian_editor_harness_version,
                        config_identity=config_identity,
                        chunk_size=cfg.russian_editor_chunk_size,
                        overlap_pairs=cfg.russian_editor_overlap_pairs,
                        enabled=cfg.russian_editor_enabled,
                        version=cfg.russian_editor_version,
                        safe_classes=cfg.russian_editor_safe_classes,
                    )
                    if identity_mismatch is not None:
                        LOG.warning(
                            "B3: standalone r_editor_report.json NOT reused "
                            "(%s); R re-run from scratch",
                            identity_mismatch,
                        )
                    else:
                        r_editor_resume = _r_editor_resume_plan_from_report(
                            fallback_report
                        )
                        if r_editor_resume:
                            LOG.info(
                                "B3: reusing %d GOOD R chunk(s) from the "
                                "standalone r_editor_report.json (audit cache "
                                "absent, identity verified) — R not re-run",
                                len(r_editor_resume),
                            )
        if cache is not None and cache.is_hit() and cache.audit_complete():
            repaired = cache.stored_translations_repaired()
            issues = cache.stored_issues()
            filtered = cache.stored_filtered()
            repair_payload = cache.stored_repair()
            step6, step7, step8 = _reports_from_cache(
                cache=cache, issue_count=len(issues),
            )
            cache_repair_complete = bool(
                repair_payload and repair_payload.get("repair_complete") is True
            )
            LOG.info(
                "B3: audit cache full hit for %s (0 model calls)", chapter_id
            )
            journal.emit(
                "audit_started",
                snapshot_hash=snapshot_hash,
                entity_context_enabled=cfg.entity_context_enabled,
                entity_context_hash=entity_hash,
                prompt_version=cfg.prompt_version,
                harness_version=cfg.harness_version,
            )
            journal.emit(
                "audit_complete",
                audit_complete=True,
                issue_count=len(issues),
                from_cache=True,
            )
            journal.emit(
                "gate",
                audit_complete=True,
                # F1: a cached repair with repair_complete=False (failed
                # batch / failed re-audit) is replayed as NOT released — the
                # cache hit must never upgrade debt into an audited release.
                released_as_audited=cache_repair_complete,
                repair_complete=cache_repair_complete,
                from_cache=True,
            )
            # Glossary resolver unified post-processing on cache hit (0 calls when valid sidecar)
            _translations_for_glossary = repaired if repaired is not None else dict(translation_map)
            self._handle_glossary_resolver(
                chapter_id=chapter_id,
                source=source,
                snapshot_hash=snapshot_hash,
                config_identity=config_identity,
                backend_identity_hash=backend_identity_hash,
                translations_repaired=_translations_for_glossary,
                out_dir=out_dir,
                journal=journal,
                is_cache_hit=True,
                entity_context_payload=entity_payload,
                quarantined_pids=quarantined_pids,
                glossary=glossary,
                book_memory_role_views=book_memory_role_views,
            )
            return B3AuditRepairResult(
                step6=step6,
                step7=step7,
                step8=step8,
                translations_repaired=(
                    repaired if repaired is not None else dict(translation_map)
                ),
                audit_complete=True,
                from_cache=True,
                entity_context_hash=cache.entity_context_hash(),
                audit_cache_path=cache_path,
                journal_path=journal.path,
                # V4.2 R: restore the Russian-editor report from the cache so
                # the trial record shows the original R outcome (candidates +
                # accept/reject journal) even on a 0-call replay.
                r_editor=cache.stored_r_editor(),
            )

        journal.emit(
            "audit_started",
            snapshot_hash=snapshot_hash,
            entity_context_enabled=cfg.entity_context_enabled,
            entity_context_hash=entity_hash,
            prompt_version=cfg.prompt_version,
            harness_version=cfg.harness_version,
            # PARTIAL-RESUME (t_a58dd881): the audit started from a partial
            # cache — GOOD chunks replayed, failed chunks re-run.
            partial_resume=bool(audit_resume or r_editor_resume),
            reused_audit_chunks=sorted(audit_resume),
            reused_r_editor_chunks=sorted(r_editor_resume),
        )

        # ------------------------------------------------------------------
        # 2.5 V4.2 R: Russian-only editor stage (card t_4707e6e5). Runs on a
        #     cache MISS only (a full hit restores the stored R report with
        #     0 model calls). The R config participates in the config
        #     identity, so a policy change invalidates the repaired cache
        #     (F5 lesson) and the audit below runs on the R-EDITED map.
        # ------------------------------------------------------------------
        r_editor_outcome: Optional[RussianEditorOutcome] = None
        review_candidates: Tuple[ReviewCandidate, ...] = ()
        r_editor_report: Optional[Dict[str, Any]] = None
        # KILL-SAFE-INCREMENTAL (t_2d16962c): accumulated stage state. Every
        # stage callback mutates it and rewrites audit_cache_b3.json via
        # _save_stage_progress(), so a kill at ANY point preserves every
        # completed chunk/batch (R GOOD chunks, audit GOOD chunks, repair
        # committed/passed batches, reaudit residual issues).
        stage_progress = {
            "r_editor": {
                "status": "disabled" if not cfg.russian_editor_enabled else "pending",
                "enabled": cfg.russian_editor_enabled,
                "done_chunks": [],
                "failed_chunks": [],
                "outcome": None,
            },
            "audit": {
                "status": "pending", "done_chunks": [], "failed_chunks": [],
                "chunks": [], "issues": [],
            },
            "repair": {
                "status": "pending", "done_batches": [], "committed": {},
                "passed": [], "outcome": None,
            },
            "reaudit": {
                "status": "pending", "done_chunks": [], "issues": [],
            },
        }

        def _save_stage_progress() -> None:
            """Rewrite audit_cache_b3.json from the accumulated stage state.

            The payload keeps the identity fields + translations_repaired so
            load()'s identity / F4 checks pass on resume; the accumulated
            per-stage slices live in ``stage_progress`` (validated by
            _validate_stage_progress on load, fail-closed).
            """
            cache_writer = B3AuditCache(cache_path)
            cache_writer.save(
                snapshot_hash=snapshot_hash,
                translation_hash=translation_hash,
                config_identity=config_identity,
                backend_identity_hash=backend_identity_hash,
                entity_context_hash=entity_hash,
                entity_context_enabled=cfg.entity_context_enabled,
                outcome=None,
                translations_repaired=dict(translation_map),
                r_editor=None,
                stage_progress=stage_progress,
                prompt_version=cfg.prompt_version,
                harness_version=cfg.harness_version,
            )

        def _on_r_editor_progress(kind: str, fields: Dict[str, Any]) -> None:
            if kind != "chunk_done" or not cfg.russian_editor_enabled:
                return
            chunks = fields["chunks"]
            failed = list(fields["failed_chunks"])
            done = [c["chunk"] for c in chunks]
            # Status mirrors _build_r_editor_report: all chunks done ->
            # complete (no failed) / incomplete (some failed); else partial.
            if len(done) == fields["chunk_count"]:
                status = "complete" if not failed else "incomplete"
            else:
                status = "partial"
            stage_progress["r_editor"] = {
                "status": status,
                "enabled": True,
                "done_chunks": done,
                "failed_chunks": failed,
                "outcome": {
                    "chunk_size": cfg.russian_editor_chunk_size,
                    "chunks": [dict(c) for c in chunks],
                },
            }
            _save_stage_progress()

        _r_editor_card = _card_text("russian_editor")
        if cfg.russian_editor_enabled:
            edited_map, review_candidates, r_editor_outcome = (
                self._run_russian_editor(
                    chapter_id=chapter_id,
                    translation=translation_map,
                    journal=journal,
                    out_dir=out_dir,
                    cached_chunks=r_editor_resume or None,
                    on_progress=_on_r_editor_progress,
                    book_memory_role_card=_r_editor_card,
                )
            )
            # The audit/repair consume the R-EDITED map (raw + SAFE edits).
            translation_map = edited_map
            # KILL-SAFE-INCREMENTAL (t_2d16962c): when the R stage FAILED
            # (evaluator exception), the stage_progress must carry a failed
            # status with NO outcome — an enabled stage that already ran can
            # never be "pending", and a failed stage has nothing replayable
            # (mirrors the top-level report: status "failed", outcome null).
            if r_editor_outcome is None:
                stage_progress["r_editor"] = {
                    "status": "failed",
                    "enabled": True,
                    "done_chunks": [],
                    "failed_chunks": [],
                    "outcome": None,
                }
            r_editor_report = _build_r_editor_report(
                cfg=cfg,
                outcome=r_editor_outcome,
                review_journal=(),
                from_cache=False,
                # R fallback identity (review b55e940 HIGH): the report is
                # bound to the RAW map (translation_hash + expected_pids
                # captured before the R stage replaced translation_map) and
                # the run config identity — the standalone fail-path
                # fallback rejects a report that does not match the current
                # run exactly.
                translation_hash=translation_hash,
                pids=expected_pids,
                config_identity=config_identity,
            )
            # PARTIAL-RESUME fail-closed guard: the cached audit chunks were
            # computed on the R-EDITED map of the ORIGINAL run (the partial
            # cache's translations_repaired IS that edited map). If the R
            # re-run of previously-failed chunks changed the edited text, the
            # cached audit chunks that CONTAIN those PIDs are stale — the
            # evaluator refuses to replay a cached chunk whose own pids
            # intersect ``changed_pids`` (re-audits it) while chunks over
            # untouched PIDs are still replayed (per-chunk fail-closed, the
            # owner's "не пересматриваем GOOD" with input-identity honesty).
            # A missing cached edited map (legacy/tampered cache) drops the
            # whole reuse plan.
            if audit_resume:
                cached_edited = partial_cache.stored_translations_repaired()
                new_edited = dict(translation_map)
                if cached_edited is None:
                    LOG.warning(
                        "B3: partial audit cache for %s — no cached edited "
                        "map to verify audit input; full re-audit (fail-closed)",
                        chapter_id,
                    )
                    audit_resume = {}
                else:
                    resume_changed_pids = {
                        pid for pid, text in new_edited.items()
                        if cached_edited.get(pid) != text
                    }
                    if resume_changed_pids:
                        LOG.warning(
                            "B3: partial audit cache for %s — R re-run "
                            "changed %d PID(s); cached audit chunks over "
                            "those PIDs will be re-audited (per-chunk "
                            "fail-closed)",
                            chapter_id, len(resume_changed_pids),
                        )
            # Artifacts are written when the R stage produced ANY edited map
            # (complete pass, or a partial pass whose successful chunks
            # applied edits / forwarded candidates) — the audit/repair
            # consume that exact map, so translations_edited.json must
            # reflect it (F8: never advertise provenance the stage did not
            # produce; a fully-failed pass — 0 successful chunks — leaves
            # no artifacts and the audit runs on the raw map).
            if r_editor_outcome is not None and (
                r_editor_outcome.complete
                or r_editor_outcome.applied
                or r_editor_outcome.candidates
            ):
                _write_r_editor_artifacts(
                    cfg=cfg,
                    chapter_id=chapter_id,
                    snapshot_hash=snapshot_hash,
                    config_identity=config_identity,
                    out_dir=out_dir,
                    edited_map=translation_map,
                    candidates=review_candidates,
                )

        # ------------------------------------------------------------------
        # 3. Chunked audit (B1).
        # ------------------------------------------------------------------
        # F2 (RV2): the audit stage is wrapped so a pre/model-call evaluator
        # failure (CoverageError/empty input, BudgetOverflowError, missing
        # role, …) writes a TERMINAL audit failure event and a fail-closed
        # gate into the append-only journal BEFORE the exception propagates
        # to the strict runner — the journal must never end on
        # audit_started/started alone. Per-chunk TRANSPORT_ERROR failures
        # are handled inside the evaluator (failed chunks -> audit_complete
        # False -> the fail-closed gate below); that transport path is
        # preserved unchanged.
        try:
            pairs = pairs_from_maps(source_map, translation_map)
            narrator_context = build_narrator_context(
                book_memory, " ".join(source_map.values())
            )
            def _journal_chunk_event(kind: str, fields: Dict[str, Any]) -> None:
                # F7: journal causality — the started event is emitted BEFORE the
                # model call (inside ChunkedAuditEvaluator._run_one_chunk) and the
                # terminal done/failed after it, so a crash during a chunk leaves
                # item-start evidence in the append-only journal instead of nothing.
                if kind == "started":
                    journal.emit(
                        "audit_chunk_started",
                        chunk=fields.get("chunk"),
                        total=fields.get("total"),
                        sub=fields.get("sub") or "",
                    )
                    self._emit_progress("audit_chunk_started", chunk=fields.get("chunk"), total=fields.get("total"), sub=fields.get("sub") or "")
                elif kind == "retry":
                    # R-RETRY (t_8ab8ab35): a TRANSPORT_ERROR chunk is retried
                    # with a NEW session — the retry attempt is journaled so
                    # the operator sees the transport blip and its backoff.
                    journal.emit(
                        "audit_chunk_retry",
                        chunk=fields.get("chunk"),
                        total=fields.get("total"),
                        attempt=fields.get("attempt"),
                        error=fields.get("error"),
                        delay=fields.get("delay"),
                    )
                    self._emit_progress("audit_chunk_retry", chunk=fields.get("chunk"), total=fields.get("total"), attempt=fields.get("attempt"), error=fields.get("error"), delay=fields.get("delay"))
                else:
                    journal.emit(
                        "audit_chunk_done",
                        chunk=fields.get("chunk"),
                        total=fields.get("total"),
                        status=fields.get("status"),
                        issue_count=fields.get("issue_count", 0),
                        # CONTEXT-PID-DROP (owner 2026-08-15): issues dropped
                        # for context-only/foreign pids are journaled as a
                        # warning count for diagnostics — the chunk itself
                        # stays GOOD (mirrors R-PID-SCOPE warning_count).
                        dropped_count=fields.get("dropped_count", 0),
                        error=fields.get("error"),
                        # PARTIAL-RESUME: the chunk was replayed from the
                        # partial cache (0 model calls), not freshly audited.
                        reused=fields.get("reused"),
                    )
                    self._emit_progress("audit_chunk_done", chunk=fields.get("chunk"), total=fields.get("total"), status=fields.get("status"), issue_count=fields.get("issue_count", 0), dropped_count=fields.get("dropped_count", 0), error=fields.get("error"), reused=fields.get("reused"))

            def _on_audit_progress(kind: str, fields: Dict[str, Any]) -> None:
                if kind != "chunk_done":
                    return
                chunks = fields["chunks"]
                issues = fields["issues"]
                done = [c["chunk"] for c in chunks]
                failed = fields["failed_chunks"]
                stage_progress["audit"] = {
                    "status": (
                        "complete" if len(done) == fields["chunk_count"] else "partial"
                    ),
                    "done_chunks": done,
                    "failed_chunks": list(failed),
                    "chunks": [dict(c) for c in chunks],
                    "issues": [dict(i) for i in issues],
                }
                _save_stage_progress()

            evaluator = ChunkedAuditEvaluator(
                self._audit_backend,
                config=ChunkedAuditConfig(
                    max_input_tokens=cfg.max_input_tokens,
                    max_tokens=cfg.max_tokens,
                    overlap_tokens=cfg.overlap_tokens,
                    reasoning_budget=cfg.reasoning_budget,
                    harness_version=cfg.harness_version,
                    prompt_version=cfg.prompt_version,
                    # R-RETRY (t_8ab8ab35, F5): the chunk-level TRANSPORT_ERROR
                    # retry policy is wired from the B3 config (identity
                    # carries it) — never silently left at module defaults.
                    transport_max_retries=cfg.audit_transport_max_retries,
                    transport_base_delay_seconds=cfg.audit_transport_base_delay_seconds,
                ),
                on_chunk_event=_journal_chunk_event,
                on_progress=_on_audit_progress,
            )
            outcome = evaluator(
                chapter_id=chapter_id,
                pairs=pairs,
                narrator_context=narrator_context,
                entity_context=entity_context,
                out_dir=out_dir,
                out_base="b3_audit",
                # PARTIAL-RESUME: replay GOOD cached chunks (0 model calls);
                # chunks whose pids changed in the R re-run are re-audited.
                cached_chunks=audit_resume or None,
                resume_changed_pids=resume_changed_pids,
            )
        except Exception as exc:  # noqa: BLE001 — fail-closed: a pre/model-call
            # audit failure is TERMINAL. The strict runner records the failed
            # step fields, but the B3 journal must carry its own terminal
            # failure event + fail-closed gate/provenance too — recorded
            # BEFORE the exception is re-raised.
            LOG.exception("B3: chunked audit evaluator failed for %s", chapter_id)
            error = f"{type(exc).__name__}: {exc}"
            journal.emit(
                "audit_failed",
                error=error,
                audit_complete=False,
            )
            journal.emit(
                "gate",
                audit_complete=False,
                released_as_audited=False,
                error=error,
            )
            # FAIL-PATH R-CACHE (architect, run_0004-0005 2026-08-15): an
            # audit exception (e.g. BudgetOverflowError) previously dropped
            # the already-completed R report — save() only ran on the
            # normal fail-closed path, so a resume re-ran R from scratch.
            # Persist the R report to its own artifact here so the next
            # resume reuses GOOD R chunks (r_editor_resume fallback in the
            # resume section below) even though the audit cache was never
            # written.
            if r_editor_report is not None:
                _atomic_write_json(
                    _r_editor_report_path(out_dir),
                    dict(r_editor_report),
                )
            raise
        journal.emit(
            "audit_complete",
            audit_complete=outcome.audit_complete,
            issue_count=outcome.issue_count,
            failed_chunks=list(outcome.failed_chunks),
            from_cache=False,
            # PARTIAL-RESUME (t_a58dd881): how many GOOD chunks were replayed
            # from the partial cache (0 model calls for them).
            reused_chunks=sorted(audit_resume) if audit_resume else [],
        )
        self._emit_progress(
            "b3_audit_done",
            audit_complete=outcome.audit_complete,
            issue_count=outcome.issue_count,
            chunk_count=outcome.chunk_count,
            successful_chunks=outcome.successful_chunks,
            failed_chunks=list(outcome.failed_chunks),
        )

        # ------------------------------------------------------------------
        # 4. Gate: incomplete audit -> fail-closed (never repair, never
        #    release the chapter as passed audit).
        # ------------------------------------------------------------------
        if not outcome.audit_complete:
            LOG.error(
                "B3: audit incomplete for %s (failed chunks %s) — "
                "fail-closed: chapter is NOT released as passed audit",
                chapter_id, list(outcome.failed_chunks),
            )
            cache_writer = B3AuditCache(cache_path)
            cache_writer.save(
                snapshot_hash=snapshot_hash,
                translation_hash=translation_hash,
                config_identity=config_identity,
                backend_identity_hash=backend_identity_hash,
                entity_context_hash=entity_hash,
                entity_context_enabled=cfg.entity_context_enabled,
                outcome=outcome,
                filtered=(),
                repair=None,
                translations_repaired=dict(translation_map),
                # PARTIAL-RESUME (t_a58dd881): the R report (with per-chunk
                # statuses + parse-validated edits) rides the partial cache so
                # the NEXT resume reuses GOOD R chunks too (previously it was
                # dropped on the fail-closed path — r_editor stayed None and
                # run_remote_007-style caches lost all R reuse data).
                r_editor=r_editor_report,
            )
            step6 = {
                "status": "incomplete",
                "audit_complete": False,
                "chunk_count": outcome.chunk_count,
                "successful_chunks": outcome.successful_chunks,
                "failed_chunks": list(outcome.failed_chunks),
                "issue_count": outcome.issue_count,
                "entity_context_enabled": cfg.entity_context_enabled,
                "entity_context_hash": entity_hash,
                "from_cache": False,
                # PARTIAL-RESUME (t_a58dd881): GOOD chunks were replayed from
                # the partial cache (0 model calls); only the failed ones were
                # re-run (and failed again — fail-closed preserved).
                "partial_resume": bool(audit_resume),
                "reused_chunks": sorted(audit_resume) if audit_resume else [],
            }
            step7 = {"status": "skipped", "reason": "audit_incomplete_fail_closed"}
            step8 = {
                "status": "fail_closed_audit_incomplete",
                "audit_complete": False,
                "released_as_audited": False,
            }
            journal.emit(
                "gate",
                audit_complete=False,
                released_as_audited=False,
                failed_chunks=list(outcome.failed_chunks),
            )
            return B3AuditRepairResult(
                step6=step6,
                step7=step7,
                step8=step8,
                translations_repaired=dict(translation_map),
                audit_complete=False,
                from_cache=False,
                entity_context_hash=entity_hash,
                audit_cache_path=cache_path,
                journal_path=journal.path,
                # The R stage already ran (edits recorded) but the audit is
                # fail-closed — the R report is still surfaced for the trial
                # record (the chapter is not released as audited).
                r_editor=r_editor_report,
            )

        # ------------------------------------------------------------------
        # 5. Hard filters (B1.1) — entity-PID issues are forced TIER_B.
        # ------------------------------------------------------------------
        filtered = apply_hard_filters(
            outcome.issues,
            source=source_map,
            translation=translation_map,
            entity_context=entity_payload,
        )
        for f in filtered:
            journal.emit(
                "finding",
                pid=str(f.issue.get("id", "")),
                category=str(f.issue.get("category", "")),
                severity=str(f.issue.get("severity", "")),
                confidence=str(f.issue.get("confidence", "")),
                verdict=f.verdict,
                filter_name=f.filter_name,
                reason=f.reason,
            )

        # ------------------------------------------------------------------
        # 6. Selective repair (B2) + single re-audit of changed PIDs.
        # ------------------------------------------------------------------
        repair_outcome: Optional[SelectiveRepairOutcome] = None
        try:
            # KILL-SAFE-INCREMENTAL (t_2d16962c): per-batch repair progress —
            # after every batch (and every reaudit chunk) the accumulated
            # state is rewritten to the audit cache so a kill preserves every
            # committed/passed batch and every reaudit chunk.
            def _on_repair_progress(kind: str, fields: Dict[str, Any]) -> None:
                if kind == "batch_done":
                    batches = fields["batches"]
                    done_batches = [
                        b["batch_index"] for b in batches if b.get("status") == "GOOD"
                    ]
                    stage_progress["repair"] = {
                        "status": "partial",
                        "done_batches": done_batches,
                        "committed": dict(fields.get("committed") or ()),
                        "passed": list(fields.get("passed_pids") or ()),
                        "outcome": {
                            "batches": [dict(b) for b in batches],
                            "batch_count": fields.get("batch_count", len(batches)),
                        },
                    }
                    _save_stage_progress()
                elif kind == "reaudit_chunk_done":
                    done_chunks = fields["done_chunks"]
                    # CONTEXT-PID-DROP (RV5 t_f82ed9ad): a done record
                    # marked failed means the stage is NOT complete — the
                    # persisted status must say "failed" (a failed chunk can
                    # never be represented as a successful complete stage).
                    stage_status = (
                        "failed"
                        if any(c.get("failed") is True for c in done_chunks)
                        else (
                            "complete"
                            if len(done_chunks) == fields.get("chunk_count")
                            else "partial"
                        )
                    )
                    stage_progress["reaudit"] = {
                        "status": stage_status,
                        "done_chunks": [dict(c) for c in done_chunks],
                        "issues": [
                            dict(i)
                            for c in done_chunks
                            for i in (c.get("issues") or ())
                        ],
                    }
                    _save_stage_progress()

            repair_evaluator = SelectiveRepairEvaluator(
                self._repair_backend,
                reaudit_backend=self._audit_backend,
                config=SelectiveRepairConfig(
                    findings_cap=cfg.repair_findings_cap,
                    microbatch_trigger=cfg.repair_microbatch_trigger,
                    microbatch_target=cfg.repair_microbatch_target,
                    # REPAIR-MAX-TOKENS (owner decision 2026-08-15): wired
                    # from the B3 config (identity carries it) — the repair
                    # output budget is never a silent module default.
                    max_tokens=cfg.repair_max_tokens,
                    # REPAIR-ROBUST (t_b6fd6cbd, F5): the repair reasoning
                    # effort is wired from the B3 config (identity carries
                    # it) — the evaluator can never silently fall back to a
                    # module default, and a reasoning change invalidates the
                    # repaired map cache.
                    repair_reasoning=cfg.repair_reasoning,
                    # CANDIDATE-MERGE (t_0ffe56e1, RV2 HIGH finding, F5): the
                    # REPAIR prompt/harness version is wired from the B3
                    # config (the run identity carries it) — the evaluator
                    # can never silently fall back to a stale module default,
                    # and a cache written under a different repair prompt
                    # must never replay the repaired map.
                    prompt_version=cfg.repair_prompt_version,
                    harness_version=cfg.repair_harness_version,
                    # REPAIR-CTX (t_97b31f81, F5): the local-context window is
                    # wired from the B3 config (identity carries it), so the
                    # evaluator can never silently fall back to module
                    # defaults — a window change invalidates the repaired map
                    # cache.
                    repair_context_window=cfg.repair_context_window,
                    # REPAIR-2 (t_768537b9, F5): the per-category window
                    # overrides are wired from the B3 config (identity
                    # carries them), so the evaluator can never silently fall
                    # back to module defaults — a per-category window change
                    # invalidates the repaired map cache.
                    repair_context_window_by_category=dict(
                        cfg.repair_context_window_by_category
                    ),
                    reaudit_neighbour_window=cfg.repair_reaudit_neighbour_window,
                    # REPAIR-CTX (t_97b31f81, F5): the re-audit chunk/overlap
                    # settings and the REPAIRED CHANGES delta format are
                    # wired from the B3 config (identity carries them), so the
                    # evaluator can never silently fall back to module
                    # defaults — a chunk/delta change invalidates the repaired
                    # map cache.
                    reaudit_max_input_tokens=cfg.repair_reaudit_max_input_tokens,
                    reaudit_overlap_tokens=cfg.repair_reaudit_overlap_tokens,
                    reaudit_min_overlap_pairs=cfg.repair_reaudit_min_overlap_pairs,
                    reaudit_max_overlap_pairs=cfg.repair_reaudit_max_overlap_pairs,
                    reaudit_delta_format=cfg.repair_reaudit_delta_format,
                    # RV 71b7cbc fix (F5): the re-audit output budget and the
                    # bounded B4 JSON retry policy are wired from the B3
                    # config — the identity carries them, so the evaluator
                    # can never silently fall back to module defaults.
                    reaudit_max_tokens=cfg.repair_reaudit_max_tokens,
                    reaudit_retry=JsonRetryPolicy(
                        max_retries=cfg.repair_reaudit_max_retries,
                        base_delay_seconds=cfg.repair_reaudit_base_delay_seconds,
                    ),
                ),
                on_progress=_on_repair_progress,
            )
            repair_outcome = repair_evaluator(
                chapter_id=chapter_id,
                source=source_map,
                translation=translation_map,
                filtered=filtered,
                entity_context=entity_context,
                narrator_context=narrator_context,
                glossary=glossary,
                # V4.2 R: REVIEW-classed Russian-editor candidates are
                # additional verify-before-repair input — the verifier
                # accepts/rejects each against the ORIGINAL; accepted ones
                # are committed and covered by the re-audit.
                review_candidates=review_candidates,
                # B1/C1 (run_011): persist repair-batch + re-audit raw and
                # reasoning artifacts next to the audit cache.
                out_dir=out_dir,
                out_base="b3_repair",
                # KILL-SAFE-INCREMENTAL (t_2d16962c): replay GOOD batches /
                # reaudit chunks from an incremental partial cache (0 model
                # calls); the missing ones are re-run.
                cached_batches=repair_resume,
                cached_reaudit_chunks=reaudit_resume or None,
            )
        except Exception as exc:  # noqa: BLE001 — a repair failure is debt, never a crash
            LOG.exception("B3: selective repair failed for %s", chapter_id)
            repair_outcome = None
            # Fall through: the cache is written with repair=None and the
            # gate stays honest (repair_complete=False).

        if repair_outcome is not None:
            journal.emit(
                "repair_round",
                round=1,
                eligible_count=repair_outcome.eligible_count,
                committed_pids=[pid for pid, _ in repair_outcome.committed],
                passed_pids=list(repair_outcome.passed_pids),
                debt_trace=list(repair_outcome.debt_trace),
                # REPAIR-2 (t_768537b9): per-index non-fatal notices (no-op
                # repairs converted to per-index pass) — journaled with the
                # round so the operator sees the model misused the decision
                # contract without losing the batch's real repairs.
                warnings=list(repair_outcome.warnings),
                repair_complete=repair_outcome.repair_complete,
                skipped=repair_outcome.skipped,
            )
            self._emit_progress(
                "repair_round",
                round=1,
                eligible_count=repair_outcome.eligible_count,
                committed_pids=[pid for pid, _ in repair_outcome.committed],
                passed_pids=list(repair_outcome.passed_pids),
                debt_trace=list(repair_outcome.debt_trace),
                warnings=list(repair_outcome.warnings),
                repair_complete=repair_outcome.repair_complete,
                batches=[
                    {
                        "batch_index": b.batch_index,
                        "status": b.status,
                        "findings": [{"index": f.index, "pid": f.pid} for f in b.findings],
                        "results": [{"index": r.index, "decision": r.decision, "pid": r.pid} for r in b.results],
                    }
                    for b in repair_outcome.batches
                ],
                batch_count=len(repair_outcome.batches),
                batches_done=len(repair_outcome.batches),
                batches_total=len(repair_outcome.batches),
            )
            reaudit = repair_outcome.reaudit
            if reaudit is not None:
                journal.emit(
                    "reaudit_scope",
                    scope_pids=list(reaudit.scope),
                    full=reaudit.full,
                    issue_count=len(reaudit.issues),
                    issues=[dict(i) for i in reaudit.issues],
                    failed=reaudit.failed,
                    complete=reaudit.complete,
                )
                self._emit_progress(
                    "reaudit_scope",
                    scope_pids=list(reaudit.scope),
                    full=reaudit.full,
                    issue_count=len(reaudit.issues),
                    issues=[dict(i) for i in reaudit.issues],
                    failed=reaudit.failed,
                    complete=reaudit.complete,
                )

        committed = (
            {pid: text for pid, text in repair_outcome.committed}
            if repair_outcome is not None
            else {}
        )
        translations_repaired = {**translation_map, **committed}
        # Glossary resolver unified post-processing (fresh path)
        self._handle_glossary_resolver(
            chapter_id=chapter_id,
            source=source,
            snapshot_hash=snapshot_hash,
            config_identity=config_identity,
            backend_identity_hash=backend_identity_hash,
            translations_repaired=translations_repaired,
            out_dir=out_dir,
            journal=journal,
            is_cache_hit=False,
            entity_context_payload=entity_payload,
            quarantined_pids=quarantined_pids,
            glossary=glossary,
            book_memory_role_views=book_memory_role_views,
        )
        repair_complete = (
            repair_outcome.repair_complete if repair_outcome is not None else False
        )
        # V4.2 R: fold the accept/reject journal into the R report so the
        # trial record and the cached report carry it (on a full cache hit
        # the journal is restored verbatim from the cache payload).
        if r_editor_report is not None:
            review_journal = (
                repair_outcome.review_journal if repair_outcome is not None else ()
            )
            r_editor_report["review_journal"] = [
                dict(entry) for entry in review_journal
            ]

        # ------------------------------------------------------------------
        # 7. Persist cache + build reports.
        # ------------------------------------------------------------------
        cache_writer = B3AuditCache(cache_path)
        cache_writer.save(
            snapshot_hash=snapshot_hash,
            translation_hash=translation_hash,
            config_identity=config_identity,
            backend_identity_hash=backend_identity_hash,
            entity_context_hash=entity_hash,
            entity_context_enabled=cfg.entity_context_enabled,
            outcome=outcome,
            filtered=filtered,
            repair=repair_outcome,
            translations_repaired=translations_repaired,
            r_editor=r_editor_report,
        )

        step6 = {
            "status": "complete",
            "audit_complete": True,
            "chunk_count": outcome.chunk_count,
            "successful_chunks": outcome.successful_chunks,
            "failed_chunks": list(outcome.failed_chunks),
            "issue_count": outcome.issue_count,
            "entity_context_enabled": cfg.entity_context_enabled,
            "entity_context_hash": entity_hash,
            "from_cache": False,
            # PARTIAL-RESUME (t_a58dd881): GOOD chunks were replayed from the
            # partial cache (0 model calls); only the failed ones re-run.
            "partial_resume": bool(audit_resume),
            "reused_chunks": sorted(audit_resume) if audit_resume else [],
        }
        step7 = {
            "status": (
                "complete" if repair_complete else (
                    "failed" if repair_outcome is None else "incomplete"
                )
            ),
            "repair_complete": repair_complete,
            "eligible_count": (
                repair_outcome.eligible_count if repair_outcome is not None else 0
            ),
            "committed_pids": [pid for pid, _ in committed.items()],
            "passed_pids": (
                list(repair_outcome.passed_pids) if repair_outcome is not None else []
            ),
            "debt_trace": (
                list(repair_outcome.debt_trace) if repair_outcome is not None else []
            ),
            # REPAIR-2 (t_768537b9): per-index non-fatal notices (no-op
            # repairs converted to per-index pass) ride the repair record so
            # the operator can see them in step7 / the trial record.
            "warnings": (
                list(repair_outcome.warnings) if repair_outcome is not None else []
            ),
        }
        # F1 (B3 review): the terminal gate is honest about repair debt. The
        # chapter is released as audited ONLY when the audit completed AND
        # the repair completed (every batch GOOD and the post-repair re-audit
        # succeeded). repair_complete=False (failed batch / failed re-audit /
        # repair exception) degrades the release to accepted_degraded with
        # released_as_audited=False — never a silent complete/PASS.
        if repair_complete:
            step8 = {
                "status": "complete",
                "audit_complete": True,
                "released_as_audited": True,
            }
        else:
            step8 = {
                "status": "accepted_degraded",
                "audit_complete": True,
                "released_as_audited": False,
                "repair_complete": False,
                "reason": (
                    "repair_failed" if repair_outcome is None else "repair_incomplete"
                ),
                "debt_trace": step7["debt_trace"],
            }
        journal.emit(
            "gate",
            audit_complete=True,
            released_as_audited=repair_complete,
            repair_complete=repair_complete,
        )
        self._emit_progress(
            "b3_repair_done",
            repair_complete=repair_complete,
            committed_pids=[pid for pid, _ in committed.items()],
        )

        return B3AuditRepairResult(
            step6=step6,
            step7=step7,
            step8=step8,
            translations_repaired=translations_repaired,
            audit_complete=True,
            from_cache=False,
            entity_context_hash=entity_hash,
            audit_cache_path=cache_path,
            journal_path=journal.path,
            r_editor=r_editor_report,
        )

    def _handle_glossary_resolver(
        self,
        *,
        chapter_id: str,
        source: Any,
        snapshot_hash: str,
        config_identity: str,
        backend_identity_hash: str,
        translations_repaired: Any,
        out_dir: Any,
        journal: Any,
        is_cache_hit: bool = False,
        entity_context_payload: Any = None,
        quarantined_pids: Any = None,
        glossary: Any = (),
        book_memory_role_views: Optional[Mapping[str, Any]] = None,
    ) -> None:
        cfg = self._config
        mode = getattr(cfg, "glossary_resolver_mode", "off")
        policy = getattr(cfg, "glossary_resolver_cache_miss_policy", "recompute")
        if mode == "off":
            LOG.info("glossary_resolver: mode off for %s", chapter_id)
            journal.emit("glossary_resolver", mode="off", status="skipped", reason="mode_off")
            self._emit_progress("glossary_resolver", mode="off", status="skipped")
            return
        # Load entity records
        entity_records = []
        source_map = dict(source.source) if hasattr(source, "source") else {}
        if entity_context_payload and isinstance(entity_context_payload, dict):
            try:
                from pact_v4.audit.entity_extractor import ChapterEntityContext, is_entity_glossary_candidate
                ctx = ChapterEntityContext.from_payload(entity_context_payload)
                for rec in ctx.entities:
                    if rec.anchor.status != "verified":
                        continue
                    if is_entity_glossary_candidate(rec, source_map):
                        entity_records.append(rec)
            except Exception as exc:
                LOG.warning("glossary_resolver: entity payload parse failed %s: %s", chapter_id, exc)
                entity_records = []
        else:
            try:
                from pact_v4.audit.entity_extractor import ChapterEntityContext, EntityContextCache, is_entity_glossary_candidate, entity_context_cache_key
                import json as _js
                cache_path = out_dir / "entity_context_cache.json"
                if cache_path.exists():
                    payload = _js.loads(cache_path.read_text(encoding="utf-8"))
                    cache = EntityContextCache.from_payload(payload)
                    key = entity_context_cache_key(source_hash=source.source_hash, extractor_version=cfg.extractor_version)
                    ctx = cache.get(key)
                    if ctx and ctx.chapter_id == chapter_id and ctx.source_hash == source.source_hash:
                        for rec in ctx.entities:
                            if rec.anchor.status == "verified" and is_entity_glossary_candidate(rec, source_map):
                                entity_records.append(rec)
            except Exception as exc:
                LOG.warning("glossary_resolver: fallback cache load failed %s: %s", chapter_id, exc)
        if not entity_records:
            LOG.info("glossary_resolver: no candidates for %s", chapter_id)
            journal.emit("glossary_resolver", mode=mode, status="skipped", reason="no_candidates", entity_count=0)
            self._emit_progress("glossary_resolver", mode=mode, status="skipped", reason="no_candidates")
            return
        allowed = compute_allowed_evidence_pids(source_map, entity_records)
        quarantined_set = set(quarantined_pids or ())
        if quarantined_set:
            for ent in list(allowed.keys()):
                allowed[ent] = allowed[ent] - quarantined_set
        cand_hash = glossary_candidate_input_hash(entity_records)
        trans_hash = glossary_translation_hash(translations_repaired)
        try:
            from pact_v4.pipeline.glossary_resolver import _model_ref_for_resolver as _mrfr
            model_ref = _mrfr(self._audit_backend) or "unknown"
        except Exception:
            model_ref = "unknown"
        backend_identity = backend_identity_hash or "unknown"
        # v4.2: bind rendered glossary view identity (hash/version/selection/
        # glossary slice) so a changed established-term card invalidates replay.
        _gv_hash = ""
        _gv_version = ""
        try:
            _gview = (book_memory_role_views or {}).get("glossary") if isinstance(book_memory_role_views, Mapping) else None
            if _gview is not None:
                _gv_hash = str(getattr(_gview, "canonical_hash", "") or _gview.get("canonical_hash", "") if isinstance(_gview, Mapping) else getattr(_gview, "canonical_hash", ""))
                _gv_version = str(getattr(_gview, "schema_version", "") or _gview.get("schema_version", "") if isinstance(_gview, Mapping) else getattr(_gview, "schema_version", ""))
                if not _gv_hash and isinstance(_gview, Mapping) and "text" in _gview:
                    import hashlib, json as _js2
                    _gv_hash = hashlib.sha256(_js2.dumps(str(_gview.get("text", "")), ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
                if not _gv_version and isinstance(_gview, Mapping):
                    _gv_version = str(_gview.get("schema_version", ""))
        except Exception:
            _gv_hash = ""
            _gv_version = ""
        payload_existing, err = load_and_validate_sidecar(
            out_dir,
            expected_chapter_id=chapter_id,
            expected_snapshot_hash=snapshot_hash,
            expected_config_identity=config_identity,
            expected_candidate_input_hash=cand_hash,
            expected_translation_hash=trans_hash,
            expected_model_ref=model_ref if model_ref != "unknown" else None,
            expected_backend_identity=backend_identity if backend_identity != "unknown" else None,
            expected_glossary_view_hash=_gv_hash or None,
            expected_glossary_view_version=_gv_version or None,
            allowed_pids=allowed,
            translation_map=translations_repaired,
            quarantined_pids=quarantined_set,
        )
        if payload_existing is not None:
            LOG.info("glossary_resolver: valid sidecar for %s, 0 calls", chapter_id)
            journal.emit("glossary_resolver", mode=mode, status="cache_hit_valid", entity_count=len(entity_records), candidate_hash=cand_hash[:12])
            self._emit_progress("glossary_resolver", mode=mode, status="cache_hit_valid", entity_count=len(entity_records))
            return
        if is_cache_hit and policy == "fail_closed":
            LOG.warning("glossary_resolver: cache hit stale %s for %s, fail_closed", err, chapter_id)
            journal.emit("glossary_resolver", mode=mode, status="cache_hit_stale_fail_closed", error=err, policy=policy)
            self._emit_progress("glossary_resolver", mode=mode, status="cache_hit_stale_fail_closed", error=err)
            return
        if is_cache_hit:
            LOG.info("glossary_resolver: cache hit stale for %s, recompute", chapter_id)
        else:
            LOG.info("glossary_resolver: no valid sidecar for %s (%s), running", chapter_id, err)
        from pact_v4.pipeline.glossary_resolver import _model_ref_for_resolver
        if not _model_ref_for_resolver(self._audit_backend):
            LOG.warning("glossary_resolver: no reviewer binding for %s", chapter_id)
            journal.emit("glossary_resolver", mode=mode, status="fail_closed", reason="no_reviewer_binding")
            self._emit_progress("glossary_resolver", mode=mode, status="fail_closed", reason="no_reviewer_binding")
            return
        existing_keys = {}
        try:
            if isinstance(glossary, dict):
                for k in glossary.keys():
                    existing_keys[str(k).casefold()] = str(k)
            elif isinstance(glossary, (list, tuple)):
                for entry in glossary:
                    if isinstance(entry, dict) and "source" in entry:
                        existing_keys[str(entry["source"]).casefold()] = str(entry["source"])
                    elif isinstance(entry, str):
                        existing_keys[entry.casefold()] = entry
        except Exception:
            pass
        resolver = GlossaryResolver(self._audit_backend, progress=self._progress)
        _glossary_card = None
        if book_memory_role_views is not None:
            _glossary_card = book_memory_role_views.get("glossary")
            if _glossary_card is not None and hasattr(_glossary_card, "text"):
                _glossary_card = _glossary_card.text  # type: ignore[attr-defined]
            elif _glossary_card is not None:
                _glossary_card = str(_glossary_card)
        result = resolver.resolve(
            chapter_id=chapter_id,
            entity_records=entity_records,
            source_map=source_map,
            translations=translations_repaired,
            allowed_pids=allowed,
            out_dir=out_dir,
            role_view_card=_glossary_card,
        )
        if result is None:
            LOG.warning("glossary_resolver: resolver failed for %s", chapter_id)
            journal.emit("glossary_resolver", mode=mode, status="failed", entity_count=len(entity_records))
            self._emit_progress("glossary_resolver", mode=mode, status="failed", entity_count=len(entity_records))
            return
        raw_proposals = result.get("raw_proposals", [])
        canonical_proposals = []
        global_ru_map = {}
        seen_entities = set()
        for prop in raw_proposals:
            entity = str(prop.get("entity") or "")
            if not entity or entity in seen_entities:
                continue
            seen_entities.add(entity)
            rec_match = next((r for r in entity_records if str(r.entity) == entity), None)
            if rec_match is None:
                continue
            proposed_ru = str(prop.get("proposed_ru") or "")
            surface_forms = prop.get("surface_forms") or []
            evidence_pid = str(prop.get("evidence_pid") or "")
            ptype = str(prop.get("type") or "person")
            confidence = prop.get("confidence", 0.9)
            decision = str(prop.get("decision") or "accept")
            if decision != "accept":
                continue
            # Deterministic canonical English key selection per spec:
            # 1) existing glossary key among entity/aliases (casefold match),
            # 2) validated B1.2 canonical surface / min-PID (EntityRecord.entity is B1.2 canonical),
            # 3) EntityRecord.entity fallback. Ordered priority, no set iteration.
            ordered_surfaces: List[str] = [entity]
            # Aliases sorted by pid then surface for determinism (min-PID canonical)
            for alias in sorted(rec_match.aliases, key=lambda x: (str(getattr(x, "pid", "")), str(getattr(x, "surface", "")).casefold())):
                surf = str(getattr(alias, "surface", "") or "")
                if surf and surf not in ordered_surfaces:
                    ordered_surfaces.append(surf)
            canonical = None
            for surf in ordered_surfaces:
                cf = surf.casefold()
                if cf in existing_keys:
                    canonical = existing_keys[cf]
                    break
            if canonical is None:
                # B1.2 canonical is EntityRecord.entity (validated min-PID surface)
                canonical = entity
            ru_cf = proposed_ru.casefold()
            if ru_cf in global_ru_map and global_ru_map[ru_cf] != canonical:
                LOG.warning("glossary_resolver: duplicate ru %r between %r and %r", proposed_ru, global_ru_map[ru_cf], canonical)
                journal.emit("glossary_resolver", mode=mode, status="conflict_duplicate_ru", entity=entity, proposed_ru=proposed_ru)
                continue
            allowed_for = allowed.get(entity, set())
            if evidence_pid not in allowed_for:
                LOG.warning("glossary_resolver: evidence not allowed %r for %r", evidence_pid, entity)
                continue
            if quarantined_set and evidence_pid in quarantined_set:
                continue
            ev_text = translations_repaired.get(evidence_pid, "")
            if not all(sf in ev_text for sf in surface_forms):
                continue
            from pact_v4.pipeline.glossary_resolver import lemma_v1_match, is_cyrillic, RU_STOP_WORDS, _GLOSSARY_BLOCKLIST
            if not lemma_v1_match(surface_forms, proposed_ru):
                continue
            if not is_cyrillic(proposed_ru):
                continue
            if proposed_ru.casefold() in RU_STOP_WORDS or proposed_ru.casefold() in _GLOSSARY_BLOCKLIST:
                continue
            canonical_proposals.append({
                "entity": canonical,
                "proposed_ru": proposed_ru,
                "surface_forms": surface_forms,
                "evidence_pid": evidence_pid,
                "type": ptype if ptype in ("person","place","group","nickname") else "person",
                "confidence": float(confidence) if isinstance(confidence, (int,float)) else 0.9,
                "decision": "accept",
            })
            global_ru_map[ru_cf] = canonical
        canonical_proposals.sort(key=lambda x: x["entity"])
        payload = build_sidecar_payload(
            chapter_id=chapter_id,
            snapshot_hash=snapshot_hash,
            config_identity=config_identity,
            candidate_input_hash=cand_hash,
            translation_hash_val=trans_hash,
            model_ref=model_ref or "unknown",
            backend_identity=backend_identity,
            proposals=canonical_proposals,
            glossary_view_hash=_gv_hash,
            glossary_view_version=_gv_version,
        )
        err = validate_sidecar_payload(
            payload,
            expected_chapter_id=chapter_id,
            expected_snapshot_hash=snapshot_hash,
            expected_config_identity=config_identity,
            expected_candidate_input_hash=cand_hash,
            expected_translation_hash=trans_hash,
            expected_glossary_view_hash=_gv_hash or None,
            expected_glossary_view_version=_gv_version or None,
            allowed_pids=allowed,
            translation_map=translations_repaired,
            quarantined_pids=quarantined_set,
        )
        if err:
            LOG.warning("glossary_resolver: sidecar validation failed for %s: %s", chapter_id, err)
            journal.emit("glossary_resolver", mode=mode, status="validation_failed", error=err)
            self._emit_progress("glossary_resolver", mode=mode, status="validation_failed", error=err)
            return
        try:
            target = glossary_sidecar_path(out_dir)
            if target.exists() and target.is_symlink():
                LOG.warning("glossary_resolver: sidecar symlink for %s", chapter_id)
                journal.emit("glossary_resolver", mode=mode, status="fail_symlink")
                return
            atomic_write_sidecar(out_dir, payload)
            LOG.info("glossary_resolver: sidecar written for %s with %d", chapter_id, len(canonical_proposals))
            journal.emit("glossary_resolver", mode=mode, status="written", proposal_count=len(canonical_proposals), candidate_hash=cand_hash[:12])
            self._emit_progress("glossary_resolver", mode=mode, status="written", proposal_count=len(canonical_proposals))
        except Exception as exc:
            LOG.warning("glossary_resolver: write failed for %s: %s", chapter_id, exc)
            journal.emit("glossary_resolver", mode=mode, status="write_failed", error=str(exc))
            self._emit_progress("glossary_resolver", mode=mode, status="write_failed", error=str(exc))

    def _emit_progress(self, event: str, **fields: Any) -> None:
        progress = self._progress
        if progress is None:
            return
        emit = getattr(progress, "emit", None)
        if callable(emit):
            try:
                emit(event, **fields)
            except Exception:  # noqa: BLE001 — progress is diagnostics
                LOG.debug("B3: progress emit failed for %s", event, exc_info=True)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

# V4.2 R artifact schemas (identity-bearing, never reused across a schema
# change).
R_EDITED_SCHEMA = "pact-v4-translations-edited/v1"
R_CANDIDATES_SCHEMA = "pact-v4-edit-candidates/v1"


def _build_r_editor_report(
    *,
    cfg: B3AuditRepairConfig,
    outcome: Optional[RussianEditorOutcome],
    review_journal: Sequence[Mapping[str, Any]],
    from_cache: bool,
    # R fallback identity (review b55e940 HIGH): the standalone
    # r_editor_report.json must be replayable ONLY against the exact run
    # that produced it — the RAW translation hash, the raw PID set/order,
    # and the run config identity (which carries the R prompt/harness/
    # chunking policy via StrictRunConfig, F5). Persisted so the fail-path
    # fallback can reject stale reports fail-closed.
    translation_hash: str,
    pids: Sequence[str],
    config_identity: str,
) -> Dict[str, Any]:
    """Build the Russian-editor report for the trial record / audit cache.

    ``outcome`` is None when the stage failed (transport/evaluator error) —
    the report then records status ``failed`` (the audit still protects the
    chapter; R edits were not applied). ``outcome.complete=False`` records
    status ``partial`` when at least one successful chunk produced edits /
    candidates (RESILIENCE t_406fc48c: per-chunk partial-apply) or
    ``incomplete`` when nothing was applied (all chunks failed — fail-closed
    with 0 successful output).
    """
    status = "disabled"
    outcome_payload: Optional[Dict[str, Any]] = None
    if cfg.russian_editor_enabled:
        if outcome is None:
            # RV fd7ee8e: enabled but the evaluator raised (transport or
            # evaluator error) — the stage FAILED, never "disabled". The
            # audit still protects the chapter; R edits were not applied.
            status = "failed"
        else:
            outcome_payload = outcome.to_payload()
            if outcome.complete:
                status = "complete"
            elif outcome.applied or outcome.candidates:
                status = "partial"
            else:
                status = "incomplete"
    report = {
        "enabled": cfg.russian_editor_enabled,
        "status": status,
        "version": cfg.russian_editor_version,
        "harness_version": cfg.russian_editor_harness_version,
        "chunk_size": cfg.russian_editor_chunk_size,
        "overlap_pairs": cfg.russian_editor_overlap_pairs,
        "safe_classes": sorted(cfg.russian_editor_safe_classes),
        # R fallback identity (review b55e940 HIGH): binds the report to the
        # exact raw translation content, PID coverage, and run config that
        # produced it. The standalone r_editor_report.json fail-path fallback
        # validates these fail-closed before reusing any GOOD chunk — a
        # stale report (old harness, changed chunk size/text) is rejected
        # and R re-runs from scratch.
        "translation_hash": translation_hash,
        "pids": list(pids),
        "config_identity": config_identity,
        "outcome": outcome_payload,
        "review_journal": [dict(entry) for entry in review_journal],
        "from_cache": from_cache,
    }
    return report


def _write_r_editor_artifacts(
    *,
    cfg: B3AuditRepairConfig,
    chapter_id: str,
    snapshot_hash: str,
    config_identity: str,
    out_dir: Path,
    edited_map: Mapping[str, str],
    candidates: Sequence[Any],
) -> None:
    """Persist the R-stage artifacts next to the audit cache.

    * ``translations_edited.json`` (schema ``pact-v4-translations-edited/v1``)
      — the R-EDITED map (raw + SAFE diff-gated edits) that the audit/repair
      consumed, with run identity;
    * ``edit_candidates.json`` (schema ``pact-v4-edit-candidates/v1``) — the
      REVIEW-classed candidates (pid, original, proposed, class, reason) that
      were forwarded to the B2 verifier, with run identity.

    Written ONLY on a cache miss (a full cache hit restores the repaired map
    and the report from the cache payload; the artifacts already exist from
    the original run — F8: the runner advertises only existing files).
    """
    identity = {
        "schema": R_EDITED_SCHEMA,
        "chapter_id": chapter_id,
        "snapshot_hash": snapshot_hash,
        "config_identity": config_identity,
    }
    _atomic_write_json(
        out_dir / "translations_edited.json",
        {**identity, "translations": dict(edited_map)},
    )
    candidate_payload = [
        {
            "pid": c.pid,
            "original": c.original,
            "proposed": c.proposed,
            "class": c.klass,
            "reason": c.reason,
        }
        for c in candidates
    ]
    _atomic_write_json(
        out_dir / "edit_candidates.json",
        {
            "schema": R_CANDIDATES_SCHEMA,
            "chapter_id": chapter_id,
            "snapshot_hash": snapshot_hash,
            "config_identity": config_identity,
            "candidates": candidate_payload,
        },
    )


def _reports_from_cache(
    *,
    cache: B3AuditCache,
    issue_count: int,
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    repair_payload = cache.stored_repair()
    repair_complete = bool(
        repair_payload and repair_payload.get("repair_complete") is True
    )
    committed_pids = [
        pair[0] for pair in (repair_payload or {}).get("committed") or []
    ]
    passed_pids = list((repair_payload or {}).get("passed_pids") or [])
    debt_trace = list((repair_payload or {}).get("debt_trace") or [])
    # REPAIR-2 (t_768537b9): per-index non-fatal notices (no-op repairs
    # converted to per-index pass) restored from the cached repair payload.
    warnings = list((repair_payload or {}).get("warnings") or [])
    step6 = {
        "status": "complete",
        "audit_complete": True,
        "issue_count": issue_count,
        "from_cache": True,
    }
    step7 = {
        "status": (
            "complete"
            if repair_complete
            else ("incomplete" if repair_payload else "failed")
        ),
        "repair_complete": repair_complete,
        "committed_pids": committed_pids,
        "passed_pids": passed_pids,
        "debt_trace": debt_trace,
        "warnings": warnings,
        "from_cache": True,
    }
    # F1: the cache replay honors the same terminal gate as a live run — a
    # cached repair that did not complete is replayed as accepted_degraded /
    # NOT released, never silently upgraded to complete/released_as_audited.
    if repair_complete:
        step8 = {
            "status": "complete",
            "audit_complete": True,
            "released_as_audited": True,
            "from_cache": True,
        }
    else:
        step8 = {
            "status": "accepted_degraded",
            "audit_complete": True,
            "released_as_audited": False,
            "repair_complete": False,
            "reason": (
                "repair_failed" if repair_payload is None else "repair_incomplete"
            ),
            "debt_trace": debt_trace,
            "from_cache": True,
        }
    return step6, step7, step8


__all__ = [
    "B3_AUDIT_CACHE_SCHEMA",
    "B3_AUDIT_JOURNAL_SCHEMA",
    "B3AuditCache",
    "B3AuditRepair",
    "B3AuditRepairConfig",
    "B3AuditRepairResult",
    "AuditJournal",
    "render_entity_context_block",
    "R_EDITED_SCHEMA",
    "R_CANDIDATES_SCHEMA",
]
