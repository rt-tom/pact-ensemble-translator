import hashlib

import pytest
from pact_v4.phase0b.source_html import SourceBlock
from pact_v4.phase1.chunker import (
    DEFAULT_FOLLOWING_BLOCKS,
    DEFAULT_MAX_WORDS,
    DEFAULT_MIN_WORDS,
    DEFAULT_TARGET_WORDS,
    ChunkPlanner,
)
from pact_v4.phase1.models import ChunkPlan

SNAPSHOT_HASH = hashlib.sha256(b"test-snapshot").hexdigest()

# ChunkPlan.MIN_WORDS/MAX_WORDS (models.py) are fixed hard bounds (280/640)
# independent of whatever ChunkPlanner instance is used, so every test that
# actually builds ChunkPlans (via planner.plan()) must stay compatible with
# that fixed range. 20 words/block puts the Gate default profile
# (min=280, target=450, max=640) at 14-32 blocks/chunk, matching the Gate
# note's observed 16-32 PID/chunk (mean 25.21).
WORDS_PER_BLOCK = 20
MIN_WORDS = DEFAULT_MIN_WORDS  # 280 -> 14 blocks
TARGET_WORDS = DEFAULT_TARGET_WORDS  # 450 -> 22.5 blocks
MAX_WORDS = DEFAULT_MAX_WORDS  # 640 -> 32 blocks


def _make_block(pid: str, role: str = "paragraph", index: int = 0, word_count: int = WORDS_PER_BLOCK) -> SourceBlock:
    return SourceBlock(
        pid=pid,
        index=index,
        tag="p",
        text=f"Block {pid}",
        html=f"<p>Block {pid}</p>",
        structural_role=role,
        inline_spans=(),
        word_count=word_count,
    )


def _make_blocks(count: int, role: str = "paragraph", start: int = 1, word_count: int = WORDS_PER_BLOCK) -> list[SourceBlock]:
    return [
        _make_block(f"p{i:05d}", role=role, index=i - 1, word_count=word_count)
        for i in range(start, start + count)
    ]


def _plan(planner: ChunkPlanner, blocks, **kwargs):
    return planner.plan(blocks, snapshot_hash=SNAPSHOT_HASH, **kwargs)


def _words(chunk: ChunkPlan) -> int:
    return chunk.total_words


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def planner():
    return ChunkPlanner()


# ---------------------------------------------------------------------------
# Defaults (Phase 0C Gate initial/default small chunk profile)
# ---------------------------------------------------------------------------

def test_default_profile_matches_gate_policy():
    assert DEFAULT_TARGET_WORDS == 450
    assert DEFAULT_MIN_WORDS == 280
    assert DEFAULT_MAX_WORDS == 640
    assert DEFAULT_FOLLOWING_BLOCKS == 0

    planner = ChunkPlanner()
    assert planner.target_words == 450
    assert planner.min_words == 280
    assert planner.max_words == 640


def test_right_context_off_by_default(planner):
    """following_blocks defaults to 0: right context needs an explicit override."""
    blocks = _make_blocks(60)
    result = _plan(planner, blocks)
    for chunk in result:
        assert chunk.context.right_en == ()


def test_right_context_accepted_but_not_activated_without_override(planner):
    """The planner accepts a following_blocks kwarg but does nothing with it at 0."""
    blocks = _make_blocks(60)
    result_default = _plan(planner, blocks)
    result_explicit_zero = _plan(planner, blocks, following_blocks=0)
    assert [c.context.right_en for c in result_default] == [c.context.right_en for c in result_explicit_zero]
    for chunk in result_default:
        assert chunk.context.right_en == ()


# ---------------------------------------------------------------------------
# Empty / edge cases
# ---------------------------------------------------------------------------

def test_empty_blocks_returns_empty_list(planner):
    assert _plan(planner, []) == []


def test_single_chunk_below_min_words(planner):
    blocks = _make_blocks(5)  # 100 words < 280
    result = _plan(planner, blocks)
    assert len(result) == 1
    assert result[0].pids == ("p00001", "p00002", "p00003", "p00004", "p00005")
    assert result[0].undersized_exception is True


def test_exactly_min_words(planner):
    blocks = _make_blocks(14)  # 280 words == MIN_WORDS
    result = _plan(planner, blocks)
    assert len(result) == 1
    assert _words(result[0]) == 280
    assert result[0].undersized_exception is False


def test_exactly_max_words(planner):
    blocks = _make_blocks(32)  # 640 words == MAX_WORDS
    result = _plan(planner, blocks)
    assert len(result) == 1
    assert _words(result[0]) == 640


# ---------------------------------------------------------------------------
# Full PID coverage — no gaps, no duplicates, no extras
# ---------------------------------------------------------------------------

