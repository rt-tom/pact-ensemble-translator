"""Phase 1A contract tests.

Covers every dataclass in pact_v4.phase1.models against the acceptance
criteria in docs/architecture/V4_FINAL_REVIEW_AND_IMPLEMENTATION_PLAN_RU_v2.md
("Исполнимые контракты до реализации"), not just the literal phrases
("reject partial JSON", "duplicate PID", ...).
"""
import dataclasses
import hashlib
from pathlib import Path

import pytest

from pact_v4.phase1.schema_check import load_schema, validate as schema_validate
from pact_v4.phase1.models import (
    Candidate,
    ChunkContext,
    ChunkPlan,
    Finding,
    GateResult,
    Provenance,
    Region,
    Repair,
    Snapshot,
    TerminalState,
    validate_candidate_ownership,
    validate_full_pid_ownership,
    validate_json_complete,
)

SCHEMA_DIR = Path(__file__).resolve().parents[3] / "docs" / "schemas"


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _provenance(**overrides) -> Provenance:
    kwargs = dict(
        source_hash=_hash("source"),
        chapter_snapshot_hash=_hash("snapshot"),
        chunk_plan_hash=_hash("chunkplan"),
        prompt_bundle_hash=_hash("prompt"),
        code_version="v4.0-phase1a",
        policy_versions={"risk_policy": "v1"},
    )
    kwargs.update(overrides)
    return Provenance(**kwargs)


def _snapshot(pids=("p00001", "p00002", "p00003", "p00004", "p00005", "p00006", "p00007", "p00008")) -> Snapshot:
    return Snapshot(
        chapter_id="ch044",
        pids=pids,
        context="frozen context",
        glossary_hash=_hash("glossary"),
        book_memory_hash=_hash("book_memory"),
        chapter_memory_hash=_hash("chapter_memory"),
    )


# ChunkPlan.MIN_WORDS/MAX_WORDS (280/640) are fixed hard bounds; 35 words/PID
# keeps every existing PID-count-based fixture in this file compatible with
# them (e.g. the default 8-PID snapshot lands exactly on MIN_WORDS=280).
WORDS_PER_PID = 35


def _chunk_plan(snapshot: Snapshot, pids=None, **overrides) -> ChunkPlan:
    resolved_pids = pids if pids is not None else snapshot.pids
    kwargs = dict(
        chunk_id="c0001",
        snapshot_hash=snapshot.snapshot_hash,
        pids=resolved_pids,
        word_counts=tuple(WORDS_PER_PID for _ in resolved_pids),
        context=ChunkContext(left_ru="", right_en=()),
    )
    kwargs.update(overrides)
    return ChunkPlan(**kwargs)


def _candidate(chunk: ChunkPlan, **overrides) -> Candidate:
    kwargs = dict(
        candidate_id="cand-a",
        chunk_id=chunk.chunk_id,
        role="fidelity_first",
        translation=tuple((pid, f"ru-{pid}") for pid in chunk.pids),
        source_hash=_hash("source"),
        snapshot_hash=chunk.snapshot_hash,
        chunk_plan_hash=_hash("chunkplan"),
        config_identity="gemma/temp0/seed1",
    )
    kwargs.update(overrides)
    return Candidate(**kwargs)


# --- Provenance --------------------------------------------------------

def test_provenance_valid_construction():
    p = _provenance()
    assert p.code_version == "v4.0-phase1a"


def test_provenance_rejects_foreign_identity_hash():
    with pytest.raises(ValueError, match="Foreign identity"):
        _provenance(source_hash="not-a-hash")


def test_provenance_rejects_empty_policy_versions():
    with pytest.raises(ValueError, match="policy_versions"):
        _provenance(policy_versions={})


def test_provenance_is_frozen():
    p = _provenance()
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.code_version = "other"


# --- Snapshot ------------------------------------------------------------

def test_snapshot_duplicate_pids_rejected():
    with pytest.raises(ValueError, match="duplicate PIDs"):
        Snapshot(
            chapter_id="ch044", pids=("p1", "p1"), context="ctx",
            glossary_hash=_hash("g"), book_memory_hash=_hash("b"),
            chapter_memory_hash=_hash("c"),
        )


