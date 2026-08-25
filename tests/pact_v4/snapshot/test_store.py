"""Offline tests for book-state snapshot handoff."""

import hashlib
import io
import json
import os
import tempfile
from pathlib import Path

import pytest

from pact_v4.snapshot.bootstrap import bootstrap
from pact_v4.snapshot.cli import main as cli_main
from pact_v4.snapshot.errors import HashMismatch, LeaseHeld, StaleParent, ValidationError
from pact_v4.snapshot.lease import acquire_lease, check_expired, read_lease, release_lease, release_with_audit
from pact_v4.snapshot.manifest import Manifest, compute_sha256_and_size
from pact_v4.snapshot.promote import promote
from pact_v4.snapshot.store import BookStore

BOOK_ID = "pact-book-ru"
CANONICAL = ["book_memory.json", "glossary.json", "chapter_index.json", "observations.json"]


def _make_json_file(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)
        f.write("\n")


def _init_store(tmp: str, book_id=BOOK_ID):
    store = BookStore(book_id, root=tmp)
    store.init_store()
    return store


def _seed_inbox(store: BookStore, ts="20260826T120000Z", extra_files=None, corrupt_one=None):
    inbox_ts_dir = store.bootstrap_inbox_dir / ts
    inbox_ts_dir.mkdir(parents=True, exist_ok=True)
    for fname in CANONICAL:
        data = {"file": fname, "content": "hello-" + fname}
        _make_json_file(inbox_ts_dir / fname, data)
    if corrupt_one:
        # overwrite one file with non-JSON
        with open(inbox_ts_dir / corrupt_one, "w", encoding="utf-8") as f:
            f.write("{ not valid json")
    if extra_files:
        for name, content in extra_files.items():
            with open(inbox_ts_dir / name, "w", encoding="utf-8") as f:
                if isinstance(content, dict):
                    json.dump(content, f)
                else:
                    f.write(str(content))
    return inbox_ts_dir


def _create_candidate(store: BookStore, candidate_id, parent_rev, terminal="complete", tamper_hash=False, revision_id=None):
    cand_dir = store.incoming_candidate_path(candidate_id)
    state_dir = cand_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    # create state files with JSON content
    for fname in CANONICAL:
        data = {"file": fname, "candidate": candidate_id, "value": 123}
        _make_json_file(state_dir / fname, data)
    # build state_files entries
    state_files = []
    for fname in CANONICAL:
        p = state_dir / fname
        sha, size = compute_sha256_and_size(p)
        if tamper_hash and fname == CANONICAL[0]:
            # corrupt hash
            sha = "0" * 64
        state_files.append({"rel_path": f"state/{fname}", "sha256": sha, "size": size})
    # Allow caller to inject arbitrary revision_id (Finding 1)
    if revision_id is None:
        revision_id = store.compute_next_revision_id()
    manifest_dict = {
        "schema_version": "1.0.0",
        "book_id": store.book_id,
        "revision_id": revision_id,
        "parent_revision_id": parent_rev,
        "created_at": "2026-08-26T12:00:00Z",
        "published_at": "2026-08-26T12:00:00Z",
        "terminal_status": terminal,
        "tool_version": "pact-snapshot/0.1.0",
        "source": {"path_on_rt": "D:\\pact\\pact_chapters", "operator": "rt", "host": "RT", "run_id": "run-123"},
        "state_files": state_files,
        "excludes": [],
        "code_commit": "unknown",
    }
    # Validate strict allow-list: ensure no unknown keys
    _make_json_file(cand_dir / "manifest.json", manifest_dict)
    return cand_dir, manifest_dict


# (a) init-store creates layout
def test_a_init_store_creates_layout():
    with tempfile.TemporaryDirectory() as tmp:
        store = BookStore(BOOK_ID, root=tmp)
        store.init_store()
        assert store.book_dir.exists()
        assert store.locks_dir.exists()
        assert store.incoming_dir.exists()
        assert store.bootstrap_inbox_dir.exists()
        assert store.quarantine_dir.exists()
        assert store.snapshots_dir.exists()
        assert store.current_path.exists()
        cur = store.read_current()
        assert cur["revision_id"] is None
        # atomic rename test
        store.write_current_atomic({"book_id": BOOK_ID, "revision_id": "rev-0001", "manifest_sha256": "abc", "published_at": "2026-08-26T12:00:00Z", "operator": "rt", "host": "RT", "run_id": None, "lease_id": None, "parent_revision_id": None})
        assert store.read_current()["revision_id"] == "rev-0001"


# (b) bootstrap from valid inbox creates rev-0001 + CURRENT
def test_b_bootstrap_valid():
    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)
        _seed_inbox(store, extra_files={"glossary_candidates.json": {"junk": 1}, "book_memory_v3_archive.json": {"old": 1}})
        result = bootstrap(store)
        assert result["revision_id"] == "rev-0001"
        snap_dir = store.snapshot_dir("rev-0001")
        assert snap_dir.exists()
        for fname in CANONICAL:
            assert (snap_dir / "state" / fname).exists()
        assert (snap_dir / "manifest.json").exists()
        cur = store.read_current()
        assert cur["revision_id"] == "rev-0001"
        # manifest excludes should contain candidates
        m = Manifest.read(snap_dir / "manifest.json")
        assert "glossary_candidates.json" in m.excludes
        assert m.terminal_status == "bootstrap-seed"
        assert m.parent_revision_id is None
        # validate secret rejection
        bad = m.to_dict()
        bad["secret_token"] = "should-fail"
        with pytest.raises(ValidationError):
            Manifest.from_dict(bad)
        bad2 = m.to_dict()
        bad2["source"]["env_dump"] = "secret"
        with pytest.raises(ValidationError):
            Manifest.from_dict(bad2)
        # bad terminal
        bad3 = m.to_dict()
        bad3["terminal_status"] = "invalid-status"
        with pytest.raises(ValidationError):
            Manifest.from_dict(bad3)