def test_full_pid_coverage(planner):
    blocks = _make_blocks(90)
    result = _plan(planner, blocks)

    all_pids = []
    for chunk in result:
        all_pids.extend(chunk.pids)

    expected = [b.pid for b in blocks]
    assert all_pids == expected


def test_no_duplicate_pids_across_chunks(planner):
    blocks = _make_blocks(90)
    result = _plan(planner, blocks)

    seen = set()
    for chunk in result:
        for pid in chunk.pids:
            assert pid not in seen, f"Duplicate PID {pid}"
            seen.add(pid)

    assert len(seen) == 90


# ---------------------------------------------------------------------------
# Chunk size bounds — max is hard, min is soft (flagged, never silently violated)
# ---------------------------------------------------------------------------

def test_no_chunk_exceeds_max_words(planner):
    blocks = _make_blocks(200)
    result = _plan(planner, blocks)

    for chunk in result:
        assert _words(chunk) <= MAX_WORDS


def test_chunks_are_at_least_min_words_except_possible_last(planner):
    blocks = _make_blocks(200)
    result = _plan(planner, blocks)

    for i, chunk in enumerate(result):
        if i < len(result) - 1:
            assert _words(chunk) >= MIN_WORDS
            assert chunk.undersized_exception is False


def test_undersized_exception_set_exactly_when_below_soft_min(planner):
    for count in range(1, 70):
        blocks = _make_blocks(count)
        result = _plan(planner, blocks)
        for chunk in result:
            assert chunk.undersized_exception == (_words(chunk) < MIN_WORDS), (
                f"count={count} chunk={chunk.chunk_id} words={_words(chunk)} "
                f"flag={chunk.undersized_exception}"
            )


# ---------------------------------------------------------------------------
# Natural break / dialogue boundaries
# ---------------------------------------------------------------------------

def test_dialogue_exchange_not_split(planner):
    blocks = (
        _make_blocks(14)
        + _make_blocks(10, role="dialogue", start=15)
        + _make_blocks(14, start=25)
    )
    result = _plan(planner, blocks)

    dialogue_pids = {b.pid for b in blocks if b.structural_role == "dialogue"}
    dialogue_chunks = [
        chunk for chunk in result
        if any(pid in chunk.pids for pid in dialogue_pids)
    ]
    assert len(dialogue_chunks) == 1


def test_break_between_non_dialogue_blocks(planner):
    blocks = _make_blocks(14) + _make_blocks(14, start=15)  # 560 words, under MAX_WORDS
    result = _plan(planner, blocks)
    assert len(result) == 1


def test_forced_break_at_hard_cap(planner):
    blocks = _make_blocks(40, role="dialogue")  # 800 words, one uninterrupted dialogue run
    result = _plan(planner, blocks)
    assert len(result) >= 2
    for chunk in result:
        assert _words(chunk) <= MAX_WORDS


# ---------------------------------------------------------------------------
# right_en context (source EN text of the next chunk's first N PIDs).
#
# left_ru is intentionally always empty at planning time: the previous
# chunk's translation doesn't exist yet (V4_MVP_SPEC_RU.md §3.3) — it is
# looked up by chunk ownership at generation time, not stored here.
# ---------------------------------------------------------------------------

def test_left_ru_always_empty_at_planning_time(planner):
    blocks = _make_blocks(60)
    result = _plan(planner, blocks, following_blocks=5)
    for chunk in result:
        assert chunk.context.left_ru == ""


def test_right_context_empty_for_last_chunk(planner):
    blocks = _make_blocks(60)
    result = _plan(planner, blocks, following_blocks=5)
    assert result[-1].context.right_en == ()


def test_right_context_is_source_text_of_next_chunk(planner):
    blocks = _make_blocks(60)
    result = _plan(planner, blocks, following_blocks=5)
    assert len(result) >= 2
    expected = tuple(f"Block {pid}" for pid in result[1].pids[:5])
    assert result[0].context.right_en == expected


def test_context_count_limit(planner):
    blocks = _make_blocks(60)
    result = _plan(planner, blocks, following_blocks=3)
    if len(result) >= 2:
        assert len(result[0].context.right_en) == 3


def test_zero_following_blocks_yields_empty(planner):
    blocks = _make_blocks(60)
    result = _plan(planner, blocks, following_blocks=0)
    for chunk in result:
        assert chunk.context.right_en == ()


# ---------------------------------------------------------------------------
# Small tail merging
# ---------------------------------------------------------------------------

