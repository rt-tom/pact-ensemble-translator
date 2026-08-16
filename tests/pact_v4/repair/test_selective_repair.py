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
from typing import List, Mapping, Optional, Sequence, Tuple

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
    extract_json_blocks,
    make_microbatches,
    merge_candidates_by_pid,
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
        [_issue_with("p00016", category="changed_fact", confidence="low", _verdict=TIER_B)]
    )
    assert not eligible
    assert len(ineligible) == 1


def test_tier_b_medium_confidence_eligible_verify():
    # Owner decision 2026-08-13 (run_remote_001): medium-confidence findings
    # go to the repair-as-verifier, which itself decides pass/repair — they
    # are eligible, never silently sent to debt.
    eligible, rejected, ineligible = select_eligible(
        [_issue_with("p00184", category="changed_fact", confidence="medium", _verdict=TIER_B)]
    )
    assert len(eligible) == 1
    assert eligible[0].tier == "B"
    assert eligible[0].confidence == "medium"
    assert not rejected and not ineligible


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
    # high AND medium are eligible (owner decision 2026-08-13); low is not.
    assert [f.pid for f in eligible] == ["p00010", "p00193", "p00016"]
    assert [f.pid for f in ineligible] == ["p00075"]


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


def _eligible(pid: str, index: int = 1, category: str = "invented_gender") -> EligibleFinding:
    return EligibleFinding(
        index=index, pid=pid, tier="B", category=category,
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
    results, errors, _ = parse_repair_batch(text, findings, {"p00193": "— внучка."})
    assert not errors
    assert len(results) == 2
    assert results[0].decision == "repair"
    assert results[0].pid == "p00193"
    assert results[1].decision == "pass"


def test_parse_repair_accepts_fenced_json():
    # RESILIENCE (t_406fc48c): the repair model sometimes wraps the batch
    # response in ```json fences — the tolerant parse must accept it.
    findings = (_eligible("p00193", 1),)
    payload = {
        "results": [
            {"index": 1, "decision": "pass", "reason": "verified against source"},
        ]
    }
    fenced = "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
    results, errors, _ = parse_repair_batch(fenced, findings, {"p00193": "x"})
    assert not errors
    assert results[0].decision == "pass"


def test_parse_repair_prose_wrapped_json():
    # Prose around the JSON block ('Here is the JSON: {...}').
    findings = (_eligible("p00193", 1),)
    payload = {
        "results": [
            {"index": 1, "decision": "pass", "reason": "verified against source"},
        ]
    }
    prose = "Here is the JSON: " + json.dumps(payload, ensure_ascii=False)
    results, errors, _ = parse_repair_batch(prose, findings, {"p00193": "x"})
    assert not errors
    assert results[0].decision == "pass"


def test_parse_repair_missing_index_fails_closed():
    findings = (_eligible("p00193", 1), _eligible("p00106", 2))
    text = json.dumps({
        "results": [{"index": 1, "decision": "pass", "reason": "ok"}]
    })
    results, errors, _ = parse_repair_batch(text, findings, {})
    assert errors and "missing" in errors[0]


def test_parse_repair_duplicate_index_fails_closed():
    findings = (_eligible("p00193", 1),)
    text = json.dumps({
        "results": [
            {"index": 1, "decision": "pass", "reason": "a"},
            {"index": 1, "decision": "pass", "reason": "b"},
        ]
    })
    _, errors, _ = parse_repair_batch(text, findings, {})
    assert errors and "duplicate" in errors[0]


def test_parse_repair_unknown_index_fails_closed():
    findings = (_eligible("p00193", 1),)
    text = json.dumps({"results": [{"index": 9, "decision": "pass"}]})
    _, errors, _ = parse_repair_batch(text, findings, {})
    assert errors and "unknown" in errors[0]


def test_parse_repair_invalid_decision_fails_closed():
    findings = (_eligible("p00193", 1),)
    text = json.dumps({"results": [{"index": 1, "decision": "maybe"}]})
    _, errors, _ = parse_repair_batch(text, findings, {})
    assert errors and "invalid decision" in errors[0]


def test_parse_repair_repair_pid_not_in_batch_fails_closed():
    findings = (_eligible("p00193", 1),)
    text = json.dumps({
        "results": [{"index": 1, "decision": "repair", "pid": "p99999",
                     "repaired_translation": "x"}]
    })
    _, errors, _ = parse_repair_batch(text, findings, {})
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
    results, errors, _ = parse_repair_batch(
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
    results, errors, _ = parse_repair_batch(
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
            "repaired_translation": "внучек", "reason": "a",
        }]
    })
    _, errors, _ = parse_repair_batch(text, findings, {"p00193": "внучка"})
    assert errors and "missing" in errors[0]  # index 2 unanswered


def test_parse_repair_noop_repair_converted_to_pass():
    """REPAIR-2 (t_768537b9, run_013 batch1): a no-op \"repair\" (the model
    returned the current text with decision='repair') is converted to a
    per-index PASS with a WARNING — it does NOT fail the batch (run_013: one
    no-op index killed the whole batch and pushed 4 real findings into debt).
    The other indices of the same batch are still processed normally."""
    findings = (
        _eligible("p00193", 1, category="changed_fact"),
        _eligible("p00106", 2, category="changed_fact"),
    )
    text = json.dumps({
        "results": [
            {"index": 1, "decision": "repair", "pid": "p00193",
             "repaired_translation": "same text", "reason": "no-op"},
            {"index": 2, "decision": "repair", "pid": "p00106",
             "repaired_translation": "реальный фикс", "reason": "fixed"},
        ]
    }, ensure_ascii=False)
    results, errors, warnings = parse_repair_batch(
        text, findings, {"p00193": "same text", "p00106": "старый текст"}
    )
    assert not errors  # batch survives the no-op index
    by_index = {r.index: r for r in results}
    assert by_index[1].decision == "pass"
    assert by_index[1].reason == "no-op repair converted to pass"
    # the other index is still processed normally (real repair survives)
    assert by_index[2].decision == "repair"
    assert by_index[2].repaired_translation == "реальный фикс"
    assert any("no-op repair converted to pass" in w for w in warnings)


def test_parse_repair_all_noop_batch_good_with_no_repairs():
    """REPAIR-2 (t_768537b9): if EVERY index of a batch is a no-op the batch
    is GOOD with no repairs committed (the model honestly decided nothing
    needed changing) — NOT a failed batch."""
    findings = (
        _eligible("p00193", 1, category="changed_fact"),
        _eligible("p00106", 2, category="changed_fact"),
    )
    text = json.dumps({
        "results": [
            {"index": 1, "decision": "repair", "pid": "p00193",
             "repaired_translation": "same text", "reason": "a"},
            {"index": 2, "decision": "repair", "pid": "p00106",
             "repaired_translation": "same two", "reason": "b"},
        ]
    }, ensure_ascii=False)
    results, errors, warnings = parse_repair_batch(
        text, findings, {"p00193": "same text", "p00106": "same two"}
    )
    assert not errors
    assert all(r.decision == "pass" for r in results)
    assert len(warnings) == 2


def test_parse_repair_invalid_json_fails_closed():
    findings = (_eligible("p00193", 1),)
    _, errors, _ = parse_repair_batch("not json {", findings, {})
    assert errors and "not valid JSON" in errors[0]


def test_parse_repair_truncated_fragment_rejected():
    """B3 (run_011): a repair that keeps <40% of the current text is a
    TRUNCATED repair (the model returned a fragment instead of the FULL
    corrected PID — 7 PIDs lost dialogues/sentences this way). The batch is
    REJECTED with 'truncated repair', never committed."""
    findings = (_eligible("p00193", 1),)
    current = {
        "p00193": "— Понимаю, — сказал я. Я встал и потянулся. Был почти "
                  "уверен, что завтра всё тело будет ломить. — Объяснять "
                  "не нужно. Я всё понимаю.",
    }
    assert len("Был почти уверен.") < 0.4 * len(current["p00193"])
    text = json.dumps({
        "results": [{
            "index": 1, "decision": "repair", "pid": "p00193",
            "repaired_translation": "Был почти уверен.",
            "reason": "обрезка",
        }]
    }, ensure_ascii=False)
    results, errors, _ = parse_repair_batch(text, findings, current)
    assert results == ()
    assert errors and "truncated repair" in errors[0]
    assert "40%" in errors[0]


def test_parse_repair_full_length_rewrite_accepted():
    """B3: the gate is a ONE-DIRECTIONAL length guard, NOT two-way
    similarity — a fix that heavily rewrites the paragraph but keeps >=40%
    of the text is accepted (the model may rephrase freely within the PID)."""
    findings = (_eligible("p00193", 1),)
    current = {"p00193": "Она была так поглощена собой, что почти не "
                         "заметила меня; её пальцы нервно перебирали что-то "
                         "на коленях."}
    repaired = "Она была так поглощена своими мыслями, что почти не " \
               "заметила меня; её пальцы нервно переплетались на коленях."
    assert len(repaired) >= 0.4 * len(current["p00193"])
    text = json.dumps({
        "results": [{
            "index": 1, "decision": "repair", "pid": "p00193",
            "repaired_translation": repaired, "reason": "переформулировано",
        }]
    }, ensure_ascii=False)
    results, errors, _ = parse_repair_batch(text, findings, current)
    assert not errors
    assert results[0].repaired_translation == repaired


def _four_findings() -> Tuple:
    """Four eligible findings (indices 1..4) for the tolerant-parser tests."""
    return (
        _eligible("p00001", 1),
        _eligible("p00002", 2),
        _eligible("p00003", 3),
        _eligible("p00004", 4),
    )


def test_parse_repair_top_level_list_wrapped() -> None:
    """REPAIR-ROBUST (run_0005 batch2): the model returned a top-level LIST
    ``[...]`` instead of ``{"results": [...]}`` — the tolerant parser wraps
    it and accepts all 4 complete records (previously 4 records were lost)."""
    findings = _four_findings()
    text = json.dumps([
        {"index": 1, "decision": "pass", "reason": "ok"},
        {"index": 2, "decision": "repair", "pid": "p00002",
         "repaired_translation": "полный исправленный текст второй", "reason": "r"},
        {"index": 3, "decision": "pass", "reason": "ok"},
        {"index": 4, "decision": "repair", "pid": "p00004",
         "repaired_translation": "полный исправленный текст четвёртый", "reason": "r"},
    ], ensure_ascii=False)
    results, errors, _ = parse_repair_batch(text, findings, {
        "p00002": "старый текст", "p00004": "старый текст",
    })
    assert not errors
    assert len(results) == 4
    assert [r.index for r in results] == [1, 2, 3, 4]


