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

from v31_common import (
    JsonGenerationError, cache_identity, cache_reuse, complete_json, issue_record,
    merge_duplicate_issues, with_cache_identity, write_json, write_text_atomic,
)
import v31_cross_verify
import v31_postcheck
import v31_repair
import v31_finalize_verification
import v31_merge_issues
import v31_artifact_dag
import v31_audit
import v31_final_lifecycle
import v31_finalize_quality


def load_runtime(project_root: Path):
    from v31_common import load_runtime as _load
    return _load(project_root)


def run(project_root: Path) -> None:
    runtime = load_runtime(project_root)

    # DAG lifecycle: redo invalidates only direct consumers and their descendants.
    def actions(**kwargs):
        return {row["stage"]: row["action"] for row in v31_artifact_dag.plan(**kwargs)}
    source = actions(redo_source=True)
    assert source["translation"] == "INVALIDATE" and source["finalization"] == "INVALIDATE"
    assert source["source_analysis"] == "INVALIDATE"
    translation = actions(redo_translation=True)
    assert translation["source_analysis"] == "REUSE" and translation["primary_audit"] == "INVALIDATE"
    quality = actions(redo_quality=True)
    assert quality["translation"] == "REUSE" and quality["primary_repair"] == "INVALIDATE"
    assert quality["residual_audit"] == "INVALIDATE" and quality["residual_repair"] == "INVALIDATE"
    assert quality["final_quality"] == "INVALIDATE" and quality["finalization"] == "INVALIDATE"
    formatting = actions(redo_formatting=True)
    assert formatting["finalization"] == "INVALIDATE"
    assert formatting["final_quality"] == "REUSE" and formatting["review"] == "REUSE"

    # v3.1.3-07: final text lineage and bounded terminal policy.
    blocks_for_final = [{"pid": "p00001"}, {"pid": "p00002"}, {"pid": "p00003"}]
    initial = {"p00001": "Первый.", "p00002": "Второй.", "p00003": "Третий."}
    residual = dict(initial, p00002="Исправленный второй.")
    ledger = v31_final_lifecycle.append_ledger(None, initial, residual, "residual", "accepted residual repair")
    final = dict(residual, p00003="Исправленный третий.")
    ledger = v31_final_lifecycle.append_ledger(ledger, residual, final, "final_repair", "accepted final repair")
    assert ledger["changed_pids"] == ["p00002", "p00003"]  # residual PID is never lost
    assert v31_final_lifecycle.changed_pids(final, dict(final)) == []  # unchanged PIDs are not recomputed
    assert v31_final_lifecycle.context_pids(blocks_for_final, ["p00002"], 1) == ["p00001", "p00002", "p00003"]
    assert v31_final_lifecycle.terminal_status(
        ledger_ok=True, coverage_ok=True, verification_ok=True, smoke_ok=True,
        blocking_findings=[], final_repair_rounds=1,
    ) == "complete"  # clean final text
    gross_omission = [{"pid": "p00002", "category": "missing", "severity": "critical"}]
    assert v31_final_lifecycle.terminal_status(
        ledger_ok=True, coverage_ok=True, verification_ok=True, smoke_ok=True,
        blocking_findings=gross_omission, final_repair_rounds=1,
    ) == "quarantined"  # global smoke catches gross omission
    assert v31_final_lifecycle.terminal_status(
        ledger_ok=True, coverage_ok=True, verification_ok=True, smoke_ok=True,
        blocking_findings=[{"ambiguous": True}], final_repair_rounds=0,
    ) == "quarantined"  # ambiguous blockers are not sent to repair
    assert v31_final_lifecycle.terminal_status(
        ledger_ok=True, coverage_ok=True, verification_ok=False, smoke_ok=True,
        blocking_findings=[], final_repair_rounds=0,
    ) == "failed"  # final verification execution failure
    assert v31_final_lifecycle.terminal_status(
        ledger_ok=False, coverage_ok=True, verification_ok=True, smoke_ok=True,
        blocking_findings=[], final_repair_rounds=0,
    ) == "failed"  # missing/corrupt ledger
    assert v31_final_lifecycle.terminal_status(
        ledger_ok=True, coverage_ok=False, verification_ok=True, smoke_ok=True,
        blocking_findings=[], final_repair_rounds=0,
    ) == "failed"  # incomplete coverage
    assert v31_final_lifecycle.terminal_status(
        ledger_ok=True, coverage_ok=True, verification_ok=True, smoke_ok=False,
        blocking_findings=[], final_repair_rounds=0,
    ) == "failed"  # technical global-smoke failure
    assert v31_final_lifecycle.terminal_status(
        ledger_ok=True, coverage_ok=True, verification_ok=True, smoke_ok=True,
        blocking_findings=[], final_repair_rounds=2,
    ) == "failed"  # a second final-repair round is forbidden
    assert v31_final_lifecycle.terminal_status(
        ledger_ok=True, coverage_ok=True, verification_ok=True, smoke_ok=True,
        blocking_findings=[], final_repair_rounds=1, prior_status="quarantined",
    ) == "quarantined"  # quarantine is monotonic and never becomes complete

    # Exercise the active finalize-quality entry point, not only terminal_status.
    def final_quality_fixture(work: Path, *, prior: str | None = None, technical_failure: bool = False) -> None:
        coverage = {"ok": True, "completed": 1}
        write_json(work / "source_scene_map.json", {"coverage": coverage})
        write_json(work / "draft_translations.json", {"p00001": "Текст."})
        write_json(work / "v31_final_translations.json", {"p00001": "Текст."})
        write_json(work / "v31_final_changed_pid_ledger.json", {"entries": [], "changed_pids": []})
        for pass_name in ("primary", "residual"):
            root = work / "v31" / pass_name
            for detector in ("qwen_semantic", "gemma_semantic", "gemma_russian", "gemma_discourse"):
                write_json(root / f"{detector}.json", {"coverage": coverage})
            write_json(root / "merged_issues.json", {"merged_issue_count": 0})
            write_json(root / "verification_report.json", {"total": 0, "repair": 0, "keep": 0, "uncertain": 0})
            write_json(root / "verified_issues.json", [])
            write_json(root / "lifecycle.json", [])
            write_json(root / "uncertain_issues.json", [])
            write_json(root / "status.json", {"retry_required": 0, "total": 0, "resolved": 0})
            for judge in ("qwen", "gemma"):
                write_json(root / f"verify_queue_{judge}.json", [])
                write_json(root / f"cross_verify_{judge}.json", {"expected": 0, "completed": 0})
        final = work / "v31" / "final"
        for detector in ("qwen_semantic", "gemma_semantic", "gemma_russian"):
            write_json(final / f"{detector}.json", {"coverage": {"ok": True, "expected": 0, "completed": 0}})
        write_json(final / "verified_issues.json", [])
        write_json(final / "qwen_global_smoke.json", {
            "coverage": {"ok": not technical_failure, "expected": 1, "completed": 1}, "issues": [],
        })
        if prior:
            write_json(work / "state.json", {"status": prior})
            write_json(work / "v31_quality_gate.json", {"status": prior})

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        original = (v31_finalize_quality.load_runtime, v31_finalize_quality.load_cfg,
                    v31_finalize_quality.selected_chapters, v31_finalize_quality.load_manifest,
                    v31_finalize_quality.deterministic, v31_finalize_quality.setup_logging, sys.argv)
        source = root / "001.html"
        source.write_text("fixture", encoding="utf-8")
        v31_finalize_quality.load_runtime = lambda _: object()
        v31_finalize_quality.load_cfg = lambda *_: {}
        v31_finalize_quality.selected_chapters = lambda *_: [(source, current_work)]
        v31_finalize_quality.load_manifest = lambda _: ({}, [{"pid": "p00001"}], {})
        v31_finalize_quality.deterministic = lambda *_: []
        v31_finalize_quality.setup_logging = lambda: None
        try:
            for prior, technical_failure, expected in (
                ("quarantined", False, "quarantined"),  # clean recompute cannot promote quarantine
                ("complete", False, "complete"),
                ("quarantined", True, "failed"),  # technical failure wins over old quarantine
                (None, False, "complete"),  # new work/run identity inherits no old terminal state
            ):
                current_work = root / f"case-{prior or 'new'}-{technical_failure}"
                final_quality_fixture(current_work, prior=prior, technical_failure=technical_failure)
                sys.argv = ["v31_finalize_quality.py", "--project-root", str(root), "--config", str(root / "cfg.json"), "--start", "1", "--end", "1", "--final-lifecycle"]
                try:
                    v31_finalize_quality.main()
                    assert not technical_failure
                except RuntimeError:
                    assert technical_failure
                assert json.loads((current_work / "state.json").read_text(encoding="utf-8"))["status"] == expected
                assert json.loads((current_work / "v31_quality_gate.json").read_text(encoding="utf-8"))["status"] == expected
        finally:
            (v31_finalize_quality.load_runtime, v31_finalize_quality.load_cfg,
             v31_finalize_quality.selected_chapters, v31_finalize_quality.load_manifest,
             v31_finalize_quality.deterministic, v31_finalize_quality.setup_logging, sys.argv) = original

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
        identity = cache_identity(producer="test", schema="v1", source="en", inputs="ru",
                                  config={"batch": 1}, prompt="prompt", profile="model")
        cached_record = with_cache_identity({"issue_id": "cached", **verdict()}, identity)
        write_json(cache, cached_record)
        before = cache.read_bytes()
        calls = 0

        def should_not_generate():
            nonlocal calls
            calls += 1
            raise AssertionError("Cached issue called model generator")

        reused = v31_cross_verify.load_or_generate(cache, False, identity, should_not_generate)
        assert calls == 0 and reused["issue_id"] == "cached"
        assert cache.read_bytes() == before

        # Relevant inputs invalidate deterministically; mtime is never consulted.
        for field, changed in (("source", "other en"), ("inputs", "other ru"),
                               ("config", {"batch": 2})):
            values = {"source": "en", "inputs": "ru", "config": {"batch": 1}}
            values[field] = changed
            stale = cache_identity(producer="test", schema="v1", prompt="prompt", profile="model", **values)
            _, reason = cache_reuse(cache, stale)
            assert reason.startswith("identity_mismatch:"), reason
        reused, reason = cache_reuse(cache, identity)
        assert reused is not None and reason == "reused"

        legacy = Path(temp) / "legacy.json"
        write_json(legacy, {"issue_id": "legacy", **verdict()})
        value, reason = cache_reuse(legacy, identity)
        assert value is None and reason == "legacy_missing_identity"

        artifact = Path(temp) / "authoritative.json"
        write_json(artifact, {"good": True})
        before_good = artifact.read_bytes()
        try:
            write_text_atomic(artifact, '{"bad":', validator=json.loads)
            raise AssertionError("Expected malformed temp rejection")
        except json.JSONDecodeError:
            pass
        assert artifact.read_bytes() == before_good
        assert not artifact.with_suffix(".json.tmp").exists()

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
        "ensemble_v31": {"repair": {"max_changed_ratio_span": 0.35}},
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
    smoke_blocks = [{"pid": "p00004", "source_text": "Mary nodded."}]
    smoke_stage, smoke_messages = v31_audit.qwen_global_smoke_messages(
        runtime, cfg, Path("."), smoke_blocks, {"p00004": smoke_blocks[0]},
        {"p00004": "Мэри кивнула."}, ["p00004"], "final",
    )
    smoke_prompt = "\n".join(message["content"] for message in smoke_messages)
    assert "gross omissions" in smoke_prompt and "<EN>Mary nodded.</EN>" in smoke_prompt and "<RU>Мэри кивнула.</RU>" in smoke_prompt
    assert smoke_stage == v31_audit.DEFAULTS["qwen_global_smoke"]
    runner_text = (HERE / "run_full_pipeline_v31.ps1").read_text(encoding="utf-8")
    assert runner_text.count("'qwen_global_smoke'") == 1  # exactly one global smoke stage

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

    # A replace_full response may echo CURRENT_RU in ``text`` and put its
    # complete replacement in ``new``.  That unambiguous fallback is valid.
    candidates = v31_repair.parse_candidates({
        "candidates": [{
            "candidate_id": "A", "action": "replace_full",
            "old": "", "new": "Их трудно прикончить.",
            "text": "Их трудно отложить.", "reason": "idiom", "challenge_reason": "",
        }]
    }, "p00001", "Их трудно отложить.", False, runtime, cfg, {"p00001": block})
    assert candidates[0]["after"] == "Их трудно прикончить."
    assert candidates[0]["valid"]

    qgate = v31_postcheck.parse({
        "verdict": "accept", "confidence": "high", "issue_valid": True,
        "faithful_to_source": True, "all_issues_fixed": True,
        "introduced_new_semantic_error": False, "reason": "ok", "feedback": "",
    }, "qwen_semantic", "replace_span")
    assert qgate["faithful_to_source"] and not qgate["introduced_new_semantic_error"]

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
