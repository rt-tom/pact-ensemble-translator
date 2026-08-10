"""B2 contract tests for pact_v4.repair.selective_repair.

Acceptance (card t_73e190f7):
- p00010/p00193-type (TP invented gender) -> repair AFTER verify (Tier B)
- p00106-type (FP dialogue tag: said -> поправил я) -> PASS, no change
- p00080-type (FP parsing) -> PASS, no change
- p00240-type (time TP) -> Tier A CONFIRMED -> repair напрямую
- cap 10 findings per chapter (policy_limit: repair_findings_cap_10)
- микробатчи (microbatches) when eligible > 4 (Cheng et al., [index] ids)
- TEaR: 0 eligible findings -> repair skipped entirely
- fail-closed: failed repair chunk -> debt, never silent PASS
- single re-audit at the end if >=1 committed repair (changed PIDs + window;
  > threshold -> full re-audit; failed re-audit -> debt, never "0 findings")

Everything runs against a scripted in-memory ``CompletionBackend`` — zero
real model calls, zero HTTP (B2 acceptance: hermetic suite). The gold PIDs
mirror the real chapter-0001 out-of-sample cases but the text is synthetic
(hermetic; no chapter data in the repo, per data restrictions).
"""
from __future__ import annotations

import inspect
import json
from typing import List, Mapping, Optional, Sequence

import pytest

from pact_v4.audit.hard_filters import (
    CONFIRMED,
    REJECTED,
    TIER_B,
    FilteredIssue,
    apply_hard_filters,
)
from pact_v4.repair.selective_repair import (
    MICROBATCH_TARGET,
    MICROBATCH_TRIGGER,
    POLICY_LIMIT_TAG,
    REPAIR_FINDINGS_CAP,
)
from pact_v4.repair import selective_repair as repair_module
from pact_v4.repair.selective_repair import (
    EligibleFinding,
    SelectiveRepairConfig,
    SelectiveRepairEvaluator,
    SelectiveRepairOutcome,
    apply_findings_cap,
    make_microbatches,
    parse_repair_batch,
    plan_reaudit_scope,
    select_eligible,
)
from pact_v4.runtime.backend_protocol import (
    BackendCallRecord,
    BackendDescriptor,
    CompletionBackend,
    CompletionError,
    CompletionRequest,
    CompletionResponse,
)
from pact_v4.runtime.prompts_runtime import (
    REPAIR_AS_VERIFIER_V1,
    render_reaudit_prompt,
    render_selective_repair_prompt,
)

# ---------------------------------------------------------------------------
# Fake backend (scripted, in-memory)
# ---------------------------------------------------------------------------


class ScriptedRepairBackend(CompletionBackend):
    """In-memory ``CompletionBackend`` returning scripted responses."""

    _BINDINGS = {
        "default": "gemma-4-26b",
        "generator": "gemma-4-26b",
        "qwen_audit": "qwen-3.6-35b",
        "fidelity_reviewer": "qwen-3.6-35b",
    }

    def __init__(self, script: Sequence[CompletionResponse]):
        self._script = list(script)
        self.requests: List[CompletionRequest] = []

    @property
    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(
            kind="local_llama",
            transport_version="openai-chat-completions/v1",
            endpoint_family="openai_chat_completions",
            public_endpoint="http://127.0.0.1:8094/v1/chat/completions",
            model_bindings=dict(self._BINDINGS),
            effective_options={"temperature": 0.0, "context_size": 49152},
        )

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        if not self._script:
            raise AssertionError("ScriptedRepairBackend: script exhausted")
        return self._script.pop(0)

    def close(self) -> None:
        pass

    def call_records(self) -> Sequence[BackendCallRecord]:
        return []


class _TransportFailingBackend(ScriptedRepairBackend):
    """Scripted backend that raises ``CompletionError`` on selected calls."""

    def __init__(self, script: Sequence[CompletionResponse], fail_on: Sequence[int] = (1,)):
        super().__init__(script)
        self._fail_on = set(fail_on)
        self._call_index = 0

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self._call_index += 1
        self.requests.append(request)
        if self._call_index in self._fail_on:
            raise CompletionError(f"simulated transport failure (call {self._call_index})")
        if not self._script:
            raise AssertionError("ScriptedRepairBackend: script exhausted")
        return self._script.pop(0)