def test_parse_repair_truncated_missing_brace_recovers_records() -> None:
    """REPAIR-ROBUST (run_0005 batch3/5/7/9): the model dropped the final
    ``}`` (``...}]`` instead of ``...}]}``) — string-aware extraction of all
    balanced ``{..}`` blocks recovers every complete record (prototype:
    15/15), so the batch is GOOD instead of losing 15 records."""
    findings = _four_findings()
    payload = [
        {"index": 1, "decision": "pass", "reason": "ok"},
        {"index": 2, "decision": "repair", "pid": "p00002",
         "repaired_translation": "полный исправленный текст второй", "reason": "r"},
        {"index": 3, "decision": "pass", "reason": "ok"},
        {"index": 4, "decision": "repair", "pid": "p00004",
         "repaired_translation": "полный исправленный текст четвёртый", "reason": "r"},
    ]
    # The outer object is truncated: no closing '}' after the ']'.
    text = '{"results": [' + ",".join(json.dumps(x, ensure_ascii=False) for x in payload) + "]"
    results, errors, warnings = parse_repair_batch(text, findings, {
        "p00002": "старый текст", "p00004": "старый текст",
    })
    assert not errors
    assert len(results) == 4
    assert [r.index for r in results] == [1, 2, 3, 4]
    assert not warnings


def test_parse_repair_tolerant_skips_broken_record_keeps_batch() -> None:
    """REPAIR-ROBUST: in the tolerant path ONE broken record (bad pid) is
    SKIPPED with a warning, the other complete records are accepted — the
    whole batch is NOT failed for a single corrupt entry."""
    findings = _four_findings()
    text = '{"results": [' + ",".join([
        json.dumps({"index": 1, "decision": "pass", "reason": "ok"}, ensure_ascii=False),
        # broken: pid does not match finding p00002
        json.dumps({"index": 2, "decision": "repair", "pid": "p99999",
                    "repaired_translation": "чужой пид", "reason": "bad"}, ensure_ascii=False),
        json.dumps({"index": 3, "decision": "pass", "reason": "ok"}, ensure_ascii=False),
        json.dumps({"index": 4, "decision": "repair", "pid": "p00004",
                    "repaired_translation": "полный исправленный текст четвёртый",
                    "reason": "r"}, ensure_ascii=False),
    ]) + "]"
    results, errors, warnings = parse_repair_batch(text, findings, {
        "p00002": "старый текст", "p00004": "старый текст",
    })
    assert not errors
    assert [r.index for r in results] == [1, 3, 4]
    assert any("does not match finding pid" in w for w in warnings)


def test_parse_repair_tolerant_coverage_below_threshold_fails() -> None:
    """REPAIR-ROBUST: recovering 1 of 4 findings (25% < 50% coverage) is a
    sign of serious corruption — the batch FAILS as before (never accept 1
    record of 4)."""
    findings = _four_findings()
    text = '{"results": [' + json.dumps({"index": 1, "decision": "pass"}) + "]"
    results, errors, warnings = parse_repair_batch(text, findings, {})
    assert results == ()
    assert errors and "coverage" in errors[0]
    assert errors and "not valid JSON" in errors[0]
    assert any("no answer recovered" in w for w in warnings)


def test_parse_repair_tolerant_two_of_four_recovers_records() -> None:
    """REPAIR-ROBUST-PARTIAL (t_c0cb8e3c): a truncated 2-of-4 response
    (only indices 1,2 present, no closing ``}``) still returns the recovered
    records with NO errors — the 50% salvage policy is preserved at the
    PARSER level. The PARTIAL state (missing indices surfaced, batch never
    GOOD/complete) is decided by the caller (``_run_batch`` / evaluator),
    not here: the parser keeps its ``(results, errors, warnings)`` contract
    and the recovered records are retained for commit."""
    findings = _four_findings()
    text = '{"results": [' + ",".join([
        json.dumps({"index": 1, "decision": "pass", "reason": "ok"},
                   ensure_ascii=False),
        json.dumps({"index": 2, "decision": "pass", "reason": "ok"},
                   ensure_ascii=False),
    ]) + "]"  # truncated: no closing '}' after the ']'
    results, errors, warnings = parse_repair_batch(text, findings, {})
    assert not errors
    assert [r.index for r in results] == [1, 2]
    assert any("no answer recovered" in w and "[3, 4]" in w for w in warnings)


def test_parse_repair_empty_body_remains_failed() -> None:
    """REPAIR-ROBUST (run_0005 batch1: raw=0, finish=length): an EMPTY body
    has no content to salvage — the batch stays FAILED (the fix for batch1
    is reasoning low so the NEXT run does not burn out)."""
    findings = _four_findings()
    results, errors, _ = parse_repair_batch("", findings, {})
    assert results == ()
    assert errors and "not valid JSON" in errors[0]


def test_extract_json_blocks_string_aware_and_truncation_safe() -> None:
    """REPAIR-ROBUST: ``extract_json_blocks`` yields EVERY balanced ``{..}``
    block, string-aware (braces inside string literals never unbalance the
    scan), and keeps scanning past an unbalanced outer object so the inner
    records of a truncated body are still recovered."""
    blocks = extract_json_blocks(
        '{"results": [{"index": 1, "note": "a { b"}, {"index": 2}]'
    )
    assert len(blocks) == 2
    assert json.loads(blocks[0])["index"] == 1
    assert json.loads(blocks[1])["index"] == 2
    # A complete outer object is returned as one block (normal path).
    complete = extract_json_blocks('{"results": [{"index": 1}]}')
    assert len(complete) == 1
    assert json.loads(complete[0])["results"][0]["index"] == 1
    # Garbage with no balanced block -> nothing.
    assert extract_json_blocks("not json {") == ()


def test_repair_prompt_requires_full_pid_text_not_fragment():
    """B2 (run_011): REPAIR_AS_VERIFIER_V1 must instruct the model that
    repaired_translation is the FULL corrected PID text — never a fragment
    (the run_011 truncations came from the model returning a fragment)."""
    instructions = REPAIR_AS_VERIFIER_V1.instructions
    assert "FULL corrected text" in instructions
    assert "every sentence of the paragraph" in instructions
    assert "Never return a fragment" in instructions
    assert REPAIR_AS_VERIFIER_V1.version == "pact-v4-repair-as-verifier/v4"


def test_repair_prompt_guardrails_self_verification_present():
    """REPAIR-2 (t_768537b9, run_013 review): the repair prompt must carry
    the SELF-VERIFICATION block — the model rejects its own rewrite and
    keeps the original when the rewrite introduces a new fact/referent,
    changes an unrelated clause, swaps an unsupported gender, or creates a
    new ambiguity. No new verifier call (architect: same prompt)."""
    instructions = REPAIR_AS_VERIFIER_V1.instructions
    assert "SELF-VERIFICATION" in instructions
    assert "compare your REWRITTEN sentence against SOURCE again" in instructions
    assert "Reject your own rewrite and keep the original" in instructions
    assert "introduces a new fact or referent" in instructions
    assert "merely replaces one unsupported gender with another" in instructions
    assert "without changing any unrelated information" in instructions


def test_repair_prompt_guardrail_gender_rule_p00193():
    """REPAIR-2 (t_768537b9, run_013 p00193 regression): invented_gender
    repair on a gender-NEUTRAL source ('grandchild') must NOT replace one
    invented gender with the opposite (внучка→внук) — the GENDER RULE in the
    prompt says: source gender-neutral -> REMOVE the unsupported gender,
    never invent EITHER gender, never replace one invented gender with the
    opposite; source specifies gender -> restore the SOURCE gender."""
    instructions = REPAIR_AS_VERIFIER_V1.instructions
    assert "GENDER RULE" in instructions
    assert "invented gender" in instructions
    assert "REMOVE the unsupported gender entirely" in instructions
    assert "never replace one invented gender with the opposite" in instructions
    assert "restore the SOURCE gender" in instructions
    assert "gender-NEUTRAL (grandchild, child, person)" in instructions


def test_repair_prompt_guardrail_referent_rule_p00096():
    """REPAIR-2 (t_768537b9, run_013 p00096 regression): a referent/
    coreference repair must preserve the grammatical attachment of the
    surrounding clauses — the REFERENT RULE in the prompt forbids
    reassigning a modifier/action to another entity unless SOURCE supports
    it (p00096 moved the narrator's leaning to the object)."""
    instructions = REPAIR_AS_VERIFIER_V1.instructions
    assert "REFERENT RULE" in instructions
    assert "preserve the grammatical attachment" in instructions
    assert "Do not reassign a modifier or action to another entity" in instructions
    assert "unless SOURCE explicitly supports it" in instructions


def test_repair_and_reaudit_write_raw_reasoning_artifacts(tmp_path):
    """B1/C1 (run_011): the repair evaluator persists
    ``b3_repair_batch{N}_raw.txt``/``_reasoning.txt`` for EVERY batch and
    ``b3_repair_reaudit_raw.txt``/``_reasoning.txt`` for the re-audit — a
    parse failure (incl. the 'truncated repair' gate) leaves a disk trail."""
    issue = _issue(
        "p00193", "invented_gender",
        note="gender-neutral grandchild translated as female внучка",
        excerpt="внучка", severity="minor", confidence="high",
    )
    source = {"p00193": "Then you say it has to be a grandchild-"}
    translation = {"p00193": "А потом заявила, что это должна быть внучка-"}
    filtered = _hard_filtered([issue], source, translation)

    class _ReasoningBackend(ScriptedRepairBackend):
        def __init__(self, script: Sequence[CompletionResponse]) -> None:
            super().__init__(script)
            self._i = 0

        def complete(self, request: CompletionRequest) -> CompletionResponse:
            self.requests.append(request)
            if not self._script:
                raise AssertionError("ScriptedRepairBackend: script exhausted")
            resp = self._script.pop(0)
            self._i += 1
            return CompletionResponse(
                text=resp.text or "",
                model=resp.model or "qwen-3.6-35b",
                finish_reason="stop",
                raw_metadata={"reasoning": f"reasoning-call-{self._i}"},
            )

    backend = _ReasoningBackend([
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
        filtered=filtered, out_dir=tmp_path, out_base="b3_repair",
    )
    assert outcome.repair_complete is True
    batch_raw = tmp_path / "b3_repair_batch1_raw.txt"
    batch_reason = tmp_path / "b3_repair_batch1_reasoning.txt"
    reaudit_raw = tmp_path / "b3_repair_reaudit_chunk1_raw.txt"
    reaudit_reason = tmp_path / "b3_repair_reaudit_chunk1_reasoning.txt"
    assert batch_raw.exists() and batch_reason.exists()
    assert reaudit_raw.exists() and reaudit_reason.exists()
    assert "внук-" in batch_raw.read_text(encoding="utf-8")
    assert batch_reason.read_text(encoding="utf-8") == "reasoning-call-1"
    assert reaudit_reason.read_text(encoding="utf-8") == "reasoning-call-2"


def test_repair_batch_sends_request_options_reasoning_for_remote(
    tmp_path, monkeypatch,
) -> None:
    """REPAIR-ROBUST (t_b6fd6cbd): the Evaluator transports the configured
    repair reasoning effort (default 1 = low) via request_options for
    REMOTE-capable backends (opencode maps it to reasoningEffort) — the
    run_0005 batch1 fix (deepseek high burned 32k reasoning tokens before
    content)."""
    issue = _issue(
        "p00193", "invented_gender",
        note="gender-neutral grandchild translated as female внучка",
        excerpt="внучка", severity="minor", confidence="high",
    )
    source = {"p00193": "Then you say it has to be a grandchild-"}
    translation = {"p00193": "А потом заявила, что это должна быть внучка-"}
    filtered = _hard_filtered([issue], source, translation)

    backend = ScriptedRepairBackend([
        _repair_response([{
            "index": 1, "decision": "repair", "pid": "p00193",
            "repaired_translation": "А потом заявила, что это должен быть внук-",
            "reason": "source is gender-neutral grandchild",
        }]),
        _reaudit_response([]),  # clean re-audit
    ])
    # ScriptedRepairBackend is a plain CompletionBackend (not local) → the
    # reasoning transport guard resolves to True (request_options path).
    evaluator = SelectiveRepairEvaluator(
        backend,
        config=SelectiveRepairConfig(repair_reasoning=1),
    )
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered, out_dir=tmp_path, out_base="b3_repair",
    )
    assert outcome.repair_complete is True
    repair_requests = [
        r for r in backend.requests if "selective_repair" in (r.label or "")
    ]
    assert repair_requests, "a repair batch request is expected"
    assert repair_requests[0].request_options == {"reasoning": 1}