# (c) bootstrap fails closed on non-JSON
def test_c_bootstrap_fails_closed_non_json():
    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)
        _seed_inbox(store, corrupt_one="glossary.json")
        with pytest.raises(ValidationError):
            bootstrap(store)
        # CURRENT should remain null (not advanced)
        cur = store.read_current()
        assert cur["revision_id"] is None
        # no snapshots created
        assert list(store.snapshots_dir.iterdir()) == []


# (d) promote ACCEPTS valid candidate with ARBITRARY/WRONG revision_id — media assigns next_rev (Finding 1)
def test_d_promote_accepts():
    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)
        _seed_inbox(store)
        bootstrap(store)
        cur_before = store.read_current()
        assert cur_before["revision_id"] == "rev-0001"
        expected_next = store.compute_next_revision_id()
        assert expected_next == "rev-0002"
        # Candidate declares a completely wrong revision_id — must still be ACCEPTED and media-assigned
        _create_candidate(store, "cand-001", parent_rev="rev-0001", revision_id="rev-9999")
        result = promote(store, "cand-001")
        assert result["status"] == "ACCEPTED"
        assert result["revision_id"] == "rev-0002"
        cur_after = store.read_current()
        assert cur_after["revision_id"] == "rev-0002"
        # Finding 1: snapshot directory name, stored manifest revision_id, and CURRENT must all agree on next_rev
        snap_dir = store.snapshot_dir("rev-0002")
        assert snap_dir.exists()
        stored_manifest = Manifest.read(snap_dir / "manifest.json")
        assert stored_manifest.revision_id == "rev-0002"
        assert cur_after["revision_id"] == stored_manifest.revision_id == "rev-0002"
        assert snap_dir.name == "rev-0002"
        # lease must be released (no lease file)
        assert not store.lease_path().exists()
        # candidate no longer in incoming
        assert not store.incoming_candidate_path("cand-001").exists()
        # not quarantined
        assert not store.quarantine_candidate_path("cand-001").exists()
        # Finding 3 (Medium) — manifest_sha256 integrity: stored manifest hash equals CURRENT and result
        stored_manifest_path = snap_dir / "manifest.json"
        computed_sha, _ = compute_sha256_and_size(stored_manifest_path)
        assert computed_sha == cur_after["manifest_sha256"]
        assert computed_sha == result["manifest_sha256"]
        # Also verify CURRENT manifest_sha matches final rewritten manifest content
        with open(stored_manifest_path, "rb") as f:
            raw_bytes = f.read()
        assert hashlib.sha256(raw_bytes).hexdigest() == cur_after["manifest_sha256"]


# (e) promote REJECTS stale parent
def test_e_promote_stale_parent():
    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)
        _seed_inbox(store)
        bootstrap(store)
        _create_candidate(store, "cand-stale", parent_rev="rev-0000")
        with pytest.raises(StaleParent):
            promote(store, "cand-stale")
        # quarantined
        assert store.quarantine_candidate_path("cand-stale").exists()
        assert not store.incoming_candidate_path("cand-stale").exists()
        # CURRENT unchanged
        assert store.read_current()["revision_id"] == "rev-0001"
        # no lease left
        assert not store.lease_path().exists()


# (f) promote REJECTS hash mismatch
def test_f_promote_hash_mismatch():
    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)
        _seed_inbox(store)
        bootstrap(store)
        _create_candidate(store, "cand-hash", parent_rev="rev-0001", tamper_hash=True)
        with pytest.raises(HashMismatch):
            promote(store, "cand-hash")
        assert store.quarantine_candidate_path("cand-hash").exists()
        assert store.read_current()["revision_id"] == "rev-0001"


# (g) promote REJECTS when lease held, then release-lease clears and audit
def test_g_lease_held_and_release():
    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)
        _seed_inbox(store)
        bootstrap(store)
        # manually acquire lease
        lease = acquire_lease(store, operator="rt", host="RT", parent_revision="rev-0001")
        assert store.lease_path().exists()
        # create candidate valid
        _create_candidate(store, "cand-lease", parent_rev="rev-0001")
        with pytest.raises(LeaseHeld):
            promote(store, "cand-lease")
        # quarantined
        assert store.quarantine_candidate_path("cand-lease").exists()
        # CURRENT unchanged
        assert store.read_current()["revision_id"] == "rev-0001"
        # lease still held
        assert store.lease_path().exists()
        # check_expired is read-only
        report = check_expired(store)
        assert report["held"] is True
        assert store.lease_path().exists()
        # release with audit
        audit = release_with_audit(store, operator="rt", reason="stale promote crashed", prior_staging_reviewed=True, recovery_decision="released")
        assert audit is not None
        assert not store.lease_path().exists()
        # audit file exists and contains record
        audit_path = store.lease_audit_path()
        assert audit_path.exists()
        lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) >= 1
        rec = json.loads(lines[-1])
        assert rec["reason"] == "stale promote crashed"
        assert rec["prior_staging_reviewed"] is True
        # Now retry with new candidate (since previous quarantined, create fresh)
        _create_candidate(store, "cand-lease-retry", parent_rev="rev-0001", revision_id="rev-1234")
        result = promote(store, "cand-lease-retry")
        assert result["status"] == "ACCEPTED"
        assert store.read_current()["revision_id"] == "rev-0002"
        assert not store.lease_path().exists()


