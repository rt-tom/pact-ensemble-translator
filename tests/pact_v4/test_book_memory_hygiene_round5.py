"""Round 5 findings 1-4: conflict/quarantine gate, bible_renderer fail-soft, rollback hashes, migration deterministic rebuild."""
import hashlib
import json
from pathlib import Path

import pytest

from pact_v4.audit.entity_extractor import CACHE_SCHEMA, EXTRACTOR_VERSION, ENTITY_CONTEXT_SCHEMA, entity_context_cache_key
from pact_v4.runtime.bible_renderer import render_bible_section, CHAPTER_INDEX_V2_SCHEMA
from pact_v4.runtime.book_memory_policy import BOOK_MEMORY_POLICY_VERSION
from pact_v4.runtime.book_memory_migration import build_migration_candidate, build_index_from_memory, create_envelope, rollback_via_media, _file_hash, CANONICAL_FILES
from pact_v4.runtime.snapshot_factory import ChapterMemory, build_snapshot, build_source_artifact
from pact_v4.phase0b.source_html import SourceBlock


def _block(pid, text, idx=0):
    return SourceBlock(pid=pid, index=idx, tag="p", text=text, html=f"<p>{text}</p>", structural_role="paragraph", inline_spans=(), word_count=len(text.split()))

# --- Finding 2: bible_renderer fail-soft ---
def test_bible_renderer_missing_schema_fails_soft_to_seed():
    bm = {
        "pov": {"gender": "male", "source_name": "Narrator"},
        "facts": [{"fact": "seed fact A", "seed": True}, {"fact": "future fact B", "chapter": "0010"}],
    }
    idx = {
        "0001": {"characters": ["FutureChar"], "facts": ["future fact B"], "address": []},
    }  # missing $schema
    out = render_bible_section("0001", idx, bm)
    # should render only narrator+seed, not FutureChar or future fact B
    assert "Narrator: male" in out
    assert "seed fact A" in out
    assert "FutureChar" not in out
    assert "future fact B" not in out

def test_bible_renderer_unknown_policy_version_fails_soft():
    bm = {
        "pov": {"gender": "female"},
        "facts": [{"fact": "seed fact", "seed": True}],
    }
    idx = {
        "$schema": CHAPTER_INDEX_V2_SCHEMA,
        "$book_memory_policy_version": "unknown/v9",
        "0001": {"characters": ["Blake"], "facts": [], "address": []},
    }
    out = render_bible_section("0001", idx, bm)
    assert "Blake" not in out
    assert "Narrator: female" in out
    assert "seed fact" in out

def test_snapshot_factory_missing_schema_fails_soft():
    blocks = [_block("p00001", "Only.", 0)]
    source = build_source_artifact(chapter_id="ch001", blocks=blocks)
    base = ChapterMemory(glossary={}, book_memory={})
    snap_no = build_snapshot(chapter_id="ch001", source=source, memory=base)
    snap_missing = build_snapshot(chapter_id="ch001", source=source, memory=ChapterMemory(glossary={}, book_memory={}, chapter_index={"ch001": {"characters": ["X"]}}))
    assert snap_missing.snapshot_hash == snap_no.snapshot_hash
    assert snap_missing.chapter_index_hash == snap_no.chapter_index_hash

# --- Finding 3: rollback requires exact hashes ---
def _make_store(tmp_path):
    from pact_v4.snapshot.store import BookStore
    store = BookStore("test-book", root=str(tmp_path / "store"))
    store.init_store()
    return store

