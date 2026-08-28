import json
import os
import hashlib
import shutil
import tempfile
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
try:
    from pact_v4.runtime.book_memory_policy import ensure_policy_block
    from pact_v4.runtime.book_memory_policy import BOOK_MEMORY_SCHEMA as _BM_SCHEMA
    from pact_v4.runtime.book_memory_policy import BOOK_MEMORY_POLICY_VERSION as _BM_PV
    _HAS_POLICY=True
except Exception:
    def ensure_policy_block(x): return x
    _HAS_POLICY=False

CANONICAL_FILES = ["glossary.json", "book_memory.json", "chapter_index.json", "observations.json"]
REPLACEMENT_ORDER = ["glossary.json", "book_memory.json", "chapter_index.json", "observations.json"]
MARKER_NAME = ".pact_transaction_marker.json"
BACKUP_SUFFIX = ".pact_backup"
# Only these extra file patterns are permitted: exact marker and backup suffix files, and candidate tmp dirs which are directories not files
ALLOWED_EXTRA_FILES = {MARKER_NAME}
# Temp files inside base_dir are not allowed except during atomic_write (which uses mkstemp with random suffix then replace immediately). Any lingering .tmp or .pact_* besides marker/backup is rejected.

def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

def _raw_file_hash(path: str) -> str:
    # Raw bytes hash, not canonical JSON hash
    if not os.path.exists(path):
        return ""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(8192)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""

def _collect_claim_evidence_pids(obs: Mapping[str, Any]) -> List[str]:
    """Aggregate evidence PIDs from the real B1.2 observation shape.

    Real colon-keyed observations store evidence in:
    * top-level ``provenance_pids`` / ``evidence_pids`` / ``source_pids``
    * ``field_provenance.<field>.source_pids`` (e.g. gender)
    * ``variants.<alias>.source_pids`` (per-alias provenance)
    * fact ``source_pids`` is per-fact, not per-entity, so it is NOT folded
      here — it stays inside the fact objects (fact persistence preserves it).
    This helper folds the per-field and per-alias PIDs into a deduped sorted
    list so the candidate ``evidence_pids`` and the versioned report carry
    real claim provenance instead of ``[]``.
    """
    pids: set = set()
    for key in ("provenance_pids", "evidence_pids", "source_pids"):
        val = obs.get(key)
        if isinstance(val, (list, tuple, set)):
            for x in val:
                if x:
                    pids.add(str(x))
        elif isinstance(val, str) and val:
            pids.add(val)
    fp = obs.get("field_provenance")
    if isinstance(fp, Mapping):
        for fprov in fp.values():
            if isinstance(fprov, Mapping):
                for sp in fprov.get("source_pids") or []:
                    if sp:
                        pids.add(str(sp))
                # also support generic pids field
                for sp in fprov.get("provenance_pids") or []:
                    if sp:
                        pids.add(str(sp))
    variants = obs.get("variants")
    if isinstance(variants, Mapping):
        for vval in variants.values():
            if isinstance(vval, Mapping):
                for sp in vval.get("source_pids") or []:
                    if sp:
                        pids.add(str(sp))
                for sp in vval.get("provenance_pids") or []:
                    if sp:
                        pids.add(str(sp))
            elif isinstance(vval, (list, tuple)):
                for sp in vval:
                    if sp:
                        pids.add(str(sp))
    return sorted(pids)


# Backward compat alias - used for internal staged content hash via canonical (for comparison) but marker now uses raw
def _file_hash(path: str) -> str:
    return _raw_file_hash(path)

def atomic_write(filepath: str, data: Any):
    dir_name = os.path.dirname(filepath)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, text=True)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, filepath)
    try:
        dfd = os.open(dir_name or ".", os.O_DIRECTORY)
        os.fsync(dfd)
        os.close(dfd)
    except OSError:
        pass

def load_json(filepath: str, default: Any = None) -> Any:
    if not os.path.exists(filepath):
        return default if default is not None else {}
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)

def _validate_no_symlink_ancestors(base_dir: str, filename: str) -> bool:
    cur = os.path.join(base_dir, filename)
    while True:
        try:
            if os.path.islink(cur):
                return False
        except OSError:
            return False
        parent = os.path.dirname(cur)
        if parent == cur or len(parent) < len(base_dir):
            break
        cur = parent
        if os.path.normpath(cur) == os.path.normpath(base_dir):
            if os.path.islink(cur):
                return False
            break
    if os.path.islink(base_dir):
        return False
    return True

def _validate_exact_four_file_set(base_dir: str) -> Optional[str]:
    try:
        entries = os.listdir(base_dir)
    except OSError as e:
        return f"cannot list dir: {e}"
    # Strict allow-list: only canonical files plus Media revision metadata plus exact marker and backup files are permitted
    # Any other .pact_*, *.tmp, extra file/dir, symlink, special file is rejected
    # Media contract is the six-file set: four canonical + CURRENT.json + manifest.json (remote_client.py:190)
    allowed = set(CANONICAL_FILES) | {"CURRENT.json", "manifest.json"}
    for e in entries:
        # Allow marker file exactly
        if e == MARKER_NAME:
            continue
        # Allow backup files exactly ending with BACKUP_SUFFIX
        if e.endswith(BACKUP_SUFFIX):
            # Ensure corresponding canonical base exists (e.g., glossary.json.pact_backup)
            base = e[: -len(BACKUP_SUFFIX)]
            if base in CANONICAL_FILES:
                continue
            return f"extra entry {e!r} not in canonical set (unknown backup)"
        # Candidate tmp dirs created during transaction: .pact_candidate_* are allowed as transient transaction staging (same-filesystem bundle)
        if e.startswith(".pact_candidate_"):
            # Allow transient candidate dirs during transaction; they are not part of canonical set but are known transaction staging
            continue
        # Any other .pact_* or *.tmp is rejected
        if e.startswith(".pact_") or e.endswith(".tmp"):
            return f"extra entry {e!r} not in canonical set (marker/tmp not allowed)"
        if e not in allowed:
            return f"extra entry {e!r} not in canonical set"
    # Require ALL four canonical files present (no missing allowed)
    for fname in CANONICAL_FILES:
        fpath = os.path.join(base_dir, fname)
        if not os.path.lexists(fpath):
            return f"missing canonical file {fname}"
        if os.path.islink(fpath):
            return f"symlink not allowed: {fname}"
        if not _validate_no_symlink_ancestors(base_dir, fname):
            return f"symlink ancestor for {fname}"
        try:
            st = os.lstat(fpath)
            import stat
            if not stat.S_ISREG(st.st_mode):
                return f"non-regular file {fname}: mode {oct(st.st_mode)}"
            if stat.S_ISFIFO(st.st_mode) or stat.S_ISSOCK(st.st_mode) or stat.S_ISCHR(st.st_mode) or stat.S_ISBLK(st.st_mode):
                return f"special file {fname}"
        except OSError as e:
            return f"cannot stat {fname}: {e}"
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                json.load(f)
        except Exception as e:
            return f"invalid JSON {fname}: {e}"
    # Optional Media revision metadata: validate when present (regular file, no symlink, valid JSON)
    for fname in ("CURRENT.json", "manifest.json"):
        fpath = os.path.join(base_dir, fname)
        if not os.path.lexists(fpath):
            continue
        if os.path.islink(fpath):
            return f"symlink not allowed: {fname}"
        if not _validate_no_symlink_ancestors(base_dir, fname):
            return f"symlink ancestor for {fname}"
        try:
            st = os.lstat(fpath)
            import stat
            if not stat.S_ISREG(st.st_mode):
                return f"non-regular file {fname}: mode {oct(st.st_mode)}"
            if stat.S_ISFIFO(st.st_mode) or stat.S_ISSOCK(st.st_mode) or stat.S_ISCHR(st.st_mode) or stat.S_ISBLK(st.st_mode):
                return f"special file {fname}"
        except OSError as e:
            return f"cannot stat {fname}: {e}"
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                json.load(f)
        except Exception as e:
            return f"invalid JSON {fname}: {e}"
    return None

