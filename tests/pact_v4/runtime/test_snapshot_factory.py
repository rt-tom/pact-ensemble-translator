"""Tests for ``pact_v4.runtime.snapshot_factory``.

No network. No model calls. Just verifies that:

* A list of ``SourceBlock`` becomes a valid ``SourceArtifact`` whose
  identity is content-derived.
* A ``ChapterMemory`` and ``SourceArtifact`` produce a ``Snapshot``
  with the right PIDs and with hashes that change when the memory
  contents change.
* A ``ConfigArtifact`` records the run's runtime values.
"""
from __future__ import annotations

import json

import pytest

from pact_v4.phase0b.source_html import SourceBlock, parse_source_html
from pact_v4.phase1.models import (
    ConfigArtifact,
    Snapshot,
    SourceArtifact,
    canonical_json_hash,
)
from pact_v4.runtime.snapshot_factory import (
    ChapterMemory,
    build_config_artifact,
    build_snapshot,
    build_source_artifact,
)


def _block(pid: str, text: str, index: int = 0) -> SourceBlock:
    return SourceBlock(
        pid=pid, index=index, tag="p", text=text,
        html=f"<p>{text}</p>", structural_role="paragraph",
        inline_spans=(), word_count=len(text.split()),
    )


def test_build_source_artifact_uses_pids_in_block_order():
    blocks = [
        _block("p00001", "First sentence.", index=0),
        _block("p00002", "Second sentence.", index=1),
        _block("p00003", "Third sentence.", index=2),
    ]
    source = build_source_artifact(chapter_id="ch046", blocks=blocks)
    assert isinstance(source, SourceArtifact)
    assert source.chapter_id == "ch046"
    assert tuple(pid for pid, _ in source.source) == ("p00001", "p00002", "p00003")
    # Identity is content-derived: rewriting any PID text changes the hash.
    modified = [_block("p00001", "Different.", index=0), *blocks[1:]]
    again = build_source_artifact(chapter_id="ch046", blocks=modified)
    assert again.source_hash != source.source_hash


def test_build_source_artifact_rejects_empty_blocks():
    with pytest.raises(ValueError, match="empty block list"):
        build_source_artifact(chapter_id="ch046", blocks=[])


def test_build_snapshot_derives_memory_hashes_from_contents():
    blocks = [
        _block("p00001", "First.", index=0),
        _block("p00002", "Second.", index=1),
    ]
    source = build_source_artifact(chapter_id="ch046", blocks=blocks)
    memory = ChapterMemory(
        glossary={"Alice": "Алиса"},
        book_memory={"style": "noir"},
    )
    snapshot = build_snapshot(chapter_id="ch046", source=source, memory=memory)
    assert isinstance(snapshot, Snapshot)
    assert snapshot.pids == ("p00001", "p00002")
    # The memory hashes are deterministic sha256 hex of canonical JSON.
    assert snapshot.glossary_hash == canonical_json_hash({"Alice": "Алиса"})
    assert snapshot.book_memory_hash == canonical_json_hash({"style": "noir"})


def test_build_snapshot_changes_identity_when_memory_changes():
    blocks = [_block("p00001", "Only.", index=0)]
    source = build_source_artifact(chapter_id="ch046", blocks=blocks)
    snap_a = build_snapshot(
        chapter_id="ch046", source=source,
        memory=ChapterMemory(glossary={}, book_memory={}),
    )
    snap_b = build_snapshot(
        chapter_id="ch046", source=source,
        memory=ChapterMemory(glossary={"New": "Нов."}, book_memory={}),
    )
    assert snap_a.snapshot_hash != snap_b.snapshot_hash


def test_build_config_artifact_records_runtime_values():
    config = build_config_artifact(
        version="pact-v4-driver/phase12/draft/v1",
        values={"temperature": 0.2, "seed": 7, "model": "gemma-4-26B-A4B-it-UD-Q4_K_XL"},
    )
    assert isinstance(config, ConfigArtifact)
    assert config.version == "pact-v4-driver/phase12/draft/v1"
    assert config.values["temperature"] == 0.2


def test_chapter_memory_from_directory_handles_missing_files(tmp_path):
    # No glossary.json / book_memory.json in tmp_path — must produce an
    # empty ChapterMemory without raising.
    memory = ChapterMemory.from_directory(tmp_path)
    assert memory.glossary == {}
    assert memory.book_memory == {}
    assert memory.source_dir == tmp_path



def test_chapter_memory_from_directory_handles_null_book_memory(tmp_path):
    # A book_memory.json containing JSON null loads as None (pre-existing
    # _load_json tolerance); build_snapshot and the B5 allowlist builders
    # must both handle it (None == empty memory).
    (tmp_path / "book_memory.json").write_text("null", encoding="utf-8")
    memory = ChapterMemory.from_directory(tmp_path)
    assert memory.book_memory is None
    blocks = [_block("p00001", "Only.", index=0)]
    source = build_source_artifact(chapter_id="ch046", blocks=blocks)
    snapshot = build_snapshot(chapter_id="ch046", source=source, memory=memory)
    assert snapshot.book_memory_hash == canonical_json_hash({})