# (h) release-lease --check-expired read-only
def test_h_check_expired_readonly():
    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)
        _seed_inbox(store)
        bootstrap(store)
        acquire_lease(store, operator="rt", host="RT", parent_revision="rev-0001")
        assert store.lease_path().exists()
        # No audit yet
        audit_path = store.lease_audit_path()
        before = audit_path.read_text(encoding="utf-8") if audit_path.exists() else ""
        # call check_expired via cli to ensure read-only
        # Also direct check
        rep = check_expired(store)
        assert rep["held"] is True
        assert store.lease_path().exists()
        after = audit_path.read_text(encoding="utf-8") if audit_path.exists() else ""
        assert before == after
        # cli --check-expired should not delete
        rc = cli_main(["--root", tmp, "release-lease", BOOK_ID, "--check-expired"])
        assert rc == 0
        assert store.lease_path().exists()
        after_cli = audit_path.read_text(encoding="utf-8") if audit_path.exists() else ""
        assert after_cli == after


def test_cli_init_and_bootstrap_via_cli():
    with tempfile.TemporaryDirectory() as tmp:
        rc = cli_main(["--root", tmp, "init-store", BOOK_ID])
        assert rc == 0
        store = BookStore(BOOK_ID, root=tmp)
        assert store.current_path.exists()
        # seed inbox
        _seed_inbox(store)
        rc2 = cli_main(["--root", tmp, "bootstrap", BOOK_ID])
        assert rc2 == 0
        assert store.read_current()["revision_id"] == "rev-0001"


def test_cli_promote_and_quarantine():
    with tempfile.TemporaryDirectory() as tmp:
        cli_main(["--root", tmp, "init-store", BOOK_ID])
        store = BookStore(BOOK_ID, root=tmp)
        _seed_inbox(store)
        cli_main(["--root", tmp, "bootstrap", BOOK_ID])
        # stale candidate via cli
        _create_candidate(store, "cand-cli-stale", parent_rev="rev-9999")
        rc = cli_main(["--root", tmp, "promote", BOOK_ID, "cand-cli-stale"])
        assert rc == 2
        assert store.quarantine_candidate_path("cand-cli-stale").exists()
        assert store.read_current()["revision_id"] == "rev-0001"

# --- Finding 2 rejection tests ---

def test_i_reject_extra_file_in_state():
    """Candidate contains extra file not in state_files -> REJECTED + quarantined + CURRENT unchanged."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)
        _seed_inbox(store)
        bootstrap(store)
        cand_dir, _ = _create_candidate(store, "cand-extra", parent_rev="rev-0001")
        # Add smuggled extra file in candidate state/
        extra_path = cand_dir / "state" / "secret_backup.json"
        _make_json_file(extra_path, {"secret": "leak"})
        with pytest.raises(ValidationError):
            promote(store, "cand-extra")
        assert store.quarantine_candidate_path("cand-extra").exists()
        assert not store.incoming_candidate_path("cand-extra").exists()
        assert store.read_current()["revision_id"] == "rev-0001"


def test_j_reject_traversal_or_non_canonical_manifest():
    """Manifest state_files includes traversal or non-canonical path -> REJECTED."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)
        _seed_inbox(store)
        bootstrap(store)
        cand_dir, manifest_dict = _create_candidate(store, "cand-traversal", parent_rev="rev-0001")
        # Inject traversal path into manifest
        bad_manifest = dict(manifest_dict)
        # replace one entry with traversal
        bad_manifest["state_files"] = list(manifest_dict["state_files"])
        bad_manifest["state_files"][0] = {"rel_path": "state/../secret.json", "sha256": manifest_dict["state_files"][0]["sha256"], "size": manifest_dict["state_files"][0]["size"]}
        _make_json_file(cand_dir / "manifest.json", bad_manifest)
        with pytest.raises(ValidationError):
            promote(store, "cand-traversal")
        assert store.quarantine_candidate_path("cand-traversal").exists()
        assert store.read_current()["revision_id"] == "rev-0001"
        # Also test non-canonical path (not in canonical four)
        cand_dir2, manifest_dict2 = _create_candidate(store, "cand-noncanon", parent_rev="rev-0001")
        bad2 = dict(manifest_dict2)
        bad2["state_files"] = list(manifest_dict2["state_files"])
        bad2["state_files"][0] = {"rel_path": "state/extra.json", "sha256": manifest_dict2["state_files"][0]["sha256"], "size": manifest_dict2["state_files"][0]["size"]}
        _make_json_file(cand_dir2 / "manifest.json", bad2)
        with pytest.raises(ValidationError):
            promote(store, "cand-noncanon")
        assert store.quarantine_candidate_path("cand-noncanon").exists()
        assert store.read_current()["revision_id"] == "rev-0001"


