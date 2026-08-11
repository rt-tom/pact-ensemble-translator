"""B2 contract tests for pact_v4.repair.selective_repair.

Acceptance (card t_73e190f7):
- p00010/p00193-type (TP invented gender) -> repair AFTER verify (Tier B)
- p00106-type (FP dialogue tag: said -> поправил я) -> PASS, no change
- p00080-type (FP parsing) -> PASS, no change
- p00240-type (time TP) -> Tier A CONFIRMED -> repair напрямую
- cap 100 findings per chapter (policy_limit: repair_findings_cap_100)
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
from pact_v4.runtime.json_resilience import JsonRetryPolicy
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
# Cap + microbatches
# ---------------------------------------------------------------------------


def test_cap_10_keeps_first_ten_and_tags_rest():
    issues = [
        _issue_with(f"p{i:05d}", category="invented_gender", confidence="high", _verdict=TIER_B)
        for i in range(1, 13)
    ]
    eligible, _, _ = select_eligible(issues)
    # Explicit small cap to exercise the boundary (the DEFAULT cap is 100).
    kept, capped = apply_findings_cap(eligible, cap=10)
    assert len(kept) == 10
    assert len(capped) == 2
    assert [f.pid for f in capped] == ["p00011", "p00012"]


def test_default_cap_100_keeps_all_37():
    issues = [
        _issue_with(f"p{i:05d}", category="invented_gender", confidence="high", _verdict=TIER_B)
        for i in range(1, 38)
    ]
    eligible, _, _ = select_eligible(issues)
    kept, capped = apply_findings_cap(eligible, cap=REPAIR_FINDINGS_CAP)
    assert REPAIR_FINDINGS_CAP >= 100
    assert len(kept) == 37
    assert not capped


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
    assert errors and "does not match finding pid" in errors[0]


def test_parse_repair_index_pid_mismatch_fails_closed():
    # HIGH review finding (fea68de): a repair may name a batch target PID of
    # a DIFFERENT index (index=1, pid of finding 2) — both are batch targets,
    # so the old "is a batch target" check passed and committed the fix to
    # the wrong paragraph. The index/PID contract requires the exact PID.
    findings = (_eligible("p00193", 1), _eligible("p00106", 2))
    text = json.dumps({
        "results": [{
            "index": 1, "decision": "repair", "pid": "p00106",
            "repaired_translation": "fix for the other paragraph",
            "reason": "oops",
        }]
    }, ensure_ascii=False)
    results, errors = parse_repair_batch(
        text, findings, {"p00193": "внучка", "p00106": "поправил я"}
    )
    assert not results
    assert errors and "does not match finding pid" in errors[0]


def test_parse_repair_multiple_findings_same_pid_each_index_must_match():
    # Several findings may share one PID (e.g. invented_gender + omission on
    # the same paragraph). Each index is still validated against its own
    # finding's PID — a shared-PID group answers per index like distinct PIDs.
    findings = (_eligible("p00193", 1), _eligible("p00193", 2))
    text = json.dumps({
        "results": [
            {"index": 1, "decision": "repair", "pid": "p00193",
             "repaired_translation": "первый фикс", "reason": "a"},
            {"index": 2, "decision": "repair", "pid": "p00193",
             "repaired_translation": "второй фикс", "reason": "b"},
        ]
    }, ensure_ascii=False)
    results, errors = parse_repair_batch(
        text, findings, {"p00193": "внучка"}
    )
    assert not errors
    assert [r.pid for r in results] == ["p00193", "p00193"]


def test_parse_repair_same_pid_wrong_index_fails_closed():
    # Same-PID group: index=1 answering with index=2's pid is a mismatch
    # even though the PID is a batch target.
    findings = (_eligible("p00193", 1), _eligible("p00193", 2))
    text = json.dumps({
        "results": [{
            "index": 1, "decision": "repair", "pid": "p00193",
            "repaired_translation": "x", "reason": "a",
        }]
    })
    _, errors = parse_repair_batch(text, findings, {"p00193": "внучка"})
    assert errors and "missing" in errors[0]  # index 2 unanswered


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
    # Explicit small cap to exercise the policy-limit debt path (the DEFAULT
    # cap is 100 — a run with 37 eligible repairs all of them, see
    # test_default_cap_100_repairs_all_37_eligible).
    evaluator = SelectiveRepairEvaluator(
        backend, config=SelectiveRepairConfig(findings_cap=10)
    )
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered,
    )
    assert outcome.eligible_count == 12  # eligible before cap
    assert len(outcome.capped) == 2
    assert any(POLICY_LIMIT_TAG in d for d in outcome.debt_trace)
    assert len(backend.requests) == 3  # 4+3+3 microbatches, no re-audit
    assert outcome.repair_complete is True
    # MEDIUM review finding (fea68de): each microbatch outcome carries its
    # ACTUAL batch index from the loop, never a hardcoded 1.
    assert [b.batch_index for b in outcome.batches] == [1, 2, 3]


def test_default_cap_100_repairs_all_37_eligible():
    """run_010 acceptance: 37 eligible findings must ALL be repaired via
    microbatches — 0 debt by policy_limit (the old cap of 10 cut 27 real
    findings into debt)."""
    n = 37
    issues = [
        _issue(f"p{i:05d}", "invented_gender", note="n", confidence="high")
        for i in range(1, n + 1)
    ]
    source = {f"p{i:05d}": f"source {i}" for i in range(1, n + 1)}
    translation = {f"p{i:05d}": f"translation {i}" for i in range(1, n + 1)}
    filtered = [
        FilteredIssue(issue=iss, verdict=TIER_B, filter_name="semantic", reason="test")
        for iss in issues
    ]
    # 37 eligible -> ceil(37/4) = 10 microbatches of 4/4/4/4/4/4/4/3/3/3
    # (base = n//n_batches, remainder spread evenly). All PASS -> no re-audit.
    sizes = [4] * 7 + [3] * 3
    script = [_repair_response(
        [{"index": i, "decision": "pass", "reason": "ok"}
         for i in range(1, size + 1)]
    ) for size in sizes]
    backend = ScriptedRepairBackend(script)
    evaluator = SelectiveRepairEvaluator(backend)  # DEFAULT config
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered,
    )
    assert outcome.eligible_count == 37
    assert outcome.capped == ()  # default cap 100: nothing beyond the cap
    assert not any(POLICY_LIMIT_TAG in d for d in outcome.debt_trace)
    assert len(outcome.batches) == 10  # microbatches, one call per group
    assert [len(b.findings) for b in outcome.batches] == [4] * 7 + [3] * 3
    assert outcome.repair_complete is True
    assert len(backend.requests) == 10


def test_microbatch_outcomes_carry_unique_batch_indexes():
    """Two microbatches (7 eligible > trigger 4) -> outcomes indexed 1, 2;
    a FAILED second batch keeps its own index (regression for the hardcoded
    ``batch_index=1`` bug)."""
    issues = [
        _issue(f"p{i:05d}", "invented_gender", note="n", confidence="high")
        for i in range(1, 8)
    ]
    source = {f"p{i:05d}": f"source {i}" for i in range(1, 8)}
    translation = {f"p{i:05d}": f"translation {i}" for i in range(1, 8)}
    filtered = [
        FilteredIssue(issue=iss, verdict=TIER_B, filter_name="semantic", reason="test")
        for iss in issues
    ]
    backend = _TransportFailingBackend(
        [
            _repair_response([{"index": i, "decision": "pass", "reason": "ok"}
                              for i in range(1, 5)]),
            _repair_response([{"index": i, "decision": "pass", "reason": "ok"}
                              for i in range(1, 4)]),
        ],
        fail_on=(2,),  # second microbatch transport failure
    )
    evaluator = SelectiveRepairEvaluator(backend)
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered,
    )
    assert [len(b.findings) for b in outcome.batches] == [4, 3]
    assert [b.batch_index for b in outcome.batches] == [1, 2]
    assert outcome.batches[1].status == "FAILED"
    assert outcome.repair_complete is False
    assert any("failed repair batch 2" in d for d in outcome.debt_trace)


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


def test_mismatched_index_pid_debt_no_commit():
    """HIGH review finding (fea68de): response index must match its finding
    PID. ``index=1, pid=<finding-2's pid>`` must fail closed: the batch goes
    to debt and NOTHING is committed (no repair on the wrong paragraph)."""
    issues = [
        _issue("p00193", "invented_gender", note="n", confidence="high"),
        _issue("p00106", "addition", note="n", confidence="high"),
    ]
    source = {
        "p00193": "Then you say it has to be a grandchild-",
        "p00106": "\u201cTen,\u201d I said.",
    }
    translation = {
        "p00193": "А потом заявила, что это должна быть внучка-",
        "p00106": "\u2014 Десять, \u2014 поправил я.",
    }
    filtered = _hard_filtered(issues, source, translation)
    assert all(f.verdict == TIER_B for f in filtered)

    backend = ScriptedRepairBackend([
        _repair_response([
            {
                # index 1 is finding p00193, but the model names p00106 — a
                # batch target, yet the WRONG one for this index.
                "index": 1, "decision": "repair", "pid": "p00106",
                "repaired_translation": "— Десять, — сказал я.",
                "reason": "confirmed",
            },
            {"index": 2, "decision": "pass", "reason": "dialogue tag, literary"},
        ]),
    ])
    evaluator = SelectiveRepairEvaluator(backend)
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered,
    )
    assert outcome.batches[0].status == "FAILED"
    assert outcome.repair_complete is False
    assert outcome.committed == ()  # nothing committed despite a 'repair'
    assert outcome.reaudit is None  # no committed repair -> no re-audit
    assert any("failed repair batch" in d for d in outcome.debt_trace)
    assert "does not match finding pid" in outcome.batches[0].error


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


def test_reaudit_empty_then_valid_retries():
    """run_010 acceptance (FIX 1a): a re-audit whose first attempt returns an
    EMPTY content (Qwen reasoning-only answer on the full input) must be
    retried — the second attempt's valid JSON completes the re-audit, instead
    of failing the chapter closed on the single call."""
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
        CompletionResponse(text="", model="qwen-3.6-35b", finish_reason="stop"),  # empty (run_010)
        _reaudit_response([]),  # valid on retry
    ])
    evaluator = SelectiveRepairEvaluator(
        backend, config=SelectiveRepairConfig(
            reaudit_retry=JsonRetryPolicy(max_retries=2, base_delay_seconds=0.0),
        )
    )
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered,
    )
    assert outcome.committed != ()
    assert outcome.reaudit is not None and outcome.reaudit.complete
    assert outcome.repair_complete is True
    assert not any("failed re-audit" in d for d in outcome.debt_trace)
    assert len(backend.requests) == 3  # 1 repair + 2 re-audit attempts


def test_reaudit_invalid_json_three_attempts_then_debt():
    """run_010 acceptance (FIX 1a): invalid JSON is retried up to 3 attempts;
    only after the budget is exhausted does the re-audit become debt."""
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
        CompletionResponse(text="not json", model="qwen-3.6-35b", finish_reason="stop"),
        CompletionResponse(text="also not json", model="qwen-3.6-35b", finish_reason="stop"),
        CompletionResponse(text="still not json", model="qwen-3.6-35b", finish_reason="stop"),
    ])
    evaluator = SelectiveRepairEvaluator(
        backend, config=SelectiveRepairConfig(
            reaudit_retry=JsonRetryPolicy(max_retries=2, base_delay_seconds=0.0),
        )
    )
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered,
    )
    assert outcome.committed != ()
    assert outcome.reaudit is not None and outcome.reaudit.failed
    assert "re-audit response invalid after 3 attempt(s)" in outcome.reaudit.reason
    assert outcome.repair_complete is False
    assert any("failed re-audit" in d for d in outcome.debt_trace)
    assert len(backend.requests) == 4  # 1 repair + 3 re-audit attempts


def test_reaudit_max_tokens_default_is_20000():
    """run_010 acceptance (FIX 1b): the re-audit output budget default must
    be >= 20000 (same input profile as the extractor: full source +
    translation; reasoning can exhaust 12000 on the full input)."""
    from pact_v4.repair.selective_repair import DEFAULT_REAUDIT_MAX_TOKENS
    assert DEFAULT_REAUDIT_MAX_TOKENS >= 20000
    assert SelectiveRepairConfig().reaudit_max_tokens >= 20000


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


def test_reaudit_request_carries_full_chapter_context_only():
    """HIGH review finding (fea68de): the actual re-audit call must include
    the FULL source + FULL current translation. Distant PIDs (far outside the
    reportable scope) appear in the request as CONTEXT_ONLY; the scope PIDs
    are the only reportable RE-AUDIT PAIRS."""
    issue = _issue("p00005", "invented_gender", note="n", confidence="high")
    source = {f"p{i:05d}": f"Source paragraph {i}." for i in range(1, 11)}
    translation = {f"p{i:05d}": f"Перевод абзаца {i}." for i in range(1, 11)}
    filtered = _hard_filtered([issue], source, translation)

    backend = ScriptedRepairBackend([
        _repair_response([{
            "index": 1, "decision": "repair", "pid": "p00005",
            "repaired_translation": "Исправленный перевод абзаца 5.",
            "reason": "confirmed",
        }]),
        _reaudit_response([]),
    ])
    evaluator = SelectiveRepairEvaluator(
        backend, config=SelectiveRepairConfig(reaudit_neighbour_window=2)
    )
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered,
    )
    assert outcome.committed != ()
    assert outcome.reaudit is not None and outcome.reaudit.complete
    assert outcome.reaudit.scope == ("p00003", "p00004", "p00005", "p00006", "p00007")
    assert len(backend.requests) == 2
    reaudit_prompt = backend.requests[1].messages[0].content
    assert "CONTEXT_ONLY" in reaudit_prompt
    # distant pairs (before AND after the scope) are present in the prompt
    assert "Перевод абзаца 1." in reaudit_prompt
    assert "Перевод абзаца 10." in reaudit_prompt
    assert 'id="p00001"' in reaudit_prompt
    assert 'id="p00010"' in reaudit_prompt
    # only the scope is reportable
    reaudit_section = reaudit_prompt.split(
        "RE-AUDIT PAIRS (changed PIDs + neighbours)"
    )[1]
    assert 'id="p00005"' in reaudit_section
    assert 'id="p00001"' not in reaudit_section
    assert 'id="p00010"' not in reaudit_section
    # the committed text is what the re-audit sees for the changed PID
    assert "Исправленный перевод абзаца 5." in reaudit_prompt


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


def test_reaudit_prompt_includes_distant_pairs_as_context_only():
    """HIGH review finding (fea68de): the re-audit input must be the FULL
    source + FULL translation — distant pairs (far before/after the scope)
    are present as CONTEXT_ONLY, and only the scope pairs are reportable
    (RE-AUDIT PAIRS)."""
    from pact_v4.audit.chunked_audit import AuditPair
    pairs = [
        AuditPair(pid=f"p{i:05d}", source=f"Source {i}.",
                  translation=f"Перевод {i}.")
        for i in range(1, 21)
    ]
    scope_pids = {f"p{i:05d}" for i in range(8, 13)}  # p00008..p00012
    audit = [p for p in pairs if p.pid in scope_pids]
    context = [p for p in pairs if p.pid not in scope_pids]
    prompt = render_reaudit_prompt(
        chapter_id="0001", audit_pairs=audit, context_pairs=context,
    )
    # distant pairs are in the prompt, marked CONTEXT_ONLY
    assert 'id="p00001"' in prompt  # far before the scope
    assert 'id="p00020"' in prompt  # far after the scope
    assert "Перевод 1." in prompt
    assert "Перевод 20." in prompt
    assert "CONTEXT_ONLY" in prompt
    # only the scope pairs are reportable
    reaudit_section = prompt.split("RE-AUDIT PAIRS (changed PIDs + neighbours)")[1]
    assert 'id="p00008"' in reaudit_section
    assert 'id="p00012"' in reaudit_section
    assert 'id="p00007"' not in reaudit_section
    assert 'id="p00013"' not in reaudit_section


def test_reaudit_prompt_full_scope_has_no_context_only():
    """When the changed-PID count exceeds the full threshold, the whole
    chapter is reportable — no pair is demoted to CONTEXT_ONLY."""
    from pact_v4.audit.chunked_audit import AuditPair
    pairs = [
        AuditPair(pid=f"p{i:05d}", source=f"S{i}", translation=f"T{i}")
        for i in range(1, 6)
    ]
    prompt = render_reaudit_prompt(chapter_id="0001", audit_pairs=pairs)
    # No CONTEXT_ONLY section is rendered (the frozen template's own wording
    # about CONTEXT_ONLY is fine; the block header must be absent).
    assert "CONTEXT_ONLY (full chapter pairs" not in prompt
    assert 'id="p00001"' in prompt
    assert 'id="p00005"' in prompt


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


def test_lifecycle_selective_repair_missing_reaudit_name_fails_closed():
    """HIGH review finding (fea68de): a missing re-audit (Qwen) binding must
    fail closed at construction with an explicit error — never a silent
    fallback to the generator model (residency=Qwen vs HTTP model=Gemma)."""
    from pact_v4.runtime.model_lifecycle_adapters import (
        LifecycleSelectiveRepairEvaluator,
    )

    router = _FakeRouter()
    with pytest.raises(ValueError, match="reaudit_model_name is required"):
        LifecycleSelectiveRepairEvaluator(
            router, repair_model_name="gemma-4-26b"  # no reaudit name
        )


def test_lifecycle_selective_repair_bindings_match_residency():
    """The repair backend must name the generator (Gemma) and the re-audit
    backend the auditor (Qwen) — residency and HTTP model agree."""
    from pact_v4.runtime.model_lifecycle_adapters import (
        LifecycleSelectiveRepairEvaluator,
    )

    router = _FakeRouter()
    evaluator = LifecycleSelectiveRepairEvaluator(
        router, repair_model_name="gemma-4-26b", reaudit_model_name="qwen-3.6-35b"
    )
    inner = evaluator._evaluator  # type: ignore[attr-defined]
    assert inner._repair_backend.api.config.model == "gemma-4-26b"
    assert inner._reaudit_backend.api.config.model == "qwen-3.6-35b"
    assert inner._repair_backend.api.config.chat_url == (
        f"{router.base_url}/v1/chat/completions"
    )
    assert inner._reaudit_backend.api.config.chat_url == (
        f"{router.base_url}/v1/chat/completions"
    )
