"""Phase 4 (B2) unit + integration tests for pact_v4.phase4.repair.

Covers the acceptance criteria for Phase 4A/4A2/4B:

  * repair planning is finding-linked through the region resolver;
  * ``full_sentence_rewrite`` requires a documented reason;
  * a challenge requires evidence and never auto-accepts;
  * Qwen re-gate failure never commits a repair;
  * Gemma re-check is mandatory for Gemma-raised findings, and a failed
    re-check leaves the Russian finding open (degraded availability);
  * one mandatory convergence round, a second only on trigger;
  * monotonic terminal transition (complete / accepted_degraded / failed);
  * transport failure at a repair call is debt/incomplete, never a
    semantic terminal status;
  * resume reuses the repair cache (same findings/re-gates);
  * dual-mode parity: the same canned repair output through a local fake
    backend and a remote OpenCode fake backend yields identical repair
    decisions, gate trace and terminal status;
  * the repair module never imports local lifecycle adapters (import guard).
"""
from __future__ import annotations

import inspect
import json
from typing import Dict, Mapping, Optional, Tuple

import pytest

from pact_v4.phase1.models import (
    Candidate,
    ChunkPlan,
    ChunkPlanArtifact,
    ConfigArtifact,
    GateResult,
    Provenance,
    Region,
    Snapshot,
    SourceArtifact,
    canonical_json_hash,
)
from pact_v4.phase2.cascade import (
    DeterministicGateData,
    deterministic_consistency_gate,
)
from pact_v4.phase3.assembly import AssembledChapter
from pact_v4.phase3.findings import Finding, FindingStore
from pact_v4.phase3.region_resolver import resolve_regions
from pact_v4.phase4 import repair as repair_module
from pact_v4.phase4.repair import (
    REPAIR_POLICY_VERSION,
    RepairCache,
    RepairRecord,
    SoftFindingsPolicy,
    _re_gate_region,
    _reaudit_chunks,
    _repair_unit_hash,
    _run_repair_round,
    filter_soft_findings,
    plan_repair_challenge,
    plan_repairs_for_chunk,
    repair_region,
    run_repair_phase,
)


def _hash(seed: str) -> str:
    return canonical_json_hash({"seed": seed})


def _source(chapter_id: str = "ch044", texts: Dict[str, str] | None = None) -> SourceArtifact:
    # Digit-free source so the deterministic re-gate (number preservation) is
    # not tripped by the canned repaired texts.
    texts = texts or {}
    pairs = tuple(
        (f"p{i:05d}", texts.get(f"p{i:05d}", f"English sentence {chr(97 + i)}."))
        for i in range(10)
    )
    return SourceArtifact(chapter_id=chapter_id, source=pairs)


def _snapshot(source: SourceArtifact) -> Snapshot:
    return Snapshot(
        chapter_id=source.chapter_id,
        pids=tuple(pid for pid, _ in source.source),
        context="ctx-v1",
        glossary_hash=_hash("glossary"),
        book_memory_hash=_hash("book_memory"),
        chapter_memory_hash=_hash("chapter_memory"),
    )


def _one_chunk_plan(snapshot: Snapshot) -> Tuple[ChunkPlanArtifact, ChunkPlan]:
    chunk = ChunkPlan(
        chunk_id="chunk0001", snapshot_hash=snapshot.snapshot_hash,
        pids=snapshot.pids, word_counts=tuple(50 for _ in snapshot.pids),
    )
    artifact = ChunkPlanArtifact.create(snapshot, (chunk,))
    return artifact, chunk


def _config() -> ConfigArtifact:
    return ConfigArtifact(version="v1", values={"model": "qwen-mock"})


def _candidate(
    *, chunk: ChunkPlan, suffix: str, source: SourceArtifact, snapshot: Snapshot,
    chunk_plan: ChunkPlanArtifact, config: ConfigArtifact, overrides: Dict[str, str] | None = None,
) -> Candidate:
    overrides = overrides or {}
    translation = tuple(
        (pid, overrides.get(pid, f"Перевод {int(pid[1:])}.")) for pid in chunk.pids
    )
    return Candidate.create(
        candidate_id=f"{chunk.chunk_id}:{suffix}",
        chunk_id=chunk.chunk_id,
        role="fidelity_first",
        translation=translation,
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config,
    )


def _provenance(*, source, snapshot, chunk_plan, config, chapter_hash) -> Provenance:
    return Provenance(
        source_hash=source.source_hash,
        chapter_snapshot_hash=snapshot.snapshot_hash,
        chunk_plan_hash=chunk_plan.plan_hash,
        prompt_bundle_hash=canonical_json_hash({"artifact": "pact-v4-phase4/v1", "chapter_hash": chapter_hash}),
        config_identity=config.config_identity,
        code_version="pact-v4-b2-test/1",
        policy_versions={"repair": REPAIR_POLICY_VERSION},
    )


def _finding(
    *, chunk_id: str, candidate_id: str, pid: str, detector: str = "qwen_chapter_audit",
    category: str = "omission", note: str = "dropped clause", snapshot_id: str,
    chapter_hash: str = "",
) -> Finding:
    return Finding(
        detector=detector,
        category=category,
        evidence={"note": note, "excerpt": ""},
        region=Region(pid=pid, start=0, end=0),
        source_id=chapter_hash or snapshot_id,
        snapshot_id=snapshot_id,
        chunk_id=chunk_id,
        candidate_id=candidate_id,
        policy_version="qwen_chapter_audit/v1",
    )


def _env():
    source = _source()
    snapshot = _snapshot(source)
    chunk_plan, chunk = _one_chunk_plan(snapshot)
    config = _config()
    candidate = _candidate(
        chunk=chunk, suffix="A", source=source, snapshot=snapshot,
        chunk_plan=chunk_plan, config=config,
    )
    candidates = {chunk.chunk_id: candidate}
    chapter = AssembledChapter.assemble(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config,
        candidates=candidates,
    )
    handoff = [{
        "chunk_id": chunk.chunk_id,
        "plan_pids": list(chunk.pids),
        "status": "audited",
        "committed": True,
        "audited_candidate_id": candidate.candidate_id,
        "audited_role": candidate.role,
        "uncovered_pids": [],
        "audit_status": "findings_present",
        "quarantine_reason": None,
    }]
    return source, snapshot, chunk_plan, chunk, config, candidate, candidates, chapter, handoff


class ScriptedRepairCaller:
    """Fake ``RepairCaller`` returning canned repaired text (or raising)."""

    def __init__(self, repaired_texts: Mapping[str, str] | None = None,
                 *, fail: Optional[Exception] = None, truncated: bool = False) -> None:
        self._repaired = dict(repaired_texts or {})
        self.fail = fail
        self.truncated = truncated
        self.calls: list = []

    def __call__(self, *, chunk_id, source, translation, region, findings) -> str:
        self.calls.append({
            "chunk_id": chunk_id,
            "source": dict(source),
            "translation": dict(translation),
            "region": {"pid": region.pid, "start": region.start, "end": region.end},
            "findings": list(findings),
        })
        if self.fail is not None:
            raise self.fail
        payload = {"repaired": dict(self._repaired), "reason": "scripted"}
        raw = json.dumps(payload, ensure_ascii=False)
        if self.truncated:
            return raw[:-5]
        return raw


class TargetedRepairCaller(ScriptedRepairCaller):
    """Fake ``RepairCaller`` returning a fixed text for the *requested* PID.

    Unlike ``ScriptedRepairCaller`` (which returns the same dict for every
    call and therefore breaks when several single-PID plans share one fake),
    this returns exactly the region's PID, so it is safe to reuse across
    multiple plans/regions.
    """

    def __call__(self, *, chunk_id, source, translation, region, findings) -> str:
        self.calls.append({
            "chunk_id": chunk_id,
            "region": {"pid": region.pid, "start": region.start, "end": region.end},
            "findings": list(findings),
        })
        if self.fail is not None:
            raise self.fail
        text = self._repaired.get(region.pid, "Исправленный текст.")
        payload = {"repaired": {region.pid: text}, "reason": "scripted"}
        raw = json.dumps(payload, ensure_ascii=False)
        if self.truncated:
            return raw[:-5]
        return raw


class ScriptedQwenGate:
    def __init__(self, passed: bool = True, detail: str = "OK") -> None:
        self.passed = passed
        self.detail = detail
        self.calls: list = []

    def __call__(self, source, translation) -> GateResult:
        self.calls.append((dict(source), dict(translation)))
        return GateResult(gate="qwen_fidelity", passed=self.passed, detail=self.detail)