def _seed_store_with_revision(store, parent_dir):
    from pact_v4.snapshot.manifest import Manifest, StateFileEntry
    import datetime
    # Create initial revision rev-0001 from parent_dir via bootstrap-like
    # Use store's internal to create a revision directly: write files to snapshots/rev-0001
    import shutil
    rev = "rev-0001"
    snap_dir = store.snapshot_dir(rev)
    (snap_dir / "state").mkdir(parents=True, exist_ok=True)
    for fname in CANONICAL_FILES:
        shutil.copy2(str(parent_dir / fname), str(snap_dir / "state" / fname))
    # Write manifest and CURRENT
    state_entries = []
    for fname in CANONICAL_FILES:
        import hashlib
        p = snap_dir / "state" / fname
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        size = p.stat().st_size
        state_entries.append(StateFileEntry(rel_path=f"state/{fname}", sha256=h, size=size))
    manifest = Manifest(schema_version="v1", book_id=store.book_id, revision_id=rev, parent_revision_id=None, created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(), published_at=datetime.datetime.now(datetime.timezone.utc).isoformat(), terminal_status="bootstrap-seed", tool_version="test", source={"path_on_rt": str(parent_dir)}, state_files=state_entries, excludes=[], code_commit="")
    manifest.write(snap_dir / "manifest.json")
    # Write CURRENT
    (store.root / "CURRENT.json").write_text(json.dumps({"revision_id": rev}, ensure_ascii=False), encoding="utf-8")
    # Also need to update store's current reading
    return rev

def _setup_parent_dir(tmp_path):
    d = tmp_path / "parent"
    d.mkdir(parents=True, exist_ok=True)
    for fname in CANONICAL_FILES:
        if fname == "book_memory.json":
            (d / fname).write_text(json.dumps({"schema":"pact-v4-book-memory/v2","book_memory_policy_version":"book-memory-policy/v1","characters":{}}, ensure_ascii=False), encoding="utf-8")
        elif fname == "chapter_index.json":
            (d / fname).write_text(json.dumps({"$schema":"pact-v4-chapter-index/v2","$book_memory_policy_version":"book-memory-policy/v1"}, ensure_ascii=False), encoding="utf-8")
        elif fname == "glossary.json":
            (d / fname).write_text("{}", encoding="utf-8")
        elif fname == "observations.json":
            (d / fname).write_text("{}", encoding="utf-8")
    return d

def test_rollback_missing_candidate_hashes_fails_closed(tmp_path):
    parent = _setup_parent_dir(tmp_path)
    store = _make_store(tmp_path)
    _seed_store_with_revision(store, parent)
    # Create snapshot_dir copy for rollback
    snap_dir = tmp_path / "snap"
    snap_dir.mkdir(parents=True, exist_ok=True)
    import shutil
    for fname in CANONICAL_FILES:
        shutil.copy2(str(parent / fname), str(snap_dir / fname))
    envelope = {"approved": True, "approval_identity": "owner", "candidate_hashes": None, "parent_revision": "rev-0001"}
    # Missing candidate_hashes -> should fail
    with pytest.raises(RuntimeError, match="candidate_hashes"):
        rollback_via_media(store, snap_dir, envelope=envelope)

def test_rollback_mismatched_hash_fails_closed(tmp_path):
    parent = _setup_parent_dir(tmp_path)
    store = _make_store(tmp_path)
    _seed_store_with_revision(store, parent)
    snap_dir = tmp_path / "snap2"
    snap_dir.mkdir(parents=True, exist_ok=True)
    import shutil
    for fname in CANONICAL_FILES:
        shutil.copy2(str(parent / fname), str(snap_dir / fname))
    # Create correct hashes then corrupt one
    hashes = {fname: _file_hash(parent / fname) for fname in CANONICAL_FILES}
    hashes["glossary.json"] = "0"*64  # mismatched
    envelope = {"approved": True, "approval_identity": "owner", "candidate_hashes": hashes, "parent_revision": "rev-0001"}
    with pytest.raises(RuntimeError, match="hash mismatch"):
        rollback_via_media(store, snap_dir, envelope=envelope)

def test_rollback_correct_hashes_publishes_new_revision(tmp_path):
    parent = _setup_parent_dir(tmp_path)
    store = _make_store(tmp_path)
    _seed_store_with_revision(store, parent)
    snap_dir = tmp_path / "snap3"
    snap_dir.mkdir(parents=True, exist_ok=True)
    import shutil
    for fname in CANONICAL_FILES:
        shutil.copy2(str(parent / fname), str(snap_dir / fname))
    hashes = {fname: _file_hash(snap_dir / fname) for fname in CANONICAL_FILES}
    envelope = {"approved": True, "approval_identity": "owner", "candidate_hashes": hashes, "parent_revision": "rev-0001"}
    result = rollback_via_media(store, snap_dir, envelope=envelope)
    assert result["revision_id"] != "rev-0001"
    cur = store.read_current()
    assert cur["revision_id"] == result["revision_id"]

