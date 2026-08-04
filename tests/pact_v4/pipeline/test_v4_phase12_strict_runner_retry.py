"""V4 B6 integration tests: the separate quarantined-retry cycle in the driver.

These run the full ``run_chapter_strict`` driver with Phase 4 repair adapters
injected, verifying:

  * a quarantined chunk with repair debt (its best-variant failed the audit and
    B2 repair could not close the finding) triggers the separate bounded retry
    cycle: the chunk is regenerated with look-ahead right_context and the
    winner replaces the best-variant, unlocking ``complete``;
  * a chunk that still fails the retry cascade is accepted as final with its
    best-variant (``quarantined_final: true``), terminal ``accepted_degraded``;
  * the ``quarantined_retry.json`` history and the ``repair_report.json``
    additions (``quarantined_final`` / ``retry_attempts``) are persisted;
  * the retry candidates are merged into the cumulative
    ``generation_outcomes.json``;
  * resume reuses a prior attempt instead of re-paying the regeneration;
  * the cycle does not fire at all when there is nothing to retry.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from pact_v4.phase1.models import GateResult
from pact_v4.pipeline.v4_phase12_strict_runner import (
    StrictBackendConfig,
    StrictRunConfig,
    run_chapter_strict,
)
from tests.pact_v4.pipeline.test_v4_phase12_strict_runner import (
    StubGemma,
    StubGemmaAudit,
    StubQwenAudit,
    StubRegionGate,
    _LifecycleAwareGemmaAudit,
    _LifecycleAwareGemmaSelector,
    _LifecycleAwareModelCaller,
    _LifecycleAwareQwenAudit,
    _make_router,
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


def _make_backend() -> StrictBackendConfig:
    return StrictBackendConfig(
        exe=Path("C:/fake/llama-server.exe"), device="FAKE0", host="127.0.0.1",
        model_paths={"gemma": Path("C:/fake/gemma.gguf"), "qwen": Path("C:/fake/qwen.gguf")},
        model_names={"gemma": "gemma-fake", "qwen": "qwen-fake"},
        server_args={"gemma": [], "qwen": []}, port=0,
    )


def _make_cfg(tmp_path: Path, *, n_paragraphs: int = 24) -> StrictRunConfig:
    chapter_html = tmp_path / "046.html"
    memory_dir = tmp_path / "memory"
    out_dir = tmp_path / "out"
    _write_chapter_html(chapter_html, n_paragraphs)
    _write_empty_memory(memory_dir)
    return StrictRunConfig(
        chapter_id="046", chapter_html_path=chapter_html, memory_dir=memory_dir,
        out_dir=out_dir, backend=_make_backend(),
        max_consecutive_terminal_nonselections=3,
    )


def _render_translation(bundle, *, marker: str) -> Dict[str, str]:
    """Translate like StubModelCaller but with a per-chunk marker prefix."""
    out: Dict[str, str] = {}
    for index, (pid, text) in enumerate(bundle.owned_source, start=1):
        digits = "".join(ch for ch in text if ch.isdigit())
        digit_part = f" ({digits})" if digits else ""
        out[pid] = f"{marker} номер{index}{digit_part}"
    return out


class LookaheadChunkCaller:
    """chunk0001's original (no look-ahead) -> bad text; chunk0002 or any
    retry with look-ahead right_context -> good text.

    Mirrors StubModelCaller's digit handling so the deterministic gate sees
    the source digits in the target.
    """

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, bundle) -> str:
        self.calls.append(bundle)
        if bundle.chunk_id == "chunk0001" and not bundle.right_context:
            out = _render_translation(bundle, marker="Плохой перевод")
        else:
            out = _render_translation(bundle, marker="Хороший перевод")
        return json.dumps(out, ensure_ascii=False)


class AlwaysBadChunk1Caller:
    """chunk0001 stays bad even with look-ahead; chunk0002 is always good."""

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, bundle) -> str:
        self.calls.append(bundle)
        if bundle.chunk_id == "chunk0001":
            out = _render_translation(bundle, marker="Плохой перевод")
        else:
            out = _render_translation(bundle, marker="Хороший перевод")
        return json.dumps(out, ensure_ascii=False)


class ContentQwen:
    """Qwen fidelity gate: bad text fails, good text passes."""

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, source, translation) -> GateResult:
        self.calls.append((dict(source), dict(translation)))
        text = " ".join(translation.values())
        if "Хороший" in text:
            return GateResult(gate="qwen_fidelity", passed=True, detail="clean")
        return GateResult(gate="qwen_fidelity", passed=False, detail="bad text")


class ContentAudit(StubQwenAudit):
    """Step 6 / re-audit Qwen: flag the bad best-variant text only."""

    def __call__(self, *, chunk_id, source, translation) -> str:
        self.calls.append((chunk_id, dict(source), dict(translation)))
        text = " ".join(translation.values())
        if "Плохой" in text:
            pid = next(iter(translation))
            return json.dumps({"issues": [
                {"pid": pid, "category": "omission", "note": "dropped clause"}
            ]})
        return json.dumps({"issues": []})


class StubRepairCaller:
    """Fake Phase 4A repair caller (fixed repaired text for the region PID)."""

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, *, chunk_id, source, translation, region, findings) -> str:
        self.calls.append((chunk_id, region.pid))
        pid = region.pid
        digits = "".join(ch for ch in source.get(pid, "") if ch.isdigit())
        digit_part = f" ({digits})" if digits else ""
        return json.dumps(
            {"repaired": {pid: f"Исправленный перевод{digit_part}"}, "reason": "scripted"},
            ensure_ascii=False,
        )


class CannedFormattingCaller:
    """Fake Phase 5 ``FormattingCaller``: map each unresolved span to a word.

    Mirrors ``test_v4_phase12_strict_runner_formatting``'s canned caller so a
    formatting-aware retry run exercises the Phase 5 re-run path (the retry
    re-runs formatting over the updated text and re-writes the artifacts).
    """

    def __init__(self, *, empty: bool = False) -> None:
        self.empty = empty
        self.calls: list = []

    def __call__(self, *, pid, source_text, translation, spans) -> str:
        self.calls.append((pid, translation))
        words = translation.split()
        mappings = []
        for index, span in enumerate(spans):
            target = "" if self.empty else (words[index] if index < len(words) else "")
            mappings.append({
                "pid": pid, "span_id": span["span_id"],
                "target_text": target, "occurrence": 1,
            })
        return json.dumps({"mappings": mappings}, ensure_ascii=False)


def _make_cfg_with_spans(tmp_path: Path, *, n_paragraphs: int = 24) -> StrictRunConfig:
    """Chapter whose paragraphs carry an inline ``<em>`` span (span contract)."""
    chapter_html = tmp_path / "046.html"
    memory_dir = tmp_path / "memory"
    _write_empty_memory(memory_dir)
    words = [f"word{i}" for i in range(WORDS_PER_PARAGRAPH)]
    words[5] = "<em>emphasized</em>"
    paragraph_text = " ".join(words)
    body = "\n".join(f"<p>{paragraph_text}</p>" for _ in range(n_paragraphs))
    chapter_html.write_text("<html><body>" + body + "</body></html>", encoding="utf-8")
    return StrictRunConfig(
        chapter_id="046", chapter_html_path=chapter_html, memory_dir=memory_dir,
        out_dir=tmp_path / "out", backend=_make_backend(),
        max_consecutive_terminal_nonselections=3,
    )


def _run_with_retry(cfg: StrictRunConfig, *, caller: Any = None,
                    formatting_adapters: Any = None):
    """Run the strict driver with Phase 4 repair adapters + the B6 stubs.

    The repair re-gate is forced to fail (``StubRegionGate(passed=False)``),
    so a quarantined chunk's best-variant repair can never commit — that is
    the repair debt that triggers the separate retry cycle.
    """
    router = _make_router()
    inner = caller or LookaheadChunkCaller()
    model_caller = _LifecycleAwareModelCaller(router, inner)
    qwen_audit = ContentAudit()
    result = run_chapter_strict(
        cfg, router=router,
        model_caller=model_caller,
        qwen_evaluator=ContentQwen(),
        gemma_selector=_LifecycleAwareGemmaSelector(router, StubGemma()),
        qwen_audit_evaluator=_LifecycleAwareQwenAudit(router, qwen_audit),
        gemma_audit_evaluator=_LifecycleAwareGemmaAudit(router, StubGemmaAudit()),
        repair_adapters=(
            StubRepairCaller(),
            StubRegionGate(passed=False, reason="re-gate fails"),
            _LifecycleAwareQwenAudit(router, StubQwenAudit()),
            _LifecycleAwareGemmaAudit(router, StubGemmaAudit()),
        ),
        formatting_adapters=formatting_adapters,
    )
    return result, router, inner, qwen_audit


def _load_report(cfg: StrictRunConfig) -> Dict[str, Any]:
    return json.loads(
        (cfg.out_dir / "repair_report.json").read_text(encoding="utf-8")
    )


def _load_retry_history(cfg: StrictRunConfig) -> Dict[str, Any]:
    path = cfg.out_dir / "quarantined_retry.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Retry unlocks complete
# ---------------------------------------------------------------------------


def test_quarantined_retry_unlocks_complete(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=24)
    result, _router, caller, _audit = _run_with_retry(cfg)

    # Phase 1-2: chunk0001 quarantined (bad text), chunk0002 selected.
    assert result.quarantined_count == 1
    assert result.selected_count == 1
    # The retry cycle ran and unlocked the previously-quarantined chunk.
    retry_block = result.step7["quarantined_retry"]
    assert retry_block["status"] == "ran"
    assert retry_block["selected_chunk_ids"] == ["chunk0001"]
    assert retry_block["quarantined_final_chunk_ids"] == []
    assert retry_block["retry_attempts"] == 1
    # The retry regeneration actually saw the look-ahead (next chunk source).
    retry_call = next(c for c in caller.calls if c.chunk_id == "chunk0001" and c.right_context)
    assert retry_call.right_context

    # The terminal moved to complete only because the retry replaced the
    # best-variant (step8 = final terminal after the retry).
    assert result.step8["status"] == "complete"
    assert retry_block["terminal"] == "complete"

    # Artifacts: quarantined_retry.json history + repair_report additions.
    history = _load_retry_history(cfg)
    assert history["schema"] == "pact-v4-phase4-quarantined-retry/v1"
    assert history["attempts"][0]["chunk_id"] == "chunk0001"
    assert history["attempts"][0]["outcome"] == "selected"
    report = _load_report(cfg)
    assert report["status"] == "complete"
    assert report["quarantined_final"] is False
    assert report["retry_attempts"] == 1
    assert report["quarantined_retry"]["selected_chunk_ids"] == ["chunk0001"]

    # The retry candidate is merged into the cumulative generation_outcomes.
    gen = json.loads(
        (cfg.out_dir / "generation_outcomes.json").read_text(encoding="utf-8")
    )
    chunk1 = next(rec for rec in gen["outcomes"] if rec["chunk_id"] == "chunk0001")
    retry_winner = next(
        variant for variant in chunk1["candidates"].values()
        if variant["candidate_id"] == history["attempts"][0]["selected_candidate_id"]
    )
    assert retry_winner["role"] == "fidelity_first"
    assert any(
        gate["gate"] == "qwen_fidelity" and gate["passed"]
        for gate in retry_winner["decision_trace"]
    )


# ---------------------------------------------------------------------------
# Fallback: still quarantined -> quarantined_final, accepted_degraded
# ---------------------------------------------------------------------------


def test_quarantined_retry_fallback_marks_final(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=24)
    result, _router, _caller, _audit = _run_with_retry(
        cfg, caller=AlwaysBadChunk1Caller(),
    )
    assert result.step7["quarantined_retry"]["status"] == "ran"
    retry_block = result.step7["quarantined_retry"]
    assert retry_block["selected_chunk_ids"] == []
    assert retry_block["quarantined_final_chunk_ids"] == ["chunk0001"]
    assert retry_block["quarantined_final"] is True
    # Debt stays (the chunk is accepted as final with its best-variant).
    assert result.step8["status"] == "accepted_degraded"

    history = _load_retry_history(cfg)
    assert history["attempts"][0]["outcome"] == "quarantined_final"
    report = _load_report(cfg)
    assert report["status"] == "accepted_degraded"
    assert report["quarantined_final"] is True
    assert report["retry_attempts"] == 1
    assert any("chunk0001" in debt for debt in report["debt_trace"])


# ---------------------------------------------------------------------------
# Resume reuses a prior attempt (no re-paid regeneration)
# ---------------------------------------------------------------------------


def test_quarantined_retry_resume_reuses_prior_attempt(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=24)
    first_result, _r1, _c1, _audit1 = _run_with_retry(cfg)
    assert first_result.step8["status"] == "complete"
    # The first run's Step 6 audit evaluator saw the quarantined chunk; record
    # that call count so the resume's zero-call assertion below is meaningful
    # (not trivially zero because the stub never logged).
    first_step6_audit_calls = len(_audit1.calls)

    resumed_cfg = StrictRunConfig(
        chapter_id=cfg.chapter_id, chapter_html_path=cfg.chapter_html_path,
        memory_dir=cfg.memory_dir, out_dir=cfg.out_dir, backend=cfg.backend,
    )
    second_result, _r2, caller2, qwen_audit2 = _run_with_retry(resumed_cfg)
    assert second_result.resumed_from_index == 2
    # The retry reuses the prior session's selected attempt: the generation
    # caller is never invoked during the resumed run (Phase 1-2 skipped all
    # journaled chunks, Step 6/7 reuse their caches, the retry reuses history).
    assert caller2.calls == []
    # Resume gate (V4 B6 owner decision 2026-08-04): with prior attempts
    # recorded and no fresh debt, the re-audit is provably identical (no text
    # changed), so the cycle must NOT call the Qwen audit evaluator again.
    # Any calls beyond the first session's count would be the elided re-audit
    # path (one per retried chunk — chunk0001 only here).
    assert len(qwen_audit2.calls) == first_step6_audit_calls
    retry_block = second_result.step7["quarantined_retry"]
    assert retry_block["status"] == "ran"
    assert retry_block["selected_chunk_ids"] == ["chunk0001"]
    assert retry_block["attempts"][0]["reused"] is True
    # Markers restored from prior attempts: terminal and report must agree
    # with the first run's outcome, not the pre-retry defaults.
    assert second_result.step8["status"] == "complete"
    second_report = _load_report(resumed_cfg)
    assert second_report["status"] == "complete"
    assert second_report["quarantined_final"] is False
    assert second_report["retry_attempts"] == 1
    assert second_report["quarantined_retry"]["selected_chunk_ids"] == ["chunk0001"]

    history = _load_retry_history(resumed_cfg)
    assert history["attempts"][0]["reused"] is True
    assert history["attempts"][0]["selected_candidate_id"] == (
        _load_retry_history(cfg)["attempts"][0]["selected_candidate_id"]
    )


# ---------------------------------------------------------------------------
# No retry when there is nothing to retry
# ---------------------------------------------------------------------------


def test_quarantined_retry_not_fired_without_quarantined_debt(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=8)

    class _AllGoodCaller:
        def __call__(self, bundle) -> str:
            return json.dumps(
                _render_translation(bundle, marker="Хороший перевод"),
                ensure_ascii=False,
            )

    result, _router, _caller, _audit = _run_with_retry(cfg, caller=_AllGoodCaller())
    # All chunks selected (no quarantine, no repair debt) -> no retry cycle.
    assert result.selected_count == result.chunk_count
    assert "quarantined_retry" not in result.step7
    assert not (cfg.out_dir / "quarantined_retry.json").exists()
    report = _load_report(cfg)
    assert report["quarantined_final"] is False
    assert report["retry_attempts"] == 0


# ---------------------------------------------------------------------------
# Formatting re-run: the retry re-applies Phase 5 over the updated text
# ---------------------------------------------------------------------------


def test_quarantined_retry_reruns_formatting_over_updated_text(tmp_path: Path):
    # Phase 5 formatting (B3) is configured, so after the retry replaces the
    # best-variant the formatting step re-runs over the updated chapter text;
    # the repair report's formatting block and formatting_report.json must
    # carry the re-run outcome (not the pre-retry one).
    cfg = _make_cfg_with_spans(tmp_path, n_paragraphs=24)
    formatting_caller = CannedFormattingCaller()
    result, _router, _caller, _audit = _run_with_retry(
        cfg, formatting_adapters=(formatting_caller,),
    )
    # The retry succeeded (chunk0001 unlocked) and formatting re-ran.
    assert result.step7["quarantined_retry"]["status"] == "ran"
    assert result.step7["quarantined_retry"]["selected_chunk_ids"] == ["chunk0001"]
    assert formatting_caller.calls  # the formatting caller was actually invoked

    report = _load_report(cfg)
    assert report["status"] == result.step8["status"]
    assert report["formatting"]["schema"] == "pact-v4-formatting-outcome/v1"
    assert report["formatting"]["resolved_count"] > 0
    # The final translation is the formatted text (restored <em> markup).
    final_texts = [text for _pid, text in report["final_translation"]]
    assert any("<em>" in text for text in final_texts)

    fmt_report = json.loads(
        (cfg.out_dir / "formatting_report.json").read_text(encoding="utf-8")
    )
    assert fmt_report["outcome"]["resolved_count"] == report["formatting"]["resolved_count"]
    assert fmt_report["backend_identity_hash"] == cfg.backend.identity_hash
    # step8's formatting block mirrors the re-run outcome, not the pre-retry one.
    assert result.step8["formatting"]["incident_count"] == report["formatting"]["incident_count"]
