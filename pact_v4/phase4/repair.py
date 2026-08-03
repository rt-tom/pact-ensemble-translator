"""Phase 4: repair, Gemma closure, convergence, terminal state (B2).

Canonical source:

  * docs/architecture/V4_FINAL_REVIEW_AND_IMPLEMENTATION_PLAN_RU_v2.md
    ("## Phase 4 — repair, convergence, terminal state": 4A minimal repair,
    4A2 Gemma finding closure, 4B targeted convergence, monotonic terminal
    transition);
  * docs/architecture/V4_MVP_SPEC_RU.md (§2 Step 7 targeted repair, Step 8
    final integrity check; §7 final states).

This module implements the *algorithm* of Phase 4A/4A2/4B over the
``b2_handoff.json`` input contract (B1-followup) and the Step 6 findings.
Every model call is injected through backend-neutral protocols
(``RepairCaller`` / ``QwenEvaluator`` / ``GemmaAuditEvaluator`` /
``QwenAuditEvaluator``) — the strict driver wires them from
``pact_v4.runtime.runtime_config.build_role_adapters``-style Backend
adapters over the coordinator ``CompletionBackend``, so repair runs
identically in local, remote and composite profiles without retrofitting.
This module deliberately never imports ``pact_v4.runtime.model_lifecycle``
/ ``model_lifecycle_adapters`` / ``ModelRouter`` (dual-mode rule; an import
guard test enforces it).

Key rules implemented here (from DECISIONS 2026-08-01/02 and the plan):

  * Repair is exact finding-linked region/PID repair (region resolver);
    ``full_sentence_rewrite`` requires a documented reason and is only
    chosen when a ``region_edit`` failed the re-gate (local minimal repair
    shown impossible first).
  * A repair can never auto-accept a challenge; a challenge requires
    evidence (``models.Repair`` already rejects ``auto_accepted=True``;
    ``plan_repair`` additionally requires ``challenge_evidence`` for a
    challenged finding).
  * After a repair, the relevant gates pass: deterministic consistency +
    Qwen re-gate. **Qwen re-gate failure never commits the repair.** The
    re-gate is *narrow* (``region_fidelity_gate``: the edited PID + region
    only, not the whole chunk); unedited PIDs are covered by the
    convergence re-audit (L2b, DECISIONS 2026-08-03).
  * If the repair closes a finding raised by Gemma Russian review
    (Step 6), a Gemma re-check of the region is **mandatory**; a failed
    re-check leaves the Russian finding open and returns the last admitted
    text as degraded availability.
  * Each round runs as four role passes (L2b): all Gemma edits, then all
    narrow Qwen re-gates, then all mandatory Gemma re-checks, then the
    deferred commit. ``repair_id`` and cache unit identity are unchanged.
  * The convergence re-audit batches by detector (L1): deterministic layer
    per chunk, model tracks detector-outer / chunk-inner, findings set
    order-independent (canonicalised by ``content_hash``).
  * Weak-evidence soft Gemma findings (``calque``/``register`` with a short
    excerpt and/or an uncertain note) are skipped from repair planning and
    are not blocking in round 2 (L3); they stay in the store and are
    recorded in the debt trace.
  * One repair round is mandatory; a second is allowed only for a remaining
    blocking finding or a changed chunk boundary. Then the final integrity
    check (deterministic by default; narrow Qwen smoke only when text
    changed outside the Step 7 re-audit scope) and the monotonic terminal
    transition.
  * Terminal states: ``complete`` / ``accepted_degraded`` (valid structural
    PID-map + debt trace, no memory promotion) / ``failed`` (no valid
    PID-map). ``quarantined`` is an internal state, not terminal.
  * Transport failure / invalid structured output at a repair call is
    *incomplete/debt*, never a semantic terminal status ("transport failure
    != semantic gate failure"); there is no silent fallback.

Artifacts produced here are persisted by the strict driver
(``pact_v4.pipeline.v4_phase12_strict_runner``) with the same
chapter/snapshot/plan/config/backend identity rules as the Step 6 audit
artifacts, so repair history is resumable and foreign-identity checked.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Protocol, Sequence, Tuple

from pact_v4._integrity_checks import (
    combine_glossary_terms,
    extract_digits,
    find_mixed_script,
    missing_numeric_values,
    source_term_present,
    strip_inline_markup,
    target_form_present,
)
from pact_v4.phase1.models import (
    Candidate,
    ChunkPlan,
    ChunkPlanArtifact,
    ConfigArtifact,
    GateResult,
    Provenance,
    Region,
    Repair,
    Snapshot,
    SourceArtifact,
    TerminalState,
    canonical_json_hash,
    validate_json_complete,
)
from pact_v4.phase2.cascade import (
    DeterministicGateData,
    QwenEvaluator,
    deterministic_consistency_gate,
)
from pact_v4.phase3.assembly import AssembledChapter
from pact_v4.phase3.audit import (
    GEMMA_AUDIT_CATEGORIES,
    QWEN_AUDIT_CATEGORIES,
    GemmaAuditEvaluator,
    QwenAuditEvaluator,
    _findings_from_issues,
    _parse_issues,
)
from pact_v4.phase3.findings import Finding, FindingStore
from pact_v4.phase3.region_resolver import ResolvedRegion, resolve_regions

LOG = logging.getLogger(__name__)

__all__ = [
    "REPAIR_UNIT_SCHEMA",
    "REPAIR_REPORT_SCHEMA",
    "REPAIR_POLICY_VERSION",
    "QWEN_REAUDIT_POLICY_VERSION",
    "GEMMA_RECHECK_POLICY_VERSION",
    "DETERMINISTIC_INTEGRITY_POLICY_VERSION",
    "RepairCaller",
    "RegionFidelityEvaluator",
    "RepairPlan",
    "RepairCache",
    "RepairRecord",
    "RepairRoundResult",
    "RepairPhaseResult",
    "SoftFindingsPolicy",
    "filter_soft_findings",
    "plan_repairs_for_chunk",
    "repair_region",
    "run_repair_phase",
]

REPAIR_UNIT_SCHEMA = "pact-v4-phase4-repair-cache/v1"
REPAIR_REPORT_SCHEMA = "pact-v4-phase4-repair-report/v1"
REPAIR_POLICY_VERSION = "pact-v4-repair-policy/v1"
QWEN_REAUDIT_POLICY_VERSION = "qwen_convergence_reaudit/v1"
GEMMA_RECHECK_POLICY_VERSION = "gemma_russian_recheck/v1"
DETERMINISTIC_INTEGRITY_POLICY_VERSION = "deterministic_integrity/v1"

# A repair is treated as a *challenge* of a finding when the model output
# claims the finding is a false positive. Such a repair must carry explicit
# evidence (a documented reason), never be auto-accepted.
CHALLENGE_CATEGORIES = frozenset({"false_positive", "challenge"})

# L3 severity filter (owner decision + DECISIONS 2026-08-03): soft-category
# Gemma Russian-review findings with *weak evidence* are skipped from repair
# planning (the findings stay in the append-only store — only planning is
# filtered). Weak evidence = a short excerpt and/or an uncertain note
# formulation. These thresholds are policy parameters consumed by
# ``SoftFindingsPolicy``, never magic numbers inline in repair logic.
L3_SOFT_CATEGORIES = frozenset({"calque", "register"})
L3_WEAK_EXCERPT_MAX_LEN = 60
L3_WEAK_NOTE_MARKERS = (
    "sounds like", "might be", "likely", "possibly", "perhaps", "maybe",
    "похоже", "возможно", "вероятно", "кажется", "скорее всего", "наверное",
)


# ---------------------------------------------------------------------------
# Protocols (backend-neutral; the strict driver injects Backend adapters)
# ---------------------------------------------------------------------------


class RepairCaller(Protocol):
    """Produce a minimal repaired translation for a located region.

    Receives the chunk's owned source, its current translation, the located
    ``region`` and the serialized findings that must be fixed. Returns raw
    text expected to be a JSON object
    ``{"repaired": {pid: text}, "reason": "..."}`` (strict; no markdown, no
    commentary). This protocol knows nothing about HTTP — production wiring
    lives in the pipeline.
    """

    def __call__(
        self,
        *,
        chunk_id: str,
        source: Mapping[str, str],
        translation: Mapping[str, str],
        region: Any,
        findings: Sequence[Mapping[str, str]],
    ) -> str: ...


class RegionFidelityEvaluator(Protocol):
    """Narrow Qwen re-gate of one repaired region (PID-level fidelity).

    L2b: the Step 7 re-gate is scoped to the *edited region* instead of the
    whole chunk — only the edited PID's source text, its repaired Russian
    text, and the located region are shown, and a short JSON verdict is
    returned (rendered from the ``region_fidelity_gate`` prompt variant,
    parsed via ``_parse_qwen_verdict`` in the Backend adapter). Unedited
    PIDs are covered by the convergence re-audit. The verdict contract is
    the same ``GateResult`` the full-chunk fidelity reviewer returns, so a
    narrow verdict is directly comparable to a full one on a fixture.

    This protocol knows nothing about HTTP — production wiring lives in the
    pipeline (``BackendRegionFidelityGate`` over the coordinator
    ``CompletionBackend``), never a local lifecycle adapter.
    """

    def __call__(
        self, *, source_text: str, repaired_text: str, region: Any
    ) -> GateResult: ...


class FormattingStep(Protocol):
    """Phase 5 formatting applied between convergence (Step 7) and the final
    integrity check (Step 8).

    Receives the repaired chapter PID map and returns a
    ``pact_v4.phase5.formatting.FormattingOutcome``-shaped object
    (``formatted_text``, ``incidents``, ``to_payload()``). The strict driver
    injects a closure built over ``pact_v4.phase5.formatting.
    run_formatting_align`` whose model-fallback tier goes through
    ``BackendFormattingCaller`` over the coordinator ``CompletionBackend`` —
    never a local lifecycle adapter. The result must preserve the PID map
    (formatting is wrap-only by contract).
    """

    def __call__(
        self, *, translation: Mapping[str, str]
    ) -> Any: ...


# ---------------------------------------------------------------------------
# Repair plan (built from findings + region resolver; validated as models.Repair)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepairPlan:
    """One planned region repair for one chunk.

    ``repair`` is the ``pact_v4.phase1.models.Repair`` contract (finding-
    linked, target PIDs, action, instructions, documented
    ``full_sentence_reason`` for full rewrites, never ``auto_accepted``).
    ``region``/``findings`` retain the exact evidence trail the plan was
    built from so execution and provenance need no re-derivation.
    """

    repair: Repair
    region: ResolvedRegion
    findings: Tuple[Finding, ...]

    @property
    def chunk_id(self) -> str:
        return self.repair.chunk_id

    @property
    def finding_ids(self) -> Tuple[str, ...]:
        return self.repair.finding_ids


def _plan_repair_id(
    *,
    chunk_id: str,
    region: ResolvedRegion,
    finding_hashes: Tuple[str, ...],
    backend_identity_hash: str,
) -> str:
    """Deterministic, content-derived repair identity (stable across resume)."""
    return canonical_json_hash({
        "artifact": "pact-v4-repair-plan/v1",
        "chunk_id": chunk_id,
        "pid": region.pid,
        "start": region.start,
        "end": region.end,
        "finding_hashes": sorted(finding_hashes),
        "backend_identity_hash": backend_identity_hash,
    })


def _instructions_for(findings: Sequence[Finding], *, action: str) -> str:
    """Human-readable minimal-edit instruction derived from finding evidence."""
    lines = []
    for finding in findings:
        note = ""
        if isinstance(finding.evidence, Mapping):
            note = str(finding.evidence.get("note", ""))
        elif finding.evidence:
            note = str(finding.evidence)
        excerpt = ""
        if isinstance(finding.evidence, Mapping):
            excerpt = str(finding.evidence.get("excerpt", ""))
        lines.append(f"[{finding.detector}/{finding.category}] {note}".strip())
        if excerpt:
            lines[-1] += f" — {excerpt}"
    if action == "full_sentence_rewrite":
        lines.append(
            "A previous minimal region edit failed the re-gate, so the whole "
            "sentence must be rewritten to resolve the finding."
        )
    joined = " ".join(lines)
    return joined or "Resolve the finding with a minimal targeted edit."


def plan_repairs_for_chunk(
    *,
    chunk: ChunkPlan,
    findings: Sequence[Finding],
    current_text: Mapping[str, str],
    backend_identity_hash: str,
    action_override: str = "",
    full_sentence_reason: str = "",
) -> Tuple[RepairPlan, ...]:
    """Deterministically plan region repairs for one chunk.

    Findings are resolved into coverage regions by
    ``pact_v4.phase3.region_resolver`` (adjacent/overlapping findings on one
    PID group into one region; findings are never merged). Each region on a
    PID becomes one ``RepairPlan``:

      * ``target_pids`` is the region's own PID (minimal repair);
      * ``action`` is ``region_edit`` by default, escalated to
        ``full_sentence_rewrite`` only when ``action_override`` requests it
        and ``full_sentence_reason`` is documented (local minimal edit shown
        impossible first — e.g. a prior region_edit failed its re-gate);
      * findings contributing to the region become ``finding_ids``.

    ``current_text`` is the chunk's current translation PID map (used only
    to decide ``full_sentence_rewrite`` eligibility when the region spans the
    whole PID).
    """
    store = FindingStore.create(
        expected_snapshot_id=findings[0].snapshot_id if findings else "",
        findings=findings,
    )
    plan = resolve_regions(store)
    plans: list[RepairPlan] = []
    for region in plan.regions:
        region_findings = tuple(
            finding
            for finding in findings
            if finding.content_hash in region.finding_content_hashes
        )
        if not region_findings:
            continue
        finding_hashes = tuple(sorted(f.content_hash for f in region_findings))
        repair_id = _plan_repair_id(
            chunk_id=chunk.chunk_id,
            region=region,
            finding_hashes=finding_hashes,
            backend_identity_hash=backend_identity_hash,
        )
        action = action_override or "region_edit"
        reason = ""
        if action == "full_sentence_rewrite":
            reason = full_sentence_reason or (
                f"Region spans PID {region.pid} text "
                f"[{region.start}, {region.end}); a minimal region edit is "
                "not a sufficient fix and a full sentence rewrite is required."
            )
        repair = Repair(
            repair_id=repair_id,
            finding_ids=finding_hashes,
            chunk_id=chunk.chunk_id,
            action=action,
            target_pids=(region.pid,),
            instructions=_instructions_for(region_findings, action=action),
            full_sentence_reason=reason,
        )
        plans.append(RepairPlan(
            repair=repair,
            region=region,
            findings=region_findings,
        ))
    return tuple(plans)


def plan_repair_challenge(
    *,
    chunk_id: str,
    region: ResolvedRegion,
    findings: Sequence[Finding],
    challenge_evidence: str,
    backend_identity_hash: str,
) -> RepairPlan:
    """Build a repair that *challenges* a finding as a false positive.

    A challenge is never auto-accepted: ``challenge_evidence`` is a required,
    documented reason the finding is wrong, and the resulting
    ``models.Repair`` still carries ``auto_accepted=False`` (enforced by the
    contract). Missing evidence raises ``ValueError`` — a challenge without
    evidence is rejected outright.
    """
    if not challenge_evidence.strip():
        raise ValueError(
            f"Repair challenge for {chunk_id}:{region.pid} requires documented "
            "evidence; a challenge is never auto-accepted"
        )
    finding_hashes = tuple(sorted(f.content_hash for f in findings))
    repair_id = _plan_repair_id(
        chunk_id=chunk_id,
        region=region,
        finding_hashes=finding_hashes,
        backend_identity_hash=backend_identity_hash,
    )
    repair = Repair(
        repair_id=repair_id,
        finding_ids=finding_hashes,
        chunk_id=chunk_id,
        action="region_edit",
        target_pids=(region.pid,),
        instructions=challenge_evidence,
    )
    return RepairPlan(repair=repair, region=region, findings=tuple(findings))


# ---------------------------------------------------------------------------
# L3 severity filter (soft Gemma findings with weak evidence)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SoftFindingsPolicy:
    """L3 severity filter for soft Gemma findings (DECISIONS 2026-08-03).

    Weak-evidence findings in ``soft_categories`` (``calque``/``register``,
    see ``L3_SOFT_CATEGORIES``) raised by the Gemma Russian review are
    skipped from repair planning. The findings **remain in the append-only
    store** (``audit_findings.json`` is untouched); only the planning of
    repairs is filtered, and the skipped findings are recorded in the debt
    trace. Soft findings are also excluded from the round-2 *blocking*
    definition, so a convergence re-audit cannot re-introduce them as a
    round-2 trigger (otherwise the L3 economy disappears).

    Weak evidence = a short excerpt (``0 < len(excerpt) <
    weak_excerpt_max_len``) and/or an uncertain note formulation
    (``weak_note_markers``). Confident findings (long excerpt and no
    hesitation marker) stay in repair. These thresholds are policy
    parameters, not magic numbers; tune them here, never by editing repair
    logic.
    """

    enabled: bool = True
    soft_categories: Tuple[str, ...] = tuple(sorted(L3_SOFT_CATEGORIES))
    weak_excerpt_max_len: int = L3_WEAK_EXCERPT_MAX_LEN
    weak_note_markers: Tuple[str, ...] = L3_WEAK_NOTE_MARKERS


def _is_weak_soft_finding(
    finding: Finding,
    *,
    soft_categories: Sequence[str],
    weak_excerpt_max_len: int,
    weak_note_markers: Sequence[str],
) -> bool:
    """Whether a finding is a *weak-evidence soft* Gemma finding.

    Only Gemma Russian-review findings in a soft category qualify. Weak
    evidence is a short non-empty excerpt and/or an uncertain note
    formulation. An absent (empty) excerpt is not by itself weak — a
    confident note without an excerpt is still a strong signal.
    """
    if finding.detector != "gemma_russian_review" or finding.category not in soft_categories:
        return False
    evidence = finding.evidence if isinstance(finding.evidence, Mapping) else {}
    note = str(evidence.get("note", ""))
    excerpt = str(evidence.get("excerpt", ""))
    uncertain_note = any(
        marker.casefold() in note.casefold() for marker in weak_note_markers
    )
    short_excerpt = bool(excerpt.strip()) and len(excerpt) < weak_excerpt_max_len
    return short_excerpt or uncertain_note


def filter_soft_findings(
    findings: Sequence[Finding],
    policy: SoftFindingsPolicy,
) -> Tuple[Tuple[Finding, ...], Tuple[Finding, ...]]:
    """Split findings into ``(repairable, weak_soft_skipped)``.

    Findings are never mutated or removed from the store; only repair
    planning is filtered (L3 policy, DECISIONS 2026-08-03). When
    ``policy.enabled`` is ``False`` every finding is repairable.
    """
    if not policy.enabled:
        return tuple(findings), ()
    repairable: list[Finding] = []
    skipped: list[Finding] = []
    for finding in findings:
        if _is_weak_soft_finding(
            finding,
            soft_categories=policy.soft_categories,
            weak_excerpt_max_len=policy.weak_excerpt_max_len,
            weak_note_markers=policy.weak_note_markers,
        ):
            skipped.append(finding)
        else:
            repairable.append(finding)
    return tuple(repairable), tuple(skipped)


# ---------------------------------------------------------------------------
# Repair cache (resume-safe; persisted by the driver with identity)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepairRecord:
    """Outcome of one executed repair plan.

    ``committed`` is ``True`` only when every relevant gate passed (and any
    mandatory Gemma re-check passed). ``new_translation`` is the repaired
    *full chunk* PID map (target PIDs replaced, everything else kept
    verbatim). ``gemma_recheck`` is ``"passed"`` / ``"failed"`` /
    ``"not_required"`` / ``"transport_error"``. A transport failure or
    invalid structured output leaves ``committed=False`` with
    ``reason`` describing it — this is debt/incomplete, never a semantic
    terminal status.
    """

    repair_id: str
    chunk_id: str
    finding_ids: Tuple[str, ...]
    target_pids: Tuple[str, ...]
    action: str
    new_translation: Tuple[Tuple[str, str], ...]
    gate_trace: Tuple[GateResult, ...]
    gemma_recheck: str
    committed: bool
    reason: str

    def to_payload(self) -> Dict[str, Any]:
        return {
            "repair_id": self.repair_id,
            "chunk_id": self.chunk_id,
            "finding_ids": list(self.finding_ids),
            "target_pids": list(self.target_pids),
            "action": self.action,
            "new_translation": [list(item) for item in self.new_translation],
            "gate_trace": [
                {"gate": g.gate, "passed": g.passed, "detail": g.detail}
                for g in self.gate_trace
            ],
            "gemma_recheck": self.gemma_recheck,
            "committed": self.committed,
            "reason": self.reason,
        }


class RepairCache:
    """Exact-match in-memory repair cache (no disk I/O here).

    Mirrors ``pact_v4.phase3.audit.AuditCache``: persistence across process
    restarts is the caller/pipeline's responsibility. A resumed run that
    passes the same populated cache back in skips every repair unit that
    previously succeeded and retries only the ones that did not — producing
    deterministically the same findings/re-gates.
    """

    def __init__(self) -> None:
        self._store: Dict[str, RepairRecord] = {}

    def get(self, unit_hash: str) -> Optional[RepairRecord]:
        return self._store.get(unit_hash)

    def put(self, unit_hash: str, record: RepairRecord) -> None:
        self._store[unit_hash] = record

    def to_payload(self) -> Dict[str, Any]:
        return {
            "schema": REPAIR_UNIT_SCHEMA,
            "units": [
                {"unit_hash": unit_hash, "record": record.to_payload()}
                for unit_hash, record in sorted(self._store.items())
            ],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "RepairCache":
        if payload.get("schema") != REPAIR_UNIT_SCHEMA:
            raise ValueError(
                f"Foreign identity: repair-cache schema={payload.get('schema')!r}"
            )
        units = payload.get("units")
        if not isinstance(units, list):
            raise ValueError("RepairCache payload: units must be an array")
        cache = cls()
        for item in units:
            if not isinstance(item, Mapping):
                raise ValueError("RepairCache payload: unit entries must be JSON objects")
            record = item["record"]
            if not isinstance(record, Mapping):
                raise ValueError("RepairCache payload: record must be a JSON object")
            cache.put(
                item["unit_hash"],
                RepairRecord(
                    repair_id=str(record["repair_id"]),
                    chunk_id=str(record["chunk_id"]),
                    finding_ids=tuple(str(f) for f in record.get("finding_ids", [])),
                    target_pids=tuple(str(p) for p in record.get("target_pids", [])),
                    action=str(record["action"]),
                    new_translation=tuple(
                        (str(pid), str(text)) for pid, text in record["new_translation"]
                    ),
                    gate_trace=tuple(
                        GateResult(
                            gate=str(g["gate"]),
                            passed=bool(g.get("passed", False)),
                            detail=str(g.get("detail", "")),
                        )
                        for g in record.get("gate_trace", [])
                    ),
                    gemma_recheck=str(record["gemma_recheck"]),
                    committed=bool(record["committed"]),
                    reason=str(record["reason"]),
                ),
            )
        return cache


def _repair_unit_hash(
    *,
    chapter_hash: str,
    plan: RepairPlan,
    backend_identity_hash: str,
    policy_version: str,
) -> str:
    """Deterministic identity of one repair unit (stable across resume)."""
    return canonical_json_hash({
        "artifact": "pact-v4-repair-unit/v1",
        "chapter_hash": chapter_hash,
        "repair_id": plan.repair.repair_id,
        "action": plan.repair.action,
        "target_pids": list(plan.repair.target_pids),
        "finding_ids": sorted(plan.repair.finding_ids),
        "backend_identity_hash": backend_identity_hash,
        "policy_version": policy_version,
    })


# ---------------------------------------------------------------------------
# Repair output parsing (strict: reject partial/foreign/unknown, never best-effort)
# ---------------------------------------------------------------------------


def _parse_repair_output(
    raw: str, *, target_pids: Tuple[str, ...], chunk_id: str
) -> Tuple[Dict[str, str], str]:
    """Parse a repair response into ``{pid: repaired_text}`` + reason.

    Strict contract: well-formed complete JSON object with exactly
    ``{"repaired": {pid: text}, "reason": "..."}``; ``repaired`` must be an
    object whose keys are exactly ``target_pids`` (no missing, no extra, no
    foreign PIDs) with non-empty string values. Any truncation / malformed
    JSON / wrong shape raises ``ValueError`` — the caller treats that as a
    non-committed (debt) outcome, never a silent fallback.
    """
    payload = validate_json_complete(raw)
    repaired = payload.get("repaired")
    if not isinstance(repaired, dict):
        raise ValueError(
            f"Repair response for {chunk_id}: 'repaired' must be a JSON object"
        )
    missing = [pid for pid in target_pids if pid not in repaired]
    extra = [pid for pid in repaired if pid not in target_pids]
    if missing or extra:
        raise ValueError(
            f"Repair response for {chunk_id}: PID set mismatch "
            f"missing={missing}, extra={extra}, expected={list(target_pids)}"
        )
    for pid, text in repaired.items():
        if not isinstance(text, str) or not text:
            raise ValueError(
                f"Repair response for {chunk_id}: PID {pid} must be a non-empty string"
            )
    reason = str(payload.get("reason", ""))
    return {pid: str(repaired[pid]) for pid in target_pids}, reason


# ---------------------------------------------------------------------------
# Gemma re-check (4A2)
# ---------------------------------------------------------------------------


def _gemma_recheck_required(plan: RepairPlan) -> bool:
    """Mandatory Gemma re-check when a repair closes a Gemma-raised finding."""
    return any(f.detector == "gemma_russian_review" for f in plan.findings)


def _run_gemma_recheck(
    *,
    chunk_id: str,
    translation: Mapping[str, str],
    target_pids: Tuple[str, ...],
    gemma_audit_evaluator: GemmaAuditEvaluator,
    source_id: str = "",
    snapshot_id: str = "",
    candidate_id: str = "",
) -> Tuple[str, Tuple[Finding, ...]]:
    """Run the mandatory Gemma Russian-only re-check of a repaired region.

    Returns ``(status, findings)`` where ``status`` is ``"passed"`` /
    ``"failed"`` / ``"transport_error"``. A transport failure is recorded as
    ``transport_error`` (debt, not a semantic verdict) — the caller leaves
    the Russian finding open and returns the last admitted text as degraded
    availability.
    """
    try:
        raw = gemma_audit_evaluator(chunk_id=chunk_id, translation=dict(translation))
        issues = _parse_issues(
            raw, owned_pids=frozenset(translation), allowed_categories=GEMMA_AUDIT_CATEGORIES
        )
    except Exception as exc:  # model call failure or output validation failure
        LOG.warning("Gemma re-check transport/validation failure for %s: %s", chunk_id, exc)
        return "transport_error", ()
    region_issues = [issue for issue in issues if issue["pid"] in target_pids]
    if region_issues:
        findings = tuple(
            Finding(
                detector="gemma_russian_review",
                category=str(issue["category"]),
                evidence={"note": issue["note"], "excerpt": issue.get("excerpt", "")},
                region=Region(
                    pid=issue["pid"], start=0, end=len(translation.get(issue["pid"], ""))
                ),
                source_id=source_id,
                snapshot_id=snapshot_id,
                chunk_id=chunk_id,
                candidate_id=candidate_id,
                policy_version=GEMMA_RECHECK_POLICY_VERSION,
            )
            for issue in region_issues
        )
        return "failed", findings
    return "passed", ()


def _apply_region_edit(
    *,
    plan: RepairPlan,
    chunk: ChunkPlan,
    current_translation: Mapping[str, str],
    source: SourceArtifact,
    repair_caller: RepairCaller,
) -> Tuple[Optional[Dict[str, str]], str]:
    """Call the Gemma repair model for one region and build the tentative
    chunk translation (target PID replaced, everything else verbatim).

    Returns ``(tentative_chunk_map, "")`` on success or
    ``(None, reason)`` on a transport / invalid-structured-output failure —
    that is debt, never a semantic terminal status (no silent fallback).
    Shared by the legacy single-region flow and the L2b pass flow so the
    edit contract stays identical.
    """
    chunk_translation = {
        pid: current_translation.get(pid, "") for pid in chunk.pids
    }
    source_map = {pid: dict(source.source).get(pid, "") for pid in chunk.pids}
    findings_payload = [
        {
            "category": finding.category,
            "note": (
                str(finding.evidence.get("note", ""))
                if isinstance(finding.evidence, Mapping)
                else str(finding.evidence)
            ),
            "excerpt": (
                str(finding.evidence.get("excerpt", ""))
                if isinstance(finding.evidence, Mapping)
                else ""
            ),
        }
        for finding in plan.findings
    ]
    try:
        raw = repair_caller(
            chunk_id=plan.chunk_id,
            source=source_map,
            translation=chunk_translation,
            region=Region(
                pid=plan.region.pid, start=plan.region.start, end=plan.region.end
            ),
            findings=findings_payload,
        )
        repaired_texts, _reason = _parse_repair_output(
            raw, target_pids=plan.repair.target_pids, chunk_id=plan.chunk_id
        )
    except Exception as exc:
        LOG.warning(
            "Repair transport/validation failure for %s (%s): %s",
            plan.chunk_id, plan.repair.action, exc,
        )
        return None, (
            "Repair call failed (transport or invalid structured output): "
            f"{exc!r} — recorded as debt, not a semantic terminal status"
        )
    tentative = {
        pid: repaired_texts.get(pid, chunk_translation.get(pid, ""))
        for pid in chunk.pids
    }
    return tentative, ""


def _re_gate_region(
    *,
    plan: RepairPlan,
    chunk: ChunkPlan,
    audited_role: str,
    source: SourceArtifact,
    snapshot: Snapshot,
    chunk_plan: ChunkPlanArtifact,
    config: ConfigArtifact,
    det_data: DeterministicGateData,
    tentative_translation: Mapping[str, str],
    region_fidelity_gate: RegionFidelityEvaluator,
) -> Tuple[Tuple[GateResult, ...], bool, Optional[Candidate], str]:
    """Run the relevant re-gates on one tentative repaired translation.

    Deterministic consistency (whole chunk, model-free) plus the **narrow**
    Qwen re-gate of the edited PID (``region_fidelity_gate``: only the
    edited PID's source + repaired text + region — unedited PIDs are
    covered by the convergence re-audit). Returns ``(gate_trace, passed,
    candidate, failure_reason)``; a candidate identity-validation failure
    returns ``passed=False`` with ``failure_reason`` describing it.
    """
    source_map = {pid: dict(source.source).get(pid, "") for pid in chunk.pids}
    try:
        repaired_candidate = Candidate.create(
            candidate_id=f"{plan.chunk_id}:repair:{plan.repair.repair_id[:16]}",
            chunk_id=plan.chunk_id,
            role=audited_role,
            translation=tuple(
                (pid, tentative_translation.get(pid, "")) for pid in chunk.pids
            ),
            source=source,
            snapshot=snapshot,
            chunk_plan=chunk_plan,
            config=config,
        )
    except ValueError as exc:
        # The repaired PID map failed the ownership/identity contract (should
        # not happen: target PIDs are the region's own PIDs of this chunk).
        return (), False, None, f"Repaired candidate failed identity validation: {exc!r}"
    gate_trace: list[GateResult] = []
    det_result = deterministic_consistency_gate(
        candidate=repaired_candidate, source=source_map, data=det_data,
    )
    gate_trace.append(det_result)
    source_text = dict(source.source).get(plan.region.pid, "")
    repaired_text = tentative_translation.get(plan.region.pid, "")
    qwen_result = region_fidelity_gate(
        source_text=source_text,
        repaired_text=repaired_text,
        region=plan.region,
    )
    gate_trace.append(qwen_result)
    return tuple(gate_trace), det_result.passed and qwen_result.passed, repaired_candidate, ""


def _commit_reason(gate_trace: Sequence[GateResult], gemma_status: str) -> str:
    """Build the non-commit ``reason`` from the gate trace + re-check status."""
    reason_parts: list[str] = []
    for gate in gate_trace:
        if gate.passed:
            continue
        if gate.gate == "deterministic_consistency":
            reason_parts.append(f"deterministic_consistency: {gate.detail[:200]}")
        else:
            reason_parts.append(f"qwen_fidelity re-gate: {gate.detail[:200]}")
    if gemma_status == "failed":
        reason_parts.append("Gemma re-check failed: the Russian finding remains open")
    if gemma_status == "transport_error":
        reason_parts.append(
            "Gemma re-check transport failure (debt, not a semantic verdict)"
        )
    return "Repair not committed: " + "; ".join(reason_parts)


# ---------------------------------------------------------------------------
# 4A execution: one region repair + deterministic/Qwen re-gates
# ---------------------------------------------------------------------------


def repair_region(
    *,
    plan: RepairPlan,
    chapter_hash: str,
    chunk: ChunkPlan,
    audited_role: str,
    source: SourceArtifact,
    snapshot: Snapshot,
    chunk_plan: ChunkPlanArtifact,
    config: ConfigArtifact,
    det_data: DeterministicGateData,
    current_translation: Mapping[str, str],
    repair_caller: RepairCaller,
    qwen_evaluator: QwenEvaluator,
    gemma_audit_evaluator: GemmaAuditEvaluator,
    backend_identity_hash: str,
    cache: RepairCache,
) -> RepairRecord:
    """Execute one region repair and re-gate the repaired candidate.

    Flow: build the repaired chunk translation (target PIDs replaced) →
    re-create the repaired candidate (``Candidate.create`` re-validates every
    identity; the candidate keeps the audited chunk's role, since a repair is
    an edit of that candidate, not a new candidate role) → run the relevant
    gates (deterministic consistency + Qwen re-gate) → mandatory Gemma
    re-check when a Gemma finding is addressed.

    Returns a ``RepairRecord``. ``committed=True`` only when every relevant
    gate passed and any mandatory Gemma re-check passed. Qwen re-gate
    failure, Gemma re-check failure, transport failure or invalid structured
    output all leave ``committed=False`` with the reason recorded — the
    chunk keeps its last admitted text (degraded availability). No silent
    fallback anywhere.
    """
    unit_hash = _repair_unit_hash(
        chapter_hash=chapter_hash,
        plan=plan,
        backend_identity_hash=backend_identity_hash,
        policy_version=REPAIR_POLICY_VERSION,
    )
    cached = cache.get(unit_hash)
    if cached is not None:
        return cached

    chunk_translation = {
        pid: current_translation.get(pid, "") for pid in chunk.pids
    }
    tentative, edit_reason = _apply_region_edit(
        plan=plan,
        chunk=chunk,
        current_translation=current_translation,
        source=source,
        repair_caller=repair_caller,
    )
    if tentative is None:
        record = RepairRecord(
            repair_id=plan.repair.repair_id,
            chunk_id=plan.chunk_id,
            finding_ids=plan.repair.finding_ids,
            target_pids=plan.repair.target_pids,
            action=plan.repair.action,
            new_translation=tuple((pid, chunk_translation[pid]) for pid in chunk.pids),
            gate_trace=(),
            gemma_recheck="not_required",
            committed=False,
            reason=edit_reason,
        )
        cache.put(unit_hash, record)
        return record

    source_map = {pid: dict(source.source).get(pid, "") for pid in chunk.pids}
    try:
        repaired_candidate = Candidate.create(
            candidate_id=f"{plan.chunk_id}:repair:{plan.repair.repair_id[:16]}",
            chunk_id=plan.chunk_id,
            role=audited_role,
            translation=tuple(
                (pid, tentative[pid]) for pid in chunk.pids
            ),
            source=source,
            snapshot=snapshot,
            chunk_plan=chunk_plan,
            config=config,
        )
    except ValueError as exc:
        # The repaired PID map failed the ownership/identity contract (should
        # not happen: target PIDs are the region's own PIDs of this chunk).
        record = RepairRecord(
            repair_id=plan.repair.repair_id,
            chunk_id=plan.chunk_id,
            finding_ids=plan.repair.finding_ids,
            target_pids=plan.repair.target_pids,
            action=plan.repair.action,
            new_translation=tuple((pid, chunk_translation[pid]) for pid in chunk.pids),
            gate_trace=(),
            gemma_recheck="not_required",
            committed=False,
            reason=f"Repaired candidate failed identity validation: {exc!r}",
        )
        cache.put(unit_hash, record)
        return record

    # --- relevant gates --------------------------------------------------
    gate_trace: list[GateResult] = []
    det_result = deterministic_consistency_gate(
        candidate=repaired_candidate, source=source_map, data=det_data,
    )
    gate_trace.append(det_result)
    qwen_result = qwen_evaluator(source_map, dict(tentative))
    gate_trace.append(qwen_result)
    gates_passed = det_result.passed and qwen_result.passed

    gemma_status = "not_required"
    gemma_findings: Tuple[Finding, ...] = ()
    if gates_passed and _gemma_recheck_required(plan):
        gemma_status, gemma_findings = _run_gemma_recheck(
            chunk_id=plan.chunk_id,
            translation=tentative,
            target_pids=plan.repair.target_pids,
            gemma_audit_evaluator=gemma_audit_evaluator,
            source_id=source.source_hash,
            snapshot_id=snapshot.snapshot_hash,
            candidate_id=repaired_candidate.candidate_id,
        )
    gemma_ok = gemma_status in ("passed", "not_required")

    committed = gates_passed and gemma_ok
    if not committed:
        # Keep the last admitted text (degraded availability); the finding
        # stays open. Qwen re-gate failure never commits a repair; a failed
        # Gemma re-check leaves the Russian finding open.
        reason = _commit_reason(tuple(gate_trace), gemma_status)
        adopted_translation = tuple((pid, chunk_translation[pid]) for pid in chunk.pids)
    else:
        reason = "Repair committed: all relevant gates passed"
        adopted_translation = tuple(
            (pid, tentative[pid]) for pid in chunk.pids
        )

    record = RepairRecord(
        repair_id=plan.repair.repair_id,
        chunk_id=plan.chunk_id,
        finding_ids=plan.repair.finding_ids,
        target_pids=plan.repair.target_pids,
        action=plan.repair.action,
        new_translation=adopted_translation,
        gate_trace=tuple(gate_trace),
        gemma_recheck=gemma_status,
        committed=committed,
        reason=reason,
    )
    cache.put(unit_hash, record)
    return record


# ---------------------------------------------------------------------------
# 4B: convergence rounds + re-audit of changed PIDs + discourse neighbours
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepairRoundResult:
    """Outcome of one convergence repair round."""

    round_number: int
    records: Tuple[RepairRecord, ...]
    reaudit_findings: Tuple[Finding, ...] = ()
    changed_chunk_ids: Tuple[str, ...] = ()

    def to_payload(self) -> Dict[str, Any]:
        return {
            "round_number": self.round_number,
            "records": [record.to_payload() for record in self.records],
            "reaudit_findings": [finding.to_payload() for finding in self.reaudit_findings],
            "changed_chunk_ids": list(self.changed_chunk_ids),
        }


def _neighbour_chunk_ids(
    chunk_plan: ChunkPlanArtifact, chunk_id: str
) -> Tuple[str, ...]:
    """Immediately adjacent chunks (discourse neighbours) of ``chunk_id``."""
    ids = [chunk.chunk_id for chunk in chunk_plan.chunks]
    try:
        index = ids.index(chunk_id)
    except ValueError:
        return ()
    neighbours = []
    if index > 0:
        neighbours.append(ids[index - 1])
    if index < len(ids) - 1:
        neighbours.append(ids[index + 1])
    return tuple(neighbours)


def _reaudit_chunks(
    *,
    source: SourceArtifact,
    snapshot: Snapshot,
    chunk_plan: ChunkPlanArtifact,
    config: ConfigArtifact,
    det_data: DeterministicGateData,
    translation_by_chunk: Mapping[str, Mapping[str, str]],
    chunk_ids: Sequence[str],
    qwen_audit_evaluator: QwenAuditEvaluator,
    gemma_audit_evaluator: GemmaAuditEvaluator,
    progress: Optional[Any] = None,
) -> Tuple[Finding, ...]:
    """Re-audit a targeted set of chunks (changed PIDs + discourse neighbours).

    Returns the fresh findings for exactly those chunks. Deterministic layer
    (numbers/glossary/mixed-script/missing) is re-run per chunk (model-free)
    alongside the model tracks so the re-audit is self-contained and needs no
    cached Step 6 results.

    L1 (DECISIONS 2026-08-01/03): the model tracks iterate **detector-outer,
    chunk-inner** (``for detector: for chunk_id:``) so a single-resident
    driver pays ~1-2 model switches for the whole re-audit instead of ~2N.
    Re-audit units are independent (the whole chapter is already assembled),
    so iteration order is purely a scheduling decision. The returned finding
    set is order-independent: it is canonicalised by ``content_hash``, so the
    exact same set is produced regardless of loop order.
    """
    source_map = dict(source.source)
    all_findings: list[Finding] = []

    # Deterministic layer (model-free), per chunk.
    for chunk_id in chunk_ids:
        chunk = chunk_plan.chunk(chunk_id)
        translation = translation_by_chunk.get(chunk_id, {})
        candidate_id = f"{chunk_id}:repair:reaudit"
        for pid in chunk.pids:
            en_text = source_map.get(pid, "")
            target = translation.get(pid, "")
            if not target:
                all_findings.append(Finding(
                    detector="deterministic_integrity",
                    category="missing",
                    evidence={"problem": "Translation is empty or missing."},
                    region=Region(pid=pid, start=0, end=0),
                    source_id=source.source_hash,
                    snapshot_id=snapshot.snapshot_hash,
                    chunk_id=chunk_id,
                    candidate_id=candidate_id,
                    policy_version=DETERMINISTIC_INTEGRITY_POLICY_VERSION,
                ))
                continue
            source_digits = extract_digits(en_text)
            if source_digits:
                missing = missing_numeric_values(source_digits, target)
                if missing:
                    all_findings.append(Finding(
                        detector="deterministic_integrity",
                        category="number",
                        evidence={"problem": f"Missing numeric values: {missing}"},
                        region=Region(pid=pid, start=0, end=len(target)),
                        source_id=source.source_hash,
                        snapshot_id=snapshot.snapshot_hash,
                        chunk_id=chunk_id,
                        candidate_id=candidate_id,
                        policy_version=DETERMINISTIC_INTEGRITY_POLICY_VERSION,
                    ))
            mixed = find_mixed_script(target, det_data.mixed_script_allow)
            if mixed:
                all_findings.append(Finding(
                    detector="deterministic_integrity",
                    category="mixed_script",
                    evidence={"problem": f"Latin or mixed-script token(s): {mixed}"},
                    region=Region(pid=pid, start=0, end=len(target)),
                    source_id=source.source_hash,
                    snapshot_id=snapshot.snapshot_hash,
                    chunk_id=chunk_id,
                    candidate_id=candidate_id,
                    policy_version=DETERMINISTIC_INTEGRITY_POLICY_VERSION,
                ))
            for en_term, ru_term in combine_glossary_terms(
                det_data.glossary_terms, det_data.names
            ).items():
                if source_term_present(en_text, en_term) and not target_form_present(target, ru_term):
                    all_findings.append(Finding(
                        detector="deterministic_integrity",
                        category="glossary_consistency",
                        evidence={"problem": f"'{en_term}' should use '{ru_term}'"},
                        region=Region(pid=pid, start=0, end=len(target)),
                        source_id=source.source_hash,
                        snapshot_id=snapshot.snapshot_hash,
                        chunk_id=chunk_id,
                        candidate_id=candidate_id,
                        policy_version=DETERMINISTIC_INTEGRITY_POLICY_VERSION,
                    ))

    # Model tracks: detector-outer, chunk-inner (L1 batching by model).
    scope = [(chunk_id, chunk_plan.chunk(chunk_id)) for chunk_id in chunk_ids]
    for detector, evaluator, allowed in (
        ("qwen_chapter_audit", qwen_audit_evaluator, QWEN_AUDIT_CATEGORIES),
        ("gemma_russian_review", gemma_audit_evaluator, GEMMA_AUDIT_CATEGORIES),
    ):
        for chunk_id, chunk in scope:
            if progress is not None:
                progress.reaudit_unit_started(chunk_id=chunk_id, detector=detector)
            translation = translation_by_chunk.get(chunk_id, {})
            candidate_id = f"{chunk_id}:repair:reaudit"
            owned_source = {pid: source_map.get(pid, "") for pid in chunk.pids}
            owned_translation = {pid: translation.get(pid, "") for pid in chunk.pids}
            try:
                if detector == "qwen_chapter_audit":
                    raw = evaluator(
                        chunk_id=chunk_id, source=owned_source, translation=owned_translation
                    )
                else:
                    raw = evaluator(chunk_id=chunk_id, translation=owned_translation)
                issues = _parse_issues(
                    raw, owned_pids=frozenset(chunk.pids), allowed_categories=allowed
                )
                unit_status = "ok"
            except Exception as exc:
                LOG.warning(
                    "Convergence re-audit %s failed for %s: %s (recorded as debt)",
                    detector, chunk_id, exc,
                )
                unit_status = "failed"
            if progress is not None:
                progress.reaudit_unit_done(chunk_id=chunk_id, detector=detector, status=unit_status)
            if unit_status != "ok":
                continue
            all_findings.extend(_findings_from_issues(
                issues,
                detector=detector,
                chapter=AssembledChapter(
                    source_hash=source.source_hash,
                    snapshot_hash=snapshot.snapshot_hash,
                    chunk_plan_hash=chunk_plan.plan_hash,
                    config_identity=config.config_identity,
                    translation=tuple(
                        (pid, translation.get(pid, "")) for pid in chunk.pids
                    ),
                ),
                chunk_id=chunk_id,
                candidate_id=candidate_id,
                policy_version=(
                    QWEN_REAUDIT_POLICY_VERSION
                    if detector == "qwen_chapter_audit"
                    else GEMMA_RECHECK_POLICY_VERSION
                ),
            ))
    return tuple(sorted(all_findings, key=lambda finding: finding.content_hash))


# ---------------------------------------------------------------------------
# 4B: final integrity check (deterministic default; narrow Qwen smoke conditional)
# ---------------------------------------------------------------------------


def _needs_qwen_smoke(
    *,
    reaudited_pids: frozenset,
    original_translation: Mapping[str, str],
    final_translation: Mapping[str, str],
) -> bool:
    """Step 8 narrow Qwen smoke is needed only if text changed OUTSIDE the
    Step 7 re-audited scope. Repair only changes regions that Step 7
    re-audits, so by default this is ``False`` (deterministic integrity only);
    the driver may still record the conditional trigger for diagnostics.

    Since Phase 5 formatting is applied **before** this check, the final
    translation may carry inline HTML markup. Formatting is wrap-only (it
    never rewrites the visible content), so the comparison is done on the
    markup-stripped text: a formatting-only change can never trip the smoke.
    """
    for pid, text in final_translation.items():
        if pid in reaudited_pids:
            continue
        if (
            strip_inline_markup(original_translation.get(pid, ""))
            != strip_inline_markup(text)
        ):
            return True
    return False


def run_integrity_check(
    *,
    source: SourceArtifact,
    snapshot: Snapshot,
    chunk_plan: ChunkPlanArtifact,
    config: ConfigArtifact,
    det_data: DeterministicGateData = DeterministicGateData(),
    final_translation: Mapping[str, str],
    original_translation: Mapping[str, str],
    reaudited_pids: frozenset,
) -> Dict[str, Any]:
    """Final integrity check (Step 8): deterministic by default.

    Verifies full PID coverage and deterministic invariants (numbers,
    mixed-script, glossary, missing) over the whole chapter. ``qwen_smoke``
    is ``False`` unless text changed outside the re-audited scope.

    The check runs over the **final** chapter text, which since Phase 5 may
    carry inline formatting markup. Content checks therefore operate on the
    markup-stripped text (``strip_inline_markup``) — the visible text — while
    ``frozen_hash`` still covers the exact final (formatted) text, i.e. the
    text that goes into ``complete``.
    """
    source_map = dict(source.source)
    missing_pids = [pid for pid in snapshot.pids if not final_translation.get(pid)]
    numeric_missing: list[str] = []
    mixed_script: list[str] = []
    glossary: list[str] = []
    for pid in snapshot.pids:
        en_text = source_map.get(pid, "")
        target = strip_inline_markup(final_translation.get(pid, ""))
        if not target:
            continue
        source_digits = extract_digits(en_text)
        if source_digits:
            missing = missing_numeric_values(source_digits, target)
            if missing:
                numeric_missing.append(pid)
        if find_mixed_script(target, det_data.mixed_script_allow):
            mixed_script.append(pid)
        for en_term, ru_term in combine_glossary_terms(
            det_data.glossary_terms, det_data.names
        ).items():
            if source_term_present(en_text, en_term) and not target_form_present(target, ru_term):
                glossary.append(pid)
    frozen_hash = canonical_json_hash({
        "artifact": "pact-v4-assembled-chapter/v1",
        "source_hash": source.source_hash,
        "snapshot_hash": snapshot.snapshot_hash,
        "chunk_plan_hash": chunk_plan.plan_hash,
        "config_identity": config.config_identity,
        "translation": [list(item) for item in sorted(final_translation.items())],
    })
    return {
        "status": "complete" if not (missing_pids or numeric_missing or mixed_script or glossary)
        else "failed",
        "missing_pids": missing_pids,
        "numeric_missing": numeric_missing,
        "mixed_script": mixed_script,
        "glossary_missing": glossary,
        "qwen_smoke": _needs_qwen_smoke(
            reaudited_pids=reaudited_pids,
            original_translation=original_translation,
            final_translation=final_translation,
        ),
        "frozen_hash": frozen_hash,
    }


# ---------------------------------------------------------------------------
# Terminal decision (monotonic; spec states complete/accepted_degraded/failed)
# ---------------------------------------------------------------------------


def _valid_pid_map(
    *,
    chunk_plan: ChunkPlanArtifact,
    final_translation: Mapping[str, str],
) -> bool:
    """Every plan PID has non-empty text (structurally-valid PID-map)."""
    for chunk in chunk_plan.chunks:
        for pid in chunk.pids:
            if not final_translation.get(pid):
                return False
    return True


def decide_terminal_state(
    *,
    chunk_plan: ChunkPlanArtifact,
    final_translation: Mapping[str, str],
    debt_reasons: Sequence[str],
    provenance: Provenance,
) -> TerminalState:
    """Monotonic terminal decision for the chapter (Step 8).

    * ``failed`` — no valid structural PID-map (after bounded repair/fallback
      there is no complete PID-map; text is never fabricated).
    * ``accepted_degraded`` — valid PID-map but unresolved debt (unrepaired
      findings / failed re-gates / transport failures). No memory promotion.
    * ``complete`` — valid PID-map and no debt.
    """
    terminal = TerminalState(state_id="chapter", status="pending", provenance=provenance)
    valid = _valid_pid_map(chunk_plan=chunk_plan, final_translation=final_translation)
    if not valid:
        terminal.transition_to("failed")
    elif debt_reasons:
        terminal.transition_to("accepted_degraded")
    else:
        terminal.transition_to("complete")
    return terminal


# ---------------------------------------------------------------------------
# Phase orchestration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepairPhaseResult:
    """Full result of the Phase 4 repair/convergence/terminal run.

    ``status`` is the chapter terminal state (``complete`` /
    ``accepted_degraded`` / ``failed``). ``rounds`` carries per-round repair
    records + convergence re-audit findings. ``debt_trace`` lists the
    unresolved reasons. ``final_translation`` is the chapter PID map after
    repair **and** Phase 5 formatting (the text the Step 8 integrity check
    and the terminal transition see — for PIDs with a restored inline span
    contract the values carry inline HTML markup). ``formatting`` holds the
    Phase 5 outcome when a formatting step was configured, else ``None``.
    """

    status: str
    rounds: Tuple[RepairRoundResult, ...]
    debt_trace: Tuple[str, ...]
    final_translation: Tuple[Tuple[str, str], ...]
    integrity: Dict[str, Any]
    terminal: TerminalState
    report_payload: Dict[str, Any]
    formatting: Any = None

    def to_payload(self) -> Dict[str, Any]:
        return self.report_payload


def _run_repair_round(
    *,
    chapter_hash: str,
    chunk_plan: ChunkPlanArtifact,
    plans_by_chunk: Mapping[str, Sequence[RepairPlan]],
    candidates: Mapping[str, Candidate],
    source: SourceArtifact,
    snapshot: Snapshot,
    config: ConfigArtifact,
    det_data: DeterministicGateData,
    translation_by_chunk: Dict[str, Dict[str, str]],
    repair_caller: RepairCaller,
    region_fidelity_gate: RegionFidelityEvaluator,
    gemma_audit_evaluator: GemmaAuditEvaluator,
    backend_identity_hash: str,
    cache: RepairCache,
    progress: Optional[Any] = None,
) -> Tuple[Tuple[RepairRecord, ...], Tuple[str, ...]]:
    """Execute one convergence round as four role passes (L2b).

    1. **Gemma edit pass** (one lease): every planned ``region_edit`` is
       prepared on the round's chunk snapshot (one text slice per chunk;
       regions on distinct PIDs) and kept as tentative.
    2. **Qwen re-gate pass** (one lease): the narrow ``region_fidelity_gate``
       (edited PID only) + the deterministic consistency gate per tentative.
    3. **Gemma recheck pass** (one lease): the mandatory Russian re-check for
       tentatives that passed their re-gate and close a Gemma finding.
    4. **Commit**: tentatives that passed re-gate (+ recheck where required)
       are applied to ``translation_by_chunk``; the rest are debt and their
       findings stay open.

    Cached units are reused verbatim on exact identity (never re-called),
    committed or not, with the same debt semantics as the interleaved flow.
    Returns ``(records, changed_chunk_ids)``.
    """
    fresh: Dict[str, Tuple[RepairPlan, Dict[str, str], Dict[str, str], str]] = {}
    failed_edits: Dict[str, Tuple[RepairPlan, Dict[str, str], str, str]] = {}
    cached: Dict[str, Tuple[RepairRecord, str]] = {}
    for chunk_id, plans in plans_by_chunk.items():
        if chunk_id not in candidates:
            continue
        base = translation_by_chunk[chunk_id]
        for plan in plans:
            unit_hash = _repair_unit_hash(
                chapter_hash=chapter_hash,
                plan=plan,
                backend_identity_hash=backend_identity_hash,
                policy_version=REPAIR_POLICY_VERSION,
            )
            record = cache.get(unit_hash)
            if progress is not None:
                progress.region_started(
                    chunk_id=plan.chunk_id,
                    repair_id=plan.repair.repair_id,
                    target_pids=list(plan.repair.target_pids),
                    action=plan.repair.action,
                )
            if record is not None:
                cached[plan.repair.repair_id] = (record, chunk_id)
                continue
            tentative, edit_reason = _apply_region_edit(
                plan=plan,
                chunk=chunk_plan.chunk(chunk_id),
                current_translation=base,
                source=source,
                repair_caller=repair_caller,
            )
            if tentative is None:
                failed_edits[plan.repair.repair_id] = (plan, base, unit_hash, edit_reason)
            else:
                fresh[plan.repair.repair_id] = (plan, tentative, base, unit_hash)

    records: list[RepairRecord] = []
    changed: list[str] = []

    # ---- Qwen re-gate pass (one lease) ----------------------------------
    gate_results: Dict[
        str, Tuple[Tuple[GateResult, ...], bool, Optional[Candidate], str]
    ] = {}
    for repair_id, (plan, tentative, _base, _unit_hash) in fresh.items():
        gate_results[repair_id] = _re_gate_region(
            plan=plan,
            chunk=chunk_plan.chunk(plan.chunk_id),
            audited_role=candidates[plan.chunk_id].role,
            source=source,
            snapshot=snapshot,
            chunk_plan=chunk_plan,
            config=config,
            det_data=det_data,
            tentative_translation=tentative,
            region_fidelity_gate=region_fidelity_gate,
        )

    # ---- Gemma recheck pass (one lease) ---------------------------------
    recheck: Dict[str, str] = {}
    for repair_id, (plan, tentative, _base, _unit_hash) in fresh.items():
        gate_trace, passed, candidate, _candidate_reason = gate_results[repair_id]
        if passed and candidate is not None and _gemma_recheck_required(plan):
            status, _gemma_findings = _run_gemma_recheck(
                chunk_id=plan.chunk_id,
                translation=tentative,
                target_pids=plan.repair.target_pids,
                gemma_audit_evaluator=gemma_audit_evaluator,
                source_id=source.source_hash,
                snapshot_id=snapshot.snapshot_hash,
                candidate_id=candidate.candidate_id,
            )
            recheck[repair_id] = status
        else:
            recheck[repair_id] = "not_required"

    # ---- commit ---------------------------------------------------------
    for repair_id, (plan, tentative, base, unit_hash) in fresh.items():
        gate_trace, passed, _candidate, candidate_reason = gate_results[repair_id]
        gemma_status = recheck[repair_id]
        gemma_ok = gemma_status in ("passed", "not_required")
        committed = passed and gemma_ok
        chunk_pids = chunk_plan.chunk(plan.chunk_id).pids
        if committed:
            reason = "Repair committed: all relevant gates passed"
            adopted = tuple((pid, tentative[pid]) for pid in chunk_pids)
        elif candidate_reason:
            reason = candidate_reason
            adopted = tuple((pid, base[pid]) for pid in chunk_pids)
        else:
            reason = _commit_reason(gate_trace, gemma_status)
            adopted = tuple((pid, base[pid]) for pid in chunk_pids)
        record = RepairRecord(
            repair_id=plan.repair.repair_id,
            chunk_id=plan.chunk_id,
            finding_ids=plan.repair.finding_ids,
            target_pids=plan.repair.target_pids,
            action=plan.repair.action,
            new_translation=adopted,
            gate_trace=gate_trace,
            gemma_recheck=gemma_status,
            committed=committed,
            reason=reason,
        )
        records.append(record)
        cache.put(unit_hash, record)
        if progress is not None:
            progress.region_done(
                chunk_id=plan.chunk_id,
                repair_id=plan.repair.repair_id,
                target_pids=list(plan.repair.target_pids),
                action=plan.repair.action,
                committed=committed,
                reason=reason,
            )
        if committed:
            # Apply only the plan's target PIDs: every tentative is prepared
            # on the round's snapshot (one text slice), so writing the whole
            # chunk here would clobber sibling repairs of the same chunk.
            for pid in plan.repair.target_pids:
                translation_by_chunk[plan.chunk_id][pid] = tentative[pid]
            if plan.chunk_id not in changed:
                changed.append(plan.chunk_id)

    for _repair_id, (plan, base, unit_hash, edit_reason) in failed_edits.items():
        chunk_pids = chunk_plan.chunk(plan.chunk_id).pids
        record = RepairRecord(
            repair_id=plan.repair.repair_id,
            chunk_id=plan.chunk_id,
            finding_ids=plan.repair.finding_ids,
            target_pids=plan.repair.target_pids,
            action=plan.repair.action,
            new_translation=tuple((pid, base[pid]) for pid in chunk_pids),
            gate_trace=(),
            gemma_recheck="not_required",
            committed=False,
            reason=edit_reason,
        )
        records.append(record)
        cache.put(unit_hash, record)
        if progress is not None:
            progress.region_done(
                chunk_id=plan.chunk_id,
                repair_id=plan.repair.repair_id,
                target_pids=list(plan.repair.target_pids),
                action=plan.repair.action,
                committed=False,
                reason=edit_reason,
            )

    for _repair_id, (record, chunk_id) in cached.items():
        records.append(record)
        if progress is not None:
            progress.region_done(
                chunk_id=record.chunk_id,
                repair_id=record.repair_id,
                target_pids=list(record.target_pids),
                action=record.action,
                committed=record.committed,
                reason=record.reason,
            )
        if record.committed:
            for pid in record.target_pids:
                text = dict(record.new_translation).get(pid, "")
                translation_by_chunk[chunk_id][pid] = text
            if chunk_id not in changed:
                changed.append(chunk_id)

    return tuple(records), tuple(changed)


def run_repair_phase(
    *,
    source: SourceArtifact,
    snapshot: Snapshot,
    chunk_plan: ChunkPlanArtifact,
    config: ConfigArtifact,
    provenance: Provenance,
    det_data: DeterministicGateData,
    handoff_chunks: Sequence[Mapping[str, Any]],
    findings_store: FindingStore,
    candidates: Mapping[str, Candidate],
    current_translation: Mapping[str, str],
    repair_caller: RepairCaller,
    region_fidelity_gate: RegionFidelityEvaluator,
    qwen_audit_evaluator: QwenAuditEvaluator,
    gemma_audit_evaluator: GemmaAuditEvaluator,
    backend_identity_hash: str,
    cache: Optional[RepairCache] = None,
    max_rounds: int = 2,
    chapter_hash: str = "",
    formatting: Optional[FormattingStep] = None,
    soft_findings_policy: Optional[SoftFindingsPolicy] = None,
    progress: Optional[Any] = None,
) -> RepairPhaseResult:
    """Run Phase 4A/4A2/4B for one chapter.

    ``handoff_chunks`` are the ``b2_handoff.json`` rows (B1 input contract);
    ``findings_store`` is the Step 6 finding store; ``candidates`` maps
    chunk_id -> audited candidate (committed winner or quarantined
    best-variant); ``current_translation`` is the assembled chapter PID map.

    ``chapter_hash`` is the Step 6 assembled-chapter hash (the same value
    used to persist the repair cache), so repair unit identities are stable
    across resume. When omitted, it is recomputed deterministically from
    ``current_translation`` (callers that already hold the authoritative hash
    should pass it).

    Each round runs as four role passes (L2b, DECISIONS 2026-08-03):
    all Gemma edits, then all narrow Qwen re-gates
    (``region_fidelity_gate`` — edited PID only, unedited PIDs are covered
    by the convergence re-audit), then all mandatory Gemma re-checks, then
    the deferred commit. Commit criteria are unchanged (deterministic +
    Qwen fidelity passed; Gemma re-check passed where required); only the
    re-gate scope and commit timing change. ``repair_id`` and the repair
    cache unit hash are unchanged, so resume/cache semantics are preserved.
    Edits inside a chunk are prepared on one text snapshot (regions on
    distinct PIDs); cross-region issues are caught by the re-audit — an
    accepted trade-off (DECISIONS 2026-08-03).

    ``soft_findings_policy`` enables the L3 severity filter (default on):
    weak-evidence soft Gemma findings (``calque``/``register`` with a short
    excerpt and/or an uncertain note) are skipped from repair planning,
    excluded from the round-2 blocking definition, and recorded in the debt
    trace. The findings stay in the store.

    ``formatting`` is the Phase 5 formatting step (B3), applied **after**
    convergence and **before** the final integrity check and the monotonic
    terminal transition, so Step 8 sees the same text that goes into
    ``complete``. Formatting incidents join the debt trace: any unresolved
    required span is blocking, so with the production default
    ``max_formatting_incidents=0`` a single incident prevents ``complete``
    and yields ``accepted_degraded`` (when the PID map stays structurally
    valid) — never ``failed`` from a formatting transport failure alone.

    One repair round is mandatory and re-audits changed PIDs + discourse
    neighbours (batched by detector, L1). A second round is allowed only
    for a remaining blocking finding or a changed chunk boundary. Then the
    final integrity check (deterministic by default) and the monotonic
    terminal transition.
    """
    if cache is None:
        cache = RepairCache()
    policy = soft_findings_policy or SoftFindingsPolicy()

    if not chapter_hash:
        chapter_hash = canonical_json_hash({
            "artifact": "pact-v4-assembled-chapter/v1",
            "source_hash": source.source_hash,
            "snapshot_hash": snapshot.snapshot_hash,
            "chunk_plan_hash": chunk_plan.plan_hash,
            "config_identity": config.config_identity,
            "translation": [list(item) for item in sorted(current_translation.items())],
        })

    # ---- round 1 (mandatory) -------------------------------------------
    translation_by_chunk: Dict[str, Dict[str, str]] = {
        chunk.chunk_id: {
            pid: current_translation.get(pid, "") for pid in chunk.pids
        }
        for chunk in chunk_plan.chunks
    }
    debt_reasons: list[str] = []

    # Plan repairs per chunk, applying the L3 severity pre-filter before the
    # region resolver (findings stay in the store; only planning is filtered).
    round_one_plans: Dict[str, list[RepairPlan]] = {}
    for chunk in chunk_plan.chunks:
        # A chunk with no auditable candidate has no text to repair; its
        # PIDs are structural gaps (debt, and terminal `failed` if the whole
        # chapter PID-map is invalid). Repair only chunks that produced a
        # candidate (committed winner or quarantined best-variant).
        if chunk.chunk_id not in candidates:
            continue
        chunk_findings = tuple(f for f in findings_store if f.chunk_id == chunk.chunk_id)
        if not chunk_findings:
            continue
        repairable, skipped = filter_soft_findings(chunk_findings, policy)
        for finding in skipped:
            note = (
                str(finding.evidence.get("note", ""))
                if isinstance(finding.evidence, Mapping)
                else str(finding.evidence)
            )
            debt_reasons.append(
                f"{chunk.chunk_id}: {finding.region.pid}: soft Gemma finding "
                f"({finding.category}) skipped by L3 policy (weak evidence): {note}"
            )
        if not repairable:
            # Every finding of this chunk was a weak-soft L3 skip (already
            # recorded in the debt trace); nothing remains to plan.
            continue
        plans = plan_repairs_for_chunk(
            chunk=chunk,
            findings=repairable,
            current_text=translation_by_chunk[chunk.chunk_id],
            backend_identity_hash=backend_identity_hash,
        )
        if not plans:
            debt_reasons.append(
                f"{chunk.chunk_id}: findings present but no region repair could be planned"
            )
            continue
        round_one_plans[chunk.chunk_id] = list(plans)

    if progress is not None:
        progress.repair_round_started(round_number=1)
    round_one_records, changed_chunk_ids = _run_repair_round(
        chapter_hash=chapter_hash,
        chunk_plan=chunk_plan,
        plans_by_chunk=round_one_plans,
        candidates=candidates,
        source=source,
        snapshot=snapshot,
        config=config,
        det_data=det_data,
        translation_by_chunk=translation_by_chunk,
        repair_caller=repair_caller,
        region_fidelity_gate=region_fidelity_gate,
        gemma_audit_evaluator=gemma_audit_evaluator,
        backend_identity_hash=backend_identity_hash,
        cache=cache,
        progress=progress,
    )
    changed_chunk_ids = list(changed_chunk_ids)
    for record in round_one_records:
        if not record.committed:
            debt_reasons.append(
                f"{record.chunk_id}: repair {record.repair_id[:12]} not committed "
                f"({record.reason})"
            )

    # ---- convergence re-audit (round 1; L1-batched by detector) ---------
    reaudit_scope = list(changed_chunk_ids)
    for chunk_id in changed_chunk_ids:
        reaudit_scope.extend(_neighbour_chunk_ids(chunk_plan, chunk_id))
    reaudit_scope = list(dict.fromkeys(reaudit_scope))
    reaudit_findings: Tuple[Finding, ...] = ()
    if reaudit_scope:
        reaudit_findings = _reaudit_chunks(
            source=source,
            snapshot=snapshot,
            chunk_plan=chunk_plan,
            config=config,
            det_data=det_data,
            translation_by_chunk=translation_by_chunk,
            chunk_ids=reaudit_scope,
            qwen_audit_evaluator=qwen_audit_evaluator,
            gemma_audit_evaluator=gemma_audit_evaluator,
            progress=progress,
        )

    round_one = RepairRoundResult(
        round_number=1,
        records=tuple(round_one_records),
        reaudit_findings=reaudit_findings,
        changed_chunk_ids=tuple(changed_chunk_ids),
    )

    # ---- round 2 (only for a remaining blocking finding or changed boundary)
    # L3: weak-evidence soft findings are not blocking in round 2 — the
    # convergence re-audit would otherwise re-raise them and the L3 economy
    # would disappear. They are recorded as debt below.
    def _is_blocking(finding: Finding) -> bool:
        if not policy.enabled:
            return True
        return not _is_weak_soft_finding(
            finding,
            soft_categories=policy.soft_categories,
            weak_excerpt_max_len=policy.weak_excerpt_max_len,
            weak_note_markers=policy.weak_note_markers,
        )

    round_two_records: list[RepairRecord] = []
    round_two_findings: Tuple[Finding, ...] = ()
    round_two_changed: list[str] = []
    round_two_ran = False
    blocking_findings = tuple(
        f for f in reaudit_findings
        if f.chunk_id in reaudit_scope and _is_blocking(f)
    )
    # Record weak-soft re-audit findings as L3 debt regardless of whether
    # round 2 runs (they are intentionally left open).
    for finding in reaudit_findings:
        if finding.chunk_id in reaudit_scope and not _is_blocking(finding):
            debt_reasons.append(
                f"{finding.chunk_id}: {finding.region.pid}: soft Gemma finding "
                f"({finding.category}) left open by L3 policy (weak evidence)"
            )
    boundary_changed = any(
        cid in _neighbour_chunk_ids(chunk_plan, chunk_id)
        for chunk_id in changed_chunk_ids
        for cid in changed_chunk_ids
        if cid != chunk_id
    )
    if max_rounds >= 2 and (blocking_findings or boundary_changed):
        round_two_plans: Dict[str, list[RepairPlan]] = {}
        for chunk in chunk_plan.chunks:
            if chunk.chunk_id not in candidates:
                continue
            chunk_blocking = tuple(
                f for f in blocking_findings if f.chunk_id == chunk.chunk_id
            )
            if not chunk_blocking:
                continue
            plans = plan_repairs_for_chunk(
                chunk=chunk,
                findings=chunk_blocking,
                current_text=translation_by_chunk[chunk.chunk_id],
                backend_identity_hash=backend_identity_hash,
                action_override="full_sentence_rewrite",
            )
            if not plans:
                debt_reasons.append(
                    f"{chunk.chunk_id}: blocking finding remains but no repair could be planned"
                )
                continue
            round_two_plans[chunk.chunk_id] = list(plans)
        if round_two_plans:
            round_two_ran = True
            if progress is not None:
                progress.repair_round_started(round_number=2)
            round_two_records, round_two_changed = _run_repair_round(
                chapter_hash=chapter_hash,
                chunk_plan=chunk_plan,
                plans_by_chunk=round_two_plans,
                candidates=candidates,
                source=source,
                snapshot=snapshot,
                config=config,
                det_data=det_data,
                translation_by_chunk=translation_by_chunk,
                repair_caller=repair_caller,
                region_fidelity_gate=region_fidelity_gate,
                gemma_audit_evaluator=gemma_audit_evaluator,
                backend_identity_hash=backend_identity_hash,
                cache=cache,
                progress=progress,
            )
            round_two_changed = list(round_two_changed)
        for record in round_two_records:
            if not record.committed:
                debt_reasons.append(
                    f"{record.chunk_id}: round 2 repair not committed "
                    f"({record.reason})"
                )
        if round_two_changed:
            scope2 = list(round_two_changed)
            for chunk_id in round_two_changed:
                scope2.extend(_neighbour_chunk_ids(chunk_plan, chunk_id))
            scope2 = list(dict.fromkeys(scope2))
            round_two_findings = _reaudit_chunks(
                source=source,
                snapshot=snapshot,
                chunk_plan=chunk_plan,
                config=config,
                det_data=det_data,
                translation_by_chunk=translation_by_chunk,
                chunk_ids=scope2,
                qwen_audit_evaluator=qwen_audit_evaluator,
                gemma_audit_evaluator=gemma_audit_evaluator,
                progress=progress,
            )

    round_two = RepairRoundResult(
        round_number=2,
        records=tuple(round_two_records),
        reaudit_findings=round_two_findings,
        changed_chunk_ids=tuple(round_two_changed),
    )

    if progress is not None:
        progress.repair_done(rounds=2 if round_two_ran else 1)

    # ---- assemble final chapter translation ----------------------------
    final_map: Dict[str, str] = {}
    for chunk in chunk_plan.chunks:
        final_map.update(translation_by_chunk[chunk.chunk_id])

    # ---- Phase 5 formatting (span contract) before Step 8 --------------
    # Applied AFTER convergence and BEFORE the final integrity check and the
    # terminal transition, so Step 8 sees the same text that goes into
    # `complete`. Formatting is wrap-only by contract: it locates the source
    # inline spans' fragments and wraps them in the source tags, never
    # rewriting the visible text, so the re-audit scope and the conditional
    # Qwen smoke are unaffected. Every unresolved required span is a blocking
    # incident that joins the debt trace.
    formatting_outcome = None
    if formatting is not None:
        formatting_outcome = formatting(translation=dict(final_map))
        formatted_map = dict(formatting_outcome.formatted_text)
        if set(formatted_map) != set(final_map):
            raise ValueError(
                "Formatting must preserve the PID map; got "
                f"{len(formatted_map)} PIDs, expected {len(final_map)}"
            )
        for pid, text in final_map.items():
            if text and not formatted_map.get(pid):
                raise ValueError(
                    f"Formatting dropped the text of PID {pid}"
                )
        final_map = formatted_map
        for incident in formatting_outcome.incidents:
            debt_reasons.append(
                f"formatting:{incident.pid}:{incident.span_id}: "
                f"unresolved required span ({incident.reason}, "
                f"tier={incident.tier})"
            )
        if progress is not None:
            progress.formatting_done(
                incidents=formatting_outcome.incident_count,
                blocking=formatting_outcome.blocking,
            )

    final_translation = tuple(
        (pid, final_map.get(pid, "")) for pid in snapshot.pids
    )

    reaudited_pids = frozenset(
        pid
        for chunk_id in reaudit_scope
        for pid in chunk_plan.chunk(chunk_id).pids
    ) if reaudit_scope else frozenset()

    integrity = run_integrity_check(
        source=source,
        snapshot=snapshot,
        chunk_plan=chunk_plan,
        config=config,
        det_data=det_data,
        final_translation=dict(final_translation),
        original_translation=current_translation,
        reaudited_pids=reaudited_pids,
    )

    # Uncommitted/no-candidate chunks that still lack text are structural
    # gaps -> debt and (if the whole PID-map is invalid) failed.
    for chunk in chunk_plan.chunks:
        uncovered = [
            pid for pid in chunk.pids if not final_map.get(pid)
        ]
        if uncovered:
            debt_reasons.append(
                f"{chunk.chunk_id}: no valid PID-map after repair "
                f"(uncovered: {uncovered[:5]}{'...' if len(uncovered) > 5 else ''})"
            )

    debt = tuple(dict.fromkeys(debt_reasons))
    terminal = decide_terminal_state(
        chunk_plan=chunk_plan,
        final_translation=dict(final_translation),
        debt_reasons=debt,
        provenance=provenance,
    )
    if progress is not None:
        progress.terminal(status=terminal.status)

    report = {
        "schema": REPAIR_REPORT_SCHEMA,
        "chapter_id": source.chapter_id,
        "source_hash": source.source_hash,
        "snapshot_hash": snapshot.snapshot_hash,
        "chunk_plan_hash": chunk_plan.plan_hash,
        "config_identity": config.config_identity,
        "backend_identity_hash": backend_identity_hash,
        "chapter_hash": chapter_hash,
        "status": terminal.status,
        "rounds": [round_one.to_payload(), round_two.to_payload()],
        "debt_trace": list(debt),
        "final_translation": [list(item) for item in final_translation],
        "integrity": integrity,
        "terminal": {
            "state_id": terminal.state_id,
            "status": terminal.status,
        },
        "formatting": (
            formatting_outcome.to_payload()
            if formatting_outcome is not None
            else None
        ),
    }

    return RepairPhaseResult(
        status=terminal.status,
        rounds=(round_one, round_two),
        debt_trace=debt,
        final_translation=final_translation,
        integrity=integrity,
        terminal=terminal,
        report_payload=report,
        formatting=formatting_outcome,
    )