# --- Finding 4: migration deterministic rebuild ---
def test_migration_rebuilt_index_diverges_rejected(tmp_path):
    parent = _setup_parent_dir(tmp_path)
    bm = {
        "schema":"pact-v4-book-memory/v2",
        "book_memory_policy_version":"book-memory-policy/v1",
        "characters": {
            "Alice": {"memory_class":"named_character","chapters":["0001"],"first_seen_chapter":"0001","variants":{},"field_provenance":{}},
            "Bob": {"memory_class":"named_character","chapters":["0002"],"first_seen_chapter":"0002","variants":{},"field_provenance":{}},
        },
        "entities": {},
        "facts": [{"fact":"Alice appears","chapter":"0001","keys":["Alice"]}],
        "policy": {"approved_terms":[]}
    }
    det = build_index_from_memory(bm)
    # Create divergent index: same schema but different content (swap characters)
    divergent = dict(det)
    # Modify one chapter entry
    if "0001" in divergent and isinstance(divergent["0001"], dict):
        divergent["0001"] = dict(divergent["0001"])
        divergent["0001"]["characters"] = ["Bob"]  # wrong
    candidate = tmp_path / "cand"
    with pytest.raises(RuntimeError, match="diverges"):
        build_migration_candidate(parent, candidate, bm, rebuilt_index=divergent)

def test_migration_rebuilt_none_is_deterministic(tmp_path):
    parent = _setup_parent_dir(tmp_path)
    bm = {
        "schema":"pact-v4-book-memory/v2",
        "book_memory_policy_version":"book-memory-policy/v1",
        "characters": {
            "Alice": {"memory_class":"named_character","chapters":["0001"],"first_seen_chapter":"0001","variants":{},"field_provenance":{}},
        },
        "entities": {
            "City": {"memory_class":"named_place","chapters":["0001"],"first_seen_chapter":"0001","variants":{},"field_provenance":{}},
        },
        "facts": [{"fact":"Alice in City","chapter":"0001","keys":["Alice"]}],
        "policy": {"approved_terms":[]}
    }
    det = build_index_from_memory(bm)
    candidate = tmp_path / "cand2"
    build_migration_candidate(parent, candidate, bm, rebuilt_index=None)
    idx = json.loads((candidate / "chapter_index.json").read_text(encoding="utf-8"))
    assert idx == det

def test_migration_stale_v1_rejected(tmp_path):
    parent = _setup_parent_dir(tmp_path)
    # Parent has v1 index (no schema)
    (parent / "chapter_index.json").write_text(json.dumps({"0001":{"characters":["Alice"]}}, ensure_ascii=False), encoding="utf-8")
    bm = {
        "schema":"pact-v4-book-memory/v2",
        "book_memory_policy_version":"book-memory-policy/v1",
        "characters": {"Alice": {"memory_class":"named_character","chapters":["0001"],"first_seen_chapter":"0001","variants":{},"field_provenance":{}}},
        "entities": {},
        "facts": [],
        "policy": {"approved_terms":[]}
    }
    stale = json.loads((parent / "chapter_index.json").read_text(encoding="utf-8"))
    candidate = tmp_path / "cand3"
    with pytest.raises(RuntimeError, match="stale|diverges"):
        build_migration_candidate(parent, candidate, bm, rebuilt_index=stale)

# --- Finding 1: run_book conflict & quarantined ---
def _setup_run_book_memory(tmp_path, book_memory):
    memory = tmp_path / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "glossary.json").write_text("{}", encoding="utf-8")
    (memory / "book_memory.json").write_text(json.dumps(book_memory, ensure_ascii=False), encoding="utf-8")
    (memory / "chapter_index.json").write_text(json.dumps({"$schema":"pact-v4-chapter-index/v2","$book_memory_policy_version":"book-memory-policy/v1"}, ensure_ascii=False), encoding="utf-8")
    (memory / "observations.json").write_text("{}", encoding="utf-8")
    return memory