def test_k_reject_duplicate_state_files():
    """Duplicate state_files paths -> REJECTED."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)
        _seed_inbox(store)
        bootstrap(store)
        cand_dir, manifest_dict = _create_candidate(store, "cand-dup", parent_rev="rev-0001")
        bad = dict(manifest_dict)
        # duplicate first entry
        bad["state_files"] = list(manifest_dict["state_files"])
        bad["state_files"].append(dict(manifest_dict["state_files"][0]))
        _make_json_file(cand_dir / "manifest.json", bad)
        with pytest.raises(ValidationError):
            promote(store, "cand-dup")
        assert store.quarantine_candidate_path("cand-dup").exists()
        assert store.read_current()["revision_id"] == "rev-0001"


def test_l_reject_non_json_canonical_file():
    """Canonical state file that is not valid JSON -> REJECTED."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)
        _seed_inbox(store)
        bootstrap(store)
        cand_dir, manifest_dict = _create_candidate(store, "cand-badjson", parent_rev="rev-0001")
        # Corrupt one canonical file to be non-JSON, but keep hash correct? Need to adjust: first corrupt file content, then also update manifest? Actually promote will check JSON validity before hash? We corrupted file after manifest hash computed.
        # Easiest: overwrite file after candidate creation
        bad_file = cand_dir / "state" / "glossary.json"
        with open(bad_file, "w", encoding="utf-8") as f:
            f.write("{ not valid json }")
        # Update manifest entry to match new bytes so hash check would pass but JSON validation fails
        sha, size = compute_sha256_and_size(bad_file)
        # Find glossary entry and update
        new_manifest = dict(manifest_dict)
        new_files = []
        for e in manifest_dict["state_files"]:
            if e["rel_path"] == "state/glossary.json":
                new_files.append({"rel_path": e["rel_path"], "sha256": sha, "size": size})
            else:
                new_files.append(dict(e))
        new_manifest["state_files"] = new_files
        _make_json_file(cand_dir / "manifest.json", new_manifest)
        with pytest.raises(ValidationError):
            promote(store, "cand-badjson")
        assert store.quarantine_candidate_path("cand-badjson").exists()
        assert store.read_current()["revision_id"] == "rev-0001"


# --- Finding 3 CLI regression test ---

def test_m_bootstrap_cli_no_current_on_failure():
    """Start with NO CURRENT.json (store never initialized) and run bootstrap against non-JSON inbox -> fails and CURRENT still absent."""
    with tempfile.TemporaryDirectory() as tmp:
        store = BookStore(BOOK_ID, root=tmp)
        # Do NOT call init_store — store never initialized, no CURRENT.json
        assert not store.current_path.exists()
        # Create inbox dir structure manually (since init_store not called)
        inbox_ts_dir = store.bootstrap_inbox_dir / "20260826T120000Z"
        inbox_ts_dir.mkdir(parents=True, exist_ok=True)
        for fname in CANONICAL:
            data = {"file": fname, "content": "hello"}
            _make_json_file(inbox_ts_dir / fname, data)
        # corrupt one file
        with open(inbox_ts_dir / "glossary.json", "w", encoding="utf-8") as f:
            f.write("{ not valid json")
        # Run bootstrap via CLI — should fail
        rc = cli_main(["--root", tmp, "bootstrap", BOOK_ID])
        assert rc in (2, 3)  # SnapshotError -> 2
        # CURRENT.json must still be absent (bootstrap must not have pre-created it via init_store)
        assert not store.current_path.exists(), "CURRENT.json must not be created on bootstrap failure"
        # No snapshot created
        if store.snapshots_dir.exists():
            assert list(store.snapshots_dir.iterdir()) == []


def test_n_bootstrap_success_without_prior_init_store():
    """Successful bootstrap without prior init-store still creates rev-0001 + CURRENT (ensures dirs are created)."""
    with tempfile.TemporaryDirectory() as tmp:
        store = BookStore(BOOK_ID, root=tmp)
        assert not store.current_path.exists()
        inbox_ts_dir = store.bootstrap_inbox_dir / "20260826T120000Z"
        inbox_ts_dir.mkdir(parents=True, exist_ok=True)
        for fname in CANONICAL:
            _make_json_file(inbox_ts_dir / fname, {"ok": fname})
        rc = cli_main(["--root", tmp, "bootstrap", BOOK_ID])
        assert rc == 0
        assert store.current_path.exists()
        cur = store.read_current()
        assert cur["revision_id"] == "rev-0001"
        assert store.snapshot_dir("rev-0001").exists()


# --- Finding 1 High: symlink rejection ---

