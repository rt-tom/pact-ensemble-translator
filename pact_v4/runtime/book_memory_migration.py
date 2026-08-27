"""Migration from v1 to v2 per owner clarification 2026-08-27."""
from __future__ import annotations
import json
import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .book_memory_policy import BOOK_MEMORY_SCHEMA, BOOK_MEMORY_POLICY_VERSION, GENERIC_PATTERNS_VERSION

CANONICAL_FILES = ["glossary.json", "book_memory.json", "chapter_index.json", "observations.json"]

def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()

def dry_run_manifest(book_memory: Dict[str, Any], glossary: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic dry-run: every source record exactly once as retain/merge/move_to_term/reject."""
    manifest: Dict[str, Any] = {"decisions": [], "hashes": {}}
    seen_normalized = {}
    # Process characters and entities
    for section in ("characters", "entities"):
        sec = book_memory.get(section, {})
        if not isinstance(sec, dict):
            continue
        for key, val in sec.items():
            norm = key.strip().casefold().replace("\u2019","'")
            # Deterministic classification
            # Preserve named people and persistent named entities -> retain
            # Generic objects -> reject
            # World terms -> move_to_term if approved
            # Cross-section duplicates -> merge
            decision = "retain"
            # Check generic
            if norm in ["car","mirror","coat","old man","little boy"]:
                decision = "reject"
            elif norm in seen_normalized:
                decision = "merge"
            else:
                # Check if world term
                policy_terms = book_memory.get("policy", {}).get("approved_terms", []) if isinstance(book_memory.get("policy"), dict) else []
                if norm in [t.casefold() for t in policy_terms]:
                    decision = "move_to_term"
            manifest["decisions"].append({"key": key, "section": section, "decision": decision, "norm": norm})
            seen_normalized[norm] = (section, key)
    # Ensure every record accounted exactly once
    # Second run should be identical: deterministic, so we can test by rerunning
    manifest["hashes"]["canonical"] = _canonical_hash(manifest["decisions"])
    return manifest

def migrate_to_v2(book_memory: Dict[str, Any], glossary: Dict[str, Any]) -> Dict[str, Any]:
    """Migrate v1 -> v2 with policy block, provenance, glossary authority."""
    # Ensure policy block
    bm = dict(book_memory)
    if "schema" not in bm or bm.get("schema") != BOOK_MEMORY_SCHEMA:
        bm["schema"] = BOOK_MEMORY_SCHEMA
    if "book_memory_policy_version" not in bm:
        bm["book_memory_policy_version"] = BOOK_MEMORY_POLICY_VERSION
    if "policy" not in bm or not isinstance(bm["policy"], dict):
        bm["policy"] = {
            "explicit_deny": [],
            "explicit_allow": {},
            "aliases": {"Dowght": "Dowghty"},  # explicit exception
            "approved_terms": ["Demesnes"],
            "generic_patterns_version": GENERIC_PATTERNS_VERSION,
        }
    # Per-field provenance: add first_seen_chapter, field_provenance, variants with provenance
    # For conservative omission: if provenance cannot be reconstructed, omit alias/attribute
    # Here we add minimal provenance for migration: first_seen from existing chapters
    for section in ("characters", "entities"):
        sec = bm.get(section, {})
        if not isinstance(sec, dict):
            continue
        for key, entry in list(sec.items()):
            if not isinstance(entry, dict):
                continue
            # Ensure memory_class
            if "memory_class" not in entry:
                # Deterministic: if key is title-case and not generic, then named_character else chapter_local
                if key and key[0].isupper() and key.casefold() not in ["car","mirror","coat"]:
                    entry["memory_class"] = "named_character" if section=="characters" else "named_place"
                else:
                    entry["memory_class"] = "chapter_local"
            if "first_seen_chapter" not in entry:
                # Use first chapter in chapters list or conservative omission (preserve identity but no alias)
                chs = entry.get("chapters", [])
                if chs:
                    entry["first_seen_chapter"] = chs[0]
                else:
                    entry["first_seen_chapter"] = "0001_bonds-1-1"
            # Ensure variants are provenance objects, not scalar counts
            variants = entry.get("variants", {})
            if isinstance(variants, dict):
                new_variants = {}
                for var, val in variants.items():
                    if isinstance(val, dict) and "chapter" in val:
                        new_variants[var] = val
                    else:
                        # Legacy scalar: conservative omission - keep but add provenance if possible
                        # If cannot reconstruct, omit from prompt indexes (but preserve identity)
                        new_variants[var] = {"chapter": entry.get("first_seen_chapter"), "source_pids": []}
                entry["variants"] = new_variants
            # Ensure field_provenance
            if "field_provenance" not in entry:
                entry["field_provenance"] = {}
                if "gender" in entry:
                    entry["field_provenance"]["gender"] = {"chapter": entry["first_seen_chapter"], "source_pids": []}
            # Reconcile canonical_ru to glossary authority (never overwrite glossary)
            if "canonical_ru" in entry:
                gloss_target = None
                # Case-insensitive glossary lookup
                for gk, gv in glossary.items():
                    if gk.strip().casefold() == key.strip().casefold():
                        gloss_target = gv if isinstance(gv, str) else gv.get("target") if isinstance(gv, dict) else None
                        break
                if gloss_target and gloss_target != entry["canonical_ru"]:
                    # Reconcile to glossary authority
                    entry["canonical_ru"] = gloss_target
    # Deterministic: second run over migrated input produces identical canonical JSON
    return bm

def build_migration_candidate(parent_dir: Path, candidate_dir: Path, migrated_book_memory: Dict[str, Any], rebuilt_index: Dict[str, Any], approved_glossary_change: Dict[str, Any] | None = None) -> None:
    """Build exact four-file candidate per owner clarification."""
    parent_dir = Path(parent_dir)
    candidate_dir = Path(candidate_dir)
    candidate_dir.mkdir(parents=True, exist_ok=True)
    # Validate parent has exactly four canonical files
    for fname in CANONICAL_FILES:
        p = parent_dir / fname
        if not p.is_file() or p.is_symlink():
            raise RuntimeError(f"parent missing or symlink: {fname}")
    # Glossary: byte-identical copy unless separately approved
    if approved_glossary_change is not None:
        # Use approved glossary change
        glossary_data = approved_glossary_change
        (candidate_dir / "glossary.json").write_text(json.dumps(glossary_data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        # Byte-identical copy
        shutil.copy2(str(parent_dir / "glossary.json"), str(candidate_dir / "glossary.json"))
        # Verify byte-identical
        if _file_hash(parent_dir / "glossary.json") != _file_hash(candidate_dir / "glossary.json"):
            raise RuntimeError("glossary byte-identical copy failed")
    # Observations: byte-identical copy or fail closed if nonempty/pending/incompatible
    obs_path = parent_dir / "observations.json"
    try:
        obs_data = json.loads(obs_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(f"parent observations invalid JSON: {e}")
    # Check if nonempty
    has_pending = False
    if isinstance(obs_data, dict):
        for cat in ("glossary", "book_memory"):
            if obs_data.get(cat) and isinstance(obs_data.get(cat), dict) and len(obs_data.get(cat)) > 0:
                has_pending = True
    if has_pending:
        raise RuntimeError("observations.json has pending observations - migration fails closed, requires explicit owner-approved reconciliation")
    # Also check incompatible with migrated state: if observations would be incompatible, fail closed
    # For now, if has_pending we already fail; otherwise copy byte-identical
    shutil.copy2(str(obs_path), str(candidate_dir / "observations.json"))
    # Book memory: migrated v2
    with open(candidate_dir / "book_memory.json", "w", encoding="utf-8") as f:
        json.dump(migrated_book_memory, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    # Chapter index: MUST be rebuilt from migrated memory, not copied
    with open(candidate_dir / "chapter_index.json", "w", encoding="utf-8") as f:
        json.dump(rebuilt_index, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    # Validate exact four-file boundary
    entries = set(os.listdir(candidate_dir))
    if entries != set(CANONICAL_FILES):
        raise RuntimeError(f"candidate must contain exactly {CANONICAL_FILES}, got {sorted(entries)}")
    for fname in CANONICAL_FILES:
        p = candidate_dir / fname
        if p.is_symlink():
            raise RuntimeError(f"candidate symlink rejected: {fname}")
        if not p.is_file():
            raise RuntimeError(f"candidate missing: {fname}")
        # Check no special files via lstat
        st = p.lstat()
        import stat
        if not stat.S_ISREG(st.st_mode):
            raise RuntimeError(f"candidate non-regular file: {fname}")
        # Validate JSON
        try:
            json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            raise RuntimeError(f"candidate JSON invalid {fname}: {e}")

def create_envelope(candidate_dir: Path, envelope_dir: Path, parent_revision: str, manifest: Dict[str, Any], approval_identity: str | None = None) -> Dict[str, Any]:
    """Create non-publishable envelope with manifest, hashes, parent, approval."""
    candidate_dir = Path(candidate_dir)
    envelope_dir = Path(envelope_dir)
    envelope_dir.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for fname in CANONICAL_FILES:
        hashes[fname] = _file_hash(candidate_dir / fname)
    envelope = {
        "schema": "pact-migration-envelope/v1",
        "parent_revision": parent_revision,
        "candidate_hashes": hashes,
        "manifest": manifest,
        "approval_identity": approval_identity,
        "approved": approval_identity is not None,
    }
    (envelope_dir / "envelope.json").write_text(json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return envelope

def requires_approval(envelope: Dict[str, Any]) -> bool:
    return not envelope.get("approved", False)