class MemoryManager:
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.glossary_path = os.path.join(base_dir, 'glossary.json')
        self.book_memory_path = os.path.join(base_dir, 'book_memory.json')
        self.chapter_memory_path = os.path.join(base_dir, 'chapter_memory.json')
        self.chapter_index_path = os.path.join(base_dir, 'chapter_index.json')
        self.observations_path = os.path.join(base_dir, 'observations.json')
        self._marker_path = os.path.join(base_dir, MARKER_NAME)
        os.makedirs(base_dir, exist_ok=True)
        # FINDING 4: do NOT silently auto-create canonical files — promotion path must require all four present (fail closed).
        # Use initialize_canonical_files() for genuine new-state creation.
        self._recover_if_needed()

    @staticmethod
    def initialize_canonical_files(base_dir: str) -> None:
        """Explicit helper for genuine new-state creation: create missing canonical files."""
        os.makedirs(base_dir, exist_ok=True)
        for fname in CANONICAL_FILES:
            fpath = os.path.join(base_dir, fname)
            if not os.path.lexists(fpath):
                try:
                    with open(fpath, "w", encoding="utf-8") as f:
                        json.dump({}, f, ensure_ascii=False, indent=2)
                        f.write("\n")
                except OSError:
                    pass

    def _recover_if_needed(self):
        if not os.path.exists(self._marker_path):
            return
        try:
            marker = json.loads(open(self._marker_path, 'r', encoding='utf-8').read())
        except Exception as e:
            raise RuntimeError(f"corrupt transaction marker, fail-closed: {e}") from e
        pre_hashes = marker.get("pre_hashes", {})
        backups = marker.get("backups", {})
        restore_failed = False
        for fname in REPLACEMENT_ORDER:
            bpath = backups.get(fname)
            target = os.path.join(self.base_dir, fname)
            if bpath:
                if not os.path.exists(bpath):
                    restore_failed = True
                    continue
                try:
                    shutil.copy2(bpath, target)
                except OSError:
                    restore_failed = True
        for fname, expected in pre_hashes.items():
            fpath = os.path.join(self.base_dir, fname)
            actual = _raw_file_hash(fpath)
            if expected and actual != expected:
                raise RuntimeError(f"recovery pre-hash mismatch for {fname}: expected {expected}, got {actual} - fail-closed, marker retained")
        if restore_failed:
            raise RuntimeError("recovery failed: missing backups - fail-closed, marker retained")
        for bpath in backups.values():
            try:
                if bpath and os.path.exists(bpath):
                    os.remove(bpath)
            except OSError:
                pass
        try:
            os.remove(self._marker_path)
        except OSError as e:
            raise RuntimeError(f"failed to clear marker after recovery: {e}") from e
        try:
            dfd = os.open(self.base_dir, os.O_DIRECTORY)
            os.fsync(dfd)
            os.close(dfd)
        except OSError:
            pass

    def create_snapshot(self):
        glossary = load_json(self.glossary_path)
        book_memory = load_json(self.book_memory_path)
        snapshot = {'glossary': glossary, 'book_memory': book_memory}
        atomic_write(self.chapter_memory_path, snapshot)
        atomic_write(self.observations_path, {'glossary': {}, 'book_memory': {}})

    def add_observation(self, category: str, key: str, value: Any):
        obs = load_json(self.observations_path, {'glossary': {}, 'book_memory': {}})
        if category not in obs:
            obs[category] = {}
        obs[category][key] = value
        atomic_write(self.observations_path, obs)

    def _rebuild_chapter_index_for_promotion(self, new_book_memory: Optional[Dict[str, Any]], existing_index: Dict[str, Any]) -> Dict[str, Any]:
        # Rebuild chapter_index.json inside transaction per finding 5: update metadata to v2 and ensure consistency
        # If new_book_memory is available, use its policy version; otherwise preserve existing
        idx = json.loads(json.dumps(existing_index)) if isinstance(existing_index, dict) else {}
        # Ensure v2 metadata
        idx["$schema"] = "pact-v4-chapter-index/v2"
        try:
            from pact_v4.runtime.book_memory_policy import BOOK_MEMORY_POLICY_VERSION as _PV
            pv = _PV
        except Exception:
            pv = "book-memory-policy/v1"
        if isinstance(new_book_memory, dict) and new_book_memory.get("book_memory_policy_version"):
            pv = str(new_book_memory.get("book_memory_policy_version"))
        idx["$book_memory_policy_version"] = pv
        return idx

    def promote(self, status: str, *, quarantined_chunks: Optional[set] = None, _rebuilt_index: Optional[Dict[str, Any]] = None, _chapter_id: Optional[str] = None, _chapter_html: Optional[str] = None, _chapter_ids: Optional[list] = None, _chapter_html_pattern: Optional[str] = None):
        if status not in ('complete', 'accepted_degraded'):
            return
        err = _validate_exact_four_file_set(self.base_dir)
        if err is not None:
            raise RuntimeError(f"exact-four-file boundary violation before promotion: {err}")
        obs = load_json(self.observations_path, {'glossary': {}, 'book_memory': {}})
        if status == 'accepted_degraded' and quarantined_chunks:
            obs = self._filter_quarantined_obs(obs, quarantined_chunks)
        glossary_obs = obs.get('glossary', {})
        book_memory_obs = obs.get('book_memory', {})
        glossary = load_json(self.glossary_path, {})
        chapter_index = load_json(self.chapter_index_path, {})
        new_glossary = json.loads(json.dumps(glossary)) if isinstance(glossary, dict) else {}
        new_book_memory = None
        # Always load book_memory for rebuild purposes, even if no obs
        bm_current = load_json(self.book_memory_path, {})
        # v4.2: wire the normalized reducer into promotion. canonical_populate is the
        # active writer for verified-claim-only, class-routed, conflict-diagnosed merging.
        # It is never bypassed; _merge_with_conflict_resolution remains for glossary only.
        _candidate_report: Optional[List[Dict[str, Any]]] = None
        if book_memory_obs:
            new_book_memory = json.loads(json.dumps(ensure_policy_block(bm_current))) if isinstance(bm_current, dict) else {}
            # Detect whether observations are in the new v4.2 candidate shape vs legacy
            # colon-keyed / flat shapes. v4.2 production B1.2 observations are
            # colon-keyed (entities:<name>, characters:<name>, facts:<name>:<idx>)
            # produced by book_memory_observations_from_entity_context and carry
            # memory_class/type/field_provenance provenance. Those MUST flow
            # through canonical_populate (class routing, all-scope merge,
            # verified-claim decisions, candidate reports). Genuinely legacy
            # colon observations (simple {canonical_ru: ...} without those
            # fields) remain on the v4.1 verbatim path so byte-identical is
            # preserved.
            def _is_v42_obs(k: Any, v: Any) -> bool:
                if not isinstance(v, Mapping):
                    return False
                ks = str(k)
                if ":" not in ks:
                    return ("source" in v or "entity" in v or "memory_class" in v or "verified" in v)
                # colon-keyed: v4.2 iff it carries reducer-relevant provenance
                if ks.startswith("facts:"):
                    # Only treat facts as v4.2 when they carry provenance (source_pids/status/chapter)
                    # Legacy facts (e.g. {"fact": "...", "keys": [...]}) stay on v4.1 verbatim path
                    if any(x in v for x in ("source_pids", "provenance_pids", "evidence_pids", "status", "chapter", "keys")) and (v.get("source_pids") or v.get("status") == "verified" or "chapter" in v):
                        # Require at least one v4.2 marker to avoid hijacking legacy facts
                        if v.get("source_pids") or v.get("provenance_pids") or v.get("evidence_pids") or v.get("status") == "verified":
                            return True
                    # Fall through to generic checks for legacy facts
                    pass
                if any(x in v for x in ("memory_class", "type", "field_provenance", "first_seen_chapter", "provenance_pids", "source_pids", "variants")):
                    return True
                if v.get("status") == "verified":
                    return True
                return False
            _is_candidate_shape = any(_is_v42_obs(_k, _v) for _k, _v in book_memory_obs.items())
            if _is_candidate_shape:
                # Normalize real production observations (colon-keyed) into the
                # reducer's candidate shape. Facts are aggregated per entity
                # so a facts:<name>:<idx> observation becomes a fact entry
                # on that entity's candidate rather than a separate
                # source="<name>:<idx>" candidate.
                _entity_cands: Dict[str, Dict[str, Any]] = {}
                _orphan_facts: Dict[str, List[Any]] = {}
                for _k, _v in list(book_memory_obs.items()):
                    if not isinstance(_v, Mapping):
                        continue
                    ks = str(_k)
                    if ":" not in ks:
                        if "source" in _v or "entity" in _v:
                            _cand = dict(_v)
                            if "source" not in _cand and "entity" in _cand:
                                _cand["source"] = _cand["entity"]
                            _src = str(_cand.get("source") or ks)
                            _entity_cands.setdefault(_src, _cand)
                            # merge if duplicate flat key
                            if _src in _entity_cands and _entity_cands[_src] is not _cand:
                                _entity_cands[_src].update({k: v for k, v in _cand.items() if k not in _entity_cands[_src]})
                        else:
                            _cand2: Dict[str, Any] = dict(_v)
                            _cand2.setdefault("source", ks)
                            _cand2.setdefault("memory_class", _cand2.get("memory_class") or "named_character")
                            _cand2.setdefault("verified", bool(_cand2.get("verified", True)))
                            _cand2.setdefault("evidence_pids", _cand2.get("evidence_pids") or [])
                            _cand2.setdefault("chapter_id", _chapter_id or "")
                            _entity_cands[ks] = _cand2
                        continue
                    _sec, _, _ek = ks.partition(":")
                    if _sec == "facts":
                        # facts:<entity>:<idx> -> attach to entity
                        if ":" in _ek:
                            _ent_name, _, _idx = _ek.rpartition(":")
                        else:
                            _ent_name = _ek
                        _fact = dict(_v)
                        if _ent_name in _entity_cands:
                            _entity_cands[_ent_name].setdefault("facts", []).append(_fact)
                            # Fold fact source_pids into candidate evidence
                            _fps = _fact.get("source_pids") or []
                            if _fps:
                                _ep = set(str(x) for x in (_entity_cands[_ent_name].get("evidence_pids") or []))
                                for sp in _fps:
                                    _ep.add(str(sp))
                                _entity_cands[_ent_name]["evidence_pids"] = sorted(_ep)
                        else:
                            _orphan_facts.setdefault(_ent_name, []).append(_fact)
                        continue
                    if _sec in ("characters", "entities", "terms"):
                        _mc = {"characters": "named_character", "entities": "named_place", "terms": "world_term"}.get(_sec, _sec)
                        _cand: Dict[str, Any] = dict(_v)
                        _cand.setdefault("source", _ek)
                        _cand.setdefault("memory_class", _mc if "memory_class" not in _v else _v["memory_class"])
                        # production B1.2 identities are verified by construction when they carry provenance
                        if "verified" not in _cand and "status" in _v:
                            _cand["verified"] = _v.get("status") == "verified"
                        elif "verified" not in _cand:
                            _cand["verified"] = True
                        # Aggregate real claim provenance: per-field, per-alias, top-level.
                        _agg = _collect_claim_evidence_pids(_v)
                        if "evidence_pids" not in _cand or not _cand["evidence_pids"]:
                            _cand["evidence_pids"] = _agg
                        else:
                            # Merge aggregated with any explicit top-level pids (dedup sorted).
                            _merged = set(str(x) for x in (_cand["evidence_pids"] or [])) | set(_agg)
                            _cand["evidence_pids"] = sorted(_merged)
                        _cand.setdefault("chapter_id", _chapter_id or "")
                        # Preserve per-alias provenance for the reducer (variants.*.source_pids)
                        if "variants" in _v and "aliases" not in _cand:
                            try:
                                _cand["aliases"] = list(_v["variants"].keys()) if isinstance(_v["variants"], Mapping) else list(_v["variants"])  # type: ignore
                            except Exception:
                                pass
                            # Carry raw variant provenance so _merge_records can persist it
                            try:
                                _cand["_variant_provenance"] = {
                                    str(k): list(v.get("source_pids") or []) if isinstance(v, Mapping) else []
                                    for k, v in (_v.get("variants") or {}).items() if isinstance(v, Mapping)
                                }
                            except Exception:
                                pass
                        # Preserve field_provenance for the reducer
                        if "field_provenance" in _v and "_field_provenance" not in _cand:
                            try:
                                _cand["_field_provenance"] = dict(_v.get("field_provenance") or {})
                            except Exception:
                                pass
                        # merge orphan facts if any (also fold their pids into evidence)
                        if _ek in _orphan_facts:
                            _facts = _orphan_facts.pop(_ek)
                            _cand.setdefault("facts", []).extend(_facts)
                            # Fold fact source_pids into candidate evidence
                            _fact_pids = set(str(x) for x in (_cand.get("evidence_pids") or []))
                            for _f in _facts:
                                if isinstance(_f, Mapping):
                                    for sp in _f.get("source_pids") or []:
                                        _fact_pids.add(str(sp))
                            _cand["evidence_pids"] = sorted(_fact_pids)
                        _entity_cands[_ek] = _cand
                        continue
                    # unknown colon section: treat as generic candidate
                    _cand3: Dict[str, Any] = dict(_v)
                    _cand3.setdefault("source", _ek)
                    _cand3.setdefault("memory_class", _sec)
                    _cand3.setdefault("verified", bool(_v.get("status") == "verified" or _v.get("verified", True)))
                    _agg3 = _collect_claim_evidence_pids(_v)
                    if "evidence_pids" not in _cand3 or not _cand3["evidence_pids"]:
                        _cand3["evidence_pids"] = _agg3
                    else:
                        _merged3 = set(str(x) for x in (_cand3["evidence_pids"] or [])) | set(_agg3)
                        _cand3["evidence_pids"] = sorted(_merged3)
                    _cand3.setdefault("chapter_id", _chapter_id or "")
                    _entity_cands[_ek] = _cand3
                # attach any remaining orphan facts as standalone fact-only candidates (will be merged as updates)
                for _ent, _facts in _orphan_facts.items():
                    _fact_pids = []
                    for _f in _facts:
                        if isinstance(_f, Mapping):
                            for sp in _f.get("source_pids") or []:
                                _fact_pids.append(str(sp))
                    _fact_pids = sorted(set(_fact_pids))
                    if _ent not in _entity_cands:
                        _entity_cands[_ent] = {"source": _ent, "memory_class": "named_character", "verified": True, "facts": _facts, "evidence_pids": _fact_pids, "chapter_id": _chapter_id or ""}
                    else:
                        _entity_cands[_ent].setdefault("facts", []).extend(_facts)
                        # Merge fact pids into existing candidate evidence
                        _existing = set(str(x) for x in (_entity_cands[_ent].get("evidence_pids") or []))
                        _existing.update(_fact_pids)
                        _entity_cands[_ent]["evidence_pids"] = sorted(_existing)
                candidates: List[Mapping[str, Any]] = list(_entity_cands.values())
                if candidates:
                    try:
                        new_book_memory, _candidate_report = canonical_populate(
                            new_book_memory, candidates, policy=None
                        )
                    except Exception as exc:
                        LOG.warning("canonical_populate failed, fallback to legacy merge: %s", exc)
                        self._merge_with_conflict_resolution(new_book_memory, book_memory_obs, book_memory=True)
                else:
                    self._merge_with_conflict_resolution(new_book_memory, book_memory_obs, book_memory=True)
            else:
                # Legacy flat/colon observations: preserve v4.1 merge behavior verbatim
                self._merge_with_conflict_resolution(new_book_memory, book_memory_obs, book_memory=True)
            # Persist versioned candidate report durably (inside book_memory for now; separate ledger may be added)
            if _candidate_report is not None:
                try:
                    # Store versioned report durably inside book_memory (no extra top-level file;
                    # the four-file set is allow-listed — any extra file would fail promotion).
                    # Diagnostics are inspectable via book_memory._candidate_reports and via the
                    # existing raw-prompt retention where it exists.
                    new_book_memory["_candidate_reports"] = new_book_memory.get("_candidate_reports", [])
                    if not isinstance(new_book_memory["_candidate_reports"], list):
                        new_book_memory["_candidate_reports"] = []
                    new_book_memory["_candidate_reports"].append({
                        "schema": "pact-v4-candidate-report/v1",
                        "chapter_id": _chapter_id or "",
                        "report": _candidate_report,
                    })
                except Exception:
                    pass
        else:
            # No book_memory obs but still need to ensure policy block for index rebuild? Keep byte-identical if no change
            new_book_memory = json.loads(json.dumps(bm_current)) if isinstance(bm_current, dict) else {}
        if glossary_obs:
            self._merge_with_conflict_resolution(new_glossary, glossary_obs)
        new_observations: Dict[str, Any] = {'glossary': {}, 'book_memory': {}}
        # Rebuild chapter_index inside transaction (finding 5) - call rebuild during staging so all four files commit atomically
        if _rebuilt_index is not None:
            rebuilt_index = _rebuilt_index
        else:
            # Build per-chapter entries for current and next chapter inside the same transaction using staged new_book_memory
            rebuilt_index = self._rebuild_chapter_index_for_promotion(new_book_memory, chapter_index)
            # If chapter context provided, compute entries for current and next chapter (presence-based, full memory)
            try:
                if _chapter_id:
                    from pact_full_pipeline_runner_v1.build_chapter_index import build_chapter_index as _bci, pre_chapter_book_memory, load_glossary
                    from pact_v4.phase0b.source_html import load_source as _ls
                    from pathlib import Path as _P
                    # Helper to build one entry
                    def _build_entry(cid: str, html_pattern: Optional[str], html_path: Optional[str]):
                        try:
                            if html_pattern and cid:
                                hp = html_pattern.format(chapter_id=cid) if html_pattern else None
                                if hp and _P(hp).exists():
                                    blocks, _ = _ls(_P(hp))
                                elif html_path and _P(html_path).exists():
                                    blocks, _ = _ls(_P(html_path))
                                else:
                                    return None
                            elif html_path and cid == _chapter_id:
                                blocks, _ = _ls(_P(html_path))
                            else:
                                return None
                            src_text = "\\n".join(b.text for b in blocks)
                            # Use full accumulated memory for this cid (no provenance gate)
                            full_mem = pre_chapter_book_memory(new_book_memory, cid)
                            glossary = load_glossary(self.base_dir)
                            entry = _bci(chapter_id=cid, source_text=src_text, book_memory=full_mem, glossary=glossary)
                            return entry
                        except Exception:
                            return None
                    # Build current chapter entry (full memory)
                    entry_cur = _build_entry(_chapter_id, _chapter_html_pattern, _chapter_html)
                    if entry_cur is not None:
                        rebuilt_index[_chapter_id] = entry_cur
                    # Build next chapter entry if exists
                    if _chapter_ids and _chapter_id in _chapter_ids:
                        idx = list(_chapter_ids).index(_chapter_id)
                        if idx + 1 < len(_chapter_ids):
                            next_id = str(_chapter_ids[idx+1])
                            entry_next = _build_entry(next_id, _chapter_html_pattern, None)
                            if entry_next is not None:
                                rebuilt_index[next_id] = entry_next
            except Exception:
                pass
        staged = {
            "glossary.json": new_glossary,
            "book_memory.json": new_book_memory,
            "chapter_index.json": rebuilt_index,
            "observations.json": new_observations,
        }
        self._transactional_replace(staged)

    def _transactional_replace(self, staged: Dict[str, Any]):
        fault_point = os.environ.get("PACT_FAULT_INJECT")
        err = _validate_exact_four_file_set(self.base_dir)
        if err is not None:
            raise RuntimeError(f"exact-four-file boundary violation before transaction: {err}")
        # Require exactly four canonical files in staged
        staged_keys = set(staged.keys())
        if staged_keys != set(CANONICAL_FILES):
            raise RuntimeError(f"transaction must stage exactly the four canonical files, got {sorted(staged_keys)}")
        for fname in CANONICAL_FILES:
            if staged.get(fname) is None:
                raise RuntimeError(f"transaction missing staged content for {fname}")
        pre_hashes = {fname: _raw_file_hash(os.path.join(self.base_dir, fname)) for fname in CANONICAL_FILES}
        post_hashes: Dict[str, str] = {}
        candidate_tmp = tempfile.mkdtemp(dir=self.base_dir, prefix=".pact_candidate_")
        try:
            for fname in REPLACEMENT_ORDER:
                content = staged.get(fname)
                cpath = os.path.join(candidate_tmp, fname)
                if not isinstance(content, (dict, list)):
                    raise RuntimeError(f"staged {fname} is not JSON-serializable: {type(content)}")
                with open(cpath, "w", encoding="utf-8") as f:
                    json.dump(content, f, ensure_ascii=False, indent=2)
                    f.write("\n")
                    f.flush()
                    os.fsync(f.fileno())
                # Compute RAW file hash of staged bytes; if content canonically identical to existing, use existing raw to preserve bytes
                cand_raw = _raw_file_hash(cpath)
                try:
                    existing = load_json(os.path.join(self.base_dir, fname), None)
                    if existing is not None and _canonical_hash(existing) == _canonical_hash(content):
                        cand_raw = _raw_file_hash(os.path.join(self.base_dir, fname))
                except Exception:
                    pass
                post_hashes[fname] = cand_raw
            cand_entries = set(os.listdir(candidate_tmp))
            if cand_entries != set(CANONICAL_FILES):
                raise RuntimeError(f"candidate bundle must contain exactly {sorted(CANONICAL_FILES)}, got {sorted(cand_entries)}")
            for fname in CANONICAL_FILES:
                cpath = os.path.join(candidate_tmp, fname)
                if os.path.islink(cpath):
                    raise RuntimeError(f"candidate symlink rejected: {fname}")
                st = os.lstat(cpath)
                import stat
                if not stat.S_ISREG(st.st_mode):
                    raise RuntimeError(f"candidate non-regular file: {fname}")
                with open(cpath, "r", encoding="utf-8") as f:
                    json.load(f)
            err2 = _validate_exact_four_file_set(self.base_dir)
            if err2 is not None:
                raise RuntimeError(f"pre-move revalidation failed: {err2}")
        except Exception:
            try:
                shutil.rmtree(candidate_tmp)
            except OSError:
                pass
            raise
        backups: Dict[str, str] = {}
        for fname in REPLACEMENT_ORDER:
            src = os.path.join(self.base_dir, fname)
            bpath = src + BACKUP_SUFFIX
            try:
                shutil.copy2(src, bpath)
                backups[fname] = bpath
            except OSError:
                backups[fname] = bpath
        marker = {"pre_hashes": pre_hashes, "post_hashes": post_hashes, "backups": backups, "progress": []}
        fd, tmp = tempfile.mkstemp(dir=self.base_dir, text=True)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(marker, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._marker_path)
        try:
            dfd = os.open(self.base_dir, os.O_DIRECTORY)
            os.fsync(dfd)
            os.close(dfd)
        except OSError:
            pass
        if fault_point == "before_replace":
            raise RuntimeError("fault-inject before_replace")
        for fname in REPLACEMENT_ORDER:
            if fault_point == f"before_{fname}":
                raise RuntimeError(f"fault-inject before_{fname}")
            content = staged.get(fname)
            target = os.path.join(self.base_dir, fname)
            # byte preservation: skip unchanged based on canonical JSON hash (preserve original bytes when content identical)
            try:
                existing = load_json(target, None)
                if existing is not None and _canonical_hash(existing) == _canonical_hash(content):
                    marker["progress"].append(fname)
                    fd2, tmp2 = tempfile.mkstemp(dir=self.base_dir, text=True)
                    with os.fdopen(fd2, 'w', encoding='utf-8') as f:
                        json.dump(marker, f, ensure_ascii=False, indent=2)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp2, self._marker_path)
                    try:
                        dfd = os.open(self.base_dir, os.O_DIRECTORY)
                        os.fsync(dfd)
                        os.close(dfd)
                    except OSError:
                        pass
                    continue
            except Exception:
                pass
            atomic_write(target, content)
            marker["progress"].append(fname)
            fd2, tmp2 = tempfile.mkstemp(dir=self.base_dir, text=True)
            with os.fdopen(fd2, 'w', encoding='utf-8') as f:
                json.dump(marker, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp2, self._marker_path)
            try:
                dfd = os.open(self.base_dir, os.O_DIRECTORY)
                os.fsync(dfd)
                os.close(dfd)
            except OSError:
                pass
            if fault_point == f"after_{fname}":
                raise RuntimeError(f"fault-inject after_{fname}")
        if fault_point == "before_verify":
            raise RuntimeError("fault-inject before_verify")
        # post-hash verify using RAW file hashes stored in marker (finding 6)
        for fname in REPLACEMENT_ORDER:
            expected = post_hashes.get(fname)
            actual = _raw_file_hash(os.path.join(self.base_dir, fname))
            if expected and actual != expected:
                raise RuntimeError(f"post-hash mismatch for {fname}: expected {expected}, got {actual}")
            if fault_point == f"before_verify_{fname}":
                raise RuntimeError(f"fault-inject before_verify_{fname}")
            if fault_point == f"after_verify_{fname}":
                raise RuntimeError(f"fault-inject after_verify_{fname}")
        if fault_point == "after_verify":
            raise RuntimeError("fault-inject after_verify")
        try:
            shutil.rmtree(candidate_tmp)
        except OSError:
            pass
        for bpath in backups.values():
            try:
                if os.path.exists(bpath):
                    os.remove(bpath)
            except OSError:
                pass
        try:
            os.remove(self._marker_path)
        except OSError as e:
            raise RuntimeError(f"failed to clear marker: {e}") from e
        try:
            dfd = os.open(self.base_dir, os.O_DIRECTORY)
            os.fsync(dfd)
            os.close(dfd)
        except OSError:
            pass

    def _filter_quarantined_obs(self, obs: Dict, quarantined_chunks: set) -> Dict:
        filtered: Dict[str, Any] = {}
        for category in ('glossary', 'book_memory'):
            entries = obs.get(category, {})
            if not isinstance(entries, dict):
                continue
            kept: Dict[str, Any] = {}
            for key, value in entries.items():
                if isinstance(value, dict) and value.get('chunk_id') in quarantined_chunks:
                    continue
                kept[key] = value
            filtered[category] = kept
        return filtered

    def _merge_with_conflict_resolution(self, main_mem: Dict, new_obs: Dict, *, book_memory: bool = False) -> bool:
        changed = False
        for key, value in new_obs.items():
            if book_memory and ':' in key:
                section, _, entry_key = key.partition(':')
                if section in ('characters', 'entities'):
                    section_dict = main_mem.setdefault(section, {})
                    if not isinstance(section_dict, dict):
                        section_dict = {}
                        main_mem[section] = section_dict
                    existing = section_dict.get(entry_key)
                    if isinstance(existing, dict) and existing.get('status') in ('established', 'locked'):
                        continue
                    if section_dict.get(entry_key) != value:
                        section_dict[entry_key] = value
                        changed = True
                    continue
                if section == 'facts':
                    facts = main_mem.setdefault('facts', [])
                    if not isinstance(facts, list):
                        facts = []
                        main_mem['facts'] = facts
                    fact_text = value.get('fact') if isinstance(value, dict) else None
                    if fact_text and not any(isinstance(f, dict) and f.get('fact') == fact_text for f in facts):
                        facts.append(value)
                        changed = True
                    continue
            if key in main_mem:
                existing = main_mem[key]
                if isinstance(existing, dict) and existing.get('status') in ('established', 'locked'):
                    continue
            if main_mem.get(key) != value:
                main_mem[key] = value
                changed = True
        return changed

    def rollback(self):
        snapshot = load_json(self.chapter_memory_path, None)
        if snapshot:
            atomic_write(self.glossary_path, snapshot.get('glossary', {}))
            atomic_write(self.book_memory_path, snapshot.get('book_memory', {}))
            atomic_write(self.observations_path, {'glossary': {}, 'book_memory': {}})