def test_snapshot_hash_is_deterministic_content_identity():
    s1 = _snapshot()
    s2 = _snapshot()
    assert s1.snapshot_hash == s2.snapshot_hash
    s3 = _snapshot(pids=("p00001",) * 1 + ("p00002",))
    assert s3.snapshot_hash != s1.snapshot_hash


def test_snapshot_rejects_bad_hash_reference():
    with pytest.raises(ValueError, match="Foreign identity"):
        Snapshot(
            chapter_id="ch044", pids=("p1",), context="ctx",
            glossary_hash="bad", book_memory_hash=_hash("b"),
            chapter_memory_hash=_hash("c"),
        )


def test_snapshot_is_frozen():
    s = _snapshot()
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.context = "other"


# --- ChunkPlan -------------------------------------------------------------

def test_chunk_plan_below_soft_min_rejected_without_exception():
    snap = _snapshot(pids=tuple(f"p{i:05d}" for i in range(1, 4)))  # only 3 PIDs
    with pytest.raises(ValueError, match="below soft minimum"):
        _chunk_plan(snap, pids=snap.pids)


def test_chunk_plan_within_cap_accepted():
    snap = _snapshot()
    plan = _chunk_plan(snap)
    assert len(plan.pids) == 8


def test_chunk_plan_above_hard_max_always_rejected():
    # undersized_exception only documents missing the *soft* minimum; it
    # must never relax the hard maximum (V4_MVP_SPEC_RU.md §3.2).
    snap = _snapshot(pids=tuple(f"p{i:05d}" for i in range(1, 26)))  # 25 PIDs
    with pytest.raises(ValueError, match="exceeds hard cap"):
        _chunk_plan(snap, pids=snap.pids, undersized_exception=True)


def test_chunk_plan_undersized_exception_allows_below_soft_min():
    # Not just exactly one PID: any documented undersized case (e.g. a
    # short chapter) is allowed as long as the hard max still holds.
    snap = _snapshot(pids=tuple(f"p{i:05d}" for i in range(1, 4)))  # 3 PIDs
    plan = _chunk_plan(snap, pids=snap.pids, undersized_exception=True)
    assert len(plan.pids) == 3


def test_chunk_plan_undersized_exception_single_pid():
    snap = _snapshot(pids=("p00001",))
    plan = _chunk_plan(snap, pids=("p00001",), undersized_exception=True)
    assert plan.pids == ("p00001",)


def test_chunk_plan_duplicate_pids_rejected():
    snap = _snapshot()
    with pytest.raises(ValueError, match="duplicate PIDs"):
        _chunk_plan(snap, pids=("p00001", "p00001", "p00002", "p00003", "p00004", "p00005", "p00006", "p00007"))


def test_chunk_plan_total_words_cannot_be_set_disconnected_from_pids():
    # Regression: total_words used to be a caller-supplied int with no
    # relation to len(pids), so e.g. 100 PIDs with total_words=640 passed
    # validation despite being nowhere near what 100 real PIDs would sum
    # to. total_words is now derived from word_counts (one entry per PID).
    snap = _snapshot(pids=tuple(f"p{i:05d}" for i in range(1, 101)))  # 100 PIDs
    with pytest.raises(ValueError, match="word_counts has"):
        ChunkPlan(
            chunk_id="c0001", snapshot_hash=snap.snapshot_hash,
            pids=snap.pids, word_counts=(640,),  # one entry, not one per PID
        )


def test_chunk_plan_word_counts_derives_total_words():
    snap = _snapshot(pids=("p00001", "p00002"))
    plan = ChunkPlan(
        chunk_id="c0001", snapshot_hash=snap.snapshot_hash,
        pids=snap.pids, word_counts=(150, 150),
    )
    assert plan.total_words == 300


def test_chunk_plan_rejects_foreign_snapshot_hash():
    snap = _snapshot()
    with pytest.raises(ValueError, match="Foreign identity"):
        _chunk_plan(snap, snapshot_hash="not-a-hash")


# --- cross-chunk ownership ---------------------------------------------

