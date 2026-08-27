"""Book memory policy block - canonical policy inside book_memory.json"""
from __future__ import annotations
from typing import Any, Dict, List, Mapping

BOOK_MEMORY_SCHEMA = "pact-v4-book-memory/v2"
BOOK_MEMORY_POLICY_VERSION = "book-memory-policy/v1"
GENERIC_PATTERNS_VERSION = "generic-memory-reject/v1"

# stable rejection codes
REJECTION_CODES = {
    "invalid_identity",
    "explicit_deny",
    "model_veto",
    "chapter_local",
    "generic_role",
    "generic_object",
    "term_not_approved",
    "duplicate",
    "conflict",
    "candidate_claim",
    "quarantined_evidence",
}

def default_policy_block() -> Dict[str, Any]:
    return {
        "schema": BOOK_MEMORY_SCHEMA,
        "book_memory_policy_version": BOOK_MEMORY_POLICY_VERSION,
        "policy": {
            "explicit_deny": [],
            "explicit_allow": {},
            "aliases": {},
            "approved_terms": [],
            "generic_patterns_version": GENERIC_PATTERNS_VERSION,
        }
    }

def ensure_policy_block(book_memory: Mapping[str, Any]) -> Dict[str, Any]:
    bm = dict(book_memory) if isinstance(book_memory, dict) else {}
    if bm.get("schema") != BOOK_MEMORY_SCHEMA:
        bm["schema"] = BOOK_MEMORY_SCHEMA
    if bm.get("book_memory_policy_version") != BOOK_MEMORY_POLICY_VERSION:
        bm["book_memory_policy_version"] = BOOK_MEMORY_POLICY_VERSION
    if "policy" not in bm or not isinstance(bm["policy"], dict):
        bm["policy"] = default_policy_block()["policy"]
    else:
        for k, v in default_policy_block()["policy"].items():
            if k not in bm["policy"]:
                bm["policy"][k] = v
    return bm