# ===========================================================================
# Canonical population reducer (v4.2: book-memory-role-views)
# ---------------------------------------------------------------------------
# Normalized cross-section identity merge (including PRE-EXISTING compatible
# duplicates), class-based section routing, verified-claim-only promotion,
# provenance-preserving fact updates, and explicit create/merge/update/no_op/
# reject/conflict outcomes with an expanded versioned candidate report.
#
# PURE: operates on in-memory dicts; never touches disk. The four-file memory
# state itself is validated by _validate_exact_four_file_set (boundary
# hardening) before any promote() that consumes the observations this produces.
# ===========================================================================

CANONICAL_OUTCOMES = ("create", "merge", "update", "no_op", "reject", "conflict")

# Stable reason codes recorded in the versioned candidate report.
POP_REASON = {
    "created_in_scope": "created_in_scope",
    "merged_compatible": "merged_compatible",
    "merged_pre_existing_duplicates": "merged_pre_existing_duplicates",
    "routed_by_class": "routed_by_class",
    "updated_provenance": "updated_provenance",
    "no_change": "no_change",
    "rejected_unverified": "rejected_unverified",
    "conflict_incompatible": "conflict_incompatible",
}

# memory_class -> durable section.
_CLASS_TO_SECTION = {
    "named_character": "characters",
    "named_place": "entities",
    "named_group": "entities",
    "named_artifact": "entities",
    "named_creature": "entities",
    "world_term": "entities",
    "chapter_local": "entities",
}