def _repair_response(results: Sequence[Mapping[str, object]]) -> CompletionResponse:
    return CompletionResponse(
        text=json.dumps({"results": list(results)}, ensure_ascii=False),
        model="gemma-4-26b",
        finish_reason="stop",
    )


def _reaudit_response(issues: Sequence[Mapping[str, str]]) -> CompletionResponse:
    return CompletionResponse(
        text=json.dumps({"issues": list(issues)}, ensure_ascii=False),
        model="qwen-3.6-35b",
        finish_reason="stop",
    )


def _issue(pid, category, note="", excerpt="", severity="major", confidence="high"):
    return {
        "id": pid,
        "category": category,
        "severity": severity,
        "confidence": confidence,
        "note": note,
        "excerpt": excerpt,
    }


def _issue_with(pid: str, **overrides) -> FilteredIssue:
    """A FilteredIssue with a synthetic verdict (hermetic unit tests)."""
    issue = _issue(pid, overrides.pop("category", "changed_fact"))
    issue.update(overrides)
    verdict = overrides.pop("_verdict", TIER_B)
    return FilteredIssue(issue=issue, verdict=verdict, filter_name="test", reason="test")


# ---------------------------------------------------------------------------
# Eligibility (pure)
# ---------------------------------------------------------------------------


def test_tier_a_confirmed_eligible_direct():
    eligible, rejected, ineligible = select_eligible(
        [_issue_with("p00240", category="changed_fact", _verdict=CONFIRMED)]
    )
    assert len(eligible) == 1
    assert eligible[0].tier == "A"
    assert eligible[0].pid == "p00240"
    assert not rejected and not ineligible


def test_tier_b_high_confidence_eligible_verify():
    eligible, rejected, ineligible = select_eligible(
        [_issue_with("p00193", category="invented_gender", _verdict=TIER_B)]
    )
    assert len(eligible) == 1
    assert eligible[0].tier == "B"


def test_severity_is_not_an_eligibility_filter():
    # Out-of-sample review: real TPs are often minor, stylistic FPs major —
    # severity must never gate repair.
    eligible, _, _ = select_eligible(
        [_issue_with("p00010", category="invented_gender", severity="minor", _verdict=TIER_B)]
    )
    assert len(eligible) == 1


def test_rejected_never_repaired():
    eligible, rejected, ineligible = select_eligible(
        [_issue_with("p00285", category="changed_fact", _verdict=REJECTED)]
    )
    assert not eligible
    assert len(rejected) == 1
    assert not ineligible


def test_tier_b_low_confidence_ineligible():
    eligible, _, ineligible = select_eligible(
        [_issue_with("p00016", category="changed_fact", confidence="medium", _verdict=TIER_B)]
    )
    assert not eligible
    assert len(ineligible) == 1


def test_tier_b_category_outside_allowed_ineligible():
    eligible, _, ineligible = select_eligible(
        [_issue_with("p00099", category="style", _verdict=TIER_B)],
        allowed_categories=frozenset({"changed_fact"}),
    )
    assert not eligible
    assert len(ineligible) == 1


def test_eligible_confidence_high_medium_categories():
    issues = [
        _issue_with("p00010", category="invented_gender", confidence="high", _verdict=TIER_B),
        _issue_with("p00193", category="invented_gender", confidence="high", _verdict=TIER_B),
        _issue_with("p00016", category="changed_fact", confidence="medium", _verdict=TIER_B),
        _issue_with("p00075", category="omission", confidence="low", _verdict=TIER_B),
    ]
    eligible, _, ineligible = select_eligible(issues)
    assert [f.pid for f in eligible] == ["p00010", "p00193"]
    assert [f.pid for f in ineligible] == ["p00016", "p00075"]


# ---------------------------------------------------------------------------
# Cap 10 + microbatches
# ---------------------------------------------------------------------------


def test_cap_10_keeps_first_ten_and_tags_rest():
    issues = [
        _issue_with(f"p{i:05d}", category="invented_gender", confidence="high", _verdict=TIER_B)
        for i in range(1, 13)
    ]
    eligible, _, _ = select_eligible(issues)
    kept, capped = apply_findings_cap(eligible, cap=REPAIR_FINDINGS_CAP)
    assert len(kept) == 10
    assert len(capped) == 2
    assert [f.pid for f in capped] == ["p00011", "p00012"]