def test_o_reject_symlink_in_state():
    """Candidate with state/book_memory.json as symlink outside candidate -> REJECTED + quarantined + CURRENT unchanged."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)
        _seed_inbox(store)
        bootstrap(store)
        cand_dir, manifest_dict = _create_candidate(store, "cand-symlink", parent_rev="rev-0001")
        # Create external temp file with JSON content
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json", encoding="utf-8") as ext:
            json.dump({"evil": "outside", "file": "book_memory.json"}, ext)
            ext_path = ext.name
        try:
            target = cand_dir / "state" / "book_memory.json"
            # Remove original regular file and replace with symlink
            target.unlink()
            os.symlink(ext_path, str(target))
            # Update manifest hash to match external file so hash check would pass but symlink rejection triggers first
            sha, size = compute_sha256_and_size(Path(ext_path))
            # Patch manifest entry
            new_manifest = dict(manifest_dict)
            new_files = []
            for e in manifest_dict["state_files"]:
                if e["rel_path"] == "state/book_memory.json":
                    new_files.append({"rel_path": e["rel_path"], "sha256": sha, "size": size})
                else:
                    new_files.append(dict(e))
            new_manifest["state_files"] = new_files
            _make_json_file(cand_dir / "manifest.json", new_manifest)
            with pytest.raises(ValidationError):
                promote(store, "cand-symlink")
            assert store.quarantine_candidate_path("cand-symlink").exists()
            # CURRENT unchanged
            assert store.read_current()["revision_id"] == "rev-0001"
            # Ensure quarantined entry still contains symlink (or at least exists)
            q_target = store.quarantine_candidate_path("cand-symlink") / "state" / "book_memory.json"
            # quarantined target should be symlink or at least path exists as symlink
            assert q_target.is_symlink() or q_target.exists()
        finally:
            try:
                os.unlink(ext_path)
            except OSError:
                pass


def test_o2_reject_symlinked_candidate_dir():
    """Candidate directory itself is a symlink -> REJECTED."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)
        _seed_inbox(store)
        bootstrap(store)
        # Create a real dir elsewhere and symlink candidate name to it
        real = Path(tmp) / "real_cand"
        real.mkdir()
        # Need to create manifest inside real dir to be plausible, but we won't because symlink dir check happens first
        # Create a valid candidate then replace its path with symlink to outside
        # First create a temp valid candidate and move its contents to real dir, then symlink
        _create_candidate(store, "cand-real", parent_rev="rev-0001")
        src = store.incoming_candidate_path("cand-real")
        # Move contents to real dir
        import shutil as _shutil
        for item in src.iterdir():
            _shutil.move(str(item), str(real / item.name))
        src.rmdir()
        os.symlink(str(real), str(src))
        # Now promote should reject due to symlink candidate_dir
        with pytest.raises(ValidationError):
            promote(store, "cand-real")
        assert store.read_current()["revision_id"] == "rev-0001"


# --- Finding 2 High: path escape validation ---

def test_p_reject_candidate_id_escape():
    """candidate_id with path traversal is rejected before promotion."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)
        _seed_inbox(store)
        bootstrap(store)
        with pytest.raises((ValidationError, ValueError)):
            promote(store, "../escape")
        # Ensure no file escaped and CURRENT unchanged
        assert store.read_current()["revision_id"] == "rev-0001"
        # Also absolute path
        with pytest.raises((ValidationError, ValueError)):
            promote(store, "/etc/passwd")
        assert store.read_current()["revision_id"] == "rev-0001"
        # Windows-style absolute
        with pytest.raises((ValidationError, ValueError)):
            promote(store, "C:\\Windows\\secret")
        assert store.read_current()["revision_id"] == "rev-0001"
        # Backslash separator
        with pytest.raises((ValidationError, ValueError)):
            promote(store, "cand\\escape")
        assert store.read_current()["revision_id"] == "rev-0001"


def test_q_reject_book_id_separator():
    """BookStore with separator in book_id raises ValueError/ValidationError."""
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises((ValueError, ValidationError)):
            BookStore("bad/book", root=tmp)
        with pytest.raises((ValueError, ValidationError)):
            BookStore("bad\\book", root=tmp)
        with pytest.raises((ValueError, ValidationError)):
            BookStore("../escape", root=tmp)
        with pytest.raises((ValueError, ValidationError)):
            BookStore("/etc/passwd", root=tmp)
        with pytest.raises((ValueError, ValidationError)):
            BookStore("", root=tmp)
        with pytest.raises((ValueError, ValidationError)):
            BookStore("bad:colon", root=tmp)
        # Valid remains allowed
        s = BookStore("valid-book_123.test", root=tmp)
        assert s.book_id == "valid-book_123.test"


def test_r_cli_rejects_escape_candidate_id():
    """CLI promote with escaping candidate_id exits REJECTED (2) and preserves CURRENT."""
    with tempfile.TemporaryDirectory() as tmp:
        cli_main(["--root", tmp, "init-store", BOOK_ID])
        store = BookStore(BOOK_ID, root=tmp)
        _seed_inbox(store)
        cli_main(["--root", tmp, "bootstrap", BOOK_ID])
        rc = cli_main(["--root", tmp, "promote", BOOK_ID, "../escape"])
        assert rc == 2
        assert store.read_current()["revision_id"] == "rev-0001"
        rc2 = cli_main(["--root", tmp, "promote", BOOK_ID, "/etc/passwd"])
        assert rc2 == 2
        assert store.read_current()["revision_id"] == "rev-0001"


def test_s_bootstrap_rejects_symlinked_inbox_file():
    """Bootstrap rejects when canonical inbox file is a symlink."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)
        inbox_ts_dir = store.bootstrap_inbox_dir / "20260826T120000Z"
        inbox_ts_dir.mkdir(parents=True, exist_ok=True)
        for fname in CANONICAL:
            _make_json_file(inbox_ts_dir / fname, {"ok": fname})
        # Replace one file with symlink to external
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json", encoding="utf-8") as ext:
            json.dump({"outside": True}, ext)
            ext_path = ext.name
        try:
            target = inbox_ts_dir / "glossary.json"
            target.unlink()
            os.symlink(ext_path, str(target))
            with pytest.raises(ValidationError):
                bootstrap(store)
            # CURRENT must still be null (bootstrap failed) — for init-store case revision is None
            cur = store.read_current()
            assert cur is None or cur.get("revision_id") is None
            assert not store.snapshot_dir("rev-0001").exists()
        finally:
            try:
                os.unlink(ext_path)
            except OSError:
                pass