class ScriptedRegionGate:
    """Fake narrow ``RegionFidelityEvaluator`` (L2b ``region_fidelity_gate``)."""

    def __init__(self, passed: bool = True, detail: str = "OK") -> None:
        self.passed = passed
        self.detail = detail
        self.calls: list = []

    def __call__(self, *, source_text, repaired_text, region) -> GateResult:
        self.calls.append({
            "source_text": source_text,
            "repaired_text": repaired_text,
            "region": {"pid": region.pid, "start": region.start, "end": region.end},
        })
        return GateResult(gate="qwen_fidelity", passed=self.passed, detail=self.detail)


class ScriptedGemmaAudit:
    def __init__(self, issues: list | None = None, *, fail: Optional[Exception] = None) -> None:
        self._issues = list(issues or [])
        self.fail = fail
        self.calls: list = []

    def __call__(self, *, chunk_id, translation) -> str:
        self.calls.append((chunk_id, dict(translation)))
        if self.fail is not None:
            raise self.fail
        return json.dumps({"issues": self._issues}, ensure_ascii=False)


class ScriptedQwenAudit:
    def __init__(self, issues: list | None = None, *, fail: Optional[Exception] = None) -> None:
        self._issues = list(issues or [])
        self.fail = fail
        self.calls: list = []

    def __call__(self, *, chunk_id, source, translation) -> str:
        self.calls.append((chunk_id, dict(source), dict(translation)))
        if self.fail is not None:
            raise self.fail
        return json.dumps({"issues": self._issues}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 4A planning / contract
# ---------------------------------------------------------------------------


def test_plan_repairs_is_finding_linked_via_region_resolver():
    source, snapshot, chunk_plan, chunk, config, candidate, candidates, chapter, handoff = _env()
    finding = _finding(
        chunk_id=chunk.chunk_id, candidate_id=candidate.candidate_id,
        pid=chunk.pids[0], snapshot_id=snapshot.snapshot_hash,
    )
    plans = plan_repairs_for_chunk(
        chunk=chunk, findings=[finding], current_text=candidate.as_pid_map(),
        backend_identity_hash=_hash("backend"),
    )
    assert len(plans) == 1
    plan = plans[0]
    assert plan.repair.action == "region_edit"
    assert plan.repair.target_pids == (chunk.pids[0],)
    assert plan.repair.finding_ids == (finding.content_hash,)
    assert plan.repair.chunk_id == chunk.chunk_id
    assert plan.repair.auto_accepted is False


def test_plan_repairs_full_sentence_rewrite_requires_documented_reason():
    source, snapshot, chunk_plan, chunk, config, candidate, candidates, chapter, handoff = _env()
    finding = _finding(
        chunk_id=chunk.chunk_id, candidate_id=candidate.candidate_id,
        pid=chunk.pids[0], snapshot_id=snapshot.snapshot_hash,
    )
    plans = plan_repairs_for_chunk(
        chunk=chunk, findings=[finding], current_text=candidate.as_pid_map(),
        backend_identity_hash=_hash("backend"),
        action_override="full_sentence_rewrite",
    )
    assert plans[0].repair.action == "full_sentence_rewrite"
    assert plans[0].repair.full_sentence_reason  # auto-documented


def test_plan_challenge_without_evidence_rejected():
    source, snapshot, chunk_plan, chunk, config, candidate, candidates, chapter, handoff = _env()
    finding = _finding(
        chunk_id=chunk.chunk_id, candidate_id=candidate.candidate_id,
        pid=chunk.pids[0], snapshot_id=snapshot.snapshot_hash,
    )
    region = resolve_regions([finding]).regions[0]
    with pytest.raises(ValueError, match="requires documented evidence"):
        plan_repair_challenge(
            chunk_id=chunk.chunk_id, region=region, findings=[finding],
            challenge_evidence="  ", backend_identity_hash=_hash("backend"),
        )


def test_plan_challenge_with_evidence_builds_region_edit_plan():
    source, snapshot, chunk_plan, chunk, config, candidate, candidates, chapter, handoff = _env()
    finding = _finding(
        chunk_id=chunk.chunk_id, candidate_id=candidate.candidate_id,
        pid=chunk.pids[0], snapshot_id=snapshot.snapshot_hash,
    )
    region = resolve_regions([finding]).regions[0]
    plan = plan_repair_challenge(
        chunk_id=chunk.chunk_id, region=region, findings=[finding],
        challenge_evidence="The finding misreads the source; translation is faithful.",
        backend_identity_hash=_hash("backend"),
    )
    assert plan.repair.action == "region_edit"
    assert plan.repair.auto_accepted is False
    assert "faithful" in plan.repair.instructions


# ---------------------------------------------------------------------------
# 4A execution + re-gates
# ---------------------------------------------------------------------------


def _run_single_repair(
    *,
    repair_caller, qwen_gate, gemma_audit=None,
    findings_override=None,
):
    source, snapshot, chunk_plan, chunk, config, candidate, candidates, chapter, handoff = _env()
    findings = findings_override or [
        _finding(
            chunk_id=chunk.chunk_id, candidate_id=candidate.candidate_id,
            pid=chunk.pids[0], snapshot_id=snapshot.snapshot_hash,
        )
    ]
    plans = plan_repairs_for_chunk(
        chunk=chunk, findings=findings, current_text=candidate.as_pid_map(),
        backend_identity_hash=_hash("backend"),
    )
    repaired_pid = chunk.pids[0]
    record = repair_region(
        plan=plans[0],
        chapter_hash=chapter.chapter_hash,
        chunk=chunk,
        audited_role="fidelity_first",
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config,
        det_data=DeterministicGateData(),
        current_translation=candidate.as_pid_map(),
        repair_caller=repair_caller,
        qwen_evaluator=qwen_gate,
        gemma_audit_evaluator=gemma_audit or ScriptedGemmaAudit(),
        backend_identity_hash=_hash("backend"),
        cache=RepairCache(),
    )
    return record, candidate, chunk, source


def test_repair_commits_when_qwen_regate_passes():
    source, snapshot, chunk_plan, chunk, config, candidate, candidates, chapter, handoff = _env()
    pid = chunk.pids[0]
    repaired_text = "Исправленный перевод."
    repair_caller = ScriptedRepairCaller({pid: repaired_text})
    qwen_gate = ScriptedQwenGate(passed=True)
    record, _cand, _chunk, _src = _run_single_repair(
        repair_caller=repair_caller, qwen_gate=qwen_gate,
    )
    assert record.committed is True
    new_map = dict(record.new_translation)
    assert new_map[pid] == repaired_text
    # The other PIDs are kept verbatim.
    assert new_map[chunk.pids[1]] == candidate.as_pid_map()[chunk.pids[1]]
    assert len(qwen_gate.calls) == 1
    assert any(g.gate == "deterministic_consistency" for g in record.gate_trace)
    assert any(g.gate == "qwen_fidelity" for g in record.gate_trace)


def test_repair_not_committed_on_qwen_regate_failure():
    source, snapshot, chunk_plan, chunk, config, candidate, candidates, chapter, handoff = _env()
    pid = chunk.pids[0]
    repair_caller = ScriptedRepairCaller({pid: "Исправленный перевод."})
    qwen_gate = ScriptedQwenGate(passed=False, detail="meaning drift")
    record, _cand, _chunk, _src = _run_single_repair(
        repair_caller=repair_caller, qwen_gate=qwen_gate,
    )
    assert record.committed is False
    # Last admitted text is kept (degraded availability).
    assert dict(record.new_translation)[pid] == candidate.as_pid_map()[pid]
    assert "not committed" in record.reason
    assert "meaning drift" in record.reason


def test_repair_transport_failure_is_debt_not_terminal():
    source, snapshot, chunk_plan, chunk, config, candidate, candidates, chapter, handoff = _env()
    pid = chunk.pids[0]
    repair_caller = ScriptedRepairCaller(fail=RuntimeError("llama-server timeout"))
    qwen_gate = ScriptedQwenGate(passed=True)
    record, _cand, _chunk, _src = _run_single_repair(
        repair_caller=repair_caller, qwen_gate=qwen_gate,
    )
    assert record.committed is False
    assert "transport" in record.reason or "failed" in record.reason
    # No fabricated text; last admitted text kept.
    assert dict(record.new_translation)[pid] == candidate.as_pid_map()[pid]


def test_repair_invalid_structured_output_is_debt_not_terminal():
    source, snapshot, chunk_plan, chunk, config, candidate, candidates, chapter, handoff = _env()
    pid = chunk.pids[0]
    repair_caller = ScriptedRepairCaller({pid: "x"}, truncated=True)
    qwen_gate = ScriptedQwenGate(passed=True)
    record, _cand, _chunk, _src = _run_single_repair(
        repair_caller=repair_caller, qwen_gate=qwen_gate,
    )
    assert record.committed is False
    assert "transport or invalid" in record.reason


def test_repair_caller_never_called_with_foreign_pids():
    # The repair caller receives the chunk's own source/translation only.
    source, snapshot, chunk_plan, chunk, config, candidate, candidates, chapter, handoff = _env()
    pid = chunk.pids[0]
    repair_caller = ScriptedRepairCaller({pid: "Исправленный перевод."})
    qwen_gate = ScriptedQwenGate(passed=True)
    _run_single_repair(repair_caller=repair_caller, qwen_gate=qwen_gate)
    call = repair_caller.calls[0]
    assert set(call["source"]) == set(chunk.pids)
    assert set(call["translation"]) == set(chunk.pids)
    assert call["region"]["pid"] == pid


# ---------------------------------------------------------------------------
# 4A2 Gemma re-check
# ---------------------------------------------------------------------------


def _gemma_finding_env():
    source, snapshot, chunk_plan, chunk, config, candidate, candidates, chapter, handoff = _env()
    pid = chunk.pids[0]
    finding = _finding(
        chunk_id=chunk.chunk_id, candidate_id=candidate.candidate_id,
        pid=pid, detector="gemma_russian_review", category="calque",
        note="word-for-word calque", snapshot_id=snapshot.snapshot_hash,
    )
    return source, snapshot, chunk_plan, chunk, config, candidate, chapter, finding, pid


def test_gemma_recheck_mandatory_and_passes_for_gemma_finding():
    source, snapshot, chunk_plan, chunk, config, candidate, chapter, finding, pid = _gemma_finding_env()
    repair_caller = ScriptedRepairCaller({pid: "Исправленный перевод."})
    qwen_gate = ScriptedQwenGate(passed=True)
    gemma_audit = ScriptedGemmaAudit(issues=[])  # clean re-check
    plans = plan_repairs_for_chunk(
        chunk=chunk, findings=[finding], current_text=candidate.as_pid_map(),
        backend_identity_hash=_hash("backend"),
    )
    record = repair_region(
        plan=plans[0], chapter_hash=chapter.chapter_hash, chunk=chunk,
        audited_role="fidelity_first",
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config,
        det_data=DeterministicGateData(), current_translation=candidate.as_pid_map(),
        repair_caller=repair_caller, qwen_evaluator=qwen_gate,
        gemma_audit_evaluator=gemma_audit, backend_identity_hash=_hash("backend"),
        cache=RepairCache(),
    )
    # Gemma re-check was actually invoked (mandatory for Gemma findings).
    assert len(gemma_audit.calls) == 1
    assert record.gemma_recheck == "passed"
    assert record.committed is True


def test_gemma_recheck_failure_leaves_russian_finding_open():
    source, snapshot, chunk_plan, chunk, config, candidate, chapter, finding, pid = _gemma_finding_env()
    repair_caller = ScriptedRepairCaller({pid: "Исправленный перевод."})
    qwen_gate = ScriptedQwenGate(passed=True)
    gemma_audit = ScriptedGemmaAudit(issues=[
        {"pid": pid, "category": "calque", "note": "still a calque"}
    ])
    plans = plan_repairs_for_chunk(
        chunk=chunk, findings=[finding], current_text=candidate.as_pid_map(),
        backend_identity_hash=_hash("backend"),
    )
    record = repair_region(
        plan=plans[0], chapter_hash=chapter.chapter_hash, chunk=chunk,
        audited_role="fidelity_first",
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config,
        det_data=DeterministicGateData(), current_translation=candidate.as_pid_map(),
        repair_caller=repair_caller, qwen_evaluator=qwen_gate,
        gemma_audit_evaluator=gemma_audit, backend_identity_hash=_hash("backend"),
        cache=RepairCache(),
    )
    assert record.gemma_recheck == "failed"
    assert record.committed is False
    # The Russian finding stays open; last admitted text is returned.
    assert dict(record.new_translation)[pid] == candidate.as_pid_map()[pid]
    assert "Gemma re-check failed" in record.reason


def test_gemma_recheck_transport_failure_is_debt_not_verdict():
    source, snapshot, chunk_plan, chunk, config, candidate, chapter, finding, pid = _gemma_finding_env()
    repair_caller = ScriptedRepairCaller({pid: "Исправленный перевод."})
    qwen_gate = ScriptedQwenGate(passed=True)
    gemma_audit = ScriptedGemmaAudit(fail=RuntimeError("gemma timeout"))
    plans = plan_repairs_for_chunk(
        chunk=chunk, findings=[finding], current_text=candidate.as_pid_map(),
        backend_identity_hash=_hash("backend"),
    )
    record = repair_region(
        plan=plans[0], chapter_hash=chapter.chapter_hash, chunk=chunk,
        audited_role="fidelity_first",
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config,
        det_data=DeterministicGateData(), current_translation=candidate.as_pid_map(),
        repair_caller=repair_caller, qwen_evaluator=qwen_gate,
        gemma_audit_evaluator=gemma_audit, backend_identity_hash=_hash("backend"),
        cache=RepairCache(),
    )
    assert record.gemma_recheck == "transport_error"
    assert record.committed is False


def test_gemma_recheck_not_required_for_qwen_finding():
    source, snapshot, chunk_plan, chunk, config, candidate, candidates, chapter, handoff = _env()
    finding = _finding(
        chunk_id=chunk.chunk_id, candidate_id=candidate.candidate_id,
        pid=chunk.pids[0], snapshot_id=snapshot.snapshot_hash,
    )
    repair_caller = ScriptedRepairCaller({chunk.pids[0]: "Исправленный перевод."})
    qwen_gate = ScriptedQwenGate(passed=True)
    gemma_audit = ScriptedGemmaAudit()
    plans = plan_repairs_for_chunk(
        chunk=chunk, findings=[finding], current_text=candidate.as_pid_map(),
        backend_identity_hash=_hash("backend"),
    )
    record = repair_region(
        plan=plans[0], chapter_hash=chapter.chapter_hash, chunk=chunk,
        audited_role="fidelity_first",
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config,
        det_data=DeterministicGateData(), current_translation=candidate.as_pid_map(),
        repair_caller=repair_caller, qwen_evaluator=qwen_gate,
        gemma_audit_evaluator=gemma_audit, backend_identity_hash=_hash("backend"),
        cache=RepairCache(),
    )
    assert record.gemma_recheck == "not_required"
    assert gemma_audit.calls == []


# ---------------------------------------------------------------------------
# Repair cache (resume safety)
# ---------------------------------------------------------------------------


def test_repair_cache_round_trip_and_reuse():
    source, snapshot, chunk_plan, chunk, config, candidate, candidates, chapter, handoff = _env()
    pid = chunk.pids[0]
    repair_caller = ScriptedRepairCaller({pid: "Исправленный перевод."})
    qwen_gate = ScriptedQwenGate(passed=True)
    findings = [_finding(
        chunk_id=chunk.chunk_id, candidate_id=candidate.candidate_id,
        pid=pid, snapshot_id=snapshot.snapshot_hash,
    )]
    plans = plan_repairs_for_chunk(
        chunk=chunk, findings=findings, current_text=candidate.as_pid_map(),
        backend_identity_hash=_hash("backend"),
    )
    cache = RepairCache()
    record = repair_region(
        plan=plans[0], chapter_hash=chapter.chapter_hash, chunk=chunk,
        audited_role="fidelity_first",
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config,
        det_data=DeterministicGateData(), current_translation=candidate.as_pid_map(),
        repair_caller=repair_caller, qwen_evaluator=qwen_gate,
        gemma_audit_evaluator=ScriptedGemmaAudit(),
        backend_identity_hash=_hash("backend"), cache=cache,
    )
    assert record.committed is True

    # Round-trip through the persisted payload.
    reloaded = RepairCache.from_payload(cache.to_payload())
    unit_hash = list(reloaded._store)[0]
    reused = reloaded.get(unit_hash)
    assert reused is not None
    assert reused.committed is True
    assert dict(reused.new_translation)[pid] == "Исправленный перевод."


def test_repair_cache_rejects_foreign_schema():
    with pytest.raises(ValueError, match="Foreign identity"):
        RepairCache.from_payload({"schema": "pact-v4-other/v1", "units": []})


def test_repair_unit_hash_bumps_with_policy_version():
    # B12-F4 (RV4 HIGH): the repair unit identity embeds the repair policy
    # version. A unit planned under the pre-F3 policy (v1) must produce a
    # different hash than the same unit under the post-F4 policy (v2), so a
    # legacy cache entry can never be looked up after the fail-closed fix.
    source, snapshot, chunk_plan, chunk, config, candidate, candidates, chapter, handoff = _env()
    pid = chunk.pids[0]
    findings = [_finding(
        chunk_id=chunk.chunk_id, candidate_id=candidate.candidate_id,
        pid=pid, snapshot_id=snapshot.snapshot_hash,
    )]
    plans = plan_repairs_for_chunk(
        chunk=chunk, findings=findings, current_text=candidate.as_pid_map(),
        backend_identity_hash=_hash("backend"),
    )
    plan = plans[0]
    legacy_hash = _repair_unit_hash(
        chapter_hash=chapter.chapter_hash, plan=plan,
        backend_identity_hash=_hash("backend"),
        policy_version="pact-v4-repair-policy/v1",  # pre-F3 generation
    )
    current_hash = _repair_unit_hash(
        chapter_hash=chapter.chapter_hash, plan=plan,
        backend_identity_hash=_hash("backend"),
        policy_version=REPAIR_POLICY_VERSION,
    )
    assert REPAIR_POLICY_VERSION == "pact-v4-repair-policy/v2"
    assert legacy_hash != current_hash


def test_repair_cache_legacy_entry_not_supplied_under_post_f4_contract():
    # B12-F4 (RV4 HIGH): a RepairCache that only holds an entry under the
    # pre-F3 unit hash (policy v1) must not supply it for the same repair
    # planned under the post-F4 policy — the hash lookup misses, so the
    # repair re-runs through the fail-closed re-gate instead of reusing a
    # record possibly committed under the old bool("false") truthiness.
    source, snapshot, chunk_plan, chunk, config, candidate, candidates, chapter, handoff = _env()
    pid = chunk.pids[0]
    findings = [_finding(
        chunk_id=chunk.chunk_id, candidate_id=candidate.candidate_id,
        pid=pid, snapshot_id=snapshot.snapshot_hash,
    )]
    plans = plan_repairs_for_chunk(
        chunk=chunk, findings=findings, current_text=candidate.as_pid_map(),
        backend_identity_hash=_hash("backend"),
    )
    plan = plans[0]

    legacy_hash = _repair_unit_hash(
        chapter_hash=chapter.chapter_hash, plan=plan,
        backend_identity_hash=_hash("backend"),
        policy_version="pact-v4-repair-policy/v1",
    )
    current_hash = _repair_unit_hash(
        chapter_hash=chapter.chapter_hash, plan=plan,
        backend_identity_hash=_hash("backend"),
        policy_version=REPAIR_POLICY_VERSION,
    )
    assert legacy_hash != current_hash

    # Simulate a pre-F3 cache: an entry keyed by the legacy hash, committed
    # under the old truthiness semantics (this is exactly the record shape a
    # malformed ``"passed": "false"`` could have produced pre-F3).
    stale = RepairRecord(
        repair_id=plan.repair.repair_id, chunk_id=plan.chunk_id,
        finding_ids=plan.repair.finding_ids, target_pids=plan.repair.target_pids,
        action=plan.repair.action,
        new_translation=tuple((pid, "старый текст") for pid in chunk.pids),
        gate_trace=(), gemma_recheck="not_required",
        committed=True, reason="pre-F3 stale record",
    )
    cache = RepairCache()
    cache.put(legacy_hash, stale)

    # Under the post-F4 contract the legacy entry must not be served.
    assert cache.get(current_hash) is None
    assert cache.get(legacy_hash) is stale  # present, but unreachable via v2

    # Re-running the repair under the current policy executes it afresh and
    # stores the new record under the current hash.
    repair_caller = ScriptedRepairCaller({pid: "Исправленный перевод."})
    record = repair_region(
        plan=plan, chapter_hash=chapter.chapter_hash, chunk=chunk,
        audited_role="fidelity_first",
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config,
        det_data=DeterministicGateData(), current_translation=candidate.as_pid_map(),
        repair_caller=repair_caller, qwen_evaluator=ScriptedQwenGate(passed=True),
        gemma_audit_evaluator=ScriptedGemmaAudit(),
        backend_identity_hash=_hash("backend"), cache=cache,
    )
    assert repair_caller.calls  # the repair actually re-ran, not reused
    assert record is not stale
    assert record.committed is True
    assert cache.get(current_hash) is not None

    # Matching post-F4 cache reuse still works: a second identical repair
    # now hits the current-hash entry without calling the model again.
    calls_before = len(repair_caller.calls)
    record2 = repair_region(
        plan=plan, chapter_hash=chapter.chapter_hash, chunk=chunk,
        audited_role="fidelity_first",
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config,
        det_data=DeterministicGateData(), current_translation=candidate.as_pid_map(),
        repair_caller=repair_caller, qwen_evaluator=ScriptedQwenGate(passed=True),
        gemma_audit_evaluator=ScriptedGemmaAudit(),
        backend_identity_hash=_hash("backend"), cache=cache,
    )
    assert record2 is record or record2 == record
    assert len(repair_caller.calls) == calls_before


# ---------------------------------------------------------------------------
# 4B convergence + terminal
# ---------------------------------------------------------------------------


def _run_phase(
    *,
    repair_caller, region_gate=None, gemma_audit=None, qwen_audit=None,
    findings_override=None, candidate_overrides=None,
    max_rounds: int = 2,
    soft_findings_policy=None,
):
    source, snapshot, chunk_plan, chunk, config, candidate, candidates, chapter, handoff = _env()
    overrides = candidate_overrides or {}
    if overrides:
        candidate = _candidate(
            chunk=chunk, suffix="A", source=source, snapshot=snapshot,
            chunk_plan=chunk_plan, config=config, overrides=overrides,
        )
        candidates = {chunk.chunk_id: candidate}
        chapter = AssembledChapter.assemble(
            source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config,
            candidates=candidates,
        )
    findings = findings_override or [
        _finding(
            chunk_id=chunk.chunk_id, candidate_id=candidate.candidate_id,
            pid=chunk.pids[0], snapshot_id=snapshot.snapshot_hash,
        )
    ]
    store = FindingStore.create(snapshot.snapshot_hash, findings)
    provenance = _provenance(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config,
        chapter_hash=chapter.chapter_hash,
    )
    result = run_repair_phase(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config,
        provenance=provenance, det_data=DeterministicGateData(),
        handoff_chunks=handoff, findings_store=store, candidates=candidates,
        current_translation=chapter.as_pid_map(),
        repair_caller=repair_caller,
        region_fidelity_gate=region_gate or ScriptedRegionGate(),
        qwen_audit_evaluator=qwen_audit or ScriptedQwenAudit(),
        gemma_audit_evaluator=gemma_audit or ScriptedGemmaAudit(),
        backend_identity_hash=_hash("backend"),
        cache=RepairCache(),
        max_rounds=max_rounds,
        soft_findings_policy=soft_findings_policy,
    )
    return result, source, snapshot, chunk_plan, chunk, config, candidate


def test_terminal_complete_when_repair_commits_and_reaudit_clean():
    source, snapshot, chunk_plan, chunk, config, candidate, candidates, chapter, handoff = _env()
    pid = chunk.pids[0]
    result, *_ = _run_phase(
        repair_caller=ScriptedRepairCaller({pid: "Исправленный перевод."}),
        region_gate=ScriptedRegionGate(passed=True),
        qwen_audit=ScriptedQwenAudit(issues=[]),
        gemma_audit=ScriptedGemmaAudit(issues=[]),
    )
    assert result.status == "complete"
    assert result.integrity["status"] == "complete"
    assert result.debt_trace == ()
    assert result.terminal.is_terminal
    assert result.terminal.status == "complete"


def test_terminal_accepted_degraded_when_qwen_regate_fails():
    source, snapshot, chunk_plan, chunk, config, candidate, candidates, chapter, handoff = _env()
    pid = chunk.pids[0]
    result, *_ = _run_phase(
        repair_caller=ScriptedRepairCaller({pid: "Исправленный перевод."}),
        region_gate=ScriptedRegionGate(passed=False, detail="meaning drift"),
    )
    assert result.status == "accepted_degraded"
    assert result.debt_trace
    # Valid PID map is retained (last admitted text covers all PIDs).
    final = dict(result.final_translation)
    assert all(final.get(p) for p in snapshot.pids)
    assert result.terminal.status == "accepted_degraded"


def test_terminal_failed_when_no_valid_pid_map():
    source, snapshot, chunk_plan, chunk, config, candidate, candidates, chapter, handoff = _env()
    # Empty the committed translation for the chunk -> no valid PID map.
    broken = {pid: "" for pid in chunk.pids}
    result, *_ = _run_phase(
        repair_caller=ScriptedRepairCaller(fail=RuntimeError("no server")),
        region_gate=ScriptedRegionGate(passed=True),
        candidate_overrides=broken,
    )
    assert result.status == "failed"
    assert result.terminal.status == "failed"
    assert any("uncovered" in reason or "no valid PID-map" in reason for reason in result.debt_trace)


def test_terminal_transition_is_monotonic_write_once():
    source, snapshot, chunk_plan, chunk, config, candidate, candidates, chapter, handoff = _env()
    pid = chunk.pids[0]
    result, *_ = _run_phase(
        repair_caller=ScriptedRepairCaller({pid: "Исправленный перевод."}),
        region_gate=ScriptedRegionGate(passed=True),
    )
    terminal = result.terminal
    with pytest.raises(ValueError, match="Non-monotonic"):
        terminal.transition_to("in_progress")


def test_second_round_triggered_for_blocking_finding():
    # A blocking finding survives round 1 (re-audit still flags the PID),
    # so a second round is allowed (and uses full_sentence_rewrite).
    source, snapshot, chunk_plan, chunk, config, candidate, candidates, chapter, handoff = _env()
    pid = chunk.pids[0]
    qwen_audit = ScriptedQwenAudit(issues=[
        {"pid": pid, "category": "omission", "note": "still missing"}
    ])
    result, *_ = _run_phase(
        repair_caller=ScriptedRepairCaller({pid: "Исправленный перевод."}),
        region_gate=ScriptedRegionGate(passed=True),
        qwen_audit=qwen_audit,
    )
    # Round 2 ran because a blocking finding remained.
    assert len(result.rounds) == 2
    assert result.rounds[1].records, "round 2 executed repairs"
    assert all(rec.action == "full_sentence_rewrite" for rec in result.rounds[1].records)


def test_no_second_round_without_blocking_finding():
    source, snapshot, chunk_plan, chunk, config, candidate, candidates, chapter, handoff = _env()
    pid = chunk.pids[0]
    result, *_ = _run_phase(
        repair_caller=ScriptedRepairCaller({pid: "Исправленный перевод."}),
        region_gate=ScriptedRegionGate(passed=True),
        qwen_audit=ScriptedQwenAudit(issues=[]),
        gemma_audit=ScriptedGemmaAudit(issues=[]),
        max_rounds=2,
    )
    assert len(result.rounds) == 2  # second round result is empty
    assert result.rounds[1].records == ()


# ---------------------------------------------------------------------------
# Backend-neutrality (dual-mode import guard)
# ---------------------------------------------------------------------------


def test_repair_module_does_not_import_local_lifecycle():
    # Inspect actual import statements (AST), not docstrings, so a doc note
    # mentioning "model_lifecycle" cannot trip the guard.
    import ast as _ast
    source = inspect.getsource(repair_module)
    tree = _ast.parse(source)
    imports: list[str] = []
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, _ast.ImportFrom):
            imports.append(node.module or "")
    for forbidden in (
        "pact_v4.runtime.model_lifecycle",
        "pact_v4.runtime.model_lifecycle_adapters",
        "pact_v4.runtime.api_client",
        "pact_v4.runtime.local_openai_backend",
    ):
        assert not any(mod == forbidden or mod.startswith(forbidden + ".")
                       for mod in imports), (
            f"repair module must not reference local lifecycle/transport: {forbidden}"
        )
    for forbidden in ("LifecycleModelCaller", "LifecycleQwenEvaluator", "ModelRouter"):
        assert forbidden not in " ".join(imports)