def test_cap_keeps_tier_a_before_tier_b():
    # Tier A (code-confirmed) must never be displaced by Tier B candidates
    # at the cap boundary.
    issues = [
        _issue_with("p00001", category="invented_gender", _verdict=TIER_B),
        _issue_with("p00002", category="changed_fact", _verdict=CONFIRMED),
    ]
    eligible, _, _ = select_eligible(issues)
    kept, capped = apply_findings_cap(eligible, cap=1)
    assert [f.pid for f in kept] == ["p00002"]  # Tier A first
    assert [f.pid for f in capped] == ["p00001"]


def test_microbatch_up_to_trigger_is_single_batch():
    eligible = [
        EligibleFinding(
            index=i, pid=f"p{i:05d}", tier="B", category="invented_gender",
            severity="minor", confidence="high", note="", excerpt="", issue={},
        )
        for i in range(1, MICROBATCH_TRIGGER + 1)
    ]
    batches = make_microbatches(eligible)
    assert len(batches) == 1
    assert len(batches[0]) == 4


def test_microbatch_above_trigger_splits_into_3_4():
    eligible = [
        EligibleFinding(
            index=i, pid=f"p{i:05d}", tier="B", category="invented_gender",
            severity="minor", confidence="high", note="", excerpt="", issue={},
        )
        for i in range(1, 7)
    ]
    batches = make_microbatches(eligible)
    assert len(batches) == 2
    assert [len(b) for b in batches] == [3, 3]
    # explicit [index] identifiers are re-numbered per batch, 1..N
    assert [f.index for f in batches[0]] == [1, 2, 3]
    assert [f.index for f in batches[1]] == [1, 2, 3]


def test_microbatch_7_splits_into_4_3():
    eligible = [
        EligibleFinding(
            index=i, pid=f"p{i:05d}", tier="B", category="negation",
            severity="major", confidence="high", note="", excerpt="", issue={},
        )
        for i in range(1, 8)
    ]
    batches = make_microbatches(eligible)
    assert [len(b) for b in batches] == [4, 3]


# ---------------------------------------------------------------------------
# Repair batch response parsing (fail-closed)
# ---------------------------------------------------------------------------


def _eligible(pid: str, index: int = 1) -> EligibleFinding:
    return EligibleFinding(
        index=index, pid=pid, tier="B", category="invented_gender",
        severity="minor", confidence="high", note="n", excerpt="e", issue={},
    )


def test_parse_repair_accepts_pass_and_repair():
    findings = (_eligible("p00193", 1), _eligible("p00106", 2))
    text = json.dumps({
        "results": [
            {"index": 1, "decision": "repair", "pid": "p00193",
             "repaired_translation": "— внук, — сказал я.", "reason": "confirmed"},
            {"index": 2, "decision": "pass", "reason": "dialogue tag, literary"},
        ]
    }, ensure_ascii=False)
    results, errors = parse_repair_batch(text, findings, {"p00193": "— внучка."})
    assert not errors
    assert len(results) == 2
    assert results[0].decision == "repair"
    assert results[0].pid == "p00193"
    assert results[1].decision == "pass"


def test_parse_repair_missing_index_fails_closed():
    findings = (_eligible("p00193", 1), _eligible("p00106", 2))
    text = json.dumps({
        "results": [{"index": 1, "decision": "pass", "reason": "ok"}]
    })
    results, errors = parse_repair_batch(text, findings, {})
    assert errors and "missing" in errors[0]


def test_parse_repair_duplicate_index_fails_closed():
    findings = (_eligible("p00193", 1),)
    text = json.dumps({
        "results": [
            {"index": 1, "decision": "pass", "reason": "a"},
            {"index": 1, "decision": "pass", "reason": "b"},
        ]
    })
    _, errors = parse_repair_batch(text, findings, {})
    assert errors and "duplicate" in errors[0]


def test_parse_repair_unknown_index_fails_closed():
    findings = (_eligible("p00193", 1),)
    text = json.dumps({"results": [{"index": 9, "decision": "pass"}]})
    _, errors = parse_repair_batch(text, findings, {})
    assert errors and "unknown" in errors[0]


def test_parse_repair_invalid_decision_fails_closed():
    findings = (_eligible("p00193", 1),)
    text = json.dumps({"results": [{"index": 1, "decision": "maybe"}]})
    _, errors = parse_repair_batch(text, findings, {})
    assert errors and "invalid decision" in errors[0]


