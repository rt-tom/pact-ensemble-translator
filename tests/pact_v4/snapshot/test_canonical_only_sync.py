"""Canonical-only local sync boundary matrix (book-state-canonical-only-sync).

Local promotion / fetch / push ignore arbitrary unrelated top-level objects,
preserve them byte-for-byte, and operate only on the four explicitly named
canonical JSON files plus the directly used CURRENT / manifest / transaction
marker / backup paths. The strict exact-four boundary is retained for the
private transaction staging bundle and for the Media candidate / snapshot.
"""
import io
import json
import os
import socket
import stat
import tarfile
import tempfile
from pathlib import Path

import pytest

from pact_v4.phase1.memory import (
    CANONICAL_FILES as MEM_CANONICAL,
    MemoryManager,
    _validate_canonical_selected_paths,
)
from pact_v4.snapshot import remote_client
from pact_v4.snapshot.remote_client import (
    _build_candidate_tar_bytes_from_pinned,
    _pin_canonical_bytes,
    _pinned_state_files,
)


def _make_four(base: Path, payload=None):
    for fname in MEM_CANONICAL:
        (base / fname).write_text(json.dumps(payload if payload is not None else {}) + "\n", encoding="utf-8")


def _snapshot_unrelated(paths):
    """Capture bytes/type of unrelated objects for byte-for-byte comparison."""
    snap = {}
    for p in paths:
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            snap[str(p)] = ("missing", None)
            continue
        if stat.S_ISLNK(st.st_mode):
            snap[str(p)] = ("symlink", os.readlink(p))
        elif stat.S_ISDIR(st.st_mode):
            snap[str(p)] = ("dir", sorted(os.listdir(p)))
        elif stat.S_ISREG(st.st_mode):
            with open(p, "rb") as f:
                snap[str(p)] = ("file", f.read())
        elif stat.S_ISFIFO(st.st_mode):
            snap[str(p)] = ("fifo", None)
        elif stat.S_ISSOCK(st.st_mode):
            snap[str(p)] = ("socket", None)
        else:
            snap[str(p)] = ("other", None)
    return snap


def _plant_unrelated(base: Path):
    """Plant extra top-level file, dir, symlink, FIFO, socket, chapter_memory.json."""
    (base / "extra.json").write_text(json.dumps({"x": 1}), encoding="utf-8")
    (base / "logs").mkdir()
    (base / "logs" / "run.log").write_text("logbody", encoding="utf-8")
    (base / "chapter_memory.json").write_text(json.dumps({"legacy": True}), encoding="utf-8")
    target = base / "extra.json"
    (base / "notes_link").symlink_to(target)
    os.mkfifo(base / "data.fifo")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(base / "data.sock"))
    return sock, [
        base / "extra.json",
        base / "logs",
        base / "chapter_memory.json",
        base / "notes_link",
        base / "data.fifo",
        base / "data.sock",
    ]


# ---------------------------------------------------------------------------
# 3.1 positive matrix
# ---------------------------------------------------------------------------

def test_promote_with_unrelated_objects_succeeds_and_preserves():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        _make_four(base)
        sock, unrelated = _plant_unrelated(base)
        try:
            before = _snapshot_unrelated(unrelated)
            mgr = MemoryManager(str(base))
            mgr.add_observation("glossary", "K", {"target": "Ц"})
            mgr.promote("complete")
            assert _snapshot_unrelated(unrelated) == before
            assert (base / "extra.json").read_text(encoding="utf-8") == json.dumps({"x": 1})
            assert (base / "chapter_memory.json").read_text(encoding="utf-8") == json.dumps({"legacy": True})
            assert not (base / ".pact_transaction_marker.json").exists()
            glossary = json.loads((base / "glossary.json").read_text(encoding="utf-8"))
            assert glossary.get("K", {}).get("target") == "Ц"
        finally:
            sock.close()


