"""Run-hook integration tests with fake media."""

import json
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


def _seed_store(tmp):
    store = BookStore(BOOK_ID, root=tmp)
    store.init_store()
    inbox = store.bootstrap_inbox_dir / "20260826T120000Z"
    inbox.mkdir(parents=True, exist_ok=True)
    for fname in CANONICAL:
        _make_json_file(inbox / fname, {"seed": fname})
    bootstrap(store)
    return store


def test_pre_init_fetch_writes_media_authoritative_state():
    with tempfile.TemporaryDirectory() as tmp:
        store = _seed_store(tmp)
        transport = FakeTransport(tmp, book_id=BOOK_ID)
        with tempfile.TemporaryDirectory() as wdir:
            wdir_p = Path(wdir)
            cur = pre_init_fetch(BOOK_ID, wdir_p, transport=transport)
            assert cur["revision_id"] == "rev-0001"
            for fname in CANONICAL:
                assert (wdir_p / fname).is_file()
                data = json.loads((wdir_p / fname).read_text(encoding="utf-8"))
                assert data["seed"] == fname


def test_pre_init_fetch_fails_fast_on_unreachable():
    # Transport that raises
    class FailingTransport:
        def fetch_current(self, book_id, dest_dir):
            raise RuntimeError("media unreachable")

    with tempfile.TemporaryDirectory() as wdir:
        wdir_p = Path(wdir)
        # Seed a stale local file that should NOT be used
        for fname in CANONICAL:
            _make_json_file(wdir_p / fname, {"stale": True})
        with pytest.raises(RuntimeError, match="media unreachable"):
            pre_init_fetch(BOOK_ID, wdir_p, transport=FailingTransport())
        # Local state should still exist but run would have failed fast (caller should not proceed)
        assert (wdir_p / "glossary.json").exists()


def test_post_promote_push_records_revision_id():
    with tempfile.TemporaryDirectory() as tmp:
        store = _seed_store(tmp)
        transport = FakeTransport(tmp, book_id=BOOK_ID)
        with tempfile.TemporaryDirectory() as wdir:
            wdir_p = Path(wdir)
            # First fetch
            pre_init_fetch(BOOK_ID, wdir_p, transport=transport)
            # Simulate MemoryManager.promote('complete') updating files
            for fname in CANONICAL:
                _make_json_file(wdir_p / fname, {"updated": fname, "v": 5})
            verdict = post_promote_push(BOOK_ID, wdir_p, transport=transport)
            assert verdict["status"] == "ACCEPTED"
            assert verdict["revision_id"] == "rev-0002"
            assert store.read_current()["revision_id"] == "rev-0002"


def test_stale_parent_bounded_retry():
    with tempfile.TemporaryDirectory() as tmp:
        store = _seed_store(tmp)
        # Create a transport that simulates external advance between fetch and push
        class StaleOnceTransport(FakeTransport):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.push_calls = 0

            def push_candidate(self, book_id, candidate_id, local_dir, manifest_dict=None):
                self.push_calls += 1
                if self.push_calls == 1:
                    # Simulate someone else promoted to rev-0002 before our push
                    # Manually advance store to rev-0002 via a dummy candidate
                    import datetime, io, tarfile, json
                    from pact_v4.snapshot.manifest import compute_sha256_and_size
                    from pact_v4.snapshot.cli import _receive_candidate_stream
                    from pact_v4.snapshot.promote import promote
                    # Build a dummy candidate that will win the race
                    with tempfile.TemporaryDirectory() as race_dir:
                        race_p = Path(race_dir)
                        for fname in CANONICAL:
                            _make_json_file(race_p / fname, {"race": True})
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
                        _receive_candidate_stream(self.store, "race-cand", bio.getvalue())
                        promote(self.store, "race-cand", operator="rt", host="RT")
                    # Now our original push will be stale
                    return super().push_candidate(book_id, candidate_id, local_dir, manifest_dict)
                return super().push_candidate(book_id, candidate_id, local_dir, manifest_dict)

        transport = StaleOnceTransport(tmp, book_id=BOOK_ID)
        with tempfile.TemporaryDirectory() as wdir:
            wdir_p = Path(wdir)
            pre_init_fetch(BOOK_ID, wdir_p, transport=transport)
            for fname in CANONICAL:
                _make_json_file(wdir_p / fname, {"updated": "hook"})
            # post_promote_push should retry once after STALE_PARENT
            verdict = post_promote_push(BOOK_ID, wdir_p, transport=transport, max_retries=1)
            assert verdict["status"] == "ACCEPTED"
            # After race, revisions: rev-0001 -> race -> rev-0002, then retry -> rev-0003
            assert verdict["revision_id"] == "rev-0003"
            assert transport.push_calls == 2


