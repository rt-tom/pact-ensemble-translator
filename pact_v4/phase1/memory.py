import json
import os
import hashlib
import shutil
import tempfile
from typing import Any, Dict, Optional
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
    # Strict allow-list: only canonical files plus exact marker and backup files are permitted
    # Any other .pact_*, *.tmp, extra file/dir, symlink, special file is rejected
    allowed = set(CANONICAL_FILES)
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
        if book_memory_obs:
            new_book_memory = json.loads(json.dumps(ensure_policy_block(bm_current))) if isinstance(bm_current, dict) else {}
            self._merge_with_conflict_resolution(new_book_memory, book_memory_obs, book_memory=True)
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
            # If chapter context provided, compute causal entries for current and next chapter
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
                            # Use pre-chapter memory for this cid
                            pre_mem = pre_chapter_book_memory(new_book_memory, cid)
                            glossary = load_glossary(self.base_dir)
                            entry = _bci(chapter_id=cid, source_text=src_text, book_memory=pre_mem, glossary=glossary)
                            return entry
                        except Exception:
                            return None
                    # Build current chapter entry (pre-N memory)
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