def test_full_pid_ownership_happy_path():
    snap = _snapshot(pids=tuple(f"p{i:05d}" for i in range(1, 17)))
    plan_a = _chunk_plan(snap, pids=snap.pids[:8])
    plan_b = _chunk_plan(snap, pids=snap.pids[8:], chunk_id="c0002")
    validate_full_pid_ownership((plan_a, plan_b), snap)  # must not raise


def test_full_pid_ownership_detects_missing_pid():
    snap = _snapshot(pids=tuple(f"p{i:05d}" for i in range(1, 17)))
    plan_a = _chunk_plan(snap, pids=snap.pids[:8])
    with pytest.raises(ValueError, match="missing chunk ownership"):
        validate_full_pid_ownership((plan_a,), snap)


def test_full_pid_ownership_detects_double_ownership():
    snap = _snapshot(pids=tuple(f"p{i:05d}" for i in range(1, 17)))
    plan_a = _chunk_plan(snap, pids=snap.pids[:9])
    plan_b = _chunk_plan(snap, pids=snap.pids[8:], chunk_id="c0002")
    with pytest.raises(ValueError, match="owned by both"):
        validate_full_pid_ownership((plan_a, plan_b), snap)


def test_full_pid_ownership_detects_foreign_plan():
    snap_a = _snapshot(pids=tuple(f"p{i:05d}" for i in range(1, 9)))
    snap_b = _snapshot(pids=tuple(f"q{i:05d}" for i in range(1, 9)))
    plan_from_b = _chunk_plan(snap_b)
    with pytest.raises(ValueError, match="foreign snapshot"):
        validate_full_pid_ownership((plan_from_b,), snap_a)


# --- Candidate ---------------------------------------------------------

def test_candidate_has_no_score_field():
    snap = _snapshot()
    chunk = _chunk_plan(snap)
    cand = _candidate(chunk)
    assert not hasattr(cand, "score")
    assert "score" not in {f.name for f in dataclasses.fields(cand)}


def test_candidate_ordered_pid_map_matches_chunk():
    snap = _snapshot()
    chunk = _chunk_plan(snap)
    cand = _candidate(chunk)
    validate_candidate_ownership(cand, chunk)  # must not raise
    assert cand.pid_order() == chunk.pids
    assert cand.as_pid_map()["p00001"] == "ru-p00001"


def test_candidate_rejects_unknown_role():
    snap = _snapshot()
    chunk = _chunk_plan(snap)
    with pytest.raises(ValueError, match="Unknown candidate role"):
        _candidate(chunk, role="best_guess")


def test_candidate_rejects_duplicate_pid_in_translation():
    snap = _snapshot()
    chunk = _chunk_plan(snap)
    bad_translation = tuple((pid, "x") for pid in chunk.pids) + (("p00001", "dup"),)
    with pytest.raises(ValueError, match="duplicate PIDs"):
        _candidate(chunk, translation=bad_translation)


def test_candidate_ownership_rejects_mismatched_pid_order():
    snap = _snapshot()
    chunk = _chunk_plan(snap)
    reordered = tuple(reversed(chunk.pids))
    cand = _candidate(chunk, translation=tuple((pid, "x") for pid in reordered))
    with pytest.raises(ValueError, match="does not match chunk plan"):
        validate_candidate_ownership(cand, chunk)


def test_candidate_is_frozen():
    snap = _snapshot()
    chunk = _chunk_plan(snap)
    cand = _candidate(chunk)
    with pytest.raises(dataclasses.FrozenInstanceError):
        cand.chunk_id = "other"


# --- Finding -------------------------------------------------------------

def test_finding_requires_detector_category_evidence_region():
    finding = Finding(
        finding_id="f-0001", detector="qwen_semantic_audit", category="omission",
        severity="major", evidence="source mentions three items, translation has two",
        region=Region(pid="p00001", start=0, end=12),
    )
    assert finding.region.pid == "p00001"


