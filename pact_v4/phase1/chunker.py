from __future__ import annotations

from typing import List, Sequence

from pact_v4.phase0b.source_html import SourceBlock
from pact_v4.phase1.models import ChunkPlan


class ChunkPlanner:
    """Structure-aware chunk planner.

    Splits a chapter (sequence of SourceBlocks) into ChunkPlans.
    Deterministic, no model calls.

    Rules:
      - Target window: min_size–max_size PID per chunk (default 8–20).
      - Chunk boundaries only at natural breaks (paragraph, scene, non-dialogue).
      - Never split inside a dialogue exchange.
      - Hard upper cap at max_size — never exceeded.
      - Each PID belongs to exactly one chunk.
      - left_context = PIDs from previous chunk (read-only, empty for chunk 0).
      - right_context = PIDs from next chunk (read-only, not translated here).
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
        context_left_count: int = 0,
        context_right_count: int = 0,
    ) -> List[ChunkPlan]:
        if not blocks:
            return []

        chunk_pid_lists = self._partition(blocks)

        chunks: List[ChunkPlan] = []
        for i, pids in enumerate(chunk_pid_lists):
            chunk_id = f"chunk{i+1:04d}"

            left_ctx: List[str] = []
            if i > 0 and context_left_count > 0:
                left_ctx = chunk_pid_lists[i - 1][-context_left_count:]

            right_ctx: List[str] = []
            if i < len(chunk_pid_lists) - 1 and context_right_count > 0:
                right_ctx = chunk_pid_lists[i + 1][:context_right_count]

            chunks.append(ChunkPlan(
                chunk_id=chunk_id,
                pids=pids,
                left_context=left_ctx,
                right_context=right_ctx,
            ))

        return chunks

    def _partition(self, blocks: Sequence[SourceBlock]) -> List[List[str]]:
        if not blocks:
            return []

        chunks: List[List[str]] = []
        start = 0

        while start < len(blocks):
            remaining = len(blocks) - start
            take = min(remaining, self.max_size)

            if remaining > self.min_size:
                break_at = self._find_break(blocks, start, take)
                take = break_at - start

            chunk_pids = [blocks[i].pid for i in range(start, start + take)]
            chunks.append(chunk_pids)
            start += take

        self._merge_small_tail(chunks)
        return chunks

    def _find_break(self, blocks: Sequence[SourceBlock], start: int, max_take: int) -> int:
        """Find the best cut position for this chunk.

        Returns end index (exclusive) in [start+min_size, start+max_size].
        """
        low = start + self.min_size
        high = start + max_take

        for i in range(high, low, -1):
            if i >= len(blocks):
                continue
            if self._is_natural_break(blocks, i):
                return i

        return high

    @staticmethod
    def _is_natural_break(blocks: Sequence[SourceBlock], index: int) -> bool:
        """True if there is a natural break before blocks[index].

        Never break inside a dialogue exchange
        (two consecutive dialogue blocks).
        """
        if index <= 0 or index >= len(blocks):
            return False

        prev = blocks[index - 1]
        curr = blocks[index]

        if prev.structural_role == "dialogue" and curr.structural_role == "dialogue":
            return False

        return True

    def _merge_small_tail(self, chunks: List[List[str]]) -> None:
        if len(chunks) < 2:
            return
        if len(chunks[-1]) >= self.min_size:
            return

        tail = chunks.pop()
        prev = chunks[-1]
        if len(prev) + len(tail) <= self.max_size:
            chunks[-1] = prev + tail
        else:
            chunks.append(tail)
