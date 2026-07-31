"""Phase 1C structure-aware chunk planner (V4_MVP_SPEC_RU.md §3.2-3.3).

Deterministic, no model calls. Splits a chapter's SourceBlocks into
ChunkPlans:

  - Sizing is word-based (docs/plans/V4_PHASE_0C_GATE_NOTE_RU.md §1,
    Phase 0C Gate): ``max_words`` is a hard ceiling, ``min_words`` a soft
    target (see ChunkPlan.undersized_exception). ``target_words`` bounds
    the accepted window (``min_words <= target_words <= max_words``) but,
    like the original PID-window planner, does not itself bias which
    natural break is chosen — among equally strong breaks the largest
    chunk (closest to ``max_words``) wins, same tie-break as before.
    Defaults are the Gate's initial/default small chunk profile
    (``target_words=450``, ``min_words=280``, ``max_words=640``) — a
    policy name, not a measured PID range: the same baseline profile
    actually produced 16-32 PIDs/chunk (mean 25.21).
  - Boundaries only at natural breaks (paragraph/scene, never inside a
    dialogue exchange).
  - Strong breaks (heading, change of structural_role) are preferred
    over a plain paragraph boundary, so the window doesn't degenerate
    into "always cut at the hard cap".
  - Each PID belongs to exactly one chunk (validate with
    ``validate_full_pid_ownership`` once a Snapshot exists).
  - ``right_en`` context is the actual EN source text of the next
    chunk's first ``following_blocks`` PIDs (available at planning time
    — deterministic). Initial/default is ``following_blocks=0`` (Gate
    §1: Track A found no measured FP-candidate-rate advantage for right
    context; this is not a prohibition, right context remains an
    admissible future option, enabled by passing ``following_blocks>0``
    explicitly). ``left_ru`` is intentionally left empty here: the
    previous chunk's translation doesn't exist yet at planning time: it
    is looked up by chunk ownership at generation time (Phase 2B), not
    baked into the static plan.
"""
from __future__ import annotations

import bisect
from typing import List, Optional, Sequence, Tuple

from pact_v4.phase0b.source_html import SourceBlock
from pact_v4.phase1.models import ChunkContext, ChunkPlan

# structural_role values that always open a new logical unit.
STRONG_BREAK_ROLES = frozenset({"heading"})

# Phase 0C Gate initial/default small chunk profile (policy names, not
# measured PID ranges — see docs/plans/V4_PHASE_0C_GATE_NOTE_RU.md §1).
DEFAULT_TARGET_WORDS = 450
DEFAULT_MIN_WORDS = 280
DEFAULT_MAX_WORDS = 640
DEFAULT_FOLLOWING_BLOCKS = 0


