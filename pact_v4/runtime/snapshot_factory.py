"""Adapters that turn raw chapter inputs into Phase 1A contract artifacts.

The Phase 1A library (``pact_v4.phase1.models``) is strict about
identities: every contract object is content-hashed, and identities
between objects must agree (e.g. ``Snapshot.pids`` must match
``SourceArtifact.source`` PID order, ``Provenance.chunk_plan_hash`` must
match the actual ``ChunkPlanArtifact.plan_hash``). This module wraps the
*only* set of constructors that produce these objects — ``SourceArtifact``,
``Snapshot``, ``ConfigArtifact``, ``ChunkPlanArtifact.create`` — and is
the place where raw chapter inputs (EN HTML, glossary/book-memory
JSON files) become v4 contract artifacts.

It does **not** load the EN HTML or run the chunk planner — that lives in
the driver module — but it does take a *list of* ``SourceBlock`` (already
parsed by ``pact_v4.phase0b.source_html.parse_source_html``) plus a frozen
glossary/book-memory snapshot and produce the
``SourceArtifact``/``Snapshot``/``ConfigArtifact`` triple that everything
downstream needs.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from pact_v4.phase0b.source_html import SourceBlock
from pact_v4.phase1.memory import MemoryManager
from pact_v4.phase1.models import (
    ConfigArtifact,
    Snapshot,
    SourceArtifact,
)


def _hash_canonical_json(value: Any) -> str:
    """Return the canonical JSON sha256 of a JSON-serialisable value.

    The Phase 1A ``Snapshot`` model validates that ``glossary_hash`` /
    ``book_memory_hash`` / ``chapter_memory_hash`` are valid sha256 hex
    digests; this helper is the one place where the actual digest is
    computed, so the rule "the hash is the canonical JSON hash of the
    memory contents" lives in exactly one place.
    """
    if value is None:
        value = {}
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _load_json(path: Path | str, default: Any = None) -> Any:
    path_obj = Path(path)
    if not path_obj.exists():
        return default if default is not None else {}
    with path_obj.open("r", encoding="utf-8") as file:
        return json.load(file)


@dataclass(frozen=True)
class ChapterMemory:
    """Glossary + book-memory state the driver hands into ``build_snapshot``.

    ``glossary`` and ``book_memory`` are stored as raw JSON-compatible
    structures (the same data the v3 production pipeline reads from
    ``glossary/established.json`` + ``book_memory.json``). The hashes the
    library cares about are computed from these contents at snapshot time,
    not from the file paths, so the memory identities move with the data
    even if the file location changes.
    """

    glossary: Any
    book_memory: Any
    chapter_memory: Any = None
    # V4.1 A2: deterministic per-chapter bible index (chapter_index.json,
    # built by pact_full_pipeline_runner_v1/build_chapter_index.py).
    # {chapter_id: {"characters": [...], "facts": [...], "address": [...]}}.
    chapter_index: Any = None
    # Optional provenance: where the memory was loaded from. Recorded into
    # the returned ``Snapshot.context`` for human-readable provenance but
    # not part of the identity (the identity is the contents).
    source_dir: Optional[Path] = None

    @classmethod
    def from_directory(
        cls,
        base_dir: Path | str,
        *,
        chapter_memory: Optional[Any] = None,
    ) -> "ChapterMemory":
        """Load glossary/book_memory from ``<base_dir>/glossary.json`` and
        ``<base_dir>/book_memory.json`` (the same on-disk format the v3
        production pipeline uses, via ``MemoryManager``'s file paths).

        ``chapter_index.json`` is loaded too when present (V4.1 A2); when
        absent it defaults to ``None`` and ``render_bible_section`` falls
        back to the legacy full-memory render.
        """
        base = Path(base_dir)
        manager = MemoryManager(str(base))
        return cls(
            glossary=_load_json(manager.glossary_path, {}),
            book_memory=_load_json(manager.book_memory_path, {}),
            chapter_memory=chapter_memory,
            chapter_index=_load_json(manager.chapter_index_path, None),
            source_dir=base,
        )


def build_source_artifact(
    *,
    chapter_id: str,
    blocks: Sequence[SourceBlock],
) -> SourceArtifact:
    """Wrap a list of parsed ``SourceBlock`` into the v4 ``SourceArtifact``.

    The artifact's identity is the canonical-JSON hash of
    ``(chapter_id, ordered PID->text pairs)`` so two different block lists
    for the same chapter (or two different orderings) cannot share a
    ``source_hash``.
    """
    if not blocks:
        raise ValueError("build_source_artifact: empty block list")
    pairs = tuple((block.pid, block.text) for block in blocks)
    return SourceArtifact(chapter_id=chapter_id, source=pairs)


def build_snapshot(
    *,
    chapter_id: str,
    source: SourceArtifact,
    memory: ChapterMemory,
    context: str = "",
) -> Snapshot:
    """Construct a Phase 1A ``Snapshot`` from a ``SourceArtifact`` and memory.

    The PIDs/order come from the ``SourceArtifact`` (single source of
    truth — the chunk planner later validates that every chunk's PID set
    is a partition of these). The memory hashes are derived from the
    *contents* the driver passes in here, never from file paths, so the
    snapshot identity moves with the data.

    A2 review fix (RV, commit 4ab250b): the snapshot identity now ALSO
    binds the canonical ``source_hash`` (``SourceArtifact.source_hash`` —
    the exact PID→text content the translation is produced from) and
    ``chapter_index_hash`` (the canonical hash of the SELECTED per-chapter
    bible record, or of ``{}`` when no ``chapter_index.json`` exists).
    Without this, a changed source text (same PIDs) or a changed
    ``chapter_index.json`` (which changes ``render_bible_section``/the
    bible prompt) left ``snapshot_hash`` unchanged, so resume could replay
    a stale translation against changed inputs. Both hashes flow into
    ``Snapshot.snapshot_hash``, and journal/generation provenance compare
    that hash on resume — so a source/index change fails closed.
    """
    pids = tuple(pid for pid, _ in source.source)
    glossary_hash = _hash_canonical_json(memory.glossary)
    book_memory_hash = _hash_canonical_json(memory.book_memory)
    chapter_memory_hash = _hash_canonical_json(
        memory.chapter_memory if memory.chapter_memory is not None else {}
    )
    # The bible prompt for THIS chapter is rendered from the selected
    # chapter_index record (render_bible_section(chapter_id, chapter_index,
    # book_memory) -> chapter_index[chapter_id]); the selected record is the
    # canonical identity input for this chapter, so changes to another
    # chapter's record do not invalidate this chapter's snapshot.
    selected_chapter_index = None
    if isinstance(memory.chapter_index, Mapping):
        selected_chapter_index = memory.chapter_index.get(chapter_id)
    chapter_index_hash = _hash_canonical_json(selected_chapter_index)
    return Snapshot(
        chapter_id=chapter_id,
        pids=pids,
        context=context,
        glossary_hash=glossary_hash,
        book_memory_hash=book_memory_hash,
        chapter_memory_hash=chapter_memory_hash,
        source_hash=source.source_hash,
        chapter_index_hash=chapter_index_hash,
    )


def build_config_artifact(
    *,
    version: str,
    values: Mapping[str, Any],
) -> ConfigArtifact:
    """Build the Phase 1A ``ConfigArtifact`` for a run.

    The ``version`` string is required by the contract and is part of
    the identity. The values dict is the run's runtime configuration:
    which model profiles, generation parameters (temperature/seed), and
    any reviewer/selector wiring flags the driver wants to record.
    """
    return ConfigArtifact(version=version, values=dict(values))
