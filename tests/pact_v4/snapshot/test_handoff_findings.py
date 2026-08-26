"""Finding regression tests for book-state-rt-runner-integration pass 1."""

import io
import json
import os
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

from pact_v4.snapshot.store import BookStore
from pact_v4.snapshot.bootstrap import bootstrap
from pact_v4.snapshot.run_hooks import pre_init_fetch, post_promote_push
from tests.pact_v4.snapshot.test_remote_client import FakeTransport, CANONICAL

BOOK_ID = "hook-book"


def _make_json_file(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)
        f.write("\n")


def _seed_store(tmp, book_id=BOOK_ID):
    store = BookStore(book_id, root=tmp)
    store.init_store()
    inbox = store.bootstrap_inbox_dir / "20260826T120000Z"
    inbox.mkdir(parents=True, exist_ok=True)
    for fname in CANONICAL:
        _make_json_file(inbox / fname, {"seed": fname})
    bootstrap(store)
    return store


# Finding 1: facade scoped book-id rejects different valid book-id

def test_facade_scoped_book_id_rejects_other_valid_id():
    # Valid but different book-id should be rejected when PACT_SNAPSHOT_BOOK_ID is set
    from pact_v4.snapshot.remote_facade import handle_request
    with tempfile.TemporaryDirectory() as tmp:
        _seed_store(tmp, BOOK_ID)
        # Allowed book is hook-book; request other-book (valid syntax) should be rejected
        other = "other-book"
        # Ensure other-book is syntactically valid
        env = os.environ.copy()
        os.environ["PACT_SNAPSHOT_BOOK_ID"] = BOOK_ID
        try:
            import sys
            from io import BytesIO
            buf = BytesIO()
            class DummyStdout:
                def __init__(self, b):
                    self.buffer = b
                def write(self, s):
                    b2 = s.encode("utf-8") if isinstance(s, str) else s
                    buf.write(b2)
                def flush(self): pass
            old = sys.stdout
            sys.stdout = DummyStdout(buf)  # type: ignore
            try:
                rc = handle_request(["fetch-current", other], root=tmp)
            finally:
                sys.stdout = old
            assert rc != 0, "scoped facade must reject different valid book-id"
            out = buf.getvalue().decode("utf-8", errors="replace")
            assert "FACADE_REJECTED" in out or "not allowed" in out
            # Ensure no store was created for other-book
            assert not (Path(tmp) / other).exists()
            # Allowed book still works
            buf2 = BytesIO()
            sys.stdout = DummyStdout(buf2)  # type: ignore
            try:
                rc2 = handle_request(["fetch-current", BOOK_ID], root=tmp)
            finally:
                sys.stdout = old
            assert rc2 == 0
        finally:
            if "PACT_SNAPSHOT_BOOK_ID" in os.environ:
                del os.environ["PACT_SNAPSHOT_BOOK_ID"]


# Finding 2: receive-candidate invokes boundary validation

