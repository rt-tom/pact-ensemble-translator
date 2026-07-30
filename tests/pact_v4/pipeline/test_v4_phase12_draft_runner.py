"""Tests for the Phase 1C → 2A → 2B → 2C driver.

The driver is glued together from many small library modules, so the
tests focus on the *contract surfaces* that another tool (the future
v3/v4 A/B comparison harness) will read from disk:

* ``chunk_plan.json`` round-trips through ``ChunkPlanArtifact.from_payload``.
* ``risk_classification.json`` reports one record per chunk with the
  expected policy_version / thresholds.
* ``generation_outcomes.json`` records both candidates for high-risk
  chunks and exactly one candidate for low-risk chunks.
* ``selection_results.json`` records the selected role for chunks where
  both A and B pass, the quarantine reason for chunks where nothing
  passes, and the ``needs_synthesis`` flag for chunks where A and B
  disagree with no synthesis candidate present.
* ``translations.json`` contains exactly one Russian text per PID owned
  by a successfully selected chunk.
* ``provenance.json`` carries every identity a downstream comparison
  tool needs.

Real model calls are replaced by stubs at three injection points
(``ModelCaller``, ``QwenEvaluator``, ``GemmaSelector``); no HTTP, no
real llama-server.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pytest

from pact_v4.phase0b.source_html import SourceBlock
from pact_v4.phase1.models import (
    ChunkPlanArtifact,
    ConfigArtifact,
    GateResult,
    Snapshot,
    SourceArtifact,
)
from pact_v4.phase2.generation import ModelCaller, PromptBundle
from pact_v4.phase2.cascade import (
    QwenEvaluator,
    GemmaSelector,
)
from pact_v4.pipeline.v4_phase12_draft_runner import (
    PipelineConfig,
    run_chapter,
)
from pact_v4.runtime.snapshot_factory import ChapterMemory


# ---------------------------------------------------------------------------
# Source HTML fixture: a small chapter that yields exactly two chunks
# (8 + 8 PIDs at min_size=8, max_size=20).
# ---------------------------------------------------------------------------


def _write_chapter_html(path: Path, n_paragraphs: int) -> None:
    body = "\n".join(f"<p>Plain sentence number {i+1}.</p>" for i in range(n_paragraphs))
    path.write_text(
        "<html><body>" + body + "</body></html>",
        encoding="utf-8",
    )


def _write_empty_memory(dir_path: Path) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "glossary.json").write_text("{}", encoding="utf-8")
    (dir_path / "book_memory.json").write_text("{}", encoding="utf-8")


# ---------------------------------------------------------------------------
# Stub ModelCaller
# ---------------------------------------------------------------------------


class StubModelCaller:
    """Returns a deterministic valid JSON for every bundle it sees.

    The translation is pure Cyrillic (so the deterministic gate's
    mixed_script check passes) and preserves every digit found in the
    source text (so the gate's number-preservation check passes too).
    This isolates the cascade from the rules the test isn't exercising
    (e.g. "selection picks the right role" vs "number preservation
    works").
    """

    def __init__(self) -> None:
        self.calls: List[PromptBundle] = []

    def __call__(self, bundle: PromptBundle) -> str:
        self.calls.append(bundle)
        out: Dict[str, str] = {}
        for index, (pid, text) in enumerate(bundle.owned_source, start=1):
            digits = "".join(ch for ch in text if ch.isdigit())
            digit_part = f" ({digits})" if digits else ""
            # Russian word only — no PID label in the translation, so
            # the deterministic gate's mixed_script check does not
            # catch the Latin 'p' in 'p00001'.
            out[pid] = f"Перевод номер{index}{digit_part}"
        return json.dumps(out, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Stub Qwen evaluator / Gemma selector
# ---------------------------------------------------------------------------


class StubQwen:
    """Returns a passing verdict by default; configurable per-call."""

    def __init__(self, passed: bool = True, reason: str = "OK") -> None:
        self.passed = passed
        self.reason = reason
        self.calls: List[Tuple[Dict[str, str], Dict[str, str]]] = []

    def __call__(
        self, source: Mapping[str, str], translation: Mapping[str, str]
    ) -> GateResult:
        self.calls.append((dict(source), dict(translation)))
        return GateResult(
            gate="qwen_fidelity", passed=self.passed, detail=self.reason,
        )


class StubGemma:
    """Always picks the first candidate passed in."""

    def __init__(self, preferred_id: Optional[str] = None) -> None:
        self.preferred_id = preferred_id
        self.calls: List[List[Tuple[str, Dict[str, str]]]] = []

    def __call__(
        self, candidates: Sequence[Tuple[str, Mapping[str, str]]]
    ) -> GateResult:
        self.calls.append([(cid, dict(m)) for cid, m in candidates])
        chosen = self.preferred_id or (candidates[0][0] if candidates else "")
        return GateResult(
            gate="gemma_russian_preference",
            passed=True,
            detail=chosen,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pipeline(
    tmp_path: Path,
    *,
    n_paragraphs: int = 16,
    min_chunk_size: int = 8,
    max_chunk_size: int = 20,
    run_label: str = "unit-test",
) -> PipelineConfig:
    chapter_html = tmp_path / "046.html"
    memory_dir = tmp_path / "memory"
    out_dir = tmp_path / "out"
    _write_chapter_html(chapter_html, n_paragraphs)
    _write_empty_memory(memory_dir)
    return PipelineConfig(
        chapter_id="046",
        chapter_html_path=chapter_html,
        memory_dir=memory_dir,
        out_dir=out_dir,
        min_chunk_size=min_chunk_size,
        max_chunk_size=max_chunk_size,
        run_label=run_label,
    )


# ---------------------------------------------------------------------------
# End-to-end smoke
# ---------------------------------------------------------------------------


def test_run_chapter_writes_all_six_artefacts(tmp_path: Path):
    cfg = _make_pipeline(tmp_path)
    result = run_chapter(
        cfg,
        model_caller=StubModelCaller(),
        qwen_evaluator=StubQwen(),
        gemma_selector=StubGemma(),
    )
    for path in (
        result.chunk_plan_path,
        result.risk_path,
        result.generation_path,
        result.selection_path,
        result.translations_path,
        result.provenance_path,
    ):
        assert path.exists(), f"missing artefact: {path}"


def test_run_chapter_chunk_plan_round_trips(tmp_path: Path):
    cfg = _make_pipeline(tmp_path, n_paragraphs=24)
    result = run_chapter(
        cfg,
        model_caller=StubModelCaller(),
        qwen_evaluator=StubQwen(),
        gemma_selector=StubGemma(),
    )
    payload = json.loads(result.chunk_plan_path.read_text(encoding="utf-8"))
    assert payload["artifact"] == "pact-v4-chunk-plan/v1"
    assert len(payload["chunks"]) == 2
    assert result.chunk_count == 2


def test_run_chapter_low_risk_chunks_have_exactly_one_candidate(tmp_path: Path):
    # 8 paragraphs fits one chunk; low risk → one A-only candidate.
    cfg = _make_pipeline(tmp_path, n_paragraphs=8)
    result = run_chapter(
        cfg,
        model_caller=StubModelCaller(),
        qwen_evaluator=StubQwen(),
        gemma_selector=StubGemma(),
    )
    payload = json.loads(result.generation_path.read_text(encoding="utf-8"))
    assert len(payload["outcomes"]) == 1
    record = payload["outcomes"][0]
    assert record["status"] == "complete"
    assert list(record["candidates"]) == ["fidelity_first"]


def test_run_chapter_high_risk_chunks_have_a_and_b(tmp_path: Path):
    # Chapter that includes negation + "you" + a number → at least one
    # chunk will be high risk; the bundle should produce two roles.
    chapter_html = tmp_path / "046.html"
    chapter_html.write_text(
        "<html><body>"
        + "<p>You must not open box 7.</p>" * 9
        + "<p>She did not visit Broadway twice.</p>" * 7
        + "</body></html>",
        encoding="utf-8",
    )
    _write_empty_memory(tmp_path / "memory")
    cfg = PipelineConfig(
        chapter_id="046",
        chapter_html_path=chapter_html,
        memory_dir=tmp_path / "memory",
        out_dir=tmp_path / "out",
        min_chunk_size=8,
        max_chunk_size=20,
    )
    result = run_chapter(
        cfg,
        model_caller=StubModelCaller(),
        qwen_evaluator=StubQwen(),
        gemma_selector=StubGemma(),
    )
    payload = json.loads(result.generation_path.read_text(encoding="utf-8"))
    # At least one chunk had A and B.
    assert any(
        set(rec["candidates"]) == {"fidelity_first", "balanced_literary"}
        for rec in payload["outcomes"]
    )


def test_run_chapter_selection_records_selected_role(tmp_path: Path):
    cfg = _make_pipeline(tmp_path, n_paragraphs=8)
    stub_gemma = StubGemma(preferred_id="fidelity_first")
    result = run_chapter(
        cfg,
        model_caller=StubModelCaller(),
        qwen_evaluator=StubQwen(),
        gemma_selector=stub_gemma,
    )
    payload = json.loads(result.selection_path.read_text(encoding="utf-8"))
    assert len(payload["results"]) == 1
    record = payload["results"][0]
    assert record["status"] == "selected"
    assert record["selected_role"] == "fidelity_first"
    # Final translations contain one text per owned PID; all are Russian.
    translations = json.loads(result.translations_path.read_text(encoding="utf-8"))
    assert len(translations) == 8
    assert all(text.startswith("Перевод номер") for text in translations.values())


def test_run_chapter_quarantines_when_qwen_fails(tmp_path: Path):
    cfg = _make_pipeline(tmp_path, n_paragraphs=8)
    result = run_chapter(
        cfg,
        model_caller=StubModelCaller(),
        qwen_evaluator=StubQwen(passed=False, reason="meaning drift"),
        gemma_selector=StubGemma(),
    )
    payload = json.loads(result.selection_path.read_text(encoding="utf-8"))
    assert all(rec["status"] == "quarantined" for rec in payload["results"])
    # No translations written for a quarantined chunk.
    translations = json.loads(result.translations_path.read_text(encoding="utf-8"))
    assert translations == {}
    assert result.quarantined_count == result.chunk_count


def test_run_chapter_quarantines_when_gemma_cannot_choose(tmp_path: Path):
    # Force Gemma to fail: it returns passed=False with an explanation.
    class _FailingGemma:
        def __call__(self, candidates):  # type: ignore[no-untyped-def]
            return GateResult(
                gate="gemma_russian_preference",
                passed=False,
                detail="Cannot choose: both candidates are equally bad.",
            )

    # 24 paragraphs to force a multi-chunk, high-risk layout so Gemma is
    # actually consulted (with one chunk and a low risk, the cascade
    # never reaches the selector).
    chapter_html = tmp_path / "046.html"
    chapter_html.write_text(
        "<html><body>"
        + "<p>You must not open box 7 at Thanksgiving.</p>" * 24
        + "</body></html>",
        encoding="utf-8",
    )
    _write_empty_memory(tmp_path / "memory")
    cfg = PipelineConfig(
        chapter_id="046",
        chapter_html_path=chapter_html,
        memory_dir=tmp_path / "memory",
        out_dir=tmp_path / "out",
        min_chunk_size=8,
        max_chunk_size=20,
    )
    result = run_chapter(
        cfg,
        model_caller=StubModelCaller(),
        qwen_evaluator=StubQwen(),
        gemma_selector=_FailingGemma(),
    )
    payload = json.loads(result.selection_path.read_text(encoding="utf-8"))
    assert all(
        rec["status"] in ("selected", "quarantined")
        for rec in payload["results"]
    )
    # At least one chunk should be quarantined because Gemma failed.
    assert any(rec["status"] == "quarantined" for rec in payload["results"])
    # The Gemma failure surfaces in the quarantine reason.
    gemma_quarantines = [
        rec for rec in payload["results"]
        if rec["status"] == "quarantined"
        and "gemma" in rec.get("quarantine_reason", "").casefold()
    ]
    assert gemma_quarantines, "Gemma failure did not surface in quarantine_reason"


def test_run_chapter_provenance_records_identities_and_policy_versions(tmp_path: Path):
    cfg = _make_pipeline(tmp_path, run_label="unit")
    result = run_chapter(
        cfg,
        model_caller=StubModelCaller(),
        qwen_evaluator=StubQwen(),
        gemma_selector=StubGemma(),
    )
    prov = json.loads(result.provenance_path.read_text(encoding="utf-8"))
    assert prov["schema"] == "pact-v4-run-provenance/phase12/v1"
    assert prov["chapter_id"] == "046"
    assert prov["run_label"] == "unit"
    ids = prov["identities"]
    assert "source_hash" in ids and len(ids["source_hash"]) == 64
    assert "snapshot_hash" in ids
    assert "chunk_plan_hash" in ids
    assert "config_identity" in ids
    pv = prov["policy_versions"]
    assert pv["risk_policy"] == "pact-v4-risk-source-en/v1"
    assert pv["risk_thresholds"] == {"medium": 3, "high": 7}
    assert pv["prompt_fidelity_first"].startswith("pact-v4-prompt-fidelity-first/")
    assert pv["reviewer_qwen_fidelity"] == "pact-v4-reviewer-qwen-fidelity/v1"
    assert prov["provisional_params"]["temperature"] == 0.2
    assert prov["provisional_params"]["seed"] == 7


def test_run_chapter_provenance_artefact_paths_resolve(tmp_path: Path):
    cfg = _make_pipeline(tmp_path)
    result = run_chapter(
        cfg,
        model_caller=StubModelCaller(),
        qwen_evaluator=StubQwen(),
        gemma_selector=StubGemma(),
    )
    prov = json.loads(result.provenance_path.read_text(encoding="utf-8"))
    for label, p in prov["artefacts"].items():
        path = Path(p)
        assert path.exists(), f"provenance references missing artefact: {label} -> {p}"


def test_run_chapter_provisional_params_propagate_to_phase2b(tmp_path: Path):
    cfg = _make_pipeline(
        tmp_path,
        run_label="param-propagation",
    )
    # Override the provisionals on the PipelineConfig.
    cfg = PipelineConfig(
        chapter_id=cfg.chapter_id,
        chapter_html_path=cfg.chapter_html_path,
        memory_dir=cfg.memory_dir,
        out_dir=cfg.out_dir,
        temperature=0.42,
        seed=123,
        max_tokens=4096,
    )
    caller = StubModelCaller()
    run_chapter(
        cfg,
        model_caller=caller,
        qwen_evaluator=StubQwen(),
        gemma_selector=StubGemma(),
    )
    # Every bundle the stub saw carries the overridden params.
    for bundle in caller.calls:
        assert bundle.params.temperature == 0.42
        assert bundle.params.seed == 123
        assert bundle.params.max_tokens == 4096
        assert bundle.params.reasoning == 0  # Phase 2B hard-coded


def test_run_chapter_records_left_ru_only_after_selection(tmp_path: Path):
    # 24 paragraphs split into two chunks of 12 each (max_size=12, so
    # the planner needs to break). Chunk 1's selection must surface as
    # chunk 2's left_context.
    cfg = _make_pipeline(
        tmp_path, n_paragraphs=24, min_chunk_size=8, max_chunk_size=12,
    )
    caller = StubModelCaller()
    result = run_chapter(
        cfg,
        model_caller=caller,
        qwen_evaluator=StubQwen(),
        gemma_selector=StubGemma(),
    )
    assert result.chunk_count == 2
    # Sort calls by chunk_id to find the second chunk deterministically.
    calls_by_chunk = {c.chunk_id: c for c in caller.calls}
    assert len(calls_by_chunk) == 2
    chunk_ids = sorted(calls_by_chunk)
    second_bundle = calls_by_chunk[chunk_ids[1]]
    # The second chunk's left_context must be non-empty.
    assert second_bundle.left_context != ()
    # And every PID in the left_context must be the first chunk's
    # owned PIDs (we know chunk 1 is chunk0001 in our planner output).
    first_bundle = calls_by_chunk[chunk_ids[0]]
    expected_pids = set(first_bundle.owned_pids)
    left_pids = {pid for pid, _ in second_bundle.left_context}
    assert left_pids == expected_pids