def test_build_repair_adapters_returns_backend_adapters():
    # The Phase 4 callables come from the backend boundary, never local
    # lifecycle adapters. This is exercised structurally by the runtime
    # config test suite; here we only pin the public contract exists.
    from pact_v4.runtime.runtime_config import build_repair_adapters
    assert callable(build_repair_adapters)


def test_repair_callable_is_repair_caller_protocol():
    # RepairCaller is a Protocol; BackendRepairCaller structurally satisfies it.
    from pact_v4.runtime.backend_role_adapters import BackendRepairCaller
    from pact_v4.runtime.backend_protocol import CompletionBackend

    class _FakeBackend:
        def __init__(self):
            self.calls = []
        @property
        def descriptor(self):
            from pact_v4.runtime.backend_protocol import BackendDescriptor
            return BackendDescriptor(
                kind="local_llama", transport_version="t", endpoint_family="e",
                public_endpoint="http://127.0.0.1:1", model_bindings={"generator": "g"},
                effective_options={},
            )
        def complete(self, request):
            self.calls.append(request)
            from pact_v4.runtime.backend_protocol import CompletionResponse
            return CompletionResponse(text=json.dumps({"repaired": {}, "reason": "ok"}))
        def close(self):
            pass
        def call_records(self):
            return []

    fake = _FakeBackend()
    caller = BackendRepairCaller(fake)  # type: ignore[arg-type]
    raw = caller(
        chunk_id="chunk0001",
        source={"p1": "Hello."},
        translation={"p1": "Привет."},
        region=Region(pid="p1", start=0, end=6),
        findings=[{"category": "omission", "note": "x"}],
    )
    assert "repaired" in raw
    assert fake.calls[0].label.startswith("phase4/")
    assert fake.calls[0].model_ref == "g"