def test_repair_batch_reasoning_off_sends_no_request_options(
    tmp_path, monkeypatch,
) -> None:
    """REPAIR-ROBUST: ``repair_reasoning=0`` (off) keeps the historical
    request — no request_options at all."""
    issue = _issue("p00193", "invented_gender")
    source = {"p00193": "Then you say it has to be a grandchild-"}
    translation = {"p00193": "А потом заявила, что это должна быть внучка-"}
    filtered = _hard_filtered([issue], source, translation)
    backend = ScriptedRepairBackend([
        _repair_response([{
            "index": 1, "decision": "pass", "reason": "verified",
        }]),
    ])
    evaluator = SelectiveRepairEvaluator(
        backend,
        config=SelectiveRepairConfig(repair_reasoning=0),
    )
    evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered, out_dir=tmp_path, out_base="b3_repair",
    )
    repair_requests = [
        r for r in backend.requests if "selective_repair" in (r.label or "")
    ]
    assert repair_requests[0].request_options == {}


def test_repair_batch_local_backend_never_gets_request_options(
    tmp_path, monkeypatch,
) -> None:
    """REPAIR-ROBUST: a LOCAL backend (LocalOpenAIBackend and friends) must
    NEVER receive reasoning request_options — the local llama-server gets
    its reasoning budget from the server args (--reasoning-budget) and
    LocalOpenAIBackend rejects request_options as a library guard (owner
    rule: local servers always run with the same args)."""
    issue = _issue("p00193", "invented_gender")
    source = {"p00193": "Then you say it has to be a grandchild-"}
    translation = {"p00193": "А потом заявила, что это должна быть внучка-"}
    filtered = _hard_filtered([issue], source, translation)
    backend = ScriptedRepairBackend([
        _repair_response([{
            "index": 1, "decision": "pass", "reason": "verified",
        }]),
    ])
    # Simulate the local transport guard: the reasoning decision follows
    # the concrete transport, and a local backend resolves to False.
    monkeypatch.setattr(
        repair_module,
        "_reasoning_transported_via_request_options",
        lambda _backend, _model_ref: False,
    )
    evaluator = SelectiveRepairEvaluator(
        backend,
        config=SelectiveRepairConfig(repair_reasoning=1),
    )
    evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered, out_dir=tmp_path, out_base="b3_repair",
    )
    repair_requests = [
        r for r in backend.requests if "selective_repair" in (r.label or "")
    ]
    assert repair_requests[0].request_options == {}


def test_repair_streams_reasoning_live_during_call(tmp_path):
    """REASONING-STREAM acceptance: the repair batch reasoning file is
    created BEFORE the call and grows live — a scripted backend firing
    on_reasoning_chunk mid-call sees the file already populated, and the
    authoritative post-completion write still carries the full reasoning."""
    issue = _issue(
        "p00193", "invented_gender",
        note="gender-neutral grandchild translated as female внучка",
        excerpt="внучка", severity="minor", confidence="high",
    )
    source = {"p00193": "Then you say it has to be a grandchild-"}
    translation = {"p00193": "А потом заявила, что это должна быть внучка-"}
    filtered = _hard_filtered([issue], source, translation)
    observed: dict = {}

    class _StreamingBackend(ScriptedRepairBackend):
        def __init__(self, script):
            super().__init__(script)
            self._first = True

        def complete(self, request: CompletionRequest) -> CompletionResponse:
            self.requests.append(request)
            if not self._script:
                raise AssertionError("ScriptedRepairBackend: script exhausted")
            resp = self._script.pop(0)
            if request.on_reasoning_chunk is not None and self._first:
                # Only the repair-batch call (not the re-audit) proves the
                # live growth of the batch reasoning file.
                request.on_reasoning_chunk("live-")
                request.on_reasoning_chunk("repair")
                observed["during"] = (
                    tmp_path / "b3_repair_batch1_reasoning.txt"
                ).read_text(encoding="utf-8")
                self._first = False
            return CompletionResponse(
                text=resp.text or "",
                model="qwen-3.6-35b",
                finish_reason="stop",
                raw_metadata={"reasoning": "full-reasoning"},
            )

    evaluator = SelectiveRepairEvaluator(_StreamingBackend([
        _repair_response([{
            "index": 1, "decision": "repair", "pid": "p00193",
            "repaired_translation": "А потом заявила, что это должен быть внук-",
            "reason": "source is gender-neutral grandchild",
        }]),
        _reaudit_response([]),  # clean re-audit
    ]))
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered, out_dir=tmp_path, out_base="b3_repair",
    )
    assert outcome.repair_complete is True
    assert observed["during"] == "live-repair"
    assert (tmp_path / "b3_repair_batch1_reasoning.txt").read_text(
        encoding="utf-8"
    ) == "full-reasoning"


def test_truncated_repair_rejected_leaves_raw_artifact(tmp_path):
    """B3 + B1: a batch whose model returns a truncated fragment is FAILED
    with 'truncated repair' AND the raw response is preserved on disk."""
    issue = _issue(
        "p00193", "invented_gender",
        note="gender-neutral grandchild translated as female внучка",
        excerpt="внучка", severity="minor", confidence="high",
    )
    source = {"p00193": "Then you say it has to be a grandchild-"}
    translation = {
        "p00193": "А потом заявила, что это должна быть внучка-"
                  " Она была так поглощена собой, что почти не заметила меня;"
                  " её пальцы нервно перебирали что-то на коленях.",
    }
    filtered = _hard_filtered([issue], source, translation)
    backend = ScriptedRepairBackend([
        _repair_response([{
            "index": 1, "decision": "repair", "pid": "p00193",
            "repaired_translation": "Она была так поглощена собой.",
            "reason": "обрезка",
        }]),
    ])
    evaluator = SelectiveRepairEvaluator(backend)
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered, out_dir=tmp_path, out_base="b3_repair",
    )
    assert outcome.repair_complete is False
    assert any("truncated repair" in d for d in outcome.debt_trace)
    raw = tmp_path / "b3_repair_batch1_raw.txt"
    assert raw.exists()
    assert "Она была так поглощена собой." in raw.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Re-audit scope planning
# ---------------------------------------------------------------------------


def test_reaudit_scope_changed_plus_neighbours():
    all_pids = [f"p{i:05d}" for i in range(1, 21)]
    scope = plan_reaudit_scope(["p00010"], all_pids, neighbour_window=2)
    assert scope == ("p00008", "p00009", "p00010", "p00011", "p00012")


def test_reaudit_scope_never_whole_chapter_above_threshold():
    """REPAIR-CTX (t_97b31f81): the whole-chapter re-audit mode is CANCELLED
    — even with many changed PIDs the scope stays the changed + neighbours
    region (the old full_threshold returned the whole chapter)."""
    all_pids = [f"p{i:05d}" for i in range(1, 21)]
    changed = [f"p{i:05d}" for i in range(1, 10)]  # 9 changed PIDs
    scope = plan_reaudit_scope(changed, all_pids, neighbour_window=2)
    # 9 changed with ±2 covers p00001..p00011 — NOT the whole 20-PID chapter
    assert scope == tuple(all_pids[:11])
    # a 50-PID chapter with 9 changed must NOT return the whole chapter
    big = [f"p{i:05d}" for i in range(1, 51)]
    scope_big = plan_reaudit_scope(changed, big, neighbour_window=2)
    assert scope_big != tuple(big)
    assert scope_big == (
        "p00001", "p00002", "p00003", "p00004", "p00005", "p00006",
        "p00007", "p00008", "p00009", "p00010", "p00011",
    )


def test_reaudit_scope_clamps_at_chapter_edges():
    all_pids = [f"p{i:05d}" for i in range(1, 6)]
    scope = plan_reaudit_scope(["p00001"], all_pids, neighbour_window=2)
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


def test_tolerant_two_of_four_partial_never_complete() -> None:
    """REPAIR-ROBUST-PARTIAL (t_c0cb8e3c, review finding): a truncated
    2-of-4 tolerant response (only indices 1,2 recovered for 4 findings)
    must NOT be published as complete. The recovered records are retained
    (salvage policy preserved) but the batch is PARTIAL: the missing
    indices 3,4 are surfaced in ``missing_indices`` and routed to debt, and
    ``repair_complete`` stays False — the 50% threshold is a recovery floor,
    never a publication-complete threshold."""
    issues = [
        _issue(f"p{i:05d}", "invented_gender", note="n", confidence="high")
        for i in range(1, 5)
    ]
    source = {f"p{i:05d}": f"source {i}" for i in range(1, 5)}
    translation = {f"p{i:05d}": f"перевод {i}" for i in range(1, 5)}
    filtered = _hard_filtered(issues, source, translation)
    assert all(f.verdict == TIER_B for f in filtered)

    # Truncated outer object: the model returned only indices 1,2 and
    # dropped the closing '}' (the exact 2-of-4 reproducer).
    text = '{"results": [' + ",".join([
        json.dumps({"index": 1, "decision": "pass", "reason": "ok"},
                   ensure_ascii=False),
        json.dumps({"index": 2, "decision": "pass", "reason": "ok"},
                   ensure_ascii=False),
    ]) + "]"  # no closing '}' — tolerant salvage path
    backend = ScriptedRepairBackend([
        CompletionResponse(text=text, model="gemma-4-26b", finish_reason="stop"),
    ])
    evaluator = SelectiveRepairEvaluator(backend)
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered,
    )

    # Recovered records retained (salvage policy), but the batch is PARTIAL.
    assert len(outcome.batches) == 1
    assert outcome.batches[0].status == "PARTIAL"
    assert outcome.batches[0].missing_indices == (3, 4)
    assert [r.index for r in outcome.batches[0].results] == [1, 2]
    # The recovered passes are retained as passed_pids (p00001, p00002).
    assert outcome.passed_pids == ("p00001", "p00002")
    # Missing findings are surfaced in debt, not silently accepted.
    assert any("p00003" in d and "index 3" in d for d in outcome.debt_trace)
    assert any("p00004" in d and "index 4" in d for d in outcome.debt_trace)
    assert any("partial tolerant repair" in d for d in outcome.debt_trace)
    # Never complete, never released.
    assert outcome.repair_complete is False
    assert outcome.skipped is False


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
    assert "re-audit chunk 1 response invalid after 3 attempt(s)" in outcome.reaudit.reason
    assert outcome.repair_complete is False
    assert any("failed re-audit" in d for d in outcome.debt_trace)
    assert len(backend.requests) == 4  # 1 repair + 3 re-audit attempts


