import pytest
from pact_v4.phase0b.source_html import SourceBlock
from pact_v4.phase1.chunker import ChunkPlanner
from pact_v4.phase1.models import ChunkPlan


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
    assert planner.plan([]) == []


def test_single_chunk_below_min_size(planner):
    blocks = _make_blocks(5)
    result = planner.plan(blocks)
    assert len(result) == 1
    assert result[0].pids == ["p00001", "p00002", "p00003", "p00004", "p00005"]


def test_exactly_min_size(planner):
    blocks = _make_blocks(8)
    result = planner.plan(blocks)
    assert len(result) == 1
    assert len(result[0].pids) == 8


def test_exactly_max_size(planner):
    blocks = _make_blocks(20)
    result = planner.plan(blocks)
    assert len(result) == 1
    assert len(result[0].pids) == 20


# ---------------------------------------------------------------------------
# Full PID coverage — no gaps, no duplicates, no extras
# ---------------------------------------------------------------------------

def test_full_pid_coverage(planner):
    blocks = _make_blocks(50)
    result = planner.plan(blocks)

    all_pids = []
    for chunk in result:
        all_pids.extend(chunk.pids)

    expected = [b.pid for b in blocks]
    assert all_pids == expected


def test_no_duplicate_pids_across_chunks(planner):
    blocks = _make_blocks(50)
    result = planner.plan(blocks)

    seen = set()
    for chunk in result:
        for pid in chunk.pids:
            assert pid not in seen, f"Duplicate PID {pid}"
            seen.add(pid)

    assert len(seen) == 50


# ---------------------------------------------------------------------------
# Chunk size bounds
# ---------------------------------------------------------------------------

def test_no_chunk_exceeds_max_size():
    planner = ChunkPlanner(min_size=8, max_size=20)
    blocks = _make_blocks(100)
    result = planner.plan(blocks)

    for chunk in result:
        assert len(chunk.pids) <= 20


def test_chunks_are_at_least_min_size_except_possible_last():
    planner = ChunkPlanner(min_size=8, max_size=20)
    blocks = _make_blocks(100)
    result = planner.plan(blocks)

    for i, chunk in enumerate(result):
        if i < len(result) - 1:
            assert len(chunk.pids) >= 8


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
    result = planner.plan(blocks)

    dialogue_pids = {b.pid for b in blocks if b.structural_role == "dialogue"}
    dialogue_chunks = [
        chunk for chunk in result
        if any(pid in chunk.pids for pid in dialogue_pids)
    ]
    assert len(dialogue_chunks) == 1


def test_break_between_non_dialogue_blocks():
    planner = ChunkPlanner(min_size=8, max_size=20)
    blocks = _make_blocks(8) + _make_blocks(8, start=9)
    result = planner.plan(blocks)
    assert len(result) == 1


def test_forced_break_at_hard_cap():
    planner = ChunkPlanner(min_size=8, max_size=20)
    blocks = _make_blocks(25, role="dialogue")
    result = planner.plan(blocks)
    assert len(result) >= 2
    for chunk in result:
        assert len(chunk.pids) <= 20


# ---------------------------------------------------------------------------
# left_context / right_context
# ---------------------------------------------------------------------------

def test_left_context_empty_for_first_chunk(planner):
    blocks = _make_blocks(30)
    result = planner.plan(blocks, context_left_count=5)
    assert result[0].left_context == []


def test_left_context_from_previous_chunk(planner):
    blocks = _make_blocks(30)
    result = planner.plan(blocks, context_left_count=5)
    assert len(result) >= 2
    expected = result[0].pids[-5:]
    assert result[1].left_context == expected


def test_right_context_empty_for_last_chunk(planner):
    blocks = _make_blocks(30)
    result = planner.plan(blocks, context_right_count=5)
    assert result[-1].right_context == []


def test_right_context_from_next_chunk(planner):
    blocks = _make_blocks(30)
    result = planner.plan(blocks, context_right_count=5)
    assert len(result) >= 2
    expected = result[1].pids[:5]
    assert result[0].right_context == expected


def test_context_count_limits(planner):
    blocks = _make_blocks(30)
    result = planner.plan(blocks, context_left_count=2, context_right_count=3)
    if len(result) >= 2:
        assert len(result[1].left_context) == 2
        assert len(result[0].right_context) == 3


def test_zero_context_yields_empty(planner):
    blocks = _make_blocks(30)
    result = planner.plan(blocks, context_left_count=0, context_right_count=0)
    for chunk in result:
        assert chunk.left_context == []
        assert chunk.right_context == []


# ---------------------------------------------------------------------------
# Small tail merging
# ---------------------------------------------------------------------------

def test_small_tail_merged():
    planner = ChunkPlanner(min_size=8, max_size=20)
    blocks = _make_blocks(27)
    result = planner.plan(blocks)
    all_pids = []
    for c in result:
        all_pids.extend(c.pids)
    assert all_pids == [b.pid for b in blocks]


def test_small_tail_alone_when_merge_exceeds_max():
    planner = ChunkPlanner(min_size=8, max_size=20)
    blocks = _make_blocks(19) + _make_blocks(3, start=20)
    result = planner.plan(blocks)
    assert len(result[-1].pids) >= 1


# ---------------------------------------------------------------------------
# Custom min/max
# ---------------------------------------------------------------------------

def test_custom_min_max():
    planner = ChunkPlanner(min_size=5, max_size=10)
    blocks = _make_blocks(30)
    result = planner.plan(blocks)
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
# Chunk plan model constraints
# ---------------------------------------------------------------------------

def test_chunk_plan_context_pid_overlap_raises():
    with pytest.raises(ValueError, match="PID in both pids and left_context"):
        ChunkPlan(chunk_id="c1", pids=["p1", "p2"], left_context=["p1"])

    with pytest.raises(ValueError, match="PID in both pids and right_context"):
        ChunkPlan(chunk_id="c1", pids=["p1", "p2"], right_context=["p1"])