def test_receive_candidate_rejects_invalid_boundary_before_move():
    with tempfile.TemporaryDirectory() as tmp:
        store = _seed_store(tmp)
        from pact_v4.snapshot.cli import _receive_candidate_stream
        from pact_v4.snapshot.manifest import compute_sha256_and_size
        import datetime
        # Build a candidate where manifest hash does not match actual file
        with tempfile.TemporaryDirectory() as ldir:
            ldir_p = Path(ldir)
            for fname in CANONICAL:
                _make_json_file(ldir_p / fname, {"v": 1, "f": fname})
            cur = store.read_current()
            now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
            state_files = []
            for idx, fname in enumerate(CANONICAL):
                h, sz = compute_sha256_and_size(ldir_p / fname)
                if idx == 0:
                    h = "0" * 64  # tampered hash
                state_files.append({"rel_path": f"state/{fname}", "sha256": h, "size": sz})
            manifest = {
                "schema_version": "1.0.0",
                "book_id": BOOK_ID,
                "revision_id": "rev-0000",
                "parent_revision_id": cur["revision_id"],
                "created_at": now, "published_at": now,
                "terminal_status": "complete",
                "tool_version": "pact-snapshot/0.1.0",
                "source": {"path_on_rt": str(ldir_p), "operator": "rt", "host": "RT"},
                "state_files": state_files, "excludes": [], "code_commit": "unknown",
            }
            bio = io.BytesIO()
            with tarfile.open(fileobj=bio, mode="w", format=tarfile.PAX_FORMAT) as tar:
                m_bytes = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
                ti = tarfile.TarInfo(name="manifest.json"); ti.size = len(m_bytes); ti.mtime = 0; ti.mode = 0o644
                tar.addfile(ti, io.BytesIO(m_bytes))
                for fname in CANONICAL:
                    data = (ldir_p / fname).read_bytes()
                    ti2 = tarfile.TarInfo(name=f"state/{fname}"); ti2.size = len(data); ti2.mtime = 0; ti2.mode = 0o644
                    tar.addfile(ti2, io.BytesIO(data))
            tar_bytes = bio.getvalue()
            # Should be rejected by validate_candidate_boundary inside receive
            with pytest.raises(Exception, match="(?i)(Hash|mismatch|hash)"):
                _receive_candidate_stream(store, "cand-bad-hash", tar_bytes)
            # Ensure not moved to incoming
            assert not (store.incoming_dir / "cand-bad-hash").exists()
            # Also ensure quarantined? For receive path, it cleans tmp but does not quarantine (promote does). But at least not in incoming.
            # Candidate with extra file should also be rejected (tar allow-list already, but boundary also checks)
            # No state pollution
            assert store.read_current()["revision_id"] == "rev-0001"


# Finding 3: STALE_PARENT retry preserves RT update content