def test_reaudit_accepts_fenced_json_no_retry():
    """RESILIENCE (t_406fc48c): a fence-wrapped re-audit response is valid
    JSON — it must parse on the first attempt, never be retried as
    truncated and never become debt."""
    issue = _issue("p00193", "invented_gender", note="n", confidence="high")
    source = {"p00193": "Then you say it has to be a grandchild-"}
    translation = {"p00193": "А потом заявила, что это должна быть внучка-"}
    filtered = _hard_filtered([issue], source, translation)

    fenced_ok = "```json\n" + json.dumps({"issues": []}) + "\n```"
    backend = ScriptedRepairBackend([
        _repair_response([{
            "index": 1, "decision": "repair", "pid": "p00193",
            "repaired_translation": "А потом заявила, что это должен быть внук-",
            "reason": "confirmed",
        }]),
        CompletionResponse(text=fenced_ok, model="qwen-3.6-35b", finish_reason="stop"),
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
    assert len(backend.requests) == 2  # 1 repair + 1 re-audit (no retry)


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


def test_reaudit_request_carries_local_context_and_repaired_delta():
    """REPAIR-CTX (t_97b31f81): the re-audit request carries ONLY the
    affected region (changed PIDs + neighbours) plus the REPAIRED CHANGES
    delta — distant PIDs are NOT in the prompt (run_012 re-audit input was
    41.5k tokens and truncated the 49k context)."""
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
    # the REPAIRED CHANGES delta tells the auditor what the repair changed
    assert "REPAIRED CHANGES" in reaudit_prompt
    assert "before:" in reaudit_prompt and "after:" in reaudit_prompt
    assert "Перевод абзаца 5." in reaudit_prompt
    assert "Исправленный перевод абзаца 5." in reaudit_prompt
    # the affected region is in the prompt (audit + preceding overlap)
    assert 'id="p00003"' in reaudit_prompt
    assert 'id="p00007"' in reaudit_prompt
    # distant pairs (far after the scope) are NOT dragged in
    assert "Перевод абзаца 10." not in reaudit_prompt
    assert 'id="p00010"' not in reaudit_prompt
    # only the scope is reportable
    reaudit_section = reaudit_prompt.split(
        "RE-AUDIT PAIRS (changed PIDs + neighbours)"
    )[1]
    assert 'id="p00005"' in reaudit_section
    assert 'id="p00003"' in reaudit_section


def test_reaudit_context_pid_issue_dropped_complete() -> None:
    """CONTEXT-PID-DROP (owner 2026-08-15): the re-audit model is given
    context_pairs (overlap) for continuity and must NOT re-audit them — an
    issue on a context pid is dropped per-issue (journaled, never a
    finding), the re-audit stays complete=True instead of failed debt (run
    gl.6 p00251 case: the same validate_chunk_json error used to fail the
    re-audit chunk -> failed=True -> debt)."""
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
        # re-audit response: one valid residual issue on an owned pid
        # (p00005) + one issue on a CONTEXT pid (p00002, overlap before the
        # scope) -> the context issue is dropped per-issue. Both issues are
        # complete canonical issue objects (non-empty note AND excerpt) —
        # RV4 t_cfb1523d: a scope-dropped issue missing a canonical field
        # is a structural failure, never a journaled dropped object.
        _reaudit_response([
            {"id": "p00005", "category": "changed_fact", "severity": "major",
             "confidence": "high", "note": "residual", "excerpt": "text"},
            {"id": "p00002", "category": "changed_fact", "severity": "major",
             "confidence": "high", "note": "context-only", "excerpt": "text"},
        ]),
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
    # the context-pid issue is dropped — never a re-audit finding
    assert [i["id"] for i in outcome.reaudit.issues] == ["p00005"]
    assert len(backend.requests) == 2


def test_reaudit_token_budget_local_region() -> None:
    """REPAIR-CTX acceptance: the re-audit input for a 400-PID chapter with a
    handful of changed PIDs must estimate well under ~5k tokens (run_012
    re-audit input was 41.5k tokens and truncated the 49k context)."""
    from pact_v4.audit.chunked_audit import text_token_estimate
    source, translation = _chapter_maps(400)
    changed = ("p00010", "p00200", "p00390")
    backend = ScriptedRepairBackend([_reaudit_response([])])
    evaluator = SelectiveRepairEvaluator(
        backend, config=SelectiveRepairConfig(reaudit_neighbour_window=2),
    )
    outcome = evaluator._run_reaudit(
        chapter_id="0001", source=source, translation=translation,
        original_translation=translation, changed_pids=changed,
        entity_context="", narrator_context="",
    )
    assert outcome.complete and not outcome.failed
    prompt = backend.requests[0].messages[0].content
    assert text_token_estimate(prompt) <= 5000, \
        f"re-audit input too large: {text_token_estimate(prompt):.0f} tokens"
    # distant PIDs are NOT in the prompt
    assert "p00100" not in prompt
    assert 'id="p00200"' in prompt  # a changed PID IS present
    # the delta block names every changed PID
    assert "p00010" in prompt.split("REPAIRED CHANGES")[1]
    assert "p00390" in prompt.split("REPAIRED CHANGES")[1]


def test_reaudit_chunked_multiple_calls_for_large_region():
    """REPAIR-CTX: a large affected region is re-audited in MULTIPLE chunks
    (like the audit), not one full-chapter call — one request per chunk and
    the chunk header labels each."""
    source, translation = _chapter_maps(60)
    changed = (f"p{i:05d}" for i in range(10, 40))  # 30 changed PIDs
    backend = ScriptedRepairBackend(
        [_reaudit_response([]) for _ in range(8)]  # enough responses
    )
    evaluator = SelectiveRepairEvaluator(
        backend, config=SelectiveRepairConfig(
            reaudit_neighbour_window=2,
            reaudit_max_input_tokens=300,  # small budget -> many chunks
        ),
    )
    outcome = evaluator._run_reaudit(
        chapter_id="0001", source=source, translation=translation,
        original_translation=translation, changed_pids=tuple(changed),
        entity_context="", narrator_context="",
    )
    assert outcome.complete and not outcome.failed
    assert len(backend.requests) >= 2, "large region must be chunked"
    chunk_headers = [
        r.messages[0].content for r in backend.requests
    ]
    assert any("RE-AUDIT PAIRS (chunk 1 of " in p for p in chunk_headers)
    assert any("RE-AUDIT PAIRS (chunk 2 of " in p for p in chunk_headers)
    # every chunk carries the REPAIRED CHANGES delta
    assert all("REPAIRED CHANGES" in p for p in chunk_headers)


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


def _chapter_maps(n: int) -> tuple:
    """Synthetic chapter: 400-style PID maps, source order = insertion order."""
    source = {f"p{i:05d}": f"Source paragraph {i} with some English words." for i in range(n)}
    translation = {pid: f"Перевод абзаца {i} — синтетический текст." for i, pid in enumerate(source)}
    return source, translation


def test_repair_prompt_local_context_only_findings_plus_neighbours():
    """REPAIR-CTX (t_97b31f81): the batch prompt carries ONLY the findings
    PIDs plus their ±3 neighbours (CONTEXT_ONLY) — NOT the full chapter maps.
    Far PIDs must be absent; the repairable PID and its neighbourhood must be
    present; neighbours are named in a CONTEXT_ONLY block."""
    source, translation = _chapter_maps(400)
    findings = [
        EligibleFinding(index=1, pid="p00003", tier="B", category="changed_fact",
                        severity="major", confidence="high", note="n3",
                        excerpt="ex3", issue={}),
        EligibleFinding(index=2, pid="p00100", tier="B", category="changed_fact",
                        severity="major", confidence="high", note="n100",
                        excerpt="ex100", issue={}),
    ]
    prompt = render_selective_repair_prompt(
        chapter_id="0001", source=source, translation=translation,
        findings=findings,
    )
    # repairable PIDs + their ±3 neighbourhood are present
    for pid in ("p00003", "p00000", "p00006", "p00100", "p00097", "p00103"):
        assert f"  {pid}: " in prompt, f"{pid} missing from local context"
    # far PIDs are NOT visible
    for pid in ("p00200", "p00399", "p00010"):
        assert f"  {pid}: " not in prompt, f"{pid} outside window leaked in"
    # neighbours are named as CONTEXT_ONLY, never repairable
    assert "CONTEXT_ONLY" in prompt
    assert "NEVER propose an edit for a CONTEXT_ONLY pid" in prompt
    assert "p00003" in prompt.split("CONTEXT_ONLY")[0]
    # FINDINGS block keeps the [index] contract
    assert "[1] p00003 | CANDIDATE | changed_fact" in prompt
    assert "[2] p00100 | CANDIDATE | changed_fact" in prompt


def test_repair_prompt_local_context_window_configurable():
    """REPAIR-CTX: the ±N window is configurable (window=0 -> findings only;
    window=1 -> one neighbour on each side)."""
    source, translation = _chapter_maps(20)
    findings = [
        EligibleFinding(index=1, pid="p00010", tier="B", category="changed_fact",
                        severity="major", confidence="high", note="n",
                        excerpt="e", issue={}),
    ]
    narrow = render_selective_repair_prompt(
        chapter_id="0001", source=source, translation=translation,
        findings=findings, repair_context_window=0,
    )
    assert "  p00010: " in narrow
    assert "  p00009: " not in narrow and "  p00011: " not in narrow
    wide = render_selective_repair_prompt(
        chapter_id="0001", source=source, translation=translation,
        findings=findings, repair_context_window=1,
    )
    assert "  p00009: " in wide and "  p00011: " in wide
    assert "  p00008: " not in wide and "  p00012: " not in wide


def test_repair_prompt_category_window_covers_far_gender_referent():
    """REPAIR-2 (t_768537b9, run_013 p00193 regression, acceptance): a
    finding whose category is in the wide-window map (invented_gender /
    referent / omission) renders with a BIGGER window so the FAR referent is
    covered. run_013's p00193 (index 192) needed the female referent at
    p00200 (index 199 — 7 PIDs away, outside ±3) to keep «внучка»; the ±3
    window hid it and the repair wrongly wrote «внук»."""
    from pact_v4.runtime.prompts_runtime import (
        DEFAULT_REPAIR_CONTEXT_WINDOW_BY_CATEGORY,
    )
    assert DEFAULT_REPAIR_CONTEXT_WINDOW_BY_CATEGORY["invented_gender"] >= 7
    source, translation = _chapter_maps(400)
    finding = EligibleFinding(
        index=1, pid="p00193", tier="B", category="invented_gender",
        severity="minor", confidence="high",
        note="gender-neutral 'grandchild' prematurely female", excerpt="внучка",
        issue={},
    )
    # default (category map active): referent 7 PIDs away IS covered
    wide = render_selective_repair_prompt(
        chapter_id="0001", source=source, translation=translation,
        findings=[finding],
    )
    assert "  p00193: " in wide
    assert "  p00200: " in wide, "wide category window must cover the far referent"
    # narrow legacy window (window=3, no category map): referent NOT covered
    narrow = render_selective_repair_prompt(
        chapter_id="0001", source=source, translation=translation,
        findings=[finding], repair_context_window=3,
        repair_context_window_by_category={},
    )
    assert "  p00193: " in narrow
    assert "  p00200: " not in narrow, "±3 window must NOT cover the far referent"


def test_repair_prompt_changed_fact_window_stays_narrow():
    """REPAIR-2 (t_768537b9, acceptance): changed_fact/addition are NOT in
    the wide-window map — their window stays ±3 (a local edit), even with
    the per-category map active."""
    from pact_v4.runtime.prompts_runtime import (
        DEFAULT_REPAIR_CONTEXT_WINDOW_BY_CATEGORY,
    )
    assert "changed_fact" not in DEFAULT_REPAIR_CONTEXT_WINDOW_BY_CATEGORY
    assert "addition" not in DEFAULT_REPAIR_CONTEXT_WINDOW_BY_CATEGORY
    source, translation = _chapter_maps(400)
    finding = EligibleFinding(
        index=1, pid="p00193", tier="B", category="changed_fact",
        severity="major", confidence="high", note="n", excerpt="e", issue={},
    )
    prompt = render_selective_repair_prompt(
        chapter_id="0001", source=source, translation=translation,
        findings=[finding],
    )
    # p00193 is index 192; ±3 covers 189..195, p00200 (index 199) stays out
    assert "  p00193: " in prompt
    assert "  p00196: " in prompt
    assert "  p00200: " not in prompt, "changed_fact must keep the ±3 window"


def test_repair_prompt_local_context_fails_loud_on_missing_pid():
    """REPAIR-CTX fail-loud: a repairable PID absent from the maps raises
    ValueError — the model cannot repair a PID it cannot see."""
    source, translation = _chapter_maps(20)
    findings = [
        EligibleFinding(index=1, pid="p00400", tier="B", category="changed_fact",
                        severity="major", confidence="high", note="n",
                        excerpt="e", issue={}),
    ]
    with pytest.raises(ValueError, match="p00400"):
        render_selective_repair_prompt(
            chapter_id="0001", source=source, translation=translation,
            findings=findings,
        )
    # a finding PID present in source but missing from translation is ALSO
    # fail-loud (the model needs the current Russian text to repair it)
    missing_tr = [
        EligibleFinding(index=1, pid="p00003", tier="B", category="changed_fact",
                        severity="major", confidence="high", note="n",
                        excerpt="e", issue={}),
    ]
    bad_tr = dict(source)
    bad_tr.pop("p00003")
    with pytest.raises(ValueError, match="p00003"):
        render_selective_repair_prompt(
            chapter_id="0001", source=source, translation=bad_tr,
            findings=list(missing_tr),
        )


def test_repair_prompt_local_context_token_budget():
    """REPAIR-CTX acceptance: a 400-PID chapter batch with 3-4 findings and
    ±3 window must estimate ≤ ~5k input tokens (was ~33.7k on run_012)."""
    from pact_v4.audit.chunked_audit import text_token_estimate
    source, translation = _chapter_maps(400)
    findings = [
        EligibleFinding(index=i, pid=pid, tier="B", category="changed_fact",
                        severity="major", confidence="high", note=f"n{i}",
                        excerpt=f"e{i}", issue={})
        for i, pid in enumerate(("p00003", "p00100", "p00250", "p00390"), start=1)
    ]
    prompt = render_selective_repair_prompt(
        chapter_id="0001", source=source, translation=translation,
        findings=findings,
    )
    assert text_token_estimate(prompt) <= 5000, \
        f"repair batch input too large: {text_token_estimate(prompt):.0f} tokens"


def test_repair_prompt_run012_batch1_replay_local_context():
    """REPAIR-CTX regression (acceptance): the SAME findings as run_012
    batch1 (p00003, p00010, p00014, p00029 — changed_fact, major, high)
    rendered with the local-context prompt must keep the [index] contract and
    parse to the SAME decisions the model made on the full-chapter prompt
    (repair p00003/p00010/p00014, pass p00029). Far PIDs must be absent."""
    source, translation = _chapter_maps(400)
    findings = [
        EligibleFinding(index=1, pid="p00003", tier="B", category="changed_fact",
                        severity="major", confidence="high",
                        note="changed_fact p00003", excerpt="ex3", issue={}),
        EligibleFinding(index=2, pid="p00010", tier="B", category="changed_fact",
                        severity="major", confidence="high",
                        note="changed_fact p00010", excerpt="ex10", issue={}),
        EligibleFinding(index=3, pid="p00014", tier="B", category="changed_fact",
                        severity="major", confidence="high",
                        note="changed_fact p00014", excerpt="ex14", issue={}),
        EligibleFinding(index=4, pid="p00029", tier="B", category="changed_fact",
                        severity="major", confidence="high",
                        note="changed_fact p00029", excerpt="ex29", issue={}),
    ]
    prompt = render_selective_repair_prompt(
        chapter_id="0001", source=source, translation=translation,
        findings=findings,
    )
    # local context: every repairable PID + its ±3 neighbourhood present
    for pid in ("p00003", "p00010", "p00014", "p00029"):
        assert f"  {pid}: " in prompt
    for pid in ("p00000", "p00006", "p00007", "p00013", "p00011", "p00017",
                "p00026", "p00032"):
        assert f"  {pid}: " in prompt
    # far PIDs (as far from the findings as the old full-chapter prompt
    # would have shown) are NOT visible
    for pid in ("p00200", "p00399", "p00050"):
        assert f"  {pid}: " not in prompt
    assert "CONTEXT_ONLY" in prompt
    for pid in ("[1] p00003 |", "[2] p00010 |", "[3] p00014 |", "[4] p00029 |"):
        assert pid in prompt

    # Replay the run_012 batch1 model decisions against the SAME findings:
    # the [index] contract is unchanged, so the same response parses to the
    # same committed/passed PIDs (equivalent results on the raw).
    raw = json.dumps({
        "results": [
            {"index": 1, "decision": "repair", "pid": "p00003",
             "repaired_translation": translation["p00003"] + " Исправлено.",
             "reason": "r1"},
            {"index": 2, "decision": "repair", "pid": "p00010",
             "repaired_translation": translation["p00010"] + " Исправлено.",
             "reason": "r2"},
            {"index": 3, "decision": "repair", "pid": "p00014",
             "repaired_translation": translation["p00014"] + " Исправлено.",
             "reason": "r3"},
            {"index": 4, "decision": "pass", "reason": "fp"},
        ]
    }, ensure_ascii=False)
    results, errors, _ = parse_repair_batch(raw, findings, current_by_pid=translation)
    assert not errors
    decisions = {r.index: r.decision for r in results}
    assert decisions == {1: "repair", 2: "repair", 3: "repair", 4: "pass"}
    repaired = {r.pid for r in results if r.decision == "repair"}
    assert repaired == {"p00003", "p00010", "p00014"}


def test_repair_prompt_run013_batch1_noop_replay():
    """REPAIR-2 (t_768537b9, acceptance): replay run_013 batch1 — the model
    answered index 1 (p00016) with a NO-OP repair (repaired_translation ==
    current, a contract violation in v2). Under the new contract index 1 is
    converted to a per-index PASS, the other 3 indices are repaired normally
    (p00033/p00035/p00080), and the batch is NOT failed — the 4 findings are
    not lost to debt (run_013: the whole batch failed and all 4 went to debt).
    """
    source, translation = _chapter_maps(400)
    findings = [
        EligibleFinding(index=1, pid="p00016", tier="B", category="changed_fact",
                        severity="major", confidence="high",
                        note="changed_fact p00016", excerpt="ex16", issue={}),
        EligibleFinding(index=2, pid="p00033", tier="B", category="changed_fact",
                        severity="major", confidence="high",
                        note="changed_fact p00033", excerpt="ex33", issue={}),
        EligibleFinding(index=3, pid="p00035", tier="B", category="changed_fact",
                        severity="major", confidence="high",
                        note="changed_fact p00035", excerpt="ex35", issue={}),
        EligibleFinding(index=4, pid="p00080", tier="B", category="changed_fact",
                        severity="major", confidence="high",
                        note="changed_fact p00080", excerpt="ex80", issue={}),
    ]
    current = {
        "p00016": "Мои мимолётные впечатления от дома быстро развеялись.",
        "p00033": "Пэйдж выглядела так, будто хотела подойти ко мне.",
        "p00035": "У тёти Ирэн тоже были дети, но я видел только двоих.",
        "p00080": "Она протянула руки для объятий, и я вздрогнул.",
    }
    raw = json.dumps({
        "results": [
            # index 1: NO-OP — the model returned the current text unchanged
            {"index": 1, "decision": "repair", "pid": "p00016",
             "repaired_translation": current["p00016"], "reason": "no-op"},
            {"index": 2, "decision": "repair", "pid": "p00033",
             "repaired_translation": current["p00033"] + " Исправлено.",
             "reason": "r2"},
            {"index": 3, "decision": "repair", "pid": "p00035",
             "repaired_translation": current["p00035"] + " Исправлено.",
             "reason": "r3"},
            {"index": 4, "decision": "repair", "pid": "p00080",
             "repaired_translation": current["p00080"] + " Исправлено.",
             "reason": "r4"},
        ]
    }, ensure_ascii=False)
    results, errors, warnings = parse_repair_batch(
        raw, findings, current_by_pid=current
    )
    assert not errors, "the no-op index must NOT fail the whole batch"
    decisions = {r.index: r.decision for r in results}
    assert decisions == {1: "pass", 2: "repair", 3: "repair", 4: "repair"}
    assert results[0].reason == "no-op repair converted to pass"
    repaired = {r.pid for r in results if r.decision == "repair"}
    assert repaired == {"p00033", "p00035", "p00080"}
    # p00016 is NOT lost — it is an explicit per-index pass with a warning
    assert any("no-op repair converted to pass" in w for w in warnings)
    assert "p00016" in warnings[0]


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


def test_reaudit_prompt_renders_repaired_changes_delta():
    """REPAIR-CTX (t_97b31f81): the re-audit prompt carries a REPAIRED
    CHANGES block {pid, before, after} so the auditor verifies the repair
    correctness instead of just re-reading the text."""
    from pact_v4.audit.chunked_audit import AuditPair
    from pact_v4.repair.selective_repair import RepairedChange
    audit = [AuditPair(pid="p00010", source="A wannabe-architect.",
                       translation="Парнем, мечтавшим стать архитектором.")]
    prompt = render_reaudit_prompt(
        chapter_id="0001", audit_pairs=audit,
        repaired_changes=[RepairedChange(
            pid="p00010",
            before="Девушкой, мечтавшей стать архитектором.",
            after="Парнем, мечтавшим стать архитектором.",
        )],
    )
    assert "REPAIRED CHANGES" in prompt
    assert "p00010" in prompt.split("REPAIRED CHANGES")[1]
    assert "before:" in prompt and "after:" in prompt
    assert "Девушкой" in prompt and "Парнем" in prompt


def test_reaudit_prompt_chunk_header_when_chunked():
    """REPAIR-CTX: a chunked re-audit labels each chunk (chunk X of Y) so a
    multi-chunk re-audit is unambiguous; a single chunk keeps the plain
    header."""
    from pact_v4.audit.chunked_audit import AuditPair
    pairs = [AuditPair(pid=f"p{i:05d}", source=f"S{i}", translation=f"T{i}")
             for i in range(1, 4)]
    multi = render_reaudit_prompt(
        chapter_id="0001", audit_pairs=pairs, chunk_index=2, chunk_total=3,
    )
    assert "RE-AUDIT PAIRS (chunk 2 of 3, changed PIDs + neighbours):" in multi
    single = render_reaudit_prompt(chapter_id="0001", audit_pairs=pairs)
    assert "RE-AUDIT PAIRS (changed PIDs + neighbours):" in single
    assert "chunk 1 of 1" not in single


def test_reaudit_prompt_overlap_context_only_block():
    """REPAIR-CTX: CONTEXT_ONLY now means the chunk's preceding overlap
    pairs (the audit's get_overlap_context mechanism) — NOT the whole
    chapter."""
    from pact_v4.audit.chunked_audit import AuditPair
    pairs = [AuditPair(pid=f"p{i:05d}", source=f"S{i}", translation=f"T{i}")
             for i in range(1, 6)]
    prompt = render_reaudit_prompt(chapter_id="0001", audit_pairs=pairs)
    # no CONTEXT_ONLY block when there is no overlap (the frozen template's
    # own wording about CONTEXT_ONLY is fine; the block header must be
    # absent)
    assert "CONTEXT_ONLY (preceding overlap pairs" not in prompt
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
# V4.2 R: REVIEW-candidate integration (card t_4707e6e5, R2)
# ---------------------------------------------------------------------------


def test_review_candidates_accepted_and_rejected_journal():
    """The verifier accepts/rejects each Russian-editor REVIEW candidate
    against the ORIGINAL: ласка→выдра REJECT (no source confirmation),
    ты прав→ты права ACCEPT. Accepted -> committed + re-audit; rejected ->
    pass; the journal records both verdicts."""
    from pact_v4.audit.russian_editor import ReviewCandidate

    source = {
        "p00106": "— Ten, — I corrected.",
        "p00240": "\"You're right,\" she said.",
    }
    translation = {
        "p00106": "— Десять, — поправил я.",
        "p00240": "— Ты прав, — сказала она.",
    }
    review_candidates = [
        ReviewCandidate(
            pid="p00106",
            original="— Десять, — поправил я.",
            proposed="— Выдра, — поправил я.",
            klass="logic",
            reason="заменил число без подтверждения (ложная правка)",
        ),
        ReviewCandidate(
            pid="p00240",
            original="— Ты прав, — сказала она.",
            proposed="— Ты права, — сказала она.",
            klass="grammar",
            reason="род адресата (женский)",
        ),
    ]
    backend = ScriptedRepairBackend([
        _repair_response([
            {"index": 1, "decision": "pass",
             "reason": "источник говорит Ten (10), не выдра — отклонено"},
            {"index": 2, "decision": "repair", "pid": "p00240",
             "repaired_translation": "— Ты права, — сказала она.",
             "reason": "адресат женского рода — принято"},
        ]),
        _reaudit_response([]),  # single re-audit after the accepted commit
    ])
    evaluator = SelectiveRepairEvaluator(
        backend, config=SelectiveRepairConfig(findings_cap=10)
    )
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=(), review_candidates=review_candidates,
    )
    assert outcome.eligible_count == 0  # no audit findings
    assert outcome.skipped is False     # review candidates still repaired
    # Accepted candidate committed -> re-audit ran (1 repair + 1 reaudit);
    # the rejected candidate answered PASS -> passed_pids.
    assert dict(outcome.committed) == {
        "p00240": "— Ты права, — сказала она.",
    }
    assert outcome.passed_pids == ("p00106",)
    assert len(backend.requests) == 2
    # Journal: one entry per candidate with the accept/reject verdict.
    journal = {entry["pid"]: entry for entry in outcome.review_journal}
    assert set(journal) == {"p00106", "p00240"}
    assert journal["p00106"]["verdict"] == "rejected"
    assert journal["p00106"]["class"] == "logic"
    assert journal["p00106"]["original"] == "— Десять, — поправил я."
    assert journal["p00106"]["proposed"] == "— Выдра, — поправил я."
    assert "не выдра" in journal["p00106"]["reason"]
    assert journal["p00240"]["verdict"] == "accepted"
    assert journal["p00240"]["committed_text"] == "— Ты права, — сказала она."
    assert outcome.repair_complete is True


def test_review_candidates_never_displace_audit_findings_at_cap():
    """Review candidates ride along AFTER the audit findings cap — they
    never displace code-confirmed audit findings at the cap boundary.
    CANDIDATE-MERGE (t_0ffe56e1): the p00001/p00002 candidates MERGE with
    the audit findings on the same PIDs (one finding, both sources), so the
    post-merge kept set is 10 findings -> microbatches [4, 3, 3]."""
    from pact_v4.audit.russian_editor import ReviewCandidate

    # REPAIR-CTX (t_97b31f81): the renderer fails loud if a finding PID is
    # missing from the maps (the model cannot repair a PID it cannot see),
    # so the fixture maps must cover every finding PID — as they always do
    # in production (findings come from the audit of the chapter's PIDs).
    source = {f"p{i:05d}": f"source {i}" for i in range(1, 13)}
    translation = {f"p{i:05d}": f"перевод {i}" for i in range(1, 13)}
    # 12 CONFIRMED audit findings + 2 review candidates; cap = 10 ->
    # 10 audit findings kept (the cap), the 2 review candidates are ADDED
    # (never capped away), 2 audit findings capped.
    issues = [
        _issue(f"p{i:05d}", "changed_fact", note="n", confidence="high")
        for i in range(1, 13)
    ]
    filtered = [
        FilteredIssue(issue=iss, verdict=CONFIRMED, filter_name="test", reason="t")
        for iss in issues
    ]
    review_candidates = [
        ReviewCandidate(pid="p00001", original="перевод 1",
                        proposed="перевод один", klass="typo", reason="r"),
        ReviewCandidate(pid="p00002", original="перевод 2",
                        proposed="перевод два", klass="grammar", reason="r"),
    ]
    backend = ScriptedRepairBackend([
        _repair_response([{"index": i, "decision": "pass", "reason": "ok"}
                          for i in range(1, 5)]),
        _repair_response([{"index": i, "decision": "pass", "reason": "ok"}
                          for i in range(1, 4)]),
        _repair_response([{"index": i, "decision": "pass", "reason": "ok"}
                          for i in range(1, 4)]),
    ])
    evaluator = SelectiveRepairEvaluator(
        backend, config=SelectiveRepairConfig(findings_cap=10)
    )
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered, review_candidates=review_candidates,
    )
    # 12 eligible audit findings -> cap 10 keeps 10, caps 2.
    assert len(outcome.capped) == 2
    # 10 kept audit findings, p00001/p00002 merge with their review
    # candidates -> 10 findings (8 audit + 2 merged) -> microbatches 4+3+3.
    assert [len(b.findings) for b in outcome.batches] == [4, 3, 3]
    # the merged p00001/p00002 findings carry both sources
    merged_stages = {
        b.findings[0].source_stage
        for b in outcome.batches
        for f in b.findings
        if f.pid in ("p00001", "p00002")
    }
    assert merged_stages == {"fidelity_auditor+russian_editor"}
    # Every review candidate was answered (journal entries for both).
    assert len(outcome.review_journal) == 2
    assert outcome.repair_complete is True


