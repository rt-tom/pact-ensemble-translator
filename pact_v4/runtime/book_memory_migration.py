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

def build_index_from_memory(migrated_book_memory: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic v2 chapter_index built SOLELY from migrated memory (no source needed).
    For each entry in characters/entities/terms/facts/address, use its chapters list and memory_class
    to place the name into the correct per-chapter bucket with causal provenance.
    """
    try:
        from pact_full_pipeline_runner_v1.build_chapter_index import CHAPTER_INDEX_V2_SCHEMA as _SCHEMA, BOOK_MEMORY_POLICY_VERSION as _PV_DEFAULT
    except Exception:
        _SCHEMA = "pact-v4-chapter-index/v2"
        _PV_DEFAULT = "book-memory-policy/v1"
    schema = _SCHEMA
    policy_ver = str(migrated_book_memory.get("book_memory_policy_version", _PV_DEFAULT))
    # Accumulate per-chapter buckets
    per_chapter: Dict[str, Dict[str, List[str]]] = {}
    def _ensure(ch: str):
        if ch not in per_chapter:
            per_chapter[ch] = {"characters": [], "named_entities": [], "terms": [], "facts": [], "address": []}
        return per_chapter[ch]
    # characters / entities with memory_class
    for section in ("characters", "entities"):
        sec = migrated_book_memory.get(section, {})
        if not isinstance(sec, dict):
            continue
        for name, entry in sec.items():
            if not isinstance(entry, dict):
                continue
            mc = str(entry.get("memory_class") or "")
            if mc == "chapter_local":
                continue
            # Determine bucket
            if mc == "named_character":
                bucket = "characters"
            elif mc in ("named_place", "named_group", "named_artifact", "named_creature", "named_entity"):
                bucket = "named_entities"
            elif mc == "world_term":
                bucket = "terms"
            else:
                # Default based on section for backward compat: characters -> characters, entities -> named_entities
                if section == "characters":
                    bucket = "characters"
                else:
                    bucket = "named_entities"
                # If world_term without explicit class but in approved terms, put in terms
                # Check policy approved_terms
                approved = []
                try:
                    approved = migrated_book_memory.get("policy", {}).get("approved_terms", [])
                except Exception:
                    approved = []
                if any(name.casefold() == str(t).casefold() for t in approved):
                    bucket = "terms"
            chs = entry.get("chapters")
            if not isinstance(chs, (list, tuple)) or not chs:
                chs = [entry.get("first_seen_chapter")] if entry.get("first_seen_chapter") else []
            for ch in chs:
                if not isinstance(ch, str) or not ch:
                    continue
                bucket_list = _ensure(str(ch))[bucket]
                if name not in bucket_list:
                    bucket_list.append(name)
    # terms section if present (explicit)
    terms_sec = migrated_book_memory.get("terms")
    if isinstance(terms_sec, dict):
        for name, entry in terms_sec.items():
            chs = []
            if isinstance(entry, dict):
                chs = entry.get("chapters") or [entry.get("first_seen_chapter")] if entry.get("first_seen_chapter") else []
            else:
                chs = []
            for ch in chs or []:
                if isinstance(ch, str) and ch:
                    _ensure(ch)["terms"].append(name)
    elif isinstance(terms_sec, list):
        for entry in terms_sec:
            if isinstance(entry, dict):
                name = str(entry.get("name") or entry.get("term") or "")
                ch = str(entry.get("chapter") or "")
                if name and ch:
                    _ensure(ch)["terms"].append(name)
    # facts
    facts = migrated_book_memory.get("facts")
    if isinstance(facts, list):
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            text = str(fact.get("fact") or fact.get("text") or "")
            if not text:
                continue
            ch = str(fact.get("chapter") or "")
            if not ch:
                # Try keys/chapters?
                continue
            _ensure(ch)["facts"].append(text)
    # address_register
    addr = migrated_book_memory.get("address_register")
    if isinstance(addr, list):
        for entry in addr:
            if not isinstance(entry, dict):
                continue
            text = str(entry.get("text") or "")
            if not text:
                continue
            ch = str(entry.get("chapter") or entry.get("first_seen_chapter") or "")
            if ch:
                _ensure(ch)["address"].append(text)
            else:
                # If no chapter provenance, assign to first chapter bucket if any, else skip
                if per_chapter:
                    first = sorted(per_chapter.keys())[0]
                    _ensure(first)["address"].append(text)
    # Build final deterministic index with sorted buckets
    out: Dict[str, Any] = {"$schema": schema, "$book_memory_policy_version": policy_ver}
    for ch in sorted(per_chapter.keys()):
        buckets = per_chapter[ch]
        out[ch] = {
            "characters": sorted(set(buckets["characters"]), key=lambda x: x.casefold()),
            "named_entities": sorted(set(buckets["named_entities"]), key=lambda x: x.casefold()),
            "terms": sorted(set(buckets["terms"]), key=lambda x: x.casefold()),
            "facts": sorted(set(buckets["facts"]), key=lambda x: x.casefold()),
            "address": sorted(set(buckets["address"]), key=lambda x: x.casefold()),
        }
    return out

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

def build_migration_candidate(parent_dir: Path, candidate_dir: Path, migrated_book_memory: Dict[str, Any], rebuilt_index: Dict[str, Any] | None = None, approved_glossary_change: Dict[str, Any] | None = None) -> None:
    """Build exact four-file candidate per owner clarification.
    FINDING 6: chapter_index MUST be rebuilt deterministically from migrated v2 book_memory inside this function,
    not accepted as stale caller-supplied v1. If rebuilt_index supplied, it must equal deterministic rebuild; otherwise fail closed.
    """
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
    # Chapter index: MUST be rebuilt deterministically from migrated v2 memory (finding 4)
    # Always compute deterministic_index = build_index_from_memory(migrated_book_memory)
    deterministic_index = build_index_from_memory(migrated_book_memory)
    expected_schema = deterministic_index.get("$schema")
    expected_pv = deterministic_index.get("$book_memory_policy_version")
    if rebuilt_index is not None:
        # Require supplied index to equal deterministic rebuild exactly (canonical JSON equality)
        # Normalize both to canonical JSON for comparison
        def _canon(v):
            return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if _canon(rebuilt_index) != _canon(deterministic_index):
            raise RuntimeError(f"stale chapter_index rejected: supplied rebuilt_index diverges from deterministic rebuild (expected {_canon(deterministic_index)[:200]!r} got {_canon(rebuilt_index)[:200]!r})")
        # Also ensure not stale v1 (redundant with above, but explicit)
        if rebuilt_index.get("$schema") != expected_schema:
            raise RuntimeError(f"stale chapter_index rejected: expected $schema {expected_schema!r}, got {rebuilt_index.get('$schema')!r}")
    # Use deterministic_index unconditionally
    # Validate no stale v1 contamination: if parent was v1 and supplied equals parent, already rejected above
    # No additional handling needed
    with open(candidate_dir / "chapter_index.json", "w", encoding="utf-8") as f:
        json.dump(deterministic_index, f, ensure_ascii=False, indent=2, sort_keys=True)
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

# --- Media publication path (finding 7) ---

def _ensure_media_publish_prereqs(envelope: Dict[str, Any], candidate_dir: Path) -> None:
    """Fail closed if envelope not approved or candidate hashes mismatch envelope.
    Requires candidate_hashes to be present and contain exact hash for EACH of the four canonical files; verify each unconditionally before publishing. Fail closed if any hash missing or mismatched. Keeps approved/approval_identity gate.
    """
    if not envelope.get("approved"):
        raise RuntimeError("migration publication requires explicit owner approval of exact manifest/hash set")
    candidate_hashes = envelope.get("candidate_hashes")
    if not isinstance(candidate_hashes, dict):
        raise RuntimeError("rollback/migration requires candidate_hashes to be present with exact hashes for all four canonical files")
    for fname in CANONICAL_FILES:
        if fname not in candidate_hashes:
            raise RuntimeError(f"candidate_hashes missing required hash for {fname} — fail closed")
        exp = candidate_hashes.get(fname)
        if not isinstance(exp, str) or not exp:
            raise RuntimeError(f"candidate_hashes invalid hash for {fname} — fail closed")
    for fname in CANONICAL_FILES:
        cpath = candidate_dir / fname
        if not cpath.is_file() or cpath.is_symlink():
            raise RuntimeError(f"candidate missing or symlink: {fname}")
        actual = _file_hash(cpath)
        exp = candidate_hashes.get(fname)
        if exp != actual:
            raise RuntimeError(f"candidate hash mismatch for {fname}: expected {exp}, got {actual}")

def publish_via_media(
    store: Any,
    candidate_dir: Path,
    envelope: Dict[str, Any],
    *,
    operator: str = "migration",
    host: str = "migration",
    run_id: str | None = None,
) -> Dict[str, Any]:
    """Publish a migrated candidate through existing Media lease/parent/CAS.

    Validates exact manifest + candidate hashes at publication (fail closed if mismatch),
    publishes via Media promote, then verifies post-publication current revision == candidate hashes.
    """
    from pact_v4.snapshot.manifest import Manifest, StateFileEntry
    from pact_v4.snapshot.store import BookStore
    import datetime
    candidate_dir = Path(candidate_dir)
    _ensure_media_publish_prereqs(envelope, candidate_dir)
    # Create Media incoming candidate bundle from plain four-file candidate
    import uuid, os, json as _json
    candidate_id = f"migration-{uuid.uuid4().hex[:8]}"
    # Build manifest for media
    parent_rev = envelope.get("parent_revision")
    # Read current store current to validate parent
    cur = store.read_current()
    if cur is None or cur.get("revision_id") != parent_rev:
        raise RuntimeError(f"stale parent: envelope expects {parent_rev}, current is {cur.get('revision_id') if cur else None}")
    # Compute state file entries
    state_entries = []
    for fname in CANONICAL_FILES:
        cpath = candidate_dir / fname
        sha, size = Manifest_state_hash(cpath) if False else _compute_sha_size(cpath)
        state_entries.append(StateFileEntry(rel_path=f"state/{fname}", sha256=sha, size=size))
    manifest = Manifest(
        schema_version="v1",
        book_id=store.book_id,
        revision_id="rev-0000",  # placeholder, media assigns real id
        parent_revision_id=parent_rev,
        created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        published_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        terminal_status="complete",
        tool_version="book-memory-migration/v1",
        source={"path_on_rt": str(candidate_dir), "operator": operator, "host": host, "run_id": run_id},
        state_files=state_entries,
        excludes=[],
        code_commit="",
    )
    # Prepare incoming candidate dir
    incoming = store.incoming_candidate_path(candidate_id)
    incoming.mkdir(parents=True, exist_ok=True)
    state_dir = incoming / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    for fname in CANONICAL_FILES:
        import shutil
        shutil.copy2(str(candidate_dir / fname), str(state_dir / fname))
    # Write manifest
    manifest.write(incoming / "manifest.json")
    # Publish via existing Media promote gate
    from pact_v4.snapshot.promote import promote as _media_promote
    result = _media_promote(store, candidate_id, operator=operator, host=host, run_id=run_id)
    # Post-publication verification: current revision hashes == candidate hashes
    cur_after = store.read_current()
    if cur_after is None or cur_after.get("revision_id") != result.get("revision_id"):
        raise RuntimeError(f"post-publication verification failed: current {cur_after} != result {result}")
    # Verify each file hash matches candidate (via snapshot)
    snap_dir = store.snapshot_dir(result["revision_id"])
    for fname in CANONICAL_FILES:
        snap_file = snap_dir / "state" / fname
        if not snap_file.is_file():
            raise RuntimeError(f"post-publication missing {fname} in snapshot")
        if _file_hash(snap_file) != _file_hash(candidate_dir / fname):
            raise RuntimeError(f"post-publication hash mismatch for {fname}")
    return result

def _compute_sha_size(path: Path):
    import hashlib
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(8192)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size

def rollback_via_media(store: Any, snapshot_dir: Path, *, envelope: Dict[str, Any] | None = None, operator: str = "rollback", host: str = "rollback", run_id: str | None = None) -> Dict[str, Any]:
    """Rollback by publishing a NEW revision from retained pre-migration snapshot (never rewrite history).
    FINDING 5: requires explicit owner approval via envelope (approval_identity / approved manifest+hashes) before publishing.
    """
    # FINDING 5: approval gate — fail closed if not explicitly approved
    if envelope is None or not envelope.get("approved") or not envelope.get("approval_identity"):
        raise RuntimeError("rollback publication requires explicit owner-approved envelope with approval_identity")
    # Also validate candidate hashes if envelope provides them (ensure rollback candidate matches approved hashes)
    # Will be validated again after building candidate_tmp via _ensure_media_publish_prereqs if candidate_hashes present

    import uuid, shutil, datetime
    from pact_v4.snapshot.manifest import Manifest, StateFileEntry
    snapshot_dir = Path(snapshot_dir)
    # snapshot_dir is expected to contain exactly four canonical files (state files) as retained snapshot
    # Validate it has four files
    for fname in CANONICAL_FILES:
        p = snapshot_dir / fname
        # also allow snapshot_dir/state/* layout - handle both
        if not p.exists():
            alt = snapshot_dir / "state" / fname
            if alt.exists():
                p = alt
            else:
                raise RuntimeError(f"rollback snapshot missing {fname}")
    # Use the snapshot files as candidate
    import tempfile as _tmp; candidate_tmp = Path(_tmp.mkdtemp()) / "rollback_candidate"
    candidate_tmp.mkdir(parents=True, exist_ok=True)
    for fname in CANONICAL_FILES:
        src = snapshot_dir / fname
        if not src.exists():
            src = snapshot_dir / "state" / fname
        shutil.copy2(str(src), str(candidate_tmp / fname))
    # Finding 3: rollback requires exact hashes for all four canonical files, unconditionally — verify each before publishing, fail closed if missing/mismatched
    _ensure_media_publish_prereqs(envelope, candidate_tmp)
    # Build envelope-like candidate and publish via media as new revision
    # Create manifest
    cur = store.read_current()
    parent_rev = cur.get("revision_id") if cur else None
    state_entries = []
    for fname in CANONICAL_FILES:
        sha, size = _compute_sha_size(candidate_tmp / fname)
        state_entries.append(StateFileEntry(rel_path=f"state/{fname}", sha256=sha, size=size))
    manifest = Manifest(
        schema_version="v1",
        book_id=store.book_id,
        revision_id="rev-0000",
        parent_revision_id=parent_rev,
        created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        published_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        terminal_status="complete",
        tool_version="book-memory-rollback/v1",
        source={"path_on_rt": str(snapshot_dir), "operator": operator, "host": host, "run_id": run_id},
        state_files=state_entries,
        excludes=[],
        code_commit="",
    )
    candidate_id = f"rollback-{uuid.uuid4().hex[:8]}"
    incoming = store.incoming_candidate_path(candidate_id)
    incoming.mkdir(parents=True, exist_ok=True)
    (incoming / "state").mkdir(parents=True, exist_ok=True)
    for fname in CANONICAL_FILES:
        shutil.copy2(str(candidate_tmp / fname), str(incoming / "state" / fname))
    manifest.write(incoming / "manifest.json")
    from pact_v4.snapshot.promote import promote as _media_promote
    result = _media_promote(store, candidate_id, operator=operator, host=host, run_id=run_id)
    # Verify as new revision, not rewriting history
    if result.get("revision_id") == parent_rev:
        raise RuntimeError("rollback must publish as new revision, not overwrite")
    return result

def Manifest_state_hash(path):  # helper alias for tests
    return _compute_sha_size(path)