# ---------------------------------------------------------------------------
# L1: re-audit batching by detector (order-independent finding set)
# ---------------------------------------------------------------------------


def _two_chunk_env():
    source = _source()
    snapshot = _snapshot(source)
    chunk1 = ChunkPlan(
        chunk_id="chunk0001", snapshot_hash=snapshot.snapshot_hash,
        pids=snapshot.pids[:5], word_counts=tuple(60 for _ in range(5)),
    )
    chunk2 = ChunkPlan(
        chunk_id="chunk0002", snapshot_hash=snapshot.snapshot_hash,
        pids=snapshot.pids[5:], word_counts=tuple(60 for _ in range(5)),
    )
    chunk_plan = ChunkPlanArtifact.create(snapshot, (chunk1, chunk2))
    config = _config()
    cand1 = _candidate(chunk=chunk1, suffix="A", source=source, snapshot=snapshot,
                       chunk_plan=chunk_plan, config=config)
    cand2 = _candidate(chunk=chunk2, suffix="A", source=source, snapshot=snapshot,
                       chunk_plan=chunk_plan, config=config)
    candidates = {chunk1.chunk_id: cand1, chunk2.chunk_id: cand2}
    return source, snapshot, chunk_plan, config, candidates


def test_reaudit_chunks_findings_set_identical_across_chunk_order():
    # L1: the re-audit finding set must be identical regardless of iteration
    # order (canonicalised by content_hash) and the model tracks must be
    # batched detector-outer.
    source, snapshot, chunk_plan, config, candidates = _two_chunk_env()
    per_chunk_qwen = {
        "chunk0001": [{"pid": "p00000", "category": "omission", "note": "q1"}],
        "chunk0002": [{"pid": "p00005", "category": "addition", "note": "q2"}],
    }
    per_chunk_gemma = {
        "chunk0001": [{"pid": "p00001", "category": "calque", "note": "g1"}],
        "chunk0002": [{"pid": "p00006", "category": "dialogue", "note": "g2"}],
    }

    class _ChunkQwen(ScriptedQwenAudit):
        def __call__(self, *, chunk_id, source, translation):
            self.calls.append((chunk_id, dict(source), dict(translation)))
            return json.dumps({"issues": per_chunk_qwen.get(chunk_id, [])},
                              ensure_ascii=False)

    class _ChunkGemma(ScriptedGemmaAudit):
        def __call__(self, *, chunk_id, translation):
            self.calls.append((chunk_id, dict(translation)))
            return json.dumps({"issues": per_chunk_gemma.get(chunk_id, [])},
                              ensure_ascii=False)

    qwen_audit = _ChunkQwen()
    gemma_audit = _ChunkGemma()
    translation_by_chunk = {
        chunk.chunk_id: candidates[chunk.chunk_id].as_pid_map()
        for chunk in chunk_plan.chunks
    }

    findings_ab = _reaudit_chunks(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config,
        det_data=DeterministicGateData(), translation_by_chunk=translation_by_chunk,
        chunk_ids=["chunk0001", "chunk0002"],
        qwen_audit_evaluator=qwen_audit, gemma_audit_evaluator=gemma_audit,
    )
    # The model tracks are detector-batched: all Qwen units then all Gemma
    # units (2 + 2 calls), one per chunk.
    assert [call[0] for call in qwen_audit.calls] == ["chunk0001", "chunk0002"]
    assert [call[0] for call in gemma_audit.calls] == ["chunk0001", "chunk0002"]

    qwen_audit_ba = _ChunkQwen()
    gemma_audit_ba = _ChunkGemma()
    findings_ba = _reaudit_chunks(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config,
        det_data=DeterministicGateData(), translation_by_chunk=translation_by_chunk,
        chunk_ids=["chunk0002", "chunk0001"],
        qwen_audit_evaluator=qwen_audit_ba, gemma_audit_evaluator=gemma_audit_ba,
    )
    assert [f.content_hash for f in findings_ab] == [f.content_hash for f in findings_ba]
    assert len(findings_ab) == 4