class ChunkPlanner:
    """Structure-aware, word-budgeted chunk planner.

    Rules:
      - Word window: min_words-max_words words per chunk (default
        280-640, Gate initial/default small profile). target_words
        (default 450) bounds the accepted window but does not itself
        bias break selection (same tie-break as the original PID
        planner: among equally strong breaks, the largest chunk wins).
      - Chunk boundaries only at natural breaks (paragraph, scene, non-dialogue).
      - Strong breaks (heading, change of structural_role) are preferred over
        plain ones, so the planner does not simply always cut at the hard cap.
      - Never split inside a dialogue exchange.
      - max_words is a hard ceiling, never exceeded, no exception. A
        single leaf block whose own word count already exceeds max_words
        cannot be split further, so no legal chunk can contain it: the
        planner raises ValueError rather than silently emitting an
        over-cap chunk (min_words/max_words must also stay within
        ChunkPlan's own [MIN_WORDS, MAX_WORDS] contractual bounds — the
        constructor rejects a wider/narrower configuration up front).
      - min_words is a soft target: chunks below it are flagged with
        ``undersized_exception=True`` (whole-chapter-shorter-than-min_words,
        or an unavoidable tail after rebalancing).
      - Each PID belongs to exactly one chunk.
      - right_en context = source EN text of the first ``following_blocks``
        PIDs of the next chunk (read-only, not translated here). Default 0:
        right context is an admissible option, not enabled by default.
    """

    def __init__(
        self,
        target_words: int = DEFAULT_TARGET_WORDS,
        min_words: int = DEFAULT_MIN_WORDS,
        max_words: int = DEFAULT_MAX_WORDS,
    ):
        if not 1 <= min_words <= target_words <= max_words:
            raise ValueError(
                "words must satisfy 1 <= min_words <= target_words <= max_words"
            )
        # min_words/max_words must stay within ChunkPlan's own contractual
        # bounds: a wider max_words would let the planner emit chunks that
        # ChunkPlan then unconditionally rejects (hard cap, no exception); a
        # narrower min_words would under-set undersized_exception and hit
        # the same rejection on the soft-minimum side.
        if min_words < ChunkPlan.MIN_WORDS or max_words > ChunkPlan.MAX_WORDS:
            raise ValueError(
                f"min_words/max_words must stay within ChunkPlan's contractual "
                f"bounds [{ChunkPlan.MIN_WORDS}, {ChunkPlan.MAX_WORDS}] "
                f"(got min_words={min_words}, max_words={max_words})"
            )
        self.target_words = target_words
        self.min_words = min_words
        self.max_words = max_words

    def plan(
        self,
        blocks: Sequence[SourceBlock],
        *,
        snapshot_hash: str,
        following_blocks: int = DEFAULT_FOLLOWING_BLOCKS,
    ) -> List[ChunkPlan]:
        if not blocks:
            return []

        cum = self._prefix_word_counts(blocks)
        ranges = self._partition(blocks, cum)
        chunk_pid_lists = [
            [blocks[i].pid for i in range(start, end)] for start, end in ranges
        ]
        chunk_text_lists = [
            [blocks[i].text for i in range(start, end)] for start, end in ranges
        ]

        chunks: List[ChunkPlan] = []
        for i, (start, end) in enumerate(ranges):
            chunk_id = f"chunk{i + 1:04d}"
            pids = chunk_pid_lists[i]
            word_counts = tuple(blocks[j].word_count for j in range(start, end))
            total_words = sum(word_counts)

            right_en: Tuple[str, ...] = ()
            if i < len(ranges) - 1 and following_blocks > 0:
                right_en = tuple(chunk_text_lists[i + 1][:following_blocks])

            chunks.append(ChunkPlan(
                chunk_id=chunk_id,
                snapshot_hash=snapshot_hash,
                pids=tuple(pids),
                word_counts=word_counts,
                context=ChunkContext(left_ru="", right_en=right_en),
                undersized_exception=total_words < self.min_words,
            ))

        return chunks

    # ------------------------------------------------------------------
    # Word-count bookkeeping
    # ------------------------------------------------------------------

    @staticmethod
    def _prefix_word_counts(blocks: Sequence[SourceBlock]) -> List[int]:
        """cum[i] = sum of word_count of blocks[0:i]; cum[0] == 0."""
        cum = [0] * (len(blocks) + 1)
        for i, block in enumerate(blocks):
            cum[i + 1] = cum[i] + block.word_count
        return cum

    @staticmethod
    def _max_reach(
        blocks: Sequence[SourceBlock], cum: Sequence[int], start: int, n: int, budget: int
    ) -> int:
        """Largest end in (start, n] with cum[end]-cum[start] <= budget.

        ``max_words`` is a hard ceiling with no exception (Phase 0C Gate
        policy): if the single next leaf block already exceeds ``budget``
        on its own, there is no legal chunk that can contain it (a leaf
        block cannot be split further) and this raises rather than
        silently emitting an over-cap chunk for ``ChunkPlan`` to reject
        later with a less informative error.
        """
        span = cum[start + 1] - cum[start]
        if span > budget:
            raise ValueError(
                f"ChunkPlanner: block {blocks[start].pid!r} has {span} words, "
                f"exceeding the hard cap max_words={budget}; a single leaf "
                f"block cannot be split, so no legal chunk can contain it"
            )
        point = bisect.bisect_right(cum, cum[start] + budget, start + 1, n + 1)
        return point - 1

    @staticmethod
    def _min_reach(cum: Sequence[int], start: int, high: int, budget: int) -> int:
        """Smallest end in [start+1, high] with cum[end]-cum[start] >= budget.

        Clamped into [start+1, high] so the caller always gets a usable
        (possibly degenerate) search window.
        """
        point = bisect.bisect_left(cum, cum[start] + budget, start + 1, high + 1)
        return min(max(point, start + 1), high)

    # ------------------------------------------------------------------
    # Partitioning
    # ------------------------------------------------------------------

    def _partition(
        self, blocks: Sequence[SourceBlock], cum: Sequence[int]
    ) -> List[Tuple[int, int]]:
        """Split blocks into [start, end) index ranges."""
        if not blocks:
            return []

        n = len(blocks)
        ranges: List[Tuple[int, int]] = []
        start = 0

        while start < n:
            remaining_words = cum[n] - cum[start]
            high = self._max_reach(blocks, cum, start, n, self.max_words)

            if remaining_words > self.min_words:
                end = self._find_break(blocks, cum, start, high)
            else:
                end = n

            ranges.append((start, end))
            start = end

        self._fix_small_tail(blocks, cum, ranges)
        return ranges

    def _find_break(
        self, blocks: Sequence[SourceBlock], cum: Sequence[int], start: int, high: int
    ) -> int:
        """Find the best cut position for this chunk.

        Returns end index (exclusive) in [low, high]. Strong breaks win
        over plain ones; among equals the largest chunk wins.
        """
        low = self._min_reach(cum, start, high, self.min_words)

        best = self._best_break(blocks, low, high)
        return best if best is not None else high

    @staticmethod
    def _is_natural_break(blocks: Sequence[SourceBlock], index: int) -> bool:
        """True if there is a natural break before blocks[index].

        Never break inside a dialogue exchange
        (two consecutive dialogue blocks).
        """
        if index <= 0 or index > len(blocks):
            return False
        if index == len(blocks):
            return True

        prev = blocks[index - 1]
        curr = blocks[index]

        if prev.structural_role == "dialogue" and curr.structural_role == "dialogue":
            return False

        return True

    @staticmethod
    def _break_score(blocks: Sequence[SourceBlock], index: int) -> int:
        """How strongly blocks[index] opens a new logical unit.

        2 - the next block is itself a section opener (heading);
        1 - the structural role changes across the boundary;
        0 - plain paragraph boundary.
        """
        if index <= 0 or index >= len(blocks):
            return 0

        prev = blocks[index - 1]
        curr = blocks[index]

        if curr.structural_role in STRONG_BREAK_ROLES:
            return 2
        if prev.structural_role in STRONG_BREAK_ROLES:
            return 1
        if prev.structural_role != curr.structural_role:
            return 1
        return 0

    def _best_break(
        self, blocks: Sequence[SourceBlock], low: int, high: int
    ) -> Optional[int]:
        """Best natural break in [low, high]; ties resolved to the largest chunk."""
        best: Optional[int] = None
        best_score = -1
        for i in range(high, low - 1, -1):
            if not self._is_natural_break(blocks, i):
                continue
            score = self._break_score(blocks, i)
            if score > best_score:
                best, best_score = i, score
                if score == 2:
                    break
        return best

    # ------------------------------------------------------------------
    # Small tail handling
    # ------------------------------------------------------------------

    def _fix_small_tail(
        self,
        blocks: Sequence[SourceBlock],
        cum: Sequence[int],
        ranges: List[Tuple[int, int]],
    ) -> None:
        """Ensure the last chunk is not below min_words when avoidable.

        Strategy: merge the tail into the previous chunk if it fits;
        otherwise rebalance the boundary between the last two chunks so
        both stay within [min_words, max_words].
        """
        if len(ranges) < 2:
            return

        prev_start, boundary = ranges[-2]
        _, end = ranges[-1]

        tail_words = cum[end] - cum[boundary]
        if tail_words >= self.min_words:
            return

        total_words = cum[end] - cum[prev_start]
        if total_words <= self.max_words:
            ranges.pop()
            ranges[-1] = (prev_start, end)
            return

        # Rebalance: both parts must be >= min_words and <= max_words.
        lowest = max(
            self._min_reach(cum, prev_start, end, self.min_words),
            self._left_bound_for_right_cap(cum, prev_start, end, self.max_words),
        )
        highest = min(
            self._max_reach(blocks, cum, prev_start, end, self.max_words),
            self._right_bound_for_left_cap(cum, prev_start, end, self.min_words),
        )
        if lowest > highest:
            return

        new_boundary = self._best_break(blocks, lowest, highest)
        if new_boundary is None:
            # Only dialogue boundaries available: a hard-cap split is preferred
            # over leaving an undersized tail.
            new_boundary = highest

        ranges[-2] = (prev_start, new_boundary)
        ranges[-1] = (new_boundary, end)

    @staticmethod
    def _left_bound_for_right_cap(cum: Sequence[int], prev_start: int, end: int, max_words: int) -> int:
        """Smallest boundary b with the right part (b, end] <= max_words."""
        point = bisect.bisect_left(cum, cum[end] - max_words, prev_start + 1, end)
        return min(max(point, prev_start + 1), end - 1)

    @staticmethod
    def _right_bound_for_left_cap(cum: Sequence[int], prev_start: int, end: int, min_words: int) -> int:
        """Largest boundary b with the right part (b, end] >= min_words."""
        point = bisect.bisect_right(cum, cum[end] - min_words, prev_start + 1, end) - 1
        return min(max(point, prev_start + 1), end - 1)
