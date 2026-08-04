"""Unit tests for the V4 B6 quarantined-retry module.

Cover the pure algorithm in ``pact_v4.phase4.quarantined_retry``: debt
identification, look-ahead context, the bounded regeneration + re-cascade,
the Variant-B ``quarantined_final`` fallback, resume reuse of prior attempts,
and the cumulative generation-record merge. No subprocess / HTTP / real
``llama-server`` — the same stub pattern as the strict-runner tests.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from pact_v4.phase1.models import GateResult
from pact_v4.phase2.cascade import DeterministicGateData
from pact_v4.phase2.generation import GenerationCache, GenerationParams
from pact_v4.phase4.quarantined_retry import (
    OUTCOME_GENERATION_INCOMPLETE,
    OUTCOME_QUARANTINED_FINAL,
    OUTCOME_SELECTED,
    QuarantinedRetryAttempt,
    debt_mentions_chunk,
    debt_mentions_pid,
    lookahead_right_context,
    merge_retry_generation_records,
    quarantined_chunks_with_debt,
    run_quarantined_retry,
)
from pact_v4.phase4.repair import (
    RepairPhaseResult,
    RepairRoundResult,
    RepairRecord,
)
from tests.pact_v4.pipeline.test_v4_phase12_strict_runner import (
    StubGemma,
    _build_artifacts,
)

WORDS_PER_PARAGRAPH = 35


def _write_chapter_html(path: Path, n_paragraphs: int) -> None:
    paragraph_text = " ".join(f"word{i}" for i in range(WORDS_PER_PARAGRAPH))
    body = "\n".join(f"<p>{paragraph_text}</p>" for _ in range(n_paragraphs))
    path.write_text("<html><body>" + body + "</body></html>", encoding="utf-8")


def _write_empty_memory(dir_path: Path) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "glossary.json").write_text("{}", encoding="utf-8")
    (dir_path / "book_memory.json").write_text("{}", encoding="utf-8")


class _LookaheadCaller:
    """Clean text when look-ahead right_context is present, bad otherwise."""

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, bundle) -> str:
        self.calls.append(bundle)
        out: Dict[str, str] = {}
        for index, (pid, _text) in enumerate(bundle.owned_source, start=1):
            if bundle.right_context:
                out[pid] = f"Хороший перевод номер{index}"
            else:
                out[pid] = f"Плохой перевод номер{index}"
        return json.dumps(out, ensure_ascii=False)


class _BadCaller:
    """Always-bad text for chunk0001 (with or without look-ahead)."""

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, bundle) -> str:
        self.calls.append(bundle)
        out: Dict[str, str] = {}
        for index, (pid, _text) in enumerate(bundle.owned_source, start=1):
            if bundle.chunk_id == "chunk0001":
                out[pid] = "Плохой перевод"
            else:
                out[pid] = f"Хороший перевод номер{index}"
        return json.dumps(out, ensure_ascii=False)


class _TruncatedCaller:
    """Generation validation failure (truncated JSON) for the retry."""

    def __call__(self, bundle) -> str:
        return '{"p00000": "перевод'


class _ContentQwen:
    """Qwen fidelity gate keyed on translation content."""

    def __init__(self, good_marker: str = "Хороший") -> None:
        self.good_marker = good_marker

    def __call__(self, source, translation) -> GateResult:
        text = " ".join(translation.values())
        if self.good_marker in text:
            return GateResult(gate="qwen_fidelity", passed=True, detail="clean")
        return GateResult(gate="qwen_fidelity", passed=False, detail="bad text")


@pytest.fixture()
def artifacts(tmp_path: Path):
    chapter_html = tmp_path / "046.html"
    memory_dir = tmp_path / "memory"
    _write_chapter_html(chapter_html, 24)
    _write_empty_memory(memory_dir)

    from pact_v4.pipeline.v4_phase12_strict_runner import StrictBackendConfig, StrictRunConfig

    backend = StrictBackendConfig(
        exe=Path("C:/fake/llama-server.exe"), device="FAKE0", host="127.0.0.1",
        model_paths={"gemma": Path("C:/fake/gemma.gguf"), "qwen": Path("C:/fake/qwen.gguf")},
        model_names={"gemma": "gemma-fake", "qwen": "qwen-fake"},
        server_args={"gemma": [], "qwen": []}, port=0,
    )
    cfg = StrictRunConfig(
        chapter_id="046", chapter_html_path=chapter_html, memory_dir=memory_dir,
        out_dir=tmp_path / "out", backend=backend,
    )
    source, snapshot, chunk_plan, config = _build_artifacts(cfg)
    from pact_v4.pipeline._shared_runner_helpers import _glossary_entries, _risk_for_chunk
    from pact_v4.runtime.snapshot_factory import ChapterMemory

    memory = ChapterMemory.from_directory(memory_dir)
    glossary = _glossary_entries(memory)
    source_map = dict(source.source)
    risk_by_chunk = {
        pc.chunk_id: _risk_for_chunk(chunk=pc, source_map=source_map, glossary=glossary)
        for pc in chunk_plan.chunks
    }
    params = GenerationParams(temperature=cfg.temperature, seed=cfg.seed, max_tokens=cfg.max_tokens)
    return {
        "cfg": cfg,
        "source": source,
        "snapshot": snapshot,
        "chunk_plan": chunk_plan,
        "config": config,
        "glossary": glossary,
        "risk_by_chunk": risk_by_chunk,
        "params": params,
    }


def _debt_phase_result(chunk_ids=("chunk0001",)):
    uncommitted = [
        RepairRecord(
            repair_id="x" * 64,
            chunk_id=chunk_id,
            finding_ids=("f",),
            target_pids=("p00001",),
            action="region_edit",
            new_translation=(("p00001", "text"),),
            gate_trace=(),
            gemma_recheck="not_required",
            committed=False,
            reason="qwen_fidelity re-gate: bad",
        )
        for chunk_id in chunk_ids
    ]
    return RepairPhaseResult(
        status="accepted_degraded",
        rounds=(RepairRoundResult(round_number=1, records=tuple(uncommitted)),),
        debt_trace=tuple(
            f"{chunk_id}: repair not committed (qwen_fidelity re-gate: bad)"
            for chunk_id in chunk_ids
        ),
        final_translation=(("p00001", "text"),),
        integrity={"status": "complete"},
        terminal=None,
        report_payload={"status": "accepted_degraded"},
    )


# ---------------------------------------------------------------------------
# Debt identification
# ---------------------------------------------------------------------------


def test_quarantined_chunks_with_debt_returns_quarantined_with_debt():
    handoff = [
        {"chunk_id": "chunk0001", "status": "quarantined"},
        {"chunk_id": "chunk0002", "status": "audited"},
        {"chunk_id": "chunk0003", "status": "quarantined"},
    ]
    # chunk0003 is quarantined but has NO debt (clean audit, no uncommitted
    # repair) — it must not be retried.
    result = RepairPhaseResult(
        status="accepted_degraded",
        rounds=(RepairRoundResult(round_number=1, records=()),),
        debt_trace=("chunk0001: repair not committed (re-gate failed)",),
        final_translation=(),
        integrity={},
        terminal=None,
        report_payload={},
    )
    assert quarantined_chunks_with_debt(handoff, result) == ("chunk0001",)


def test_quarantined_chunks_with_debt_empty_when_no_quarantine():
    handoff = [
        {"chunk_id": "chunk0001", "status": "audited"},
        {"chunk_id": "chunk0002", "status": "audited"},
    ]
    assert quarantined_chunks_with_debt(handoff, _debt_phase_result()) == ()


def test_quarantined_chunks_with_debt_prefix_is_not_matched():
    # chunk0001 must never match the debt of chunk00010 (word boundary).
    result = RepairPhaseResult(
        status="accepted_degraded",
        rounds=(RepairRoundResult(round_number=1, records=()),),
        debt_trace=("chunk00010: repair not committed",),
        final_translation=(),
        integrity={},
        terminal=None,
        report_payload={},
    )
    handoff = [{"chunk_id": "chunk0001", "status": "quarantined"}]
    assert quarantined_chunks_with_debt(handoff, result) == ()


def test_debt_mention_helpers():
    assert debt_mentions_chunk("chunk0005: p00095: soft Gemma finding", "chunk0005")
    assert not debt_mentions_chunk("chunk0005: repair", "chunk00050")
    assert debt_mentions_pid("formatting:p00095:span1: unresolved required span", "p00095")
    assert not debt_mentions_pid("formatting:p00095:span1", "p00096")


# ---------------------------------------------------------------------------
# Look-ahead right context
# ---------------------------------------------------------------------------


def test_lookahead_right_context_is_next_chunk_source(artifacts):
    chunk_plan = artifacts["chunk_plan"]
    source = artifacts["source"]
    lookahead = lookahead_right_context(
        chunk_id="chunk0001", chunk_plan=chunk_plan, source=source,
    )
    next_chunk = chunk_plan.chunks[1]
    assert lookahead == tuple(
        (pid, dict(source.source)[pid]) for pid in next_chunk.pids
    )
    assert lookahead  # a second chunk exists, so the look-ahead is non-empty


def test_lookahead_right_context_empty_for_last_chunk(artifacts):
    chunk_plan = artifacts["chunk_plan"]
    source = artifacts["source"]
    last_chunk = chunk_plan.chunks[-1].chunk_id
    assert lookahead_right_context(
        chunk_id=last_chunk, chunk_plan=chunk_plan, source=source,
    ) == ()


# ---------------------------------------------------------------------------
# Regeneration + re-cascade
# ---------------------------------------------------------------------------


def _run_retry(artifacts, *, caller=None, qwen=None, prior=None):
    params = artifacts["params"]
    gen_cache = GenerationCache()
    caller = caller or _LookaheadCaller()
    qwen = qwen or _ContentQwen()
    return run_quarantined_retry(
        chunk_ids=["chunk0001"],
        source=artifacts["source"],
        snapshot=artifacts["snapshot"],
        chunk_plan=artifacts["chunk_plan"],
        config=artifacts["config"],
        det_data_base=DeterministicGateData(),
        risk_by_chunk=artifacts["risk_by_chunk"],
        glossary=artifacts["glossary"],
        selected_text_by_chunk={},
        generation_params=params,
        model_caller=caller,
        gen_cache=gen_cache,
        qwen_evaluator=qwen,
        gemma_selector=StubGemma(),
        prior_attempts=prior,
    ), caller


def test_retry_selected_replaces_best_variant(artifacts):
    result, caller = _run_retry(artifacts)
    assert result.selected_chunk_ids == ("chunk0001",)
    assert result.quarantined_final_chunk_ids == ()
    assert result.retry_attempts == 1
    assert result.quarantined_final is False
    # The look-ahead right_context was actually fed to generation.
    assert caller.calls
    assert caller.calls[0].right_context
    # The winner is reconstructible and owned by chunk0001.
    chunk_id, candidate = result.candidates[0]
    assert chunk_id == "chunk0001"
    assert candidate.chunk_id == "chunk0001"
    assert candidate.pid_order() == artifacts["chunk_plan"].chunk("chunk0001").pids
    attempt = result.attempts[0]
    assert attempt.outcome == OUTCOME_SELECTED
    assert attempt.selected_candidate_id == candidate.candidate_id
    # The generation record carries the winner with the cascade trace, so a
    # resumed Step 6 best-variant prefers it (more passed gates).
    record = result.generation_records[0]
    winner_payload = record["candidates"][candidate.role]
    assert winner_payload["candidate_id"] == candidate.candidate_id
    assert any(
        gate["gate"] == "qwen_fidelity" and gate["passed"]
        for gate in winner_payload["decision_trace"]
    )


def test_retry_still_quarantined_is_quarantined_final(artifacts):
    result, caller = _run_retry(artifacts, caller=_BadCaller())
    assert result.selected_chunk_ids == ()
    assert result.quarantined_final_chunk_ids == ("chunk0001",)
    assert result.quarantined_final is True
    assert result.attempts[0].outcome == OUTCOME_QUARANTINED_FINAL
    assert caller.calls  # regeneration was attempted


def test_retry_generation_incomplete_is_final(artifacts):
    result, _caller = _run_retry(artifacts, caller=_TruncatedCaller())
    assert result.selected_chunk_ids == ()
    assert result.quarantined_final_chunk_ids == ("chunk0001",)
    assert result.attempts[0].outcome == OUTCOME_GENERATION_INCOMPLETE


def test_retry_resume_reuses_prior_attempt(artifacts):
    first, _c1 = _run_retry(artifacts)
    assert first.attempts[0].reused is False

    prior = {attempt.chunk_id: attempt for attempt in first.attempts}

    class _BoomCaller:
        def __init__(self) -> None:
            self.calls: list = []

        def __call__(self, bundle):
            self.calls.append(bundle)
            raise AssertionError("resume must not regenerate a reused attempt")

    second, caller = _run_retry(artifacts, caller=_BoomCaller(), prior=prior)
    assert second.attempts[0].reused is True
    assert second.selected_chunk_ids == ("chunk0001",)
    assert second.candidates  # candidate reconstructed from the persisted record
    assert caller.calls == []


def test_retry_reuses_prior_quarantined_final_without_regenerating(artifacts):
    first, _c1 = _run_retry(artifacts, caller=_BadCaller())
    assert first.attempts[0].outcome == OUTCOME_QUARANTINED_FINAL

    class _BoomCaller:
        def __init__(self) -> None:
            self.calls: list = []

        def __call__(self, bundle):
            self.calls.append(bundle)
            raise AssertionError("resume must not regenerate a reused attempt")

    prior = {attempt.chunk_id: attempt for attempt in first.attempts}
    second, caller = _run_retry(artifacts, caller=_BoomCaller(), prior=prior)
    assert second.attempts[0].reused is True
    assert second.attempts[0].outcome == OUTCOME_QUARANTINED_FINAL
    assert second.quarantined_final_chunk_ids == ("chunk0001",)
    assert caller.calls == []


def test_retry_attempt_roundtrip_payload():
    attempt = QuarantinedRetryAttempt(
        chunk_id="chunk0001",
        attempt=1,
        outcome=OUTCOME_SELECTED,
        candidate_ids=("c1",),
        selected_candidate_id="c1",
        selected_role="fidelity_first",
        decision_trace=(GateResult(gate="qwen_fidelity", passed=True, detail="ok"),),
        serialized_candidate={"candidate_id": "c1", "role": "fidelity_first", "translation": {}},
    )
    restored = QuarantinedRetryAttempt.from_payload(attempt.to_payload())
    assert restored == attempt


# ---------------------------------------------------------------------------
# Cumulative generation-record merge
# ---------------------------------------------------------------------------


def test_merge_retry_generation_records_unions_candidates():
    existing = [
        {
            "chunk_id": "chunk0001",
            "status": "complete",
            "candidates": {
                "fidelity_first": {
                    "candidate_id": "orig",
                    "role": "fidelity_first",
                    "translation": {"p00001": "старо"},
                    "decision_trace": [],
                }
            },
            "errors": {},
        }
    ]
    retry = [
        {
            "chunk_id": "chunk0001",
            "status": "complete",
            "candidates": {
                "fidelity_first": {
                    "candidate_id": "retry",
                    "role": "fidelity_first",
                    "translation": {"p00001": "ново"},
                    "decision_trace": [{"gate": "qwen_fidelity", "passed": True, "detail": ""}],
                }
            },
            "errors": {},
        }
    ]
    merged = merge_retry_generation_records(existing, retry)
    assert len(merged) == 1
    assert merged[0]["candidates"]["fidelity_first"]["candidate_id"] == "retry"


def test_merge_retry_generation_records_appends_new_chunk():
    merged = merge_retry_generation_records([], [
        {"chunk_id": "chunk0001", "status": "complete", "candidates": {}, "errors": {}}
    ])
    assert [rec["chunk_id"] for rec in merged] == ["chunk0001"]


# ---------------------------------------------------------------------------
# Backend-neutrality (dual-mode import guard)
# ---------------------------------------------------------------------------


def test_quarantined_retry_module_does_not_import_local_lifecycle():
    # Inspect actual import statements (AST), not docstrings, so a doc note
    # mentioning "model_lifecycle" cannot trip the guard — same pattern as
    # ``test_repair_module_does_not_import_local_lifecycle``.
    import ast as _ast
    import inspect as _inspect

    import pact_v4.phase4.quarantined_retry as module

    source = _inspect.getsource(module)
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
            f"quarantined_retry must not reference local lifecycle/transport: {forbidden}"
        )
    for forbidden in ("LifecycleModelCaller", "LifecycleQwenEvaluator", "ModelRouter"):
        assert forbidden not in " ".join(imports)
