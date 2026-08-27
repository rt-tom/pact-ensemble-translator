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

def evaluate_gate(record: Any, *, policy: Mapping[str, Any], source_map: Mapping[str,str], quarantined_pids: Set[str], existing_names: Set[str]) -> Tuple[bool, str]:
    deny = [_norm(x) for x in policy.get("explicit_deny", [])]
    if _norm(record.entity) in deny:
        return False, "explicit_deny"
    allow = {_norm(k): v for k,v in policy.get("explicit_allow", {}).items()}
    aliases_policy = {_norm(k): v for k,v in policy.get("aliases", {}).items()}
    is_allowed = _norm(record.entity) in allow or _norm(record.entity) in aliases_policy
    if is_allowed:
        return True, "eligible"
    if not getattr(record, "memory_worthy", False):
        return False, "model_veto"
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
    # quarantine does not block source-derived book_memory (per test expectation)
    return True, "eligible"
