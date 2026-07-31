"""Phase 3B contract tests for pact_v4.phase3.assembly (AssembledChapter)."""
from __future__ import annotations

from typing import Tuple

import pytest

from pact_v4.phase1.models import (
    Candidate,
    ChunkPlan,
    ChunkPlanArtifact,
    ConfigArtifact,
    Snapshot,
    SourceArtifact,
    canonical_json_hash,
)
from pact_v4.phase3.assembly import AssembledChapter


def _hash(seed: str) -> str:
    return canonical_json_hash({"seed": seed})


def _source(chapter_id: str = "ch044", pid_count: int = 16) -> SourceArtifact:
    pairs = tuple((f"p{i:05d}", f"English sentence {i}.") for i in range(pid_count))
    return SourceArtifact(chapter_id=chapter_id, source=pairs)


def _snapshot(source: SourceArtifact) -> Snapshot:
    return Snapshot(
        chapter_id=source.chapter_id,
        pids=tuple(pid for pid, _ in source.source),
        context="ctx-v1",
        glossary_hash=_hash("glossary"),
        book_memory_hash=_hash("book_memory"),
        chapter_memory_hash=_hash("chapter_memory"),
    )


def _two_chunk_plan(snapshot: Snapshot) -> Tuple[ChunkPlanArtifact, ChunkPlan, ChunkPlan]:
    half = len(snapshot.pids) // 2
    # 50 words/PID keeps each half comfortably inside ChunkPlan's fixed
    # word-based bounds (MIN_WORDS=280/MAX_WORDS=640).
    chunk1 = ChunkPlan(
        chunk_id="chunk0001", snapshot_hash=snapshot.snapshot_hash,
        pids=snapshot.pids[:half], word_counts=tuple(50 for _ in snapshot.pids[:half]),
    )
    chunk2 = ChunkPlan(
        chunk_id="chunk0002", snapshot_hash=snapshot.snapshot_hash,
        pids=snapshot.pids[half:], word_counts=tuple(50 for _ in snapshot.pids[half:]),
    )
    artifact = ChunkPlanArtifact.create(snapshot, (chunk1, chunk2))
    return artifact, chunk1, chunk2


def _config(version: str = "v1") -> ConfigArtifact:
    return ConfigArtifact(version=version, values={"model": "qwen-mock"})


def _candidate(
    *, chunk: ChunkPlan, suffix: str, source: SourceArtifact, snapshot: Snapshot,
    chunk_plan: ChunkPlanArtifact, config: ConfigArtifact,
) -> Candidate:
    source_map = dict(source.source)
    translation = tuple((pid, f"RU[{pid}]") for pid in chunk.pids)
    return Candidate.create(
        candidate_id=f"{chunk.chunk_id}:{suffix}",
        chunk_id=chunk.chunk_id,
        role="fidelity_first",
        translation=translation,
        source=source,
        snapshot=snapshot,
        chunk_plan=chunk_plan,
        config=config,
    )


def _env():
    source = _source()
    snapshot = _snapshot(source)
    chunk_plan, chunk1, chunk2 = _two_chunk_plan(snapshot)
    config = _config()
    return source, snapshot, chunk_plan, chunk1, chunk2, config


# 1. Happy path: multi-chunk assembly concatenates winners in chunk-plan/PID order.
def test_assemble_concatenates_winners_in_pid_order():
    source, snapshot, chunk_plan, chunk1, chunk2, config = _env()
    c1 = _candidate(chunk=chunk1, suffix="A", source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config)
    c2 = _candidate(chunk=chunk2, suffix="A", source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config)

    chapter = AssembledChapter.assemble(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config,
        candidates={chunk1.chunk_id: c1, chunk2.chunk_id: c2},
    )

    assert tuple(pid for pid, _ in chapter.translation) == snapshot.pids
    assert chapter.as_pid_map()["p00000"] == "RU[p00000]"
    assert chapter.source_hash == source.source_hash
    assert chapter.snapshot_hash == snapshot.snapshot_hash
    assert chapter.chunk_plan_hash == chunk_plan.plan_hash
    assert chapter.config_identity == config.config_identity


# 2. Missing winner for a chunk raises — no partial/best-effort chapter.
def test_missing_winner_raises():
    source, snapshot, chunk_plan, chunk1, chunk2, config = _env()
    c1 = _candidate(chunk=chunk1, suffix="A", source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config)

    with pytest.raises(ValueError, match="no winning candidate"):
        AssembledChapter.assemble(
            source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config,
            candidates={chunk1.chunk_id: c1},
        )


# 3. A candidate belonging to a foreign snapshot/config is rejected, not silently included.
def test_foreign_identity_candidate_rejected():
    source, snapshot, chunk_plan, chunk1, chunk2, config = _env()
    c1 = _candidate(chunk=chunk1, suffix="A", source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config)

    other_config = _config(version="v2-different")
    other_source = _source()
    other_snapshot = _snapshot(other_source)
    _, other_chunk1, other_chunk2 = _two_chunk_plan(other_snapshot)
    other_chunk_plan = ChunkPlanArtifact.create(other_snapshot, (other_chunk1, other_chunk2))
    foreign_c2 = _candidate(
        chunk=other_chunk2, suffix="A", source=other_source, snapshot=other_snapshot,
        chunk_plan=other_chunk_plan, config=other_config,
    )

    with pytest.raises(ValueError):
        AssembledChapter.assemble(
            source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config,
            candidates={chunk1.chunk_id: c1, chunk2.chunk_id: foreign_c2},
        )


# 5. chapter_hash determinism and sensitivity to content changes.
def test_chapter_hash_deterministic_and_sensitive():
    source, snapshot, chunk_plan, chunk1, chunk2, config = _env()
    c1 = _candidate(chunk=chunk1, suffix="A", source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config)
    c2 = _candidate(chunk=chunk2, suffix="A", source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config)
    candidates = {chunk1.chunk_id: c1, chunk2.chunk_id: c2}

    chapter_a = AssembledChapter.assemble(source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config, candidates=candidates)
    chapter_b = AssembledChapter.assemble(source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config, candidates=candidates)
    assert chapter_a.chapter_hash == chapter_b.chapter_hash

    c2_edited = Candidate.create(
        candidate_id=f"{chunk2.chunk_id}:B",
        chunk_id=chunk2.chunk_id,
        role="fidelity_first",
        translation=tuple((pid, f"RU-edited[{pid}]") for pid in chunk2.pids),
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config,
    )
    chapter_c = AssembledChapter.assemble(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config,
        candidates={chunk1.chunk_id: c1, chunk2.chunk_id: c2_edited},
    )
    assert chapter_c.chapter_hash != chapter_a.chapter_hash