def test_parse_repair_repair_pid_not_in_batch_fails_closed():
    findings = (_eligible("p00193", 1),)
    text = json.dumps({
        "results": [{"index": 1, "decision": "repair", "pid": "p99999",
                     "repaired_translation": "x"}]
    })
    _, errors = parse_repair_batch(text, findings, {})
    assert errors and "not a batch target" in errors[0]


def test_parse_repair_noop_repair_fails_closed():
    findings = (_eligible("p00193", 1),)
    text = json.dumps({
        "results": [{"index": 1, "decision": "repair", "pid": "p00193",
                     "repaired_translation": "same text"}]
    })
    _, errors = parse_repair_batch(text, findings, {"p00193": "same text"})
    assert errors and "no-op" in errors[0]


def test_parse_repair_invalid_json_fails_closed():
    findings = (_eligible("p00193", 1),)
    _, errors = parse_repair_batch("not json {", findings, {})
    assert errors and "not valid JSON" in errors[0]


# ---------------------------------------------------------------------------
# Re-audit scope planning
# ---------------------------------------------------------------------------


def test_reaudit_scope_changed_plus_neighbours():
    all_pids = [f"p{i:05d}" for i in range(1, 21)]
    scope, full = plan_reaudit_scope(["p00010"], all_pids, neighbour_window=2)
    assert not full
    assert scope == ("p00008", "p00009", "p00010", "p00011", "p00012")


def test_reaudit_scope_full_when_threshold_exceeded():
    all_pids = [f"p{i:05d}" for i in range(1, 21)]
    changed = [f"p{i:05d}" for i in range(1, 10)]  # 9 > threshold 8
    scope, full = plan_reaudit_scope(changed, all_pids, full_threshold=8)
    assert full
    assert tuple(scope) == tuple(all_pids)


def test_reaudit_scope_clamps_at_chapter_edges():
    all_pids = [f"p{i:05d}" for i in range(1, 6)]
    scope, full = plan_reaudit_scope(["p00001"], all_pids, neighbour_window=2)
    assert not full
    assert scope == ("p00001", "p00002", "p00003")


# ---------------------------------------------------------------------------
# End-to-end acceptance cases (scripted backend)
# ---------------------------------------------------------------------------


def _hard_filtered(issues, source, translation):
    return apply_hard_filters(issues, source=source, translation=translation)


def test_p00193_type_tp_repair_after_verify():
    """TP invented gender (grandchild -> внучка): Tier B -> verify -> repair."""
    issue = _issue(
        "p00193", "invented_gender",
        note="SOURCE uses gender-neutral grandchild, but TRANSLATION specifies "
             "female внучка without contextual establishment.",
        excerpt="внучка",
        severity="minor", confidence="high",
    )
    source = {"p00193": "Then you say it has to be a grandchild-"}
    translation = {"p00193": "А потом заявила, что это должна быть внучка-"}
    filtered = _hard_filtered([issue], source, translation)
    assert filtered[0].verdict == TIER_B

    backend = ScriptedRepairBackend([
        _repair_response([{
            "index": 1, "decision": "repair", "pid": "p00193",
            "repaired_translation": "А потом заявила, что это должен быть внук-",
            "reason": "source is gender-neutral grandchild",
        }]),
        _reaudit_response([]),  # clean re-audit
    ])
    evaluator = SelectiveRepairEvaluator(backend)
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered,
    )
    assert outcome.committed == (("p00193", "А потом заявила, что это должен быть внук-"),)
    assert outcome.repair_complete is True
    assert outcome.skipped is False
    assert outcome.reaudit is not None and outcome.reaudit.complete
    assert len(backend.requests) == 2  # 1 repair batch + 1 re-audit


