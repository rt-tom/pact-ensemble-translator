"""Phase 3B (part 1): assembled-chapter artifact.

Canonical source: docs/architecture/V4_FINAL_REVIEW_AND_IMPLEMENTATION_PLAN_RU_v2.md
("### 3B. One full audit") and docs/architecture/V4_MVP_SPEC_RU.md, §2 Step 6
("Assembled-chapter audit (один раз, по собранной главе)").

No "assembled chapter" artifact exists anywhere else in the codebase yet:
Phase 2C (``pact_v4.phase2.cascade``) only produces a per-chunk
``SelectionResult`` naming a winning ``candidate_id`` — nothing concatenates
the winners across a whole chapter. This module builds exactly that, as the
required input to the Step 6 audit implemented in
``pact_v4.phase3.audit``.

``AssembledChapter.chapter_hash`` is the "неподвижность финального результата
(frozen hash)" value referenced by Step 8 (final integrity check) — it is
recomputed from content, never caller-supplied, so an audit or terminal-state
transition can detect if the assembled text changed underneath it.

Out of scope here: any model call, finding production (``pact_v4.phase3.audit``),
repair, or terminal-state transition (Phase 4).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Tuple

from pact_v4.phase1.models import (
    Candidate,
    ChunkPlanArtifact,
    ConfigArtifact,
    Snapshot,
    SourceArtifact,
    canonical_json_hash,
)

__all__ = ["AssembledChapter"]


@dataclass(frozen=True)
class AssembledChapter:
    """The full chapter translation, assembled from Phase 2C's winning candidates.

    ``translation`` is the concatenation of every chunk's winning candidate's
    PID-map, in ``chunk_plan`` order — i.e. exactly the chapter's PID order,
    since ``ChunkPlanArtifact`` already guarantees every snapshot PID is
    owned by exactly one chunk (``validate_full_pid_ownership``). Full PID
    coverage is therefore a structural consequence of successful assembly,
    not a separate check.
    """

    source_hash: str
    snapshot_hash: str
    chunk_plan_hash: str
    config_identity: str
    translation: Tuple[Tuple[str, str], ...]
    chapter_hash: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "chapter_hash",
            canonical_json_hash({
                "artifact": "pact-v4-assembled-chapter/v1",
                "source_hash": self.source_hash,
                "snapshot_hash": self.snapshot_hash,
                "chunk_plan_hash": self.chunk_plan_hash,
                "config_identity": self.config_identity,
                "translation": [list(item) for item in self.translation],
            }),
        )

    def as_pid_map(self) -> Mapping[str, str]:
        return dict(self.translation)

    @classmethod
    def assemble(
        cls,
        *,
        source: SourceArtifact,
        snapshot: Snapshot,
        chunk_plan: ChunkPlanArtifact,
        config: ConfigArtifact,
        candidates: Mapping[str, Candidate],
    ) -> "AssembledChapter":
        """Concatenate one winning ``Candidate`` per chunk into a chapter.

        ``candidates`` maps ``chunk_id -> the winning Candidate for that
        chunk`` (the caller resolves Phase 2C's ``SelectionResult`` to the
        actual candidate object; this module knows nothing about selection).
        A chunk with no entry in ``candidates`` raises — there is no
        partial/best-effort assembly. Every candidate is re-validated against
        the same ``source``/``snapshot``/``chunk_plan``/``config`` (never
        trusted on identity alone), so a candidate belonging to a foreign
        snapshot/config cannot silently enter the assembled chapter.
        """
        translation: list = []
        for chunk in chunk_plan.chunks:
            candidate = candidates.get(chunk.chunk_id)
            if candidate is None:
                raise ValueError(
                    f"AssembledChapter: no winning candidate supplied for "
                    f"chunk {chunk.chunk_id!r}"
                )
            if candidate.chunk_id != chunk.chunk_id:
                raise ValueError(
                    f"AssembledChapter: candidate {candidate.candidate_id} "
                    f"targets chunk {candidate.chunk_id!r}, expected "
                    f"{chunk.chunk_id!r}"
                )
            candidate.validate_against(
                source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config
            )
            translation.extend(candidate.translation)

        return cls(
            source_hash=source.source_hash,
            snapshot_hash=snapshot.snapshot_hash,
            chunk_plan_hash=chunk_plan.plan_hash,
            config_identity=config.config_identity,
            translation=tuple(translation),
        )
