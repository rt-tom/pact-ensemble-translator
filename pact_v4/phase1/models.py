"""Phase 1A data contracts for Pact v4.

Canonical source: docs/architecture/V4_MVP_SPEC_RU.md and
docs/architecture/V4_FINAL_REVIEW_AND_IMPLEMENTATION_PLAN_RU_v2.md
(section "Исполнимые контракты до реализации").

Design rules enforced here, not just documented:

  * No scoring engine anywhere (V4_MVP_SPEC_RU.md #4: selection is a
    lexicographic cascade, not a scored ranking). ``Candidate`` therefore
    has no numeric score field.
  * Terminal states are exactly ``complete`` / ``accepted_degraded`` /
    ``failed`` (V4_MVP_SPEC_RU.md §7); all three are write-once (monotonic).
    ``quarantined`` is an *internal* repair state (V4_MVP_SPEC_RU.md §7:
    "quarantined — внутреннее состояние automatic repair/fallback, не
    user-facing terminal при valid PID-map"), so it is deliberately not a
    terminal state here.
  * Identity fields (hashes) are validated as hex-encoded content hashes,
    not merely "non-empty" — a malformed/foreign-looking identity is
    rejected at construction time.
  * Findings, candidates and repairs are immutable (frozen) records once
    built; only ``TerminalState`` is a mutable state machine by design.
"""
from __future__ import annotations

import json
import re
from dataclasses import InitVar, dataclass, field
from types import MappingProxyType
from typing import Any, Dict, Mapping, Tuple

HEX_HASH_RE = re.compile(r"^[a-f0-9]{64}$")

CANDIDATE_ROLES = frozenset({"fidelity_first", "balanced_literary", "synthesis"})
REPAIR_ACTIONS = frozenset({"region_edit", "full_sentence_rewrite"})
TERMINAL_STATES = frozenset({"complete", "accepted_degraded", "failed"})
NON_TERMINAL_STATES = frozenset({"pending", "in_progress", "quarantined"})
ALL_STATES = TERMINAL_STATES | NON_TERMINAL_STATES

# Non-terminal states (including the internal ``quarantined`` repair state)
# may advance to any terminal state or to another non-terminal state;
# terminal states have no outgoing transitions (write-once / monotonic).
_ALLOWED_TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    "pending": ("in_progress", "quarantined", "complete", "accepted_degraded", "failed"),
    "in_progress": ("quarantined", "complete", "accepted_degraded", "failed"),
    "quarantined": ("in_progress", "complete", "accepted_degraded", "failed"),
    "complete": (),
    "accepted_degraded": (),
    "failed": (),
}