def test_push_candidate_members_exact_four_and_unrelated_untouched():
    from tests.pact_v4.snapshot.test_remote_client import FakeTransport, _seed_store, BOOK_ID
    with tempfile.TemporaryDirectory() as tmp:
        store = _seed_store(tmp)
        transport = FakeTransport(tmp)
        with tempfile.TemporaryDirectory() as local:
            wdir = Path(local)
            _make_four(wdir, {"v": 7})
            sock, unrelated = _plant_unrelated(wdir)
            try:
                before = _snapshot_unrelated(unrelated)
                cur = store.read_current()
                (wdir / "CURRENT.json").write_text(json.dumps(cur), encoding="utf-8")
                pinned = _pin_canonical_bytes(wdir)
                tar_bytes = _build_candidate_tar_bytes_from_pinned(pinned, {
                    "schema_version": "1.0.0",
                    "state_files": _pinned_state_files(pinned),
                })
                with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as tar:
                    names = sorted(m.name for m in tar.getmembers() if not m.isdir())
                assert names == sorted(["manifest.json"] + [f"state/{f}" for f in MEM_CANONICAL])
                # manifest hash/size match the real tar member bytes
                with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as tar:
                    mfile = tar.extractfile("manifest.json")
                    assert mfile is not None
                    manifest = json.loads(mfile.read().decode("utf-8"))
                with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as tar:
                    for entry in manifest["state_files"]:
                        member = tar.extractfile(entry["rel_path"])
                        assert member is not None
                        data = member.read()
                        import hashlib
                        assert hashlib.sha256(data).hexdigest() == entry["sha256"]
                        assert len(data) == entry["size"]
                        assert data == pinned[entry["rel_path"].split("/")[-1]]
                assert _snapshot_unrelated(unrelated) == before
                verdict = remote_client.push_candidate(BOOK_ID, "cand-canonical-1", wdir, transport=transport)
                assert verdict["status"] == "ACCEPTED"
                assert _snapshot_unrelated(unrelated) == before
            finally:
                sock.close()


def test_fetch_into_nonempty_dir_updates_only_selected():
    from tests.pact_v4.snapshot.test_remote_client import FakeTransport, _seed_store, BOOK_ID
    with tempfile.TemporaryDirectory() as tmp:
        store = _seed_store(tmp)
        transport = FakeTransport(tmp, book_id=BOOK_ID)
        with tempfile.TemporaryDirectory() as local:
            wdir = Path(local)
            _make_four(wdir, {"stale": True})
            sock, unrelated = _plant_unrelated(wdir)
            try:
                before = _snapshot_unrelated(unrelated)
                cur = remote_client.fetch_current(BOOK_ID, wdir, transport=transport)
                assert cur["revision_id"] == "rev-0001"
                for fname in MEM_CANONICAL:
                    assert json.loads((wdir / fname).read_text(encoding="utf-8"))["seed"] == fname
                assert _snapshot_unrelated(unrelated) == before
            finally:
                sock.close()


# ---------------------------------------------------------------------------
# 3.2 negative matrix for the selected inputs
# ---------------------------------------------------------------------------

def test_root_must_be_directory():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        _make_four(base)
        not_a_dir = base / "glossary.json"
        assert _validate_canonical_selected_paths(str(not_a_dir)) is not None
        # Fail-closed: a file as working root is rejected (validator error on
        # promote, or makedirs refusal at construction) — never treated as state.
        try:
            mgr = MemoryManager(str(not_a_dir))
        except (FileExistsError, NotADirectoryError, OSError):
            return
        with pytest.raises(RuntimeError, match="canonical-only boundary violation"):
            mgr.promote("complete")


def test_symlink_at_each_ancestor_level_rejected():
    with tempfile.TemporaryDirectory() as td:
        real = Path(td) / "real"
        real.mkdir()
        _make_four(real)
        # base_dir itself reached through a symlink
        link = Path(td) / "linkbase"
        link.symlink_to(real, target_is_directory=True)
        assert _validate_canonical_selected_paths(str(link)) is not None
        # parent of base reached through a symlink
        outer = Path(td) / "outer"
        outer.mkdir()
        inner = outer / "inner"
        inner.mkdir()
        _make_four(inner)
        outer_link = Path(td) / "outerlink"
        outer_link.symlink_to(outer, target_is_directory=True)
        assert _validate_canonical_selected_paths(str(outer_link / "inner")) is not None
        # real path itself still passes
        assert _validate_canonical_selected_paths(str(real)) is None