def test_review_candidate_failed_batch_journal_failed():
    """A failed batch marks its review candidates 'failed' (fail-closed:
    never silently accepted)."""
    from pact_v4.audit.russian_editor import ReviewCandidate

    source = {"p00001": "source 1"}
    translation = {"p00001": "перевод 1"}
    review_candidates = [
        ReviewCandidate(pid="p00001", original="перевод 1",
                        proposed="перевод один", klass="typo", reason="r"),
    ]
    backend = ScriptedRepairBackend([_repair_response([])])  # empty -> FAILED
    evaluator = SelectiveRepairEvaluator(backend)
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=(), review_candidates=review_candidates,
    )
    assert outcome.repair_complete is False
    assert outcome.review_journal[0]["verdict"] == "failed"
    assert outcome.committed == ()


def test_review_candidates_zero_audit_findings_tear_not_skipped():
    """TEaR (0 eligible audit findings) still repairs review candidates —
    the skip is only when BOTH audit findings and review candidates are
    empty."""
    from pact_v4.audit.russian_editor import ReviewCandidate

    source = {"p00001": "source 1"}
    translation = {"p00001": "перевод 1"}
    review_candidates = [
        ReviewCandidate(pid="p00001", original="перевод 1",
                        proposed="перевод один", klass="typo", reason="r"),
    ]
    backend = ScriptedRepairBackend([
        _repair_response([{"index": 1, "decision": "repair", "pid": "p00001",
                           "repaired_translation": "перевод один",
                           "reason": "принято"}]),
        _reaudit_response([]),
    ])
    evaluator = SelectiveRepairEvaluator(backend)
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=(), review_candidates=review_candidates,
    )
    assert outcome.skipped is False
    assert dict(outcome.committed) == {"p00001": "перевод один"}
    assert outcome.review_journal[0]["verdict"] == "accepted"
    # Accepted commit -> re-audit ran.
    assert len(backend.requests) == 2


