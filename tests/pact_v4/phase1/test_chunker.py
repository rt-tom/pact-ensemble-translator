import hashlib

import pytest
from pact_v4.phase0b.source_html import SourceBlock
from pact_v4.phase1.chunker import ChunkPlanner
from pact_v4.phase1.models import ChunkPlan

SNAPSHOT_HASH = hashlib.sha256(b"test-snapshot").hexdigest()


def _make_block(pid: str, role: str = "paragraph", index: int = 0) -> SourceBlock:
    return SourceBlock(
        pid=pid,
        index=index,
        tag="p",
        text=f"Block {pid}",
        html=f"<p>Block {pid}</p>",
        structural_role=role,
        inline_spans=(),
        word_count=3,
    )


def _make_blocks(count: int, role: str = "paragraph", start: int = 1) -> list[SourceBlock]:
    return [_make_block(f"p{i:05d}", role=role, index=i - 1) for i in range(start, start + count)]


def _plan(planner: ChunkPlanner, blocks, **kwargs):
    return planner.plan(blocks, snapshot_hash=SNAPSHOT_HASH, **kwargs)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def planner():
    return ChunkPlanner()


# ---------------------------------------------------------------------------
# Empty / edge cases
# ---------------------------------------------------------------------------

def test_empty_blocks_returns_empty_list(planner):
    assert _plan(planner, []) == []


def test_single_chunk_below_min_size(planner):
    blocks = _make_blocks(5)
    result = _plan(planner, blocks)
    assert len(result) == 1
    assert result[0].pids == ("p00001", "p00002", "p00003", "p00004", "p00005")
    assert result[0].undersized_exception is True


def test_exactly_min_size(planner):
    blocks = _make_blocks(8)
    result = _plan(planner, blocks)
    assert len(result) == 1
    assert len(result[0].pids) == 8
    assert result[0].undersized_exception is False


def test_exactly_max_size(planner):
    blocks = _make_blocks(20)
    result = _plan(planner, blocks)
    assert len(result) == 1
    assert len(result[0].pids) == 20


# ---------------------------------------------------------------------------
# Full PID coverage — no gaps, no duplicates, no extras
# ---------------------------------------------------------------------------

def test_full_pid_coverage(planner):
    blocks = _make_blocks(50)
    result = _plan(planner, blocks)

    all_pids = []
    for chunk in result:
        all_pids.extend(chunk.pids)

    expected = [b.pid for b in blocks]
    assert all_pids == expected


def test_no_duplicate_pids_across_chunks(planner):
    blocks = _make_blocks(50)
    result = _plan(planner, blocks)

    seen = set()
    for chunk in result:
        for pid in chunk.pids:
            assert pid not in seen, f"Duplicate PID {pid}"
            seen.add(pid)

    assert len(seen) == 50


# ---------------------------------------------------------------------------
# Chunk size bounds — max is hard, min is soft (flagged, never silently violated)
# ---------------------------------------------------------------------------

def test_no_chunk_exceeds_max_size():
    planner = ChunkPlanner(min_size=8, max_size=20)
    blocks = _make_blocks(100)
    result = _plan(planner, blocks)

    for chunk in result:
        assert len(chunk.pids) <= 20


def test_chunks_are_at_least_min_size_except_possible_last():
    planner = ChunkPlanner(min_size=8, max_size=20)
    blocks = _make_blocks(100)
    result = _plan(planner, blocks)

    for i, chunk in enumerate(result):
        if i < len(result) - 1:
            assert len(chunk.pids) >= 8
            assert chunk.undersized_exception is False


def test_undersized_exception_set_exactly_when_below_soft_min():
    planner = ChunkPlanner(min_size=8, max_size=20)
    for count in range(1, 40):
        blocks = _make_blocks(count)
        result = _plan(planner, blocks)
        for chunk in result:
            assert chunk.undersized_exception == (len(chunk.pids) < 8), (
                f"count={count} chunk={chunk.chunk_id} size={len(chunk.pids)} "
                f"flag={chunk.undersized_exception}"
            )


