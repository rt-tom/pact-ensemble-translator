"""B1.3 contract tests: entity-context A/B harness + 8 §9.1 cases.

Everything runs against the scripted ``MockABBackend`` — zero real model
calls, zero HTTP (developer scope: mock-прогоны, 0 вызовов Qwen). Real
Qwen A/B runs are OWNER-ONLY (rule 2026-08-06); the harness CLI
(``python -m pact_v4.audit.b13_ab --backend real``) is the owner path and
is NOT exercised here.

Pinned contracts:
* 8 §9.1 fixtures: 2 positive + 4 negative + 2 provenance; every case's
  source/translation PID maps are parallel; gold sets are consistent with
  the case's own PIDs.
* Metrics: gold TP recall / gold negative rejection / NEW unknown issues
  (list, not the raw issue count).
* ``render_entity_context_text``: structured context -> etalon-style text
  block; per decision gate §9.5.3 ONLY verified claims render (candidate
  same_entity relations are dropped from the audit prompt — case 8).
* A/B on the SAME chunks: chunk layout identical across the three configs
  (none/gold/auto) — any outcome difference is caused only by the
  entity-context block; the mock run validates the wiring (prompts contain
  the entity block only in gold/auto).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pact_v4.audit.b13_ab import (
    B13_CASES,
    MockABBackend,
    _issue,
    _ok_response,
    build_source_artifact,
    case_by_id,
    compute_metrics,
    gold_negative_rejection,
    gold_tp_recall,
    new_unknown_issues,
    render_entity_context_text,
    run_ab,
)
from pact_v4.audit.chunked_audit import (
    ChunkedAuditConfig,
    ChunkedAuditEvaluator,
    ChunkedAuditOutcome,
    build_greedy_chunks,
)
from pact_v4.audit.entity_extractor import (
    AnchorRef,
    ChapterEntityContext,
    EntityClaim,
    EntityRecord,
    EvidenceRef,
)


# ---------------------------------------------------------------------------
# §9.1 fixture inventory
# ---------------------------------------------------------------------------


def test_b13_cases_inventory_matches_card() -> None:
    assert len(B13_CASES) == 8
    kinds = [c.kind for c in B13_CASES]
    assert kinds.count("positive") == 2
    assert kinds.count("negative") == 4
    assert kinds.count("provenance") == 2
    # §9.1 order: 1-2 positive, 3-6 negative, 7-8 provenance
    assert kinds == [
        "positive", "positive",
        "negative", "negative", "negative", "negative",
        "provenance", "provenance",
    ]
    assert [c.case_id for c in B13_CASES] == [str(i) for i in range(1, 9)]


def test_b13_cases_pid_maps_are_parallel_and_gold_sets_consistent() -> None:
    for case in B13_CASES:
        assert set(case.source) == set(case.translation), case.case_id
        assert case.source, case.case_id
        for pid, cat in case.gold_tp:
            assert pid in case.source, f"case {case.case_id} gold_tp pid {pid}"
            assert cat in (
                "omission", "addition", "referent", "invented_gender",
                "changed_fact", "negation",
            ), f"case {case.case_id} category {cat}"
        for pid in case.gold_negative:
            assert pid in case.source, f"case {case.case_id} gold_negative pid {pid}"
        # a PID cannot be both gold TP and gold negative
        tp_pids = {pid for pid, _ in case.gold_tp}
        assert not (tp_pids & set(case.gold_negative)), case.case_id


def test_b13_positive_cases_carry_gold_tp_and_context() -> None:
    for case_id in ("1", "2"):
        case = case_by_id(case_id)
        assert case.gold_tp, case_id
        assert "entity:" in case.entity_context, case_id


def test_b13_negative_and_provenance_cases_carry_gold_negative() -> None:
    for case_id in ("3", "4", "5", "6", "7", "8"):
        case = case_by_id(case_id)
        assert case.gold_negative, case_id


def test_b13_case2_gold_includes_p00002_invented_gender_canon() -> None:
    """§9.5.3 Fix 3: p00002 'Медсестра' IS a real gender error.

    The real Qwen run flagged p00002 as invented_gender and the harness
    counted it as 'unknown' — the gold set was incomplete. Canon check: in
    this SYNTHETIC case the nurse IS Rich (male) — the entity context says
    so — so the feminine 'медсестра подала' is a genuine invented_gender
    (the real chapter-0001 'The Nurse' is a female GENERIC, not Rich, and
    does not apply to this case). The gold must include it.
    """
    case = case_by_id("2")
    assert ("p00002", "invented_gender") in case.gold_tp
    assert ("p00004", "invented_gender") in case.gold_tp
    assert "male" in case.entity_context  # the case's own canon: Rich male
    assert "Rich" in case.entity_context


def test_b13_case8_context_is_post_fix_verified_only() -> None:
    """§9.5.3 Fix 2: case-8 fixture context is the POST-FIX form — no
    candidate claim reaches the audit prompt, only verified anchor facts."""
    case = case_by_id("8")
    assert "candidate" not in case.entity_context.lower()
    assert "not verified" not in case.entity_context.lower()
    assert "motorcycle" in case.entity_context
    assert case.gold_negative == ("p00003",)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _outcome(issues) -> ChunkedAuditOutcome:
    return ChunkedAuditOutcome(
        schema="pact-audit/v4", harness_version="4.1",
        prompt_version="pact-v4-reviewer-qwen-audit/v4.1", model="qwen-3.6-35b",
        reasoning_budget=8192, max_input_tokens=3600, max_tokens=12000,
        overlap_tokens=400, narrator_context=False, entity_context=True,
        chunk_count=1, successful_chunks=1, failed_chunks=(),
        audit_complete=True, issue_count=len(issues), issues=tuple(issues),
        chunks=(),
    )


def test_gold_tp_recall_requires_pid_and_category_match() -> None:
    gold = [("p00003", "changed_fact"), ("p00004", "invented_gender")]
    # same pid, wrong category -> not a hit
    issues = [_issue("p00003", category="invented_gender", note="wrong cat")]
    assert gold_tp_recall(issues, gold) == 0.0
    # exact match on both -> hit
    issues = [_issue("p00003", category="changed_fact", note="hit")]
    assert gold_tp_recall(issues, gold) == 0.5
    # empty gold -> trivially full recall
    assert gold_tp_recall([], []) == 1.0


def test_gold_negative_rejection_counts_any_issue_on_pid() -> None:
    gold_neg = ["p00002", "p00003", "p00004"]
    # issue on one negative pid (any category) -> 2/3 rejected
    issues = [_issue("p00002", category="changed_fact", note="violation")]
    assert gold_negative_rejection(issues, gold_neg) == pytest.approx(2 / 3)
    # clean -> full rejection
    assert gold_negative_rejection([], gold_neg) == 1.0
    assert gold_negative_rejection([], []) == 1.0


def test_new_unknown_issues_are_neither_tp_nor_negative() -> None:
    gold_tp = [("p00003", "changed_fact")]
    gold_neg = ["p00002"]
    issues = [
        _issue("p00003", category="changed_fact", note="gold TP"),
        _issue("p00002", category="changed_fact", note="negative violation"),
        _issue("p00099", category="addition", note="NEW unknown"),
        _issue("p00098", category="referent", note="NEW unknown 2"),
    ]
    unknown = new_unknown_issues(issues, gold_tp, gold_neg)
    assert sorted(i["id"] for i in unknown) == ["p00098", "p00099"]


def test_compute_metrics_aggregates_all_three() -> None:
    metrics = compute_metrics(
        _outcome([
            _issue("p00003", category="changed_fact", note="tp"),
            _issue("p00099", category="addition", note="unknown"),
        ]),
        gold_tp=[("p00003", "changed_fact"), ("p00004", "changed_fact")],
        gold_negative=["p00002"],
    )
    assert metrics["gold_tp_recall"] == 0.5
    assert metrics["gold_negative_rejection"] == 1.0
    assert metrics["new_unknown_count"] == 1
    assert [u["id"] for u in metrics["new_unknown"]] == ["p00099"]
    assert metrics["audit_complete"] is True


# ---------------------------------------------------------------------------
# Structured context -> text block renderer
# ---------------------------------------------------------------------------


def _sample_context() -> ChapterEntityContext:
    return ChapterEntityContext(
        schema="pact-v4-chapter-entity-context/v1",
        extractor_version="pact-v4-entity-extractor/v1",
        chapter_id="0001",
        source_hash="h" * 64,
        entities=(
            EntityRecord(
                entity="Blake's vehicle",
                canonical_type="motorcycle",
                anchor=AnchorRef(pid="p00007", span="motorcycle"),
                aliases=(),
                claims=(
                    EntityClaim(
                        kind="object_identity", value="bike = motorcycle",
                        status="candidate",
                        evidence=(EvidenceRef(pid="p00007", span="motorcycle"),),
                        evidence_windows=((),),
                    ),
                ),
            ),
            EntityRecord(
                entity="Rich",
                canonical_type="nurse",
                anchor=AnchorRef(pid="p00197", span="The nurse"),
                aliases=(),
                claims=(
                    EntityClaim(
                        kind="gender", value="male", status="verified",
                        evidence=(EvidenceRef(pid="p00197", span="him"),),
                        evidence_windows=((),),
                    ),
                ),
            ),
        ),
    )


def test_render_entity_context_text_mirrors_etalon_format() -> None:
    text = render_entity_context_text(_sample_context())
    assert "- entity: Blake's vehicle" in text
    assert "established_type: motorcycle" in text
    assert "gender: male (verified)" in text
    # Decision gate §9.5.3: candidate claims are DROPPED from the audit
    # prompt (the real Qwen run accepted a rendered candidate as fact —
    # case 8). Only verified claims render.
    assert "object_identity" not in text
    assert "(candidate)" not in text
    assert "evidence: p00007 (\"motorcycle\")" in text
    # verified claims stay
    assert "(verified)" in text


# ---------------------------------------------------------------------------
# A/B harness wiring (mock, 0 Qwen): SAME chunks across configs
# ---------------------------------------------------------------------------


def test_run_ab_same_chunks_three_configs_mock() -> None:
    source = {
        "p00001": "I pushed my motorcycle through the gap.",
        "p00002": "I set the motorcycle on the lawn.",
        "p00003": "\"Is that your bike?\"",
        "p00004": "I nodded. \"It's a cheap bike, but it's mine.\"",
    }
    translation = {pid: "пер." + src for pid, src in source.items()}
    # 3 configs x 1 chunk = 3 audit calls (no retry-shrink: responses GOOD)
    backend = MockABBackend(audit_script=[
        _ok_response([]),                     # none
        _ok_response([_issue("p00003")]),     # gold: finds the TP
        _ok_response([]),                     # auto (mock: empty)
    ])
    results = run_ab(
        chapter_id="0001", source=source, translation=translation,
        gold_entity_context="- entity: Blake's vehicle\n  established_type: motorcycle\n",
        backend=backend, auto_context="- entity: Blake's vehicle\n",
    )
    assert results["chunk_count"] == 1
    assert list(results["configs"]) == ["none", "gold", "auto"]
    for name in ("none", "gold", "auto"):
        assert results["configs"][name]["outcome"]["audit_complete"] is True
    assert results["configs"]["gold"]["outcome"]["issue_count"] == 1
    assert results["configs"]["none"]["outcome"]["issue_count"] == 0

    # prompts: entity block only in gold/auto, never in none (test leakage)
    prompts = [req.messages[0].content for req in backend.requests]
    assert len(prompts) == 3
    assert "CHAPTER ENTITY FACTS - SOURCE-DERIVED" not in prompts[0]
    assert "CHAPTER ENTITY FACTS - SOURCE-DERIVED" in prompts[1]
    assert "CHAPTER ENTITY FACTS - SOURCE-DERIVED" in prompts[2]


def test_run_ab_real_chapter_0001_8_chunks_identical_layout(tmp_path) -> None:
    """Real chapter 0001 (env-pointed) -> exactly 8 chunks, layout pinned.

    Mirrors the B1 acceptance (chunking 0001 = 8 chunks at max_input=3600)
    and proves the A/B uses the SAME chunks for every config. Skipped when
    the real artifacts are not pointed to (they are not part of the repo).
    """
    src_path = os.environ.get("PACT_B1_CH0001_SOURCE")
    tr_path = os.environ.get("PACT_B1_CH0001_TRANSLATION")
    if not (src_path and tr_path):
        pytest.skip("PACT_B1_CH0001_SOURCE/TRANSLATION not set")
    source = json.loads(Path(src_path).read_text(encoding="utf-8"))
    translation = json.loads(Path(tr_path).read_text(encoding="utf-8"))
    from pact_v4.audit.chunked_audit import pairs_from_maps

    pairs = pairs_from_maps(source, translation)
    assert len(pairs) == 400
    chunks = build_greedy_chunks(pairs, max_input=3600)
    assert len(chunks) == 8

    # mock A/B: 8 chunks x 3 configs = 24 audit calls
    backend = MockABBackend(audit_script=[_ok_response([]) for _ in range(24)])
    results = run_ab(
        chapter_id="0001", source=source, translation=translation,
        gold_entity_context="", backend=backend, out_dir=tmp_path,
    )
    assert results["chunk_count"] == 8
    assert results["pair_count"] == 400
    for name in ("none", "gold", "auto"):
        outcome = results["configs"][name]["outcome"]
        assert outcome["chunk_count"] == 8
        assert outcome["successful_chunks"] == 8
        assert outcome["audit_complete"] is True
    # A/B output persisted
    ab_json = tmp_path / "ab_mock.json"
    assert ab_json.exists()


# ---------------------------------------------------------------------------
# B1.2 extractor reuse: auto context is a validated ChapterEntityContext
# ---------------------------------------------------------------------------


def test_build_source_artifact_and_auto_context_render() -> None:
    case = case_by_id("1")
    artifact = build_source_artifact("0001", case.source)
    assert artifact.chapter_id == "0001"
    assert artifact.source_hash  # content-derived identity
    # a validated structured context renders into the etalon text block
    text = render_entity_context_text(_sample_context())
    assert text  # non-empty, no exception


def test_case_8_candidate_relation_never_verified_via_render() -> None:
    """§9.1 #8 provenance, post-decision-gate (§9.5.3): a candidate same_entity
    relation must NOT appear in the audit prompt AT ALL — the real Qwen run
    showed the auditor accepts a rendered candidate as fact (changed_fact FP).
    The renderer drops candidate claims; the audit sees only the verified
    anchor facts."""
    context = ChapterEntityContext(
        schema="pact-v4-chapter-entity-context/v1",
        extractor_version="pact-v4-entity-extractor/v1",
        chapter_id="0001",
        source_hash="h" * 64,
        entities=(
            EntityRecord(
                entity="Blake's vehicle",
                canonical_type="motorcycle",
                anchor=AnchorRef(pid="p00001", span="motorcycle"),
                aliases=(),
                claims=(
                    EntityClaim(
                        kind="object_identity", value="bike = motorcycle?",
                        status="candidate",
                        evidence=(EvidenceRef(pid="p00003", span="bike"),),
                        evidence_windows=((),),
                    ),
                ),
            ),
        ),
    )
    text = render_entity_context_text(context)
    assert "candidate" not in text
    assert "object_identity" not in text
    assert "bike" not in text
    # the verified anchor facts still render
    assert "- entity: Blake's vehicle" in text
    assert "established_type: motorcycle" in text
    assert "evidence: p00001 (\"motorcycle\")" in text


# ---------------------------------------------------------------------------
# CLI parsing sanity (mock path only; real path never invoked here)
# ---------------------------------------------------------------------------


def test_cli_parse_mock_flags() -> None:
    from pact_v4.audit.b13_ab import build_argparser

    args = build_argparser().parse_args([
        "--source", "s.json", "--translation", "t.json",
        "--out-dir", "out", "--backend", "mock",
    ])
    assert args.backend == "mock"
    assert args.gold_context == ""
    assert args.model == "Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"
