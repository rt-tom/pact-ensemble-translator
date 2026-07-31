"""Tests for the sequential-model (generate/select two-pass) driver.

These mirror ``test_v4_phase12_draft_runner.py``'s stub-based approach,
but exercise ``run_generate`` and ``run_select`` as two independent
calls with the artifact hand-off (``generation_bundle.json``) in
between -- there is no in-process ``run_chapter`` call linking them.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from pact_v4.phase1.models import Candidate, GateResult
from pact_v4.phase2.generation import PromptBundle
from pact_v4.pipeline.v4_phase12_sequential_runner import (
    GENERATION_BUNDLE_SCHEMA,
    PROVENANCE_SCHEMA,
    SEQUENTIAL_MODEL_CAVEAT,
    SequentialGenerateConfig,
    SequentialSelectConfig,
    _deserialize_candidate,
    _serialize_candidate,
    run_generate,
    run_select,
)


# ---------------------------------------------------------------------------
# Fixtures shared with the interleaved-driver test suite
# ---------------------------------------------------------------------------


def _write_chapter_html(path: Path, n_paragraphs: int) -> None:
    body = "\n".join(f"<p>Plain sentence number {i+1}.</p>" for i in range(n_paragraphs))
    path.write_text("<html><body>" + body + "</body></html>", encoding="utf-8")


def _write_empty_memory(dir_path: Path) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "glossary.json").write_text("{}", encoding="utf-8")
    (dir_path / "book_memory.json").write_text("{}", encoding="utf-8")


class StubModelCaller:
    def __init__(self) -> None:
        self.calls: List[PromptBundle] = []

    def __call__(self, bundle: PromptBundle) -> str:
        self.calls.append(bundle)
        out: Dict[str, str] = {}
        for index, (pid, text) in enumerate(bundle.owned_source, start=1):
            digits = "".join(ch for ch in text if ch.isdigit())
            digit_part = f" ({digits})" if digits else ""
            out[pid] = f"Перевод номер{index}{digit_part}"
        return json.dumps(out, ensure_ascii=False)


class _StubModelCallerDistinctAB:
    """Distinct A/B text so left_context provenance can be told apart."""

    def __init__(self) -> None:
        self.calls: List[PromptBundle] = []

    def __call__(self, bundle: PromptBundle) -> str:
        self.calls.append(bundle)
        out: Dict[str, str] = {}
        for index, (pid, text) in enumerate(bundle.owned_source, start=1):
            digits = "".join(ch for ch in text if ch.isdigit())
            digit_part = f" ({digits})" if digits else ""
            if bundle.role == "fidelity_first":
                out[pid] = f"Это короткий буквальный перевод{digit_part}"
            else:
                out[pid] = f"Это красивый литературный перевод{digit_part}"
        return json.dumps(out, ensure_ascii=False)


class StubQwen:
    def __init__(self, passed: bool = True, reason: str = "OK") -> None:
        self.passed = passed
        self.reason = reason
        self.calls: List[Tuple[Dict[str, str], Dict[str, str]]] = []

    def __call__(self, source: Mapping[str, str], translation: Mapping[str, str]) -> GateResult:
        self.calls.append((dict(source), dict(translation)))
        return GateResult(gate="qwen_fidelity", passed=self.passed, detail=self.reason)


class StubGemma:
    def __init__(self, preferred_id: Optional[str] = None) -> None:
        self.preferred_id = preferred_id
        self.calls: List[List[Tuple[str, Dict[str, str]]]] = []

    def __call__(self, candidates: Sequence[Tuple[str, Mapping[str, str]]]) -> GateResult:
        self.calls.append([(cid, dict(m)) for cid, m in candidates])
        chosen = self.preferred_id or (candidates[0][0] if candidates else "")
        return GateResult(gate="gemma_russian_preference", passed=True, detail=chosen)


def _make_generate_cfg(
    tmp_path: Path,
    *,
    n_paragraphs: int = 16,
    min_chunk_size: int = 8,
    max_chunk_size: int = 20,
) -> SequentialGenerateConfig:
    chapter_html = tmp_path / "046.html"
    memory_dir = tmp_path / "memory"
    out_dir = tmp_path / "out"
    _write_chapter_html(chapter_html, n_paragraphs)
    _write_empty_memory(memory_dir)
    return SequentialGenerateConfig(
        chapter_id="046",
        chapter_html_path=chapter_html,
        memory_dir=memory_dir,
        out_dir=out_dir,
        min_chunk_size=min_chunk_size,
        max_chunk_size=max_chunk_size,
    )


# ---------------------------------------------------------------------------
# Candidate (de)serialisation round-trip
# ---------------------------------------------------------------------------


def test_candidate_round_trips_through_serialize_deserialize():
    original = Candidate(
        candidate_id="chunk0001:fidelity_first:abcdef0123456789",
        chunk_id="chunk0001",
        role="fidelity_first",
        translation=(("p00001", "Привет"), ("p00002", "Мир")),
        source_hash="a" * 64,
        snapshot_hash="b" * 64,
        chunk_plan_hash="c" * 64,
        config_identity="d" * 64,
        decision_trace=(GateResult(gate="phase2b_prompt_bundle", passed=True, detail="hash"),),
    )
    payload = _serialize_candidate(original)
    # Must round-trip through JSON (the real hand-off is via a JSON file).
    payload = json.loads(json.dumps(payload, ensure_ascii=False))
    restored = _deserialize_candidate(payload)
    assert restored.candidate_id == original.candidate_id
    assert restored.chunk_id == original.chunk_id
    assert restored.role == original.role
    assert restored.translation == original.translation
    assert restored.source_hash == original.source_hash
    assert restored.snapshot_hash == original.snapshot_hash
    assert restored.chunk_plan_hash == original.chunk_plan_hash
    assert restored.config_identity == original.config_identity
    assert len(restored.decision_trace) == 1
    assert restored.decision_trace[0].gate == "phase2b_prompt_bundle"


# ---------------------------------------------------------------------------
# run_generate
# ---------------------------------------------------------------------------


def test_run_generate_writes_bundle_and_side_artefacts(tmp_path: Path):
    cfg = _make_generate_cfg(tmp_path)
    result = run_generate(cfg, model_caller=StubModelCaller())
    assert result.chunk_plan_path.exists()
    assert result.risk_path.exists()
    assert result.generation_bundle_path.exists()

    bundle = json.loads(result.generation_bundle_path.read_text(encoding="utf-8"))
    assert bundle["schema"] == GENERATION_BUNDLE_SCHEMA
    assert bundle["chapter_id"] == "046"
    assert bundle["sequential_model_caveat"] == SEQUENTIAL_MODEL_CAVEAT
    assert "source" in bundle and "chunk_plan" in bundle and "outcomes" in bundle
    assert len(bundle["outcomes"]) == result.chunk_count


def test_run_generate_low_risk_chunk_has_one_candidate(tmp_path: Path):
    cfg = _make_generate_cfg(tmp_path, n_paragraphs=8)
    result = run_generate(cfg, model_caller=StubModelCaller())
    bundle = json.loads(result.generation_bundle_path.read_text(encoding="utf-8"))
    assert len(bundle["outcomes"]) == 1
    outcome = bundle["outcomes"][0]
    assert outcome["status"] == "complete"
    assert list(outcome["candidates"]) == ["fidelity_first"]
    # Every serialised candidate carries the identity fields select needs.
    cand = outcome["candidates"]["fidelity_first"]
    for key in ("candidate_id", "chunk_id", "role", "translation",
                "source_hash", "snapshot_hash", "chunk_plan_hash", "config_identity"):
        assert key in cand


def test_run_generate_left_context_uses_fidelity_first_draft_not_selection(tmp_path: Path):
    """Sequential-model deviation: chunk 1's left_context must be built
    from chunk 0's fidelity_first DRAFT (there is no selection yet on
    this pass), never left empty just because no cascade ran."""
    cfg = _make_generate_cfg(tmp_path, n_paragraphs=24, min_chunk_size=8, max_chunk_size=12)
    caller = _StubModelCallerDistinctAB()
    result = run_generate(cfg, model_caller=caller)
    assert result.chunk_count == 2
    calls_by_chunk = {c.chunk_id: c for c in caller.calls}
    chunk_ids = sorted(calls_by_chunk)
    # There may be 1 or 2 calls per chunk (low vs high risk); pick the
    # fidelity_first call explicitly rather than assuming role order.
    second_chunk_fidelity_calls = [
        c for c in caller.calls
        if c.chunk_id == chunk_ids[1] and c.role == "fidelity_first"
    ]
    assert second_chunk_fidelity_calls, "expected a fidelity_first call for chunk 2"
    second_bundle = second_chunk_fidelity_calls[0]
    assert second_bundle.left_context != ()
    left_texts = [text for _, text in second_bundle.left_context]
    # Only fidelity_first ("короткий буквальный") text may appear, since
    # no selection has happened yet on this pass.
    assert all("буквальный" in t for t in left_texts)


# ---------------------------------------------------------------------------
# run_select
# ---------------------------------------------------------------------------


def _generate_then_select_cfg(
    tmp_path: Path,
    *,
    n_paragraphs: int = 8,
    caller=None,
) -> Tuple[SequentialSelectConfig, Path]:
    gen_cfg = _make_generate_cfg(tmp_path, n_paragraphs=n_paragraphs)
    gen_result = run_generate(gen_cfg, model_caller=caller or StubModelCaller())
    select_out = tmp_path / "out"  # same out_dir as generate, per CLI convention
    select_cfg = SequentialSelectConfig(
        generation_bundle_path=gen_result.generation_bundle_path,
        out_dir=select_out,
    )
    return select_cfg, gen_result.generation_bundle_path


def test_run_select_writes_translations_and_provenance(tmp_path: Path):
    select_cfg, _ = _generate_then_select_cfg(tmp_path)
    result = run_select(
        select_cfg, qwen_evaluator=StubQwen(), gemma_selector=StubGemma(),
    )
    assert result.translations_path.exists()
    assert result.selection_path.exists()
    assert result.provenance_path.exists()

    prov = json.loads(result.provenance_path.read_text(encoding="utf-8"))
    assert prov["schema"] == PROVENANCE_SCHEMA
    assert prov["chapter_id"] == "046"
    assert "source_hash" in prov["identities"]
    assert prov["sequential_model_caveat"] == SEQUENTIAL_MODEL_CAVEAT

    translations = json.loads(result.translations_path.read_text(encoding="utf-8"))
    assert len(translations) == 8
    assert all(text.startswith("Перевод номер") for text in translations.values())


def test_run_select_quarantines_when_qwen_fails(tmp_path: Path):
    select_cfg, _ = _generate_then_select_cfg(tmp_path)
    result = run_select(
        select_cfg,
        qwen_evaluator=StubQwen(passed=False, reason="meaning drift"),
        gemma_selector=StubGemma(),
    )
    payload = json.loads(result.selection_path.read_text(encoding="utf-8"))
    assert all(rec["status"] == "quarantined" for rec in payload["results"])
    translations = json.loads(result.translations_path.read_text(encoding="utf-8"))
    assert translations == {}
    assert result.quarantined_count == result.chunk_count


def test_run_select_works_without_gemma_selector(tmp_path: Path):
    """gemma_selector=None must not error; it falls back to the
    cascade's documented deterministic role-order tie-break."""
    select_cfg, _ = _generate_then_select_cfg(tmp_path)
    result = run_select(select_cfg, qwen_evaluator=StubQwen(), gemma_selector=None)
    assert result.selected_count + result.quarantined_count + result.needs_synthesis_count == result.chunk_count
    translations = json.loads(result.translations_path.read_text(encoding="utf-8"))
    assert len(translations) == 8


