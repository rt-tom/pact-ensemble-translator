"""End-to-end boundary negative matrix via fake transport."""

import io
import json
import os
import tarfile
import tempfile
from pathlib import Path

import pytest

from pact_v4.snapshot.store import BookStore
from pact_v4.snapshot.bootstrap import bootstrap
from pact_v4.snapshot.cli import _receive_candidate_stream
from pact_v4.snapshot.promote import promote
from pact_v4.snapshot.manifest import compute_sha256_and_size

BOOK_ID = "boundary-book"
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


def _build_tar_bytes(manifest_dict, local_dir: Path, extra_entries=None, mutate=None):
    """Build tar with manifest + state files, optionally adding extra/symlink/special."""
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w", format=tarfile.PAX_FORMAT) as tar:
        m_bytes = json.dumps(manifest_dict, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        ti = tarfile.TarInfo(name="manifest.json"); ti.size = len(m_bytes); ti.mtime = 0; ti.mode = 0o644
        tar.addfile(ti, io.BytesIO(m_bytes))
        for fname in CANONICAL:
            data = (local_dir / fname).read_bytes()
            ti2 = tarfile.TarInfo(name=f"state/{fname}"); ti2.size = len(data); ti2.mtime = 0; ti2.mode = 0o644
            tar.addfile(ti2, io.BytesIO(data))
        if extra_entries:
            for name, data in extra_entries:
                ti3 = tarfile.TarInfo(name=name); ti3.size = len(data); ti3.mtime = 0; ti3.mode = 0o644
                tar.addfile(ti3, io.BytesIO(data))
        if mutate:
            # mutate is callback to add symlink/fifo after tar? Handled via direct file manipulation after receive?
            pass
    return bio.getvalue()


def _build_valid_manifest(local_dir: Path, parent_rev: str):
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    state_files = []
    for fname in CANONICAL:
        h, sz = compute_sha256_and_size(local_dir / fname)
        state_files.append({"rel_path": f"state/{fname}", "sha256": h, "size": sz})
    return {
        "schema_version": "1.0.0",
        "book_id": BOOK_ID,
        "revision_id": "rev-0000",
        "parent_revision_id": parent_rev,
        "created_at": now, "published_at": now,
        "terminal_status": "complete",
        "tool_version": "pact-snapshot/0.1.0",
        "source": {"path_on_rt": str(local_dir), "operator": "rt", "host": "RT"},
        "state_files": state_files, "excludes": [], "code_commit": "unknown",
    }


def test_extra_top_level_file_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        store = _seed_store(tmp)
        with tempfile.TemporaryDirectory() as ldir:
            ldir_p = Path(ldir)
            for fname in CANONICAL:
                _make_json_file(ldir_p / fname, {"v": 1})
            manifest = _build_valid_manifest(ldir_p, "rev-0001")
            tar_bytes = _build_tar_bytes(manifest, ldir_p, extra_entries=[("credentials.env", b"SECRET=1")])
            with pytest.raises(Exception):
                _receive_candidate_stream(store, "cand-extra-top", tar_bytes)
            # Alternatively if received, promote should quarantine
            # Try direct receive that validates allow-list: it should reject
            assert not (store.incoming_dir / "cand-extra-top").exists()


def test_symlink_in_state_rejected_via_promote():
    with tempfile.TemporaryDirectory() as tmp:
        store = _seed_store(tmp)
        # Build valid tar then after receive, replace file with symlink before promote
        with tempfile.TemporaryDirectory() as ldir:
            ldir_p = Path(ldir)
            for fname in CANONICAL:
                _make_json_file(ldir_p / fname, {"v": 1})
            manifest = _build_valid_manifest(ldir_p, "rev-0001")
            tar_bytes = _build_tar_bytes(manifest, ldir_p)
            rc = _receive_candidate_stream(store, "cand-sym", tar_bytes)
            assert rc == 0
            cand_dir = store.incoming_candidate_path("cand-sym")
            # Replace one file with symlink
            target = cand_dir / "state" / "book_memory.json"
            target.unlink()
            # Create external file
            with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json") as ext:
                json.dump({"evil": True}, ext)
                ext_path = ext.name
            os.symlink(ext_path, str(target))
            # Need to also update manifest to match external file hash so it would pass hash check but symlink check triggers first
            # Recompute manifest hash to match external (so promotion proceeds to symlink check)
            import hashlib, json as _json
            h, sz = compute_sha256_and_size(Path(ext_path))
            # Update manifest entry
            m_path = cand_dir / "manifest.json"
            data = _json.loads(m_path.read_text(encoding="utf-8"))
            for e in data["state_files"]:
                if e["rel_path"] == "state/book_memory.json":
                    e["sha256"] = h; e["size"] = sz
            m_path.write_text(_json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            with pytest.raises(Exception):
                promote(store, "cand-sym")
            assert store.quarantine_candidate_path("cand-sym").exists()
            os.unlink(ext_path)


def test_fifo_in_state_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        store = _seed_store(tmp)
        with tempfile.TemporaryDirectory() as ldir:
            ldir_p = Path(ldir)
            for fname in CANONICAL:
                _make_json_file(ldir_p / fname, {"v": 1})
            manifest = _build_valid_manifest(ldir_p, "rev-0001")
            tar_bytes = _build_tar_bytes(manifest, ldir_p)
            rc = _receive_candidate_stream(store, "cand-fifo", tar_bytes)
            assert rc == 0
            cand_dir = store.incoming_candidate_path("cand-fifo")
            fifo = cand_dir / "state" / "smuggled-fifo"
            os.mkfifo(str(fifo))
            # Also need to make manifest include it? promote will detect extra file in state regardless of manifest
            with pytest.raises(Exception):
                promote(store, "cand-fifo")
            assert store.quarantine_candidate_path("cand-fifo").exists()


def test_post_lock_mutation_toctou():
    """Mutate candidate state/ into file during compute_next_revision_id -> rejected."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _seed_store(tmp)
        with tempfile.TemporaryDirectory() as ldir:
            ldir_p = Path(ldir)
            for fname in CANONICAL:
                _make_json_file(ldir_p / fname, {"v": 1})
            manifest = _build_valid_manifest(ldir_p, "rev-0001")
            tar_bytes = _build_tar_bytes(manifest, ldir_p)
            _receive_candidate_stream(store, "cand-toctou", tar_bytes)
            cand_dir = store.incoming_candidate_path("cand-toctou")
            orig = BookStore.compute_next_revision_id

            def mutating(self):
                import shutil
                state_path = cand_dir / "state"
                if state_path.is_dir() and not state_path.is_symlink():
                    shutil.rmtree(state_path)
                    state_path.write_text("replaced", encoding="utf-8")
                return orig(self)

            BookStore.compute_next_revision_id = mutating
            try:
                with pytest.raises(Exception):
                    promote(store, "cand-toctou")
            finally:
                BookStore.compute_next_revision_id = orig
            assert store.quarantine_candidate_path("cand-toctou").exists()
            assert not store.snapshot_dir("rev-0002").exists()