# ---------------------------------------------------------------------------
# L2b: narrow re-gate, role passes, deferred commit
# ---------------------------------------------------------------------------


class DerivedFullGate:
    """Full-chunk gate whose verdict derives from the repaired PID text."""

    def __call__(self, source, translation) -> GateResult:
        passed = any("исправ" in text.casefold() for text in translation.values())
        return GateResult(gate="qwen_fidelity", passed=passed, detail="derived")


class DerivedRegionGate:
    """Narrow gate whose verdict derives from the repaired PID text."""

    def __call__(self, *, source_text, repaired_text, region) -> GateResult:
        passed = "исправ" in repaired_text.casefold()
        return GateResult(gate="qwen_fidelity", passed=passed, detail="derived")


class FailingRegionGate:
    def __init__(self, fail_pids=()) -> None:
        self.fail_pids = set(fail_pids)

    def __call__(self, *, source_text, repaired_text, region) -> GateResult:
        passed = region.pid not in self.fail_pids
        return GateResult(gate="qwen_fidelity", passed=passed, detail="scripted")


def test_l2b_narrow_and_full_regate_agree_on_fixture():
    # The narrow per-region re-gate and the full-chunk re-gate produce the
    # same verdict when the repaired region content determines it.
    source, snapshot, chunk_plan, chunk, config, candidate, candidates, chapter, handoff = _env()
    pid = chunk.pids[0]
    finding = _finding(
        chunk_id=chunk.chunk_id, candidate_id=candidate.candidate_id,
        pid=pid, snapshot_id=snapshot.snapshot_hash,
    )
    plan = plan_repairs_for_chunk(
        chunk=chunk, findings=[finding], current_text=candidate.as_pid_map(),
        backend_identity_hash=_hash("backend"),
    )[0]
    source_map = {pid: dict(source.source).get(pid, "") for pid in chunk.pids}

    for repaired, expected in [
        ("Исправленный перевод.", True),
        ("другой вариант перевода", False),
    ]:
        tentative = dict(candidate.as_pid_map())
        tentative[pid] = repaired

        trace, passed_narrow, candidate_obj, reason = _re_gate_region(
            plan=plan, chunk=chunk, audited_role="fidelity_first",
            source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config,
            det_data=DeterministicGateData(), tentative_translation=tentative,
            region_fidelity_gate=DerivedRegionGate(),
        )
        assert candidate_obj is not None
        det_result = deterministic_consistency_gate(
            candidate=candidate_obj, source=source_map,
            data=DeterministicGateData(),
        )
        full_passed = det_result.passed and DerivedFullGate()(source_map, tentative).passed
        assert passed_narrow == full_passed == expected
        assert len(trace) == 2


