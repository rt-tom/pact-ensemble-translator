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
# deterministic replacement order per spec: glossary, book_memory, chapter_index, observations
REPLACEMENT_ORDER = ["glossary.json", "book_memory.json", "chapter_index.json", "observations.json"]
MARKER_NAME = ".pact_transaction_marker.json"
BACKUP_SUFFIX = ".pact_backup"

def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

def _file_hash(path: str) -> str:
    if not os.path.exists(path):
        return _canonical_hash({})
    try:
        data = open(path, "rb").read()
        return hashlib.sha256(data).hexdigest()
    except OSError:
        return ""

def atomic_write(filepath: str, data: Any):
    dir_name = os.path.dirname(filepath)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, text=True)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, filepath)
    # fsync dir
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

def _is_regular_file(path: str) -> bool:
    try:
        st = os.lstat(path)
        import stat
        if not stat.S_ISREG(st.st_mode):
            return False
        # reject symlink at ancestor: lstat already checks leaf; check ancestors
        return True
    except OSError:
        return False

def _validate_no_symlink_ancestors(base_dir: str, filename: str) -> bool:
    # check each ancestor up to base_dir for symlink
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
    # also check base_dir itself
    if os.path.islink(base_dir):
        return False
    return True

def _validate_exact_four_file_set(base_dir: str) -> Optional[str]:
    try:
        entries = os.listdir(base_dir)
    except OSError as e:
        return f"cannot list dir: {e}"
    # filter out marker and backup files, temp files
    visible = []
    for e in entries:
        if e.startswith(".pact_") or e.endswith(".tmp") or e.endswith(BACKUP_SUFFIX):
            continue
        visible.append(e)
    # check extra files/dirs
    allowed = set(CANONICAL_FILES)
    for e in visible:
        if e not in allowed:
            return f"extra entry {e!r} not in canonical set"
    # check each canonical file exists and is regular file, no symlink, no special
    for fname in CANONICAL_FILES:
        fpath = os.path.join(base_dir, fname)
        if not os.path.lexists(fpath):
            # missing is allowed for initial state; lexists (not exists) is required
            # so a broken symlink is still detected by the islink check below
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
        except OSError as e:
            return f"cannot stat {fname}: {e}"
        # check special files via stat
        try:
            if stat.S_ISFIFO(st.st_mode) or stat.S_ISSOCK(st.st_mode) or stat.S_ISCHR(st.st_mode) or stat.S_ISBLK(st.st_mode):
                return f"special file {fname}"
        except Exception:
            pass
        # validate JSON
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
        # startup recovery: if marker exists, restore backups
        self._recover_if_needed()

    def _recover_if_needed(self):
        if not os.path.exists(self._marker_path):
            return
        # Fail-closed: corrupt marker must NOT be silently deleted; keep it and abort
        try:
            marker = json.loads(open(self._marker_path, 'r', encoding='utf-8').read())
        except Exception as e:
            # Keep corrupt marker, do not clear; log and raise to block further runs
            raise RuntimeError(f"corrupt transaction marker, fail-closed: {e}") from e
        pre_hashes = marker.get("pre_hashes", {})
        backups = marker.get("backups", {})
        post_hashes = marker.get("post_hashes", {})
        # Restore all four from backups - if any backup missing, fail-closed
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
                except OSError as e:
                    restore_failed = True
            else:
                # No backup but file may have been partially overwritten - check pre_hash
                pass
        # Verify pre hashes - if mismatch, fail-closed, do NOT clear marker
        for fname, expected in pre_hashes.items():
            fpath = os.path.join(self.base_dir, fname)
            actual = _file_hash(fpath)
            if expected and actual != expected:
                raise RuntimeError(f"recovery pre-hash mismatch for {fname}: expected {expected}, got {actual} - fail-closed, marker retained")
        if restore_failed:
            raise RuntimeError("recovery failed: missing backups - fail-closed, marker retained")
        # Only after successful restore and verification, cleanup backups and marker
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

    def promote(self, status: str, *, quarantined_chunks: Optional[set] = None):
        if status not in ('complete', 'accepted_degraded'):
            return
        # Boundary validation MUST run before loading any canonical file: opening a
        # special file (FIFO/socket/device) would block the reading process.
        # lstat-based checks are non-blocking and reject such entries first.
        err = _validate_exact_four_file_set(self.base_dir)
        if err is not None:
            raise RuntimeError(f"exact-four-file boundary violation before promotion: {err}")
        obs = load_json(self.observations_path, {'glossary': {}, 'book_memory': {}})
        if status == 'accepted_degraded' and quarantined_chunks:
            obs = self._filter_quarantined_obs(obs, quarantined_chunks)
        # build new states for four files
        glossary_obs = obs.get('glossary', {})
        book_memory_obs = obs.get('book_memory', {})
        # Load current canonical files
        glossary = load_json(self.glossary_path, {})
        chapter_index = load_json(self.chapter_index_path, {})
        # Determine new glossary/book_memory after merge. Only load and upgrade the
        # book_memory policy block when there are actual book_memory observations,
        # otherwise leave book_memory.json byte-for-byte untouched (B9-RV9).
        new_glossary = json.loads(json.dumps(glossary)) if isinstance(glossary, dict) else {}
        new_book_memory = None
        changed_glossary = False
        changed_book_memory = False
        if glossary_obs:
            if self._merge_with_conflict_resolution(new_glossary, glossary_obs):
                changed_glossary = True
        if book_memory_obs:
            bm = load_json(self.book_memory_path, {})
            new_book_memory = json.loads(json.dumps(ensure_policy_block(bm))) if isinstance(bm, dict) else {}
            if self._merge_with_conflict_resolution(new_book_memory, book_memory_obs, book_memory=True):
                changed_book_memory = True
        # observations cleared only in committed bundle
        new_observations: Dict[str, Any] = {'glossary': {}, 'book_memory': {}}
        # If nothing changed, still clear observations via transaction to preserve atomicity
        # But we can use transactional path for all
        staged = {
            "glossary.json": new_glossary,
            "book_memory.json": new_book_memory,
            "chapter_index.json": chapter_index,
            "observations.json": new_observations,
        }
        self._transactional_replace(staged)

    def _transactional_replace(self, staged: Dict[str, Any]):
        # fault injection for tests: check env
        fault_point = os.environ.get("PACT_FAULT_INJECT")
        # validate exact set before - fail-closed per boundary hardening
        err = _validate_exact_four_file_set(self.base_dir)
        if err is not None:
            raise RuntimeError(f"exact-four-file boundary violation before transaction: {err}")
        # compute pre hashes
        pre_hashes = {fname: _file_hash(os.path.join(self.base_dir, fname)) for fname in CANONICAL_FILES}
        # Post-hashes must be file hashes (actual bytes) of staged files, not content canonical hashes
        # We will compute after writing staged files to same-FS candidate bundle
        post_hashes = {}
        # For staged we compute canonical json hash; but file hash is raw bytes hash of pretty-printed json
        # We'll compute post raw hash after writing to temp: same as file will be
        # Create same-filesystem candidate bundle, validate exact set/schema/hash before marker
        # This ensures staged bundle is valid before any mutation
        candidate_tmp = tempfile.mkdtemp(dir=self.base_dir, prefix=".pact_candidate_")
        try:
            # Write staged files to candidate bundle
            for fname in REPLACEMENT_ORDER:
                content = staged.get(fname)
                if content is None:
                    continue
                cpath = os.path.join(candidate_tmp, fname)
                # Validate JSON schema before writing
                if not isinstance(content, (dict, list)):
                    raise RuntimeError(f"staged {fname} is not JSON-serializable: {type(content)}")
                # Write via atomic write in candidate dir (use same formatting as atomic_write for consistency)
                with open(cpath, "w", encoding="utf-8") as f:
                    json.dump(content, f, ensure_ascii=False, indent=2)
                    f.write("\n")
                    f.flush()
                    os.fsync(f.fileno())
                # Compute canonical hash for post verification
                post_hashes[fname] = _canonical_hash(content)
            # Validate candidate bundle exact boundary: it must contain exactly the
            # canonical files being replaced (staged non-None), no extras.
            staged_keys = {fname for fname in REPLACEMENT_ORDER if staged.get(fname) is not None}
            cand_entries = os.listdir(candidate_tmp)
            if set(cand_entries) != staged_keys:
                raise RuntimeError(f"candidate bundle must contain exactly {sorted(staged_keys)}, got {cand_entries}")
            for fname in staged_keys:
                cpath = os.path.join(candidate_tmp, fname)
                if os.path.islink(cpath):
                    raise RuntimeError(f"candidate symlink rejected: {fname}")
                try:
                    st = os.lstat(cpath)
                    import stat
                    if not stat.S_ISREG(st.st_mode):
                        raise RuntimeError(f"candidate non-regular file: {fname}")
                except OSError as e:
                    raise RuntimeError(f"candidate stat failed for {fname}: {e}")
                # Validate JSON
                try:
                    with open(cpath, "r", encoding="utf-8") as f:
                        json.load(f)
                except Exception as e:
                    raise RuntimeError(f"candidate JSON invalid for {fname}: {e}")
            # Pre-move revalidation of live boundary (TOCTOU defense) - same check before acquiring marker
            err2 = _validate_exact_four_file_set(self.base_dir)
            if err2 is not None:
                raise RuntimeError(f"pre-move revalidation failed: {err2}")
        except Exception:
            # Cleanup candidate on failure
            try:
                shutil.rmtree(candidate_tmp)
            except OSError:
                pass
            raise
        # Keep candidate_tmp for now; will be used for post-hash verification and cleanup after
        # create backups
        backups: Dict[str, str] = {}
        for fname in REPLACEMENT_ORDER:
            src = os.path.join(self.base_dir, fname)
            if os.path.exists(src):
                bpath = src + BACKUP_SUFFIX
                try:
                    shutil.copy2(src, bpath)
                    backups[fname] = bpath
                except OSError:
                    backups[fname] = bpath
        # write marker with pre/post hashes and progress
        marker = {
            "pre_hashes": pre_hashes,
            "post_hashes": post_hashes,
            "backups": backups,
            "progress": [],
        }
        # write marker fsync
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
        # deterministic replacement order; byte preservation: skip unchanged files
        for fname in REPLACEMENT_ORDER:
            if fault_point == f"before_{fname}":
                raise RuntimeError(f"fault-inject before_{fname}")
            content = staged.get(fname)
            if content is None:
                continue
            target = os.path.join(self.base_dir, fname)
            try:
                existing = load_json(target, None)
                if _canonical_hash(existing) == _canonical_hash(content):
                    marker["progress"].append(fname)
                    # still fsync marker but skip file write to preserve bytes
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
            # update marker progress fsync
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
        # post-hash verify
        for fname in REPLACEMENT_ORDER:
            content = staged.get(fname)
            if content is None:
                # this canonical file was intentionally left unchanged
                continue
            expected = post_hashes.get(fname)
            actual_file = _file_hash(os.path.join(self.base_dir, fname))
            # compare raw file hash vs canonical staged hash? They differ due to pretty print.
            # Instead verify staged content equals file content JSON
            try:
                file_content = load_json(os.path.join(self.base_dir, fname), None)
                if _canonical_hash(file_content) != _canonical_hash(content):
                    raise RuntimeError(f"post-hash mismatch for {fname}")
            except Exception as e:
                raise RuntimeError(f"post-hash verify failed for {fname}: {e}")
        # File hash verification already done via canonical compare above
        # cleanup candidate bundle
        try:
            shutil.rmtree(candidate_tmp)
        except OSError:
            pass
        # cleanup backups and marker
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