def test_small_tail_merged(planner):
    blocks = _make_blocks(35)  # a naive greedy split would leave a small tail
    result = _plan(planner, blocks)
    all_pids = []
    for c in result:
        all_pids.extend(c.pids)
    assert all_pids == [b.pid for b in blocks]
    for chunk in result:
        assert _words(chunk) <= MAX_WORDS


def test_small_tail_alone_when_merge_exceeds_max(planner):
    blocks = _make_blocks(31) + _make_blocks(3, start=32)  # 620 + 60 = 680 > MAX_WORDS if merged
    result = _plan(planner, blocks)
    assert len(result[-1].pids) >= 1


# ---------------------------------------------------------------------------
# Custom min/max/target
# ---------------------------------------------------------------------------

def test_custom_min_max_target():
    # Stay within ChunkPlan's fixed hard bounds (280/640) so plan() can
    # actually construct chunks; this just exercises a tighter window.
    planner = ChunkPlanner(target_words=400, min_words=300, max_words=500)
    blocks = _make_blocks(60, word_count=25)
    result = _plan(planner, blocks)
    for i, chunk in enumerate(result):
        if i < len(result) - 1:
            assert _words(chunk) >= 300
        assert _words(chunk) <= 500


def test_invalid_min_max_target_raises():
    with pytest.raises(ValueError):
        ChunkPlanner(target_words=10, min_words=0, max_words=10)
    with pytest.raises(ValueError):
        ChunkPlanner(target_words=10, min_words=10, max_words=5)
    with pytest.raises(ValueError):
        # target below min_words
        ChunkPlanner(target_words=1, min_words=10, max_words=20)
    with pytest.raises(ValueError):
        # target above max_words
        ChunkPlanner(target_words=100, min_words=10, max_words=20)


# ---------------------------------------------------------------------------
# ChunkPlan model constraints (hard max never relaxed, soft min needs a flag)
# ---------------------------------------------------------------------------

def test_chunk_plan_above_hard_max_rejected_even_with_flag():
    with pytest.raises(ValueError, match="exceeds hard cap"):
        ChunkPlan(
            chunk_id="c1", snapshot_hash=SNAPSHOT_HASH,
            pids=("p1", "p2"), total_words=ChunkPlan.MAX_WORDS + 1,
            undersized_exception=True,
        )


def test_chunk_plan_below_soft_min_needs_flag():
    with pytest.raises(ValueError, match="below soft minimum"):
        ChunkPlan(chunk_id="c1", snapshot_hash=SNAPSHOT_HASH, pids=("p1", "p2"), total_words=1)
    # With the flag, it's accepted.
    plan = ChunkPlan(
        chunk_id="c1", snapshot_hash=SNAPSHOT_HASH, pids=("p1", "p2"), total_words=1,
        undersized_exception=True,
    )
    assert plan.pids == ("p1", "p2")


# ---------------------------------------------------------------------------
# Regression: window is not degenerate, tail is rebalanced
# ---------------------------------------------------------------------------

def test_break_at_exactly_min_words_is_reachable(planner):
    # Only one legal boundary, at 280 words (14 blocks): everything after it
    # is one uninterrupted dialogue exchange.
    blocks = _make_blocks(14) + _make_blocks(30, role="dialogue", start=15)
    result = _plan(planner, blocks)
    assert _words(result[0]) == 280
    assert len(result[0].pids) == 14


def test_strong_break_preferred_over_hard_cap(planner):
    """A heading inside the window wins over cutting at max_words."""
    blocks = (
        _make_blocks(17)  # 340 words
        + [_make_block("h00001", role="heading", index=17, word_count=WORDS_PER_BLOCK)]
        + _make_blocks(20, start=19)
    )
    result = _plan(planner, blocks)
    assert result[0].pids[-1] == "p00017"
    assert result[1].pids[0] == "h00001"


def test_tail_rebalanced_instead_of_tiny_chunk(planner):
    """Enough blocks must never produce a tail chunk far below min_words."""
    for count in range(33, 70):
        blocks = _make_blocks(count)
        result = _plan(planner, blocks)
        assert _words(result[-1]) >= MIN_WORDS, f"count={count} tail_words={_words(result[-1])}"
        for chunk in result:
            assert _words(chunk) <= MAX_WORDS


def test_no_pid_loss_after_rebalance(planner):
    for count in range(1, 90):
        blocks = _make_blocks(count)
        result = _plan(planner, blocks)
        seen = [pid for chunk in result for pid in chunk.pids]
        assert seen == [b.pid for b in blocks], f"count={count}"


def test_full_pid_ownership_holds_for_every_plan_size(planner):
    """Cross-check against Phase 1A's validate_full_pid_ownership."""
    from pact_v4.phase1.models import Snapshot, validate_full_pid_ownership

    for count in (1, 5, 14, 32, 33, 70, 121):
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