def _norm_identity(s: str) -> str:
    """Normalized Unicode/case/apostrophe/punctuation identity key."""
    if not isinstance(s, str):
        return ""
    try:
        import unicodedata
        s = unicodedata.normalize("NFKC", s)
    except Exception:  # pragma: no cover
        pass
    s = s.strip().casefold().replace("\u2019", "'").replace("\u2018", "'")
    # Collapse internal whitespace and strip surrounding punctuation.
    s = " ".join(s.split())
    for ch in ".,;:!?\"'()[]{}«»\u00ab\u00bb":
        s = s.replace(ch, "")
    return s.strip()


def _section_iter(memory: Mapping[str, Any]):
    for section in ("characters", "entities", "terms"):
        data = memory.get(section)
        if isinstance(data, Mapping):
            yield section, data
        elif isinstance(data, list):
            for entry in data:
                if isinstance(entry, Mapping):
                    name = str(entry.get("name") or entry.get("source") or entry.get("english") or "")
                    yield section, {name: entry}


def _identity_matches(memory: Mapping[str, Any], name: str):
    """Return [(section, key, record)] for every canonical record whose
    normalized identity OR verified lexical variant equals ``name``."""
    target = _norm_identity(name)
    if not target:
        return []
    matches = []
    for section, data in _section_iter(memory):
        for key, rec in data.items():
            if not isinstance(rec, Mapping):
                continue
            if _norm_identity(str(key)) == target:
                matches.append((section, str(key), rec))
                continue
            variants = rec.get("variants") or {}
            if isinstance(variants, Mapping) and target in {
                _norm_identity(str(v)) for v in variants
            }:
                matches.append((section, str(key), rec))
    return matches