# ---------------------------------------------------------------------------
# Natural break / dialogue boundaries
# ---------------------------------------------------------------------------

def test_dialogue_exchange_not_split():
    planner = ChunkPlanner(min_size=8, max_size=20)
    blocks = (
        _make_blocks(8)
        + _make_blocks(5, role="dialogue", start=9)
        + _make_blocks(8, start=14)
    )
    result = _plan(planner, blocks)

    dialogue_pids = {b.pid for b in blocks if b.structural_role == "dialogue"}
    dialogue_chunks = [
        chunk for chunk in result
        if any(pid in chunk.pids for pid in dialogue_pids)
    ]
    assert len(dialogue_chunks) == 1


def test_break_between_non_dialogue_blocks():
    planner = ChunkPlanner(min_size=8, max_size=20)
    blocks = _make_blocks(8) + _make_blocks(8, start=9)
    result = _plan(planner, blocks)
    assert len(result) == 1


def test_forced_break_at_hard_cap():
    planner = ChunkPlanner(min_size=8, max_size=20)
    blocks = _make_blocks(25, role="dialogue")
    result = _plan(planner, blocks)
    assert len(result) >= 2
    for chunk in result:
        assert len(chunk.pids) <= 20


# ---------------------------------------------------------------------------
# right_en context (source EN text of the next chunk's first N PIDs).
#
# left_ru is intentionally always empty at planning time: the previous
# chunk's translation doesn't exist yet (V4_MVP_SPEC_RU.md §3.3) — it is
# looked up by chunk ownership at generation time, not stored here.
# ---------------------------------------------------------------------------

def test_left_ru_always_empty_at_planning_time(planner):
    blocks = _make_blocks(30)
    result = _plan(planner, blocks, context_right_count=5)
    for chunk in result:
        assert chunk.context.left_ru == ""


def test_right_context_empty_for_last_chunk(planner):
    blocks = _make_blocks(30)
    result = _plan(planner, blocks, context_right_count=5)
    assert result[-1].context.right_en == ()


def test_right_context_is_source_text_of_next_chunk(planner):
    blocks = _make_blocks(30)
    result = _plan(planner, blocks, context_right_count=5)
    assert len(result) >= 2
    expected = tuple(f"Block {pid}" for pid in result[1].pids[:5])
    assert result[0].context.right_en == expected


def test_context_count_limit(planner):
    blocks = _make_blocks(30)
    result = _plan(planner, blocks, context_right_count=3)
    if len(result) >= 2:
        assert len(result[0].context.right_en) == 3


def test_zero_context_yields_empty(planner):
    blocks = _make_blocks(30)
    result = _plan(planner, blocks, context_right_count=0)
    for chunk in result:
        assert chunk.context.right_en == ()


# ---------------------------------------------------------------------------
# Small tail merging
# ---------------------------------------------------------------------------

def test_small_tail_merged():
    planner = ChunkPlanner(min_size=8, max_size=20)
    blocks = _make_blocks(27)
    result = _plan(planner, blocks)
    all_pids = []
    for c in result:
        all_pids.extend(c.pids)
    assert all_pids == [b.pid for b in blocks]


def test_small_tail_alone_when_merge_exceeds_max():
    planner = ChunkPlanner(min_size=8, max_size=20)
    blocks = _make_blocks(19) + _make_blocks(3, start=20)
    result = _plan(planner, blocks)
    assert len(result[-1].pids) >= 1


# ---------------------------------------------------------------------------
# Custom min/max
# ---------------------------------------------------------------------------

def test_custom_min_max():
    planner = ChunkPlanner(min_size=5, max_size=10)
    blocks = _make_blocks(30)
    result = _plan(planner, blocks)
    for i, chunk in enumerate(result):
        if i < len(result) - 1:
            assert len(chunk.pids) >= 5
        assert len(chunk.pids) <= 10


