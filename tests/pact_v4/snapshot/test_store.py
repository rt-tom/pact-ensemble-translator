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


def _create_candidate(store: BookStore, candidate_id, parent_rev, terminal="complete", tamper_hash=False):
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
    manifest_dict = {
        "schema_version": "1.0.0",
        "book_id": store.book_id,
        "revision_id": store.compute_next_revision_id(),
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


# (d) promote ACCEPTS valid candidate
def test_d_promote_accepts():
    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)
        _seed_inbox(store)
        bootstrap(store)
        cur_before = store.read_current()
        assert cur_before["revision_id"] == "rev-0001"
        _create_candidate(store, "cand-001", parent_rev="rev-0001")
        result = promote(store, "cand-001")
        assert result["status"] == "ACCEPTED"
        assert result["revision_id"] == "rev-0002"
        cur_after = store.read_current()
        assert cur_after["revision_id"] == "rev-0002"
        # lease must be released (no lease file)
        assert not store.lease_path().exists()
        # snapshot exists
        assert store.snapshot_dir("rev-0002").exists()
        # candidate no longer in incoming
        assert not store.incoming_candidate_path("cand-001").exists()
        # not quarantined
        assert not store.quarantine_candidate_path("cand-001").exists()


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
        _create_candidate(store, "cand-lease-retry", parent_rev="rev-0001")
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