@pytest.mark.parametrize("fname", list(MEM_CANONICAL))
def test_canonical_name_symlink_dir_fifo_socket_invalid_rejected(fname):
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        _make_four(base)
        victim = base / fname
        victim.unlink()
        (base / "real_target.json").write_text("{}", encoding="utf-8")
        victim.symlink_to(base / "real_target.json")
        assert _validate_canonical_selected_paths(str(base)) is not None
        victim.unlink()
        victim.mkdir()
        assert _validate_canonical_selected_paths(str(base)) is not None
        victim.rmdir()
        os.mkfifo(victim)
        assert _validate_canonical_selected_paths(str(base)) is not None
        victim.unlink()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.bind(str(victim))
            assert _validate_canonical_selected_paths(str(base)) is not None
        finally:
            s.close()
            victim.unlink()
        victim.write_text("{not valid json", encoding="utf-8")
        assert _validate_canonical_selected_paths(str(base)) is not None


@pytest.mark.parametrize("fname", list(MEM_CANONICAL))
def test_missing_canonical_rejected(fname):
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        _make_four(base)
        (base / fname).unlink()
        assert _validate_canonical_selected_paths(str(base)) is not None
        with pytest.raises(RuntimeError, match="canonical-only boundary violation"):
            MemoryManager(str(base)).promote("complete")


def test_fetch_refuses_symlink_destination():
    tar_bytes = _build_fetch_tar()
    with tempfile.TemporaryDirectory() as td:
        dest = Path(td)
        _make_four(dest)
        victim = dest / "glossary.json"
        victim.unlink()
        (dest / "real.json").write_text("{}", encoding="utf-8")
        victim.symlink_to(dest / "real.json")
        with pytest.raises(RuntimeError, match="[Ss]ymlink"):
            remote_client._extract_fetch_tar(tar_bytes, dest)
        # the link target must not have been overwritten through the link
        assert (dest / "real.json").read_text(encoding="utf-8") == "{}"


def _build_fetch_tar(extra_member=None):
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, obj in [("CURRENT.json", {"revision_id": "rev-0001"}), ("manifest.json", {"schema_version": "1.0.0"})]:
            data = (json.dumps(obj) + "\n").encode()
            ti = tarfile.TarInfo(name=name)
            ti.size = len(data)
            ti.mtime = 0
            ti.mode = 0o644
            tar.addfile(ti, io.BytesIO(data))
        for fname in MEM_CANONICAL:
            data = json.dumps({"seed": fname}).encode()
            ti = tarfile.TarInfo(name=f"state/{fname}")
            ti.size = len(data)
            ti.mtime = 0
            ti.mode = 0o644
            tar.addfile(ti, io.BytesIO(data))
        if extra_member:
            data = b"{}"
            ti = tarfile.TarInfo(name=extra_member)
            ti.size = len(data)
            ti.mtime = 0
            ti.mode = 0o644
            tar.addfile(ti, io.BytesIO(data))
    return bio.getvalue()


def test_corrupt_marker_is_fail_closed():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        _make_four(base)
        (base / ".pact_transaction_marker.json").write_text("{corrupt", encoding="utf-8")
        with pytest.raises(RuntimeError, match="marker"):
            MemoryManager(str(base))
        # symlink marker is fail-closed too
        (base / ".pact_transaction_marker.json").unlink()
        (base / "m.json").write_text("{}", encoding="utf-8")
        (base / ".pact_transaction_marker.json").symlink_to(base / "m.json")
        with pytest.raises(RuntimeError, match="marker"):
            MemoryManager(str(base))


def test_escaping_backup_path_in_marker_rejected():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        _make_four(base)
        outside = Path(td) / "outside_backup"
        outside.write_text("{}", encoding="utf-8")
        marker = {
            "pre_hashes": {},
            "post_hashes": {},
            "backups": {"glossary.json": str(outside)},
            "progress": [],
        }
        (base / ".pact_transaction_marker.json").write_text(json.dumps(marker), encoding="utf-8")
        with pytest.raises(RuntimeError, match="[Ee]scap|marker"):
            MemoryManager(str(base))


def test_premoval_mutation_detected_by_identical_revalidation():
    """TOCTOU: mutate a selected file after the pre-transaction check; the
    identical pre-move revalidation must fail the transaction."""
    import pact_v4.phase1.memory as memmod
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        _make_four(base)
        real_validate = memmod._validate_canonical_selected_paths
        calls = {"n": 0}

        def _flaky(base_dir):
            calls["n"] += 1
            # call 1 = promote precheck, call 2 = pre-transaction check,
            # call 3 = pre-move recheck: corrupt just before the last one.
            if calls["n"] == 3:
                (Path(base_dir) / "observations.json").write_text("{broken", encoding="utf-8")
            return real_validate(base_dir)

        memmod._validate_canonical_selected_paths = _flaky
        try:
            mgr = MemoryManager(str(base))
            with pytest.raises(RuntimeError, match="pre-move revalidation failed"):
                mgr.promote("complete")
        finally:
            memmod._validate_canonical_selected_paths = real_validate


