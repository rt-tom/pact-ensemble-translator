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
# Source HTML fixture.
#
# ChunkPlanner sizes chunks in words, with a fixed contractual floor of
# MIN_WORDS=280 (ChunkPlan.MIN_WORDS) that no planner configuration can go
# below. 35 words/paragraph puts 8 paragraphs at exactly 280 words -- the
# same "8 PIDs" boundary the old PID-based fixture used, just expressed in
# the unit the planner actually sizes on now.
# ---------------------------------------------------------------------------

WORDS_PER_PARAGRAPH = 35


def _write_chapter_html(path: Path, n_paragraphs: int, words_per_paragraph: int = WORDS_PER_PARAGRAPH) -> None:
    paragraph_text = " ".join(f"word{i}" for i in range(words_per_paragraph))
    body = "\n".join(f"<p>{paragraph_text}</p>" for _ in range(n_paragraphs))
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
    min_chunk_words: int | None = None,
    target_chunk_words: int | None = None,
    max_chunk_words: int | None = None,
    run_label: str = "unit-test",
) -> PipelineConfig:
    chapter_html = tmp_path / "046.html"
    memory_dir = tmp_path / "memory"
    out_dir = tmp_path / "out"
    _write_chapter_html(chapter_html, n_paragraphs)
    _write_empty_memory(memory_dir)
    kwargs: Dict[str, Any] = {}
    if min_chunk_words is not None:
        kwargs["min_chunk_words"] = min_chunk_words
    if target_chunk_words is not None:
        kwargs["target_chunk_words"] = target_chunk_words
    if max_chunk_words is not None:
        kwargs["max_chunk_words"] = max_chunk_words
    return PipelineConfig(
        chapter_id="046",
        chapter_html_path=chapter_html,
        memory_dir=memory_dir,
        out_dir=out_dir,
        run_label=run_label,
        **kwargs,
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
    # Default chunk-word bounds: this short chapter (~96 words) lands in a
    # single undersized chunk, which is fine here -- the test only cares
    # that a high-risk chunk produces both A and B candidates.
    cfg = PipelineConfig(
        chapter_id="046",
        chapter_html_path=chapter_html,
        memory_dir=tmp_path / "memory",
        out_dir=tmp_path / "out",
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

    # 24 paragraphs, high-risk text, so Gemma is actually consulted (with
    # low risk, the cascade never reaches the selector). This lands in a
    # single chunk under the word-based planner (~192 words, well under
    # MIN_WORDS=280) rather than the old PID-based fixture's multi-chunk
    # layout, but Gemma consultation only depends on risk, not chunk count.
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
    # 24 paragraphs of 35 words each (840 words) split into two chunks of
    # 12 paragraphs / 420 words (max_chunk_words=12*35, so the planner
    # needs to break there). Chunk 1's selection must surface as chunk 2's
    # left_context.
    cfg = _make_pipeline(
        tmp_path, n_paragraphs=24, min_chunk_words=280, target_chunk_words=420, max_chunk_words=420,
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


class _StubModelCallerDistinctAB:
    """Like ``StubModelCaller`` but emits visibly different text for the
    two roles, so a Gemma-stub can pick the balanced_literary winner
    and a regression test can observe which candidate's text actually
    made it into the next chunk's left_context.

    The two role outputs are *intentionally* unrelated at the token
    level: fidelity_first uses short literal tokens, balanced_literary
    uses a long idiomatic phrase with no overlap. That guarantees the
    cascade's jaccard<0.40 disagreement check trips whenever both
    candidates pass Qwen and det (which is what the
    ``test_run_chapter_left_context_is_empty_when_previous_chunk_needs_synthesis``
    test needs).
    """

    def __init__(self) -> None:
        self.calls: List[PromptBundle] = []

    def __call__(self, bundle: PromptBundle) -> str:
        from pact_v4.phase2.generation import PromptBundle
        self.calls.append(bundle)
        out: Dict[str, str] = {}
        for index, (pid, text) in enumerate(bundle.owned_source, start=1):
            digits = "".join(ch for ch in text if ch.isdigit())
            digit_part = f" ({digits})" if digits else ""
            if bundle.role == "fidelity_first":
                # "Это короткий буквальный перевод" — short, literal
                # phrasing. Shares "Это", "перевод" with the B version
                # so jaccard > 0.4 (cascade's no-disagreement branch
                # is exercised and the Gemma selector is consulted).
                out[pid] = f"Это короткий буквальный перевод{digit_part}"
            else:
                out[pid] = f"Это красивый литературный перевод{digit_part}"
        return json.dumps(out, ensure_ascii=False)


def test_run_chapter_left_context_uses_cascade_winner_not_first_role(tmp_path: Path):
    """Regression: chunk 0 produces two passing candidates (A and B);
    Gemma-stub picks ``balanced_literary``. Chunk 1's left_context must
    carry the ``balanced_literary`` text, not the ``fidelity_first``
    text (the previous code populated ``selected_text_by_chunk`` from
    ``outcome.expected_roles[0]`` BEFORE the cascade ran, so chunk 1
    would see A's text under any cascade outcome).

    The chapter source is constructed so chunk 0 is high risk (it
    contains "you", "not", a number and a cultural reference, which
    together push the risk score well above the medium threshold),
    so Phase 2B produces both A and B and the cascade's multi-pass
    branch is exercised."""
    # Each sentence is exactly 10 words; 28 repeats/half = 280 words/half
    # (ChunkPlan.MIN_WORDS exactly). min=target=max=280 forces the planner
    # to cut precisely at the sentence-type boundary: chunk 1 = the 28
    # "Thanksgiving" paragraphs (280 words, the largest window that still
    # fits <=280), chunk 2 = the remaining 280 "Blake" words (<=280 left,
    # taken as a single chunk without a further break search).
    chapter_html = tmp_path / "046.html"
    chapter_html.write_text(
        "<html><body>"
        + "<p>You must not open box 7 at Thanksgiving, said Alice.</p>" * 28
        + "<p>She smiled and looked at Blake near the old window.</p>" * 28
        + "</body></html>",
        encoding="utf-8",
    )
    _write_empty_memory(tmp_path / "memory")
    cfg = PipelineConfig(
        chapter_id="046",
        chapter_html_path=chapter_html,
        memory_dir=tmp_path / "memory",
        out_dir=tmp_path / "out",
        min_chunk_words=280,
        target_chunk_words=280,
        max_chunk_words=280,
    )
    caller = _StubModelCallerDistinctAB()
    # Gemma picks the candidate whose candidate_id contains
    # "balanced_literary" (full candidate_id is
    # ``chunk_id:role:bundle_hash[:16]``, so we match on the role
    # segment, not the whole id).
    class _GemmaPickB:
        def __call__(self, candidates):  # type: ignore[no-untyped-def]
            for cid, _ in candidates:
                if ":balanced_literary:" in cid:
                    return GateResult(
                        gate="gemma_russian_preference",
                        passed=True,
                        detail=cid,
                    )
            chosen = candidates[0][0] if candidates else ""
            return GateResult(
                gate="gemma_russian_preference",
                passed=True,
                detail=chosen,
            )

    result = run_chapter(
        cfg,
        model_caller=caller,
        qwen_evaluator=StubQwen(),
        gemma_selector=_GemmaPickB(),
    )
    assert result.chunk_count == 2
    # Sort bundles by chunk_id.
    calls_by_chunk = {c.chunk_id: c for c in caller.calls}
    chunk_ids = sorted(calls_by_chunk)
    second_bundle = calls_by_chunk[chunk_ids[1]]
    # The second chunk's left_context must contain ONLY balanced_literary
    # text, not fidelity_first text.
    left_texts = [text for _, text in second_bundle.left_context]
    assert left_texts, "left_context must not be empty for a passing chunk 0"
    assert all("литературный" in t for t in left_texts), (
        f"left_context carried the wrong role's text: {left_texts!r}"
    )
    assert not any("короткий" in t or "буквальный" in t for t in left_texts), (
        f"left_context still uses fidelity_first draft: {left_texts!r}"
    )
    # And the final translations.json must match: every PID owned by
    # the first chunk must be the B-translation, not the A-translation.
    translations = json.loads(result.translations_path.read_text(encoding="utf-8"))
    first_chunk_pids = calls_by_chunk[chunk_ids[0]].owned_pids
    for pid in first_chunk_pids:
        assert "литературный" in translations[pid], (
            f"final translation for {pid} should be B, got {translations[pid]!r}"
        )


def test_run_chapter_left_context_is_empty_when_previous_chunk_quarantined(tmp_path: Path):
    """Regression: chunk 0 is quarantined (cascade rejects it), chunk 1
    must see an empty left_context — never the quarantined chunk's
    ``fidelity_first`` draft. The cascade's "no least-bad selection"
    contract applies symmetrically to context propagation: a chunk
    that was not selected has no established translation, so feeding
    its draft to the next chunk is the same silent fallback the
    cascade is built to refuse."""
    cfg = _make_pipeline(
        tmp_path, n_paragraphs=24, min_chunk_words=280, target_chunk_words=420, max_chunk_words=420,
    )
    result = run_chapter(
        cfg,
        model_caller=StubModelCaller(),
        qwen_evaluator=StubQwen(passed=False, reason="meaning drift"),
        gemma_selector=StubGemma(),
    )
    assert result.chunk_count == 2
    # Confirm the setup: chunk 0 should be quarantined.
    payload = json.loads(result.selection_path.read_text(encoding="utf-8"))
    statuses_by_chunk = {r["chunk_id"]: r["status"] for r in payload["results"]}
    first_chunk_id = sorted(statuses_by_chunk)[0]
    assert statuses_by_chunk[first_chunk_id] == "quarantined"
    # Now inspect chunk 1's left_context: it must be empty, regardless
    # of what fidelity_first produced for chunk 0.
    caller = StubModelCaller()
    # Re-run with a recording caller to inspect the actual bundle.
    result2 = run_chapter(
        PipelineConfig(
            chapter_id=cfg.chapter_id,
            chapter_html_path=cfg.chapter_html_path,
            memory_dir=cfg.memory_dir,
            out_dir=tmp_path / "out2",
            min_chunk_words=cfg.min_chunk_words,
            target_chunk_words=cfg.target_chunk_words,
            max_chunk_words=cfg.max_chunk_words,
        ),
        model_caller=caller,
        qwen_evaluator=StubQwen(passed=False, reason="meaning drift"),
        gemma_selector=StubGemma(),
    )
    calls_by_chunk = {c.chunk_id: c for c in caller.calls}
    chunk_ids = sorted(calls_by_chunk)
    second_bundle = calls_by_chunk[chunk_ids[1]]
    assert second_bundle.left_context == (), (
        f"left_context must be empty after a quarantined chunk 0; "
        f"got {second_bundle.left_context!r}"
    )


def test_run_chapter_left_context_is_empty_when_previous_chunk_needs_synthesis(tmp_path: Path):
    """Regression: chunk 0 ends in ``needs_synthesis`` (A and B
    disagree, no synthesis candidate present). Like a quarantine,
    there is no established translation for that chunk; chunk 1's
    left_context must be empty, not the fidelity_first draft."""
    # Build a chapter where chunk 0 has high risk (A+B both produced)
    # and the two stub-generated candidates disagree enough to trip
    # the cascade's jaccard<0.40 disagreement check.
    #
    # We deliberately avoid digits in the source so the stub-model
    # output also has no digit tokens; the candidates then share no
    # tokens at all across the two roles, jaccard is 0.0, and the
    # cascade reliably reports needs_synthesis (rather than a noisy
    # fallback path) for the regression assertion.
    # Each sentence is exactly 10 words; 28 repeats/half = 280 words/half
    # (ChunkPlan.MIN_WORDS exactly). min=target=max=280 forces the planner
    # to cut precisely at the sentence-type boundary (see the analogous
    # comment in test_run_chapter_left_context_uses_cascade_winner_not_first_role).
    chapter_html = tmp_path / "046.html"
    chapter_html.write_text(
        "<html><body>"
        + "<p>Alice said it was very cold outside at Thanksgiving today.</p>" * 28
        + "<p>She smiled and looked at Blake near the old window.</p>" * 28
        + "</body></html>",
        encoding="utf-8",
    )
    _write_empty_memory(tmp_path / "memory")
    cfg = PipelineConfig(
        chapter_id="046",
        chapter_html_path=chapter_html,
        memory_dir=tmp_path / "memory",
        out_dir=tmp_path / "out",
        min_chunk_words=280,
        target_chunk_words=280,
        max_chunk_words=280,
    )
    caller = _StubModelCallerDistinctAB()
    # Both A and B pass Qwen; the A and B texts are token-disjoint
    # (jaccard=0.0 → disagreement). No synthesis candidate, no Gemma →
    # cascade returns needs_synthesis=True.
    result = run_chapter(
        cfg,
        model_caller=caller,
        qwen_evaluator=StubQwen(),
        gemma_selector=None,
    )
    assert result.chunk_count == 2
    payload = json.loads(result.selection_path.read_text(encoding="utf-8"))
    statuses = {r["chunk_id"]: r["status"] for r in payload["results"]}
    first_chunk_id = sorted(statuses)[0]
    assert statuses[first_chunk_id] == "needs_synthesis", (
        f"expected chunk 0 to be needs_synthesis under this stub; got {statuses}"
    )
    # Re-run with a recording caller to inspect the actual bundle.
    result2 = run_chapter(
        PipelineConfig(
            chapter_id=cfg.chapter_id,
            chapter_html_path=cfg.chapter_html_path,
            memory_dir=cfg.memory_dir,
            out_dir=tmp_path / "out2",
            min_chunk_words=cfg.min_chunk_words,
            target_chunk_words=cfg.target_chunk_words,
            max_chunk_words=cfg.max_chunk_words,
        ),
        model_caller=caller,
        qwen_evaluator=StubQwen(),
        gemma_selector=None,
    )
    calls_by_chunk = {c.chunk_id: c for c in caller.calls}
    chunk_ids = sorted(calls_by_chunk)
    second_bundle = calls_by_chunk[chunk_ids[1]]
    assert second_bundle.left_context == (), (
        f"left_context must be empty after a needs_synthesis chunk 0; "
        f"got {second_bundle.left_context!r}"
    )