def test_p00106_type_fp_dialogue_tag_pass_no_change():
    """FP dialogue tag (Ten, I said -> поправил я): PASS, no change."""
    issue = _issue(
        "p00106", "addition",
        note="Source simply says Ten, I said. Translation adds поправил я "
             "(corrected I), introducing an action not present in the source.",
        excerpt="поправил я",
        severity="minor", confidence="high",
    )
    source = {"p00106": "\u201cTen,\u201d I said."}
    translation = {"p00106": "\u2014 Десять, \u2014 поправил я."}
    filtered = _hard_filtered([issue], source, translation)
    assert filtered[0].verdict == TIER_B

    backend = ScriptedRepairBackend([
        _repair_response([{
            "index": 1, "decision": "pass",
            "reason": "поправил я is a literary interpretation of the speech "
                      "verb said — not a fidelity defect",
        }]),
    ])
    evaluator = SelectiveRepairEvaluator(backend)
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered,
    )
    assert outcome.committed == ()
    assert outcome.passed_pids == ("p00106",)
    assert outcome.repair_complete is True
    assert outcome.reaudit is None  # nothing committed -> no re-audit
    assert len(backend.requests) == 1


def test_p00080_type_fp_parsing_pass_no_change():
    """FP parsing (bumped into the wall -> задев ее плечом): PASS."""
    issue = _issue(
        "p00080", "changed_fact",
        note="SOURCE says the narrator bumped into the wall, but TRANSLATION "
             "uses задев ее плечом (bumping into its shoulder).",
        excerpt="задев ее плечом",
        severity="major", confidence="high",
    )
    source = {"p00080": "She reached out, arms extended for a hug, and I "
                        "flinched. I stepped back, and nearly knocked a picture "
                        "off the wall behind me as I bumped into the wall."}
    translation = {"p00080": "Она протянула руки для объятий, и я вздрогнул. "
                             "Я отступил назад и чуть не сбил картину со стены, "
                             "задев ее плечом."}
    filtered = _hard_filtered([issue], source, translation)
    assert filtered[0].verdict == TIER_B

    backend = ScriptedRepairBackend([
        _repair_response([{
            "index": 1, "decision": "pass",
            "reason": "auditor misparsed: задев ее плечом refers to the "
                      "picture on the wall, not a new fact",
        }]),
    ])
    evaluator = SelectiveRepairEvaluator(backend)
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered,
    )
    assert outcome.committed == ()
    assert outcome.passed_pids == ("p00080",)
    assert outcome.repair_complete is True


def test_p00240_type_tier_a_confirmed_repair_directly():
    """Time TP (Eleven-fifty at night -> Двенадцать минут первого ночи):
    Tier A CONFIRMED by hard filters -> repair напрямую (no verify wording
    needed — the model still produces the corrected text)."""
    issue = _issue(
        "p00240", "changed_fact",
        note="\u201cEleven-fifty at night\u201d (23:50) translated as "
             "\u201cДвенадцать минут первого ночи\u201d (00:12).",
        excerpt="Двенадцать минут первого ночи",
        severity="major", confidence="high",
    )
    source = {"p00240": "Eleven-fifty at night."}
    translation = {"p00240": "Двенадцать минут первого ночи."}
    filtered = _hard_filtered([issue], source, translation)
    assert filtered[0].verdict == CONFIRMED

    backend = ScriptedRepairBackend([
        _repair_response([{
            "index": 1, "decision": "repair", "pid": "p00240",
            "repaired_translation": "Без десяти двенадцать ночи.",
            "reason": "source time is 23:50, not 00:12",
        }]),
        _reaudit_response([]),
    ])
    evaluator = SelectiveRepairEvaluator(backend)
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered,
    )
    assert outcome.committed == (("p00240", "Без десяти двенадцать ночи."),)
    assert outcome.repair_complete is True
    assert len(backend.requests) == 2


def test_cap_10_policy_limit_debt():
    issues = [
        _issue(f"p{i:05d}", "invented_gender", note="n", confidence="high")
        for i in range(1, 13)
    ]
    source = {f"p{i:05d}": f"source {i}" for i in range(1, 13)}
    translation = {f"p{i:05d}": f"translation {i}" for i in range(1, 13)}
    # Force all to TIER_B via synthetic FilteredIssue (avoids 12 model calls
    # just to exercise the cap).
    filtered = [
        FilteredIssue(issue=iss, verdict=TIER_B, filter_name="semantic", reason="test")
        for iss in issues
    ]
    backend = ScriptedRepairBackend([
        _repair_response([{"index": i, "decision": "pass", "reason": "ok"}
                          for i in range(1, 5)]),
        _repair_response([{"index": i, "decision": "pass", "reason": "ok"}
                          for i in range(1, 4)]),
        _repair_response([{"index": i, "decision": "pass", "reason": "ok"}
                          for i in range(1, 4)]),
    ])
    evaluator = SelectiveRepairEvaluator(backend)
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered,
    )
    assert outcome.eligible_count == 12  # eligible before cap
    assert len(outcome.capped) == 2
    assert any(POLICY_LIMIT_TAG in d for d in outcome.debt_trace)
    assert len(backend.requests) == 3  # 4+3+3 microbatches, no re-audit
    assert outcome.repair_complete is True


