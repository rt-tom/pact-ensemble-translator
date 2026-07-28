"""Phase 1C structure-aware chunk planner (V4_MVP_SPEC_RU.md §3.2-3.3).

Deterministic, no model calls. Splits a chapter's SourceBlocks into
ChunkPlans:

  - Target window 8-20 PIDs; 20 is a hard ceiling, 8 a soft target
    (V4_MVP_SPEC_RU.md §3.2 — see ChunkPlan.undersized_exception).
  - Boundaries only at natural breaks (paragraph/scene, never inside a
    dialogue exchange).
  - Strong breaks (heading, change of structural_role) are preferred
    over a plain paragraph boundary, so the window doesn't degenerate
    into "always cut at the hard cap".
  - Each PID belongs to exactly one chunk (validate with
    ``validate_full_pid_ownership`` once a Snapshot exists).
  - ``right_en`` context is the actual EN source text of the next
    chunk's first few PIDs (available at planning time — deterministic).
    ``left_ru`` is intentionally left empty here: the previous chunk's
    translation doesn't exist yet at planning time: it is looked up by
    chunk ownership at generation time (Phase 2B), not baked into the
    static plan.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from pact_v4.phase0b.source_html import SourceBlock
from pact_v4.phase1.models import ChunkContext, ChunkPlan

# structural_role values that always open a new logical unit.
STRONG_BREAK_ROLES = frozenset({"heading"})


class ChunkPlanner:
    """Structure-aware chunk planner.

    Rules:
      - Target window: min_size-max_size PIDs per chunk (default 8-20).
      - Chunk boundaries only at natural breaks (paragraph, scene, non-dialogue).
      - Strong breaks (heading, change of structural_role) are preferred over
        plain ones, so the planner does not simply always cut at the hard cap.
      - Never split inside a dialogue exchange.
      - max_size is a hard ceiling, never exceeded, no exception.
      - min_size is a soft target: chunks below it are flagged with
        ``undersized_exception=True`` (whole-chapter-shorter-than-min_size,
        or an unavoidable tail after rebalancing).
      - Each PID belongs to exactly one chunk.
      - right_en context = source EN text of the first ``context_right_count``
        PIDs of the next chunk (read-only, not translated here).
    """

    def __init__(self, min_size: int = 8, max_size: int = 20):
        if not 1 <= min_size <= max_size:
            raise ValueError("min_size must be between 1 and max_size")
        self.min_size = min_size
        self.max_size = max_size

    def plan(
        self,
        blocks: Sequence[SourceBlock],
        *,
        snapshot_hash: str,
        context_right_count: int = 0,
    ) -> List[ChunkPlan]:
        if not blocks:
            return []

        ranges = self._partition(blocks)
        chunk_pid_lists = [
            [blocks[i].pid for i in range(start, end)] for start, end in ranges
        ]
        chunk_text_lists = [
            [blocks[i].text for i in range(start, end)] for start, end in ranges
        ]

        chunks: List[ChunkPlan] = []
        for i, pids in enumerate(chunk_pid_lists):
            chunk_id = f"chunk{i + 1:04d}"

            right_en: Tuple[str, ...] = ()
            if i < len(chunk_pid_lists) - 1 and context_right_count > 0:
                right_en = tuple(chunk_text_lists[i + 1][:context_right_count])

            chunks.append(ChunkPlan(
                chunk_id=chunk_id,
                snapshot_hash=snapshot_hash,
                pids=tuple(pids),
                context=ChunkContext(left_ru="", right_en=right_en),
                undersized_exception=len(pids) < self.min_size,
            ))

        return chunks

    # ------------------------------------------------------------------
    # Partitioning
    # ------------------------------------------------------------------

    def _partition(self, blocks: Sequence[SourceBlock]) -> List[Tuple[int, int]]:
        """Split blocks into [start, end) index ranges."""
        if not blocks:
            return []

        ranges: List[Tuple[int, int]] = []
        start = 0

        while start < len(blocks):
            remaining = len(blocks) - start
            take = min(remaining, self.max_size)

            if remaining > self.min_size:
                take = self._find_break(blocks, start, take) - start

            ranges.append((start, start + take))
            start += take

        self._fix_small_tail(blocks, ranges)
        return ranges

    def _find_break(self, blocks: Sequence[SourceBlock], start: int, max_take: int) -> int:
        """Find the best cut position for this chunk.

        Returns end index (exclusive) in [start+min_size, start+max_size].
        Strong breaks win over plain ones; among equals the largest chunk wins.
        """
        low = start + self.min_size
        high = start + max_take

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
        self, blocks: Sequence[SourceBlock], ranges: List[Tuple[int, int]]
    ) -> None:
        """Ensure the last chunk is not below min_size when avoidable.

        Strategy: merge the tail into the previous chunk if it fits;
        otherwise rebalance the boundary between the last two chunks so
        both stay within [min_size, max_size].
        """
        if len(ranges) < 2:
            return

        prev_start, boundary = ranges[-2]
        _, end = ranges[-1]

        if end - boundary >= self.min_size:
            return

        total = end - prev_start
        if total <= self.max_size:
            ranges.pop()
            ranges[-1] = (prev_start, end)
            return

        # Rebalance: both parts must be >= min_size and <= max_size.
        lowest = max(prev_start + self.min_size, end - self.max_size)
        highest = min(prev_start + self.max_size, end - self.min_size)
        if lowest > highest:
            return

        new_boundary = self._best_break(blocks, lowest, highest)
        if new_boundary is None:
            # Only dialogue boundaries available: a hard-cap split is preferred
            # over leaving an undersized tail.
            new_boundary = highest

        ranges[-2] = (prev_start, new_boundary)
        ranges[-1] = (new_boundary, end)
