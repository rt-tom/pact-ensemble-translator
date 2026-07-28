"""Phase 1A data contracts for Pact v4.

Canonical source: docs/architecture/V4_MVP_SPEC_RU.md and
docs/architecture/V4_FINAL_REVIEW_AND_IMPLEMENTATION_PLAN_RU_v2.md
(section "Исполнимые контракты до реализации").

Design rules enforced here, not just documented:

  * No scoring engine anywhere (V4_MVP_SPEC_RU.md #4: selection is a
    lexicographic cascade, not a scored ranking). ``Candidate`` therefore
    has no numeric score field.
  * Terminal states are exactly ``complete`` / ``quarantined`` / ``failed``
    (V4_MVP_SPEC_RU.md §7); all three are write-once (monotonic).
  * Identity fields (hashes) are validated as hex-encoded content hashes,
    not merely "non-empty" — a malformed/foreign-looking identity is
    rejected at construction time.
  * Findings, candidates and repairs are immutable (frozen) records once
    built; only ``TerminalState`` is a mutable state machine by design.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Dict, Mapping, Tuple

HEX_HASH_RE = re.compile(r"^[a-f0-9]{64}$")

CANDIDATE_ROLES = frozenset({"fidelity_first", "balanced_literary", "synthesis"})
REPAIR_ACTIONS = frozenset({"region_edit", "full_sentence_rewrite"})
TERMINAL_STATES = frozenset({"complete", "quarantined", "failed"})
NON_TERMINAL_STATES = frozenset({"pending", "in_progress"})
ALL_STATES = TERMINAL_STATES | NON_TERMINAL_STATES

# Non-terminal states may advance to any terminal state or to the other
# non-terminal state; terminal states have no outgoing transitions
# (write-once / monotonic).
_ALLOWED_TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    "pending": ("in_progress", "complete", "quarantined", "failed"),
    "in_progress": ("complete", "quarantined", "failed"),
    "complete": (),
    "quarantined": (),
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


@dataclass(frozen=True)
class SourceArtifact:
    """Canonical ordered source PID-map with a content-derived identity."""

    chapter_id: str
    source: Tuple[Tuple[str, str], ...]
    source_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_nonempty_str(self.chapter_id, "chapter_id")
        pids = tuple(pid for pid, _ in self.source)
        _require_unique_pids(pids, "SourceArtifact")
        for pid, text in self.source:
            _require_nonempty_str(pid, "source PID")
            if not isinstance(text, str):
                raise ValueError(f"SourceArtifact: PID {pid} text must be a string")
        object.__setattr__(self, "source_hash", canonical_json_hash({
            "artifact": "pact-v4-source/v1",
            "chapter_id": self.chapter_id,
            "source": [list(item) for item in self.source],
        }))


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
    code_version: str
    policy_versions: Dict[str, str] = field(default_factory=dict)
    model_config: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_hash(self.source_hash, "source_hash")
        _require_hash(self.chapter_snapshot_hash, "chapter_snapshot_hash")
        _require_hash(self.chunk_plan_hash, "chunk_plan_hash")
        _require_hash(self.prompt_bundle_hash, "prompt_bundle_hash")
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
        if source.chapter_id != snapshot.chapter_id:
            raise ValueError(
                f"Foreign identity: source chapter {source.chapter_id!r}, "
                f"expected {snapshot.chapter_id!r}"
            )
        chunk_plan.validate_against(snapshot)
        expected = {
            "source_hash": source.source_hash,
            "chapter_snapshot_hash": snapshot.snapshot_hash,
            "chunk_plan_hash": chunk_plan.plan_hash,
            "prompt_bundle_hash": canonical_json_hash(prompt_bundle),
        }
        for field_name, expected_hash in expected.items():
            actual = getattr(self, field_name)
            if actual != expected_hash:
                raise ValueError(
                    f"Foreign identity: {field_name}={actual}, expected {expected_hash}"
                )
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
    """

    chapter_id: str
    pids: Tuple[str, ...]
    context: str
    glossary_hash: str
    book_memory_hash: str
    chapter_memory_hash: str
    snapshot_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_nonempty_str(self.chapter_id, "chapter_id")
        _require_unique_pids(self.pids, "Snapshot")
        for name in ("glossary_hash", "book_memory_hash", "chapter_memory_hash"):
            _require_hash(getattr(self, name), name)
        computed = canonical_json_hash({
            "chapter_id": self.chapter_id,
            "pids": list(self.pids),
            "context": self.context,
            "glossary_hash": self.glossary_hash,
            "book_memory_hash": self.book_memory_hash,
            "chapter_memory_hash": self.chapter_memory_hash,
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

    V4_MVP_SPEC_RU.md §3.2: "целевое окно 8-20 PID, мягкий min/max... но
    жёсткий верхний cap" — the upper bound (20) is a hard ceiling, always
    enforced, with no exception. The lower bound (8) is a *soft* target:
    it can be legitimately missed (a whole chapter shorter than 8 PIDs, an
    unavoidable undersized tail after rebalancing, or a single oversized
    leaf block that cannot be split) — such cases must set
    ``undersized_exception=True`` to document why the soft minimum was not
    met; the flag never relaxes the hard maximum.

    ``snapshot_hash`` ties the plan back to the frozen snapshot it was
    computed from.
    """

    chunk_id: str
    snapshot_hash: str
    pids: Tuple[str, ...]
    context: ChunkContext = field(default_factory=ChunkContext)
    undersized_exception: bool = False

    MIN_PIDS = 8
    MAX_PIDS = 20

    def __post_init__(self) -> None:
        _require_nonempty_str(self.chunk_id, "chunk_id")
        _require_hash(self.snapshot_hash, "snapshot_hash")
        _require_unique_pids(self.pids, "ChunkPlan")
        count = len(self.pids)
        if count > self.MAX_PIDS:
            raise ValueError(
                f"ChunkPlan {self.chunk_id}: {count} PIDs exceeds hard cap "
                f"{self.MAX_PIDS} (no exception relaxes the upper bound)"
            )
        if count < self.MIN_PIDS and not self.undersized_exception:
            raise ValueError(
                f"ChunkPlan {self.chunk_id}: {count} PIDs below soft minimum "
                f"{self.MIN_PIDS} (set undersized_exception=True if this is a "
                f"documented, unavoidable case)"
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


@dataclass(frozen=True)
class ChunkPlanArtifact:
    """Authoritative chapter-level plan with a content-derived identity."""

    snapshot_hash: str
    chunks: Tuple[ChunkPlan, ...]
    plan_hash: str = field(init=False)

    @classmethod
    def create(cls, snapshot: Snapshot, chunks: Tuple[ChunkPlan, ...]) -> "ChunkPlanArtifact":
        validate_full_pid_ownership(chunks, snapshot)
        return cls(snapshot_hash=snapshot.snapshot_hash, chunks=chunks)

    def __post_init__(self) -> None:
        _require_hash(self.snapshot_hash, "snapshot_hash")
        if not self.chunks:
            raise ValueError("ChunkPlanArtifact: at least one chunk is required")
        chunk_ids = tuple(chunk.chunk_id for chunk in self.chunks)
        if len(chunk_ids) != len(set(chunk_ids)):
            raise ValueError("ChunkPlanArtifact: duplicate chunk IDs are not allowed")
        for chunk in self.chunks:
            if chunk.snapshot_hash != self.snapshot_hash:
                raise ValueError(
                    f"ChunkPlan {chunk.chunk_id} references foreign snapshot "
                    f"{chunk.snapshot_hash}, expected {self.snapshot_hash}"
                )
        object.__setattr__(self, "plan_hash", canonical_json_hash({
            "artifact": "pact-v4-chunk-plan/v1",
            "snapshot_hash": self.snapshot_hash,
            "chunks": [
                {
                    "chunk_id": chunk.chunk_id,
                    "pids": list(chunk.pids),
                    "context": {
                        "left_ru": chunk.context.left_ru,
                        "right_en": list(chunk.context.right_en),
                    },
                    "undersized_exception": chunk.undersized_exception,
                }
                for chunk in self.chunks
            ],
        }))

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
                raise ValueError(f"Candidate {self.candidate_id}: PID {pid} translation must be a string")
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
    ) -> "Candidate":
        """Create a candidate whose identities cannot be caller-fabricated."""
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
            source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config
        )
        return candidate

    def validate_against(
        self,
        *,
        source: SourceArtifact,
        snapshot: Snapshot,
        chunk_plan: ChunkPlanArtifact,
        config: ConfigArtifact,
    ) -> None:
        """Semantic identity validation required at persistence/cache boundaries."""
        if source.chapter_id != snapshot.chapter_id:
            raise ValueError(
                f"Foreign identity: source chapter {source.chapter_id!r}, "
                f"expected {snapshot.chapter_id!r}"
            )
        chunk_plan.validate_against(snapshot)
        expected = {
            "source_hash": source.source_hash,
            "snapshot_hash": snapshot.snapshot_hash,
            "chunk_plan_hash": chunk_plan.plan_hash,
            "config_identity": config.config_identity,
        }
        for field_name, expected_hash in expected.items():
            actual = getattr(self, field_name)
            if actual != expected_hash:
                raise ValueError(
                    f"Foreign identity: {field_name}={actual}, expected {expected_hash}"
                )
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

    Terminal states are exactly ``complete`` / ``quarantined`` / ``failed``.
    Once in a terminal state there are no outgoing transitions — this is
    enforced by ``_ALLOWED_TRANSITIONS`` mapping terminal states to an
    empty tuple, not by special-casing in ``transition_to``.
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
