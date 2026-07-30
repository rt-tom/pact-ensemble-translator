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
    consistency, over every PID. Reuses the pure helper functions and
    ``DeterministicGateData`` already built and tested in
    ``pact_v4.phase2.cascade`` (imported directly, not duplicated) — that
    module's gate operates per-candidate pre-selection; this one applies the
    same checks chapter-wide, post-selection.

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
``(chunk_id, detector)`` identity hash; a cached *successful* unit is reused
untouched on resume (never re-called), while a missing or previously failed
unit is (re)attempted. ``AuditOutcome.status`` is ``"complete"`` if and only
if every unit succeeded — a model failure can never be silently read as "no
issues found".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Protocol, Tuple

from pact_v4.phase1.models import (
    Candidate,
    ChunkPlanArtifact,
    Region,
    SourceArtifact,
    canonical_json_hash,
    validate_json_complete,
)
from pact_v4.phase2.cascade import (
    DeterministicGateData,
    _extract_digits,
    _find_mixed_script,
    _missing_numeric_values,
    _source_term_present,
    _target_form_present,
)
from pact_v4.phase3.assembly import AssembledChapter
from pact_v4.phase3.findings import Finding, FindingStore
from pact_v4.phase3.region_resolver import RegionPlan, resolve_regions

__all__ = [
    "QwenAuditEvaluator",
    "GemmaAuditEvaluator",
    "AuditUnitResult",
    "AuditCache",
    "AuditOutcome",
    "QWEN_AUDIT_CATEGORIES",
    "GEMMA_AUDIT_CATEGORIES",
    "run_chapter_audit",
]

QWEN_AUDIT_CATEGORIES = frozenset({"omission", "addition", "referent", "scene"})
GEMMA_AUDIT_CATEGORIES = frozenset({"calque", "register", "repetition", "dialogue", "ty_vy"})

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


def _unit_hash(*, chapter_hash: str, chunk_id: str, detector: str, policy_version: str) -> str:
    return canonical_json_hash({
        "artifact": "pact-v4-audit-unit/v1",
        "chapter_hash": chapter_hash,
        "chunk_id": chunk_id,
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

    all_terms: Dict[str, str] = {}
    for en_term, ru_term in det_data.glossary_terms:
        key = en_term.strip()
        if key and ru_term.strip():
            all_terms[key] = ru_term.strip()
    for en_name, ru_name in det_data.names:
        key = en_name.strip()
        if key and ru_name.strip():
            all_terms[key] = ru_name.strip()

    findings: List[Finding] = []
    for chunk in chunk_plan.chunks:
        candidate_id = candidates[chunk.chunk_id].candidate_id
        for pid in chunk.pids:
            en_text = source_map.get(pid, "")
            target = chapter_map.get(pid, "")

            def _add(category: str, problem: str, end: int) -> None:
                findings.append(Finding(
                    detector="deterministic_integrity",
                    category=category,
                    evidence={"problem": problem},
                    region=Region(pid=pid, start=0, end=end),
                    source_id=chapter.source_hash,
                    snapshot_id=chapter.snapshot_hash,
                    chunk_id=chunk.chunk_id,
                    candidate_id=candidate_id,
                    policy_version=policy_version,
                ))

            if not target:
                _add("missing", "Translation is empty or missing.", 0)
                continue

            source_digits = _extract_digits(en_text)
            if source_digits:
                missing = _missing_numeric_values(source_digits, target)
                if missing:
                    _add("number", f"Missing numeric values: {missing}", len(target))

            mixed = _find_mixed_script(target, det_data.mixed_script_allow)
            if mixed:
                _add("mixed_script", f"Latin or mixed-script token(s): {mixed}", len(target))

            for en_term, ru_term in all_terms.items():
                if _source_term_present(en_text, en_term) and not _target_form_present(target, ru_term):
                    _add("glossary_consistency", f"'{en_term}' should use '{ru_term}'", len(target))

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

    Every chunk is audited by both Qwen and Gemma as one resumable unit each
    (four units total across two detectors would be wrong; it's one Qwen
    unit + one Gemma unit per chunk). A unit whose cached result has
    ``ok=True`` is reused without calling the evaluator again; any other
    unit (uncached, or a previous failure) is attempted, and the attempt's
    outcome — success or failure — is written back to the cache before
    moving on, so a subsequent call with the same cache only re-attempts
    what's still outstanding.
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

    for chunk in chunk_plan.chunks:
        candidate_id = candidates[chunk.chunk_id].candidate_id
        owned_pids = frozenset(chunk.pids)
        owned_source = {pid: source_map.get(pid, "") for pid in chunk.pids}
        owned_translation = {pid: chapter_map.get(pid, "") for pid in chunk.pids}

        units: Tuple[Tuple[str, str], ...] = (
            ("qwen_chapter_audit", qwen_policy_version),
            ("gemma_russian_review", gemma_policy_version),
        )
        for detector, policy_version in units:
            unit_hash = _unit_hash(
                chapter_hash=chapter.chapter_hash,
                chunk_id=chunk.chunk_id,
                detector=detector,
                policy_version=policy_version,
            )
            cached = cache.get(unit_hash)
            if cached is not None and cached.ok:
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