def test_l2b_repair_round_batches_model_calls_by_role():
    # L2b: within a round the model calls happen as role passes — all Gemma
    # edits, then all Qwen re-gates, then all Gemma re-checks — never
    # interleaved per region.
    source, snapshot, chunk_plan, chunk, config, candidate, candidates, chapter, handoff = _env()
    pid_a, pid_b = chunk.pids[0], chunk.pids[1]
    findings = [
        _finding(chunk_id=chunk.chunk_id, candidate_id=candidate.candidate_id,
                 pid=pid_a, detector="gemma_russian_review", category="register",
                 note="inconsistent register", snapshot_id=snapshot.snapshot_hash),
        _finding(chunk_id=chunk.chunk_id, candidate_id=candidate.candidate_id,
                 pid=pid_b, detector="gemma_russian_review", category="calque",
                 note="word-for-word calque", snapshot_id=snapshot.snapshot_hash),
    ]
    plans = plan_repairs_for_chunk(
        chunk=chunk, findings=findings, current_text=candidate.as_pid_map(),
        backend_identity_hash=_hash("backend"),
    )
    assert len(plans) == 2

    events: list = []

    class LoggingRepairCaller(TargetedRepairCaller):
        def __call__(self, **kwargs):
            events.append(("repair", kwargs["region"].pid))
            return super().__call__(**kwargs)

    class LoggingRegionGate(ScriptedRegionGate):
        def __call__(self, **kwargs):
            events.append(("gate", kwargs["region"].pid))
            return super().__call__(**kwargs)

    class LoggingGemmaAudit(ScriptedGemmaAudit):
        def __call__(self, **kwargs):
            events.append(("recheck", kwargs["chunk_id"]))
            return super().__call__(**kwargs)

    repair_caller = LoggingRepairCaller({pid_a: "Исправленный вариант один.", pid_b: "Исправленный вариант два."})
    region_gate = LoggingRegionGate(passed=True)
    gemma_audit = LoggingGemmaAudit(issues=[])
    translation_by_chunk = {chunk.chunk_id: dict(candidate.as_pid_map())}

    records, changed = _run_repair_round(
        chapter_hash=chapter.chapter_hash, chunk_plan=chunk_plan,
        plans_by_chunk={chunk.chunk_id: plans}, candidates=candidates,
        source=source, snapshot=snapshot, config=config,
        det_data=DeterministicGateData(), translation_by_chunk=translation_by_chunk,
        repair_caller=repair_caller, region_fidelity_gate=region_gate,
        gemma_audit_evaluator=gemma_audit,
        backend_identity_hash=_hash("backend"), cache=RepairCache(),
    )

    # Both edits committed (re-gate passed, re-checks clean) and both are
    # reflected in the chunk translation.
    assert [r.committed for r in records] == [True, True]
    assert translation_by_chunk[chunk.chunk_id][pid_a] == "Исправленный вариант один."
    assert translation_by_chunk[chunk.chunk_id][pid_b] == "Исправленный вариант два."

    # Role pass order: repair, repair, gate, gate, recheck, recheck.
    kinds = [kind for kind, _ in events]
    assert kinds == ["repair", "repair", "gate", "gate", "recheck", "recheck"], kinds


def test_l2b_commits_identical_legacy_vs_passes_on_frozen_verdicts():
    # Under identical (frozen) model verdicts the pass-based flow commits the
    # same regions as the legacy interleaved single-region flow.
    source, snapshot, chunk_plan, chunk, config, candidate, candidates, chapter, handoff = _env()
    pid_a, pid_b = chunk.pids[0], chunk.pids[1]
    findings = [
        _finding(chunk_id=chunk.chunk_id, candidate_id=candidate.candidate_id,
                 pid=pid_a, snapshot_id=snapshot.snapshot_hash),
        _finding(chunk_id=chunk.chunk_id, candidate_id=candidate.candidate_id,
                 pid=pid_b, snapshot_id=snapshot.snapshot_hash),
    ]
    plans = plan_repairs_for_chunk(
        chunk=chunk, findings=findings, current_text=candidate.as_pid_map(),
        backend_identity_hash=_hash("backend"),
    )
    base = candidate.as_pid_map()

    # Legacy interleaved flow.
    legacy_cache = RepairCache()
    legacy_ids = set()
    progressive = dict(base)
    for plan in plans:
        record = repair_region(
            plan=plan, chapter_hash=chapter.chapter_hash, chunk=chunk,
            audited_role="fidelity_first",
            source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config,
            det_data=DeterministicGateData(), current_translation=progressive,
            repair_caller=TargetedRepairCaller(
                {pid_a: "Исправленный вариант один.", pid_b: "Исправленный вариант два."}),
            qwen_evaluator=ScriptedQwenGate(passed=True),
            gemma_audit_evaluator=ScriptedGemmaAudit(),
            backend_identity_hash=_hash("backend"), cache=legacy_cache,
        )
        if record.committed:
            legacy_ids.add(record.repair_id)
            for pid, text in record.new_translation:
                progressive[pid] = text

    # New pass-based flow with the narrow re-gate (frozen passed verdict).
    pass_cache = RepairCache()
    translation_by_chunk = {chunk.chunk_id: dict(base)}
    records, _changed = _run_repair_round(
        chapter_hash=chapter.chapter_hash, chunk_plan=chunk_plan,
        plans_by_chunk={chunk.chunk_id: plans}, candidates=candidates,
        source=source, snapshot=snapshot, config=config,
        det_data=DeterministicGateData(), translation_by_chunk=translation_by_chunk,
        repair_caller=TargetedRepairCaller(
            {pid_a: "Исправленный вариант один.", pid_b: "Исправленный вариант два."}),
        region_fidelity_gate=ScriptedRegionGate(passed=True),
        gemma_audit_evaluator=ScriptedGemmaAudit(),
        backend_identity_hash=_hash("backend"), cache=pass_cache,
    )
    pass_ids = {r.repair_id for r in records if r.committed}

    assert pass_ids == legacy_ids == {p.repair.repair_id for p in plans}
    assert set(translation_by_chunk[chunk.chunk_id].items()) == set(progressive.items())
    # repair_id / unit-hash identity is stable across the two flows.
    assert {r.repair_id for r in records} == {p.repair.repair_id for p in plans}


