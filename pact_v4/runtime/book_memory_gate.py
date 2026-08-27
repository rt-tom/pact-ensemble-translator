"""Deterministic durable-memory gate: identity -> deny -> allow/alias -> model_veto -> class-check -> duplicate/conflict/quarantine"""
from __future__ import annotations
import re
from typing import Any, Dict, Mapping, Tuple, Set

GENERIC_OBJECT_PATTERNS = re.compile(r"\b(car|mirror|coat|door|window|table|chair|hatchet|broom|pocketwatch|bike|motorcycle|cat|dog|hat|bag)\b", re.I)
GENERIC_ROLE_PATTERNS = re.compile(r"\b(old man|old woman|little boy|little girl|nurse|teacher|doctor|officer)\b", re.I)

MEMORY_CLASSES = frozenset({"named_character","named_place","named_group","named_artifact","named_creature","world_term","chapter_local"})

def _norm(s: str) -> str:
    return s.strip().casefold().replace("\u2019","'")

def _word_boundary_in_source(term: str, source_text: str) -> bool:
    if not term:
        return False
    esc = re.escape(term)
    esc = esc.replace(r"\ ", r"\s+")
    return bool(re.search(rf"(?<![A-Za-z0-9_]){esc}(?![A-Za-z0-9_])", source_text, flags=re.I))

def evaluate_gate(record: Any, *, policy: Mapping[str, Any], source_map: Mapping[str,str], quarantined_pids: Set[str] | None = None, existing_names: Set[str] | None = None, current_chapter: str | None = None, source_pids: Set[str] | None = None, duplicate_names: Set[str] | None = None, conflicts: Set[str] | None = None, _legacy_current_chapter: str | None = None) -> Tuple[bool, str]:
    # Normalize optional sets for backward compatibility
    if quarantined_pids is None:
        quarantined_pids = set()
    if existing_names is None:
        existing_names = set()
    if duplicate_names is None:
        duplicate_names = set(existing_names) if existing_names else set()
    if conflicts is None:
        conflicts = set()
    if source_pids is None:
        source_pids = set(source_map.keys()) if source_map else set()
    # (a) current chapter/source/extractor IDENTITY check first
    if current_chapter is not None:
        if not isinstance(current_chapter, str) or not current_chapter:
            return False, "invalid_identity"
        # anchor PID must be within source_pids for this chapter
        anchor_pid = getattr(getattr(record, "anchor", None), "pid", None)
        if anchor_pid is not None and anchor_pid not in source_pids:
            return False, "invalid_identity"
        # source_map must contain at least one entry for identity to be valid
        if not source_map:
            return False, "invalid_identity"
        # memory_class/memory_worthy must be present and valid (B1.2 contract)
        mc_raw = getattr(record, "memory_class", None)
        mw_raw = getattr(record, "memory_worthy", None)
        if mc_raw is None or mw_raw is None:
            return False, "invalid_identity"
        if mc_raw not in MEMORY_CLASSES:
            return False, "invalid_identity"
        if not isinstance(mw_raw, bool):
            return False, "invalid_identity"
    else:
        # When identity not supplied, still validate class for invalid_identity precedence before model_veto
        mc_raw = getattr(record, "memory_class", None)
        if mc_raw is not None and mc_raw not in MEMORY_CLASSES:
            return False, "invalid_identity"
    # (b) explicit_deny
    deny = [_norm(x) for x in policy.get("explicit_deny", [])]
    if _norm(record.entity) in deny:
        return False, "explicit_deny"
    # (c) explicit_allow/aliases override
    allow = {_norm(k): v for k,v in policy.get("explicit_allow", {}).items()}
    aliases_policy = {_norm(k): v for k,v in policy.get("aliases", {}).items()}
    is_allowed = _norm(record.entity) in allow or _norm(record.entity) in aliases_policy
    if is_allowed:
        # still need quarantine/duplicate/conflict checks after allow per spec: allow does not bypass quarantine/duplicate/conflict
        # but spec order: deny -> allow -> model_veto -> class-check -> duplicate/conflict/quarantine
        # So allow returns eligible unless quarantine/duplicate/conflict later reject. However spec says duplicate/conflict/quarantine are last.
        # To respect order, we postpone final eligible until after those checks.
        pass
    else:
        # (d) model_veto (memory_worthy)
        if not getattr(record, "memory_worthy", False):
            return False, "model_veto"
        # (e) class-check
        mc = getattr(record, "memory_class", "chapter_local")
        if mc == "chapter_local":
            return False, "chapter_local"
        if mc not in MEMORY_CLASSES:
            return False, "invalid_identity"
        if mc == "world_term":
            approved = [_norm(t) for t in policy.get("approved_terms", [])]
            if _norm(record.entity) not in approved:
                return False, "term_not_approved"
        else:
            joined = " ".join(source_map.values())
            if not record.entity or not record.entity[0].isupper():
                return False, "generic_role"
            norm_ent = _norm(record.entity)
            if GENERIC_OBJECT_PATTERNS.fullmatch(norm_ent):
                return False, "generic_object"
            if GENERIC_ROLE_PATTERNS.fullmatch(norm_ent):
                return False, "generic_role"
            if not _word_boundary_in_source(record.entity, joined):
                return False, "invalid_identity"
    # If explicit_allow was matched, we still need to run quarantine/duplicate/conflict but skip model_veto/class-check
    if is_allowed:
        # no model_veto/class-check when allowed; proceed directly to duplicate/conflict/quarantine
        pass
    # (f) duplicate/conflict/quarantine checks
    norm_ent = _norm(record.entity)
    # Duplicate across chapters is merged, not rejected - only reject if marked as conflict
    # Keep conflict rejection, but duplicate is handled via merge in promotion layer
    if norm_ent in conflicts:
        return False, "conflict"
    quarantined_set = set(quarantined_pids) if quarantined_pids else set()
    if quarantined_set:
        evidence_pids: Set[str] = set()
        anchor = getattr(record, "anchor", None)
        if anchor is not None and getattr(anchor, "pid", None):
            evidence_pids.add(str(anchor.pid))
        for alias in getattr(record, "aliases", []) or []:
            if getattr(alias, "pid", None):
                evidence_pids.add(str(alias.pid))
        for claim in getattr(record, "claims", []) or []:
            for ev in getattr(claim, "evidence", []) or []:
                if getattr(ev, "pid", None):
                    evidence_pids.add(str(ev.pid))
        if evidence_pids & quarantined_set:
            return False, "quarantined_evidence"
        # For candidate-claim handling: if any candidate claim present, that claim alone is withheld but identity may still be eligible.
        # However quarantine on candidate evidence still rejects the whole record per spec (quarantined_evidence rejects).
    # Handle candidate_claim code: if all claims are candidate, identity still eligible but facts not promoted (handled by promotion layer). For gate, identity eligible.
    # But if record has only candidate claims and no verified anchor? Anchor is always verified. So gate passes.
    return True, "eligible"
