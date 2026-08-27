"""Migration Media publication via existing Media API."""
import json, tempfile, hashlib, shutil
from pathlib import Path
from pact_v4.runtime.book_memory_migration import build_migration_candidate, create_envelope, migrate_to_v2, dry_run_manifest, publish_via_media, rollback_via_media
from pact_v4.snapshot.store import BookStore

def _setup_parent(tmp: Path):
    for fname in ["glossary.json", "book_memory.json", "chapter_index.json", "observations.json"]:
        data = {}
        if fname == "glossary.json":
            data = {"Hello": "Привет"}
        elif fname == "book_memory.json":
            data = {"characters": {"Blake": {"type": "character", "chapters": ["0001"]}}, "entities": {}}
        elif fname == "chapter_index.json":
            data = {}
        elif fname == "observations.json":
            data = {"glossary": {}, "book_memory": {}}
        (tmp / fname).write_text(json.dumps(data) + "\n")

def test_publish_via_media_success(tmp_path):
    store_root = tmp_path / "store"
    store = BookStore(book_id="1", root=str(store_root))
    store.init_store()
    # parent snapshot
    parent = tmp_path / "parent"
    parent.mkdir()
    _setup_parent(parent)
    migrated = migrate_to_v2(json.loads((parent / "book_memory.json").read_text()), json.loads((parent / "glossary.json").read_text()))
    from pact_v4.runtime.book_memory_migration import build_index_from_memory as _det
    rebuilt = _det(migrated)
    cand = tmp_path / "cand"
    build_migration_candidate(parent, cand, migrated, rebuilt)
    manifest = dry_run_manifest(json.loads((parent / "book_memory.json").read_text()), json.loads((parent / "glossary.json").read_text()))
    envelope_dir = tmp_path / "env"
    envelope = create_envelope(cand, envelope_dir, parent_revision=store.read_current()["revision_id"], manifest=manifest, approval_identity="owner")
    result = publish_via_media(store, cand, envelope, operator="test", host="test")
    assert result["status"] == "ACCEPTED"
    cur = store.read_current()
    assert cur["revision_id"] == result["revision_id"]

def test_publish_fails_without_approval(tmp_path):
    store_root = tmp_path / "store"
    store = BookStore(book_id="1", root=str(store_root))
    store.init_store()
    parent = tmp_path / "parent"
    parent.mkdir()
    _setup_parent(parent)
    migrated = migrate_to_v2(json.loads((parent / "book_memory.json").read_text()), json.loads((parent / "glossary.json").read_text()))
    from pact_v4.runtime.book_memory_migration import build_index_from_memory as _det
    rebuilt = _det(migrated)
    cand = tmp_path / "cand"
    build_migration_candidate(parent, cand, migrated, rebuilt)
    manifest = dry_run_manifest(json.loads((parent / "book_memory.json").read_text()), json.loads((parent / "glossary.json").read_text()))
    envelope_dir = tmp_path / "env"
    envelope = create_envelope(cand, envelope_dir, parent_revision=store.read_current()["revision_id"], manifest=manifest, approval_identity=None)
    try:
        publish_via_media(store, cand, envelope)
        assert False, "should fail without approval"
    except RuntimeError as e:
        assert "approval" in str(e).lower()

def test_publish_fails_hash_mismatch(tmp_path):
    store_root = tmp_path / "store"
    store = BookStore(book_id="1", root=str(store_root))
    store.init_store()
    parent = tmp_path / "parent"
    parent.mkdir()
    _setup_parent(parent)
    migrated = migrate_to_v2(json.loads((parent / "book_memory.json").read_text()), json.loads((parent / "glossary.json").read_text()))
    from pact_v4.runtime.book_memory_migration import build_index_from_memory as _det
    rebuilt = _det(migrated)
    cand = tmp_path / "cand"
    build_migration_candidate(parent, cand, migrated, rebuilt)
    manifest = dry_run_manifest(json.loads((parent / "book_memory.json").read_text()), json.loads((parent / "glossary.json").read_text()))
    envelope_dir = tmp_path / "env"
    envelope = create_envelope(cand, envelope_dir, parent_revision=store.read_current()["revision_id"], manifest=manifest, approval_identity="owner")
    # tamper candidate
    (cand / "book_memory.json").write_text(json.dumps({"tampered": True}) + "\n")
    try:
        publish_via_media(store, cand, envelope)
        assert False
    except RuntimeError as e:
        assert "hash mismatch" in str(e).lower()