def test_invalid_min_max_raises():
    with pytest.raises(ValueError):
        ChunkPlanner(min_size=0, max_size=10)
    with pytest.raises(ValueError):
        ChunkPlanner(min_size=10, max_size=5)


# ---------------------------------------------------------------------------
# ChunkPlan model constraints (hard max never relaxed, soft min needs a flag)
# ---------------------------------------------------------------------------

def test_chunk_plan_above_hard_max_rejected_even_with_flag():
    with pytest.raises(ValueError, match="exceeds hard cap"):
        ChunkPlan(
            chunk_id="c1", snapshot_hash=SNAPSHOT_HASH,
            pids=tuple(f"p{i:05d}" for i in range(21)),
            undersized_exception=True,
        )


def test_chunk_plan_below_soft_min_needs_flag():
    with pytest.raises(ValueError, match="below soft minimum"):
        ChunkPlan(chunk_id="c1", snapshot_hash=SNAPSHOT_HASH, pids=("p1", "p2"))
    # With the flag, it's accepted.
    plan = ChunkPlan(
        chunk_id="c1", snapshot_hash=SNAPSHOT_HASH, pids=("p1", "p2"),
        undersized_exception=True,
    )
    assert plan.pids == ("p1", "p2")


# ---------------------------------------------------------------------------
# Regression: window is not degenerate, tail is rebalanced
# ---------------------------------------------------------------------------

def test_break_at_exactly_min_size_is_reachable():
    """range(high, low - 1, -1) must include start + min_size."""
    planner = ChunkPlanner(min_size=8, max_size=20)
    # Only one legal boundary at index 8: everything after it is one
    # uninterrupted dialogue exchange.
    blocks = _make_blocks(8) + _make_blocks(14, role="dialogue", start=9)
    result = _plan(planner, blocks)
    assert len(result[0].pids) == 8


def test_strong_break_preferred_over_hard_cap():
    """A heading inside the window wins over cutting at max_size."""
    planner = ChunkPlanner(min_size=8, max_size=20)
    blocks = (
        _make_blocks(11)
        + [_make_block("h00001", role="heading", index=11)]
        + _make_blocks(20, start=13)
    )
    result = _plan(planner, blocks)
    assert result[0].pids[-1] == "p00011"
    assert result[1].pids[0] == "h00001"


def test_tail_rebalanced_instead_of_tiny_chunk():
    """27+ blocks must never produce a 1-2 PID tail."""
    planner = ChunkPlanner(min_size=8, max_size=20)
    for count in range(21, 60):
        blocks = _make_blocks(count)
        result = _plan(planner, blocks)
        assert len(result[-1].pids) >= 8, f"count={count} tail={len(result[-1].pids)}"
        for chunk in result:
            assert len(chunk.pids) <= 20


def test_no_pid_loss_after_rebalance():
    planner = ChunkPlanner(min_size=8, max_size=20)
    for count in range(1, 80):
        blocks = _make_blocks(count)
        result = _plan(planner, blocks)
        seen = [pid for chunk in result for pid in chunk.pids]
        assert seen == [b.pid for b in blocks], f"count={count}"


def test_full_pid_ownership_holds_for_every_plan_size():
    """Cross-check against Phase 1A's validate_full_pid_ownership."""
    from pact_v4.phase1.models import Snapshot, validate_full_pid_ownership

    planner = ChunkPlanner(min_size=8, max_size=20)
    for count in (1, 7, 8, 20, 21, 45, 79):
        blocks = _make_blocks(count)
        pids = tuple(b.pid for b in blocks)
        snapshot = Snapshot(
            chapter_id="ch999", pids=pids, context="",
            glossary_hash=hashlib.sha256(b"g").hexdigest(),
            book_memory_hash=hashlib.sha256(b"b").hexdigest(),
            chapter_memory_hash=hashlib.sha256(b"c").hexdigest(),
        )
        result = planner.plan(blocks, snapshot_hash=snapshot.snapshot_hash)
        validate_full_pid_ownership(tuple(result), snapshot)