def _require_hash(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not HEX_HASH_RE.match(value):
        raise ValueError(
            f"Foreign identity: {field_name}={value!r} is not a valid "
            f"sha256 content hash"
        )


def _require_nonempty_str(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_unique_pids(pids: Tuple[str, ...], owner: str) -> None:
    if len(pids) != len(set(pids)):
        raise ValueError(f"{owner}: duplicate PIDs are not allowed")
    if not pids:
        raise ValueError(f"{owner}: at least one PID is required")


def canonical_json_hash(value: Any) -> str:
    """Deterministic sha256 hex digest of a JSON-serialisable value.

    Used to derive artifact identities (e.g. ``Snapshot.snapshot_hash``)
    so identity is a function of content, not caller-supplied and
    possibly wrong/foreign data.
    """
    import hashlib

    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _freeze_json(value: Any) -> Any:
    """Detach identity inputs from caller mutation and make them read-only."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_json(item) for item in value)
    return value


def _plain_json(value: Any) -> Any:
    """Return ordinary JSON containers from the immutable in-memory view."""
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


def _validate_identity_context(
    source: "SourceArtifact",
    snapshot: "Snapshot",
    chunk_plan: "ChunkPlanArtifact",
) -> None:
    """Bind source and plan content to the same authoritative snapshot."""
    if source.chapter_id != snapshot.chapter_id:
        raise ValueError(
            f"Foreign identity: source chapter {source.chapter_id!r}, "
            f"expected {snapshot.chapter_id!r}"
        )
    source_pids = tuple(pid for pid, _ in source.source)
    if source_pids != snapshot.pids:
        raise ValueError(
            f"Foreign identity: source PID order {source_pids}, "
            f"expected {snapshot.pids}"
        )
    # A2 review fix: the snapshot binds the canonical source content hash,
    # so a source whose text changed (same PIDs) is a foreign identity and
    # must never be validated against a snapshot built from different text.
    if source.source_hash != snapshot.source_hash:
        raise ValueError(
            f"Foreign identity: source_hash={source.source_hash}, "
            f"expected {snapshot.source_hash}"
        )
    chunk_plan.validate_against(snapshot)


def _validate_expected_identities(owner: Any, expected: Mapping[str, str]) -> None:
    """Reject well-formed identities belonging to different content."""
    for field_name, expected_hash in expected.items():
        actual = getattr(owner, field_name)
        if actual != expected_hash:
            raise ValueError(
                f"Foreign identity: {field_name}={actual}, expected {expected_hash}"
            )


def _require_exact_keys(
    payload: Mapping[str, Any], expected: set[str], owner: str
) -> None:
    unexpected = set(payload) - expected
    missing = expected - set(payload)
    if unexpected or missing:
        raise ValueError(
            f"{owner} keys invalid; missing={sorted(missing)}, "
            f"unexpected={sorted(unexpected)}"
        )


@dataclass(frozen=True)
class SourceArtifact:
    """Canonical ordered source PID-map with a content-derived identity."""

    chapter_id: str
    source: Tuple[Tuple[str, str], ...]
    source_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_nonempty_str(self.chapter_id, "chapter_id")
        normalized = []
        for item in self.source:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise ValueError("SourceArtifact: source entries must be PID/text pairs")
            normalized.append((item[0], item[1]))
        source = tuple(normalized)
        object.__setattr__(self, "source", source)
        pids = tuple(pid for pid, _ in source)
        _require_unique_pids(pids, "SourceArtifact")
        for pid, text in source:
            _require_nonempty_str(pid, "source PID")
            if not isinstance(text, str):
                raise ValueError(f"SourceArtifact: PID {pid} text must be a string")
        object.__setattr__(self, "source_hash", canonical_json_hash({
            "artifact": "pact-v4-source/v1",
            "chapter_id": self.chapter_id,
            "source": [list(item) for item in self.source],
        }))

    def __hash__(self) -> int:
        return hash(self.source_hash)


@dataclass(frozen=True)
class ConfigArtifact:
    """Versioned model/runtime configuration with a content-derived identity."""

    version: str
    values: Mapping[str, Any]
    config_identity: str = field(init=False)

    def __post_init__(self) -> None:
        _require_nonempty_str(self.version, "config version")
        plain_values = dict(self.values)
        object.__setattr__(self, "config_identity", canonical_json_hash({
            "artifact": "pact-v4-config/v1",
            "version": self.version,
            "values": plain_values,
        }))
        object.__setattr__(self, "values", _freeze_json(plain_values))

    def __hash__(self) -> int:
        return hash(self.config_identity)


def validate_json_complete(json_str: str) -> dict:
    """Parse JSON and reject anything partial/truncated/invalid.

    This is the front door for artifact ingestion: callers should route
    untrusted JSON payloads through this before constructing any of the
    dataclasses below.
    """
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Reject partial or invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("Reject partial or invalid JSON: expected a JSON object")
    return data


@dataclass(frozen=True)
class Provenance:
    """Minimum provenance set per contract item 11.

    ``source_hash`` / ``chapter_snapshot_hash`` / ``chunk_plan_hash`` /
    ``prompt_bundle_hash`` are content hashes (sha256 hex) — a value that
    doesn't look like one is treated as a foreign/invalid identity and
    rejected.
    """

    source_hash: str
    chapter_snapshot_hash: str
    chunk_plan_hash: str
    prompt_bundle_hash: str
    config_identity: str
    code_version: str
    policy_versions: Dict[str, str] = field(default_factory=dict)
    model_config: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_hash(self.source_hash, "source_hash")
        _require_hash(self.chapter_snapshot_hash, "chapter_snapshot_hash")
        _require_hash(self.chunk_plan_hash, "chunk_plan_hash")
        _require_hash(self.prompt_bundle_hash, "prompt_bundle_hash")
        _require_hash(self.config_identity, "config_identity")
        _require_nonempty_str(self.code_version, "code_version")
        if not self.policy_versions:
            raise ValueError("policy_versions must record at least one versioned policy")

    def validate_against(
        self,
        *,
        source: SourceArtifact,
        snapshot: "Snapshot",
        chunk_plan: "ChunkPlanArtifact",
        prompt_bundle: Any,
        config: ConfigArtifact,
    ) -> None:
        """Reject well-formed hashes that belong to foreign artifacts."""
        _validate_identity_context(source, snapshot, chunk_plan)
        _validate_expected_identities(self, {
            "source_hash": source.source_hash,
            "chapter_snapshot_hash": snapshot.snapshot_hash,
            "chunk_plan_hash": chunk_plan.plan_hash,
            "prompt_bundle_hash": canonical_json_hash(prompt_bundle),
            "config_identity": config.config_identity,
        })
        if canonical_json_hash(self.model_config) != canonical_json_hash(
            _plain_json(config.values)
        ):
            raise ValueError("Foreign identity: model_config does not match config artifact")


@dataclass(frozen=True)
class Snapshot:
    """Frozen per-chapter book-context snapshot (V4_MVP_SPEC_RU.md §6).

    Identity (``snapshot_hash``) is derived from content, not supplied by
    the caller, so two snapshots with the same inputs are guaranteed to
    have the same identity and a tampered/foreign snapshot cannot claim an
    arbitrary hash.

    A2 review fix (RV, commit 4ab250b): the snapshot identity now ALSO
    binds the canonical ``source_hash`` (the exact source text/PID map the
    translation is produced from) and the per-chapter bible record
    (``chapter_index_hash`` — the canonical hash of the selected
    ``chapter_index`` record, or of ``{}`` when no index exists). Without
    these, two SourceArtifacts with the same PIDs but different text (or a
    changed ``chapter_index.json``, which changes the bible prompt) shared
    the same snapshot identity, so resume could silently replay a stale
    translation against changed source/prompt content. Both fields are
    validated hex content hashes and part of ``snapshot_hash``, so a
    source/index change fails closed on resume (journal/provenance compare
    ``snapshot_hash``).
    """

    chapter_id: str
    pids: Tuple[str, ...]
    context: str
    glossary_hash: str
    book_memory_hash: str
    chapter_memory_hash: str
    source_hash: str
    chapter_index_hash: str
    snapshot_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_nonempty_str(self.chapter_id, "chapter_id")
        _require_unique_pids(self.pids, "Snapshot")
        for name in (
            "glossary_hash",
            "book_memory_hash",
            "chapter_memory_hash",
            "source_hash",
            "chapter_index_hash",
        ):
            _require_hash(getattr(self, name), name)
        computed = canonical_json_hash({
            "chapter_id": self.chapter_id,
            "pids": list(self.pids),
            "context": self.context,
            "glossary_hash": self.glossary_hash,
            "book_memory_hash": self.book_memory_hash,
            "chapter_memory_hash": self.chapter_memory_hash,
            "source_hash": self.source_hash,
            "chapter_index_hash": self.chapter_index_hash,
        })
        object.__setattr__(self, "snapshot_hash", computed)


@dataclass(frozen=True)
class ChunkContext:
    """Read-only neighbouring context for a chunk (V4_MVP_SPEC_RU.md §3.3)."""

    left_ru: str = ""
    right_en: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ChunkPlan:
    """Structure-aware chunk boundary (contract item 7).

    Phase 0C Gate (docs/plans/V4_PHASE_0C_GATE_NOTE_RU.md §1) fixes the
    hard/soft window in **words**, not PIDs: baseline grid labels like
    ``8_12``/``12_20`` were never real PID ranges — Track A's small
    profile actually produced 16-32 PIDs/chunk (mean 25.21). The upper
    bound (``MAX_WORDS``) is a hard ceiling, always enforced, with no
    exception — including for a single oversized leaf PID whose own word
    count already exceeds ``MAX_WORDS``: the planner (chunker.py) rejects
    that at planning time rather than ever constructing such a
    ``ChunkPlan``, since a leaf block cannot be split further and no
    ``undersized_exception``-style flag relaxes this bound. The lower
    bound (``MIN_WORDS``) is a *soft* target: it can be legitimately
    missed (a whole chapter shorter than the minimum, or an unavoidable
    undersized tail after rebalancing) — such cases must set
    ``undersized_exception=True`` to document why the soft minimum was not
    met; the flag never relaxes the hard maximum.

    ``snapshot_hash`` ties the plan back to the frozen snapshot it was
    computed from. ``word_counts`` is the per-PID source word count,
    positionally aligned with ``pids`` (one entry per PID, same order) —
    supplied by the caller (the chunk planner) since ``ChunkPlan`` itself
    only stores PIDs, not source text. ``total_words`` is *derived* from
    ``word_counts`` (not caller-supplied) so the hard cap below is checked
    against a value structurally tied to the chunk's actual PID count,
    not an arbitrary unrelated integer a caller could pass to bypass it.
    """

    chunk_id: str
    snapshot_hash: str
    pids: Tuple[str, ...]
    word_counts: Tuple[int, ...]
    context: ChunkContext = field(default_factory=ChunkContext)
    undersized_exception: bool = False
    total_words: int = field(init=False)

    MIN_WORDS = 280
    MAX_WORDS = 640

    def __post_init__(self) -> None:
        _require_nonempty_str(self.chunk_id, "chunk_id")
        _require_hash(self.snapshot_hash, "snapshot_hash")
        _require_unique_pids(self.pids, "ChunkPlan")
        if len(self.word_counts) != len(self.pids):
            raise ValueError(
                f"ChunkPlan {self.chunk_id}: word_counts has {len(self.word_counts)} "
                f"entries, expected exactly one per PID ({len(self.pids)})"
            )
        if any(not isinstance(w, int) or isinstance(w, bool) or w < 0 for w in self.word_counts):
            raise ValueError(f"ChunkPlan {self.chunk_id}: word_counts must be non-negative integers")
        total = sum(self.word_counts)
        object.__setattr__(self, "total_words", total)
        if total > self.MAX_WORDS:
            raise ValueError(
                f"ChunkPlan {self.chunk_id}: {total} words exceeds hard cap "
                f"{self.MAX_WORDS} (no exception relaxes the upper bound)"
            )
        if total < self.MIN_WORDS and not self.undersized_exception:
            raise ValueError(
                f"ChunkPlan {self.chunk_id}: {total} words below soft minimum "
                f"{self.MIN_WORDS} (set undersized_exception=True if this is a "
                f"documented, unavoidable case)"
            )


@dataclass(frozen=True)
class WholeChapterPidMap:
    """Full ordered PID map of a whole chapter, derived from the ChunkPlanArtifact.

    Deliberately NOT a ``ChunkPlan``: the whole chapter exceeds
    ``ChunkPlan.MAX_WORDS`` (640 hard cap enforced in ``__post_init__``),
    so whole-chapter generation works with this derived contract instead of
    a synthetic one-chunk plan. Derivation is authoritative: the map's PIDs
    are exactly the chapter's full source-order PID list (``snapshot.pids``),
    which ``ChunkPlanArtifact`` guarantees is fully owned across its chunks.

    The identity (``map_hash``) is content-derived from the PID list plus the
    snapshot/plan hashes it was derived from, so a change in any of them
    invalidates the map identity exactly like the source artifacts.
    """

    pids: Tuple[str, ...]
    snapshot_hash: str
    chunk_plan_hash: str
    map_hash: str = field(init=False)

    @classmethod
    def derive(
        cls, chunk_plan: "ChunkPlanArtifact", snapshot: "Snapshot"
    ) -> "WholeChapterPidMap":
        """Derive the full ordered PID map from an authoritative chunk plan.

        The plan's chunks are source-ordered (the planner emits them in
        source order), so concatenating each chunk's PIDs in chunk order
        yields the chapter's full source-order PID list. Validation is
        defensive: the derived list must equal ``snapshot.pids`` exactly.
        """
        pids = tuple(pid for chunk in chunk_plan.chunks for pid in chunk.pids)
        if pids != snapshot.pids:
            raise ValueError(
                "WholeChapterPidMap: derived PID order "
                f"{pids} does not match snapshot source order {snapshot.pids}"
            )
        return cls(
            pids=pids,
            snapshot_hash=snapshot.snapshot_hash,
            chunk_plan_hash=chunk_plan.plan_hash,
        )

    def __post_init__(self) -> None:
        _require_unique_pids(self.pids, "WholeChapterPidMap")
        _require_hash(self.snapshot_hash, "snapshot_hash")
        _require_hash(self.chunk_plan_hash, "chunk_plan_hash")
        object.__setattr__(
            self,
            "map_hash",
            canonical_json_hash(
                {
                    "artifact": "pact-v4-whole-chapter-pid-map/v1",
                    "snapshot_hash": self.snapshot_hash,
                    "chunk_plan_hash": self.chunk_plan_hash,
                    "pids": list(self.pids),
                }
            ),
        )

    def validate_against(self, snapshot: "Snapshot") -> None:
        """Bind the map to the authoritative snapshot (defense in depth)."""
        if self.snapshot_hash != snapshot.snapshot_hash:
            raise ValueError(
                f"Foreign identity: snapshot_hash={self.snapshot_hash}, "
                f"expected {snapshot.snapshot_hash}"
            )
        if self.pids != snapshot.pids:
            raise ValueError(
                f"WholeChapterPidMap: PID order {self.pids} "
                f"does not match snapshot source order {snapshot.pids}"
            )


def validate_full_pid_ownership(plans: Tuple[ChunkPlan, ...], snapshot: Snapshot) -> None:
    """Cross-chunk invariant: every snapshot PID is owned by exactly one chunk.

    V4_MVP_SPEC_RU.md §3.3: "владение = каждый PID переводится ровно одним
    chunk'ом (без двойного перевода)". This cannot be checked by a single
    ChunkPlan in isolation, hence a module-level validator over the whole
    plan set.
    """
    seen: Dict[str, str] = {}
    for plan in plans:
        if plan.snapshot_hash != snapshot.snapshot_hash:
            raise ValueError(
                f"ChunkPlan {plan.chunk_id} references foreign snapshot "
                f"{plan.snapshot_hash}, expected {snapshot.snapshot_hash}"
            )
        for pid in plan.pids:
            if pid in seen:
                raise ValueError(
                    f"PID {pid} owned by both {seen[pid]} and {plan.chunk_id}"
                )
            seen[pid] = plan.chunk_id
    missing = set(snapshot.pids) - set(seen)
    if missing:
        raise ValueError(f"PIDs missing chunk ownership: {sorted(missing)}")
    extra = set(seen) - set(snapshot.pids)
    if extra:
        raise ValueError(f"Chunk plans own PIDs outside the snapshot: {sorted(extra)}")


# V4.1 audit W (§14): in whole-chapter mode the real chunk boundaries are
# NOT used by generation (only the ordered PID map, ``WholeChapterPidMap``,
# matters), so the persisted chunk_plan.json is explicitly annotated instead
# of silently looking like an active chunking contract. The annotation is
# payload-level metadata only — it never participates in ``plan_hash``, and
# ``from_payload`` validates the values when present.
CHUNK_PLAN_MODE_WHOLE_CHAPTER = "whole-chapter-derived"
CHUNK_PLAN_NOTE_WHOLE_CHAPTER = "chunk boundaries not used"


@dataclass(frozen=True)
class ChunkPlanArtifact:
    """Authoritative chapter-level plan with a content-derived identity.

    ``create`` and ``from_payload`` are the supported construction paths.
    Both require the authoritative Snapshot, so ownership cannot be bypassed
    by calling the dataclass constructor or ``dataclasses.replace``.
    """

    chunks: Tuple[ChunkPlan, ...]
    snapshot: InitVar[Snapshot]
    snapshot_hash: str = field(init=False)
    plan_hash: str = field(init=False)

    @classmethod
    def create(cls, snapshot: Snapshot, chunks: Tuple[ChunkPlan, ...]) -> "ChunkPlanArtifact":
        return cls(chunks=tuple(chunks), snapshot=snapshot)

    def __post_init__(self, snapshot: Snapshot) -> None:
        if not isinstance(snapshot, Snapshot):
            raise TypeError("ChunkPlanArtifact requires an authoritative Snapshot")
        if not self.chunks:
            raise ValueError("ChunkPlanArtifact: at least one chunk is required")
        chunk_ids = tuple(chunk.chunk_id for chunk in self.chunks)
        if len(chunk_ids) != len(set(chunk_ids)):
            raise ValueError("ChunkPlanArtifact: duplicate chunk IDs are not allowed")
        for chunk in self.chunks:
            if chunk.snapshot_hash != snapshot.snapshot_hash:
                raise ValueError(
                    f"ChunkPlan {chunk.chunk_id} references foreign snapshot "
                    f"{chunk.snapshot_hash}, expected {snapshot.snapshot_hash}"
                )
        validate_full_pid_ownership(self.chunks, snapshot)
        object.__setattr__(self, "snapshot_hash", snapshot.snapshot_hash)
        object.__setattr__(self, "plan_hash", canonical_json_hash(self._identity_payload()))

    def _identity_payload(self) -> Dict[str, Any]:
        return {
            "artifact": "pact-v4-chunk-plan/v1",
            "snapshot_hash": self.snapshot_hash,
            "chunks": [
                {
                    "chunk_id": chunk.chunk_id,
                    "snapshot_hash": chunk.snapshot_hash,
                    "pids": list(chunk.pids),
                    "word_counts": list(chunk.word_counts),
                    "context": {
                        "left_ru": chunk.context.left_ru,
                        "right_en": list(chunk.context.right_en),
                    },
                    "undersized_exception": chunk.undersized_exception,
                }
                for chunk in self.chunks
            ],
        }

    def to_payload(self) -> Dict[str, Any]:
        """Single canonical persisted representation, including its digest."""
        payload = self._identity_payload()
        payload["plan_hash"] = self.plan_hash
        return payload

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any], *, snapshot: Snapshot
    ) -> "ChunkPlanArtifact":
        """Load, reconstruct, and re-hash a persisted authoritative plan.

        V4.1 audit W (§14): the payload may carry the optional whole-chapter
        annotation keys ``mode``/``note`` (written by the strict runner in
        whole-chapter mode to mark the plan as ``whole-chapter-derived``).
        They are metadata only — never part of the identity hash — but when
        present their values are validated so a foreign/typo'd annotation
        fails closed instead of round-tripping silently.
        """
        optional = {k for k in ("mode", "note") if k in payload}
        _require_exact_keys(
            payload,
            {"artifact", "snapshot_hash", "chunks", "plan_hash"} | optional,
            "ChunkPlanArtifact payload",
        )
        if payload.get("mode", CHUNK_PLAN_MODE_WHOLE_CHAPTER) != CHUNK_PLAN_MODE_WHOLE_CHAPTER:
            raise ValueError(
                f"Foreign identity: mode={payload.get('mode')!r}, expected "
                f"{CHUNK_PLAN_MODE_WHOLE_CHAPTER!r}"
            )
        if payload.get("note", CHUNK_PLAN_NOTE_WHOLE_CHAPTER) != CHUNK_PLAN_NOTE_WHOLE_CHAPTER:
            raise ValueError(
                f"Foreign identity: note={payload.get('note')!r}, expected "
                f"{CHUNK_PLAN_NOTE_WHOLE_CHAPTER!r}"
            )
        if payload["artifact"] != "pact-v4-chunk-plan/v1":
            raise ValueError(f"Foreign identity: artifact={payload['artifact']!r}")
        if payload["snapshot_hash"] != snapshot.snapshot_hash:
            raise ValueError(
                f"Foreign identity: snapshot_hash={payload['snapshot_hash']}, "
                f"expected {snapshot.snapshot_hash}"
            )
        chunks = []
        for index, item in enumerate(payload["chunks"]):
            _require_exact_keys(
                item,
                {"chunk_id", "snapshot_hash", "pids", "word_counts", "context", "undersized_exception"},
                f"ChunkPlanArtifact chunk[{index}]",
            )
            _require_exact_keys(
                item["context"],
                {"left_ru", "right_en"},
                f"ChunkPlanArtifact chunk[{index}].context",
            )
            chunks.append(ChunkPlan(
                chunk_id=item["chunk_id"],
                snapshot_hash=item["snapshot_hash"],
                pids=tuple(item["pids"]),
                word_counts=tuple(item["word_counts"]),
                context=ChunkContext(
                    left_ru=item["context"]["left_ru"],
                    right_en=tuple(item["context"]["right_en"]),
                ),
                undersized_exception=item["undersized_exception"],
            ))
        artifact = cls.create(snapshot, tuple(chunks))
        _require_hash(payload["plan_hash"], "plan_hash")
        if payload["plan_hash"] != artifact.plan_hash:
            raise ValueError(
                f"Foreign identity: plan_hash={payload['plan_hash']}, "
                f"expected {artifact.plan_hash}"
            )
        return artifact

    def chunk(self, chunk_id: str) -> ChunkPlan:
        for chunk in self.chunks:
            if chunk.chunk_id == chunk_id:
                return chunk
        raise ValueError(f"Foreign identity: chunk {chunk_id!r} is not in chunk plan")

    def validate_against(self, snapshot: Snapshot) -> None:
        """Bind a loaded plan to the authoritative snapshot and PID ownership."""
        if self.snapshot_hash != snapshot.snapshot_hash:
            raise ValueError(
                f"Foreign identity: snapshot_hash={self.snapshot_hash}, "
                f"expected {snapshot.snapshot_hash}"
            )
        validate_full_pid_ownership(self.chunks, snapshot)


@dataclass(frozen=True)
class GateResult:
    """One cascade-stage decision, kept as part of a candidate/repair trace."""

    gate: str
    passed: bool
    detail: str = ""

    def __post_init__(self) -> None:
        _require_nonempty_str(self.gate, "gate")


@dataclass(frozen=True)
class Candidate:
    """A/B/C generation candidate or repair output (contract item 1).

    No ``score`` field: V4_MVP_SPEC_RU.md #4 explicitly forbids a scoring
    engine. Selection is decided by ``decision_trace`` (the lexicographic
    cascade's pass/fail record), not by a number.
    """

    candidate_id: str
    chunk_id: str
    role: str
    translation: Tuple[Tuple[str, str], ...]
    source_hash: str
    snapshot_hash: str
    chunk_plan_hash: str
    config_identity: str
    decision_trace: Tuple[GateResult, ...] = ()

    def __post_init__(self) -> None:
        _require_nonempty_str(self.candidate_id, "candidate_id")
        _require_nonempty_str(self.chunk_id, "chunk_id")
        if self.role not in CANDIDATE_ROLES:
            raise ValueError(f"Unknown candidate role: {self.role!r}")
        pids = [pid for pid, _ in self.translation]
        _require_unique_pids(tuple(pids), f"Candidate {self.candidate_id}")
        for pid, text in self.translation:
            if not isinstance(text, str):
                raise ValueError(
                    f"Candidate {self.candidate_id}: PID {pid} "
                    "translation must be a string"
                )
        for name, value in (
            ("source_hash", self.source_hash),
            ("snapshot_hash", self.snapshot_hash),
            ("chunk_plan_hash", self.chunk_plan_hash),
        ):
            _require_hash(value, name)
        _require_hash(self.config_identity, "config_identity")

    @classmethod
    def create(
        cls,
        *,
        candidate_id: str,
        chunk_id: str,
        role: str,
        translation: Tuple[Tuple[str, str], ...],
        source: SourceArtifact,
        snapshot: Snapshot,
        chunk_plan: ChunkPlanArtifact,
        config: ConfigArtifact,
        decision_trace: Tuple[GateResult, ...] = (),
        whole_chapter_pid_map: Optional["WholeChapterPidMap"] = None,
    ) -> "Candidate":
        """Create a candidate whose identities cannot be caller-fabricated.

        ``whole_chapter_pid_map`` (V4.1 A1) switches the ownership check from
        the per-chunk plan lookup to the derived whole-chapter PID map: the
        candidate owns the entire chapter (chunk_id="whole_chapter"), whose
        ordered PID list is not a ``ChunkPlan`` (the whole chapter exceeds
        ``ChunkPlan.MAX_WORDS``). Only used by whole-chapter generation; the
        per-chunk path passes ``None`` and keeps the historical validation.
        """
        candidate = cls(
            candidate_id=candidate_id,
            chunk_id=chunk_id,
            role=role,
            translation=translation,
            source_hash=source.source_hash,
            snapshot_hash=snapshot.snapshot_hash,
            chunk_plan_hash=chunk_plan.plan_hash,
            config_identity=config.config_identity,
            decision_trace=decision_trace,
        )
        candidate.validate_against(
            source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config,
            whole_chapter_pid_map=whole_chapter_pid_map,
        )
        return candidate

    def validate_against(
        self,
        *,
        source: SourceArtifact,
        snapshot: Snapshot,
        chunk_plan: ChunkPlanArtifact,
        config: ConfigArtifact,
        whole_chapter_pid_map: Optional["WholeChapterPidMap"] = None,
    ) -> None:
        """Semantic identity validation required at persistence/cache boundaries."""
        _validate_identity_context(source, snapshot, chunk_plan)
        _validate_expected_identities(self, {
            "source_hash": source.source_hash,
            "snapshot_hash": snapshot.snapshot_hash,
            "chunk_plan_hash": chunk_plan.plan_hash,
            "config_identity": config.config_identity,
        })
        if whole_chapter_pid_map is not None:
            # Whole-chapter candidate: the derived full-chapter PID map is the
            # authoritative ownership contract (not a per-chunk plan lookup —
            # chunk_id="whole_chapter" is deliberately absent from the plan).
            if self.chunk_id != "whole_chapter":
                raise ValueError(
                    f"Candidate {self.candidate_id}: whole_chapter_pid_map given "
                    f"but chunk_id={self.chunk_id!r} != 'whole_chapter'"
                )
            whole_chapter_pid_map.validate_against(snapshot)
            if self.pid_order() != whole_chapter_pid_map.pids:
                raise ValueError(
                    f"Candidate {self.candidate_id} PID order {self.pid_order()} "
                    f"does not match whole-chapter map {whole_chapter_pid_map.pids}"
                )
            return
        validate_candidate_ownership(self, chunk_plan.chunk(self.chunk_id))

    def pid_order(self) -> Tuple[str, ...]:
        return tuple(pid for pid, _ in self.translation)

    def as_pid_map(self) -> Dict[str, str]:
        return dict(self.translation)


def validate_candidate_ownership(candidate: Candidate, chunk: ChunkPlan) -> None:
    """A candidate's PID-map must cover exactly the owning chunk's PIDs, in order."""
    if candidate.chunk_id != chunk.chunk_id:
        raise ValueError(
            f"Candidate {candidate.candidate_id} targets chunk {candidate.chunk_id}, "
            f"expected {chunk.chunk_id}"
        )
    if candidate.pid_order() != chunk.pids:
        raise ValueError(
            f"Candidate {candidate.candidate_id} PID order {candidate.pid_order()} "
            f"does not match chunk plan {chunk.pids}"
        )


@dataclass(frozen=True)
class Region:
    """A located span inside a single PID's text."""

    pid: str
    start: int
    end: int

    def __post_init__(self) -> None:
        _require_nonempty_str(self.pid, "pid")
        if self.start < 0 or self.end < self.start:
            raise ValueError(f"Region: invalid span [{self.start}, {self.end}) for {self.pid}")


@dataclass(frozen=True)
class Finding:
    """Immutable audit finding (contract item 6).

    Stable ``finding_id`` + ``detector``/``category``/``evidence``/``region``.
    Findings are never merged by span overlap — this type simply has no
    merge operation, by design.
    """

    finding_id: str
    detector: str
    category: str
    severity: str
    evidence: str
    region: Region

    def __post_init__(self) -> None:
        _require_nonempty_str(self.finding_id, "finding_id")
        _require_nonempty_str(self.detector, "detector")
        _require_nonempty_str(self.category, "category")
        _require_nonempty_str(self.severity, "severity")
        _require_nonempty_str(self.evidence, "evidence")


@dataclass(frozen=True)
class Repair:
    """Targeted region/PID repair (contract item 5, Phase 4A).

    Must reference at least one ``Finding`` (no free-floating repairs).
    ``full_sentence_rewrite`` requires a documented reason. ``auto_accepted``
    exists only to be rejected: a repair can never auto-accept a challenge,
    it must carry gate evidence in ``decision_trace``.
    """

    repair_id: str
    finding_ids: Tuple[str, ...]
    chunk_id: str
    action: str
    target_pids: Tuple[str, ...]
    instructions: str
    full_sentence_reason: str = ""
    decision_trace: Tuple[GateResult, ...] = ()
    auto_accepted: bool = False

    def __post_init__(self) -> None:
        _require_nonempty_str(self.repair_id, "repair_id")
        if not self.finding_ids:
            raise ValueError(f"Repair {self.repair_id}: must reference at least one finding")
        _require_nonempty_str(self.chunk_id, "chunk_id")
        if self.action not in REPAIR_ACTIONS:
            raise ValueError(f"Repair {self.repair_id}: unknown action {self.action!r}")
        if not self.target_pids:
            raise ValueError(f"Repair {self.repair_id}: must target at least one PID")
        if len(self.target_pids) != len(set(self.target_pids)):
            raise ValueError(f"Repair {self.repair_id}: duplicate target PIDs are not allowed")
        _require_nonempty_str(self.instructions, "instructions")
        if self.action == "full_sentence_rewrite" and not self.full_sentence_reason:
            raise ValueError(
                f"Repair {self.repair_id}: full_sentence_rewrite requires a documented reason "
                f"(local minimal repair must be shown impossible first)"
            )
        if self.auto_accepted:
            raise ValueError(
                f"Repair {self.repair_id}: cannot be auto-accepted; a challenge requires "
                f"gate evidence in decision_trace, never automatic acceptance"
            )


@dataclass
class TerminalState:
    """Write-once terminal state machine (contract item 9, V4_MVP_SPEC_RU.md §7).

    Terminal states are exactly ``complete`` / ``accepted_degraded`` /
    ``failed``. ``quarantined`` is an *internal* repair state (V4_MVP_SPEC_RU.md
    §7: "внутреннее состояние automatic repair/fallback, не user-facing
    terminal при valid PID-map"), so it is a non-terminal state that may still
    advance to a terminal state once repair resolves it. Once in a terminal
    state there are no outgoing transitions — this is enforced by
    ``_ALLOWED_TRANSITIONS`` mapping terminal states to an empty tuple, not by
    special-casing in ``transition_to``.
    """

    state_id: str
    status: str
    provenance: Provenance

    def __post_init__(self) -> None:
        _require_nonempty_str(self.state_id, "state_id")
        if self.status not in ALL_STATES:
            raise ValueError(f"Unknown status: {self.status!r}")

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATES

    def transition_to(self, new_status: str) -> None:
        if new_status not in ALL_STATES:
            raise ValueError(f"Unknown status: {new_status!r}")
        allowed = _ALLOWED_TRANSITIONS.get(self.status, ())
        if new_status not in allowed:
            raise ValueError(
                f"Non-monotonic terminal transition from {self.status} to {new_status}"
            )
        self.status = new_status