def test_l2b_deferred_commit_keeps_failed_region_base_text():
    # A region whose re-gate fails is never applied (its tentative edit stays
    # on the base text) while the sibling committed region is.
    source, snapshot, chunk_plan, chunk, config, candidate, candidates, chapter, handoff = _env()
    pid_a, pid_b = chunk.pids[0], chunk.pids[1]
    findings = [
        _finding(chunk_id=chunk.chunk_id, candidate_id=candidate.candidate_id,
                 pid=pid_a, snapshot_id=snapshot.snapshot_hash),
        _finding(chunk_id=chunk.chunk_id, candidate_id=candidate.candidate_id,
                 pid=pid_b, snapshot_id=snapshot.snapshot_hash),
    ]
    plans = plan_repairs_for_chunk(
        chunk=chunk, findings=findings, current_text=candidate.as_pid_map(),
        backend_identity_hash=_hash("backend"),
    )
    base = candidate.as_pid_map()
    translation_by_chunk = {chunk.chunk_id: dict(base)}
    records, changed = _run_repair_round(
        chapter_hash=chapter.chapter_hash, chunk_plan=chunk_plan,
        plans_by_chunk={chunk.chunk_id: plans}, candidates=candidates,
        source=source, snapshot=snapshot, config=config,
        det_data=DeterministicGateData(), translation_by_chunk=translation_by_chunk,
        repair_caller=TargetedRepairCaller(
            {pid_a: "Исправленный вариант один.", pid_b: "Исправленный вариант два."}),
        region_fidelity_gate=FailingRegionGate(fail_pids={pid_b}),
        gemma_audit_evaluator=ScriptedGemmaAudit(),
        backend_identity_hash=_hash("backend"), cache=RepairCache(),
    )
    by_pid = {r.target_pids[0]: r for r in records}
    assert by_pid[pid_a].committed is True
    assert by_pid[pid_b].committed is False
    assert translation_by_chunk[chunk.chunk_id][pid_a] == "Исправленный вариант один."
    # The failed region's tentative edit was never applied.
    assert translation_by_chunk[chunk.chunk_id][pid_b] == base[pid_b]


# ---------------------------------------------------------------------------
# L3: severity filter of soft Gemma findings
# ---------------------------------------------------------------------------


def _gemma_soft_finding(*, pid: str, snapshot_id: str, chunk_id: str,
                        candidate_id: str, note: str, excerpt: str,
                        category: str = "calque") -> Finding:
    return Finding(
        detector="gemma_russian_review",
        category=category,
        evidence={"note": note, "excerpt": excerpt},
        region=Region(pid=pid, start=0, end=0),
        source_id=snapshot_id,
        snapshot_id=snapshot_id,
        chunk_id=chunk_id,
        candidate_id=candidate_id,
        policy_version="gemma_russian_review/v1",
    )


def test_l3_filter_weak_excerpt_skipped():
    policy = SoftFindingsPolicy()
    finding = _gemma_soft_finding(
        pid="p1", snapshot_id="snap", chunk_id="chunk0001", candidate_id="c",
        note="literal translation", excerpt="короткий фрагмент",
    )
    repairable, skipped = filter_soft_findings([finding], policy)
    assert skipped == (finding,)
    assert repairable == ()


def test_l3_filter_uncertain_note_skipped():
    policy = SoftFindingsPolicy()
    finding = _gemma_soft_finding(
        pid="p1", snapshot_id="snap", chunk_id="chunk0001", candidate_id="c",
        note="this might be a calque", excerpt="",
    )
    repairable, skipped = filter_soft_findings([finding], policy)
    assert skipped == (finding,)


def test_l3_filter_confident_finding_kept():
    # A finding whose evidence is strong (long excerpt and a confident,
    # detailed note) stays in repair — "уверенные находки остаются в ремонте".
    policy = SoftFindingsPolicy()
    finding = _gemma_soft_finding(
        pid="p1", snapshot_id="snap", chunk_id="chunk0001", candidate_id="c",
        note="The whole phrase is a literal word-for-word calque of the "
             "English idiom and reads unnaturally in Russian",
        excerpt="Длинный цитируемый фрагмент из русского текста, который "
                "подтверждает конкретную проблему в переводе на странице.",
    )
    assert len(finding.evidence["excerpt"]) >= SoftFindingsPolicy().weak_excerpt_max_len
    repairable, skipped = filter_soft_findings([finding], policy)
    assert repairable == (finding,)
    assert skipped == ()


def test_l3_filter_empty_excerpt_confident_kept():
    # An absent excerpt is not itself weak evidence: a confident note without
    # an excerpt is still a strong signal.
    policy = SoftFindingsPolicy()
    finding = _gemma_soft_finding(
        pid="p1", snapshot_id="snap", chunk_id="chunk0001", candidate_id="c",
        note="literal translation", excerpt="",
    )
    repairable, skipped = filter_soft_findings([finding], policy)
    assert repairable == (finding,)


def test_l3_filter_ignores_non_soft_and_other_detectors():
    policy = SoftFindingsPolicy()
    qwen = _finding(
        chunk_id="chunk0001", candidate_id="c", pid="p1",
        snapshot_id="snap", category="omission",
    )
    repairable, skipped = filter_soft_findings([qwen], policy)
    assert repairable == (qwen,)
    assert skipped == ()
    dialogue = _gemma_soft_finding(
        pid="p1", snapshot_id="snap", chunk_id="chunk0001", candidate_id="c",
        note="stilted dialogue", excerpt="x", category="dialogue",
    )
    repairable, skipped = filter_soft_findings([dialogue], policy)
    assert repairable == (dialogue,)


def test_l3_disabled_keeps_everything():
    finding = _gemma_soft_finding(
        pid="p1", snapshot_id="snap", chunk_id="chunk0001", candidate_id="c",
        note="sounds like a calque", excerpt="кр",
    )
    repairable, skipped = filter_soft_findings(
        [finding], SoftFindingsPolicy(enabled=False)
    )
    assert repairable == (finding,)
    assert skipped == ()


def test_l3_soft_findings_do_not_block_round2():
    # A weak-soft Gemma finding skipped in round 1 and re-raised by the
    # convergence re-audit must NOT trigger round 2 (L3 blocking exclusion).
    source, snapshot, chunk_plan, chunk, config, candidate, candidates, chapter, handoff = _env()
    pid_a, pid_b = chunk.pids[0], chunk.pids[1]
    strong = _finding(
        chunk_id=chunk.chunk_id, candidate_id=candidate.candidate_id,
        pid=pid_a, snapshot_id=snapshot.snapshot_hash,
    )
    weak_soft = _gemma_soft_finding(
        pid=pid_b, snapshot_id=snapshot.snapshot_hash,
        chunk_id=chunk.chunk_id, candidate_id=candidate.candidate_id,
        note="literal translation", excerpt="короткий",
    )
    store = FindingStore.create(snapshot.snapshot_hash, [strong, weak_soft])
    qwen_audit = ScriptedQwenAudit(issues=[])  # clean re-audit (Qwen)
    gemma_audit = ScriptedGemmaAudit(issues=[
        {"pid": pid_b, "category": "calque", "note": "literal translation",
         "excerpt": "короткий"},
    ])  # re-audit re-raises the weak-soft finding
    result, *_ = _run_phase(
        repair_caller=ScriptedRepairCaller({pid_a: "Исправленный вариант один."}),
        region_gate=ScriptedRegionGate(passed=True),
        qwen_audit=qwen_audit,
        gemma_audit=gemma_audit,
        findings_override=[strong, weak_soft],
        soft_findings_policy=SoftFindingsPolicy(enabled=True),
    )
    # Round 1 committed the strong finding; round 2 must NOT run for the
    # weak-soft re-audit finding (it is excluded from the blocking set).
    assert any(r.committed for r in result.rounds[0].records)
    assert result.rounds[1].records == ()
    assert any("L3 policy" in reason for reason in result.debt_trace)