def test_finding_is_frozen():
    finding = Finding(
        finding_id="f-0001", detector="d", category="c", severity="s",
        evidence="e", region=Region(pid="p00001", start=0, end=1),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        finding.severity = "minor"


def test_region_rejects_invalid_span():
    with pytest.raises(ValueError, match="invalid span"):
        Region(pid="p00001", start=10, end=2)


# --- Repair ---------------------------------------------------------------

def test_repair_requires_at_least_one_finding():
    with pytest.raises(ValueError, match="at least one finding"):
        Repair(
            repair_id="r-0001", finding_ids=(), chunk_id="c0001",
            action="region_edit", target_pids=("p00001",),
            instructions="fix referent",
        )


def test_repair_full_sentence_rewrite_requires_reason():
    with pytest.raises(ValueError, match="documented reason"):
        Repair(
            repair_id="r-0001", finding_ids=("f-0001",), chunk_id="c0001",
            action="full_sentence_rewrite", target_pids=("p00001",),
            instructions="rewrite",
        )


def test_repair_full_sentence_rewrite_with_reason_accepted():
    repair = Repair(
        repair_id="r-0001", finding_ids=("f-0001",), chunk_id="c0001",
        action="full_sentence_rewrite", target_pids=("p00001",),
        instructions="rewrite", full_sentence_reason="local edit breaks syntax",
    )
    assert repair.full_sentence_reason


def test_repair_never_auto_accepts():
    with pytest.raises(ValueError, match="cannot be auto-accepted"):
        Repair(
            repair_id="r-0001", finding_ids=("f-0001",), chunk_id="c0001",
            action="region_edit", target_pids=("p00001",),
            instructions="fix", auto_accepted=True,
        )


def test_repair_carries_gate_decision_trace():
    repair = Repair(
        repair_id="r-0001", finding_ids=("f-0001",), chunk_id="c0001",
        action="region_edit", target_pids=("p00001",), instructions="fix",
        decision_trace=(GateResult(gate="deterministic_consistency", passed=True),),
    )
    assert repair.decision_trace[0].passed is True


# --- TerminalState -------------------------------------------------------

@pytest.mark.parametrize("target", ["complete", "quarantined", "failed"])
def test_terminal_state_reaches_all_three_terminal_states(target):
    state = TerminalState(state_id="s1", status="pending", provenance=_provenance())
    state.transition_to(target)
    assert state.status == target
    assert state.is_terminal


@pytest.mark.parametrize("terminal", ["complete", "quarantined", "failed"])
def test_terminal_state_is_write_once(terminal):
    state = TerminalState(state_id="s1", status="pending", provenance=_provenance())
    state.transition_to(terminal)
    with pytest.raises(ValueError, match="Non-monotonic terminal transition"):
        state.transition_to("in_progress")


def test_terminal_state_rejects_unknown_status():
    with pytest.raises(ValueError, match="Unknown status"):
        TerminalState(state_id="s1", status="archived", provenance=_provenance())


def test_terminal_state_pending_can_go_via_in_progress():
    state = TerminalState(state_id="s1", status="pending", provenance=_provenance())
    state.transition_to("in_progress")
    state.transition_to("quarantined")
    assert state.status == "quarantined"


# --- JSON ingestion --------------------------------------------------------

def test_json_completeness_rejects_partial():
    with pytest.raises(ValueError, match="Reject partial or invalid JSON"):
        validate_json_complete('{"key": "value"')


def test_json_completeness_rejects_non_object():
    with pytest.raises(ValueError, match="expected a JSON object"):
        validate_json_complete('[1, 2, 3]')


def test_json_completeness_accepts_full_object():
    assert validate_json_complete('{"key": "value"}') == {"key": "value"}


# --- schema <-> dataclass round-trip ---------------------------------------

def _schema(name: str) -> dict:
    return load_schema(SCHEMA_DIR / name)


def test_snapshot_matches_its_schema():
    snap = _snapshot()
    payload = {
        "chapter_id": snap.chapter_id,
        "pids": list(snap.pids),
        "context": snap.context,
        "glossary_hash": snap.glossary_hash,
        "book_memory_hash": snap.book_memory_hash,
        "chapter_memory_hash": snap.chapter_memory_hash,
        "snapshot_hash": snap.snapshot_hash,
    }
    assert schema_validate(payload, _schema("v4_snapshot.schema.json")) == []


def test_chunk_plan_matches_its_schema():
    snap = _snapshot()
    plan = _chunk_plan(snap)
    payload = {
        "chunk_id": plan.chunk_id,
        "snapshot_hash": plan.snapshot_hash,
        "pids": list(plan.pids),
        "total_words": plan.total_words,
        "context": {"left_ru": plan.context.left_ru, "right_en": list(plan.context.right_en)},
        "undersized_exception": plan.undersized_exception,
    }
    assert schema_validate(payload, _schema("v4_chunkplan.schema.json")) == []


def test_candidate_matches_its_schema():
    snap = _snapshot()
    chunk = _chunk_plan(snap)
    cand = _candidate(chunk)
    payload = {
        "candidate_id": cand.candidate_id,
        "chunk_id": cand.chunk_id,
        "role": cand.role,
        "translation": [list(pair) for pair in cand.translation],
        "source_hash": cand.source_hash,
        "snapshot_hash": cand.snapshot_hash,
        "chunk_plan_hash": cand.chunk_plan_hash,
        "config_identity": cand.config_identity,
        "decision_trace": [],
    }
    assert schema_validate(payload, _schema("v4_candidates.schema.json")) == []


def test_candidate_schema_rejects_score_field_style_payload():
    # A payload shaped like the old (rejected) contract must fail: it is
    # missing the now-required identity/role fields entirely.
    legacy_style = {"candidate_id": "c1", "translation": "plain string", "score": 0.9}
    errs = schema_validate(legacy_style, _schema("v4_candidates.schema.json"))
    assert errs


def test_finding_matches_its_schema():
    finding = Finding(
        finding_id="f-0001", detector="qwen_semantic_audit", category="omission",
        severity="major", evidence="evidence text",
        region=Region(pid="p00001", start=0, end=5),
    )
    payload = {
        "finding_id": finding.finding_id,
        "detector": finding.detector,
        "category": finding.category,
        "severity": finding.severity,
        "evidence": finding.evidence,
        "region": {"pid": finding.region.pid, "start": finding.region.start, "end": finding.region.end},
    }
    assert schema_validate(payload, _schema("v4_findings.schema.json")) == []


def test_repair_matches_its_schema():
    repair = Repair(
        repair_id="r-0001", finding_ids=("f-0001",), chunk_id="c0001",
        action="region_edit", target_pids=("p00001",), instructions="fix",
    )
    payload = {
        "repair_id": repair.repair_id,
        "finding_ids": list(repair.finding_ids),
        "chunk_id": repair.chunk_id,
        "action": repair.action,
        "target_pids": list(repair.target_pids),
        "instructions": repair.instructions,
        "full_sentence_reason": repair.full_sentence_reason,
        "decision_trace": [],
        "auto_accepted": repair.auto_accepted,
    }
    assert schema_validate(payload, _schema("v4_repair.schema.json")) == []


def test_terminal_state_matches_its_schema():
    state = TerminalState(state_id="s1", status="quarantined", provenance=_provenance())
    payload = {
        "state_id": state.state_id,
        "status": state.status,
        "provenance": {
            "source_hash": state.provenance.source_hash,
            "chapter_snapshot_hash": state.provenance.chapter_snapshot_hash,
            "chunk_plan_hash": state.provenance.chunk_plan_hash,
            "prompt_bundle_hash": state.provenance.prompt_bundle_hash,
            "code_version": state.provenance.code_version,
            "policy_versions": state.provenance.policy_versions,
            "model_config": state.provenance.model_config,
        },
    }
    assert schema_validate(payload, _schema("v4_terminal_state.schema.json")) == []


def test_terminal_state_schema_rejects_old_vocabulary_only_completed():
    # The old contract's "completed" (without quarantined) must not silently
    # pass; the schema enum must be exactly the new vocabulary.
    payload = {
        "state_id": "s1",
        "status": "completed",
        "provenance": {
            "source_hash": _hash("s"), "chapter_snapshot_hash": _hash("c"),
            "chunk_plan_hash": _hash("p"), "prompt_bundle_hash": _hash("b"),
            "code_version": "v1", "policy_versions": {"x": "1"},
        },
    }
    errs = schema_validate(payload, _schema("v4_terminal_state.schema.json"))
    assert errs