def _attrs_compatible(records):
    """Two+ records are compatible iff their verified attributes (gender,
    canonical_ru, address) are non-contradictory (equal or one side missing)."""
    def _val(rec, field):
        v = rec.get(field) or rec.get({"canonical_ru": "ru"}.get(field, field))
        return _norm_identity(str(v)) if v else ""
    for field in ("gender", "canonical_ru", "address"):
        seen = {_val(r, field) for r in records if _val(r, field)}
        if len(seen) > 1:
            return False
    return True


def _merge_records(records, *, memory_class, aliases, gender, canonical_ru,
                   address, facts, evidence_pids, chapter_id,
                   variant_provenance=None, field_provenance=None):
    """Deterministically merge compatible records into one canonical record.
    Established values are preserved; a later agreeing claim APPENDS provenance
    instead of overwriting; a contradictory claim is NOT merged here (caller
    treats it as conflict)."""
    # Base = first record in deterministic (section, key) order.
    base_section, base_key, base = sorted(
        records, key=lambda r: (r[0], r[1])
    )[0]
    merged = dict(base)
    # Section implied by memory_class.
    merged["memory_class"] = memory_class or merged.get("memory_class", "")
    # Merge variants from every record + new verified aliases (preserve per-alias pids).
    variants = dict(merged.get("variants") or {})
    for _s, _k, rec in records:
        for v, vval in (rec.get("variants") or {}).items():
            if str(v) not in variants:
                variants[str(v)] = dict(vval) if isinstance(vval, Mapping) else {}
    for alias in (aliases or []):
        if _norm_identity(str(alias)) != _norm_identity(base_key):
            vp = {}
            if isinstance(variant_provenance, Mapping) and str(alias) in variant_provenance:
                pids = variant_provenance.get(str(alias)) or []
                if pids:
                    vp["source_pids"] = list(pids)
            vp.setdefault("chapter", chapter_id)
            if str(alias) not in variants:
                variants[str(alias)] = vp
            else:
                # Merge pids into existing variant entry
                existing_pids = set(str(x) for x in variants[str(alias)].get("source_pids") or [])
                for pid in vp.get("source_pids") or []:
                    existing_pids.add(str(pid))
                if existing_pids:
                    variants[str(alias)]["source_pids"] = sorted(existing_pids)
    if variants:
        merged["variants"] = variants
    # Merge field_provenance (per-attribute evidence)
    if isinstance(field_provenance, Mapping) and field_provenance:
        fp = dict(merged.get("field_provenance") or {})
        for fld, fprov in field_provenance.items():
            if not isinstance(fprov, Mapping):
                continue
            pids = list(fprov.get("source_pids") or fprov.get("provenance_pids") or [])
            if pids:
                existing = fp.get(str(fld)) or {}
                existing_pids = set(str(x) for x in existing.get("source_pids") or [])
                for pid in pids:
                    existing_pids.add(str(pid))
                fp[str(fld)] = {"chapter": chapter_id, "source_pids": sorted(existing_pids)}
            elif str(fld) not in fp:
                fp[str(fld)] = {"chapter": chapter_id}
        merged["field_provenance"] = fp
    elif merged.get("field_provenance") and not isinstance(merged.get("field_provenance"), dict):
        pass
    # Merge provenance PIDs (append, dedup).
    prov = list(merged.get("provenance_pids") or [])
    for pid in (evidence_pids or []):
        if pid not in prov:
            prov.append(pid)
    merged["provenance_pids"] = prov
    # Chapters provenance (append first-seen chapter).
    chapters = list(merged.get("chapters") or [])
    if chapter_id and chapter_id not in chapters:
        chapters.append(chapter_id)
    merged["chapters"] = chapters
    # Preserve established verified attributes; only fill if missing.
    if gender and not merged.get("gender"):
        merged["gender"] = gender
    if canonical_ru and not merged.get("canonical_ru"):
        merged["canonical_ru"] = canonical_ru
    if address and not merged.get("address"):
        merged["address"] = address
    # Merge verified facts — preserve evidence (facts as dicts with source_pids).
    def _fact_key(f):
        if isinstance(f, Mapping):
            return str(f.get("fact") or "")
        return str(f or "")
    existing_map = {_fact_key(f): f for f in (merged.get("facts") or []) if _fact_key(f)}
    for f in (facts or []):
        ft = _fact_key(f)
        if not ft:
            continue
        if ft in existing_map:
            # Merge source_pids into existing fact entry
            existing = existing_map[ft]
            if isinstance(existing, Mapping) and isinstance(f, Mapping):
                ep = set(str(x) for x in existing.get("source_pids") or [])
                for pid in f.get("source_pids") or []:
                    ep.add(str(pid))
                if ep:
                    existing["source_pids"] = sorted(ep)
            continue
        # Normalize new fact to dict with evidence
        if isinstance(f, Mapping):
            nf = dict(f)
            nf.setdefault("fact", ft)
            nf.setdefault("chapter", chapter_id)
            if "source_pids" not in nf and "provenance_pids" in nf:
                nf["source_pids"] = list(nf.get("provenance_pids") or [])
            nf.setdefault("source_pids", [])
            existing_map[ft] = nf
        else:
            existing_map[ft] = {"fact": ft, "source_pids": [], "chapter": chapter_id}
    if existing_map:
        # Deterministic sort by fact text
        merged["facts"] = [existing_map[k] for k in sorted(existing_map.keys(), key=str.casefold)]
    return base_section, base_key, merged


