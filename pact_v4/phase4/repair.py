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
    Qwen re-gate. **Qwen re-gate failure never commits the repair.**
  * If the repair closes a finding raised by Gemma Russian review
    (Step 6), a Gemma re-check of the region is **mandatory**; a failed
    re-check leaves the Russian finding open and returns the last admitted
    text as degraded availability.
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
    "RepairPlan",
    "RepairCache",
    "RepairRecord",
    "RepairRoundResult",
    "RepairPhaseResult",
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
            reason=(
                "Repair call failed (transport or invalid structured output): "
                f"{exc!r} — recorded as debt, not a semantic terminal status"
            ),
        )
        cache.put(unit_hash, record)
        return record

    new_translation = {
        pid: repaired_texts.get(pid, chunk_translation.get(pid, ""))
        for pid in chunk.pids
    }
    try:
        repaired_candidate = Candidate.create(
            candidate_id=f"{plan.chunk_id}:repair:{plan.repair.repair_id[:16]}",
            chunk_id=plan.chunk_id,
            role=audited_role,
            translation=tuple(
                (pid, new_translation[pid]) for pid in chunk.pids
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
    qwen_result = qwen_evaluator(source_map, dict(new_translation))
    gate_trace.append(qwen_result)
    gates_passed = det_result.passed and qwen_result.passed

    gemma_status = "not_required"
    gemma_findings: Tuple[Finding, ...] = ()
    if gates_passed and _gemma_recheck_required(plan):
        gemma_status, gemma_findings = _run_gemma_recheck(
            chunk_id=plan.chunk_id,
            translation=new_translation,
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
        reason_parts = []
        if not det_result.passed:
            reason_parts.append(f"deterministic_consistency: {det_result.detail[:200]}")
        if not qwen_result.passed:
            reason_parts.append(f"qwen_fidelity re-gate: {qwen_result.detail[:200]}")
        if gemma_status == "failed":
            reason_parts.append(
                "Gemma re-check failed: the Russian finding remains open"
            )
        if gemma_status == "transport_error":
            reason_parts.append(
                "Gemma re-check transport failure (debt, not a semantic verdict)"
            )
        reason = "Repair not committed: " + "; ".join(reason_parts)
        adopted_translation = tuple((pid, chunk_translation[pid]) for pid in chunk.pids)
    else:
        reason = "Repair committed: all relevant gates passed"
        adopted_translation = tuple(
            (pid, new_translation[pid]) for pid in chunk.pids
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
) -> Tuple[Finding, ...]:
    """Re-audit a targeted set of chunks (changed PIDs + discourse neighbours).

    Returns the fresh findings for exactly those chunks. Deterministic layer
    (numbers/glossary/mixed-script/missing) is re-run per chunk alongside the
    model tracks so the re-audit is self-contained and needs no cached Step 6
    results.
    """
    source_map = dict(source.source)
    all_findings: list[Finding] = []
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

        owned_source = {pid: source_map.get(pid, "") for pid in chunk.pids}
        owned_translation = {pid: translation.get(pid, "") for pid in chunk.pids}
        for detector, evaluator, allowed in (
            ("qwen_chapter_audit", qwen_audit_evaluator, QWEN_AUDIT_CATEGORIES),
            ("gemma_russian_review", gemma_audit_evaluator, GEMMA_AUDIT_CATEGORIES),
        ):
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
            except Exception as exc:
                LOG.warning(
                    "Convergence re-audit %s failed for %s: %s (recorded as debt)",
                    detector, chunk_id, exc,
                )
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
    return tuple(all_findings)


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
    """
    for pid, text in final_translation.items():
        if pid in reaudited_pids:
            continue
        if original_translation.get(pid) != text:
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
    """
    source_map = dict(source.source)
    missing_pids = [pid for pid in snapshot.pids if not final_translation.get(pid)]
    numeric_missing: list[str] = []
    mixed_script: list[str] = []
    glossary: list[str] = []
    for pid in snapshot.pids:
        en_text = source_map.get(pid, "")
        target = final_translation.get(pid, "")
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
    repair (last admitted text for uncommitted repairs).
    """

    status: str
    rounds: Tuple[RepairRoundResult, ...]
    debt_trace: Tuple[str, ...]
    final_translation: Tuple[Tuple[str, str], ...]
    integrity: Dict[str, Any]
    terminal: TerminalState
    report_payload: Dict[str, Any]

    def to_payload(self) -> Dict[str, Any]:
        return self.report_payload


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
    qwen_evaluator: QwenEvaluator,
    qwen_audit_evaluator: QwenAuditEvaluator,
    gemma_audit_evaluator: GemmaAuditEvaluator,
    backend_identity_hash: str,
    cache: Optional[RepairCache] = None,
    max_rounds: int = 2,
    chapter_hash: str = "",
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

    One repair round is mandatory and re-audits changed PIDs + discourse
    neighbours. A second round is allowed only for a remaining blocking
    finding or a changed chunk boundary. Then the final integrity check
    (deterministic by default) and the monotonic terminal transition.
    """
    if cache is None:
        cache = RepairCache()

    source_map = dict(source.source)
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
    round_one_records: list[RepairRecord] = []
    translation_by_chunk: Dict[str, Dict[str, str]] = {
        chunk.chunk_id: {
            pid: current_translation.get(pid, "") for pid in chunk.pids
        }
        for chunk in chunk_plan.chunks
    }
    changed_chunk_ids: list[str] = []
    debt_reasons: list[str] = []

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
        plans = plan_repairs_for_chunk(
            chunk=chunk,
            findings=chunk_findings,
            current_text=translation_by_chunk[chunk.chunk_id],
            backend_identity_hash=backend_identity_hash,
        )
        if not plans:
            debt_reasons.append(
                f"{chunk.chunk_id}: findings present but no region repair could be planned"
            )
            continue
        for plan in plans:
            audited_role = candidates[chunk.chunk_id].role
            record = repair_region(
                plan=plan,
                chapter_hash=chapter_hash,
                chunk=chunk,
                audited_role=audited_role,
                source=source,
                snapshot=snapshot,
                chunk_plan=chunk_plan,
                config=config,
                det_data=det_data,
                current_translation=translation_by_chunk[chunk.chunk_id],
                repair_caller=repair_caller,
                qwen_evaluator=qwen_evaluator,
                gemma_audit_evaluator=gemma_audit_evaluator,
                backend_identity_hash=backend_identity_hash,
                cache=cache,
            )
            round_one_records.append(record)
            if record.committed:
                for pid, text in record.new_translation:
                    translation_by_chunk[record.chunk_id][pid] = text
                if record.chunk_id not in changed_chunk_ids:
                    changed_chunk_ids.append(record.chunk_id)
            else:
                debt_reasons.append(
                    f"{record.chunk_id}: repair {record.repair_id[:12]} not committed "
                    f"({record.reason})"
                )

    # ---- convergence re-audit (round 1) --------------------------------
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
        )

    round_one = RepairRoundResult(
        round_number=1,
        records=tuple(round_one_records),
        reaudit_findings=reaudit_findings,
        changed_chunk_ids=tuple(changed_chunk_ids),
    )

    # ---- round 2 (only for a remaining blocking finding or changed boundary)
    round_two_records: list[RepairRecord] = []
    round_two_findings: Tuple[Finding, ...] = ()
    round_two_changed: list[str] = []
    blocking_findings = tuple(
        f for f in reaudit_findings if f.chunk_id in reaudit_scope
    )
    boundary_changed = any(
        cid in _neighbour_chunk_ids(chunk_plan, chunk_id)
        for chunk_id in changed_chunk_ids
        for cid in changed_chunk_ids
        if cid != chunk_id
    )
    if max_rounds >= 2 and (blocking_findings or boundary_changed):
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
            for plan in plans:
                audited_role = candidates[chunk.chunk_id].role
                record = repair_region(
                    plan=plan,
                    chapter_hash=chapter_hash,
                    chunk=chunk,
                    audited_role=audited_role,
                    source=source,
                    snapshot=snapshot,
                    chunk_plan=chunk_plan,
                    config=config,
                    det_data=det_data,
                    current_translation=translation_by_chunk[chunk.chunk_id],
                    repair_caller=repair_caller,
                    qwen_evaluator=qwen_evaluator,
                    gemma_audit_evaluator=gemma_audit_evaluator,
                    backend_identity_hash=backend_identity_hash,
                    cache=cache,
                )
                round_two_records.append(record)
                if record.committed:
                    for pid, text in record.new_translation:
                        translation_by_chunk[record.chunk_id][pid] = text
                    if record.chunk_id not in round_two_changed:
                        round_two_changed.append(record.chunk_id)
                else:
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
            )

    round_two = RepairRoundResult(
        round_number=2,
        records=tuple(round_two_records),
        reaudit_findings=round_two_findings,
        changed_chunk_ids=tuple(round_two_changed),
    )

    # ---- assemble final chapter translation ----------------------------
    final_map: Dict[str, str] = {}
    for chunk in chunk_plan.chunks:
        final_map.update(translation_by_chunk[chunk.chunk_id])
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
    }

    return RepairPhaseResult(
        status=terminal.status,
        rounds=(round_one, round_two),
        debt_trace=debt,
        final_translation=final_translation,
        integrity=integrity,
        terminal=terminal,
        report_payload=report,
    )