def test_t_bootstrap_rejects_symlinked_inbox_dir():
    """Regression: _bootstrap_inbox/<ts> as SYMLINK to outside dir must be REJECTED and must NOT create/advance CURRENT.json."""
    # Case 1: store without CURRENT (never bootstrapped) — symlink inbox outside root
    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)
        # Create external directory outside store root containing valid canonical JSON
        with tempfile.TemporaryDirectory() as external:
            ext_dir = Path(external) / "evil_inbox"
            ext_dir.mkdir(parents=True)
            for fname in CANONICAL:
                _make_json_file(ext_dir / fname, {"ok": fname, "external": True})
            # Replace inbox ts with symlink to external dir
            ts = "20260826T120000Z"
            link = store.bootstrap_inbox_dir / ts
            # Ensure clean: if real dir exists from _seed_inbox etc, remove; _init_store leaves inbox empty
            if link.exists() or link.is_symlink():
                import shutil as _sh
                if link.is_symlink():
                    link.unlink()
                else:
                    _sh.rmtree(link)
            os.symlink(str(ext_dir), str(link))
            with pytest.raises(ValidationError):
                bootstrap(store)
            # CURRENT must NOT be created/advanced — still null
            cur = store.read_current()
            assert cur is not None
            assert cur.get("revision_id") is None
            assert not store.snapshot_dir("rev-0001").exists()
            # Also via explicit ts argument
            if link.is_symlink():
                # keep symlink for second attempt with explicit ts
                with pytest.raises(ValidationError):
                    bootstrap(store, ts=ts)
                cur2 = store.read_current()
                assert cur2.get("revision_id") is None
    # Case 2: bootstrap_inbox_dir itself is a symlink -> also rejected
    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)
        with tempfile.TemporaryDirectory() as external:
            ext_inbox = Path(external) / "fake_inbox"
            ext_inbox.mkdir()
            ts_dir = ext_inbox / "20260826T120000Z"
            ts_dir.mkdir()
            for fname in CANONICAL:
                _make_json_file(ts_dir / fname, {"ok": fname})
            # Replace bootstrap_inbox_dir with symlink to external inbox
            inbox = store.bootstrap_inbox_dir
            import shutil as _sh2
            _sh2.rmtree(inbox)
            os.symlink(str(ext_inbox), str(inbox))
            with pytest.raises(ValidationError):
                bootstrap(store)
            cur = store.read_current()
            assert cur.get("revision_id") is None
            assert not store.snapshot_dir("rev-0001").exists()
    # Case 3: legitimate non-symlinked bootstrap still succeeds (sanity)
    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)
        _seed_inbox(store, ts="20260826T120000Z")
        result = bootstrap(store)
        assert result["revision_id"] == "rev-0001"
        cur = store.read_current()
        assert cur["revision_id"] == "rev-0001"
        assert store.snapshot_dir("rev-0001").exists()


def test_t2_bootstrap_rejects_symlinked_ancestor():
    """Regression: symlink on ancestor between store.root and inbox must be rejected."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)
        # Make books dir component a symlink to external (simulate ancestor escape)
        # store.root/tmp/books is created by _init_store; replace books/<book_id> with symlink
        with tempfile.TemporaryDirectory() as external:
            fake_book = Path(external) / "fake_book"
            fake_book.mkdir()
            # create _bootstrap_inbox inside fake_book
            inbox_ts = fake_book / "_bootstrap_inbox" / "20260826T120000Z"
            inbox_ts.mkdir(parents=True)
            for fname in CANONICAL:
                _make_json_file(inbox_ts / fname, {"ok": fname})
            # Replace real book dir with symlink to fake_book
            real_book = store.book_dir
            import shutil as _sh3
            _sh3.rmtree(real_book)
            os.symlink(str(fake_book), str(real_book))
            with pytest.raises(ValidationError):
                bootstrap(store)
            # CURRENT should not be advanced (may be missing because book_dir was replaced)
            # After rejection, the symlink still exists; read_current will follow it
            cur = store.read_current()
            # If current was read via symlinked book_dir, it would be inside fake_book/CURRENT.json which doesn't exist
            assert cur is None or cur.get("revision_id") is None


# --- Round 5: top-level boundary rejection (credentials.env etc) ---

def test_v_reject_top_level_extra_file():
    """Candidate with top-level extra file credentials.env -> REJECTED, quarantined, CURRENT unchanged."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)
        _seed_inbox(store)
        bootstrap(store)
        cand_dir, _ = _create_candidate(store, "cand-top-extra-file", parent_rev="rev-0001")
        extra = cand_dir / "credentials.env"
        extra.write_text("SECRET=leak\n", encoding="utf-8")
        with pytest.raises(ValidationError):
            promote(store, "cand-top-extra-file")
        assert store.quarantine_candidate_path("cand-top-extra-file").exists()
        assert not store.incoming_candidate_path("cand-top-extra-file").exists()
        assert store.read_current()["revision_id"] == "rev-0001"
        # quarantine must contain the smuggled file, snapshot must NOT
        assert (store.quarantine_candidate_path("cand-top-extra-file") / "credentials.env").exists()
        assert not (store.snapshot_dir("rev-0002") / "credentials.env").exists()
        assert not store.snapshot_dir("rev-0002").exists()
        # lease must be released
        assert not store.lease_path().exists()


