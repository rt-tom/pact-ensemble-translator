#!/usr/bin/env python3
"""Offline contract tests for Pact ensemble pipeline v3.1."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from v31_common import JsonGenerationError, complete_json, issue_record, merge_duplicate_issues
import v31_cross_verify
import v31_postcheck
import v31_repair
import v31_adjudicate
import v31_finalize_verification
import v31_merge_issues


def load_runtime(project_root: Path):
    from v31_common import load_runtime as _load
    return _load(project_root)


def run(project_root: Path) -> None:
    runner_script = (HERE / "run_full_pipeline_v31.ps1").read_text(encoding="utf-8")
    assert "[int]$QwenGlobalSmokeContextSize = 32768" in runner_script
    assert "'QwenGlobalSmoke'" in runner_script
    assert "--reviewer-context-size" in runner_script
    assert "-Profile QwenGlobalSmoke" in runner_script

    runtime = load_runtime(project_root)

    class FakeJsonRuntime:
        safe_json_loads = staticmethod(runtime.safe_json_loads)

        @staticmethod
        def fit_output_budget(client, messages, stage, proposed):
            return proposed

    class FakeJsonClient:
        def __init__(self, responses):
            self.responses = list(responses)
            self.budgets = []
            self.messages = []

        def complete(self, messages, stage, max_tokens, label):
            self.budgets.append(max_tokens)
            self.messages.append(messages)
            content, finish_reason = self.responses.pop(0)
            return SimpleNamespace(
                content=content,
                reasoning="",
                finish_reason=finish_reason,
                usage={"completion_tokens": max_tokens},
                wall_seconds=0.01,
            )

    def verdict(reason="Краткая причина. Нужна локальная проверка."):
        return {
            "decision": "repair",
            "confidence": "high",
            "reason": reason,
            "required_invariant": "Смысл должен быть сохранён.",
            "forbidden_interpretations": ["Не менять субъект."],
            "repair_scope": "span",
            "target_span": "фрагмент",
        }

    compact_json = json.dumps(verdict(), ensure_ascii=False)
    json_stage = dict(v31_cross_verify.DEFAULT_QWEN)
    json_messages = [{"role": "system", "content": "Return JSON."}]

    client = FakeJsonClient([
        ('{"decision":"repair","reason":"обрезано', "length"),
        (compact_json, "stop"),
    ])
    parsed, diagnostics = complete_json(
        FakeJsonRuntime, client, json_messages, json_stage, 1400,
        "test:length-retry", 3, validator=v31_cross_verify.parse,
        length_retry_max_tokens=1600,
    )
    assert parsed["decision"] == "repair"
    assert client.budgets == [1400, 1600]
    assert [item["max_tokens"] for item in diagnostics] == [1400, 1600]
    assert diagnostics[0]["finish_reason"] == "length" and not diagnostics[0]["ok"]
    assert "обрезан" in client.messages[1][-1]["content"]
    assert "Не продолжай" in client.messages[1][-1]["content"]
    assert "не более 800 символов" not in client.messages[1][-1]["content"]

    client = FakeJsonClient([(compact_json, "length"), (compact_json, "stop")])
    _, diagnostics = complete_json(
        FakeJsonRuntime, client, json_messages, json_stage, 1400,
        "test:valid-but-length", 3, validator=v31_cross_verify.parse,
        length_retry_max_tokens=1600,
    )
    assert client.budgets == [1400, 1600]
    assert diagnostics[0]["finish_reason"] == "length" and not diagnostics[0]["ok"]

    client = FakeJsonClient([(compact_json, "length")])
    _, diagnostics = complete_json(
        FakeJsonRuntime, client, json_messages, json_stage, 1400,
        "test:length-awareness-opt-in", 3, validator=v31_cross_verify.parse,
    )
    assert client.budgets == [1400]
    assert diagnostics[0]["ok"] and diagnostics[0]["finish_reason"] == "length"

    client = FakeJsonClient([(compact_json, "length")] * 3)
    try:
        complete_json(
            FakeJsonRuntime, client, json_messages, json_stage, 1400,
            "test:all-length", 3, validator=v31_cross_verify.parse,
            length_retry_max_tokens=1600,
        )
        raise AssertionError("Expected JsonGenerationError")
    except JsonGenerationError as exc:
        assert client.budgets == [1400, 1600, 1600]
        assert [item["max_tokens"] for item in exc.attempt_errors] == [1400, 1600, 1600]
        assert all(item.get("finish_reason") == "length" for item in exc.attempt_errors)

    client = FakeJsonClient([
        ('{"decision":"repair","reason":"обрезано', "stop"),
        (compact_json, "stop"),
    ])
    _, diagnostics = complete_json(
        FakeJsonRuntime, client, json_messages, json_stage, 1400,
        "test:no-json-repair", 3, validator=v31_cross_verify.parse,
        length_retry_max_tokens=1600,
    )
    assert client.budgets == [1400, 1400]
    assert not diagnostics[0]["ok"] and "Invalid JSON response" in diagnostics[0]["error"]

    retry_calls = 0

    def reject_once(data):
        nonlocal retry_calls
        retry_calls += 1
        if retry_calls == 1:
            raise ValueError("span repair changed_ratio=0.433")
        return data

    client = FakeJsonClient([(compact_json, "stop"), (compact_json, "stop")])
    parsed, diagnostics = complete_json(
        FakeJsonRuntime, client, json_messages, json_stage, 1400,
        "test:repair-span-ratio-guidance", 2, validator=reject_once,
        retry_guidance=v31_repair.REPAIR_RETRY_GUIDANCE,
    )
    assert parsed == json.loads(compact_json)
    assert not diagnostics[0]["ok"] and diagnostics[1]["ok"]
    assert "replace_full" in client.messages[1][-1]["content"]
    assert "полный исправленный CURRENT_RU" in client.messages[1][-1]["content"]

    long_reason_json = json.dumps(verdict("x" * 801), ensure_ascii=False)
    client = FakeJsonClient([(long_reason_json, "stop")])
    parsed, diagnostics = complete_json(
        FakeJsonRuntime, client, json_messages, json_stage, 1400,
        "test:advisory-reason-limit", 3,
        validator=v31_cross_verify.parse,
        length_retry_max_tokens=1600,
        retry_guidance=v31_cross_verify.CROSS_VERIFY_RETRY_GUIDANCE,
    )
    assert parsed["reason"] == "x" * 801
    assert diagnostics[0]["ok"] and diagnostics[0]["finish_reason"] == "stop"
    assert client.budgets == [1400]
    assert v31_cross_verify.parse(verdict("x" * 5000))["reason"] == "x" * 5000

    empty_reason = verdict("   ")
    try:
        v31_cross_verify.parse(empty_reason)
        raise AssertionError("Expected empty reason rejection")
    except ValueError as exc:
        assert "reason must not be empty" in str(exc)
    assert v31_cross_verify.parse(verdict())["reason"] == verdict()["reason"]

    keep_none = verdict()
    keep_none.update({"decision": "keep", "repair_scope": "none"})
    assert v31_cross_verify.parse(keep_none)["repair_scope"] == "span"

    uncertain_none = verdict()
    uncertain_none.update({"decision": "uncertain", "repair_scope": "none"})
    try:
        v31_cross_verify.parse(uncertain_none)
        raise AssertionError("Expected uncertain + none scope rejection")
    except ValueError as exc:
        assert "allowed only when decision is keep" in str(exc)

    repair_none = verdict()
    repair_none["repair_scope"] = "none"
    try:
        v31_cross_verify.parse(repair_none)
        raise AssertionError("Expected repair + none scope rejection")
    except ValueError as exc:
        assert "allowed only when decision is keep" in str(exc)

    unknown_scope = verdict()
    unknown_scope["repair_scope"] = "document"
    try:
        v31_cross_verify.parse(unknown_scope)
        raise AssertionError("Expected unknown scope rejection")
    except ValueError as exc:
        assert "Invalid repair_scope: document" in str(exc)

    for valid_scope in ("span", "sentence", "paragraph"):
        scoped = verdict()
        scoped["repair_scope"] = valid_scope
        assert v31_cross_verify.parse(scoped)["repair_scope"] == valid_scope

    uncertain_sentence = verdict()
    uncertain_sentence.update({
        "decision": "uncertain",
        "confidence": "low",
        "repair_scope": "sentence",
        "target_span": "",
    })
    parsed_uncertain = v31_cross_verify.parse(uncertain_sentence)
    original_issue = {"scope": "paragraph", "target_span": "исходный фрагмент"}
    downstream_scope = parsed_uncertain.get("repair_scope") or original_issue["scope"]
    downstream_target = parsed_uncertain.get("target_span") or original_issue["target_span"]
    resolved = v31_finalize_verification.resolve_uncertain_policy(
        parsed_uncertain["decision"], parsed_uncertain["confidence"], "repair"
    )
    assert resolved == ("repair", "medium", True)
    assert downstream_scope == "sentence"
    assert downstream_target == "исходный фрагмент"

    with tempfile.TemporaryDirectory() as temp:
        cache = Path(temp) / "cached.json"
        cached_record = {"issue_id": "cached", **verdict()}
        cache.write_text(json.dumps(cached_record, ensure_ascii=False), encoding="utf-8")
        before = cache.read_bytes()
        calls = 0

        def should_not_generate():
            nonlocal calls
            calls += 1
            raise AssertionError("Cached issue called model generator")

        reused = v31_cross_verify.load_or_generate(cache, False, should_not_generate)
        assert calls == 0 and reused["issue_id"] == "cached"
        assert cache.read_bytes() == before

    qwen = issue_record(
        pid="p00001", category="meaning", problem="wrong idiom",
        detector="qwen_semantic_primary", target_span="трудно отложить",
        required_invariant="hard to kill", confidence="high",
    )
    gemma = issue_record(
        pid="p00001", category="calque", problem="broken collocation",
        detector="gemma_russian_primary", target_span="трудно отложить",
        required_invariant="natural Russian", confidence="high",
    )
    merged = merge_duplicate_issues([qwen, gemma])
    assert len(merged) == 2, merged

    same_qwen = issue_record(
        pid="p00002", category="meaning", problem="wrong idiom",
        detector="qwen_semantic_primary", target_span="трудно отложить",
        required_invariant="hard to kill", confidence="high",
    )
    same_gemma = issue_record(
        pid="p00002", category="meaning", problem="wrong idiom",
        detector="gemma_semantic_primary", target_span="трудно отложить",
        required_invariant="hard to kill", confidence="high",
    )
    exact = merge_duplicate_issues([same_qwen, same_gemma])
    assert len(exact) == 1 and exact[0]["agreement_count"] == 2
    preverified, judges = v31_merge_issues.route_issue(exact[0])
    assert preverified and not judges
    assert exact[0]["verification_route"] == "independent_detector_agreement"

    hard = issue_record(
        pid="p00003", category="mixed_script", problem="Mary remains",
        detector="deterministic", target_span="Mary",
        required_invariant="Russian spelling", confidence="deterministic",
    )
    hard["detected_by"].append("gemma_semantic_primary")
    hard["source_issues"] = [dict(hard)]
    preverified, judges = v31_merge_issues.route_issue(hard)
    assert preverified and not judges and hard["verification_route"] == "hard_deterministic"

    assert v31_finalize_verification.consolidate_dual_decisions(
        {"decision": "repair", "confidence": "high"},
        {"decision": "repair", "confidence": "medium"},
    ) == ("repair", "medium")
    assert v31_finalize_verification.consolidate_dual_decisions(
        {"decision": "repair", "confidence": "high"},
        {"decision": "keep", "confidence": "high"},
    ) == ("uncertain", "medium")
    assert v31_finalize_verification.resolve_uncertain_policy(
        "uncertain", "medium", "repair"
    ) == ("repair", "medium", True)

    block = runtime.Block(
        pid="p00001", index=0, tag="p", source_html="<p>x</p>",
        source_text="They are hard to put down.", word_count=6,
        digits=[], inline_spans=[],
    )
    cfg = runtime.merge(runtime.DEFAULTS, {
        "validation": {"strict_digits": False, "english_sequence_min_words": 2},
        "ensemble_v31": {"repair": {"max_changed_ratio_span": 0.55}},
    })
    glossary = runtime.Glossary(cfg)
    book = runtime.BookBible(Path(cfg["paths"]["book_bible_file"]))
    det_block = runtime.Block(
        pid="p00004", index=0, tag="p", source_html="<p>Mary nodded.</p>",
        source_text="Mary nodded.", word_count=2, digits=[], inline_spans=[],
    )
    det_issues = runtime.deterministic_issues(
        [det_block], {"p00004": "Mary кивнула."}, cfg, glossary, {}, book
    )
    assert "mixed_script" in {item.category for item in det_issues}
    clean_issues = runtime.deterministic_issues(
        [det_block], {"p00004": "Мэри кивнула."}, cfg, glossary, {}, book
    )
    assert "mixed_script" not in {item.category for item in clean_issues}

    digit_block = runtime.Block(
        pid="p00005", index=0, tag="p", source_html="<p>3 rules.</p>",
        source_text="3 rules.", word_count=2, digits=["3"], inline_spans=[],
    )
    digit_map = {"p00005": digit_block}
    strict_cfg = runtime.merge(cfg, {"validation": {"strict_digits": True}})
    assert not runtime.validate_single_repair(
        "p00005", "Три правила.", digit_map, strict_cfg, "Три старых правила."
    )
    assert runtime.validate_single_repair(
        "p00005", "Четыре правила.", digit_map, strict_cfg, "Три старых правила."
    )
    assert runtime.validate_single_repair(
        "p00005", "Три правила, 4 исключения.", digit_map, strict_cfg, "Три старых правила."
    )

    candidates = v31_repair.parse_candidates({
        "candidates": [{
            "candidate_id": "A", "action": "replace_span",
            "old": "трудно отложить", "new": "трудно прикончить",
            "text": "", "reason": "idiom", "challenge_reason": "",
        }]
    }, "p00001", "Их трудно отложить.", False, runtime, cfg, {"p00001": block})
    assert candidates[0]["after"] == "Их трудно прикончить."

    # An exact whole-PID replace_span is normalized to replace_full, preserves
    # provenance, and bypasses only the local span-ratio guard.
    candidates = v31_repair.parse_candidates({
        "candidates": [{
            "candidate_id": "A", "action": "replace_span",
            "old": "Дункан подчинился.", "new": "Дункан так и сделал.",
            "text": "", "reason": "meaning", "challenge_reason": "",
        }]
    }, "p00001", "Дункан подчинился.", False, runtime, cfg, {"p00001": block})
    assert candidates[0]["valid"]
    assert candidates[0]["after"] == "Дункан так и сделал."
    assert candidates[0]["changed_ratio"] > 0.35
    assert candidates[0]["action"] == "replace_full"
    assert candidates[0]["operation_provenance"] == "whole_pid_replace_span_normalized_to_replace_full"

    # Exact whole-PID edits are also valid below the partial-span ratio limit.
    candidates = v31_repair.parse_candidates({
        "candidates": [{
            "candidate_id": "A", "action": "replace_span",
            "old": "Он был дома.", "new": "Он был дома!",
            "text": "", "reason": "punctuation", "challenge_reason": "",
        }]
    }, "p00001", "Он был дома.", False, runtime, cfg, {"p00001": block})
    assert candidates[0]["valid"] and candidates[0]["changed_ratio"] <= 0.35
    assert candidates[0]["action"] == "replace_full"

    # Partial edits with extreme ratio (> 0.55) are still rejected.
    try:
        v31_repair.parse_candidates({"candidates": [{
            "candidate_id": "A", "action": "replace_span", "old": "подчинился",
            "new": "сделал так быстро", "text": "", "reason": "meaning", "challenge_reason": "",
        }]}, "p00001", "он подчинился", False, runtime, cfg, {"p00001": block})
        raise AssertionError("high-ratio partial span was accepted")
    except ValueError:
        pass

    # Near matches and repeated target spans are not whole-PID replacements.
    for current, target in (("Дункан подчинился.", "Дункан подчинился. "), ("абв абв", "абв")):
        try:
            v31_repair.parse_candidates({"candidates": [{
                "candidate_id": "A", "action": "replace_span", "old": target,
                "new": "где", "text": "", "reason": "meaning", "challenge_reason": "",
            }]}, "p00001", current, False, runtime, cfg, {"p00001": block})
            raise AssertionError("non-exact or ambiguous span was accepted")
        except ValueError:
            pass

    # Regression: a legitimate partial span repair with changed_ratio between
    # 0.35 and 0.55 is accepted with the updated threshold (production incident:
    # repair looped 3 times because the old 0.35 cap rejected both candidates).
    repar_current = "Он закончить распятие одной из них!"
    repar_old = "закончить распятие одной"
    repar_new = "пригвоздить одну"
    candidates = v31_repair.parse_candidates({"candidates": [{
        "candidate_id": "A", "action": "replace_span",
        "old": repar_old, "new": repar_new,
        "text": "", "reason": "register", "challenge_reason": "",
    }]}, "p00001", repar_current, False, runtime, cfg, {"p00001": block})
    assert candidates[0]["valid"]
    assert candidates[0]["action"] == "replace_span"
    assert candidates[0]["changed_ratio"] > 0.35
    assert candidates[0]["changed_ratio"] < 0.55

    # Normalized whole-PID candidates retain the mandatory four independent
    # downstream decisions; adjudication cannot accept a candidate without one.
    adjudicate_source = (HERE / "v31_adjudicate.py").read_text(encoding="utf-8")
    for gate in ("qwen_semantic", "gemma_semantic", "gemma_russian", "deterministic"):
        assert gate in adjudicate_source
    assert "Missing gate decision" in adjudicate_source

    # Some repair responses put a full replacement in ``new`` and echo
    # CURRENT_RU in ``text``.  The candidate must remain usable rather than
    # failing as unchanged.
    candidates = v31_repair.parse_candidates({
        "candidates": [{
            "candidate_id": "A", "action": "replace_full",
            "old": "", "new": "Их трудно прикончить.",
            "text": "Их трудно отложить.", "reason": "idiom", "challenge_reason": "",
        }]
    }, "p00001", "Их трудно отложить.", False, runtime, cfg, {"p00001": block})
    assert candidates[0]["after"] == "Их трудно прикончить."
    assert candidates[0]["valid"]
    assert candidates[0]["text"] == "Их трудно прикончить."
    assert candidates[0]["text_source"] == "new_fallback"
    assert candidates[0]["model_text"] == "Их трудно отложить."

    # If primary text is valid, it remains authoritative even when ``new`` is
    # invalid or independently valid but different.
    candidates = v31_repair.parse_candidates({
        "candidates": [{
            "candidate_id": "A", "action": "replace_full",
            "old": "", "new": "They are hard to put down.",
            "text": "Их трудно прикончить.", "reason": "idiom", "challenge_reason": "",
        }]
    }, "p00001", "Их трудно отложить.", False, runtime, cfg, {"p00001": block})
    assert candidates[0]["valid"]
    assert candidates[0]["after"] == "Их трудно прикончить."
    assert candidates[0]["text_source"] == "text"

    candidates = v31_repair.parse_candidates({
        "candidates": [{
            "candidate_id": "A", "action": "replace_full",
            "old": "", "new": "Их сложно прикончить.",
            "text": "Их трудно прикончить.", "reason": "idiom", "challenge_reason": "",
        }]
    }, "p00001", "Их трудно отложить.", False, runtime, cfg, {"p00001": block})
    assert candidates[0]["valid"]
    assert candidates[0]["after"] == "Их трудно прикончить."
    assert candidates[0]["text"] == "Их трудно прикончить."
    assert candidates[0]["text_source"] == "text"

    # Both fields invalid: do not combine or recover them heuristically.
    try:
        v31_repair.parse_candidates({
            "candidates": [{
                "candidate_id": "A", "action": "replace_full",
                "old": "", "new": "Still English.",
                "text": "They are hard to put down.", "reason": "idiom", "challenge_reason": "",
            }]
        }, "p00001", "Их трудно отложить.", False, runtime, cfg, {"p00001": block})
        raise AssertionError("both-invalid candidate was accepted")
    except ValueError:
        pass

    # Fallback applies the same active validator, including strict numeric
    # invariants, before it can replace the primary text.
    candidates = v31_repair.parse_candidates({
        "candidates": [{
            "candidate_id": "A", "action": "replace_full",
            "old": "", "new": "3 правила.",
            "text": "Three rules.", "reason": "digits", "challenge_reason": "",
        }]
    }, "p00005", "3 старых правила.", False, runtime, strict_cfg, {"p00005": digit_block})
    assert candidates[0]["valid"]
    assert candidates[0]["text"] == "3 правила."
    assert candidates[0]["text_source"] == "new_fallback"

    # A replace_full response can place EN in ``text`` while ``new`` contains
    # the complete valid Russian repair.  The valid field must recover it.
    candidates = v31_repair.parse_candidates({
        "candidates": [{
            "candidate_id": "A", "action": "replace_full",
            "old": "", "new": "Их трудно прикончить.",
            "text": "They are hard to put down.", "reason": "idiom", "challenge_reason": "",
        }]
    }, "p00001", "Их трудно отложить.", False, runtime, cfg, {"p00001": block})
    assert candidates[0]["after"] == "Их трудно прикончить."
    assert candidates[0]["valid"]
    assert candidates[0]["text"] == "Их трудно прикончить."
    assert candidates[0]["text_source"] == "new_fallback"
    assert candidates[0]["model_text"] == "They are hard to put down."

    qgate = v31_postcheck.parse({
        "verdict": "accept", "confidence": "high", "issue_valid": True,
        "faithful_to_source": True, "all_issues_fixed": True,
        "introduced_new_semantic_error": False, "reason": "ok", "feedback": "",
    }, "qwen_semantic", "replace_span")
    assert qgate["faithful_to_source"] and not qgate["introduced_new_semantic_error"]

    # A challenge response can use the generic label while its structured
    # finding is unambiguous; the gate must preserve it as a challenge result.
    challenge_gate = v31_postcheck.parse({
        "verdict": "accept", "confidence": "high", "issue_valid": False,
        "faithful_to_source": True, "all_issues_fixed": True,
        "introduced_new_semantic_error": False, "reason": "no issue", "feedback": "",
    }, "qwen_semantic", "challenge_issue")
    assert challenge_gate["verdict"] == "accept_challenge"

    extended = {
        "pid": "p00001", "severity": "minor", "category": "grammar",
        "problem": "x", "repair_instruction": "fix", "issue_id": "v31i00001",
        "detected_by": ["gemma_russian_primary"], "detector_families": ["gemma"],
        "verification_confidence": "high", "verification_reason": "yes",
        "required_invariant": "natural",
    }
    runtime.Issue(**v31_finalize_verification.compatible_issue(extended))

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        for name in ("locked.json", "established.json", "provisional.json", "conflicts.json"):
            (root / name).write_text("{}", encoding="utf-8")
        (root / "book.json").write_text("{}", encoding="utf-8")
        (root / "arcs.json").write_text("{}", encoding="utf-8")
        prompt_cfg = runtime.merge(runtime.DEFAULTS, {
            "paths": {
                "glossary_dir": str(root),
                "book_bible_file": str(root / "book.json"),
                "arc_names_file": str(root / "arcs.json"),
            }
        })
        messages = runtime.translation_messages(
            prompt_cfg, runtime.Glossary(prompt_cfg), runtime.BookBible(root / "book.json"),
            {}, {"by_pid": {"p00001": {"idioms": [{"source_span": "put down", "meaning": "kill"}]}}},
            runtime.Chunk("c1", ["p00001"], 6), {"p00001": block}, [], [], [], {}, None,
        )
        assert "SOURCE_ANALYSIS_DO_NOT_TRANSLATE" in messages[1]["content"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=HERE.parent)
    args = parser.parse_args()
    run(args.project_root.resolve())
    print("Pact v3.1 offline self-tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
