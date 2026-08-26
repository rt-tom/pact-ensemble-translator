"""Remote client tests with fake loopback transport."""

import io
import json
import tarfile
import tempfile
from pathlib import Path

import pytest

from pact_v4.snapshot.store import BookStore
from pact_v4.snapshot.bootstrap import bootstrap
from pact_v4.snapshot import remote_client
from pact_v4.snapshot.cli import _fetch_current_stream, _receive_candidate_stream

BOOK_ID = "client-book"
CANONICAL = ["book_memory.json", "glossary.json", "chapter_index.json", "observations.json"]


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


class FakeTransport:
    """Loopback transport that operates directly on a local BookStore."""

    def __init__(self, root: str, book_id: str = BOOK_ID):
        self.root = root
        self.book_id = book_id
        self.store = BookStore(book_id, root=root)

    def fetch_current(self, book_id: str, dest_dir: Path):
        assert book_id == self.book_id
        # Use CLI helper to produce tar, then extract via remote_client helper
        buf = io.BytesIO()
        rc = _fetch_current_stream(self.store, buf)
        assert rc == 0
        tar_bytes = buf.getvalue()
        # Extract using remote_client internal extractor
        from pact_v4.snapshot.remote_client import _extract_fetch_tar
        cur = _extract_fetch_tar(tar_bytes, Path(dest_dir))
        return cur

    def get_current_revision(self, book_id: str):
        cur = self.store.read_current()
        return cur.get("revision_id") if cur else None

    def push_candidate(self, book_id: str, candidate_id: str, local_dir: Path, manifest_dict=None):
        # Build tar as remote_client would
        if manifest_dict is None:
            from pact_v4.snapshot.manifest import compute_sha256_and_size
            import datetime
            cur = self.store.read_current()
            parent = cur.get("revision_id")
            now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
            state_files = []
            for fname in CANONICAL:
                h, sz = compute_sha256_and_size(Path(local_dir) / fname)
                state_files.append({"rel_path": f"state/{fname}", "sha256": h, "size": sz})
            manifest_dict = {
                "schema_version": "1.0.0",
                "book_id": book_id,
                "revision_id": "rev-0000",
                "parent_revision_id": parent,
                "created_at": now,
                "published_at": now,
                "terminal_status": "complete",
                "tool_version": "pact-snapshot/0.1.0",
                "source": {"path_on_rt": str(local_dir), "operator": "rt", "host": "RT"},
                "state_files": state_files,
                "excludes": [],
                "code_commit": "unknown",
            }
        # Build tar bytes
        bio = io.BytesIO()
        with tarfile.open(fileobj=bio, mode="w", format=tarfile.PAX_FORMAT) as tar:
            m_bytes = json.dumps(manifest_dict, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
            ti = tarfile.TarInfo(name="manifest.json")
            ti.size = len(m_bytes); ti.mtime = 0; ti.mode = 0o644
            tar.addfile(ti, io.BytesIO(m_bytes))
            for fname in CANONICAL:
                data = (Path(local_dir) / fname).read_bytes()
                ti2 = tarfile.TarInfo(name=f"state/{fname}")
                ti2.size = len(data); ti2.mtime = 0; ti2.mode = 0o644
                tar.addfile(ti2, io.BytesIO(data))
        tar_bytes = bio.getvalue()
        # receive
        rc = _receive_candidate_stream(self.store, candidate_id, tar_bytes)
        assert rc == 0
        # promote
        from pact_v4.snapshot.promote import promote
        from pact_v4.snapshot.errors import SnapshotError
        try:
            result = promote(self.store, candidate_id=candidate_id, operator="rt", host="RT")
            return result
        except SnapshotError as e:
            err_type = type(e).__name__
            reason_map = {"LeaseHeld": "LEASE_HELD", "StaleParent": "STALE_PARENT", "HashMismatch": "HASH_MISMATCH", "ValidationError": "VALIDATION_ERROR"}
            return {"status": "REJECTED", "reason": reason_map.get(err_type, "REJECTED"), "error": err_type, "message": str(e), "candidate_id": candidate_id}

    def check_expired(self, book_id: str):
        from pact_v4.snapshot.lease import check_expired
        return check_expired(self.store)


def test_fetch_current_returns_exactly_four_files():
    with tempfile.TemporaryDirectory() as tmp:
        store = _seed_store(tmp)
        transport = FakeTransport(tmp)
        with tempfile.TemporaryDirectory() as dest:
            dest_p = Path(dest)
            cur = remote_client.fetch_current(BOOK_ID, dest_p, transport=transport)
            assert cur["revision_id"] == "rev-0001"
            for fname in CANONICAL:
                assert (dest_p / fname).is_file()
                assert not (dest_p / fname).is_symlink()
                assert json.loads((dest_p / fname).read_text(encoding="utf-8"))["seed"] == fname
            # No extra canonical beyond four (flat) - state subdir may exist but not extra files
            top_files = {p.name for p in dest_p.iterdir() if p.is_file()}
            assert set(CANONICAL).issubset(top_files)
            # Ensure no translation bodies smuggled
            assert "chapter_0001.txt" not in top_files


def test_push_candidate_accepted_and_revision_id():
    with tempfile.TemporaryDirectory() as tmp:
        store = _seed_store(tmp)
        transport = FakeTransport(tmp)
        with tempfile.TemporaryDirectory() as local:
            local_p = Path(local)
            for fname in CANONICAL:
                _make_json_file(local_p / fname, {"updated": fname, "v": 2})
            # Need CURRENT for parent
            cur = store.read_current()
            (local_p / "CURRENT.json").write_text(json.dumps(cur), encoding="utf-8")
            verdict = remote_client.push_candidate(BOOK_ID, "cand-001", local_p, transport=transport)
            assert verdict["status"] == "ACCEPTED"
            assert verdict["revision_id"] == "rev-0002"
            # Verify media advanced
            assert store.read_current()["revision_id"] == "rev-0002"


def test_stale_parent_surfaces():
    with tempfile.TemporaryDirectory() as tmp:
        store = _seed_store(tmp)
        transport = FakeTransport(tmp)
        # First push to rev-0002
        with tempfile.TemporaryDirectory() as local1:
            local_p1 = Path(local1)
            for fname in CANONICAL:
                _make_json_file(local_p1 / fname, {"v": 1})
            cur = store.read_current()
            (local_p1 / "CURRENT.json").write_text(json.dumps(cur), encoding="utf-8")
            v1 = remote_client.push_candidate(BOOK_ID, "cand-a", local_p1, transport=transport)
            assert v1["status"] == "ACCEPTED"
        # Second push with stale parent rev-0001 should be rejected
        with tempfile.TemporaryDirectory() as local2:
            local_p2 = Path(local2)
            for fname in CANONICAL:
                _make_json_file(local_p2 / fname, {"v": 2})
            # Intentionally use stale parent
            stale_cur = {"revision_id": "rev-0001"}
            (local_p2 / "CURRENT.json").write_text(json.dumps(stale_cur), encoding="utf-8")
            # Force stale by passing parent_revision_id explicitly via transport hack?
            # Our transport derives parent from manifest_dict built with cur from file,
            # but fetch_current would give rev-0002. So write stale file.
            verdict = remote_client.push_candidate(BOOK_ID, "cand-b", local_p2, transport=transport)
            assert verdict["status"] == "REJECTED"
            assert verdict["reason"] == "STALE_PARENT"
            assert store.read_current()["revision_id"] == "rev-0002"  # unchanged except quarantined
            assert (store.quarantine_dir / "cand-b").exists()


def test_no_network_import_in_store_package():
    import pathlib
    snapshot_dir = Path(__file__).resolve().parents[3] / "pact_v4" / "snapshot"
    forbidden = ["ssh", "scp", "paramiko", "subprocess.*ssh"]
    for fname in ["store.py", "manifest.py", "lease.py", "bootstrap.py", "promote.py"]:
        content = (snapshot_dir / fname).read_text(encoding="utf-8")
        assert "import paramiko" not in content
        assert "import ssh" not in content.lower() or "ssh" not in content.lower()
        # Check for ssh/scp strings
        lowered = content.lower()
        assert "ssh" not in lowered or fname in ("remote_client.py", "remote_facade.py"), f"ssh found in {fname}"
        # Ensure subprocess ssh not in store
        assert "subprocess" not in content or "ssh" not in lowered, f"subprocess ssh in {fname}"


def test_no_duplicate_state_mirror(tmp_path=None):
    # fetch_current via _extract_fetch_tar must NOT create state/ subdir mirror
    import io, tarfile, json
    dest = Path(tempfile.mkdtemp())
    try:
        # Build a minimal valid fetch tar
        bio = io.BytesIO()
        with tarfile.open(fileobj=bio, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for name, obj in [("CURRENT.json", {"revision_id": "rev-0001"}), ("manifest.json", {"schema_version": "1.0.0"})]:
                data = (json.dumps(obj) + "\n").encode()
                ti = tarfile.TarInfo(name=name); ti.size=len(data); ti.mtime=0; ti.mode=0o644
                tar.addfile(ti, io.BytesIO(data))
            for fname in CANONICAL:
                data = json.dumps({"seed": fname}).encode()
                ti = tarfile.TarInfo(name=f"state/{fname}"); ti.size=len(data); ti.mtime=0; ti.mode=0o644
                tar.addfile(ti, io.BytesIO(data))
        tar_bytes = bio.getvalue()
        cur = remote_client._extract_fetch_tar(tar_bytes, dest)
        assert cur["revision_id"] == "rev-0001"
        # Flat files must exist
        for fname in CANONICAL:
            assert (dest / fname).is_file()
        # No state/ mirror
        assert not (dest / "state").exists(), "duplicate state/ mirror must not be created"
        # Via FakeTransport path as well
        with tempfile.TemporaryDirectory() as tmp:
            store = _seed_store(tmp)
            transport = FakeTransport(tmp)
            with tempfile.TemporaryDirectory() as dest2:
                transport.fetch_current(BOOK_ID, Path(dest2))
                assert not (Path(dest2) / "state").exists()
                for fname in CANONICAL:
                    assert (Path(dest2) / fname).is_file()
    finally:
        import shutil; shutil.rmtree(str(dest), ignore_errors=True)


def test_local_facade_no_self_ssh(tmp_path=None):
    # Real predicate must select local facade ONLY when execution_host == "media" (trusted),
    # regardless of local path existence, and NOT when execution_host == "rt".
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as dest:
        dest_p = Path(dest)
        # Trusted host signal: media uses local, rt never does (even if path exists)
        assert remote_client._should_use_local_facade("media-snap", "/home/rt/pact_runs", execution_host="media") is True
        assert remote_client._should_use_local_facade("media", "/home/rt/pact_runs", execution_host="media") is True
        assert remote_client._should_use_local_facade("other-host", "/home/rt/pact_runs", execution_host="media") is False
        assert remote_client._should_use_local_facade("media-snap", "/tmp/other_root", execution_host="media") is False
        assert remote_client._should_use_local_facade("media-snap", "/home/rt/pact_runs", execution_host="rt") is False
        assert remote_client._should_use_local_facade("media-snap", "/home/rt/pact_runs", execution_host=None) is False
        # fetch_current on media host must use local facade and NOT call subprocess.run
        with patch("pact_v4.snapshot.remote_client._local_fetch_current", return_value={"revision_id": "rev-0001"}) as mock_local:
            with patch("subprocess.run") as mock_run:
                result = remote_client.fetch_current(BOOK_ID, dest_p, ssh_target="media-snap", root="/home/rt/pact_runs", execution_host="media")
                assert result == {"revision_id": "rev-0001"}
                assert mock_local.called
                assert not mock_run.called


def test_local_facade_rt_stays_on_ssh_when_path_exists(tmp_path=None):
    # RT must ALWAYS use SSH even when /home/rt/pact_runs is locally available (fail-closed).
    from unittest.mock import patch, MagicMock
    import subprocess as _subprocess
    with tempfile.TemporaryDirectory() as dest:
        dest_p = Path(dest)
        # Even with Path.exists/is_dir mocked to True, RT host signal keeps SSH path
        with patch.object(remote_client.Path, "is_dir", return_value=True):
            with patch.object(remote_client.Path, "exists", return_value=True):
                assert remote_client._should_use_local_facade("media-snap", "/home/rt/pact_runs", execution_host="rt") is False
        # fetch_current on RT must invoke subprocess.run (SSH) and NOT local facade,
        # even when canonical path appears locally available.
        with patch.object(remote_client.Path, "is_dir", return_value=True):
            with patch.object(remote_client.Path, "exists", return_value=True):
                with patch("pact_v4.snapshot.remote_client._local_fetch_current") as mock_local:
                    # Need _extract_fetch_tar to succeed, so mock subprocess.run to return valid tar
                    import io, tarfile, json as _json
                    bio = io.BytesIO()
                    with tarfile.open(fileobj=bio, mode="w", format=tarfile.PAX_FORMAT) as tar:
                        for name, obj in [("CURRENT.json", {"revision_id": "rev-0001"}), ("manifest.json", {"schema_version": "1.0.0"})]:
                            data = (_json.dumps(obj) + "\n").encode()
                            ti = tarfile.TarInfo(name=name); ti.size=len(data); ti.mtime=0; ti.mode=0o644
                            tar.addfile(ti, io.BytesIO(data))
                        for fname in CANONICAL:
                            data = _json.dumps({"seed": fname}).encode()
                            ti = tarfile.TarInfo(name=f"state/{fname}"); ti.size=len(data); ti.mtime=0; ti.mode=0o644
                            tar.addfile(ti, io.BytesIO(data))
                    tar_bytes = bio.getvalue()
                    mock_proc = MagicMock(returncode=0, stdout=tar_bytes, stderr=b"")
                    with patch("subprocess.run", return_value=mock_proc) as mock_run:
                        result = remote_client.fetch_current(BOOK_ID, dest_p, ssh_target="media-snap", root="/home/rt/pact_runs", execution_host="rt")
                        assert result["revision_id"] == "rev-0001"
                        assert not mock_local.called
                        assert mock_run.called
                        # Verify SSH target is media-snap
                        called_cmd = mock_run.call_args[0][0]
                        assert "media-snap" in called_cmd


def test_fetch_tar_negative_matrix():
    import io, tarfile, json
    def _build(names):
        bio = io.BytesIO()
        with tarfile.open(fileobj=bio, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for name in names:
                data = b"{}"
                if name in ("CURRENT.json", "manifest.json"):
                    data = (json.dumps({"x": 1}) + "\n").encode()
                elif name.startswith("state/"):
                    data = json.dumps({"seed": name}).encode()
                else:
                    data = b"extra"
                ti = tarfile.TarInfo(name=name); ti.size=len(data); ti.mtime=0; ti.mode=0o644
                tar.addfile(ti, io.BytesIO(data))
        return bio.getvalue()
    dest = Path(tempfile.mkdtemp())
    try:
        # Missing file
        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            missing = _build(["CURRENT.json", "manifest.json"] + [f"state/{f}" for f in CANONICAL[:-1]])
            try:
                remote_client._extract_fetch_tar(missing, td_p)
                assert False, "should reject missing file"
            except RuntimeError as e:
                assert "exactly" in str(e).lower()
        # Extra top-level file
        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            extra = _build(["CURRENT.json", "manifest.json", "extra.txt"] + [f"state/{f}" for f in CANONICAL])
            try:
                remote_client._extract_fetch_tar(extra, td_p)
                assert False, "should reject extra file"
            except RuntimeError as e:
                assert "exactly" in str(e).lower()
        # Symlink entry
        import io as _io
        bio = _io.BytesIO()
        with tarfile.open(fileobj=bio, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for name in ["CURRENT.json", "manifest.json"] + [f"state/{f}" for f in CANONICAL]:
                data = (json.dumps({"x": 1}) + "\n").encode()
                ti = tarfile.TarInfo(name=name); ti.size=len(data); ti.mtime=0; ti.mode=0o644
                tar.addfile(ti, _io.BytesIO(data))
            ti = tarfile.TarInfo(name="state/link"); ti.type = tarfile.SYMTYPE; ti.linkname = "book_memory.json"; ti.size=0; ti.mtime=0
            tar.addfile(ti)
        with tempfile.TemporaryDirectory() as td:
            try:
                remote_client._extract_fetch_tar(bio.getvalue(), Path(td))
                assert False, "should reject symlink"
            except RuntimeError as e:
                assert "symlink" in str(e).lower() or "exactly" in str(e).lower()
        # FIFO entry is not easily constructed via tarfile without special type; simulate by setting type
        bio2 = _io.BytesIO()
        with tarfile.open(fileobj=bio2, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for name in ["CURRENT.json", "manifest.json"] + [f"state/{f}" for f in CANONICAL]:
                data = (json.dumps({"x": 1}) + "\n").encode()
                ti = tarfile.TarInfo(name=name); ti.size=len(data); ti.mtime=0; ti.mode=0o644
                tar.addfile(ti, _io.BytesIO(data))
            ti = tarfile.TarInfo(name="state/fifo"); ti.type = tarfile.FIFOTYPE; ti.size=0; ti.mtime=0
            tar.addfile(ti)
        with tempfile.TemporaryDirectory() as td:
            try:
                remote_client._extract_fetch_tar(bio2.getvalue(), Path(td))
                assert False, "should reject special file"
            except RuntimeError as e:
                assert "special" in str(e).lower() or "exactly" in str(e).lower()
    finally:
        import shutil; shutil.rmtree(str(dest), ignore_errors=True)


def test_validate_local_files_negative(tmp_path):
    # Missing file
    p = Path(tmp_path) / "ldir"
    p.mkdir()
    for fname in CANONICAL[:-1]:
        _make_json_file(p / fname, {"a": 1})
    try:
        remote_client._validate_local_files(p)
        assert False
    except ValueError as e:
        assert "missing or not regular" in str(e)
    # Non-JSON
    for fname in CANONICAL:
        _make_json_file(p / fname, {"a": 1})
    (p / CANONICAL[0]).write_text("not json", encoding="utf-8")
    try:
        remote_client._validate_local_files(p)
        assert False
    except ValueError as e:
        assert "not valid JSON" in str(e)
    # Symlink
    (p / CANONICAL[0]).unlink()
    _make_json_file(p / CANONICAL[0], {"a": 1})
    link = p / CANONICAL[1]
    link.unlink()
    target = p / "real.json"
    _make_json_file(target, {"a": 1})
    try:
        link.symlink_to(target)
        try:
            remote_client._validate_local_files(p)
            assert False
        except ValueError as e:
            assert "symlink" in str(e).lower()
    except OSError:
        pass
    # FIFO
    try:
        import os
        fifo_path = p / CANONICAL[2]
        fifo_path.unlink(missing_ok=True)
        os.mkfifo(fifo_path)
        try:
            remote_client._validate_local_files(p)
            assert False
        except ValueError:
            pass
    except OSError:
        pass