def test_tear_zero_eligible_findings_skips_repair():
    issue = _issue("p00184", "invented_gender", note="n")
    source = {"p00184": "Rich was a nurse. He had trained at the city hospital."}
    translation = {"p00184": "Рич был медбратом. Он учился в городской больнице."}
    filtered = _hard_filtered([issue], source, translation)
    assert filtered[0].verdict == REJECTED  # source_gender refutes

    backend = ScriptedRepairBackend([])  # no script: any call would fail the test
    evaluator = SelectiveRepairEvaluator(backend)
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered,
    )
    assert outcome.skipped is True
    assert outcome.repair_complete is True
    assert outcome.committed == ()
    assert backend.requests == []


def test_failed_repair_chunk_debt_never_silent_pass():
    issue = _issue("p00193", "invented_gender", note="n", confidence="high")
    source = {"p00193": "Then you say it has to be a grandchild-"}
    translation = {"p00193": "А потом заявила, что это должна быть внучка-"}
    filtered = _hard_filtered([issue], source, translation)

    backend = _TransportFailingBackend([], fail_on=(1,))
    evaluator = SelectiveRepairEvaluator(backend)
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered,
    )
    assert outcome.batches[0].status == "FAILED"
    assert outcome.repair_complete is False
    assert any("failed repair batch" in d for d in outcome.debt_trace)
    assert outcome.committed == ()
    assert outcome.reaudit is None


def test_invalid_batch_response_debt():
    issue = _issue("p00193", "invented_gender", note="n", confidence="high")
    source = {"p00193": "Then you say it has to be a grandchild-"}
    translation = {"p00193": "А потом заявила, что это должна быть внучка-"}
    filtered = _hard_filtered([issue], source, translation)

    backend = ScriptedRepairBackend([
        CompletionResponse(text="not json", model="gemma-4-26b", finish_reason="stop"),
    ])
    evaluator = SelectiveRepairEvaluator(backend)
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered,
    )
    assert outcome.batches[0].status == "FAILED"
    assert outcome.repair_complete is False
    assert outcome.committed == ()


def test_failed_reaudit_debt_never_zero_findings():
    issue = _issue("p00193", "invented_gender", note="n", confidence="high")
    source = {"p00193": "Then you say it has to be a grandchild-"}
    translation = {"p00193": "А потом заявила, что это должна быть внучка-"}
    filtered = _hard_filtered([issue], source, translation)

    backend = _TransportFailingBackend(
        [
            _repair_response([{
                "index": 1, "decision": "repair", "pid": "p00193",
                "repaired_translation": "А потом заявила, что это должен быть внук-",
                "reason": "confirmed",
            }]),
        ],
        fail_on=(2,),  # re-audit transport failure
    )
    evaluator = SelectiveRepairEvaluator(backend)
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered,
    )
    assert outcome.committed != ()
    assert outcome.reaudit is not None and outcome.reaudit.failed
    assert outcome.repair_complete is False
    assert any("failed re-audit" in d for d in outcome.debt_trace)


def test_reaudit_finds_residual_issues_debt():
    issue = _issue("p00193", "invented_gender", note="n", confidence="high")
    source = {"p00193": "Then you say it has to be a grandchild-"}
    translation = {"p00193": "А потом заявила, что это должна быть внучка-"}
    filtered = _hard_filtered([issue], source, translation)

    backend = ScriptedRepairBackend([
        _repair_response([{
            "index": 1, "decision": "repair", "pid": "p00193",
            "repaired_translation": "А потом заявила, что это должен быть внук-",
            "reason": "confirmed",
        }]),
        _reaudit_response([{
            "id": "p00193", "category": "changed_fact", "severity": "major",
            "confidence": "high", "note": "residual",
        }]),
    ])
    evaluator = SelectiveRepairEvaluator(backend)
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered,
    )
    assert outcome.reaudit is not None and outcome.reaudit.complete
    assert len(outcome.reaudit.issues) == 1
    assert any("residual" in d for d in outcome.debt_trace)


