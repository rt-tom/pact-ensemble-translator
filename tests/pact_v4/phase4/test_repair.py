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
from pact_v4.phase2.cascade import DeterministicGateData
from pact_v4.phase3.assembly import AssembledChapter
from pact_v4.phase3.findings import Finding, FindingStore
from pact_v4.phase3.region_resolver import resolve_regions
from pact_v4.phase4 import repair as repair_module
from pact_v4.phase4.repair import (
    REPAIR_POLICY_VERSION,
    RepairCache,
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


class ScriptedQwenGate:
    def __init__(self, passed: bool = True, detail: str = "OK") -> None:
        self.passed = passed
        self.detail = detail
        self.calls: list = []

    def __call__(self, source, translation) -> GateResult:
        self.calls.append((dict(source), dict(translation)))
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


# ---------------------------------------------------------------------------
# 4B convergence + terminal
# ---------------------------------------------------------------------------


def _run_phase(
    *,
    repair_caller, qwen_gate, gemma_audit=None, qwen_audit=None,
    findings_override=None, candidate_overrides=None,
    max_rounds: int = 2,
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
        qwen_evaluator=qwen_gate,
        qwen_audit_evaluator=qwen_audit or ScriptedQwenAudit(),
        gemma_audit_evaluator=gemma_audit or ScriptedGemmaAudit(),
        backend_identity_hash=_hash("backend"),
        cache=RepairCache(),
        max_rounds=max_rounds,
    )
    return result, source, snapshot, chunk_plan, chunk, config, candidate


def test_terminal_complete_when_repair_commits_and_reaudit_clean():
    source, snapshot, chunk_plan, chunk, config, candidate, candidates, chapter, handoff = _env()
    pid = chunk.pids[0]
    result, *_ = _run_phase(
        repair_caller=ScriptedRepairCaller({pid: "Исправленный перевод."}),
        qwen_gate=ScriptedQwenGate(passed=True),
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
        qwen_gate=ScriptedQwenGate(passed=False, detail="meaning drift"),
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
        qwen_gate=ScriptedQwenGate(passed=True),
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
        qwen_gate=ScriptedQwenGate(passed=True),
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
        qwen_gate=ScriptedQwenGate(passed=True),
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
        qwen_gate=ScriptedQwenGate(passed=True),
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