def test_stale_parent_retry_preserves_rt_content():
    with tempfile.TemporaryDirectory() as tmp:
        store = _seed_store(tmp)
        # Transport that simulates external advance on first push
        class StaleOnceTransport(FakeTransport):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.push_calls = 0

            def push_candidate(self, book_id, candidate_id, local_dir, manifest_dict=None):
                self.push_calls += 1
                if self.push_calls == 1:
                    # Advance store externally to rev-0002 with race content
                    import datetime, io, tarfile, json
                    from pact_v4.snapshot.manifest import compute_sha256_and_size
                    from pact_v4.snapshot.cli import _receive_candidate_stream
                    from pact_v4.snapshot.promote import promote
                    with tempfile.TemporaryDirectory() as race_dir:
                        race_p = Path(race_dir)
                        for fname in CANONICAL:
                            _make_json_file(race_p / fname, {"race": True, "fname": fname})
                        cur = self.store.read_current()
                        now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
                        state_files = []
                        for fname in CANONICAL:
                            h, sz = compute_sha256_and_size(race_p / fname)
                            state_files.append({"rel_path": f"state/{fname}", "sha256": h, "size": sz})
                        manifest = {
                            "schema_version": "1.0.0",
                            "book_id": book_id,
                            "revision_id": "rev-0000",
                            "parent_revision_id": cur["revision_id"],
                            "created_at": now, "published_at": now,
                            "terminal_status": "complete",
                            "tool_version": "pact-snapshot/0.1.0",
                            "source": {"path_on_rt": str(race_p), "operator": "rt", "host": "RT"},
                            "state_files": state_files, "excludes": [], "code_commit": "unknown",
                        }
                        bio = io.BytesIO()
                        with tarfile.open(fileobj=bio, mode="w", format=tarfile.PAX_FORMAT) as tar:
                            m_bytes = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
                            ti = tarfile.TarInfo(name="manifest.json"); ti.size = len(m_bytes); ti.mtime = 0; ti.mode = 0o644
                            tar.addfile(ti, io.BytesIO(m_bytes))
                            for fname in CANONICAL:
                                data = (race_p / fname).read_bytes()
                                ti2 = tarfile.TarInfo(name=f"state/{fname}"); ti2.size = len(data); ti2.mtime = 0; ti2.mode = 0o644
                                tar.addfile(ti2, io.BytesIO(data))
                        _receive_candidate_stream(self.store, "race-cand-preserve", bio.getvalue())
                        promote(self.store, "race-cand-preserve", operator="rt", host="RT")
                    return super().push_candidate(book_id, candidate_id, local_dir, manifest_dict)
                return super().push_candidate(book_id, candidate_id, local_dir, manifest_dict)

        transport = StaleOnceTransport(tmp, book_id=BOOK_ID)
        with tempfile.TemporaryDirectory() as wdir:
            wdir_p = Path(wdir)
            pre_init_fetch(BOOK_ID, wdir_p, transport=transport)
            # RT updated content
            rt_content = {"rt_updated": True, "v": 999, "marker": "rt-preserve-test"}
            for fname in CANONICAL:
                _make_json_file(wdir_p / fname, {**rt_content, "fname": fname})
            # Save RT content for later comparison
            rt_bytes = {fname: (wdir_p / fname).read_bytes() for fname in CANONICAL}
            verdict = post_promote_push(BOOK_ID, wdir_p, transport=transport, max_retries=1)
            assert verdict["status"] == "ACCEPTED"
            # Published revision should contain RT content, not race content
            snap_dir = store.snapshot_dir(verdict["revision_id"])
            for fname in CANONICAL:
                data = json.loads((snap_dir / "state" / fname).read_text(encoding="utf-8"))
                assert data.get("rt_updated") is True, f"snapshot {fname} should contain RT update, got {data}"
                assert data.get("race") is None, f"snapshot {fname} should NOT contain race content"
                assert data["marker"] == "rt-preserve-test"
            # Working dir files should still be RT content (not overwritten by re-pull)
            for fname in CANONICAL:
                assert (wdir_p / fname).read_bytes() == rt_bytes[fname]


# Finding 4: accepted revision advances local parent pointer, consecutive pushes use new parent

def test_accepted_advances_pointer_and_consecutive_pushes():
    with tempfile.TemporaryDirectory() as tmp:
        store = _seed_store(tmp)
        transport = FakeTransport(tmp, book_id=BOOK_ID)
        with tempfile.TemporaryDirectory() as wdir:
            wdir_p = Path(wdir)
            pre_init_fetch(BOOK_ID, wdir_p, transport=transport)
            # First chapter push
            for fname in CANONICAL:
                _make_json_file(wdir_p / fname, {"chapter": 1, "v": 10})
            verdict1 = post_promote_push(BOOK_ID, wdir_p, transport=transport, max_retries=1)
            assert verdict1["status"] == "ACCEPTED"
            assert verdict1["revision_id"] == "rev-0002"
            # Local CURRENT.json should have been advanced
            cur = json.loads((wdir_p / "CURRENT.json").read_text(encoding="utf-8"))
            assert cur["revision_id"] == "rev-0002", f"local CURRENT should advance to rev-0002, got {cur}"
            # Second chapter push without manual fetch – should use advanced pointer
            for fname in CANONICAL:
                _make_json_file(wdir_p / fname, {"chapter": 2, "v": 20})
            verdict2 = post_promote_push(BOOK_ID, wdir_p, transport=transport, max_retries=1)
            assert verdict2["status"] == "ACCEPTED"
            assert verdict2["revision_id"] == "rev-0003", f"second push should advance to rev-0003, got {verdict2}"
            cur2 = json.loads((wdir_p / "CURRENT.json").read_text(encoding="utf-8"))
            assert cur2["revision_id"] == "rev-0003"
            assert store.read_current()["revision_id"] == "rev-0003"