def test_reaudit_scope_uses_changed_pids_and_neighbours():
    # Two changed PIDs near each other -> one re-audit call with the union
    # scope (changed + neighbours), never per-repair calls.
    issues = [
        _issue("p00010", "invented_gender", note="n", confidence="high"),
        _issue("p00011", "negation", note="n", confidence="high"),
    ]
    source = {
        "p00009": "The previous paragraph.",
        "p00010": "A wannabe-architect from the neighborhood.",
        "p00011": "I didn't already know that.",
        "p00012": "The following paragraph.",
    }
    translation = {
        "p00009": "Предыдущий абзац.",
        "p00010": "Девушкой, мечтавшей стать архитектором, из соседнего квартала.",
        "p00011": "Я уже не знал этого.",
        "p00012": "Следующий абзац.",
    }
    filtered = _hard_filtered(issues, source, translation)

    backend = ScriptedRepairBackend([
        _repair_response([
            {"index": 1, "decision": "repair", "pid": "p00010",
             "repaired_translation": "Парнем, мечтавшим стать архитектором, из соседнего квартала.",
             "reason": "gender-neutral"},
            {"index": 2, "decision": "pass", "reason": "ok"},
        ]),
        _reaudit_response([]),
    ])
    evaluator = SelectiveRepairEvaluator(backend, config=SelectiveRepairConfig(
        reaudit_neighbour_window=2,
    ))
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered,
    )
    assert outcome.committed == (("p00010", "Парнем, мечтавшим стать архитектором, из соседнего квартала."),)
    assert outcome.reaudit is not None and outcome.reaudit.complete
    # Only p00010 changed; its neighbour window covers p00009..p00012.
    assert outcome.reaudit.scope == ("p00009", "p00010", "p00011", "p00012")
    assert len(backend.requests) == 2  # one repair call + ONE re-audit


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def test_repair_prompt_contains_mandatory_verifier_wording():
    instructions = REPAIR_AS_VERIFIER_V1.instructions
    assert "candidate, not an established fact" in instructions
    assert "independently verify against SOURCE and TRANSLATION" in instructions
    assert "return PASS, no change" in instructions
    assert "Only repair after confirming" in instructions


def test_repair_prompt_mentions_dialogue_tags_fp_class():
    instructions = REPAIR_AS_VERIFIER_V1.instructions
    assert "speech" in instructions
    assert "позвала" in instructions and "перебила" in instructions


def test_repair_prompt_renders_findings_with_index_identifiers():
    findings = [
        EligibleFinding(index=1, pid="p00193", tier="B", category="invented_gender",
                        severity="minor", confidence="high", note="grandchild",
                        excerpt="внучка", issue={}),
        EligibleFinding(index=2, pid="p00240", tier="A", category="changed_fact",
                        severity="major", confidence="high", note="time",
                        excerpt="Двенадцать минут первого ночи", issue={}),
    ]
    prompt = render_selective_repair_prompt(
        chapter_id="0001",
        source={"p00193": "grandchild", "p00240": "Eleven-fifty at night."},
        translation={"p00193": "внучка", "p00240": "Двенадцать минут первого ночи."},
        findings=findings,
    )
    assert "[1] p00193 | CANDIDATE | invented_gender" in prompt
    assert "[2] p00240 | CONFIRMED | changed_fact" in prompt
    assert "SOURCE (PID -> English text)" in prompt
    assert "TRANSLATION (PID -> Russian text" in prompt


def test_reaudit_prompt_marks_context_pairs_context_only():
    from pact_v4.audit.chunked_audit import AuditPair
    audit = [AuditPair(pid="p00010", source="A wannabe-architect.",
                       translation="Девушкой, мечтавшей стать архитектором.")]
    context = [AuditPair(pid="p00009", source="Previous pair.",
                         translation="Предыдущая пара.")]
    prompt = render_reaudit_prompt(
        chapter_id="0001", audit_pairs=audit, context_pairs=context,
    )
    assert "RE-AUDIT PAIRS (changed PIDs + neighbours)" in prompt
    assert 'id="p00010"' in prompt
    assert 'id="p00009"' in prompt
    assert "CONTEXT_ONLY" in prompt
    assert "NEVER report an issue for a CONTEXT_ONLY pair" in prompt