def test_w_reject_top_level_extra_directory():
    """Candidate with top-level extra directory -> REJECTED."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)
        _seed_inbox(store)
        bootstrap(store)
        cand_dir, _ = _create_candidate(store, "cand-top-extra-dir", parent_rev="rev-0001")
        (cand_dir / "extra_dir").mkdir()
        (cand_dir / "extra_dir" / "junk.json").write_text("{\"x\":1}", encoding="utf-8")
        with pytest.raises(ValidationError):
            promote(store, "cand-top-extra-dir")
        assert store.quarantine_candidate_path("cand-top-extra-dir").exists()
        assert store.read_current()["revision_id"] == "rev-0001"
        assert not store.snapshot_dir("rev-0002").exists()
        assert not store.lease_path().exists()


def test_x_reject_top_level_symlinked_manifest():
    """Candidate whose manifest.json is a top-level symlink -> REJECTED."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)
        _seed_inbox(store)
        bootstrap(store)
        cand_dir, manifest_dict = _create_candidate(store, "cand-top-symlink-manifest", parent_rev="rev-0001")
        # Create external file with same manifest content
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json", encoding="utf-8") as ext:
            json.dump(manifest_dict, ext)
            ext_path = ext.name
        try:
            target = cand_dir / "manifest.json"
            target.unlink()
            os.symlink(ext_path, str(target))
            with pytest.raises(ValidationError):
                promote(store, "cand-top-symlink-manifest")
            assert store.quarantine_candidate_path("cand-top-symlink-manifest").exists()
            assert store.read_current()["revision_id"] == "rev-0001"
            assert not store.snapshot_dir("rev-0002").exists()
            assert not store.lease_path().exists()
            # also ensure state symlink is rejected (completeness)
        finally:
            try:
                os.unlink(ext_path)
            except OSError:
                pass
        # also verify symlinked state/ is rejected at top-level
        with tempfile.TemporaryDirectory() as tmp2:
            store2 = _init_store(tmp2)
            _seed_inbox(store2)
            bootstrap(store2)
            cand_dir2, _ = _create_candidate(store2, "cand-top-symlink-state", parent_rev="rev-0001")
            # replace state dir with symlink
            import shutil as _sh
            real_state = cand_dir2 / "state"
            external_state = Path(tmp2) / "ext_state"
            _sh.move(str(real_state), str(external_state))
            os.symlink(str(external_state), str(real_state))
            with pytest.raises(ValidationError):
                promote(store2, "cand-top-symlink-state")
            assert store2.quarantine_candidate_path("cand-top-symlink-state").exists()
            assert store2.read_current()["revision_id"] == "rev-0001"


