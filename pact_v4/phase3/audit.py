"""Phase 3B: the assembled-chapter audit ("Step 6" in V4_MVP_SPEC_RU.md §2).

Canonical source: docs/architecture/V4_FINAL_REVIEW_AND_IMPLEMENTATION_PLAN_RU_v2.md
("### 3B. One full audit"):

    Qwen EN↔RU, Gemma RU-only, deterministic integrity/formatting/HTML. Full
    PID coverage, resumable partial units, audit cannot claim complete on
    model failure.

and V4_MVP_SPEC_RU.md §2 Step 6:

    Qwen: source ↔ translation (пропуски/добавления/референты/сцена)
    Gemma: Russian-only review (кальки, регистр, повторы, диалог, ты/вы)
           — коррелированный сигнал (та же модель, что генерировала текст)
    (deterministic layer): PID coverage / numbers / mixed-script / glossary /
           names / formatting contract / HTML structure

This module runs once per assembled chapter (``pact_v4.phase3.assembly.
AssembledChapter``) and produces ``pact_v4.phase3.findings.Finding``
objects into one ``FindingStore``.

Three check tracks:

  * ``QwenAuditEvaluator`` — EN<->RU fidelity: omissions/additions/referents/
    scene, per chunk.
  * ``GemmaAuditEvaluator`` — Russian-only review (never shown the English
    source, per spec "Russian-only review без оригинала"): calques/
    register/repetition/dialogue/ты-вы, per chunk.
  * ``_deterministic_chapter_findings`` — model-free: missing translation,
    numeric-value preservation, mixed-script tokens, and glossary/name
    consistency, over every PID. Reuses the pure helper functions in
    ``pact_v4._integrity_checks`` (a public, versioned utility module shared
    with ``pact_v4.phase2.cascade``'s ``deterministic_consistency_gate`` —
    that gate operates per-candidate pre-selection; this one applies the
    same checks chapter-wide, post-selection) and the ``DeterministicGateData``
    dataclass already built and tested in ``pact_v4.phase2.cascade``.

Explicitly NOT implemented here: formatting-contract / HTML-structure
checks. No v4 runtime formatting/HTML artifact exists yet — ``pact_v4.
phase0b`` is the read-only *measurement* harness (Phase 0), not the runtime
formatting-alignment module (Phase 5, not yet built; see
V4_FINAL_REVIEW_AND_IMPLEMENTATION_PLAN_RU_v2.md "Phase 5 — formatting
alignment"). Building that logic here would mean inventing a contract that
doesn't correspond to any real artifact — left as an explicit follow-up once
Phase 5 lands, not stubbed.

Also explicitly out of scope: repair execution, challenge/dispute flow,
convergence, and terminal-state transitions (Phase 4).

Neither evaluator ``Protocol`` accepts a context-window parameter (left_ru/
right_en PID counts): that is one of the Phase 0C benchmark-gate values that
is not yet frozen. This module, like Phase 3A, does not depend on any
benchmark-gate value.

Resumability and the "cannot claim complete on model failure" guard mirror
``pact_v4.phase2.generation``'s ``ModelCaller``/``GenerationCache``/
``GenerationOutcome.status`` shape exactly: ``AuditCache`` is a plain
in-memory exact-match cache (no disk I/O — persisting/reloading it across
process restarts is the pipeline's job), keyed by a deterministic per-
``(chunk_id, candidate_id, detector, policy_version)`` identity hash over
``chapter_hash`` — ``candidate_id`` is included precisely because two
different winning candidates for the same chunk can produce identical
output text (and therefore the same ``chapter_hash``); a cache hit's
findings are also revalidated against the requested ``candidate_id`` before
being reused, the same defense-in-depth ``GenerationCache`` applies to its
own hits. A cached *successful* unit is reused untouched on resume (never
re-called), while a missing or previously failed unit is (re)attempted.
``AuditOutcome.status`` is ``"complete"`` if and only if every unit
succeeded — a model failure can never be silently read as "no issues
found".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Protocol, Tuple

from pact_v4._integrity_checks import (
    combine_glossary_terms,
    extract_digits,
    find_mixed_script,
    missing_numeric_values,
    source_term_present,
    target_form_present,
)
from pact_v4.phase1.models import (
    Candidate,
    ChunkPlanArtifact,
    Region,
    SourceArtifact,
    canonical_json_hash,
    validate_json_complete,
    _require_exact_keys,
)
from pact_v4.phase2.cascade import DeterministicGateData
from pact_v4.phase3.assembly import AssembledChapter
from pact_v4.phase3.findings import Finding, FindingStore
from pact_v4.phase3.region_resolver import RegionPlan, resolve_regions

__all__ = [
    "QwenAuditEvaluator",
    "GemmaAuditEvaluator",
    "AuditUnitResult",
    "AuditCache",
    "AuditOutcome",
    "NO_CANDIDATE_MARKER",
    "QWEN_AUDIT_CATEGORIES",
    "GEMMA_AUDIT_CATEGORIES",
    "run_chapter_audit",
]

QWEN_AUDIT_CATEGORIES = frozenset({"omission", "addition", "referent", "scene"})
GEMMA_AUDIT_CATEGORIES = frozenset({"calque", "register", "repetition", "dialogue", "ty_vy"})

# Tagged onto deterministic ``missing`` findings for a chunk that has no
# auditable candidate at all (quarantined without a recoverable variant,
# needs_synthesis, incomplete_generation, or simply never processed). It is
# the honest marker of a coverage gap — no fabricated ``candidate_id`` is
# ever attached to a finding for text that was never produced.
NO_CANDIDATE_MARKER = "<no-candidate>"

_ISSUE_REQUIRED_KEYS = {"pid", "category", "note"}
_ISSUE_ALLOWED_KEYS = _ISSUE_REQUIRED_KEYS | {"excerpt"}


# ---------------------------------------------------------------------------
# Model call interfaces (injectable; no default HTTP implementation)
# ---------------------------------------------------------------------------


class QwenAuditEvaluator(Protocol):
    """Qwen: source <-> translation fidelity check for one chunk.

    Receives the chunk's own owned English source and Russian translation
    (PID -> text mappings, restricted to that chunk's owned PIDs — no
    neighbouring context). Returns raw text expected to be a JSON object
    ``{"issues": [...]}`` (see module docstring for the issue shape); this
    protocol knows nothing about HTTP — production wiring lives in the
    pipeline, not here.
    """

    def __call__(
        self, *, chunk_id: str, source: Mapping[str, str], translation: Mapping[str, str]
    ) -> str: ...


class GemmaAuditEvaluator(Protocol):
    """Gemma: Russian-only review for one chunk (NO English source).

    Per spec, "Russian-only review без оригинала" — this protocol is not
    given the source at all, so a caller cannot accidentally leak it.
    Returns raw text expected to be a JSON object ``{"issues": [...]}``.
    """

    def __call__(self, *, chunk_id: str, translation: Mapping[str, str]) -> str: ...


# ---------------------------------------------------------------------------
# Resumable per-(chunk, detector) unit cache
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditUnitResult:
    """Outcome of one (chunk, detector) audit unit.

    ``ok=False`` means the unit must be retried on resume (model call
    raised, or its output failed parsing/validation) — it is never treated
    as "zero findings".
    """

    ok: bool
    findings: Tuple[Finding, ...] = ()
    error: str = ""


class _AuditCachePoisoned(AssertionError):
    """Raised if a cache hit's findings don't match the identity that
    produced its cache key — mirrors ``pact_v4.phase2.generation.
    _CachePoisoned``: this indicates an internal bug (e.g. a unit-hash
    change that dropped an identity field, or a caller writing into
    ``AuditCache`` directly), never an expected runtime path, hence
    ``AssertionError`` rather than a typed/recoverable error."""


class AuditCache:
    """Exact-match in-memory cache keyed on a deterministic unit identity hash.

    Mirrors ``pact_v4.phase2.generation.GenerationCache``: no disk I/O here
    — persistence across process restarts is the caller/pipeline's
    responsibility. A resumed run that passes the same populated cache back
    in will skip every unit that previously succeeded and retry only the
    ones that didn't.
    """

    def __init__(self) -> None:
        self._store: Dict[str, AuditUnitResult] = {}

    def get(self, unit_hash: str) -> Optional[AuditUnitResult]:
        return self._store.get(unit_hash)

    def put(self, unit_hash: str, result: AuditUnitResult) -> None:
        self._store[unit_hash] = result

    def to_payload(self) -> Dict[str, Any]:
        """Serialisable round-trip form for the pipeline's on-disk cache.

        Persisting/reloading across process restarts is the pipeline's job
        (see the module docstring); this gives the strict driver exactly
        what it needs to do that: an ordered list of ``{unit_hash, ok,
        error, findings}`` records.
        """
        return {
            "schema": "pact-v4-audit-cache/v1",
            "units": [
                {
                    "unit_hash": unit_hash,
                    "ok": result.ok,
                    "error": result.error,
                    "findings": [finding.to_payload() for finding in result.findings],
                }
                for unit_hash, result in sorted(self._store.items())
            ],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "AuditCache":
        """Rebuild a cache from ``to_payload`` output.

        The strict driver verifies the enclosing artifact's run identities
        (chapter/snapshot/plan/config/backend) before feeding this back in,
        so a cache written under a different run cannot be mixed into this
        one.
        """
        if payload.get("schema") != "pact-v4-audit-cache/v1":
            raise ValueError(
                f"Foreign identity: audit-cache schema={payload.get('schema')!r}"
            )
        units = payload.get("units")
        if not isinstance(units, list):
            raise ValueError("AuditCache payload: units must be an array")
        cache = cls()
        for item in units:
            if not isinstance(item, Mapping):
                raise ValueError("AuditCache payload: unit entries must be JSON objects")
            _require_exact_keys(item, {"unit_hash", "ok", "error", "findings"}, "AuditCache unit")
            findings = item["findings"]
            if not isinstance(findings, list):
                raise ValueError("AuditCache payload: unit findings must be an array")
            cache.put(
                item["unit_hash"],
                AuditUnitResult(
                    ok=bool(item["ok"]),
                    findings=tuple(Finding.from_payload(f) for f in findings),
                    error=str(item["error"]),
                ),
            )
        return cache


def _candidate_id_for(chunk_id: str, candidates: Mapping[str, Candidate]) -> str:
    """Resolve the ``candidate_id`` tagged onto findings for one chunk.

    A chunk missing from the map (no auditable candidate — e.g. a
    quarantined chunk without a recoverable variant, or a chunk that was
    never processed) is tagged with ``NO_CANDIDATE_MARKER`` instead of
    raising: the deterministic layer must still honestly mark its PIDs as
    uncovered, and a fabricated ``candidate_id`` would misrepresent
    provenance. Model units are skipped for such chunks (see
    ``run_chapter_audit``), so this marker only ever appears on
    deterministic findings.
    """
    candidate = candidates.get(chunk_id)
    if candidate is None:
        return NO_CANDIDATE_MARKER
    return candidate.candidate_id


def _unit_hash(
    *, chapter_hash: str, chunk_id: str, candidate_id: str, detector: str, policy_version: str
) -> str:
    """Identity of one resumable (chunk, detector) audit unit.

    ``candidate_id`` is part of the identity, not just ``chapter_hash``:
    two different winning ``Candidate``s for the same chunk can produce
    byte-identical ``translation`` text (and therefore the same
    ``chapter_hash``) while still being different generation events with
    different provenance. Keying only on ``chapter_hash`` would let a
    resumed run silently reuse cached findings tagged with a stale
    ``candidate_id`` after the winning candidate changed but its output
    text happened not to.
    """
    return canonical_json_hash({
        "artifact": "pact-v4-audit-unit/v2",
        "chapter_hash": chapter_hash,
        "chunk_id": chunk_id,
        "candidate_id": candidate_id,
        "detector": detector,
        "policy_version": policy_version,
    })


# ---------------------------------------------------------------------------
# Issue parsing (strict: reject partial/foreign/unknown, never best-effort)
# ---------------------------------------------------------------------------


def _parse_issues(
    raw: str,
    *,
    owned_pids: frozenset,
    allowed_categories: frozenset,
) -> List[Dict[str, Any]]:
    payload = validate_json_complete(raw)
    issues = payload.get("issues")
    if not isinstance(issues, list):
        raise ValueError("Audit response: 'issues' must be a JSON array")

    parsed: List[Dict[str, Any]] = []
    for index, item in enumerate(issues):
        if not isinstance(item, dict):
            raise ValueError(f"Audit response: issues[{index}] must be a JSON object")
        keys = set(item)
        missing = _ISSUE_REQUIRED_KEYS - keys
        unexpected = keys - _ISSUE_ALLOWED_KEYS
        if missing or unexpected:
            raise ValueError(
                f"Audit response: issues[{index}] keys invalid; "
                f"missing={sorted(missing)}, unexpected={sorted(unexpected)}"
            )
        pid = item["pid"]
        category = item["category"]
        note = item["note"]
        if not isinstance(pid, str) or not pid:
            raise ValueError(f"Audit response: issues[{index}].pid must be a non-empty string")
        if pid not in owned_pids:
            raise ValueError(
                f"Audit response: issues[{index}] references pid {pid!r} outside "
                f"the queried chunk's owned PIDs"
            )
        if category not in allowed_categories:
            raise ValueError(
                f"Audit response: issues[{index}].category {category!r} is not "
                f"one of {sorted(allowed_categories)}"
            )
        if not isinstance(note, str) or not note:
            raise ValueError(f"Audit response: issues[{index}].note must be a non-empty string")
        excerpt = item.get("excerpt", "")
        if not isinstance(excerpt, str):
            raise ValueError(f"Audit response: issues[{index}].excerpt must be a string")
        parsed.append({"pid": pid, "category": category, "note": note, "excerpt": excerpt})
    return parsed


def _findings_from_issues(
    issues: List[Dict[str, Any]],
    *,
    detector: str,
    chapter: AssembledChapter,
    chunk_id: str,
    candidate_id: str,
    policy_version: str,
) -> Tuple[Finding, ...]:
    """Convert parsed Qwen/Gemma issues into ``Finding``s.

    Known MVP simplification: Qwen/Gemma aren't given character offsets, so
    every finding's region defaults to the whole PID span, ``Region(pid, 0,
    len(text))`` — same convention used by the deterministic layer
    (``_deterministic_finding``). A consequence, intentional per Phase 3A's
    region-resolver contract: a zero-length span (an empty/missing
    translation, ``len(text) == 0``) is adjacent to (touches) any other
    zero-length finding on that same PID, so ``resolve_regions`` groups them
    into one coverage region — it still keeps every finding's own evidence
    distinct, it only merges the *coverage span*, exactly like any other
    same-PID overlap/adjacency.
    """
    chapter_map = chapter.as_pid_map()
    findings = []
    for issue in issues:
        pid = issue["pid"]
        text = chapter_map.get(pid, "")
        findings.append(Finding(
            detector=detector,
            category=issue["category"],
            evidence={"note": issue["note"], "excerpt": issue["excerpt"]},
            region=Region(pid=pid, start=0, end=len(text)),
            source_id=chapter.source_hash,
            snapshot_id=chapter.snapshot_hash,
            chunk_id=chunk_id,
            candidate_id=candidate_id,
            policy_version=policy_version,
        ))
    return tuple(findings)


# ---------------------------------------------------------------------------
# Deterministic layer (model-free; run fresh every time, never cached)
# ---------------------------------------------------------------------------


def _deterministic_finding(
    *,
    chapter: AssembledChapter,
    chunk_id: str,
    candidate_id: str,
    pid: str,
    category: str,
    problem: str,
    end: int,
    policy_version: str,
) -> Finding:
    return Finding(
        detector="deterministic_integrity",
        category=category,
        evidence={"problem": problem},
        region=Region(pid=pid, start=0, end=end),
        source_id=chapter.source_hash,
        snapshot_id=chapter.snapshot_hash,
        chunk_id=chunk_id,
        candidate_id=candidate_id,
        policy_version=policy_version,
    )


def _deterministic_chapter_findings(
    *,
    chapter: AssembledChapter,
    source: SourceArtifact,
    chunk_plan: ChunkPlanArtifact,
    candidates: Mapping[str, Candidate],
    det_data: DeterministicGateData,
    policy_version: str,
) -> Tuple[Finding, ...]:
    source_map = dict(source.source)
    chapter_map = dict(chapter.translation)
    all_terms = combine_glossary_terms(det_data.glossary_terms, det_data.names)

    findings: List[Finding] = []
    for chunk in chunk_plan.chunks:
        candidate_id = _candidate_id_for(chunk.chunk_id, candidates)
        for pid in chunk.pids:
            en_text = source_map.get(pid, "")
            target = chapter_map.get(pid, "")

            if not target:
                findings.append(_deterministic_finding(
                    chapter=chapter, chunk_id=chunk.chunk_id, candidate_id=candidate_id,
                    pid=pid, category="missing", problem="Translation is empty or missing.",
                    end=0, policy_version=policy_version,
                ))
                continue

            source_digits = extract_digits(en_text)
            if source_digits:
                missing = missing_numeric_values(source_digits, target)
                if missing:
                    findings.append(_deterministic_finding(
                        chapter=chapter, chunk_id=chunk.chunk_id, candidate_id=candidate_id,
                        pid=pid, category="number", problem=f"Missing numeric values: {missing}",
                        end=len(target), policy_version=policy_version,
                    ))

            mixed = find_mixed_script(target, det_data.mixed_script_allow)
            if mixed:
                findings.append(_deterministic_finding(
                    chapter=chapter, chunk_id=chunk.chunk_id, candidate_id=candidate_id,
                    pid=pid, category="mixed_script",
                    problem=f"Latin or mixed-script token(s): {mixed}",
                    end=len(target), policy_version=policy_version,
                ))

            for en_term, ru_term in all_terms.items():
                if source_term_present(en_text, en_term) and not target_form_present(target, ru_term):
                    findings.append(_deterministic_finding(
                        chapter=chapter, chunk_id=chunk.chunk_id, candidate_id=candidate_id,
                        pid=pid, category="glossary_consistency",
                        problem=f"'{en_term}' should use '{ru_term}'",
                        end=len(target), policy_version=policy_version,
                    ))

    return tuple(findings)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditOutcome:
    """Result of one Step 6 assembled-chapter audit run.

    ``status`` is ``"complete"`` if and only if every (chunk, detector) unit
    across both Qwen and Gemma succeeded — never inferred from "no findings
    were produced". ``failed_units`` is exactly what a resumed run needs to
    retry: rerun with the same populated ``AuditCache`` and only these units
    will actually call a model again.
    """

    chapter_hash: str
    status: str
    store: FindingStore
    region_plan: RegionPlan
    failed_units: Tuple[Tuple[str, str, str], ...] = ()


def run_chapter_audit(
    *,
    chapter: AssembledChapter,
    source: SourceArtifact,
    chunk_plan: ChunkPlanArtifact,
    candidates: Mapping[str, Candidate],
    qwen_evaluator: QwenAuditEvaluator,
    gemma_evaluator: GemmaAuditEvaluator,
    det_data: DeterministicGateData = DeterministicGateData(),
    qwen_policy_version: str = "qwen_chapter_audit/v1",
    gemma_policy_version: str = "gemma_russian_review/v1",
    deterministic_policy_version: str = "deterministic_integrity/v1",
    cache: Optional[AuditCache] = None,
) -> AuditOutcome:
    """Run the Step 6 assembled-chapter audit (Qwen + Gemma + deterministic).

    ``candidates`` must be the same ``chunk_id -> winning Candidate`` mapping
    used to build ``chapter`` (``pact_v4.phase3.assembly.AssembledChapter.
    assemble``) — it supplies the ``candidate_id`` identity tagged onto every
    finding raised against that chunk.

    Since the B1 follow-up (owner decision 2026-08-02, "audit all chunks,
    best-variant for quarantine"), ``candidates`` is allowed to be **partial**:
    a chunk missing from the map is not a contract error. Such a chunk is
    skipped by both model tracks (no Qwen/Gemma unit is created or attempted)
    while the deterministic layer still covers it — every PID of the chunk
    gets a ``missing`` finding tagged with ``NO_CANDIDATE_MARKER``, so the
    audit honestly fixes the gap instead of silently narrowing its scope.
    The model track's status semantics are unchanged: ``"complete"`` if and
    only if every *attempted* unit succeeded.

    Every chunk that *has* a candidate is audited by both Qwen and Gemma as
    one resumable unit each (four units total across two detectors would be
    wrong; it's one Qwen unit + one Gemma unit per chunk). A unit whose
    cached result has ``ok=True`` is reused without calling the evaluator
    again; any other unit (uncached, or a previous failure) is attempted, and
    the attempt's outcome — success or failure — is written back to the cache
    before moving on, so a subsequent call with the same cache only
    re-attempts what's still outstanding.
    """
    if cache is None:
        cache = AuditCache()

    deterministic_findings = _deterministic_chapter_findings(
        chapter=chapter,
        source=source,
        chunk_plan=chunk_plan,
        candidates=candidates,
        det_data=det_data,
        policy_version=deterministic_policy_version,
    )

    source_map = dict(source.source)
    chapter_map = chapter.as_pid_map()

    all_findings: List[Finding] = list(deterministic_findings)
    failed_units: List[Tuple[str, str, str]] = []

    # Detector-outer, chunk-inner iteration (DECISIONS.md, 2026-08-01): the
    # audit of one chunk does not depend on the audit of any other chunk
    # (the whole chapter is already assembled), so batching per detector —
    # all Qwen units across the chapter, then all Gemma units — is correct
    # and lets a single-resident driver pay ~1-2 model switches for the
    # whole phase instead of ~2N. Unit identity is unchanged (per
    # (chunk_id, detector) unit_hash), so the iteration order is purely a
    # scheduling decision; cache/resume semantics are identical either way.
    units: Tuple[Tuple[str, str], ...] = (
        ("qwen_chapter_audit", qwen_policy_version),
        ("gemma_russian_review", gemma_policy_version),
    )
    for detector, policy_version in units:
        for chunk in chunk_plan.chunks:
            candidate = candidates.get(chunk.chunk_id)
            if candidate is None:
                # No auditable candidate for this chunk (owner decision
                # 2026-08-02): no model unit is created or attempted. The
                # deterministic layer already marked every PID of the chunk
                # ``missing`` with the ``NO_CANDIDATE_MARKER``, so the gap
                # stays visible in the findings store without a fabricated
                # candidate identity.
                continue
            candidate_id = candidate.candidate_id
            owned_pids = frozenset(chunk.pids)
            owned_source = {pid: source_map.get(pid, "") for pid in chunk.pids}
            owned_translation = {pid: chapter_map.get(pid, "") for pid in chunk.pids}

            unit_hash = _unit_hash(
                chapter_hash=chapter.chapter_hash,
                chunk_id=chunk.chunk_id,
                candidate_id=candidate_id,
                detector=detector,
                policy_version=policy_version,
            )
            cached = cache.get(unit_hash)
            if cached is not None and cached.ok:
                # Defense in depth, mirroring generation.py's GenerationCache
                # hit-revalidation: never trust the hash alone. A finding
                # whose tagged candidate_id doesn't match what was actually
                # requested here would misrepresent provenance even though
                # the cache key matched.
                for finding in cached.findings:
                    if finding.candidate_id != candidate_id:
                        raise _AuditCachePoisoned(
                            f"Cache identity corruption: unit_hash {unit_hash} "
                            f"resolved to candidate_id={finding.candidate_id!r}, "
                            f"expected {candidate_id!r}"
                        )
                all_findings.extend(cached.findings)
                continue

            try:
                if detector == "qwen_chapter_audit":
                    raw = qwen_evaluator(
                        chunk_id=chunk.chunk_id, source=owned_source, translation=owned_translation
                    )
                    allowed_categories = QWEN_AUDIT_CATEGORIES
                else:
                    raw = gemma_evaluator(chunk_id=chunk.chunk_id, translation=owned_translation)
                    allowed_categories = GEMMA_AUDIT_CATEGORIES

                issues = _parse_issues(raw, owned_pids=owned_pids, allowed_categories=allowed_categories)
                unit_findings = _findings_from_issues(
                    issues,
                    detector=detector,
                    chapter=chapter,
                    chunk_id=chunk.chunk_id,
                    candidate_id=candidate_id,
                    policy_version=policy_version,
                )
                result = AuditUnitResult(ok=True, findings=unit_findings)
            except Exception as exc:  # model call failure or output validation failure
                result = AuditUnitResult(ok=False, error=str(exc))

            cache.put(unit_hash, result)
            if result.ok:
                all_findings.extend(result.findings)
            else:
                failed_units.append((chunk.chunk_id, detector, result.error))

    store = FindingStore.create(chapter.snapshot_hash, all_findings)
    region_plan = resolve_regions(store)
    status = "complete" if not failed_units else "incomplete"

    return AuditOutcome(
        chapter_hash=chapter.chapter_hash,
        status=status,
        store=store,
        region_plan=region_plan,
        failed_units=tuple(failed_units),
    )