def test_review_candidates_import_contract():
    """The review-candidate type is the audit module's (no new repair-side
    transport import)."""
    import pact_v4.audit.russian_editor as russian_editor

    assert russian_editor.ReviewCandidate.__name__ == "ReviewCandidate"
    assert "proposed" in russian_editor.ReviewCandidate.__dataclass_fields__


# ---------------------------------------------------------------------------
# CANDIDATE-MERGE (t_0ffe56e1): source_stage in candidates + PID merging
# ---------------------------------------------------------------------------


def test_select_eligible_sets_source_stage_fidelity_auditor():
    """CANDIDATE-MERGE: audit findings carry ``source_stage=fidelity_auditor``
    (the repair prompt renders it so the verifier knows the remark claims a
    SOURCE mismatch, not a Russian defect)."""
    eligible, _, _ = select_eligible(
        [_issue_with("p00240", category="changed_fact", _verdict=CONFIRMED)]
    )
    assert eligible[0].source_stage == "fidelity_auditor"


def test_review_candidate_source_stage_russian_editor():
    """CANDIDATE-MERGE: a Russian-editor REVIEW candidate carries
    ``source_stage=russian_editor`` (the prompt tells the verifier this is a
    Russian-defect hypothesis)."""
    from pact_v4.audit.russian_editor import ReviewCandidate

    candidate = ReviewCandidate(
        pid="p00303", original="Он сделал себе обещание.",
        proposed="Он пообещал себе.", klass="calque", reason="калька",
    )
    assert candidate.source_stage == "russian_editor"