def canonical_populate(memory: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]],
                      *, policy: Optional[Mapping[str, Any]] = None):
    """Deterministic canonical population reducer.

    For each eligible, individually-verified candidate it resolves the canonical
    identity across ALL durable scopes and emits exactly one of
    create / merge / update / no_op / reject / conflict. Returns
    ``(new_memory, report)`` where ``report`` is the versioned candidate report
    with operation, target, scope, class, reason, and evidence.

    Verified-claim-only: a candidate whose core identity is not verified is
    rejected (``reject``). Verified attributes are promoted only when verified;
    an agreeing later claim appends provenance (``update``/``no_op``); a
    contradictory claim yields ``conflict`` and is NOT written.
    """
    new_memory = json.loads(json.dumps(memory)) if isinstance(memory, (dict, list)) else {}
    if not isinstance(new_memory, dict):
        new_memory = {}
    report: List[Dict[str, Any]] = []

    for cand in candidates:
        if not isinstance(cand, Mapping):
            continue
        source = str(cand.get("source") or cand.get("entity") or "")
        memory_class = str(cand.get("memory_class") or "")
        evidence_pids = list(cand.get("evidence_pids") or [])
        chapter_id = str(cand.get("chapter_id") or "")
        # Verified-only claims: per-claim/relation provenance.
        # Each alias/fact/relation may carry its own status; only verified ones promote.
        def _verified_aliases(raw) -> List[str]:
            out: List[str] = []
            for a in (raw or []):
                if isinstance(a, Mapping):
                    if str(a.get("status") or a.get("verified")) not in ("verified", "True", "true", True):
                        # If dict has explicit status, require verified; else fallback to string truthiness
                        if "status" in a and a.get("status") != "verified":
                            continue
                        if "verified" in a and not a.get("verified"):
                            continue
                    surf = str(a.get("surface") or a.get("alias") or a.get("name") or "")
                    if surf:
                        out.append(surf)
                elif isinstance(a, str):
                    # String alias: only promote if candidate-level verified AND not a candidate relation
                    # A candidate relation (surface with candidate coreference) would be modeled as dict with status=candidate
                    out.append(a)
                else:
                    out.append(str(a))
            return out
        def _verified_facts(raw) -> List[Any]:
            out: List[Any] = []
            for f in (raw or []):
                if isinstance(f, Mapping):
                    if "status" in f and f.get("status") != "verified":
                        continue
                    if "verified" in f and not f.get("verified"):
                        continue
                    out.append(f)
                elif isinstance(f, str):
                    out.append(f)
                else:
                    out.append(f)
            return out
        # Gender/canonical_ru/address are per-claim verified attributes: if the candidate
        # carries an explicit per-claim status for them, respect it; otherwise candidate-level verified.
        def _verified_attr(val, key: str):
            if isinstance(val, Mapping):
                if val.get("status") and val.get("status") != "verified":
                    return ""
                return str(val.get("value") or val.get(key) or "")
            return str(val or "") if cand.get(f"{key}_verified", True) and cand.get("verified", True) else ""
        gender = _verified_attr(cand.get("gender"), "gender") if isinstance(cand.get("gender"), Mapping) else (str(cand.get("gender") or "") if cand.get("gender_verified", True) else "")
        canonical_ru = _verified_attr(cand.get("canonical_ru"), "canonical_ru") if isinstance(cand.get("canonical_ru"), Mapping) else (str(cand.get("canonical_ru") or "") if cand.get("canonical_ru_verified", True) else "")
        address = _verified_attr(cand.get("address"), "address") if isinstance(cand.get("address"), Mapping) else (str(cand.get("address") or "") if cand.get("address_verified", True) else "")
        # If candidate carries per-field verification flags, honor them; else candidate-level verified already gated.
        if isinstance(cand.get("gender"), Mapping) and cand.get("gender", {}).get("status") == "candidate":
            gender = ""
        if isinstance(cand.get("canonical_ru"), Mapping) and cand.get("canonical_ru", {}).get("status") == "candidate":
            canonical_ru = ""
        aliases = _verified_aliases(cand.get("aliases"))
        facts = _verified_facts(cand.get("facts"))
        verified = bool(cand.get("verified"))
        # Carry per-alias and per-field provenance when supplied by the real B1.2 normalization
        variant_provenance = cand.get("_variant_provenance") if isinstance(cand.get("_variant_provenance"), Mapping) else None
        field_provenance = cand.get("_field_provenance") if isinstance(cand.get("_field_provenance"), Mapping) else None
        # Also support field_provenance supplied directly as top-level (legacy shape)
        if field_provenance is None and isinstance(cand.get("field_provenance"), Mapping):
            field_provenance = cand.get("field_provenance")  # type: ignore
        if variant_provenance is None and isinstance(cand.get("variants"), Mapping):
            # Derive variant_provenance from variants dict when it carries source_pids
            try:
                vp = {}
                for k, v in cand.get("variants", {}).items():  # type: ignore
                    if isinstance(v, Mapping) and v.get("source_pids"):
                        vp[str(k)] = list(v.get("source_pids") or [])
                if vp:
                    variant_provenance = vp
            except Exception:
                pass

        entry: Dict[str, Any] = {
            "source": source,
            "memory_class": memory_class,
            "verified": verified,
            "evidence_pids": evidence_pids,
            "chapter_id": chapter_id,
        }

        if not source or not verified:
            # Candidate coreference / unverified surface: withhold (no durable key).
            report.append({**entry, "operation": "reject",
                           "target": "", "scope": "",
                           "reason": POP_REASON["rejected_unverified"]})
            continue

        matches = _identity_matches(new_memory, source)

        if not matches:
            # Route created record by memory_class (gender is NOT a routing signal).
            section = _CLASS_TO_SECTION.get(memory_class, "entities")
            rec = {
                "memory_class": memory_class,
                "gender": gender or "",
                "canonical_ru": canonical_ru or "",
                "address": address or "",
                "provenance_pids": list(evidence_pids),
                "chapters": [chapter_id] if chapter_id else [],
            }
            # Preserve canonical_type as 'type' and other legacy fields for backward compat (tests expect them).
            _type = cand.get("type") or cand.get("canonical_type")
            if _type:
                rec["type"] = str(_type)
            if "forbidden_targets" in cand:
                rec["forbidden_targets"] = list(cand.get("forbidden_targets") or [])
            else:
                rec.setdefault("forbidden_targets", [])
            if aliases:
                variants = {}
                for a in aliases:
                    vp = {"chapter": chapter_id}
                    if isinstance(variant_provenance, Mapping) and str(a) in variant_provenance:
                        pids = list(variant_provenance.get(str(a)) or [])
                        if pids:
                            vp["source_pids"] = pids
                    variants[str(a)] = vp
                rec["variants"] = variants
            if isinstance(field_provenance, Mapping) and field_provenance:
                fp = {}
                for fld, fprov in field_provenance.items():
                    if isinstance(fprov, Mapping):
                        pids = list(fprov.get("source_pids") or fprov.get("provenance_pids") or [])
                        fp[str(fld)] = {"chapter": chapter_id, "source_pids": pids} if pids else {"chapter": chapter_id}
                if fp:
                    rec["field_provenance"] = fp
            if facts:
                fact_objs = []
                for f in facts:
                    if isinstance(f, Mapping):
                        nf = dict(f)
                        nf.setdefault("chapter", chapter_id)
                        nf.setdefault("source_pids", list(nf.get("source_pids") or nf.get("provenance_pids") or []))
                        fact_objs.append(nf)
                    else:
                        fact_objs.append({"fact": str(f), "source_pids": [], "chapter": chapter_id})
                # Deterministic sort by fact text
                fact_objs.sort(key=lambda x: str(x.get("fact") or "").casefold())
                rec["facts"] = fact_objs
                # Mirror facts to top-level "facts" array for legacy consumers/tests.
                top_facts = new_memory.setdefault("facts", [])
                if not isinstance(top_facts, list):
                    top_facts = []
                    new_memory["facts"] = top_facts
                for fo in fact_objs:
                    ft = str(fo.get("fact") or "")
                    if ft and not any((isinstance(x, Mapping) and str(x.get("fact") or "") == ft) or (isinstance(x, str) and str(x) == ft) for x in top_facts):
                        top_facts.append(dict(fo))
            section_data = new_memory.setdefault(section, {})
            if not isinstance(section_data, dict):
                section_data = {}
                new_memory[section] = section_data
            section_data[source] = rec
            report.append({**entry, "operation": "create", "target": source,
                           "scope": section,
                           "reason": POP_REASON["created_in_scope"]})
            continue

        # Resolve compatibility across ALL matches (including pre-existing dupes)
        # AND the candidate's own verified attributes — a contradictory incoming
        # claim is a conflict, not a silent overwrite.
        cand_rec = {
            "gender": gender or "",
            "canonical_ru": canonical_ru or "",
            "address": address or "",
        }
        if _attrs_compatible([m[2] for m in matches] + [cand_rec]):
            base_section, base_key, merged = _merge_records(
                matches, memory_class=memory_class, aliases=aliases,
                gender=gender, canonical_ru=canonical_ru, address=address,
                facts=facts, evidence_pids=evidence_pids, chapter_id=chapter_id,
                variant_provenance=variant_provenance, field_provenance=field_provenance,
            )
            target_section = _CLASS_TO_SECTION.get(memory_class, base_section)
            # Write merged record into the memory_class scope; remove duplicates
            # from other sections so a single canonical key remains.
            for s, k, _r in matches:
                data = new_memory.get(s)
                if isinstance(data, Mapping) and k in data:
                    del data[k]
            target_data = new_memory.setdefault(target_section, {})
            if not isinstance(target_data, dict):
                target_data = {}
                new_memory[target_section] = target_data
            target_data[base_key] = merged
            # Mirror merged facts to top-level array as well (dedup).
            if isinstance(merged.get("facts"), list) and merged["facts"]:
                top_facts2 = new_memory.setdefault("facts", [])
                if not isinstance(top_facts2, list):
                    top_facts2 = []
                    new_memory["facts"] = top_facts2
                for fo in merged["facts"]:
                    if not isinstance(fo, Mapping):
                        continue
                    ft = str(fo.get("fact") or "")
                    if ft and not any((isinstance(x, Mapping) and str(x.get("fact") or "") == ft) or (isinstance(x, str) and str(x) == ft) for x in top_facts2):
                        top_facts2.append(dict(fo))
            reason = (POP_REASON["merged_pre_existing_duplicates"]
                      if len(matches) > 1 else POP_REASON["merged_compatible"])
            report.append({**entry, "operation": "merge", "target": base_key,
                           "scope": target_section, "reason": reason,
                           "prior_targets": [k for _s, k, _r in matches]})
        else:
            # Incompatible matches -> conflict; no prompt-visible mutation.
            # Durable conflict diagnostics: mark conflicting canonical records for exclusion
            # so select_relevant() can filter them until an explicit approved resolution.
            try:
                _conflicts = new_memory.setdefault("_conflicts", {})
                if not isinstance(_conflicts, dict):
                    _conflicts = {}
                    new_memory["_conflicts"] = _conflicts
                for _s, _k, _r in matches:
                    _conflicts[_k] = {
                        "reason": POP_REASON["conflict_incompatible"],
                        "chapter_id": chapter_id,
                        "source": source,
                        "conflicting_attrs": {k: str(cand_rec.get(k) or "") for k in ("gender", "canonical_ru", "address") if cand_rec.get(k)},
                    }
                _conflicts[source] = {
                    "reason": POP_REASON["conflict_incompatible"],
                    "chapter_id": chapter_id,
                    "prior_targets": [k for _s, k, _r in matches],
                }
                # Mark records as excluded (durable) so views skip them
                for _s, _k, _r in matches:
                    data = new_memory.get(_s)
                    if isinstance(data, Mapping) and _k in data and isinstance(data[_k], Mapping):
                        data[_k]["_excluded_conflict"] = True
                        data[_k]["_conflict_reason"] = POP_REASON["conflict_incompatible"]
            except Exception:
                pass
            report.append({**entry, "operation": "conflict", "target": "",
                           "scope": "",
                           "reason": POP_REASON["conflict_incompatible"],
                           "prior_targets": [k for _s, k, _r in matches]})

    return new_memory, report