def test_run_select_rejects_foreign_bundle_schema(tmp_path: Path):
    bad_bundle_path = tmp_path / "generation_bundle.json"
    bad_bundle_path.write_text(json.dumps({"schema": "not-the-right-schema"}), encoding="utf-8")
    select_cfg = SequentialSelectConfig(
        generation_bundle_path=bad_bundle_path, out_dir=tmp_path / "out",
    )
    try:
        run_select(select_cfg, qwen_evaluator=StubQwen())
        assert False, "expected a ValueError for a foreign bundle schema"
    except ValueError as exc:
        assert "Foreign identity" in str(exc)


# ---------------------------------------------------------------------------
# End-to-end: generate -> select produces a v4_v3_draft_compare-readable pair
# ---------------------------------------------------------------------------


def test_generate_then_select_end_to_end_matches_compare_tool_shape(tmp_path: Path):
    select_cfg, bundle_path = _generate_then_select_cfg(tmp_path, n_paragraphs=16)
    result = run_select(select_cfg, qwen_evaluator=StubQwen(), gemma_selector=StubGemma())

    # Matches what pact_full_pipeline_runner_v1.v4_v3_draft_compare.load_v4_run_outputs expects.
    translations = json.loads(result.translations_path.read_text(encoding="utf-8"))
    assert isinstance(translations, dict)
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in translations.items())

    prov = json.loads(result.provenance_path.read_text(encoding="utf-8"))
    assert prov["schema"] == "pact-v4-run-provenance/phase12/v1"
    assert isinstance(prov.get("identities"), dict)
    assert isinstance(prov.get("policy_versions"), dict)
    assert isinstance(prov.get("provisional_params"), dict)
    assert isinstance(prov.get("counts"), dict)