def test_l3_weak_soft_blocks_round2_when_disabled():
    # Without the L3 policy the same weak-soft re-audit finding IS blocking,
    # so round 2 runs (demonstrates the exclusion is what changes behaviour).
    source, snapshot, chunk_plan, chunk, config, candidate, candidates, chapter, handoff = _env()
    pid_a, pid_b = chunk.pids[0], chunk.pids[1]
    strong = _finding(
        chunk_id=chunk.chunk_id, candidate_id=candidate.candidate_id,
        pid=pid_a, snapshot_id=snapshot.snapshot_hash,
    )
    weak_soft = _gemma_soft_finding(
        pid=pid_b, snapshot_id=snapshot.snapshot_hash,
        chunk_id=chunk.chunk_id, candidate_id=candidate.candidate_id,
        note="literal translation", excerpt="короткий",
    )
    result, *_ = _run_phase(
        repair_caller=TargetedRepairCaller(
            {pid_a: "Исправленный вариант один.", pid_b: "Исправленный вариант два."}),
        region_gate=ScriptedRegionGate(passed=True),
        qwen_audit=ScriptedQwenAudit(issues=[]),
        gemma_audit=ScriptedGemmaAudit(issues=[
            {"pid": pid_b, "category": "calque", "note": "literal translation",
             "excerpt": "короткий"},
        ]),
        findings_override=[strong, weak_soft],
        soft_findings_policy=SoftFindingsPolicy(enabled=False),
    )
    assert result.rounds[1].records, "round 2 must run when L3 is disabled"


def test_l3_skipped_soft_findings_recorded_in_debt_trace():
    source, snapshot, chunk_plan, chunk, config, candidate, candidates, chapter, handoff = _env()
    pid = chunk.pids[0]
    weak_soft = _gemma_soft_finding(
        pid=pid, snapshot_id=snapshot.snapshot_hash,
        chunk_id=chunk.chunk_id, candidate_id=candidate.candidate_id,
        note="literal translation", excerpt="короткий",
    )
    result, *_ = _run_phase(
        repair_caller=ScriptedRepairCaller(),
        region_gate=ScriptedRegionGate(passed=True),
        findings_override=[weak_soft],
        soft_findings_policy=SoftFindingsPolicy(enabled=True),
    )
    # The weak-soft finding stays in the store; only planning was filtered.
    assert len(result.rounds[0].records) == 0
    assert any("skipped by L3 policy" in reason for reason in result.debt_trace)


# ---------------------------------------------------------------------------
# B12: batched narrow Qwen re-gate (one call per chunk)
# ---------------------------------------------------------------------------


class BatchRegionGate(ScriptedRegionGate):
    """``ScriptedRegionGate`` with a ``batch`` method (B12).

    The batched call returns one verdict per item, in order — identical to
    what per-region calls would return — so the batched re-gate path must
    produce the same commit decisions as the per-region path; only the
    number of model calls differs.
    """

    def __init__(self, passed: bool = True, detail: str = "OK"):
        super().__init__(passed=passed, detail=detail)
        self.batch_calls: list = []

    def batch(self, items):
        self.batch_calls.append([dict(item) for item in items])
        return [
            GateResult(gate="qwen_fidelity", passed=self.passed, detail=self.detail)
            for _ in items
        ]


def _two_repair_env():
    source, snapshot, chunk_plan, chunk, config, candidate, candidates, chapter, handoff = _env()
    pid_a, pid_b = chunk.pids[0], chunk.pids[1]
    findings = [
        _finding(chunk_id=chunk.chunk_id, candidate_id=candidate.candidate_id,
                 pid=pid_a, snapshot_id=snapshot.snapshot_hash),
        _finding(chunk_id=chunk.chunk_id, candidate_id=candidate.candidate_id,
                 pid=pid_b, snapshot_id=snapshot.snapshot_hash),
    ]
    plans = plan_repairs_for_chunk(
        chunk=chunk, findings=findings, current_text=candidate.as_pid_map(),
        backend_identity_hash=_hash("backend"),
    )
    return (source, snapshot, chunk_plan, chunk, config, candidate, candidates,
            chapter, handoff, pid_a, pid_b, plans)


def test_b12_re_gate_batches_regions_of_one_chunk_into_one_call():
    # Two repairs of the SAME chunk -> ONE batched gate call for both
    # regions; both commit (verdict passed), the translation reflects both.
    (source, snapshot, chunk_plan, chunk, config, candidate, candidates,
     chapter, handoff, pid_a, pid_b, plans) = _two_repair_env()
    repair_caller = TargetedRepairCaller(
        {pid_a: "Исправленный вариант один.", pid_b: "Исправленный вариант два."})
    region_gate = BatchRegionGate(passed=True)
    gemma_audit = ScriptedGemmaAudit(issues=[])
    translation_by_chunk = {chunk.chunk_id: dict(candidate.as_pid_map())}

    records, changed = _run_repair_round(
        chapter_hash=chapter.chapter_hash, chunk_plan=chunk_plan,
        plans_by_chunk={chunk.chunk_id: plans}, candidates=candidates,
        source=source, snapshot=snapshot, config=config,
        det_data=DeterministicGateData(), translation_by_chunk=translation_by_chunk,
        repair_caller=repair_caller, region_fidelity_gate=region_gate,
        gemma_audit_evaluator=gemma_audit,
        backend_identity_hash=_hash("backend"), cache=RepairCache(),
    )
    assert [r.committed for r in records] == [True, True]
    assert len(region_gate.batch_calls) == 1  # one batched call for the chunk
    assert len(region_gate.batch_calls[0]) == 2
    assert len(region_gate.calls) == 0  # no per-region calls when batching
    assert translation_by_chunk[chunk.chunk_id][pid_a] == "Исправленный вариант один."
    assert translation_by_chunk[chunk.chunk_id][pid_b] == "Исправленный вариант два."


def test_b12_re_gate_batch_transport_failure_is_debt_for_all_regions():
    # A batched transport failure marks every region of the chunk as a
    # failed re-gate -> no repair commits (debt), base text is kept.
    (source, snapshot, chunk_plan, chunk, config, candidate, candidates,
     chapter, handoff, pid_a, pid_b, plans) = _two_repair_env()
    base = candidate.as_pid_map()

    class ExplodingBatchGate(ScriptedRegionGate):
        def batch(self, items):
            raise RuntimeError("network down")

    translation_by_chunk = {chunk.chunk_id: dict(base)}
    records, _changed = _run_repair_round(
        chapter_hash=chapter.chapter_hash, chunk_plan=chunk_plan,
        plans_by_chunk={chunk.chunk_id: plans}, candidates=candidates,
        source=source, snapshot=snapshot, config=config,
        det_data=DeterministicGateData(), translation_by_chunk=translation_by_chunk,
        repair_caller=TargetedRepairCaller(
            {pid_a: "Исправленный вариант один.", pid_b: "Исправленный вариант два."}),
        region_fidelity_gate=ExplodingBatchGate(passed=True),
        gemma_audit_evaluator=ScriptedGemmaAudit(issues=[]),
        backend_identity_hash=_hash("backend"), cache=RepairCache(),
    )
    assert [r.committed for r in records] == [False, False]
    assert all("API failure" in r.reason or "not committed" in r.reason
               for r in records)
    assert translation_by_chunk[chunk.chunk_id][pid_a] == base[pid_a]
    assert translation_by_chunk[chunk.chunk_id][pid_b] == base[pid_b]


def test_b12_re_gate_batch_matches_per_region_path_on_identical_verdicts():
    # Batched and per-region gates returning identical verdicts must yield
    # identical commit decisions and gate traces (B12 parity contract).
    (source, snapshot, chunk_plan, chunk, config, candidate, candidates,
     chapter, handoff, pid_a, pid_b, plans) = _two_repair_env()
    base = candidate.as_pid_map()
    repair_texts = {pid_a: "Исправленный вариант один.", pid_b: "Исправленный вариант два."}

    def run(gate, *, use_batch: bool):
        translation_by_chunk = {chunk.chunk_id: dict(base)}
        records, _changed = _run_repair_round(
            chapter_hash=chapter.chapter_hash, chunk_plan=chunk_plan,
            plans_by_chunk={chunk.chunk_id: plans}, candidates=candidates,
            source=source, snapshot=snapshot, config=config,
            det_data=DeterministicGateData(),
            translation_by_chunk=translation_by_chunk,
            repair_caller=TargetedRepairCaller(repair_texts),
            region_fidelity_gate=gate,
            gemma_audit_evaluator=ScriptedGemmaAudit(issues=[]),
            backend_identity_hash=_hash("backend"), cache=RepairCache(),
        )
        return records, translation_by_chunk

    per_region, per_region_map = run(ScriptedRegionGate(passed=True), use_batch=False)
    batched, batched_map = run(BatchRegionGate(passed=True), use_batch=True)
    assert [r.committed for r in per_region] == [r.committed for r in batched]
    assert [r.gate_trace for r in per_region] == [r.gate_trace for r in batched]
    assert batched_map == per_region_map
    assert batched_map[chunk.chunk_id][pid_a] == repair_texts[pid_a]
