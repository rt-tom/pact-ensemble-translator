"""Offline replay of the Phase 4 repair pipeline over the chapter 0001
run artifacts (``D:\\pact\\gate_bench_runs\\v4_phase12_strict_0001\\run_001``).

Acceptance criterion 4 of the Phase 4 call-optimization card (L1/L2b/L3,
DECISIONS 2026-08-03): run the **new** pass-based repair pipeline on the
*frozen* model verdicts reconstructed from the run artifacts
(``audit_findings.json``, ``b2_handoff.json``, ``audit_cache.json``) and
compare decisions/commits with the **current** (interleaved) implementation.

The source English text is not present in the run artifacts, so the source
is reconstructed as per-PID placeholders — identical for both flows. Model
verdicts are frozen:

  * the repair caller returns a fixed repaired text per region;
  * the Qwen re-gate (narrow in the new flow, full-chunk in the legacy
    reference) returns a fixed ``passed`` verdict;
  * the convergence re-audit returns the Step 6 cached issues per
    (chunk, detector) from ``audit_cache.json``.

Because both flows consume byte-identical frozen verdicts and the same
reconstructed environment, the L2b restructure (passes + narrow re-gate +
deferred commit) must produce the same set of committed repairs, the same
debt trace (with L3 disabled) and the same terminal status.

The test is skipped when the run artifacts are not present (they live on a
development machine, not in the repository).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

import pytest

from pact_v4.phase1.models import (
    Candidate,
    ChunkContext,
    ChunkPlan,
    ChunkPlanArtifact,
    ConfigArtifact,
    GateResult,
    Provenance,
    Snapshot,
    SourceArtifact,
    canonical_json_hash,
)
from pact_v4.phase2.cascade import DeterministicGateData
from pact_v4.phase3.findings import Finding, FindingStore
from pact_v4.phase4.repair import (
    RepairCache,
    SoftFindingsPolicy,
    _neighbour_chunk_ids,
    _reaudit_chunks,
    decide_terminal_state,
    plan_repairs_for_chunk,
    repair_region,
    run_repair_phase,
)

REPLAY_RUN_DIR = Path(r"D:\pact\gate_bench_runs\v4_phase12_strict_0001\run_001")
FIXED_REPAIRED_TEXT = "Исправленный перевод по замороженному вердикту."

pytestmark = pytest.mark.skipif(
    not REPLAY_RUN_DIR.exists(),
    reason="chapter 0001 run artifacts are not present on this machine",
)


# ---------------------------------------------------------------------------
# Reconstruction helpers (self-consistent placeholder environment)
# ---------------------------------------------------------------------------


def _load(name: str) -> Dict[str, Any]:
    return json.loads((REPLAY_RUN_DIR / name).read_text(encoding="utf-8"))


def _reconstruct_env() -> Tuple[Dict[str, Any], ...]:
    handoff = _load("b2_handoff.json")
    audit = _load("audit_findings.json")
    chunk_plan_payload = _load("chunk_plan.json")
    translations = _load("translations.json")

    pids = [pid for chunk in chunk_plan_payload["chunks"] for pid in chunk["pids"]]
    source = SourceArtifact(
        chapter_id=handoff["chapter_id"],
        source=tuple((pid, f"source text for {pid}") for pid in pids),
    )
    snapshot = Snapshot(
        chapter_id=handoff["chapter_id"],
        pids=tuple(pids),
        context="offline-replay-placeholder",
        glossary_hash=canonical_json_hash({"replay": "glossary"}),
        book_memory_hash=canonical_json_hash({"replay": "book"}),
        chapter_memory_hash=canonical_json_hash({"replay": "chapter"}),
    )
    chunks = []
    for item in chunk_plan_payload["chunks"]:
        context = item.get("context") or {}
        chunks.append(ChunkPlan(
            chunk_id=item["chunk_id"],
            snapshot_hash=snapshot.snapshot_hash,
            pids=tuple(item["pids"]),
            word_counts=tuple(item["word_counts"]),
            context=ChunkContext(
                left_ru=str(context.get("left_ru", "")),
                right_en=tuple(context.get("right_en", [])),
            ),
            undersized_exception=bool(item.get("undersized_exception", False)),
        ))
    chunk_plan = ChunkPlanArtifact.create(snapshot, tuple(chunks))
    config = ConfigArtifact(version="v1", values={"profile": "offline-replay"})

    candidates: Dict[str, Candidate] = {}
    for row in handoff["chunks"]:
        chunk_id = row["chunk_id"]
        chunk = chunk_plan.chunk(chunk_id)
        if not row.get("audited_candidate_id"):
            continue
        candidates[chunk_id] = Candidate.create(
            candidate_id=row["audited_candidate_id"],
            chunk_id=chunk_id,
            role=row.get("audited_role") or "fidelity_first",
            translation=tuple(
                (pid, translations.get(pid, "")) for pid in chunk.pids
            ),
            source=source,
            snapshot=snapshot,
            chunk_plan=chunk_plan,
            config=config,
        )

    findings = [Finding.from_payload(item) for item in audit["store"]["findings"]]
    findings_store = FindingStore.create(
        expected_snapshot_id=audit["store"]["expected_snapshot_id"],
        findings=findings,
    )
    provenance = Provenance(
        source_hash=source.source_hash,
        chapter_snapshot_hash=snapshot.snapshot_hash,
        chunk_plan_hash=chunk_plan.plan_hash,
        prompt_bundle_hash=canonical_json_hash({"artifact": "offline-replay"}),
        config_identity=config.config_identity,
        code_version="offline-replay/1",
        policy_versions={"replay": "offline-replay/v1"},
    )
    current_translation = dict(translations)
    return (source, snapshot, chunk_plan, config, provenance, candidates,
            findings_store, current_translation, handoff)


# ---------------------------------------------------------------------------
# Frozen model verdicts
# ---------------------------------------------------------------------------


def _frozen_reaudit_issues() -> Dict[Tuple[str, str], list[Dict[str, str]]]:
    """Per-(chunk_id, detector) frozen ``{"issues": [...]}`` from audit_cache."""
    cache = _load("audit_cache.json")
    issues_map: Dict[Tuple[str, str], list[Dict[str, str]]] = {}
    for unit in cache["cache"]["units"]:
        if not unit.get("ok"):
            continue
        for finding in unit.get("findings", []):
            if finding["detector"] not in ("qwen_chapter_audit", "gemma_russian_review"):
                continue
            key = (finding["chunk_id"], finding["detector"])
            issues_map.setdefault(key, []).append({
                "pid": finding["region"]["pid"],
                "category": finding["category"],
                "note": str(finding["evidence"].get("note", "")),
                "excerpt": str(finding["evidence"].get("excerpt", "")),
            })
    return issues_map


class FrozenRepairCaller:
    """Returns the fixed repaired text for whatever PID is requested."""

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, *, chunk_id, source, translation, region, findings) -> str:
        self.calls.append(region.pid)
        return json.dumps(
            {"repaired": {region.pid: FIXED_REPAIRED_TEXT}, "reason": "frozen"},
            ensure_ascii=False,
        )


class FrozenFullGate:
    """Full-chunk Qwen re-gate used by the legacy reference flow."""

    def __init__(self, passed: bool = True) -> None:
        self.passed = passed

    def __call__(self, source, translation) -> GateResult:
        return GateResult(gate="qwen_fidelity", passed=self.passed, detail="frozen")


class FrozenRegionGate:
    """Narrow L2b Qwen re-gate used by the new pass flow."""

    def __init__(self, passed: bool = True) -> None:
        self.passed = passed

    def __call__(self, *, source_text, repaired_text, region) -> GateResult:
        return GateResult(gate="qwen_fidelity", passed=self.passed, detail="frozen")


class FrozenReaudit:
    """Re-audit evaluator returning the Step 6 cached issues per chunk."""

    def __init__(self, detector: str, issues_map: Mapping[Tuple[str, str], list]) -> None:
        self.detector = detector
        self._issues_map = issues_map

    def __call__(self, *, chunk_id, **kwargs) -> str:
        issues = self._issues_map.get((chunk_id, self.detector), [])
        return json.dumps({"issues": issues}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Legacy reference flow (faithful port of the pre-L2b run_repair_phase rounds)
# ---------------------------------------------------------------------------


def _legacy_committed(
    *,
    source, snapshot, chunk_plan, config, provenance, det_data,
    findings_store, candidates, current_translation,
    backend_identity_hash, chapter_hash,
) -> Tuple[set, Tuple[str, ...], str]:
    """Run the legacy interleaved rounds (per-region edit -> full re-gate ->
    recheck -> immediate commit) and return ``(committed_repair_ids,
    debt_trace, terminal_status)``."""
    translation_by_chunk = {
        chunk.chunk_id: {pid: current_translation.get(pid, "") for pid in chunk.pids}
        for chunk in chunk_plan.chunks
    }
    repair_caller = FrozenRepairCaller()
    full_gate = FrozenFullGate(passed=True)
    issues_map = _frozen_reaudit_issues()
    qwen_audit = FrozenReaudit("qwen_chapter_audit", issues_map)
    gemma_audit = FrozenReaudit("gemma_russian_review", issues_map)

    cache = RepairCache()
    debt_reasons: list[str] = []
    changed_chunk_ids: list[str] = []
    committed_ids: set = set()

    # ---- round 1 (interleaved) ------------------------------------------
    for chunk in chunk_plan.chunks:
        if chunk.chunk_id not in candidates:
            continue
        chunk_findings = tuple(
            f for f in findings_store if f.chunk_id == chunk.chunk_id
        )
        if not chunk_findings:
            continue
        plans = plan_repairs_for_chunk(
            chunk=chunk, findings=chunk_findings,
            current_text=translation_by_chunk[chunk.chunk_id],
            backend_identity_hash=backend_identity_hash,
        )
        if not plans:
            debt_reasons.append(
                f"{chunk.chunk_id}: findings present but no region repair could be planned"
            )
            continue
        for plan in plans:
            record = repair_region(
                plan=plan, chapter_hash=chapter_hash, chunk=chunk,
                audited_role=candidates[chunk.chunk_id].role,
                source=source, snapshot=snapshot, chunk_plan=chunk_plan,
                config=config, det_data=det_data,
                current_translation=translation_by_chunk[chunk.chunk_id],
                repair_caller=repair_caller, qwen_evaluator=full_gate,
                gemma_audit_evaluator=gemma_audit,
                backend_identity_hash=backend_identity_hash, cache=cache,
            )
            if record.committed:
                for pid, text in record.new_translation:
                    translation_by_chunk[record.chunk_id][pid] = text
                if record.chunk_id not in changed_chunk_ids:
                    changed_chunk_ids.append(record.chunk_id)
                committed_ids.add(record.repair_id)
            else:
                debt_reasons.append(
                    f"{record.chunk_id}: repair {record.repair_id[:12]} not committed "
                    f"({record.reason})"
                )

    # ---- round 1 re-audit ----------------------------------------------
    reaudit_scope = list(dict.fromkeys(
        list(changed_chunk_ids)
        + [n for cid in changed_chunk_ids for n in _neighbour_chunk_ids(chunk_plan, cid)]
    ))
    reaudit_findings: Tuple[Finding, ...] = ()
    if reaudit_scope:
        reaudit_outcome = _reaudit_chunks(
            source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config,
            det_data=det_data, translation_by_chunk=translation_by_chunk,
            chunk_ids=reaudit_scope,
            qwen_audit_evaluator=qwen_audit, gemma_audit_evaluator=gemma_audit,
        )
        reaudit_findings = reaudit_outcome.findings

    # ---- round 2 (interleaved) ------------------------------------------
    blocking_findings = tuple(
        f for f in reaudit_findings if f.chunk_id in reaudit_scope
    )
    boundary_changed = any(
        cid in _neighbour_chunk_ids(chunk_plan, chunk_id)
        for chunk_id in changed_chunk_ids
        for cid in changed_chunk_ids
        if cid != chunk_id
    )
    round_two_changed: list[str] = []
    if blocking_findings or boundary_changed:
        for chunk in chunk_plan.chunks:
            if chunk.chunk_id not in candidates:
                continue
            chunk_blocking = tuple(
                f for f in blocking_findings if f.chunk_id == chunk.chunk_id
            )
            if not chunk_blocking:
                continue
            plans = plan_repairs_for_chunk(
                chunk=chunk, findings=chunk_blocking,
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
                record = repair_region(
                    plan=plan, chapter_hash=chapter_hash, chunk=chunk,
                    audited_role=candidates[chunk.chunk_id].role,
                    source=source, snapshot=snapshot, chunk_plan=chunk_plan,
                    config=config, det_data=det_data,
                    current_translation=translation_by_chunk[chunk.chunk_id],
                    repair_caller=repair_caller, qwen_evaluator=full_gate,
                    gemma_audit_evaluator=gemma_audit,
                    backend_identity_hash=backend_identity_hash, cache=cache,
                )
                if record.committed:
                    for pid, text in record.new_translation:
                        translation_by_chunk[record.chunk_id][pid] = text
                    if record.chunk_id not in round_two_changed:
                        round_two_changed.append(record.chunk_id)
                    committed_ids.add(record.repair_id)
                else:
                    debt_reasons.append(
                        f"{record.chunk_id}: round 2 repair not committed "
                        f"({record.reason})"
                    )

    final_map: Dict[str, str] = {}
    for chunk in chunk_plan.chunks:
        final_map.update(translation_by_chunk[chunk.chunk_id])
    for chunk in chunk_plan.chunks:
        uncovered = [pid for pid in chunk.pids if not final_map.get(pid)]
        if uncovered:
            debt_reasons.append(
                f"{chunk.chunk_id}: no valid PID-map after repair "
                f"(uncovered: {uncovered[:5]}{'...' if len(uncovered) > 5 else ''})"
            )
    debt = tuple(dict.fromkeys(debt_reasons))
    terminal = decide_terminal_state(
        chunk_plan=chunk_plan, final_translation=final_map,
        debt_reasons=debt, provenance=provenance,
    )
    return committed_ids, debt, terminal.status


# ---------------------------------------------------------------------------
# Replay test
# ---------------------------------------------------------------------------


def test_offline_replay_0001_commits_equivalent_to_current_implementation():
    """New pass-based flow (L2b, L3 disabled) commits the same regions as the
    legacy interleaved flow on the frozen chapter 0001 verdicts."""
    (source, snapshot, chunk_plan, config, provenance, candidates,
     findings_store, current_translation, handoff) = _reconstruct_env()

    backend_identity_hash = _load("b2_handoff.json")["backend_identity_hash"]
    chapter_hash = _load("b2_handoff.json")["chapter_hash"]

    # ---- legacy reference ------------------------------------------------
    legacy_ids, legacy_debt, legacy_terminal = _legacy_committed(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config,
        provenance=provenance, det_data=DeterministicGateData(),
        findings_store=findings_store, candidates=candidates,
        current_translation=current_translation,
        backend_identity_hash=backend_identity_hash, chapter_hash=chapter_hash,
    )

    # ---- new pass-based flow (L3 disabled for a like-for-like comparison) -
    issues_map = _frozen_reaudit_issues()
    result = run_repair_phase(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config,
        provenance=provenance, det_data=DeterministicGateData(),
        handoff_chunks=handoff["chunks"], findings_store=findings_store,
        candidates=candidates, current_translation=current_translation,
        repair_caller=FrozenRepairCaller(),
        region_fidelity_gate=FrozenRegionGate(passed=True),
        qwen_audit_evaluator=FrozenReaudit("qwen_chapter_audit", issues_map),
        gemma_audit_evaluator=FrozenReaudit("gemma_russian_review", issues_map),
        backend_identity_hash=backend_identity_hash,
        cache=RepairCache(),
        max_rounds=2,
        chapter_hash=chapter_hash,
        soft_findings_policy=SoftFindingsPolicy(enabled=False),
    )
    new_ids = {
        record.repair_id
        for round_result in result.rounds
        for record in round_result.records
        if record.committed
    }

    # The L2b restructure must not change which regions commit.
    assert new_ids == legacy_ids, (
        f"commit sets differ: only-legacy={sorted(legacy_ids - new_ids)}, "
        f"only-new={sorted(new_ids - legacy_ids)}"
    )
    # Debt trace (L3 disabled): the A1c convergence fail-open fix makes the
    # new flow's debt a strict superset of the legacy flow's — every legacy
    # reason is preserved, and residual blocking findings / failed re-audit
    # units of the last round are ADDITIONALLY recorded (the legacy flow
    # silently dropped them, which is the fail-open being fixed). Terminal
    # status must be unchanged.
    assert set(legacy_debt) <= set(result.debt_trace), (
        "the fail-closed flow must never drop a legacy debt reason"
    )
    # The additions are exactly the fail-closed convergence entries (residual
    # blockers / failed re-audit units of the last round, and Step 6
    # unit_failed chunks).
    new_only = set(result.debt_trace) - set(legacy_debt)
    assert new_only, "the fail-closed flow must add residual-blocker debt"
    for reason in new_only:
        assert (
            "remains after the last convergence re-audit" in reason
            or ("convergence re-audit" in reason and "failed" in reason)
            or "Step 6 audit unit failed" in reason
        ), f"unexpected new debt reason: {reason}"
    assert result.status == legacy_terminal


def test_offline_replay_0001_l3_skips_weak_soft_findings_and_records_debt():
    """With L3 enabled the new flow skips weak-soft Gemma findings from repair
    planning and records them in the debt trace (they stay in the store)."""
    (source, snapshot, chunk_plan, config, provenance, candidates,
     findings_store, current_translation, handoff) = _reconstruct_env()

    soft_before = sum(
        1 for f in findings_store
        if f.detector == "gemma_russian_review" and f.category in ("calque", "register")
    )
    backend_identity_hash = _load("b2_handoff.json")["backend_identity_hash"]
    chapter_hash = _load("b2_handoff.json")["chapter_hash"]
    issues_map = _frozen_reaudit_issues()

    result = run_repair_phase(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config,
        provenance=provenance, det_data=DeterministicGateData(),
        handoff_chunks=handoff["chunks"], findings_store=findings_store,
        candidates=candidates, current_translation=current_translation,
        repair_caller=FrozenRepairCaller(),
        region_fidelity_gate=FrozenRegionGate(passed=True),
        qwen_audit_evaluator=FrozenReaudit("qwen_chapter_audit", issues_map),
        gemma_audit_evaluator=FrozenReaudit("gemma_russian_review", issues_map),
        backend_identity_hash=backend_identity_hash,
        cache=RepairCache(),
        max_rounds=2,
        chapter_hash=chapter_hash,
        soft_findings_policy=SoftFindingsPolicy(enabled=True),
    )

    # The append-only store is untouched by the L3 filter.
    assert len(findings_store) == 114
    assert any("L3 policy" in reason for reason in result.debt_trace)
    assert soft_before > 0
