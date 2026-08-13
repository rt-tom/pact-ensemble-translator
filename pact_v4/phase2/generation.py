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
  * Whichever of ``pact_v4.phase2.risk.REQUIRED_RISK_CATEGORIES`` the
    source risk pre-screen actually flagged for this chunk (``number_word``,
    ``tone_profanity``) is threaded onto the ``PromptBundle`` as
    ``required_risk_feature_codes`` and rendered into the request text as an
    explicit instruction by ``pact_v4.phase2.prompts.render_prompt`` —
    conditionally, only for the categories actually present, never
    unconditionally. This module imports ``REQUIRED_RISK_CATEGORIES`` from
    ``pact_v4.phase2.risk`` rather than redeclaring the category list.

Explicitly OUT of scope for this module (Phase 2C, "Cascaded selection"):
Qwen fidelity/semantic analysis, the deterministic consistency gate, Gemma
Russian-preference judging, a synthesis/"C" candidate, and any
selection/winner function. None of that is implemented, stubbed, or
imported here.
"""
from __future__ import annotations

import json
import time
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
    WholeChapterPidMap,
    canonical_json_hash,
)
from pact_v4.phase2.prompts import (
    BALANCED_LITERARY_V3,
    FIDELITY_FIRST_V1,
    PromptTemplate,
)
# NOTE: the transport-boundary failure type (CompletionError) is NOT imported
# at module level on purpose: `pact_v4.runtime.backend_protocol` is reached
# through `pact_v4.runtime`, whose package __init__ imports
# `backend_role_adapters`, which imports this module — a top-level import here
# would create a circular import at module load. `generate_whole_chapter`
# imports it lazily (see there) to classify session aborts honestly.
from pact_v4.phase2.risk import (
    REQUIRED_RISK_CATEGORIES,
    GlossaryEntry,
    RiskAssessment,
    RiskBand,
    assess_source_risk,
)

__all__ = [
    "GenerationParams",
    "PromptBundle",
    "ModelCaller",
    "GenerationCache",
    "GenerationErrorCode",
    "GenerationError",
    "GenerationOutcome",
    "generate_for_chunk",
    "WholeChapterRetryPolicy",
    "validate_whole_chapter_raw",
    "generate_whole_chapter",
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

    ``reasoning`` is the Phase 2B reasoning/thinking budget: ``0`` = off
    (B1 baseline), ``1`` = low, ``2`` = medium, ``3`` = high. The value is
    transported to backends that support per-request reasoning effort
    (opencode serve ``reasoningEffort``) and is part of the bundle identity,
    so changing it invalidates the cache. Other phases (audit/repair/
    formatting) never receive it.
    """

    temperature: float
    seed: int
    max_tokens: int
    reasoning: int = 0

    def __post_init__(self) -> None:
        if self.reasoning not in (0, 1, 2, 3):
            raise ValueError(
                f"GenerationParams: reasoning must be in {{0, 1, 2, 3}} "
                f"(0=off, 1=low, 2=medium, 3=high), got {self.reasoning!r}"
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
    required_risk_feature_codes: Tuple[str, ...]
    snapshot_hash: str
    source_hash: str
    chunk_id: str
    owned_pids: Tuple[str, ...]
    owned_source: Tuple[Tuple[str, str], ...]
    left_context: Tuple[Tuple[str, str], ...]
    right_context: Tuple[Tuple[str, str], ...]
    glossary: Tuple[Tuple[str, Tuple[str, ...]], ...]
    style_constraints: Tuple[Tuple[str, str], ...]
    bible_text: str
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
        unknown = set(self.required_risk_feature_codes) - REQUIRED_RISK_CATEGORIES
        if unknown:
            raise ValueError(
                f"PromptBundle: required_risk_feature_codes contains non-required "
                f"categories {sorted(unknown)}; only {sorted(REQUIRED_RISK_CATEGORIES)} "
                "may appear here"
            )
        object.__setattr__(self, "bundle_hash", canonical_json_hash(self._identity_payload()))

    def _identity_payload(self) -> dict:
        return {
            "artifact": "pact-v4-prompt-bundle/v3",
            "template_role": self.template.role,
            "template_version": self.template.version,
            "template_instructions_hash": canonical_json_hash(self.template.instructions),
            "risk_band": self.risk_band,
            "risk_policy_version": self.risk_policy_version,
            "required_risk_feature_codes": sorted(self.required_risk_feature_codes),
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
            "bible_text": self.bible_text,
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
    # V4.1 A1: the model call itself failed (transport error / session abort
    # finish=other|error). Distinguished from JSON/PID failures because the
    # whole-chapter contract retries these boundedly and must record *why* a
    # chapter could not be generated when the retry budget is exhausted.
    SESSION_ABORT = "session_abort"


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


def _roles_for_band(band: RiskBand, *, lazy_balanced: bool = True) -> Tuple[str, ...]:
    """Roles to generate up-front for a risk band.

    V4 Efficiency A2 (lazy balanced-only, default ``lazy_balanced=True``):
    exactly one primary candidate (``balanced_literary``) for every band.
    ``fidelity_first`` is NOT generated up-front — the strict driver
    generates it lazily only when the primary fails the Qwen/deterministic
    gates (run_005 chunk0010/0014 fidelity-wins cases).

    ``lazy_balanced=False`` restores the legacy scheme: one ``fidelity_first``
    candidate for low risk, the A/B pair (``fidelity_first`` +
    ``balanced_literary``) for medium/high risk.
    """
    if lazy_balanced:
        return ("balanced_literary",)
    if band is RiskBand.LOW:
        return ("fidelity_first",)
    if band in (RiskBand.MEDIUM, RiskBand.HIGH):
        return ("fidelity_first", "balanced_literary")
    raise ValueError(f"Unknown risk band: {band!r}")


_TEMPLATES: Mapping[str, PromptTemplate] = MappingProxyType({
    "fidelity_first": FIDELITY_FIRST_V1,
    "balanced_literary": BALANCED_LITERARY_V3,
})


class _OrderedPairs(list):
    """Marks a parsed JSON *object* (as opposed to array) while preserving
    every raw key/value pair in source order, including literal duplicate
    keys — plain ``json.loads`` silently collapses duplicate keys to "last
    write wins" before any validation code can see them, which would make
    duplicate-PID rejection unreachable."""


def _parse_ordered_pid_pairs(
    raw: str,
    *,
    expected_pids: Optional[Tuple[str, ...]] = None,
    min_coverage: float = 0.9,
) -> list:
    """Reject truncated/partial/invalid JSON outright; never best-effort repair.

    Returns the raw ``(key, value)`` pairs of the top-level JSON object, in
    source order, with duplicates intact so callers can detect them.

    REPAIR-RECEIVER (t_b590c24f, run_remote_007): the one deterministic
    exception is whole-chapter pid-keyed JSON, handled by the SAME single
    tolerant receiver as ``parse_json_response`` — ``extract_pid_pairs``
    splits the text on the top-level ``"p\\d{5}"`` keys and is robust to ANY
    model defect on a long output (pid-colon ``, "``, an ASCII quote inside
    a value — p00087 „…" —, truncation, missing commas, garbage). The
    extractor is fail-closed: it returns a dict only when coverage >=
    ``min_coverage`` (expected = ``expected_pids`` when the caller knows the
    contract PID set, else the keys found in the text) and every value is
    clean; a body below 90% coverage or with a suspicious value is honestly
    truncated and raises ``ValueError`` exactly as before (bounded retry,
    real damage is never masked). ``repair_pid_colon_comma`` (PR #178) is
    removed — its logic is absorbed by the extractor.
    """
    try:
        parsed = json.loads(raw, object_pairs_hook=_OrderedPairs)
    except json.JSONDecodeError as exc:
        from pact_v4.runtime.json_resilience import (  # noqa: PLC0415
            extract_pid_pairs,
        )

        extracted = extract_pid_pairs(
            raw, expected_pids=expected_pids, min_coverage=min_coverage
        )
        if extracted is None:
            raise ValueError(f"Reject partial or invalid JSON: {exc}") from exc
        # The extractor returns a dict in source order; duplicates are
        # rejected inside it, so converting to ordered pairs keeps the
        # contract intact.
        return list(extracted.items())
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
    bible_text: str,
    config: ConfigArtifact,
    params: GenerationParams,
    model_caller: ModelCaller,
    cache: GenerationCache,
) -> GenerationCandidateResult:
    chunk = chunk_plan.chunk(chunk_id)
    template = _TEMPLATES[role]
    required_risk_feature_codes = tuple(
        sorted({feature.code for feature in risk.features} & REQUIRED_RISK_CATEGORIES)
    )

    bundle = PromptBundle(
        template=template,
        role=role,
        risk_band=risk.band.value,
        risk_policy_version=risk.policy_version,
        required_risk_feature_codes=required_risk_feature_codes,
        snapshot_hash=snapshot.snapshot_hash,
        source_hash=source.source_hash,
        chunk_id=chunk_id,
        owned_pids=chunk.pids,
        owned_source=_owned_source_for(source, chunk.pids),
        left_context=left_context,
        right_context=right_context,
        glossary=_glossary_identity(glossary),
        style_constraints=style_constraints,
        bible_text=bible_text,
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
        pairs = _parse_ordered_pid_pairs(raw, expected_pids=chunk.pids)
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
    bible_text: str = "",
    config: ConfigArtifact,
    params: GenerationParams,
    model_caller: ModelCaller,
    cache: Optional[GenerationCache] = None,
    lazy_balanced: bool = True,
    roles: Optional[Tuple[str, ...]] = None,
) -> GenerationOutcome:
    """Generate the risk-gated candidate set for one chunk.

    V4 Efficiency A2 (default, ``lazy_balanced=True``): every risk band
    generates exactly one primary candidate (``balanced_literary``);
    ``fidelity_first`` is deferred to the strict driver's lazy fallback
    (re-generated only when the primary fails the gates).

    Legacy scheme (``lazy_balanced=False``): low risk -> exactly one
    candidate (``fidelity_first``); medium/high risk -> exactly two
    candidates, ``fidelity_first`` (A) and ``balanced_literary`` (B).
    There is no third candidate and no selection/winner logic here
    (Phase 2C).

    ``roles`` (optional) overrides the risk-based role routing entirely and
    generates exactly those roles — used by the strict driver's A2 lazy
    fallback to re-generate a single ``fidelity_first`` candidate after the
    primary failed the gates. Must be non-empty and contain only known roles;
    ``lazy_balanced`` is ignored for role resolution when ``roles`` is given.

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

    if roles is not None:
        if not roles:
            raise ValueError("generate_for_chunk: roles must be non-empty")
        unknown = set(roles) - set(_TEMPLATES)
        if unknown:
            raise ValueError(
                f"generate_for_chunk: unknown role(s) {sorted(unknown)}; "
                f"known roles are {sorted(_TEMPLATES)}"
            )
    else:
        roles = _roles_for_band(risk.band, lazy_balanced=lazy_balanced)
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
            bible_text=bible_text,
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


# ---------------------------------------------------------------------------
# Whole-chapter generation (V4.1 A1): one call per chapter, strict full-PID
# JSON contract, bounded retry on every failure class including session abort.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WholeChapterRetryPolicy:
    """Bounded retry policy for whole-chapter generation (V4.1 A1).

    Unlike per-chunk generation (whose adapter retries only empty/truncated
    JSON at the transport level), the whole-chapter contract retries EVERY
    failure class at the generation layer — malformed/missing/extra/reordered
    PID, empty/truncated JSON, and session aborts (Gate 0: 2/5 calls aborted
    with finish=other/error) — because one call produces the entire chapter
    and a transient failure must not silently degrade it. Retries re-issue
    the identical bundle (same identity), so they never change cache/resume
    identity.

    ``max_attempts`` is the total number of model calls (1 initial + retries).
    ``base_delay_seconds`` is the exponential-backoff base: the k-th retry
    waits ``base * 2**k`` seconds.
    """

    max_attempts: int = 3
    base_delay_seconds: float = 1.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or self.max_attempts < 1
        ):
            raise ValueError("WholeChapterRetryPolicy: max_attempts must be a positive int")
        if self.base_delay_seconds < 0:
            raise ValueError(
                "WholeChapterRetryPolicy: base_delay_seconds must be >= 0"
            )

    def delay_for(self, attempt: int) -> float:
        """Backoff delay before the attempt-th retry (0-based)."""
        return self.base_delay_seconds * (2 ** int(attempt))


def _whole_chapter_risk(
    source: SourceArtifact, glossary: Tuple[GlossaryEntry, ...]
) -> RiskAssessment:
    """Chapter-level risk pre-screen for the whole-chapter prompt bundle.

    Whole-chapter generation has no per-chunk risk routing, but the bundle
    identity still carries ``risk_band``/``required_risk_feature_codes``, so
    the full chapter is screened as one unit (deterministic, zero model
    calls) and the bundle's risk identity reflects the whole source.
    """
    return assess_source_risk(
        tuple((pid, text) for pid, text in source.source),
        glossary=glossary,
        source_complete=True,
    )


def _CompletionErrorType() -> type:
    """The transport-boundary failure type, imported lazily (no import cycle).

    ``pact_v4.runtime.backend_protocol`` is reached through
    ``pact_v4.runtime``, whose package ``__init__`` imports
    ``backend_role_adapters``, which imports this module — so a module-level
    import of ``CompletionError`` here would create a circular import at load
    time. Importing it on first use (after all modules are loaded) is safe and
    keeps the whole-chapter retry contract honest: session aborts are
    classified as ``SESSION_ABORT`` rather than guessed.
    """
    from pact_v4.runtime.backend_protocol import CompletionError  # noqa: PLC0415

    return CompletionError


def _JsonResilienceErrorTypes() -> tuple:
    """The adapter JSON-resilience failure types, imported lazily.

    ``BackendModelCaller`` retries an empty/truncated body up to its own
    ``JsonRetryPolicy`` budget (``retry_json_call``) and re-raises
    ``EmptyResponseError`` / ``TruncatedJSONError`` when exhausted. Those are
    ``ValueError`` subclasses, NOT ``CompletionError``, so without an explicit
    catch the whole-chapter bounded-retry loop (which catches only
    ``CompletionError`` as a session abort) would let the adapter's exhaustion
    exception escape and crash the run instead of returning an honest
    ``incomplete_generation``. Imported lazily for the same reason as
    ``_CompletionErrorType`` (runtime package imports this module).
    """
    from pact_v4.runtime.json_resilience import (  # noqa: PLC0415
        EmptyResponseError,
        TruncatedJSONError,
    )

    return (EmptyResponseError, TruncatedJSONError)


def validate_whole_chapter_raw(
    raw: str, pid_map: WholeChapterPidMap
) -> Tuple[Tuple[str, str], ...]:
    """Strictly validate a whole-chapter raw snapshot against the A1 contract.

    Applies the exact same validation a whole-chapter generation attempt
    performs (``_parse_ordered_pid_pairs`` + ``_validate_pid_map`` over the
    full chapter map): the text must be a JSON object whose keys are exactly
    ``pid_map.pids`` in the same source order, all values strings, with
    literal duplicate keys rejected. Failure taxonomy matches a generation
    attempt: ``ValueError`` for truncated/invalid/non-object JSON and
    ``_GenerationValidationError`` for PID-set/order/type violations — so a
    damaged or partial raw snapshot can never be mistaken for a complete
    one, whether it arrives as model output (generation) or as a resume
    snapshot on disk.
    """
    pairs = _parse_ordered_pid_pairs(raw, expected_pids=pid_map.pids)
    return _validate_pid_map(pairs, owned_pids=pid_map.pids, context_pids=frozenset())


def generate_whole_chapter(
    *,
    role: str = "balanced_literary",
    source: SourceArtifact,
    snapshot: Snapshot,
    chunk_plan: ChunkPlanArtifact,
    pid_map: WholeChapterPidMap,
    glossary: Tuple[GlossaryEntry, ...],
    bible_text: str,
    config: ConfigArtifact,
    params: GenerationParams,
    model_caller: ModelCaller,
    cache: Optional[GenerationCache] = None,
    retry: WholeChapterRetryPolicy = WholeChapterRetryPolicy(),
    on_retry: Optional[Callable[[int, str], None]] = None,
    reasoning_sink: Optional[Callable[[int, str], None]] = None,
    live_reasoning_writer: Optional[
        Callable[[int], Optional[Callable[[str], None]]]
    ] = None,
    raw_sink: Optional[Callable[[int, str], None]] = None,
) -> GenerationOutcome:
    """Generate the whole chapter in ONE model call (V4.1 A1).

    The prompt bundle carries the chapter's full ordered PID map
    (``chunk_id="whole_chapter"``, ``owned_pids``/``owned_source`` = every
    PID in source order, no left/right context). Output must be a strict JSON
    object mapping EVERY PID to its Russian text, in exact source order — the
    same ``_validate_pid_map`` contract as chunked generation, applied to the
    full chapter map. Validation failures (malformed/missing/extra/reordered
    PID, empty/truncated JSON) and transport/session aborts are retried
    boundedly per ``retry``; after the budget the last error is returned with
    ``status="incomplete"`` — never a partial PID map.

    The returned ``GenerationOutcome`` uses ``chunk_id="whole_chapter"`` and
    exactly one candidate role, so the strict runner serializes the chapter's
    generation record with the standard ``_serialize_generation_outcome``
    shape (candidate_id ``whole_chapter:<role>:<hash>``).

    ``on_retry`` (optional, diagnostics-only) is invoked as
    ``on_retry(attempt, reason)`` after EVERY failed attempt with the 1-based
    attempt number and a classification of the failure (``malformed`` /
    ``missing_pid`` / ``truncated`` / ``abort``) — the same reason vocabulary
    the phase-progress monitor renders as "GEN attempt N/M (reason)". It is
    purely observational: a raise inside the hook is swallowed (logged) and
    never changes retry behavior.

    ``reasoning_sink`` (optional, GEN-REASONING, diagnostics-only) is invoked
    as ``reasoning_sink(attempt_index, reasoning_text)`` after EVERY model
    call attempt (0-based attempt index; the successful attempt AND any
    truncated/aborted retry), with the reasoning text the caller reported for
    that attempt (``''`` when the transport or provider produced none). The
    text is read from the caller's ``last_reasoning`` attribute (production
    callers capture ``response.raw_metadata['reasoning']``), so a stub caller
    without the attribute yields ``''``. Purely observational: a raise inside
    the sink is swallowed (logged) and never changes generation behavior.
    Reasoning is a text artifact only — it never enters cache/resume identity.

    ``live_reasoning_writer`` (optional, GEN-STREAM, diagnostics-only) is a
    per-attempt factory invoked BEFORE each model call as
    ``live_reasoning_writer(attempt_index)``; the returned callable (or
    ``None``) is installed on the model caller as its live reasoning-chunk
    sink (``CompletionRequest.on_reasoning_chunk``) for the duration of that
    attempt and cleared immediately after the call. The runner passes a
    factory that opens the per-attempt ``whole_chapter_reasoning.txt`` /
    ``whole_chapter_retryN_reasoning.txt`` file via ``open_reasoning_writer``
    BEFORE the call, so the file grows live while the model is still
    generating (the REASONING-STREAM pattern; local llama-server streams
    reasoning_content chunks, the OpenCode transport delivers once after
    completion — both through the same sink). Callers without a
    ``set_reasoning_chunk_sink`` method are left untouched (stub callers keep
    the post-completion ``reasoning_sink`` path). Purely observational: any
    failure here is logged and swallowed, and the live file is never the
    source of truth — the authoritative post-completion write stays in the
    runner.
    """
    if cache is None:
        cache = GenerationCache()

    def _notify_retry(attempt: int, reason: str) -> None:
        if on_retry is None:
            return
        try:
            on_retry(attempt, reason)
        except Exception:  # noqa: BLE001 — diagnostics hook, never breaks generation
            LOG.debug("whole-chapter on_retry hook failed", exc_info=True)

    def _emit_reasoning(attempt: int) -> None:
        if reasoning_sink is None:
            return
        try:
            reasoning = getattr(model_caller, "last_reasoning", None)
            reasoning_sink(attempt, str(reasoning or ""))
        except Exception:  # noqa: BLE001 — diagnostics sink, never breaks generation
            LOG.debug("whole-chapter reasoning_sink failed", exc_info=True)

    # V4.1 GEN-STREAM: optional per-attempt live reasoning sink. The caller
    # may expose ``set_reasoning_chunk_sink`` (BackendModelCaller and its
    # HttpModelCaller/LifecycleModelCaller wrappers do; stub callers do not).
    # When both the factory and the setter exist, the per-attempt writer is
    # installed BEFORE the model call and cleared right after, so the
    # ``*_reasoning.txt`` file grows live during generation. Every hook here
    # is best-effort: a raise never changes generation behavior.
    _live_setter = getattr(model_caller, "set_reasoning_chunk_sink", None)

    def _install_live_reasoning(attempt: int) -> None:
        if live_reasoning_writer is None or _live_setter is None:
            return
        try:
            _live_setter(live_reasoning_writer(attempt))
        except Exception:  # noqa: BLE001 — diagnostics hook, never breaks generation
            LOG.debug("whole-chapter live reasoning install failed", exc_info=True)
            try:
                _live_setter(None)
            except Exception:  # noqa: BLE001
                LOG.debug("whole-chapter live reasoning clear failed", exc_info=True)

    def _clear_live_reasoning() -> None:
        if _live_setter is None:
            return
        try:
            _live_setter(None)
        except Exception:  # noqa: BLE001 — diagnostics hook, never breaks generation
            LOG.debug("whole-chapter live reasoning clear failed", exc_info=True)

    template = _TEMPLATES[role]
    risk = _whole_chapter_risk(source, glossary)
    required_risk_feature_codes = tuple(
        sorted({feature.code for feature in risk.features} & REQUIRED_RISK_CATEGORIES)
    )

    bundle = PromptBundle(
        template=template,
        role=role,
        risk_band=risk.band.value,
        risk_policy_version=risk.policy_version,
        required_risk_feature_codes=required_risk_feature_codes,
        snapshot_hash=snapshot.snapshot_hash,
        source_hash=source.source_hash,
        chunk_id="whole_chapter",
        owned_pids=pid_map.pids,
        owned_source=_owned_source_for(source, pid_map.pids),
        left_context=(),
        right_context=(),
        glossary=_glossary_identity(glossary),
        style_constraints=(),
        bible_text=bible_text,
        config_identity=config.config_identity,
        params=params,
    )

    cached = cache.get(bundle.bundle_hash)
    if cached is not None:
        if cached.candidate is not None:
            # Defense in depth, mirroring the chunked cache-hit re-verification:
            # a poisoned cache entry for the wrong identity must never be handed
            # back as the chapter's candidate.
            if cached.candidate.chunk_id != "whole_chapter" or cached.candidate.role != role:
                raise _CachePoisoned(
                    f"Cache identity corruption: bundle_hash {bundle.bundle_hash} "
                    f"resolved to chunk_id={cached.candidate.chunk_id!r} "
                    f"role={cached.candidate.role!r}, expected whole_chapter/{role}"
                )
            cached.candidate.validate_against(
                source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config,
                whole_chapter_pid_map=pid_map,
            )
        return _whole_chapter_outcome(role=role, risk=risk, result=cached)

    last_error: Optional[GenerationError] = None
    for attempt in range(retry.max_attempts):
        # GEN-STREAM: open/install the per-attempt live reasoning writer
        # BEFORE the model call so the file exists and grows during it.
        _install_live_reasoning(attempt)
        try:
            raw = model_caller(bundle)
            # RAW-SINK (architect, run_remote_004/005): persist the raw
            # model response of EVERY attempt — a later parse/validation
            # failure then leaves a disk trail (the run_011 lesson applied
            # to whole-chapter generation; previously raw was only written
            # on success as translations_raw.json, so TruncatedJSONError
            # diagnosis was guesswork). Best-effort: a raise here never
            # changes generation behavior.
            if raw_sink is not None:
                try:
                    raw_sink(attempt, raw)
                except Exception:  # noqa: BLE001 — diagnostics hook
                    LOG.debug("whole-chapter raw_sink failed", exc_info=True)
        except _CompletionErrorType() as exc:
            # Transport/session abort (Gate 0: finish=other/error). Retried
            # boundedly like every other failure class. Imported lazily: the
            # runtime package's __init__ pulls in backend_role_adapters, which
            # imports this module, so a top-level import would be circular.
            last_error = GenerationError(
                role,
                GenerationErrorCode.SESSION_ABORT,
                f"whole-chapter attempt {attempt + 1}/{retry.max_attempts}: {exc}",
            )
            _emit_reasoning(attempt)
            _notify_retry(attempt + 1, "abort")
            if attempt < retry.max_attempts - 1:
                time.sleep(retry.delay_for(attempt))
                continue
            break
        except _JsonResilienceErrorTypes() as exc:
            # RAW-SINK: the transport returned text, but classify rejected
            # it (TruncatedJSONError). The raw survives in the caller's
            # ``last_raw`` (captured in _complete BEFORE classification) —
            # persist it so the disk trail exists for diagnosis. Best-effort.
            if raw_sink is not None:
                try:
                    fallback_raw = getattr(model_caller, "last_raw", None)
                    if fallback_raw:
                        raw_sink(attempt, fallback_raw)
                except Exception:  # noqa: BLE001 — diagnostics hook
                    LOG.debug("whole-chapter raw_sink (fallback) failed", exc_info=True)
            # A2 review fix: ``BackendModelCaller`` re-raises
            # EmptyResponseError/TruncatedJSONError after ITS OWN bounded JSON
            # retry budget is exhausted. Those are ValueError subclasses, not
            # CompletionError, so they used to escape this loop and crash the
            # run instead of producing an honest incomplete_generation. Now
            # they are classified as INVALID_JSON (empty/truncated body) inside
            # the whole-chapter bounded retry: total attempts stay bounded by
            # retry.max_attempts, no partial translation is accepted, and the
            # failure is recorded truthfully.
            last_error = GenerationError(
                role,
                GenerationErrorCode.INVALID_JSON,
                f"whole-chapter attempt {attempt + 1}/{retry.max_attempts}: {exc}",
            )
            _emit_reasoning(attempt)
            _notify_retry(attempt + 1, "truncated")
            if attempt < retry.max_attempts - 1:
                time.sleep(retry.delay_for(attempt))
                continue
            break
        finally:
            # GEN-STREAM: the live sink is per-attempt — clear it as soon as
            # the model call returns (success, abort, or truncated) so the
            # next attempt (or a later caller user) never appends into the
            # previous attempt's file. The authoritative post-completion
            # reasoning_sink write is unaffected by this clear.
            _clear_live_reasoning()

        _emit_reasoning(attempt)

        try:
            translation = validate_whole_chapter_raw(raw, pid_map)
        except ValueError as exc:
            last_error = GenerationError(
                role,
                GenerationErrorCode.INVALID_JSON,
                f"whole-chapter attempt {attempt + 1}/{retry.max_attempts}: {exc}",
            )
            _notify_retry(attempt + 1, "malformed")
        except _GenerationValidationError as exc:
            last_error = GenerationError(
                role,
                exc.code,
                f"whole-chapter attempt {attempt + 1}/{retry.max_attempts}: {exc.detail}",
            )
            # The A1 contract retries every PID-contract violation
            # (missing/extra/reordered/duplicate) with the same "missing_pid"
            # reason; CONTEXT_LEAKAGE cannot fire here (whole-chapter has no
            # context PIDs) but falls back to malformed for completeness.
            _notify_retry(
                attempt + 1,
                "missing_pid"
                if exc.code is GenerationErrorCode.PID_MISMATCH
                else "malformed",
            )
        else:
            candidate = Candidate.create(
                candidate_id=f"whole_chapter:{role}:{bundle.bundle_hash[:16]}",
                chunk_id="whole_chapter",
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
                whole_chapter_pid_map=pid_map,
            )
            result = GenerationCandidateResult(candidate=candidate, error=None)
            cache.put(bundle.bundle_hash, result)
            return _whole_chapter_outcome(role=role, risk=risk, result=result)

        if attempt < retry.max_attempts - 1:
            time.sleep(retry.delay_for(attempt))
            continue
        break

    assert last_error is not None
    result = GenerationCandidateResult(candidate=None, error=last_error)
    cache.put(bundle.bundle_hash, result)
    return _whole_chapter_outcome(role=role, risk=risk, result=result)


def _whole_chapter_outcome(
    *,
    role: str,
    risk: RiskAssessment,
    result: GenerationCandidateResult,
) -> GenerationOutcome:
    """Render one whole-chapter generation result as a ``GenerationOutcome``."""
    if result.candidate is not None:
        return GenerationOutcome(
            chunk_id="whole_chapter",
            risk_band=risk.band.value,
            expected_roles=(role,),
            candidates={role: result.candidate},
            errors={},
            status="complete",
        )
    assert result.error is not None
    return GenerationOutcome(
        chunk_id="whole_chapter",
        risk_band=risk.band.value,
        expected_roles=(role,),
        candidates={},
        errors={role: result.error},
        status="incomplete",
    )
