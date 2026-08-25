"""Facade allow-list tests."""

import io
import json
import os
import tempfile
from pathlib import Path

import pytest

from pact_v4.snapshot.remote_facade import handle_request
from pact_v4.snapshot.store import BookStore
from pact_v4.snapshot.bootstrap import bootstrap

BOOK_ID = "facade-book"
CANONICAL = ["book_memory.json", "glossary.json", "chapter_index.json", "observations.json"]
@pytest.fixture(autouse=True)
def _scoped_env():
    # Fail-closed default: every test runs scoped to BOOK_ID unless it explicitly clears env
    prev = os.environ.get("PACT_SNAPSHOT_BOOK_ID")
    os.environ["PACT_SNAPSHOT_BOOK_ID"] = BOOK_ID
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("PACT_SNAPSHOT_BOOK_ID", None)
        else:
            os.environ["PACT_SNAPSHOT_BOOK_ID"] = prev




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
        _make_json_file(inbox / fname, {"ok": fname})
    bootstrap(store)
    return store

@pytest.mark.parametrize("scoped_val", [None, "", "   "])
@pytest.mark.parametrize("tokens", [
    ["fetch-current", BOOK_ID],
    ["receive-candidate", BOOK_ID, "cand-1"],
    ["promote", BOOK_ID, "cand-1"],
    ["release-lease", BOOK_ID, "--check-expired"],
])
def test_facade_rejects_when_scoped_unset_or_empty(scoped_val, tokens):
    """Fail-closed: unset/empty/whitespace PACT_SNAPSHOT_BOOK_ID must REJECT every valid command form."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _seed_store(tmp)
        import sys
        from io import BytesIO
        # Capture CURRENT.json bytes before the call
        current_before = store.current_path.read_bytes()
        fresh = "new-book-should-not-exist"
        fresh_path = Path(tmp) / "books" / fresh
        assert not fresh_path.exists()
        old = os.environ.pop("PACT_SNAPSHOT_BOOK_ID", None)
        try:
            if scoped_val is not None:
                os.environ["PACT_SNAPSHOT_BOOK_ID"] = scoped_val
            buf = BytesIO()
            class DummyStdout:
                def __init__(self, b):
                    self.buffer = b
                def write(self, s):
                    b = s.encode("utf-8") if isinstance(s, str) else s
                    buf.write(b)
                def flush(self): pass
            orig_stdout = sys.stdout
            # receive-candidate would read stdin if scoped; it must not reach that point when unscoped,
            # but provide dummy stdin to ensure no hang if implementation changes
            import io as _io
            orig_stdin = sys.stdin
            dummy_stdin_buf = _io.BytesIO(b"")
            class DummyStdin:
                def __init__(self, b):
                    self.buffer = b
            sys.stdout = DummyStdout(buf)  # type: ignore
            sys.stdin = DummyStdin(dummy_stdin_buf)  # type: ignore
            try:
                rc = handle_request(tokens, root=tmp)
            finally:
                sys.stdout = orig_stdout
                sys.stdin = orig_stdin
            assert rc != 0, f"should reject tokens={tokens} when scoped={scoped_val!r}"
            out = buf.getvalue().decode("utf-8", errors="replace")
            assert "FACADE_REJECTED" in out, out
            assert "PACT_SNAPSHOT_BOOK_ID" in out, out
            # NO BookStore construction: sentinel fresh dir must not exist
            assert not fresh_path.exists(), "facade must not create BookStore when unscoped"
            # Also ensure the token's book dir was not newly created beyond the seeded one
            # (if tokens used a fresh id we check it; here tokens use seeded BOOK_ID which already exists,
            # so verify no extra sentinel was created and incoming not created for receive-candidate)
            if tokens[0] == "receive-candidate":
                assert not (store.incoming_dir / "cand-1").exists(), "receive-candidate must not create incoming when unscoped"
            # CURRENT.json bytes are UNCHANGED before vs after
            current_after = store.current_path.read_bytes()
            assert current_before == current_after, "CURRENT.json must be unchanged when facade rejects unscoped request"
        finally:
            if old is not None:
                os.environ["PACT_SNAPSHOT_BOOK_ID"] = old
            elif scoped_val is not None:
                os.environ.pop("PACT_SNAPSHOT_BOOK_ID", None)
            else:
                # scoped_val was None, we popped; restore to BOOK_ID for autouse fixture consistency
                if old is None:
                    os.environ.pop("PACT_SNAPSHOT_BOOK_ID", None)

def test_facade_allowed_fetch_current(tmp_path_factory=None):
    with tempfile.TemporaryDirectory() as tmp:
        store = _seed_store(tmp)
        # Capture stdout.buffer via handle_request
        import sys
        from io import BytesIO
        # Monkey patch sys.stdout.buffer to BytesIO for fetch-current binary
        # handle_request writes tar to sys.stdout.buffer; we capture via replacing
        old_stdout = sys.stdout
        old_stdin = sys.stdin
        # Use custom buffer
        buf = BytesIO()
        class DummyStdout:
            def __init__(self, buf):
                self.buffer = buf
            def write(self, s):
                # For JSON error path, sys.stdout.write called
                if isinstance(s, str):
                    buf.write(s.encode("utf-8"))
                else:
                    buf.write(s)
            def flush(self):
                pass
        sys.stdout = DummyStdout(buf)  # type: ignore
        try:
            rc = handle_request(["fetch-current", BOOK_ID], root=tmp)
        finally:
            sys.stdout = old_stdout
        assert rc == 0
        data = buf.getvalue()
        assert len(data) > 0
        # Validate tar contains exactly expected entries
        import tarfile, io
        bio = io.BytesIO(data)
        with tarfile.open(fileobj=bio, mode="r:*") as tar:
            names = {m.name for m in tar.getmembers() if not m.isdir()}
            assert names == {"CURRENT.json", "manifest.json", "state/glossary.json", "state/book_memory.json", "state/chapter_index.json", "state/observations.json"}


def test_facade_rejects_disallowed_subcommand():
    with tempfile.TemporaryDirectory() as tmp:
        import sys
        from io import BytesIO, StringIO
        buf = BytesIO()
        class DummyStdout:
            def __init__(self, buf):
                self.buffer = buf
            def write(self, s):
                buf.write(s.encode("utf-8") if isinstance(s, str) else s)
            def flush(self): pass
        old_stdout = sys.stdout
        sys.stdout = DummyStdout(buf)  # type: ignore
        try:
            rc = handle_request(["init-store", BOOK_ID], root=tmp)
        finally:
            sys.stdout = old_stdout
        assert rc != 0
        out = buf.getvalue().decode("utf-8", errors="replace")
        assert "FACADE_REJECTED" in out or "REJECTED" in out


def test_facade_rejects_wrong_book_id_and_extra_args():
    with tempfile.TemporaryDirectory() as tmp:
        _seed_store(tmp)
        import sys
        from io import BytesIO
        for tokens in [
            ["fetch-current", "../escape"],
            ["fetch-current", BOOK_ID, "extra"],
            ["promote", BOOK_ID],  # missing candidate_id
            ["receive-candidate", BOOK_ID],  # missing
            ["release-lease", BOOK_ID],  # missing --check-expired
            ["release-lease", BOOK_ID, "--check-expired", "extra"],
            ["fetch-current", "/etc/passwd"],
        ]:
            buf = BytesIO()
            class DummyStdout:
                def __init__(self, buf):
                    self.buffer = buf
                def write(self, s):
                    buf.write(s.encode("utf-8") if isinstance(s, str) else s)
                def flush(self): pass
            old_stdout = sys.stdout
            sys.stdout = DummyStdout(buf)  # type: ignore
            try:
                rc = handle_request(tokens, root=tmp)
            finally:
                sys.stdout = old_stdout
            assert rc != 0, f"should reject {tokens}"
            # Ensure no side effect: CURRENT still exists and unchanged
            store = BookStore(BOOK_ID, root=tmp)
            assert store.current_path.exists()


def test_facade_receive_and_promote_allowed():
    with tempfile.TemporaryDirectory() as tmp:
        store = _seed_store(tmp)
        # Build candidate tar via cli helper
        # Build via remote_client helper
        from pact_v4.snapshot import remote_client
        import tempfile as tf
        with tf.TemporaryDirectory() as ldir:
            ldir_p = Path(ldir)
            for fname in CANONICAL:
                _make_json_file(ldir_p / fname, {"v": 1, "f": fname})
            # Create CURRENT.json for parent lookup
            cur = store.read_current()
            (ldir_p / "CURRENT.json").write_text(json.dumps(cur), encoding="utf-8")
            # Use fake transport via direct promotion? Build tar manually
            # Use remote_client internal builder
            from pact_v4.snapshot.manifest import compute_sha256_and_size
            import datetime
            now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
            state_files = []
            for fname in CANONICAL:
                h, sz = compute_sha256_and_size(ldir_p / fname)
                state_files.append({"rel_path": f"state/{fname}", "sha256": h, "size": sz})
            manifest = {
                "schema_version": "1.0.0",
                "book_id": BOOK_ID,
                "revision_id": "rev-0000",
                "parent_revision_id": cur["revision_id"],
                "created_at": now,
                "published_at": now,
                "terminal_status": "complete",
                "tool_version": "pact-snapshot/0.1.0",
                "source": {"path_on_rt": str(ldir_p), "operator": "rt", "host": "RT"},
                "state_files": state_files,
                "excludes": [],
                "code_commit": "unknown",
            }
            import io, tarfile
            bio = io.BytesIO()
            with tarfile.open(fileobj=bio, mode="w", format=tarfile.PAX_FORMAT) as tar:
                m_bytes = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
                ti = tarfile.TarInfo(name="manifest.json")
                ti.size = len(m_bytes); ti.mtime = 0; ti.mode = 0o644
                tar.addfile(ti, io.BytesIO(m_bytes))
                for fname in CANONICAL:
                    data = (ldir_p / fname).read_bytes()
                    ti2 = tarfile.TarInfo(name=f"state/{fname}")
                    ti2.size = len(data); ti2.mtime = 0; ti2.mode = 0o644
                    tar.addfile(ti2, io.BytesIO(data))
            tar_bytes = bio.getvalue()
            # Now via facade receive-candidate
            import sys
            from io import BytesIO
            old_stdout = sys.stdout
            old_stdin = sys.stdin
            buf_out = BytesIO()
            buf_in = BytesIO(tar_bytes)
            class DummyStdout2:
                def __init__(self, b):
                    self.buffer = b
                def write(self, s):
                    b = s.encode("utf-8") if isinstance(s, str) else s
                    buf_out.write(b)
                def flush(self): pass
            class DummyStdin:
                def __init__(self, b):
                    self.buffer = b
            sys.stdout = DummyStdout2(buf_out)  # type: ignore
            sys.stdin = DummyStdin(buf_in)  # type: ignore
            try:
                rc = handle_request(["receive-candidate", BOOK_ID, "cand-facade-1"], root=tmp)
            finally:
                sys.stdout = old_stdout
                sys.stdin = old_stdin
            assert rc == 0
            assert (store.incoming_dir / "cand-facade-1" / "manifest.json").exists()
            # Now promote via facade
            buf2 = BytesIO()
            sys.stdout = DummyStdout2(buf2)  # type: ignore
            # Need fresh dummy for promote
            class DummyStdout3:
                def __init__(self, b):
                    self.buffer = b
                    self._buf = b
                def write(self, s):
                    b = s.encode("utf-8") if isinstance(s, str) else s
                    self._buf.write(b)
                def flush(self): pass
            sys.stdout = DummyStdout3(buf2)  # type: ignore
            try:
                rc2 = handle_request(["promote", BOOK_ID, "cand-facade-1"], root=tmp)
            finally:
                sys.stdout = old_stdout
            assert rc2 == 0
            out2 = buf2.getvalue().decode("utf-8")
            j = json.loads(out2)
            assert j["status"] == "ACCEPTED"
            assert j["revision_id"] == "rev-0002"