def test_merge_candidates_by_pid_two_auditors_one_editor_all_merged():
    """CANDIDATE-MERGE (owner clarification 2026-08-13): a PID with TWO
    same-stage auditor findings PLUS one editor candidate merges ALL THREE
    remarks into ONE finding — ``[A1, A2, E] -> [merged(A1+A2+E)]`` with all
    three remarks in ``sources``, one index, one decision (never two blocks
    that force partial decisions)."""
    a1 = EligibleFinding(
        index=1, pid="p00303", tier="B", category="changed_fact",
        severity="major", confidence="high",
        note="fact shifted", excerpt="excerpt1", issue={"id": "p00303"},
        source_stage="fidelity_auditor",
    )
    a2 = EligibleFinding(
        index=2, pid="p00303", tier="B", category="omission",
        severity="minor", confidence="high",
        note="missing clause", excerpt="excerpt2", issue={"id": "p00303"},
        source_stage="fidelity_auditor",
    )
    e = EligibleFinding(
        index=3, pid="p00303", tier="B", category="calque",
        severity="minor", confidence="high",
        note="калька с английского", excerpt="proposed rewrite",
        issue={"id": "p00303", "source": "russian_editor"},
        source_stage="russian_editor",
    )
    merged = merge_candidates_by_pid((a1, a2, e))
    # ONE finding with ALL THREE remarks — same-stage included.
    assert len(merged) == 1
    f = merged[0]
    assert f.pid == "p00303"
    assert f.source_stage == "fidelity_auditor+russian_editor"
    assert len(f.sources) == 3
    assert {s["stage"] for s in f.sources} == {
        "fidelity_auditor", "russian_editor",
    }
    assert {s["category"] for s in f.sources} == {
        "changed_fact", "omission", "calque",
    }
    # the headline values come from the first finding
    assert f.category == "changed_fact"
    assert f.index == 1
    assert [f.index for f in merged] == [1]


def test_merge_candidates_by_pid_one_auditor_two_editors_all_merged():
    """CANDIDATE-MERGE symmetric multiplicity (owner clarification
    2026-08-13): one auditor finding + TWO editor candidates on the same PID
    merge into ONE finding with all three remarks (one decision for the
    whole PID)."""
    a = EligibleFinding(
        index=1, pid="p00303", tier="B", category="changed_fact",
        severity="major", confidence="high",
        note="fact shifted", excerpt="excerpt", issue={"id": "p00303"},
        source_stage="fidelity_auditor",
    )
    e1 = EligibleFinding(
        index=2, pid="p00303", tier="B", category="calque",
        severity="minor", confidence="high",
        note="калька", excerpt="proposed1", issue={"id": "p00303"},
        source_stage="russian_editor",
    )
    e2 = EligibleFinding(
        index=3, pid="p00303", tier="B", category="grammar",
        severity="minor", confidence="high",
        note="грамматика", excerpt="proposed2", issue={"id": "p00303"},
        source_stage="russian_editor",
    )
    merged = merge_candidates_by_pid((a, e1, e2))
    assert len(merged) == 1
    f = merged[0]
    assert f.source_stage == "fidelity_auditor+russian_editor"
    assert len(f.sources) == 3
    assert {s["category"] for s in f.sources} == {
        "changed_fact", "calque", "grammar",
    }
    assert [f.index for f in merged] == [1]


def test_merge_candidates_by_pid_two_auditors_two_editors_all_merged():
    """CANDIDATE-MERGE full multiplicity (owner clarification 2026-08-13):
    TWO auditor findings + TWO editor candidates on one PID -> ONE merged
    finding with all four remarks (one index, one decision)."""
    def mk(idx, stage, cat):
        return EligibleFinding(
            index=idx, pid="p00303", tier="B", category=cat,
            severity="minor", confidence="high", note=f"n{idx}",
            excerpt=f"e{idx}", issue={"id": "p00303"}, source_stage=stage,
        )
    merged = merge_candidates_by_pid((
        mk(1, "fidelity_auditor", "changed_fact"),
        mk(2, "fidelity_auditor", "omission"),
        mk(3, "russian_editor", "calque"),
        mk(4, "russian_editor", "grammar"),
    ))
    assert len(merged) == 1
    f = merged[0]
    assert f.source_stage == "fidelity_auditor+russian_editor"
    assert len(f.sources) == 4
    assert {s["category"] for s in f.sources} == {
        "changed_fact", "omission", "calque", "grammar",
    }
    assert [f.index for f in merged] == [1]


def test_merge_candidates_by_pid_same_pid_editor_auditor_merged():
    """CANDIDATE-MERGE regression (acceptance, p00303-class): one PID with
    BOTH an editor candidate (calque) and an auditor changed_fact finding
    becomes ONE EligibleFinding whose ``source_stage`` joins both stages and
    whose ``sources`` carries both remarks — one repair call sees both."""
    from pact_v4.audit.russian_editor import ReviewCandidate

    audit = EligibleFinding(
        index=1, pid="p00303", tier="B", category="changed_fact",
        severity="major", confidence="high",
        note="translation changed the fact", excerpt="excerpt", issue={},
        source_stage="fidelity_auditor",
    )
    editor = EligibleFinding(
        index=2, pid="p00303", tier="B", category="calque",
        severity="minor", confidence="high",
        note="калька с английского", excerpt="proposed rewrite", issue={},
        source_stage="russian_editor",
    )
    merged = merge_candidates_by_pid((audit, editor))
    assert len(merged) == 1
    f = merged[0]
    assert f.pid == "p00303"
    assert f.source_stage == "fidelity_auditor+russian_editor"
    assert len(f.sources) == 2
    stages = {s["stage"] for s in f.sources}
    assert stages == {"fidelity_auditor", "russian_editor"}
    # indices are unique (single finding keeps index 1)
    assert f.index == 1


def test_merge_candidates_by_pid_same_stage_merged_too():
    """CANDIDATE-MERGE (owner clarification 2026-08-13): same-stage multiple
    findings of one PID (e.g. two audit findings on one paragraph) ALSO
    merge into ONE finding — the repair model must see all remarks of the
    pid in one block, regardless of stage."""
    a = EligibleFinding(
        index=1, pid="p00193", tier="B", category="invented_gender",
        severity="minor", confidence="high", note="n1", excerpt="e1", issue={},
        source_stage="fidelity_auditor",
    )
    b = EligibleFinding(
        index=2, pid="p00193", tier="B", category="omission",
        severity="minor", confidence="high", note="n2", excerpt="e2", issue={},
        source_stage="fidelity_auditor",
    )
    merged = merge_candidates_by_pid((a, b))
    assert len(merged) == 1
    f = merged[0]
    assert f.pid == "p00193"
    assert f.source_stage == "fidelity_auditor"  # single stage, no join
    assert len(f.sources) == 2
    assert {s["category"] for s in f.sources} == {"invented_gender", "omission"}
    assert f.index == 1


def test_merge_candidates_by_pid_distinct_pids_untouched():
    """CANDIDATE-MERGE: findings on different PIDs are untouched (each keeps
    its own index and stage)."""
    a = EligibleFinding(
        index=1, pid="p00106", tier="B", category="logic",
        severity="minor", confidence="high", note="n", excerpt="e", issue={},
        source_stage="russian_editor",
    )
    b = EligibleFinding(
        index=2, pid="p00240", tier="A", category="changed_fact",
        severity="major", confidence="high", note="n", excerpt="e", issue={},
        source_stage="fidelity_auditor",
    )
    merged = merge_candidates_by_pid((a, b))
    assert len(merged) == 2
    assert [f.pid for f in merged] == ["p00106", "p00240"]
    assert [f.source_stage for f in merged] == ["russian_editor", "fidelity_auditor"]


def test_repair_prompt_renders_source_stage_per_finding():
    """ACCEPTANCE: the repair prompt shows ``source=<stage>`` for every
    candidate (fidelity_auditor / russian_editor)."""
    findings = [
        EligibleFinding(
            index=1, pid="p00193", tier="B", category="invented_gender",
            severity="minor", confidence="high", note="grandchild",
            excerpt="внучка", issue={}, source_stage="fidelity_auditor",
        ),
        EligibleFinding(
            index=2, pid="p00303", tier="B", category="calque",
            severity="minor", confidence="high", note="калька",
            excerpt="пообещал", issue={}, source_stage="russian_editor",
        ),
    ]
    prompt = render_selective_repair_prompt(
        chapter_id="0001",
        source={"p00193": "grandchild", "p00303": "made a promise"},
        translation={"p00193": "внучка", "p00303": "сделал обещание"},
        findings=findings,
    )
    assert "source=fidelity_auditor" in prompt
    assert "source=russian_editor" in prompt
    assert "[1] p00193 | CANDIDATE | invented_gender" in prompt
    assert "[2] p00303 | CANDIDATE | calque" in prompt


def test_repair_prompt_merged_finding_shows_both_remarks():
    """ACCEPTANCE: a merged finding (editor + auditor on one PID) renders
    BOTH remarks with their stages in the FINDINGS block — the model sees
    both in one call and builds one decision."""
    merged = EligibleFinding(
        index=1, pid="p00303", tier="B", category="changed_fact",
        severity="major", confidence="high",
        note="audit note", excerpt="audit excerpt", issue={},
        source_stage="fidelity_auditor+russian_editor",
        sources=(
            {
                "stage": "fidelity_auditor", "tier": "B",
                "category": "changed_fact", "severity": "major",
                "confidence": "high", "note": "audit note",
                "excerpt": "audit excerpt", "issue": {},
            },
            {
                "stage": "russian_editor", "tier": "B",
                "category": "calque", "severity": "minor",
                "confidence": "high", "note": "калька",
                "excerpt": "пообещал", "issue": {},
            },
        ),
    )
    prompt = render_selective_repair_prompt(
        chapter_id="0001",
        source={"p00303": "made a promise to himself"},
        translation={"p00303": "сделал себе обещание"},
        findings=[merged],
    )
    assert "source=fidelity_auditor+russian_editor" in prompt
    assert "[fidelity_auditor |" in prompt and "changed_fact" in prompt
    assert "[russian_editor |" in prompt and "calque" in prompt
    assert "audit note" in prompt and "калька" in prompt