def _write_html(src_dir, chapter_id, html):
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / f"{chapter_id}.html").write_text(html, encoding="utf-8")

def test_run_book_quarantined_evidence_not_promoted(tmp_path, monkeypatch):
    from pact_full_pipeline_runner_v1 import v4_book_run
    # Existing memory empty, but chapter has quarantined chunk containing the entity anchor
    book_memory = {"pov":{"gender":"male","source_name":"Blake"}, "characters":{}, "entities":{}}
    memory = _setup_run_book_memory(tmp_path, book_memory)
    out_base = tmp_path / "out"
    src_dir = tmp_path / "src"
    html = "<p>Blake met Rose at the gate.</p><p>She waved.</p>"
    _write_html(src_dir, "0001", html)
    # Setup out_dir artifacts with quarantined chunk0001 covering p00001 where Rose anchor is
    out_dir = out_base / "chapter_0001"
    out_dir.mkdir(parents=True, exist_ok=True)
    # chunk plan
    plan = {"artifact":"pact-v4-chunk-plan/v1","snapshot_hash":"test","plan_hash":"test","chunks":[{"chunk_id":"chunk0001","snapshot_hash":"test","pids":["p00001","p00002"],"word_counts":[],"context":{"left_ru":"","right_en":[]}, "undersized_exception": False}]}
    (out_dir / "chunk_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (out_dir / "selection_results.json").write_text(json.dumps({"chapter_id":"0001","results":[{"chunk_id":"chunk0001","status":"quarantined","quarantine_reason":"qwen_fidelity"}]}, ensure_ascii=False), encoding="utf-8")
    (out_dir / "strict_chapter_trial_record.json").write_text(json.dumps({"chapter_id":"0001","step8":{"status":"accepted_degraded"},"identities":{"source_hash":"hash1"},"operational_policy":{"audit":{"extractor_version": EXTRACTOR_VERSION}}}), encoding="utf-8")
    (out_dir / "translations.json").write_text(json.dumps({"p00001":"текст","p00002":"текст2"}, ensure_ascii=False), encoding="utf-8")
    # entity cache with Rose anchor in p00001 (quarantined)
    cache = {"schema": CACHE_SCHEMA, "entries": [{"key": entity_context_cache_key(source_hash="hash1", extractor_version=EXTRACTOR_VERSION), "context": {"schema": ENTITY_CONTEXT_SCHEMA, "extractor_version": EXTRACTOR_VERSION, "chapter_id":"0001","source_hash":"hash1","entities":[{"entity":"Rose","canonical_type":"woman","anchor":{"pid":"p00001","span":"Rose"},"aliases":[],"claims":[{"kind":"gender","value":"female","status":"verified","evidence":[{"pid":"p00001","span":"Rose"}],"evidence_windows":[["p00001","p00001"]]}],"memory_class":"named_character","memory_worthy":True}]}}]}
    (out_dir / "entity_context_cache.json").write_text(json.dumps(cache), encoding="utf-8")
    def fake_run(*a, **kw): return {"status":"ok"}
    monkeypatch.setattr(v4_book_run, "_run_one_chapter", fake_run)
    result = v4_book_run.run_book(memory_dir=memory, chapter_ids=["0001"], chapter_html_pattern=str(src_dir / "{chapter_id}.html"), out_base=out_base)
    bm_after = json.loads((memory / "book_memory.json").read_text(encoding="utf-8"))
    assert "Rose" not in bm_after.get("characters", {})
    # hash unchanged
    before_hash = result["chapters"][0]["book_memory_hash_before"]
    after_hash = result["chapters"][0]["book_memory_hash_after"]
    assert before_hash == after_hash
    # report lists rejected with quarantined_evidence
    report = json.loads((out_dir / "book_memory_candidates_report.json").read_text(encoding="utf-8"))
    assert any(r.get("reason") == "quarantined_evidence" for r in report.get("rejected", []))

def test_run_book_conflicting_name_rejected(tmp_path, monkeypatch):
    from pact_full_pipeline_runner_v1 import v4_book_run
    # Existing memory has Alice in both characters and entities (ambiguous) -> conflict
    book_memory = {
        "pov":{"gender":"male","source_name":"Blake"},
        "characters": {"Alice": {"memory_class":"named_character","chapters":["0000"],"first_seen_chapter":"0000","variants":{},"field_provenance":{}}},
        "entities": {"Alice": {"memory_class":"named_place","chapters":["0000"],"first_seen_chapter":"0000","variants":{},"field_provenance":{}}},
        "facts": []
    }
    memory = _setup_run_book_memory(tmp_path, book_memory)
    before_hash = hashlib.sha256((memory / "book_memory.json").read_bytes()).hexdigest()
    out_base = tmp_path / "out"
    src_dir = tmp_path / "src"
    html = "<p>Alice met Blake.</p><p>She waved.</p>"
    _write_html(src_dir, "0001", html)
    out_dir = out_base / "chapter_0001"
    out_dir.mkdir(parents=True, exist_ok=True)
    plan = {"artifact":"pact-v4-chunk-plan/v1","snapshot_hash":"test","plan_hash":"test","chunks":[{"chunk_id":"chunk0001","snapshot_hash":"test","pids":["p00001","p00002"],"word_counts":[],"context":{"left_ru":"","right_en":[]}, "undersized_exception": False}]}
    (out_dir / "chunk_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (out_dir / "selection_results.json").write_text(json.dumps({"chapter_id":"0001","results":[{"chunk_id":"chunk0001","status":"selected"}]}, ensure_ascii=False), encoding="utf-8")
    (out_dir / "strict_chapter_trial_record.json").write_text(json.dumps({"chapter_id":"0001","step8":{"status":"complete"},"identities":{"source_hash":"hash1"},"operational_policy":{"audit":{"extractor_version": EXTRACTOR_VERSION}}}), encoding="utf-8")
    (out_dir / "translations.json").write_text(json.dumps({"p00001":"текст","p00002":"текст2"}, ensure_ascii=False), encoding="utf-8")
    cache = {"schema": CACHE_SCHEMA, "entries": [{"key": entity_context_cache_key(source_hash="hash1", extractor_version=EXTRACTOR_VERSION), "context": {"schema": ENTITY_CONTEXT_SCHEMA, "extractor_version": EXTRACTOR_VERSION, "chapter_id":"0001","source_hash":"hash1","entities":[{"entity":"Alice","canonical_type":"woman","anchor":{"pid":"p00001","span":"Alice"},"aliases":[],"claims":[{"kind":"gender","value":"female","status":"verified","evidence":[{"pid":"p00001","span":"Alice"}],"evidence_windows":[["p00001","p00001"]]}],"memory_class":"named_character","memory_worthy":True}]}}]}
    (out_dir / "entity_context_cache.json").write_text(json.dumps(cache), encoding="utf-8")
    def fake_run(*a, **kw): return {"status":"ok"}
    monkeypatch.setattr(v4_book_run, "_run_one_chapter", fake_run)
    result = v4_book_run.run_book(memory_dir=memory, chapter_ids=["0001"], chapter_html_pattern=str(src_dir / "{chapter_id}.html"), out_base=out_base)
    bm_after = json.loads((memory / "book_memory.json").read_text(encoding="utf-8"))
    # Alice should not be newly promoted beyond existing; hash should remain same (no new entry)
    after_hash = hashlib.sha256((memory / "book_memory.json").read_bytes()).hexdigest()
    # Since Alice already existed, but gate should have rejected the incoming Alice with conflict, so hash unchanged from before plus no new chapters added?
    # The incoming would have added chapter 0001 if promoted; since rejected, chapters should stay ["0000"]
    assert bm_after["characters"]["Alice"]["chapters"] == ["0000"]
    assert before_hash == after_hash or result["chapters"][0]["book_memory_hash_before"] == result["chapters"][0]["book_memory_hash_after"]
    report = json.loads((out_dir / "book_memory_candidates_report.json").read_text(encoding="utf-8"))
    assert any(r.get("reason") == "conflict" for r in report.get("rejected", []))