def test_post_push_rejection_preserves_local_state():
    with tempfile.TemporaryDirectory() as tmp:
        store = _seed_store(tmp)
        transport = FakeTransport(tmp, book_id=BOOK_ID)
        with tempfile.TemporaryDirectory() as wdir:
            wdir_p = Path(wdir)
            pre_init_fetch(BOOK_ID, wdir_p, transport=transport)
            # Create an invalid state file (missing one) to trigger validation error before push
            # Instead create valid then corrupt manifest via transport to cause HashMismatch?
            # Use transport that forces hash mismatch by tampering hash
            class TamperTransport(FakeTransport):
                def push_candidate(self, book_id, candidate_id, local_dir, manifest_dict=None):
                    # Build manifest with wrong hash for first file
                    import datetime
                    from pact_v4.snapshot.manifest import compute_sha256_and_size
                    cur = self.store.read_current()
                    now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
                    state_files = []
                    for idx, fname in enumerate(CANONICAL):
                        h, sz = compute_sha256_and_size(Path(local_dir) / fname)
                        if idx == 0:
                            h = "0" * 64  # tamper
                        state_files.append({"rel_path": f"state/{fname}", "sha256": h, "size": sz})
                    manifest = {
                        "schema_version": "1.0.0",
                        "book_id": book_id,
                        "revision_id": "rev-0000",
                        "parent_revision_id": cur["revision_id"],
                        "created_at": now, "published_at": now,
                        "terminal_status": "complete",
                        "tool_version": "pact-snapshot/0.1.0",
                        "source": {"path_on_rt": str(local_dir), "operator": "rt", "host": "RT"},
                        "state_files": state_files, "excludes": [], "code_commit": "unknown",
                    }
                    return super().push_candidate(book_id, candidate_id, local_dir, manifest)

            tamper = TamperTransport(tmp, book_id=BOOK_ID)
            for fname in CANONICAL:
                _make_json_file(wdir_p / fname, {"ok": fname, "v": 2})
            # snapshot local files before push
            before = {fname: (wdir_p / fname).read_bytes() for fname in CANONICAL}
            with pytest.raises(RuntimeError):
                post_promote_push(BOOK_ID, wdir_p, transport=tamper)
            # Local state preserved
            for fname in CANONICAL:
                assert (wdir_p / fname).read_bytes() == before[fname]
            # Media not advanced
            assert store.read_current()["revision_id"] == "rev-0001"


def test_state_only_boundary_never_translation_bodies():
    with tempfile.TemporaryDirectory() as tmp:
        store = _seed_store(tmp)
        transport = FakeTransport(tmp, book_id=BOOK_ID)
        with tempfile.TemporaryDirectory() as wdir:
            wdir_p = Path(wdir)
            pre_init_fetch(BOOK_ID, wdir_p, transport=transport)
            # Simulate that wdir also contains translation bodies (should not be pushed)
            (wdir_p / "chapter_0001.txt").write_text("translation body", encoding="utf-8")
            for fname in CANONICAL:
                _make_json_file(wdir_p / fname, {"v": 9})
            verdict = post_promote_push(BOOK_ID, wdir_p, transport=transport)
            assert verdict["status"] == "ACCEPTED"
            # Verify snapshot does not contain translation body
            snap = store.snapshot_dir(verdict["revision_id"])
            assert not (snap / "chapter_0001.txt").exists()
            assert not (snap / "state" / "chapter_0001.txt").exists()