def test_repair_instructions_mention_source_difference():
    """ACCEPTANCE: REPAIR_AS_VERIFIER_V1 (v4) tells the verifier the two
    source kinds differ — editor: Russian defect; auditor: source mismatch —
    and that a merged finding is one decision, never sequential rewrites."""
    instructions = REPAIR_AS_VERIFIER_V1.instructions
    assert "source=fidelity_auditor" in instructions
    assert "source=russian_editor" in instructions
    assert "source=fidelity_auditor+russian_editor" in instructions
    assert "Never apply two sequential rewrites to the same pid" in instructions
    assert REPAIR_AS_VERIFIER_V1.version == "pact-v4-repair-as-verifier/v4"


def test_merged_editor_auditor_single_repair_call():
    """CANDIDATE-MERGE regression (acceptance, p00303-class): one PID with
    editor calque + auditor changed_fact is ONE repair call with BOTH remarks
    visible; the model builds ONE decision — committed once, re-audit covers
    the PID (never two sequential rewrites)."""
    from pact_v4.audit.russian_editor import ReviewCandidate

    source = {
        "p00303": "He made a solemn promise to himself.",
        "p00304": "Next paragraph.",
    }
    translation = {
        "p00303": "Он сделал торжественное обещание себе.",
        "p00304": "Следующий абзац.",
    }
    filtered = [
        FilteredIssue(
            issue=_issue("p00303", "changed_fact", note="fact shifted",
                         excerpt="торжественное обещание"),
            verdict=TIER_B, filter_name="test", reason="test",
        )
    ]
    review_candidates = [
        ReviewCandidate(
            pid="p00303",
            original="Он сделал торжественное обещание себе.",
            proposed="Он торжественно пообещал себе.",
            klass="calque", reason="калька с английского",
        ),
    ]
    backend = ScriptedRepairBackend([
        _repair_response([
            {
                "index": 1, "decision": "repair", "pid": "p00303",
                "repaired_translation": (
                    "Он дал себе торжественное обещание."
                ),
                "reason": "fixed the calque and kept the fact",
            },
        ]),
        _reaudit_response([]),
    ])
    evaluator = SelectiveRepairEvaluator(
        backend, config=SelectiveRepairConfig(findings_cap=10)
    )
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered, review_candidates=review_candidates,
    )
    # ONE repair call (repair + re-audit), one batch, one finding for p00303
    assert len(backend.requests) == 2
    assert [len(b.findings) for b in outcome.batches] == [1]
    finding = outcome.batches[0].findings[0]
    assert finding.pid == "p00303"
    assert finding.source_stage == "fidelity_auditor+russian_editor"
    assert len(finding.sources) == 2
    # the single call's prompt shows BOTH remarks
    prompt = backend.requests[0].messages[0].content
    assert "source=fidelity_auditor+russian_editor" in prompt
    assert "changed_fact" in prompt and "calque" in prompt
    # one decision, committed once
    assert dict(outcome.committed) == {
        "p00303": "Он дал себе торжественное обещание."
    }
    assert outcome.review_journal[0]["verdict"] == "accepted"
    assert outcome.repair_complete is True


def test_mixed_multiplicity_two_auditors_one_editor_one_index():
    """CANDIDATE-MERGE regression (owner clarification 2026-08-13), end-to-
    end: a PID with TWO auditor findings PLUS one editor candidate produces
    ONE per-index repair finding — ALL THREE remarks merged into one block
    (every remark visible in one prompt block, ONE decision), never two
    indices that would force a partial decision. Unique indices, correct
    commit behavior, and the review journal maps the editor candidate to the
    single merged finding's verdict."""
    from pact_v4.audit.russian_editor import ReviewCandidate

    source = {
        "p00303": "He made a solemn promise to himself.",
        "p00304": "Next paragraph.",
    }
    translation = {
        "p00303": "Он сделал торжественное обещание себе.",
        "p00304": "Следующий абзац.",
    }
    filtered = [
        FilteredIssue(
            issue=_issue("p00303", "changed_fact", note="fact shifted",
                         excerpt="торжественное обещание"),
            verdict=TIER_B, filter_name="test", reason="test",
        ),
        FilteredIssue(
            issue=_issue("p00303", "omission", note="clause omitted",
                         excerpt="себе"),
            verdict=TIER_B, filter_name="test", reason="test",
        ),
    ]
    review_candidates = [
        ReviewCandidate(
            pid="p00303",
            original="Он сделал торжественное обещание себе.",
            proposed="Он торжественно пообещал себе.",
            klass="calque", reason="калька с английского",
        ),
    ]
    backend = ScriptedRepairBackend([
        _repair_response([
            {
                "index": 1, "decision": "repair", "pid": "p00303",
                "repaired_translation": (
                    "Он дал себе торжественное обещание."
                ),
                "reason": "fixed the calque and kept the fact",
            },
        ]),
        _reaudit_response([]),
    ])
    evaluator = SelectiveRepairEvaluator(
        backend, config=SelectiveRepairConfig(findings_cap=10)
    )
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered, review_candidates=review_candidates,
    )
    # ONE repair call (repair + re-audit) — all three remarks share the batch
    assert len(backend.requests) == 2
    assert [len(b.findings) for b in outcome.batches] == [1]
    f = outcome.batches[0].findings[0]
    # ALL THREE remarks in ONE merged finding
    assert f.pid == "p00303"
    assert f.source_stage == "fidelity_auditor+russian_editor"
    assert len(f.sources) == 3
    assert {s["category"] for s in f.sources} == {
        "changed_fact", "omission", "calque",
    }
    # unique index (single finding keeps index 1)
    assert [f.index for f in outcome.batches[0].findings] == [1]
    # prompt: the merged finding shows ALL THREE remarks with their stages
    prompt = backend.requests[0].messages[0].content
    assert "source=fidelity_auditor+russian_editor" in prompt
    assert "fact shifted" in prompt and "калька" in prompt
    assert "clause omitted" in prompt
    # commit behavior: the merged index's repair is committed once
    assert dict(outcome.committed) == {
        "p00303": "Он дал себе торжественное обещание."
    }
    # the review journal maps the editor candidate to the merged finding's
    # verdict (the single index answered 'repair' -> accepted)
    assert len(outcome.review_journal) == 1
    assert outcome.review_journal[0]["verdict"] == "accepted"
    assert outcome.repair_complete is True


def test_one_pid_auditor_two_editors_single_index_journal_both_bound():
    """CANDIDATE-MERGE regression (RV2 MEDIUM finding, owner clarification
    2026-08-13): one PID with [A, E1, E2] (auditor + TWO editor candidates)
    merges into ONE index (all three remarks in one block, one decision) —
    the journal binds BOTH editor candidates to that single merged index's
    verdict (no sub-index ambiguity: there is exactly one index per pid)."""
    from pact_v4.audit.russian_editor import ReviewCandidate

    source = {
        "p00303": "He made a solemn promise to himself.",
        "p00304": "Next paragraph.",
    }
    translation = {
        "p00303": "Он сделал торжественное обещание себе.",
        "p00304": "Следующий абзац.",
    }
    filtered = [
        FilteredIssue(
            issue=_issue("p00303", "changed_fact", note="fact shifted",
                         excerpt="торжественное обещание"),
            verdict=TIER_B, filter_name="test", reason="test",
        ),
    ]
    review_candidates = [
        ReviewCandidate(
            pid="p00303",
            original="Он сделал торжественное обещание себе.",
            proposed="Он торжественно пообещал себе.",
            klass="calque", reason="калька с английского",
        ),
        ReviewCandidate(
            pid="p00303",
            original="Он сделал торжественное обещание себе.",
            proposed="Он дал себе торжественное обещание.",
            klass="grammar", reason="порядок слов",
        ),
    ]
    backend = ScriptedRepairBackend([
        _repair_response([
            {
                "index": 1, "decision": "repair", "pid": "p00303",
                "repaired_translation": (
                    "Он дал себе торжественное обещание."
                ),
                "reason": "fixed the calque, the word order and kept the fact",
            },
        ]),
        _reaudit_response([]),
    ])
    evaluator = SelectiveRepairEvaluator(
        backend, config=SelectiveRepairConfig(findings_cap=10)
    )
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered, review_candidates=review_candidates,
    )
    # ONE index carries all three remarks (auditor + both editor candidates).
    assert [len(b.findings) for b in outcome.batches] == [1]
    f = outcome.batches[0].findings[0]
    assert f.source_stage == "fidelity_auditor+russian_editor"
    assert len(f.sources) == 3
    assert f.index == 1
    # BOTH editor candidates are bound to the single merged index's verdict
    # (accepted — the one decision for the pid); no candidate is left with
    # the "never answered" failure verdict.
    assert len(outcome.review_journal) == 2
    for entry in outcome.review_journal:
        assert entry["pid"] == "p00303"
        assert entry["verdict"] == "accepted"
        assert entry["committed_text"] == "Он дал себе торжественное обещание."
    assert dict(outcome.committed) == {
        "p00303": "Он дал себе торжественное обещание."
    }
    assert outcome.repair_complete is True


def test_one_pid_two_editor_candidates_only_single_index():
    """CANDIDATE-MERGE (owner clarification 2026-08-13): a PID with ONLY
    same-stage remarks (two editor candidates, no audit finding) also merges
    into ONE finding — the repair model sees both proposals in one block and
    decides once."""
    from pact_v4.audit.russian_editor import ReviewCandidate

    source = {
        "p00303": "He made a solemn promise to himself.",
        "p00304": "Next paragraph.",
    }
    translation = {
        "p00303": "Он сделал торжественное обещание себе.",
        "p00304": "Следующий абзац.",
    }
    review_candidates = [
        ReviewCandidate(
            pid="p00303",
            original="Он сделал торжественное обещание себе.",
            proposed="Он торжественно пообещал себе.",
            klass="calque", reason="калька с английского",
        ),
        ReviewCandidate(
            pid="p00303",
            original="Он сделал торжественное обещание себе.",
            proposed="Он дал себе торжественное обещание.",
            klass="grammar", reason="порядок слов",
        ),
    ]
    backend = ScriptedRepairBackend([
        _repair_response([
            {
                "index": 1, "decision": "pass",
                "reason": "правка спорна — отклонено",
            },
        ]),
    ])
    evaluator = SelectiveRepairEvaluator(
        backend, config=SelectiveRepairConfig(findings_cap=10)
    )
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=(), review_candidates=review_candidates,
    )
    assert [len(b.findings) for b in outcome.batches] == [1]
    f = outcome.batches[0].findings[0]
    assert f.source_stage == "russian_editor"  # single stage, no join
    assert len(f.sources) == 2
    assert f.index == 1
    # both candidates share the single index's verdict (rejected)
    assert len(outcome.review_journal) == 2
    for entry in outcome.review_journal:
        assert entry["verdict"] == "rejected"
    assert outcome.committed == ()
    assert outcome.repair_complete is True


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