# ---------------------------------------------------------------------------
# Backend-neutrality (dual-mode import guard)
# ---------------------------------------------------------------------------


def test_repair_module_does_not_import_local_lifecycle():
    # Inspect actual import statements (AST), not docstrings.
    import ast as _ast
    source = inspect.getsource(repair_module)
    tree = _ast.parse(source)
    imports: list = []
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
    for forbidden in ("ModelRouter", "LifecycleModelCaller"):
        assert forbidden not in " ".join(imports)


# ---------------------------------------------------------------------------
# Lifecycle wiring (B2 integration: LifecycleSelectiveRepairEvaluator)
# ---------------------------------------------------------------------------


class _FakeRouter:
    base_url = "http://127.0.0.1:1"  # never contacted (backend replaced)

    def __init__(self) -> None:
        self.resident_calls: List[str] = []

    def ensure_resident(self, model_key: str):
        self.resident_calls.append(model_key)


def test_lifecycle_selective_repair_ensures_generator_then_auditor():
    from pact_v4.runtime.model_lifecycle_adapters import (
        LifecycleSelectiveRepairEvaluator,
    )

    router = _FakeRouter()
    evaluator = LifecycleSelectiveRepairEvaluator(
        router, repair_model_name="gemma-4-26b", reaudit_model_name="qwen-3.6-35b"
    )
    captured: dict = {}

    class _DummyEvaluator:
        def __call__(self, **kwargs):
            captured.update(kwargs)
            on_phase = kwargs.get("on_phase")
            if on_phase:
                on_phase("repair")
                on_phase("reaudit")  # a committed repair triggers the re-audit
            return SelectiveRepairOutcome(
                schema="pact-repair/v1", harness_version="1.0",
                prompt_version="pact-v4-repair-as-verifier/v1",
                model="gemma-4-26b", eligible_count=1, capped=(),
                rejected=(), ineligible=(),
                batches=(), committed=(("p00193", "fixed"),),
                passed_pids=(), debt_trace=(), reaudit=None,
                repair_complete=True, skipped=False,
            )

    evaluator._evaluator = _DummyEvaluator()  # type: ignore[attr-defined]
    issue = _issue("p00193", "invented_gender", note="n", confidence="high")
    filtered = [FilteredIssue(issue=issue, verdict=TIER_B, filter_name="t", reason="r")]
    out = evaluator(
        chapter_id="0001", source={"p00193": "grandchild"},
        translation={"p00193": "внучка"}, filtered=filtered,
    )
    assert isinstance(out, SelectiveRepairOutcome)
    # repair phase (gemma) then re-audit phase (qwen)
    assert router.resident_calls == ["gemma", "qwen"]
    assert captured["chapter_id"] == "0001"


def test_lifecycle_selective_repair_skips_reaudit_residency_when_nothing_committed():
    from pact_v4.runtime.model_lifecycle_adapters import (
        LifecycleSelectiveRepairEvaluator,
    )

    router = _FakeRouter()
    evaluator = LifecycleSelectiveRepairEvaluator(
        router, repair_model_name="gemma-4-26b", reaudit_model_name="qwen-3.6-35b"
    )

    class _DummyEvaluator:
        def __call__(self, **kwargs):
            on_phase = kwargs.get("on_phase")
            if on_phase:
                on_phase("repair")  # repair phase only; no re-audit
            return SelectiveRepairOutcome(
                schema="pact-repair/v1", harness_version="1.0",
                prompt_version="pact-v4-repair-as-verifier/v1",
                model="gemma-4-26b", eligible_count=1, capped=(),
                rejected=(), ineligible=(), batches=(),
                committed=(), passed_pids=("p00106",),
                debt_trace=(), reaudit=None,
                repair_complete=True, skipped=False,
            )

    evaluator._evaluator = _DummyEvaluator()  # type: ignore[attr-defined]
    issue = _issue("p00106", "addition", note="n", confidence="high")
    filtered = [FilteredIssue(issue=issue, verdict=TIER_B, filter_name="t", reason="r")]
    evaluator(chapter_id="0001", source={"p00106": "Ten, I said."},
              translation={"p00106": "— Десять, — поправил я."}, filtered=filtered)
    assert router.resident_calls == ["gemma"]  # no re-audit -> no qwen
