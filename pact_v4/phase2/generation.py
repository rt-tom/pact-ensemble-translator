"""Phase 2B: risk-gated A/B candidate generation for a completed ChunkPlan.

Scope (V4_FINAL_REVIEW_AND_IMPLEMENTATION_PLAN_RU_v2.md, "2B. A/B
generation"):

  * risk low            -> exactly 1 candidate (role ``fidelity_first``).
  * risk medium / high  -> exactly 2 candidates, A (``fidelity_first``) and
    B (``balanced_literary``), using the two versioned templates in
    ``pact_v4.phase2.prompts``.
  * Each generation request exposes only: the frozen snapshot identity, the
    chunk's own owned PIDs *and their English text* in source order,
    read-only left context (already committed Russian) and right context
    (English source), and the glossary/style constraints the caller reads
    from the frozen snapshot (passed in structured, not as an opaque
    string) — all of it actually rendered into the request text by
    ``pact_v4.phase2.prompts.render_prompt``, not merely hashed for cache
    purposes.
  * Model output is a strict ordered PID -> Russian-text JSON map, fully
    validated (well-formed JSON, exact PID set/order/ownership, no context
    leakage) before it is wrapped in the immutable ``Candidate`` contract
    from Phase 1A. The full ``bundle_hash`` is recorded in the candidate's
    ``decision_trace`` so provenance is recoverable, not just its 16-char
    prefix in ``candidate_id``.
  * Generation identity (prompt template + version, role, risk-routing
    decision, frozen context inputs including the owned PIDs' actual text,
    model/config identity, generation params) hashes to a deterministic
    cache key; any change to any of those inputs invalidates cache reuse.
    A cache *hit* is still re-verified against the requested chunk_id/role
    and against the candidate's own ownership contract before being
    returned — reuse never depends solely on trusting the hash.

Explicitly OUT of scope for this module (Phase 2C, "Cascaded selection"):
Qwen fidelity/semantic analysis, the deterministic consistency gate, Gemma
Russian-preference judging, a synthesis/"C" candidate, and any
selection/winner function. None of that is implemented, stubbed, or
imported here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Callable, Dict, Mapping, Optional, Protocol, Tuple

from pact_v4.phase1.models import (
    Candidate,
    ChunkPlanArtifact,
    ConfigArtifact,
    GateResult,
    Snapshot,
    SourceArtifact,
    canonical_json_hash,
)
from pact_v4.phase2.prompts import (
    BALANCED_LITERARY_V1,
    FIDELITY_FIRST_V1,
    PromptTemplate,
)
from pact_v4.phase2.risk import GlossaryEntry, RiskAssessment, RiskBand

__all__ = [
    "GenerationParams",
    "PromptBundle",
    "ModelCaller",
    "GenerationCache",
    "GenerationErrorCode",
    "GenerationError",
    "GenerationOutcome",
    "generate_for_chunk",
]


# ---------------------------------------------------------------------------
# Generation params
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GenerationParams:
    """Model-call parameters for one candidate generation.

    ``temperature`` and ``seed`` are PROVISIONAL: they are placeholders
    until the Phase-2 benchmark gate freezes them (see
    docs/architecture/V4_FINAL_REVIEW_AND_IMPLEMENTATION_PLAN_RU_v2.md,
    "Gate: run v3/v4 A/B and chunk benchmark ... freezes chunk range, right
    context, temperature/seed and risk thresholds"). They are still part of
    the bundle identity, so changing them still invalidates the cache.

    ``reasoning`` is fixed to ``0`` for all Phase 2B generation (no
    reasoning/thinking budget at this stage); this is enforced, not just
    documented.
    """

    temperature: float
    seed: int
    max_tokens: int
    reasoning: int = 0

    def __post_init__(self) -> None:
        if self.reasoning != 0:
            raise ValueError(
                f"GenerationParams: reasoning must be fixed to 0 for Phase 2B, "
                f"got {self.reasoning!r}"
            )
        if self.max_tokens <= 0:
            raise ValueError("GenerationParams: max_tokens must be positive")


# ---------------------------------------------------------------------------
# Prompt bundle: full generation identity + deterministic cache key
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromptBundle:
    """The complete, versioned identity of one candidate generation call.

    ``bundle_hash`` is a deterministic sha256 of every input listed in the
    module docstring; it is the cache key. It is derived from content, not
    caller-supplied, so it cannot be spoofed to force a false cache hit.
    """

    template: PromptTemplate
    role: str
    risk_band: str
    risk_policy_version: str
    snapshot_hash: str
    source_hash: str
    chunk_id: str
    owned_pids: Tuple[str, ...]
    owned_source: Tuple[Tuple[str, str], ...]
    left_context: Tuple[Tuple[str, str], ...]
    right_context: Tuple[Tuple[str, str], ...]
    glossary: Tuple[Tuple[str, Tuple[str, ...]], ...]
    style_constraints: Tuple[Tuple[str, str], ...]
    config_identity: str
    params: GenerationParams
    bundle_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.role != self.template.role:
            raise ValueError(
                f"PromptBundle: role {self.role!r} does not match template role "
                f"{self.template.role!r}"
            )
        if tuple(pid for pid, _ in self.owned_source) != self.owned_pids:
            raise ValueError(
                "PromptBundle: owned_source PIDs/order must exactly match owned_pids "
                f"({tuple(pid for pid, _ in self.owned_source)!r} != {self.owned_pids!r})"
            )
        object.__setattr__(self, "bundle_hash", canonical_json_hash(self._identity_payload()))

    def _identity_payload(self) -> dict:
        return {
            "artifact": "pact-v4-prompt-bundle/v2",
            "template_role": self.template.role,
            "template_version": self.template.version,
            "template_instructions_hash": canonical_json_hash(self.template.instructions),
            "risk_band": self.risk_band,
            "risk_policy_version": self.risk_policy_version,
            "snapshot_hash": self.snapshot_hash,
            "source_hash": self.source_hash,
            "chunk_id": self.chunk_id,
            "owned_pids": list(self.owned_pids),
            # owned_source is already implied by source_hash + owned_pids (the
            # source artifact's identity is content-derived), but it is
            # hashed explicitly too: it is the actual text sent to the
            # model, and this bundle's job is to be the full, auditable
            # identity of *the request*, not merely a value that happens to
            # be collision-resistant with it.
            "owned_source": [list(item) for item in self.owned_source],
            "left_context": [list(item) for item in self.left_context],
            "right_context": [list(item) for item in self.right_context],
            "glossary": [[term, list(targets)] for term, targets in self.glossary],
            "style_constraints": [list(item) for item in self.style_constraints],
            "config_identity": self.config_identity,
            "params": {
                "temperature": self.params.temperature,
                "seed": self.params.seed,
                "max_tokens": self.params.max_tokens,
                "reasoning": self.params.reasoning,
            },
        }


# ---------------------------------------------------------------------------
# Model call interface (injectable; no real HTTP client by default)
# ---------------------------------------------------------------------------


class ModelCaller(Protocol):
    """Injectable model-call interface.

    Tests supply a mock/stub. There is deliberately no default
    implementation that talks to a real llama-server, Qwen/Gemma profile,
    or any other network endpoint — that wiring belongs to the production
    pipeline, not to this module.
    """

    def __call__(self, bundle: PromptBundle) -> str:
        """Return the model's raw text output (expected to be JSON)."""
        ...


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

CacheEntry = "GenerationCandidateResult"


class GenerationCache:
    """Exact-match cache keyed on the full ``PromptBundle.bundle_hash``.

    Reuse happens only on an exact identity match; any change to any input
    captured by the bundle (template content/version, role, risk decision,
    context, snapshot version, model/config identity, chunk PIDs, params)
    produces a different hash and therefore a cache miss.
    """

    def __init__(self) -> None:
        self._store: Dict[str, "GenerationCandidateResult"] = {}

    def get(self, bundle_hash: str) -> Optional["GenerationCandidateResult"]:
        return self._store.get(bundle_hash)

    def put(self, bundle_hash: str, result: "GenerationCandidateResult") -> None:
        self._store[bundle_hash] = result


# ---------------------------------------------------------------------------
# Errors / outcome
# ---------------------------------------------------------------------------


class GenerationErrorCode(str, Enum):
    INVALID_JSON = "invalid_json"
    PID_MISMATCH = "pid_mismatch"
    CONTEXT_LEAKAGE = "context_leakage"


class _GenerationValidationError(Exception):
    def __init__(self, code: GenerationErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class GenerationError:
    role: str
    code: GenerationErrorCode
    detail: str


# What actually sits in the cache: either a validated Candidate or a
# recorded validation failure for that exact identity.
@dataclass(frozen=True)
class GenerationCandidateResult:
    candidate: Optional[Candidate]
    error: Optional[GenerationError]


@dataclass(frozen=True)
class GenerationOutcome:
    """Result of generating all candidates required for one chunk's risk band.

    ``status`` is ``"complete"`` only if every role required by the risk
    band produced a valid candidate. If any required role failed
    validation, ``status`` is ``"incomplete"`` — the other, successful
    role's candidate is still reported in ``candidates`` but is never used
    to substitute or fabricate the missing one, and no selection is made
    here (that is Phase 2C).
    """

    chunk_id: str
    risk_band: str
    expected_roles: Tuple[str, ...]
    candidates: Mapping[str, Candidate]
    errors: Mapping[str, GenerationError]
    status: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", MappingProxyType(dict(self.candidates)))
        object.__setattr__(self, "errors", MappingProxyType(dict(self.errors)))


def _roles_for_band(band: RiskBand) -> Tuple[str, ...]:
    if band is RiskBand.LOW:
        return ("fidelity_first",)
    if band in (RiskBand.MEDIUM, RiskBand.HIGH):
        return ("fidelity_first", "balanced_literary")
    raise ValueError(f"Unknown risk band: {band!r}")


_TEMPLATES: Mapping[str, PromptTemplate] = MappingProxyType({
    "fidelity_first": FIDELITY_FIRST_V1,
    "balanced_literary": BALANCED_LITERARY_V1,
})


class _OrderedPairs(list):
    """Marks a parsed JSON *object* (as opposed to array) while preserving
    every raw key/value pair in source order, including literal duplicate
    keys — plain ``json.loads`` silently collapses duplicate keys to "last
    write wins" before any validation code can see them, which would make
    duplicate-PID rejection unreachable."""


def _parse_ordered_pid_pairs(raw: str) -> list:
    """Reject truncated/partial/invalid JSON outright; never best-effort repair.

    Returns the raw ``(key, value)`` pairs of the top-level JSON object, in
    source order, with duplicates intact so callers can detect them.
    """
    try:
        parsed = json.loads(raw, object_pairs_hook=_OrderedPairs)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Reject partial or invalid JSON: {exc}") from exc
    if not isinstance(parsed, _OrderedPairs):
        raise ValueError("Reject partial or invalid JSON: expected a JSON object")
    return list(parsed)


def _validate_pid_map(
    pairs: list,
    *,
    owned_pids: Tuple[str, ...],
    context_pids: frozenset,
) -> Tuple[Tuple[str, str], ...]:
    keys = [key for key, _ in pairs]

    leaked = [key for key in keys if key in context_pids]
    if leaked:
        raise _GenerationValidationError(
            GenerationErrorCode.CONTEXT_LEAKAGE,
            f"output contains context-only PID(s): {leaked}",
        )

    owned_set = set(owned_pids)
    if len(keys) != len(set(keys)):
        seen: set = set()
        duplicates = sorted({key for key in keys if key in seen or seen.add(key)})
        raise _GenerationValidationError(
            GenerationErrorCode.PID_MISMATCH, f"duplicate PIDs in output: {duplicates}"
        )
    missing = [pid for pid in owned_pids if pid not in keys]
    extra = [key for key in keys if key not in owned_set]
    if missing or extra:
        raise _GenerationValidationError(
            GenerationErrorCode.PID_MISMATCH,
            f"PID set mismatch: missing={missing}, extra={extra}",
        )
    if keys != list(owned_pids):
        raise _GenerationValidationError(
            GenerationErrorCode.PID_MISMATCH,
            f"PID order {keys} does not match owned order {list(owned_pids)}",
        )
    data = dict(pairs)
    for pid, text in pairs:
        if not isinstance(text, str):
            raise _GenerationValidationError(
                GenerationErrorCode.PID_MISMATCH,
                f"PID {pid}: translation must be a string",
            )
    return tuple((pid, data[pid]) for pid in owned_pids)


def _owned_source_for(source: SourceArtifact, owned_pids: Tuple[str, ...]) -> Tuple[Tuple[str, str], ...]:
    source_map = dict(source.source)
    return tuple((pid, source_map[pid]) for pid in owned_pids)


def _glossary_identity(glossary: Tuple[GlossaryEntry, ...]) -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
    return tuple((entry.source_term, tuple(entry.target_terms)) for entry in glossary)


class _CachePoisoned(AssertionError):
    """Raised if a cache hit's candidate doesn't match the identity that
    produced its cache key — this indicates an internal bug (e.g. a caller
    writing into ``GenerationCache`` directly), never an expected runtime
    path, hence ``AssertionError`` rather than a typed/recoverable error."""


def _generate_one(
    *,
    role: str,
    risk: RiskAssessment,
    source: SourceArtifact,
    snapshot: Snapshot,
    chunk_plan: ChunkPlanArtifact,
    chunk_id: str,
    left_context: Tuple[Tuple[str, str], ...],
    right_context: Tuple[Tuple[str, str], ...],
    glossary: Tuple[GlossaryEntry, ...],
    style_constraints: Tuple[Tuple[str, str], ...],
    config: ConfigArtifact,
    params: GenerationParams,
    model_caller: ModelCaller,
    cache: GenerationCache,
) -> GenerationCandidateResult:
    chunk = chunk_plan.chunk(chunk_id)
    template = _TEMPLATES[role]

    bundle = PromptBundle(
        template=template,
        role=role,
        risk_band=risk.band.value,
        risk_policy_version=risk.policy_version,
        snapshot_hash=snapshot.snapshot_hash,
        source_hash=source.source_hash,
        chunk_id=chunk_id,
        owned_pids=chunk.pids,
        owned_source=_owned_source_for(source, chunk.pids),
        left_context=left_context,
        right_context=right_context,
        glossary=_glossary_identity(glossary),
        style_constraints=style_constraints,
        config_identity=config.config_identity,
        params=params,
    )

    cached = cache.get(bundle.bundle_hash)
    if cached is not None:
        # Defense in depth: a bundle_hash collision or a bug/tamper in a
        # caller that writes into GenerationCache directly must never
        # silently hand back a candidate for the wrong chunk/role. This is
        # not "trust the hash" — it re-verifies the cached candidate against
        # the artifacts that were actually requested, on every hit.
        if cached.candidate is not None:
            if cached.candidate.chunk_id != chunk_id or cached.candidate.role != role:
                raise _CachePoisoned(
                    f"Cache identity corruption: bundle_hash {bundle.bundle_hash} "
                    f"resolved to chunk_id={cached.candidate.chunk_id!r} "
                    f"role={cached.candidate.role!r}, expected "
                    f"chunk_id={chunk_id!r} role={role!r}"
                )
            cached.candidate.validate_against(
                source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config
            )
        return cached

    context_pids = frozenset(
        pid for pid, _ in left_context
    ) | frozenset(pid for pid, _ in right_context)

    raw = model_caller(bundle)

    try:
        pairs = _parse_ordered_pid_pairs(raw)
        translation = _validate_pid_map(
            pairs, owned_pids=chunk.pids, context_pids=context_pids
        )
        candidate = Candidate.create(
            candidate_id=f"{chunk_id}:{role}:{bundle.bundle_hash[:16]}",
            chunk_id=chunk_id,
            role=role,
            translation=translation,
            source=source,
            snapshot=snapshot,
            chunk_plan=chunk_plan,
            config=config,
            decision_trace=(
                GateResult(
                    gate="phase2b_prompt_bundle",
                    passed=True,
                    detail=bundle.bundle_hash,
                ),
            ),
        )
        result = GenerationCandidateResult(candidate=candidate, error=None)
    except ValueError as exc:
        # _parse_ordered_pid_pairs raises plain ValueError for truncated,
        # malformed, or non-object JSON.
        result = GenerationCandidateResult(
            candidate=None,
            error=GenerationError(role, GenerationErrorCode.INVALID_JSON, str(exc)),
        )
    except _GenerationValidationError as exc:
        result = GenerationCandidateResult(
            candidate=None,
            error=GenerationError(role, exc.code, exc.detail),
        )

    cache.put(bundle.bundle_hash, result)
    return result


def generate_for_chunk(
    *,
    chunk_id: str,
    risk: RiskAssessment,
    source: SourceArtifact,
    snapshot: Snapshot,
    chunk_plan: ChunkPlanArtifact,
    left_context: Tuple[Tuple[str, str], ...] = (),
    right_context: Tuple[Tuple[str, str], ...] = (),
    glossary: Tuple[GlossaryEntry, ...] = (),
    style_constraints: Mapping[str, str] = MappingProxyType({}),
    config: ConfigArtifact,
    params: GenerationParams,
    model_caller: ModelCaller,
    cache: Optional[GenerationCache] = None,
) -> GenerationOutcome:
    """Generate the risk-gated candidate set for one chunk.

    low risk -> exactly one candidate (``fidelity_first``).
    medium/high risk -> exactly two candidates, ``fidelity_first`` (A) and
    ``balanced_literary`` (B). There is no third candidate and no
    selection/winner logic here (Phase 2C).

    ``glossary``/``style_constraints`` are the frozen snapshot's actual
    character/style/voice constraints (structured, not a caller-flattened
    opaque string) — they are both part of the hashed prompt-bundle identity
    and are rendered into the request text (see
    ``pact_v4.phase2.prompts.render_prompt``), so a snapshot with different
    constraints both invalidates the cache and actually changes what the
    model sees.
    """
    if cache is None:
        cache = GenerationCache()

    roles = _roles_for_band(risk.band)
    style_pairs = tuple(sorted(style_constraints.items()))

    candidates: Dict[str, Candidate] = {}
    errors: Dict[str, GenerationError] = {}

    for role in roles:
        result = _generate_one(
            role=role,
            risk=risk,
            source=source,
            snapshot=snapshot,
            chunk_plan=chunk_plan,
            chunk_id=chunk_id,
            left_context=left_context,
            right_context=right_context,
            glossary=glossary,
            style_constraints=style_pairs,
            config=config,
            params=params,
            model_caller=model_caller,
            cache=cache,
        )
        if result.candidate is not None:
            candidates[role] = result.candidate
        else:
            assert result.error is not None
            errors[role] = result.error

    status = "complete" if len(candidates) == len(roles) else "incomplete"

    return GenerationOutcome(
        chunk_id=chunk_id,
        risk_band=risk.band.value,
        expected_roles=roles,
        candidates=candidates,
        errors=errors,
        status=status,
    )