def test_pinned_bytes_survive_post_pin_mutation():
    """TOCTOU: bytes mutated after pinning must not enter the tar without a
    matching manifest — the tar carries exactly the pinned bytes."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        _make_four(base, {"v": 1})
        pinned = _pin_canonical_bytes(base)
        (base / "glossary.json").write_text(json.dumps({"v": 2}), encoding="utf-8")
        manifest = {"schema_version": "1.0.0", "state_files": _pinned_state_files(pinned)}
        tar_bytes = _build_candidate_tar_bytes_from_pinned(pinned, manifest)
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as tar:
            member = tar.extractfile("state/glossary.json")
            assert member is not None
            data = member.read()
        assert json.loads(data.decode("utf-8")) == {"v": 1}
        import hashlib
        entry = [e for e in manifest["state_files"] if e["rel_path"] == "state/glossary.json"][0]
        assert entry["sha256"] == hashlib.sha256(data).hexdigest()
        assert entry["size"] == len(data)


# ---------------------------------------------------------------------------
# 3.3 Media boundary + no-push-on-failure + STALE_PARENT
# ---------------------------------------------------------------------------

def test_media_rejects_extra_candidate_member():
    from pact_v4.snapshot.store import BookStore
    from pact_v4.snapshot.cli import _receive_candidate_stream
    from pact_v4.snapshot.errors import ValidationError
    with tempfile.TemporaryDirectory() as tmp:
        store = BookStore("media-book", root=tmp)
        store.init_store()
        # Build a candidate tar with a fifth state member (rogue.json).
        bio = io.BytesIO()
        with tarfile.open(fileobj=bio, mode="w", format=tarfile.PAX_FORMAT) as tar:
            m = json.dumps({"schema_version": "1.0.0"}).encode()
            ti = tarfile.TarInfo(name="manifest.json")
            ti.size = len(m)
            ti.mtime = 0
            ti.mode = 0o644
            tar.addfile(ti, io.BytesIO(m))
            for fname in list(MEM_CANONICAL) + ["rogue.json"]:
                data = json.dumps({}).encode()
                ti = tarfile.TarInfo(name=f"state/{fname}")
                ti.size = len(data)
                ti.mtime = 0
                ti.mode = 0o644
                tar.addfile(ti, io.BytesIO(data))
        with pytest.raises(Exception, match="(?i)(allow|extra|unexpected|rogue|reject|invalid)"):
            _receive_candidate_stream(store, "cand-rogue", bio.getvalue())


def test_no_push_after_failed_local_promote(tmp_path):
    from unittest import mock
    wdir = tmp_path / "w"
    wdir.mkdir()
    # missing canonical -> push must fail before touching transport
    for fname in list(MEM_CANONICAL)[:-1]:
        (wdir / fname).write_text("{}", encoding="utf-8")
    transport = mock.MagicMock()
    with pytest.raises((ValueError, RuntimeError)):
        remote_client.push_candidate("b", "cand-x", wdir, transport=transport)
    transport.push_candidate.assert_not_called()


def test_stale_parent_retry_preserves_unrelated_objects():
    from tests.pact_v4.snapshot.test_remote_client import FakeTransport, _seed_store, BOOK_ID
    from pact_v4.snapshot.run_hooks import pre_init_fetch, post_promote_push
    with tempfile.TemporaryDirectory() as tmp:
        store = _seed_store(tmp)

        class StaleOnceTransport(FakeTransport):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.push_calls = 0

            def push_candidate(self, book_id, candidate_id, local_dir, manifest_dict=None, pinned=None):
                self.push_calls += 1
                if self.push_calls == 1:
                    return {"status": "REJECTED", "reason": "STALE_PARENT", "message": "stale", "candidate_id": candidate_id}
                return super().push_candidate(book_id, candidate_id, local_dir, manifest_dict, pinned=pinned)

        transport = StaleOnceTransport(tmp, book_id=BOOK_ID)
        with tempfile.TemporaryDirectory() as local:
            wdir = Path(local)
            pre_init_fetch(BOOK_ID, wdir, transport=transport)
            sock, unrelated = _plant_unrelated(wdir)
            try:
                before = _snapshot_unrelated(unrelated)
                for fname in MEM_CANONICAL:
                    (wdir / fname).write_text(json.dumps({"updated": True}), encoding="utf-8")
                verdict = post_promote_push(BOOK_ID, wdir, transport=transport, max_retries=1)
                assert verdict["status"] == "ACCEPTED"
                assert transport.push_calls == 2
                assert _snapshot_unrelated(unrelated) == before
            finally:
                sock.close()


# ---------------------------------------------------------------------------
# D5 regression: mutation after pin must not enter published bytes
# ---------------------------------------------------------------------------

def test_injected_transport_publishes_pinned_bytes_despite_post_pin_mutation():
    """D5: poison the live dir inside the injected transport (i.e. after the
    client pinned bytes and built the manifest); the published snapshot and
    manifest must still match the pinned pre-poison bytes."""
    import hashlib
    from tests.pact_v4.snapshot.test_remote_client import FakeTransport, _seed_store, BOOK_ID
    with tempfile.TemporaryDirectory() as tmp:
        store = _seed_store(tmp)

        class MutatingTransport(FakeTransport):
            def push_candidate(self, book_id, candidate_id, local_dir, manifest_dict=None, pinned=None):
                assert pinned is not None, "client must pass pinned bytes (D5)"
                # Simulate a mutation between pin/hash and publication.
                for fname in MEM_CANONICAL:
                    (Path(local_dir) / fname).write_text(json.dumps({"poison": True}), encoding="utf-8")
                return super().push_candidate(book_id, candidate_id, local_dir, manifest_dict, pinned=pinned)

        transport = MutatingTransport(tmp, book_id=BOOK_ID)
        with tempfile.TemporaryDirectory() as local:
            wdir = Path(local)
            for fname in MEM_CANONICAL:
                (wdir / fname).write_text(json.dumps({"v": 1, "fname": fname}), encoding="utf-8")
            cur = store.read_current()
            (wdir / "CURRENT.json").write_text(json.dumps(cur), encoding="utf-8")
            expected = {fname: (wdir / fname).read_bytes() for fname in MEM_CANONICAL}
            verdict = remote_client.push_candidate(BOOK_ID, "cand-pin-injected", wdir, transport=transport)
            assert verdict["status"] == "ACCEPTED", verdict
            snap_dir = store.snapshot_dir(verdict["revision_id"])
            manifest = json.loads((snap_dir / "manifest.json").read_text(encoding="utf-8"))
            by_rel = {e["rel_path"]: e for e in manifest["state_files"]}
            for fname in MEM_CANONICAL:
                published = (snap_dir / "state" / fname).read_bytes()
                assert published == expected[fname], f"published {fname} must equal pinned pre-poison bytes"
                entry = by_rel[f"state/{fname}"]
                assert entry["sha256"] == hashlib.sha256(published).hexdigest()
                assert entry["size"] == len(published)
                assert b"poison" not in published


def test_local_facade_publishes_pinned_bytes_despite_post_pin_mutation():
    """D5: poison the live dir immediately after the client's pin; the local
    facade must still publish the original pinned bytes (it must not re-pin
    the live directory)."""
    import hashlib
    from unittest.mock import patch
    from tests.pact_v4.snapshot.test_remote_client import _seed_store, BOOK_ID
    with tempfile.TemporaryDirectory() as tmp:
        store = _seed_store(tmp)
        with tempfile.TemporaryDirectory() as local:
            wdir = Path(local)
            for fname in MEM_CANONICAL:
                (wdir / fname).write_text(json.dumps({"v": 1, "fname": fname}), encoding="utf-8")
            cur = store.read_current()
            (wdir / "CURRENT.json").write_text(json.dumps(cur), encoding="utf-8")
            expected = {fname: (wdir / fname).read_bytes() for fname in MEM_CANONICAL}
            real_pin = remote_client._pin_canonical_bytes
            calls = {"n": 0}

            def _poison_after_pin(local_dir):
                pinned = real_pin(Path(local_dir))
                calls["n"] += 1
                if calls["n"] == 1:
                    for fname in MEM_CANONICAL:
                        (Path(local_dir) / fname).write_text(json.dumps({"poison": True}), encoding="utf-8")
                return pinned

            with patch.object(remote_client, "_pin_canonical_bytes", side_effect=_poison_after_pin):
                with patch.object(remote_client, "_should_use_local_facade", return_value=True):
                    verdict = remote_client.push_candidate(
                        BOOK_ID, "cand-pin-local", wdir, ssh_target="media",
                        root=tmp, execution_host="media",
                    )
            assert verdict["status"] == "ACCEPTED", verdict
            snap_dir = store.snapshot_dir(verdict["revision_id"])
            manifest = json.loads((snap_dir / "manifest.json").read_text(encoding="utf-8"))
            by_rel = {e["rel_path"]: e for e in manifest["state_files"]}
            for fname in MEM_CANONICAL:
                published = (snap_dir / "state" / fname).read_bytes()
                assert published == expected[fname], f"local facade published {fname} must equal pinned pre-poison bytes"
                entry = by_rel[f"state/{fname}"]
                assert entry["sha256"] == hashlib.sha256(published).hexdigest()
                assert entry["size"] == len(published)
                assert b"poison" not in published


# ---------------------------------------------------------------------------
# Fetch-tar hardening: extra directory + duplicate members
# ---------------------------------------------------------------------------

def _build_fetch_tar_with_dir(dirname="extra_dir"):
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, obj in [("CURRENT.json", {"revision_id": "rev-0001"}), ("manifest.json", {"schema_version": "1.0.0"})]:
            data = (json.dumps(obj) + "\n").encode()
            ti = tarfile.TarInfo(name=name)
            ti.size = len(data)
            ti.mtime = 0
            ti.mode = 0o644
            tar.addfile(ti, io.BytesIO(data))
        for fname in MEM_CANONICAL:
            data = json.dumps({"seed": fname}).encode()
            ti = tarfile.TarInfo(name=f"state/{fname}")
            ti.size = len(data)
            ti.mtime = 0
            ti.mode = 0o644
            tar.addfile(ti, io.BytesIO(data))
        ti = tarfile.TarInfo(name=dirname)
        ti.type = tarfile.DIRTYPE
        ti.mtime = 0
        ti.mode = 0o755
        tar.addfile(ti)
    return bio.getvalue()


def _build_fetch_tar_with_duplicate():
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, obj in [("CURRENT.json", {"revision_id": "rev-0001"}), ("manifest.json", {"schema_version": "1.0.0"})]:
            data = (json.dumps(obj) + "\n").encode()
            ti = tarfile.TarInfo(name=name)
            ti.size = len(data)
            ti.mtime = 0
            ti.mode = 0o644
            tar.addfile(ti, io.BytesIO(data))
        for fname in MEM_CANONICAL:
            data = json.dumps({"seed": fname}).encode()
            ti = tarfile.TarInfo(name=f"state/{fname}")
            ti.size = len(data)
            ti.mtime = 0
            ti.mode = 0o644
            tar.addfile(ti, io.BytesIO(data))
        dup = json.dumps({"seed": "glossary.json"}).encode()
        ti = tarfile.TarInfo(name="state/glossary.json")
        ti.size = len(dup)
        ti.mtime = 0
        ti.mode = 0o644
        tar.addfile(ti, io.BytesIO(dup))
    return bio.getvalue()


def test_fetch_tar_rejects_extra_directory_and_writes_nothing():
    with tempfile.TemporaryDirectory() as td:
        dest = Path(td)
        before = {fname: (dest / fname).exists() for fname in MEM_CANONICAL}
        assert before == {fname: False for fname in MEM_CANONICAL}
        with pytest.raises(RuntimeError, match="(?i)(directory|exactly|unexpected|extra|reject)"):
            remote_client._extract_fetch_tar(_build_fetch_tar_with_dir(), dest)
        for fname in list(MEM_CANONICAL) + ["CURRENT.json", "manifest.json"]:
            assert not (dest / fname).exists(), f"{fname} must not be written on rejection"


def test_fetch_tar_rejects_duplicate_members_and_writes_nothing():
    with tempfile.TemporaryDirectory() as td:
        dest = Path(td)
        with pytest.raises(RuntimeError, match="(?i)(duplicate|exactly)"):
            remote_client._extract_fetch_tar(_build_fetch_tar_with_duplicate(), dest)
        for fname in list(MEM_CANONICAL) + ["CURRENT.json", "manifest.json"]:
            assert not (dest / fname).exists(), f"{fname} must not be written on rejection"


def test_fetch_tar_valid_canonical_members_still_accepted():
    with tempfile.TemporaryDirectory() as td:
        dest = Path(td)
        cur = remote_client._extract_fetch_tar(_build_fetch_tar(), dest)
        assert cur["revision_id"] == "rev-0001"
        for fname in MEM_CANONICAL:
            assert json.loads((dest / fname).read_text(encoding="utf-8"))["seed"] == fname


# ---------------------------------------------------------------------------
# D5 SSH regression: mutation after pin must not enter the SSH payload
# ---------------------------------------------------------------------------

def test_ssh_push_publishes_pinned_bytes_despite_post_pin_mutation():
    """D5: poison the live dir right after the client's pin; the real SSH
    branch's receive-candidate `input` tar must still carry exactly the
    pre-poison pinned bytes (manifest hash/size/data match, no poison)."""
    import hashlib
    import subprocess
    from unittest.mock import patch
    from tests.pact_v4.snapshot.test_remote_client import BOOK_ID
    with tempfile.TemporaryDirectory() as tmp:
        with tempfile.TemporaryDirectory() as local:
            wdir = Path(local)
            for fname in MEM_CANONICAL:
                (wdir / fname).write_text(json.dumps({"v": 1, "fname": fname}), encoding="utf-8")
            (wdir / "CURRENT.json").write_text(json.dumps({"revision_id": "rev-0001"}), encoding="utf-8")
            expected = {fname: (wdir / fname).read_bytes() for fname in MEM_CANONICAL}
            captured = {}
            real_pin = remote_client._pin_canonical_bytes

            def _poison_after_pin(local_dir):
                pinned = real_pin(Path(local_dir))
                for fname in MEM_CANONICAL:
                    (Path(local_dir) / fname).write_text(json.dumps({"poison": True}), encoding="utf-8")
                return pinned

            def _fake_run(cmd, input=None, stdout=None, stderr=None, timeout=None, env=None):
                assert cmd[0] == "ssh", f"SSH branch must shell via ssh, got {cmd[0]}"
                assert cmd[1] == "fake-ssh-target", f"must use stub target, got {cmd[1]}"
                assert "fake-ssh-target" not in ("media-snap", "media")
                if "receive-candidate" in cmd:
                    assert input, "receive-candidate must carry a tar payload"
                    captured["tar"] = bytes(input)
                    captured["recv_cmd"] = list(cmd)
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"received", stderr=b"")
                if "promote" in cmd:
                    captured["prom_cmd"] = list(cmd)
                    verdict = {"status": "ACCEPTED", "revision_id": "rev-0002", "candidate_id": cmd[-1]}
                    return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(verdict).encode(), stderr=b"")
                raise AssertionError(f"unexpected SSH command: {cmd}")

            with patch.object(remote_client, "_pin_canonical_bytes", side_effect=_poison_after_pin):
                with patch.object(remote_client.subprocess, "run", side_effect=_fake_run):
                    verdict = remote_client.push_candidate(
                        BOOK_ID, "cand-pin-ssh", wdir, ssh_target="fake-ssh-target",
                        root=tmp, execution_host="rt",
                    )
            assert verdict["status"] == "ACCEPTED", verdict
            assert "recv_cmd" in captured and "prom_cmd" in captured, "both SSH steps must run"
            assert captured["recv_cmd"][:2] == ["ssh", "fake-ssh-target"]
            assert captured["prom_cmd"][:2] == ["ssh", "fake-ssh-target"]
            tar_bytes = captured["tar"]
            with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as tar:
                names = sorted(m.name for m in tar.getmembers())
            assert names == sorted(["manifest.json"] + [f"state/{f}" for f in MEM_CANONICAL])
            with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as tar:
                manifest_raw = tar.extractfile("manifest.json")
                assert manifest_raw is not None
                manifest = json.loads(manifest_raw.read().decode("utf-8"))
            by_rel = {e["rel_path"]: e for e in manifest["state_files"]}
            with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as tar:
                for fname in MEM_CANONICAL:
                    member = tar.extractfile(f"state/{fname}")
                    assert member is not None
                    data = member.read()
                    assert data == expected[fname], f"SSH tar {fname} must equal pinned pre-poison bytes"
                    assert b"poison" not in data
                    entry = by_rel[f"state/{fname}"]
                    assert entry["sha256"] == hashlib.sha256(data).hexdigest()
                    assert entry["size"] == len(data)