def test_rollback_publishes_new_revision(tmp_path):
    store_root = tmp_path / "store"
    store = BookStore(book_id="1", root=str(store_root))
    store.init_store()
    parent = tmp_path / "parent"
    parent.mkdir()
    _setup_parent(parent)
    migrated = migrate_to_v2(json.loads((parent / "book_memory.json").read_text()), json.loads((parent / "glossary.json").read_text()))
    from pact_v4.runtime.book_memory_migration import build_index_from_memory as _det
    rebuilt = _det(migrated)
    cand = tmp_path / "cand"
    build_migration_candidate(parent, cand, migrated, rebuilt)
    manifest = dry_run_manifest(json.loads((parent / "book_memory.json").read_text()), json.loads((parent / "glossary.json").read_text()))
    envelope_dir = tmp_path / "env"
    envelope = create_envelope(cand, envelope_dir, parent_revision=store.read_current()["revision_id"], manifest=manifest, approval_identity="owner")
    publish_via_media(store, cand, envelope)
    # now rollback — FINDING 5 requires owner-approved envelope
    snapshot_dir = parent  # retained pre-migration snapshot
    # Build rollback envelope: hash the snapshot's four files
    from pact_v4.runtime.book_memory_migration import _file_hash as _rfh, CANONICAL_FILES as _CF
    import json as _js
    # Create a minimal approved rollback envelope (reuse manifest from before)
    rollback_manifest = {"decisions": [{"key": "rollback", "decision": "retain"}]}
    rollback_candidate_dir = snapshot_dir  # snapshot_dir is candidate equivalent
    rollback_hashes = {}
    for _fname in _CF:
        _p = snapshot_dir / _fname
        if not _p.exists():
            _p = snapshot_dir / "state" / _fname
        # compute hash via raw bytes
        import hashlib
        _h = hashlib.sha256()
        with open(_p, "rb") as _f:
            while chunk := _f.read(8192):
                _h.update(chunk)
        rollback_hashes[_fname] = _h.hexdigest()
    rollback_envelope = {
        "schema": "pact-migration-envelope/v1",
        "parent_revision": store.read_current()["revision_id"],
        "candidate_hashes": rollback_hashes,
        "manifest": rollback_manifest,
        "approval_identity": "owner",
        "approved": True,
    }
    result = rollback_via_media(store, snapshot_dir, envelope=rollback_envelope)
    assert "revision_id" in result
    # Ensure new revision different from previous
    assert result["revision_id"] != envelope["parent_revision"]

def test_build_migration_rejects_stale_v1_index(tmp_path):
    """FINDING 6: stale/copied v1 index (missing v2 schema) is rejected."""
    from pact_v4.runtime.book_memory_migration import build_migration_candidate, migrate_to_v2
    import json
    parent = tmp_path / "parent"
    parent.mkdir()
    for fname in ["glossary.json", "book_memory.json", "chapter_index.json", "observations.json"]:
        data = {"Hello": "Привет"} if fname=="glossary.json" else {"characters": {}, "entities": {}} if fname=="book_memory.json" else {} if fname=="chapter_index.json" else {"glossary": {}, "book_memory": {}}
        (parent / fname).write_text(json.dumps(data) + "\n")
    migrated = migrate_to_v2(json.loads((parent / "book_memory.json").read_text()), json.loads((parent / "glossary.json").read_text()))
    # Stale v1 index: copy parent's index (which lacks $schema v2) — should be rejected
    stale = json.loads((parent / "chapter_index.json").read_text())
    cand = tmp_path / "cand"
    try:
        build_migration_candidate(parent, cand, migrated, stale)
        assert False, "should have rejected stale v1 index"
    except RuntimeError as e:
        assert "stale" in str(e).lower() or "schema" in str(e).lower()

def test_rollback_fails_without_approval(tmp_path):
    """FINDING 5: rollback without owner-approved envelope fails closed."""
    from pact_v4.snapshot.store import BookStore
    import json
    store_root = tmp_path / "store"
    store = BookStore(book_id="1", root=str(store_root))
    store.init_store()
    parent = tmp_path / "parent"
    parent.mkdir()
    for fname in ["glossary.json", "book_memory.json", "chapter_index.json", "observations.json"]:
        data = {"Hello": "Привет"} if fname=="glossary.json" else {"characters": {}, "entities": {}} if fname=="book_memory.json" else {} if fname=="chapter_index.json" else {"glossary": {}, "book_memory": {}}
        (parent / fname).write_text(json.dumps(data) + "\n")
    try:
        rollback_via_media(store, parent, envelope=None)
        assert False, "should fail without approval"
    except RuntimeError as e:
        assert "approval" in str(e).lower()
    # also with unapproved envelope
    try:
        rollback_via_media(store, parent, envelope={"approved": False, "approval_identity": None, "candidate_hashes": {}})
        assert False
    except RuntimeError as e:
        assert "approval" in str(e).lower()