def test_y_legitimate_promote_still_accepted_after_top_level_fix():
    """Legitimate four-file candidate still promotes to rev-0002 with CURRENT advanced."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)
        _seed_inbox(store)
        bootstrap(store)
        cand_dir, _ = _create_candidate(store, "cand-legit", parent_rev="rev-0001")
        # sanity: candidate has exactly two top-level entries
        assert {p.name for p in cand_dir.iterdir()} == {"manifest.json", "state"}
        result = promote(store, "cand-legit")
        assert result["status"] == "ACCEPTED"
        assert result["revision_id"] == "rev-0002"
        assert store.read_current()["revision_id"] == "rev-0002"
        snap = store.snapshot_dir("rev-0002")
        assert snap.exists()
        assert (snap / "manifest.json").exists()
        for fname in CANONICAL:
            assert (snap / "state" / fname).exists()
        # no extra top-level file persisted
        assert {p.name for p in snap.iterdir()} == {"manifest.json", "state"}


def test_u_bootstrap_symlinked_ancestor_malformed_current_regression():
    """Round 4 regression: symlinked books/<book-id> ancestor whose target contains malformed CURRENT.json
    must raise ValidationError (NOT JSONDecodeError) BEFORE any store-path read, and must NOT create/advance snapshot."""
    import json as _json
    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)
        # Prepare external target that will be the symlink target of books/<book-id>
        with tempfile.TemporaryDirectory() as external:
            ext_book = Path(external) / "ext_book"
            ext_book.mkdir(parents=True)
            # Valid inbox inside external target
            inbox = ext_book / "_bootstrap_inbox" / "20260826T120000Z"
            inbox.mkdir(parents=True)
            for fname in CANONICAL:
                _make_json_file(inbox / fname, {"ok": fname})
            # Malformed CURRENT.json inside symlink target (would raise JSONDecodeError if read before symlink check)
            malformed = ext_book / "CURRENT.json"
            with open(malformed, "w", encoding="utf-8") as f:
                f.write("{ not valid json }\n")
            # Replace real book_dir with symlink to ext_book
            real_book = store.book_dir
            import shutil as _sh4
            if real_book.is_symlink():
                real_book.unlink()
            elif real_book.exists():
                _sh4.rmtree(real_book)
            os.symlink(str(ext_book), str(real_book))
            # Bootstrap must fail with ValidationError, not JSONDecodeError
            with pytest.raises(ValidationError):
                bootstrap(store)
            # Also ensure calling with explicit ts also rejects before CURRENT read
            with pytest.raises(ValidationError):
                bootstrap(store, ts="20260826T120000Z")
            # Ensure the malformed CURRENT was NOT overwritten/advanced and no snapshot was created
            assert malformed.exists()
            assert "{ not valid json" in malformed.read_text(encoding="utf-8")
            # Snapshot must not exist (even via symlinked path)
            assert not (store.snapshots_dir / "rev-0001").exists()
            # Reading CURRENT via store (which follows symlink) must still be malformed / not a valid revision
            # Direct store.read_current would raise JSONDecodeError if we called it; we verify bootstrap did NOT
            # advance CURRENT to a valid revision by checking raw file still malformed
            # and that no new CURRENT was created via alternate path
            # Also ensure JSONDecodeError was not raised by bootstrap (already asserted via ValidationError)
            try:
                _ = store.read_current()
                # If read somehow succeeded, it must not be a valid rev-0001
                assert False, "read_current should still be malformed JSON, not a valid revision"
            except _json.JSONDecodeError:
                pass  # expected — malformed remains, bootstrap did not fix it
            except ValidationError:
                pass
            # Sanity: legitimate (non-symlinked) bootstrap still seeds correctly after cleanup
        # Separate sanity scope: non-symlinked bootstrap succeeds
    with tempfile.TemporaryDirectory() as tmp2:
        store2 = _init_store(tmp2)
        _seed_inbox(store2, ts="20260826T120000Z")
        result = bootstrap(store2)
        assert result["revision_id"] == "rev-0001"
        cur = store2.read_current()
        assert cur["revision_id"] == "rev-0001"
        assert store2.snapshot_dir("rev-0001").exists()


# --- Round 6: TOCTOU full boundary re-validation (promote pre-move hardening) ---

def test_z_toctou_state_dir_replaced_with_file_during_compute_next_revision_id():
    """TOCTOU integration: mutate candidate state/ into regular file during compute_next_revision_id -> REJECTED."""
    import shutil as _sh
    from pact_v4.snapshot import promote as _promote_mod
    from pact_v4.snapshot.store import BookStore as _BS
    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)
        _seed_inbox(store)
        bootstrap(store)
        assert store.read_current()["revision_id"] == "rev-0001"
        _create_candidate(store, "cand-toctou", parent_rev="rev-0001")
        cand_dir = store.incoming_candidate_path("cand-toctou")
        assert (cand_dir / "state").is_dir()
        orig_compute = _BS.compute_next_revision_id

        def _mutating_compute(self):
            # TOCTOU side effect: replace state/ directory with regular file on disk
            state_path = cand_dir / "state"
            if state_path.is_dir() and not state_path.is_symlink():
                _sh.rmtree(state_path)
                state_path.write_text("replaced state with file", encoding="utf-8")
            return orig_compute(self)

        # Monkeypatch bound method via class
        _BS.compute_next_revision_id = _mutating_compute
        try:
            with pytest.raises((ValidationError, HashMismatch)):
                promote(store, "cand-toctou")
        finally:
            _BS.compute_next_revision_id = orig_compute
        # Must be quarantined, CURRENT unchanged, no rev-0002 created
        assert store.quarantine_candidate_path("cand-toctou").exists()
        assert not store.incoming_candidate_path("cand-toctou").exists()
        assert store.read_current()["revision_id"] == "rev-0001"
        assert not store.snapshot_dir("rev-0002").exists()
        assert not (store.snapshots_dir / "rev-0002").exists()
        # Snapshot state must not be a file
        assert not store.lease_path().exists()
        # Ensure quarantined candidate does not become a valid snapshot
        q = store.quarantine_candidate_path("cand-toctou")
        # state is now a file inside quarantine (the mutated artifact was quarantined)
        assert (q / "state").is_file()


def test_z2_validate_candidate_boundary_rejects_mutated_state_file():
    """Direct unit: validate_candidate_boundary rejects state/ replaced with file."""
    import shutil as _sh
    from pact_v4.snapshot.promote import validate_candidate_boundary
    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)
        _seed_inbox(store)
        bootstrap(store)
        cand_dir, _ = _create_candidate(store, "cand-validate", parent_rev="rev-0001")
        # Valid first
        m = validate_candidate_boundary(cand_dir)
        assert m.book_id == BOOK_ID
        # Mutate state/ into regular file
        _sh.rmtree(cand_dir / "state")
        (cand_dir / "state").write_text("not a dir", encoding="utf-8")
        with pytest.raises(ValidationError):
            validate_candidate_boundary(cand_dir)
        # Restore to valid then mutate via extra top-level
        _sh.rmtree(cand_dir / "state") if (cand_dir / "state").is_symlink() else None
        try:
            (cand_dir / "state").unlink()
        except Exception:
            pass
        (cand_dir / "state").mkdir()
        for fname in CANONICAL:
            _make_json_file(cand_dir / "state" / fname, {"file": fname, "v": 1})
        # Need to recreate manifest hashes to be valid again for next check
        # Rebuild manifest with correct hashes for restored state
        import json as _json
        raw = _json.loads((cand_dir / "manifest.json").read_text(encoding="utf-8"))
        new_files = []
        for fname in CANONICAL:
            sha, size = compute_sha256_and_size(cand_dir / "state" / fname)
            new_files.append({"rel_path": f"state/{fname}", "sha256": sha, "size": size})
        raw["state_files"] = new_files
        _make_json_file(cand_dir / "manifest.json", raw)
        # Now valid again
        validate_candidate_boundary(cand_dir)
        # Extra top-level file
        (cand_dir / "credentials.env").write_text("SECRET=1", encoding="utf-8")
        with pytest.raises(ValidationError):
            validate_candidate_boundary(cand_dir)
        (cand_dir / "credentials.env").unlink()
        # Missing canonical file
        (cand_dir / "state" / "glossary.json").unlink()
        with pytest.raises((ValidationError, HashMismatch)):
            validate_candidate_boundary(cand_dir)
