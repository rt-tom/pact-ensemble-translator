"""Tests for the strict single-resident chapter driver.

No subprocess, no HTTP, no real ``llama-server``: ``ModelRouter`` is
wired to ``FakeLifecycleAdapter`` (see
``tests/pact_v4/runtime/test_model_lifecycle.py``), and the three
``ModelCaller``/``QwenEvaluator``/``GemmaSelector`` injection points use
the same stub pattern as
``tests/pact_v4/pipeline/test_v4_phase12_draft_runner.py``, wrapped so
each call goes through ``router.ensure_resident(...)`` first -- exactly
what the real ``Lifecycle*`` adapters in
``pact_v4.runtime.model_lifecycle_adapters`` do, just pointed at a stub
instead of a real HTTP client.
"""
from __future__ import annotations

import inspect
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from pact_v4.phase1.models import ChunkPlanArtifact, GateResult
from pact_v4.phase1.chunker import ChunkPlanner
from pact_v4.phase2.generation import PromptBundle
from pact_v4.pipeline import _shared_runner_helpers
from pact_v4.pipeline import v4_phase12_sequential_runner
from pact_v4.pipeline import v4_phase12_strict_runner
from pact_v4.pipeline.v4_phase12_strict_runner import (
    StrictBackendConfig,
    StrictRunConfig,
    _audit_candidate_map,
    run_chapter_strict,
)
from pact_v4.phase0b.source_html import load_source
from pact_v4.runtime.model_lifecycle import ModelRouter
from pact_v4.runtime.snapshot_factory import (
    ChapterMemory,
    build_snapshot,
    build_source_artifact,
)

WORDS_PER_PARAGRAPH = 35


def _write_chapter_html(path: Path, n_paragraphs: int, words_per_paragraph: int = WORDS_PER_PARAGRAPH) -> None:
    paragraph_text = " ".join(f"word{i}" for i in range(words_per_paragraph))
    body = "\n".join(f"<p>{paragraph_text}</p>" for _ in range(n_paragraphs))
    path.write_text("<html><body>" + body + "</body></html>", encoding="utf-8")


def _write_empty_memory(dir_path: Path) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "glossary.json").write_text("{}", encoding="utf-8")
    (dir_path / "book_memory.json").write_text("{}", encoding="utf-8")


# ---------------------------------------------------------------------------
# Fake lifecycle (see tests/pact_v4/runtime/test_model_lifecycle.py)
# ---------------------------------------------------------------------------


class FakeLifecycleAdapter:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, str]] = []

    def start(self, model_key: str, profile: str, extra_args: list, retries: int = 1):
        self.calls.append(("start", model_key))
        return 1.5, 0

    def stop(self):
        self.calls.append(("stop", ""))
        return 0.5, True, 0

    def sample_vram(self) -> int:
        return 1024 * 1024 * 100


def _make_router() -> ModelRouter:
    return ModelRouter(
        FakeLifecycleAdapter(),
        role_profile_names={"gemma": "Gemma", "qwen": "Qwen"},
        role_args={"gemma": [], "qwen": []},
    )


# ---------------------------------------------------------------------------
# Stub model callers, lifecycle-aware (mirrors pact_v4.runtime.model_lifecycle_adapters)
# ---------------------------------------------------------------------------


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


class StubQwenAudit:
    """Phase 3B Step 6 Qwen audit evaluator stub (protocol: ``QwenAuditEvaluator``).

    Returns a scripted ``{"issues": [...]}`` JSON string; when ``fail`` is
    set it raises instead, simulating a model/transport failure that the
    audit layer must record as a failed unit, never as "no issues".
    """

    def __init__(
        self,
        issues_json: Optional[str] = None,
        *,
        fail: Optional[Exception] = None,
    ) -> None:
        self._issues_json = issues_json or json.dumps({"issues": []})
        self.fail = fail
        self.calls: List[Tuple[str, Dict[str, str], Dict[str, str]]] = []

    def __call__(self, *, chunk_id: str, source: Mapping[str, str],
                 translation: Mapping[str, str]) -> str:
        self.calls.append((chunk_id, dict(source), dict(translation)))
        if self.fail is not None:
            raise self.fail
        return self._issues_json


class StubGemmaAudit:
    """Phase 3B Step 6 Gemma audit evaluator stub (protocol: ``GemmaAuditEvaluator``).

    Russian-only by contract: the protocol signature has no ``source``, so a
    caller cannot accidentally leak the English source to it.
    """

    def __init__(
        self,
        issues_json: Optional[str] = None,
        *,
        fail: Optional[Exception] = None,
    ) -> None:
        self._issues_json = issues_json or json.dumps({"issues": []})
        self.fail = fail
        self.calls: List[Tuple[str, Dict[str, str]]] = []

    def __call__(self, *, chunk_id: str, translation: Mapping[str, str]) -> str:
        self.calls.append((chunk_id, dict(translation)))
        if self.fail is not None:
            raise self.fail
        return self._issues_json


class _LifecycleAwareModelCaller:
    def __init__(self, router: ModelRouter, inner: StubModelCaller) -> None:
        self._router = router
        self._inner = inner

    def __call__(self, bundle: PromptBundle) -> str:
        self._router.ensure_resident("gemma")
        return self._inner(bundle)


class _LifecycleAwareQwen:
    def __init__(self, router: ModelRouter, inner: StubQwen) -> None:
        self._router = router
        self._inner = inner

    def __call__(self, source, translation) -> GateResult:
        self._router.ensure_resident("qwen")
        return self._inner(source, translation)


class _LifecycleAwareGemmaSelector:
    def __init__(self, router: ModelRouter, inner: StubGemma) -> None:
        self._router = router
        self._inner = inner

    def __call__(self, candidates) -> GateResult:
        self._router.ensure_resident("gemma")
        return self._inner(candidates)


class _LifecycleAwareQwenAudit:
    def __init__(self, router: ModelRouter, inner: StubQwenAudit) -> None:
        self._router = router
        self._inner = inner

    def __call__(self, *, chunk_id, source, translation) -> str:
        self._router.ensure_resident("qwen")
        return self._inner(chunk_id=chunk_id, source=source, translation=translation)


class _LifecycleAwareGemmaAudit:
    def __init__(self, router: ModelRouter, inner: StubGemmaAudit) -> None:
        self._router = router
        self._inner = inner

    def __call__(self, *, chunk_id, translation) -> str:
        self._router.ensure_resident("gemma")
        return self._inner(chunk_id=chunk_id, translation=translation)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


def _make_backend() -> StrictBackendConfig:
    return StrictBackendConfig(
        exe=Path("C:/fake/llama-server.exe"), device="FAKE0", host="127.0.0.1",
        model_paths={"gemma": Path("C:/fake/gemma.gguf"), "qwen": Path("C:/fake/qwen.gguf")},
        model_names={"gemma": "gemma-fake", "qwen": "qwen-fake"},
        server_args={"gemma": [], "qwen": []}, port=0,
    )


def _make_cfg(tmp_path: Path, *, n_paragraphs: int = 24, max_consecutive: int = 3) -> StrictRunConfig:
    chapter_html = tmp_path / "046.html"
    memory_dir = tmp_path / "memory"
    _write_chapter_html(chapter_html, n_paragraphs)
    _write_empty_memory(memory_dir)
    return StrictRunConfig(
        chapter_id="046", chapter_html_path=chapter_html, memory_dir=memory_dir,
        out_dir=tmp_path / "out", backend=_make_backend(),
        max_consecutive_terminal_nonselections=max_consecutive,
    )


def _run(cfg: StrictRunConfig, *, qwen: Optional[StubQwen] = None,
          gemma: Optional[StubGemma] = None, router: Optional[ModelRouter] = None,
          qwen_audit: Optional[StubQwenAudit] = None,
          gemma_audit: Optional[StubGemmaAudit] = None):
    router = router or _make_router()
    model_caller = _LifecycleAwareModelCaller(router, StubModelCaller())
    qwen_evaluator = _LifecycleAwareQwen(router, qwen or StubQwen())
    gemma_selector = _LifecycleAwareGemmaSelector(router, gemma or StubGemma())
    qwen_audit_evaluator = _LifecycleAwareQwenAudit(router, qwen_audit or StubQwenAudit())
    gemma_audit_evaluator = _LifecycleAwareGemmaAudit(router, gemma_audit or StubGemmaAudit())
    result = run_chapter_strict(
        cfg, router=router, model_caller=model_caller,
        qwen_evaluator=qwen_evaluator, gemma_selector=gemma_selector,
        qwen_audit_evaluator=qwen_audit_evaluator,
        gemma_audit_evaluator=gemma_audit_evaluator,
    )
    return result, router


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_run_writes_all_artefacts(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=8)
    result, _router = _run(cfg)
    for path in (result.journal_path, result.translations_path, result.record_path):
        assert path.exists(), f"missing artefact: {path}"


def test_local_record_v2_shape(tmp_path: Path):
    # The v2 record keeps the legacy ``lifecycle`` block for old readers and
    # adds the generic ``backend`` + ``runtime`` blocks (plan §9.3).
    cfg = _make_cfg(tmp_path, n_paragraphs=8)
    result, router = _run(cfg)
    record = result.record
    assert record["schema"] == "pact-v4-strict-chapter-trial/v2"
    assert record["backend"]["kind"] == "local_llama"
    assert record["backend"]["identity_hash"] == cfg.backend.identity_hash
    assert record["runtime"]["remote_calls"] is None
    assert record["runtime"]["local_lifecycle"]["startup_count"] == len(router.switches)
    # Legacy block equals the runtime block's local_lifecycle.
    assert record["lifecycle"] == record["runtime"]["local_lifecycle"]
    assert record["lifecycle"]["restart_count"] == max(0, len(router.switches) - 1)


def test_two_low_risk_chunks_restart_count_matches_2n_minus_1(tmp_path: Path):
    # 24 paragraphs -> 2 chunks (same fixture as the draft_runner tests),
    # neither high-risk -> single candidate each, no Gemma preference
    # ever needed. Expected switches: Ggen(start), Q(gate1), G(gen2),
    # Q(gate2) -- 4 total for Phase 1-2, restart_count = 3 = 2*2 - 1. The
    # Step 6 audit then batches by detector: one acquire of Qwen for all
    # Qwen units + one switch to Gemma for all Gemma units -- +2 switches.
    cfg = _make_cfg(tmp_path, n_paragraphs=24)
    result, router = _run(cfg)
    assert result.chunk_count == 2
    assert result.selected_count == 2
    assert len(router.switches) == 6
    assert result.record["lifecycle"]["restart_count"] == 5
    assert result.step6["status"] == "complete"


def test_high_risk_chunk_with_agreeing_candidates_calls_gemma_preference(tmp_path: Path):
    # "You must not open box 7." repeated trips the risk pre-screen into
    # a high-risk band (same trigger content as
    # test_v4_phase12_draft_runner.test_run_chapter_high_risk_chunks_have_a_and_b),
    # producing 2 candidates. StubQwen passes both, and StubModelCaller
    # (role-blind) gives both candidates identical text, so there is no
    # semantic disagreement -- select_candidate's decision tree (case
    # "d": 2+ passed, no disagreement) calls gemma_selector. This is the
    # restart-accounting case test_two_low_risk_chunks_... does not cover:
    # a real switch back to Gemma for preference within the same chunk,
    # not just the next chunk's generation.
    chapter_html = tmp_path / "046.html"
    chapter_html.write_text(
        "<html><body>" + "<p>You must not open box 7.</p>" * 9 + "</body></html>",
        encoding="utf-8",
    )
    memory_dir = tmp_path / "memory"
    _write_empty_memory(memory_dir)
    cfg = StrictRunConfig(
        chapter_id="046", chapter_html_path=chapter_html, memory_dir=memory_dir,
        out_dir=tmp_path / "out", backend=_make_backend(),
    )
    router = _make_router()
    stub_gemma = StubGemma()
    stub_qwen_audit = StubQwenAudit()
    stub_gemma_audit = StubGemmaAudit()
    model_caller = _LifecycleAwareModelCaller(router, StubModelCaller())
    qwen_evaluator = _LifecycleAwareQwen(router, StubQwen(passed=True))
    gemma_selector = _LifecycleAwareGemmaSelector(router, stub_gemma)
    result = run_chapter_strict(
        cfg, router=router, model_caller=model_caller,
        qwen_evaluator=qwen_evaluator, gemma_selector=gemma_selector,
        qwen_audit_evaluator=_LifecycleAwareQwenAudit(router, stub_qwen_audit),
        gemma_audit_evaluator=_LifecycleAwareGemmaAudit(router, stub_gemma_audit),
    )
    assert result.chunk_count == 1
    assert len(stub_gemma.calls) == 1  # Gemma preference was actually invoked
    # Ggen(start) -> switch to Q(gate) -> switch back to G(preference): 2 restarts.
    # Step 6 then adds Qwen (audit) + one switch to Gemma (audit): +2 switches.
    assert len(router.switches) == 5
    assert result.record["lifecycle"]["restart_count"] == 4


def test_selected_translations_written(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=8)
    result, _router = _run(cfg)
    translations = json.loads(result.translations_path.read_text(encoding="utf-8"))
    assert len(translations) > 0
    assert all(text.startswith("Перевод номер") for text in translations.values())


def test_quarantine_halts_after_max_consecutive_nonselections(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=24, max_consecutive=1)
    result, _router = _run(cfg, qwen=StubQwen(passed=False, reason="meaning drift"))
    assert result.halted_early is True
    assert result.halt_reason is not None
    assert result.quarantined_count == 1
    assert result.processed_count == 1  # halted after the first chunk, not both
    # The quarantined chunk still produced a candidate, so Step 6 audits it
    # (best-variant for quarantine, owner decision 2026-08-02) instead of
    # skipping the whole chapter; the unprocessed second chunk is left to the
    # deterministic missing layer.
    assert result.step6["status"] == "complete"
    assert result.step6["covered_chunks"] == 1
    assert result.step6["uncovered_chunks"] == 1
    handoff = json.loads((result.out_dir / "b2_handoff.json").read_text(encoding="utf-8"))
    by_id = {row["chunk_id"]: row for row in handoff["chunks"]}
    assert by_id["chunk0001"]["status"] == "quarantined"
    assert by_id["chunk0001"]["committed"] is False
    assert by_id["chunk0001"]["audited_candidate_id"] is not None  # best-variant was audited
    assert by_id["chunk0001"]["audit_status"] == "clean"  # audited clean, still not accepted
    assert by_id["chunk0002"]["status"] == "incomplete_generation"
    assert by_id["chunk0002"]["uncovered_pids"] == by_id["chunk0002"]["plan_pids"]
    assert by_id["chunk0002"]["audit_status"] == "no_candidate"


def test_resume_skips_already_journaled_chunks_and_completes(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=24, max_consecutive=1)
    first_result, _router1 = _run(cfg, qwen=StubQwen(passed=False, reason="meaning drift"))
    assert first_result.halted_early is True
    assert first_result.processed_count == 1

    resumed_cfg = StrictRunConfig(
        chapter_id=cfg.chapter_id, chapter_html_path=cfg.chapter_html_path,
        memory_dir=cfg.memory_dir, out_dir=cfg.out_dir, backend=cfg.backend,
        max_consecutive_terminal_nonselections=3,
    )
    second_result, _router2 = _run(resumed_cfg, qwen=StubQwen(passed=True))
    assert second_result.resumed_from_index == 1
    assert second_result.processed_count == 2
    assert second_result.selected_count == 1  # only the resumed (2nd) chunk
    translations = json.loads(second_result.translations_path.read_text(encoding="utf-8"))
    assert len(translations) > 0


def test_resume_rejects_journal_from_a_different_snapshot(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=24, max_consecutive=1)
    _run(cfg, qwen=StubQwen(passed=False))

    # Overwrite the chapter source after the journal was written -> the
    # snapshot_hash on resume no longer matches what's journaled.
    _write_chapter_html(cfg.chapter_html_path, n_paragraphs=8)
    resumed_cfg = StrictRunConfig(
        chapter_id=cfg.chapter_id, chapter_html_path=cfg.chapter_html_path,
        memory_dir=cfg.memory_dir, out_dir=cfg.out_dir, backend=cfg.backend,
    )
    try:
        _run(resumed_cfg)
    except ValueError as exc:
        assert "Foreign identity" in str(exc)
    else:
        raise AssertionError("expected a Foreign identity ValueError")


# ---------------------------------------------------------------------------
# Phase 3B Step 6 assembled-chapter audit tests
# ---------------------------------------------------------------------------


def _load_audit_findings(result) -> Dict[str, Any]:
    path = result.out_dir / "audit_findings.json"
    assert path.exists(), f"missing audit findings artefact: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def test_step6_audit_runs_on_full_chapter_and_persists_findings(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=24)
    result, _router = _run(cfg)
    assert result.step6["status"] == "complete"
    assert result.step6["failed_units"] == []
    cache_path = result.out_dir / "audit_cache.json"
    assert cache_path.exists()
    payload = _load_audit_findings(result)
    assert payload["status"] == "complete"
    # Stub audits report no issues and the stub translations satisfy the
    # deterministic checks (source digits are carried into the target), so
    # the findings store is empty but the run is genuinely complete.
    assert payload["store"]["findings"] == []
    assert payload["chapter_hash"] == result.step6["chapter_hash"]
    assert "region_plan" in payload
    # A full chapter still produces the B2 handoff; every chunk is audited.
    handoff = json.loads((result.out_dir / "b2_handoff.json").read_text(encoding="utf-8"))
    assert handoff["schema"] == "pact-v4-step6-b2-handoff/v1"
    assert {row["status"] for row in handoff["chunks"]} == {"audited"}
    assert all(row["committed"] is True for row in handoff["chunks"])
    assert result.step6["covered_chunks"] == 2
    assert result.step6["uncovered_chunks"] == 0


def test_step6_audit_covers_every_chunk_and_batches_by_detector(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=24)  # 2 chunks
    qwen_audit = StubQwenAudit()
    gemma_audit = StubGemmaAudit()
    result, router = _run(cfg, qwen_audit=qwen_audit, gemma_audit=gemma_audit)
    assert result.chunk_count == 2
    # Every chunk is audited by both detectors.
    assert {cid for cid, _src, _tr in qwen_audit.calls} == {"chunk0001", "chunk0002"}
    assert {cid for cid, _tr in gemma_audit.calls} == {"chunk0001", "chunk0002"}
    # Detector-outer loop (DECISIONS 2026-08-01): the audit phase adds one
    # Qwen acquire + one Qwen->Gemma switch for the whole chapter, not 2N.
    audit_switches = [sw.to_model for sw in router.switches[-2:]]
    assert audit_switches == ["qwen", "gemma"]
    assert len(router.switches) == 6
    assert result.step6["switch_count"] == 2
    assert [sw["to_model"] for sw in result.step6["switches"]] == ["qwen", "gemma"]


def test_step6_audit_does_not_claim_complete_on_model_failure(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=24)
    qwen_audit = StubQwenAudit(fail=RuntimeError("llama-server timeout"))
    result, _router = _run(cfg, qwen_audit=qwen_audit)
    assert result.step6["status"] == "incomplete"
    assert len(result.step6["failed_units"]) == 2  # one Qwen unit per chunk
    payload = _load_audit_findings(result)
    assert payload["status"] == "incomplete"
    assert payload["failed_units"]
    # A model failure is never silently read as "no issues found".
    assert result.step6["finding_count"] >= 0
    assert result.step6["status"] != "complete"


def test_step6_audit_truncated_json_is_not_complete(tmp_path: Path):
    # Qwen max_tokens fix (PR #96): a response truncated mid-JSON must be a
    # failed unit (the audit layer re-attempts it on resume), never a pass.
    cfg = _make_cfg(tmp_path, n_paragraphs=24)
    truncated = '{"issues": [{"pid": "x", "category": "omission", "note": "trunc'
    qwen_audit = StubQwenAudit(issues_json=truncated)
    result, _router = _run(cfg, qwen_audit=qwen_audit)
    assert result.step6["status"] == "incomplete"
    assert len(result.step6["failed_units"]) == 2
    payload = _load_audit_findings(result)
    assert payload["status"] == "incomplete"


def test_step6_audit_finding_tied_to_owning_chunk(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=24)

    class _FlaggingQwenAudit(StubQwenAudit):
        def __call__(self, *, chunk_id, source, translation):
            if chunk_id == "chunk0001":
                pid = next(iter(translation))
                return json.dumps({"issues": [
                    {"pid": pid, "category": "omission", "note": "dropped clause"}
                ]})
            return json.dumps({"issues": []})

    result, _router = _run(cfg, qwen_audit=_FlaggingQwenAudit())
    assert result.step6["status"] == "complete"
    payload = _load_audit_findings(result)
    findings = payload["store"]["findings"]
    qwen_findings = [f for f in findings if f["detector"] == "qwen_chapter_audit"]
    assert len(qwen_findings) == 1
    # The finding is attached to the central chunk that owns the flagged PID,
    # and carries that chunk's winning candidate id for provenance.
    assert qwen_findings[0]["chunk_id"] == "chunk0001"
    assert qwen_findings[0]["candidate_id"].startswith("chunk0001:")


def test_step6_audit_resume_only_reruns_failed_units(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=24)

    class _FailFirstChunkQwenAudit(StubQwenAudit):
        def __call__(self, *, chunk_id, source, translation):
            if chunk_id == "chunk0001":
                raise RuntimeError("llama-server timeout")
            return json.dumps({"issues": []})

    first_result, _router1 = _run(cfg, qwen_audit=_FailFirstChunkQwenAudit())
    assert first_result.step6["status"] == "incomplete"
    assert [unit[0] for unit in first_result.step6["failed_units"]] == ["chunk0001"]

    resumed_cfg = StrictRunConfig(
        chapter_id=cfg.chapter_id, chapter_html_path=cfg.chapter_html_path,
        memory_dir=cfg.memory_dir, out_dir=cfg.out_dir, backend=cfg.backend,
    )
    resumed_qwen_audit = StubQwenAudit()
    second_result, _router2 = _run(resumed_cfg, qwen_audit=resumed_qwen_audit)
    assert second_result.resumed_from_index == 2
    assert second_result.step6["status"] == "complete"
    # Resume restored the persisted audit cache: only the previously-failed
    # unit was re-attempted, every other unit was reused without a model call.
    assert [cid for cid, _s, _t in resumed_qwen_audit.calls] == ["chunk0001"]


def test_step6_audits_partial_selection_with_best_variant(tmp_path: Path):
    # First chunk quarantined (not enough to halt), second selected: Step 6
    # must audit BOTH chunks (owner decision 2026-08-02) — the quarantined
    # one through its deterministic best-variant, the selected one through
    # its committed winner — and hand off the real per-chunk status.
    cfg = _make_cfg(tmp_path, n_paragraphs=24)

    class _FailOnceQwen(StubQwen):
        def __init__(self):
            super().__init__(passed=True)
            self._calls = 0

        def __call__(self, source, translation):
            self._calls += 1
            return GateResult(
                gate="qwen_fidelity",
                passed=self._calls > 1,
                detail="first chunk rejected, then OK",
            )

    qwen_audit = StubQwenAudit()
    gemma_audit = StubGemmaAudit()
    result, _router = _run(
        cfg, qwen=_FailOnceQwen(), qwen_audit=qwen_audit, gemma_audit=gemma_audit,
    )
    assert result.selected_count == 1
    assert result.quarantined_count == 1
    # No more skip on partial selection: the whole plan is audited.
    assert result.step6["status"] == "complete"
    assert result.step6["covered_chunks"] == 2
    assert result.step6["uncovered_chunks"] == 0
    # Both chunks were actually audited by both detectors.
    assert {cid for cid, _src, _tr in qwen_audit.calls} == {"chunk0001", "chunk0002"}
    assert {cid for cid, _tr in gemma_audit.calls} == {"chunk0001", "chunk0002"}

    handoff = json.loads((result.out_dir / "b2_handoff.json").read_text(encoding="utf-8"))
    assert handoff["schema"] == "pact-v4-step6-b2-handoff/v1"
    by_id = {row["chunk_id"]: row for row in handoff["chunks"]}
    assert by_id["chunk0001"]["status"] == "quarantined"
    assert by_id["chunk0001"]["committed"] is False
    assert by_id["chunk0001"]["audited_candidate_id"] is not None
    assert by_id["chunk0001"]["audit_status"] == "clean"
    assert by_id["chunk0001"]["best_variant_rule"] == "max_gates_passed>role(fidelity_first>balanced_literary>synthesis)>candidate_id"
    assert by_id["chunk0001"]["quarantine_reason"] is not None
    assert by_id["chunk0002"]["status"] == "audited"
    assert by_id["chunk0002"]["committed"] is True
    assert by_id["chunk0002"]["audited_candidate_id"] is not None
    assert by_id["chunk0002"]["audit_status"] == "clean"
    assert by_id["chunk0002"]["uncovered_pids"] == []


def test_step6_best_variant_picks_fidelity_first_among_two_gemma_variants(tmp_path: Path):
    # A high-risk chunk produces two Gemma candidates (fidelity_first A +
    # balanced_literary B); both fail the Qwen gate so the chunk is
    # quarantined. Step 6's best-variant rule (owner decision 2026-08-02)
    # must deterministically pick A: equal gates passed on each candidate's
    # own decision_trace, then role priority fidelity_first > balanced_literary.
    chapter_html = tmp_path / "046.html"
    chapter_html.write_text(
        "<html><body>" + "<p>You must not open box 7.</p>" * 9 + "</body></html>",
        encoding="utf-8",
    )
    memory_dir = tmp_path / "memory"
    _write_empty_memory(memory_dir)
    cfg = StrictRunConfig(
        chapter_id="046", chapter_html_path=chapter_html, memory_dir=memory_dir,
        out_dir=tmp_path / "out", backend=_make_backend(),
    )
    router = _make_router()
    model_caller = _LifecycleAwareModelCaller(router, StubModelCaller())
    qwen_evaluator = _LifecycleAwareQwen(router, StubQwen(passed=False, reason="meaning drift"))
    gemma_selector = _LifecycleAwareGemmaSelector(router, StubGemma())
    result = run_chapter_strict(
        cfg, router=router, model_caller=model_caller,
        qwen_evaluator=qwen_evaluator, gemma_selector=gemma_selector,
        qwen_audit_evaluator=_LifecycleAwareQwenAudit(router, StubQwenAudit()),
        gemma_audit_evaluator=_LifecycleAwareGemmaAudit(router, StubGemmaAudit()),
    )
    assert result.chunk_count == 1
    assert result.quarantined_count == 1
    assert result.step6["status"] == "complete"
    handoff = json.loads((result.out_dir / "b2_handoff.json").read_text(encoding="utf-8"))
    row = handoff["chunks"][0]
    assert row["status"] == "quarantined"
    # Both A and B are listed as available variants; the audited best-variant
    # is the fidelity_first one (role priority), deterministically.
    roles = {v["role"] for v in row["available_variants"]}
    assert roles == {"fidelity_first", "balanced_literary"}
    assert row["audited_role"] == "fidelity_first"
    assert ":fidelity_first:" in row["audited_candidate_id"]
    best = next(v for v in row["available_variants"] if v["role"] == "fidelity_first")
    assert row["audited_candidate_id"] == best["candidate_id"]
    # A quarantined chunk stays quarantined even though its best-variant
    # audited clean — the handoff carries the chunk's status, not step6.status.
    assert row["committed"] is False


def test_step6_no_candidate_chunk_is_missing_coverage_without_model_units(tmp_path: Path):
    # A needs_synthesis / incomplete_generation / never-processed chunk has no
    # auditable candidate: Step 6 must cover its PIDs via the deterministic
    # missing layer and must NOT call any model unit for it.
    cfg = _make_cfg(tmp_path, n_paragraphs=24, max_consecutive=1)

    class _FailGenerationModelCaller(StubModelCaller):
        def __call__(self, bundle: PromptBundle) -> str:
            # Truncated JSON -> generation validation failure for every chunk.
            return '{"p00000": "перевод'

    qwen_audit = StubQwenAudit()
    gemma_audit = StubGemmaAudit()
    router = _make_router()
    model_caller = _LifecycleAwareModelCaller(router, _FailGenerationModelCaller())
    qwen_evaluator = _LifecycleAwareQwen(router, StubQwen())
    gemma_selector = _LifecycleAwareGemmaSelector(router, StubGemma())
    result = run_chapter_strict(
        cfg, router=router, model_caller=model_caller,
        qwen_evaluator=qwen_evaluator, gemma_selector=gemma_selector,
        qwen_audit_evaluator=_LifecycleAwareQwenAudit(router, qwen_audit),
        gemma_audit_evaluator=_LifecycleAwareGemmaAudit(router, gemma_audit),
    )
    assert result.incomplete_generation_count == 1
    assert result.halted_early is True
    # Zero candidates (no selected chunk, no quarantined chunk with variants):
    # the audit is skipped, so no model audit unit was ever attempted.
    assert result.step6["status"] == "skipped"
    assert result.step6["reason"] == "no_selected_chunks"
    assert qwen_audit.calls == []
    assert gemma_audit.calls == []


def test_step6_quarantined_chunk_without_variants_is_missing_coverage(tmp_path: Path):
    # Review PR #108, issue 4: a quarantined chunk whose variants are not
    # recoverable (e.g. a prior session whose generation_outcomes.json was
    # lost) must fall into the same no-candidate branch as incomplete_generation
    # — no candidate, missing coverage, no fabricated variant.
    cfg = _make_cfg(tmp_path, n_paragraphs=8)
    blocks, _raw_sha = load_source(cfg.chapter_html_path)
    source = build_source_artifact(chapter_id=cfg.chapter_id, blocks=blocks)
    memory = ChapterMemory.from_directory(cfg.memory_dir)
    snapshot = build_snapshot(
        chapter_id=cfg.chapter_id, source=source, memory=memory,
        context=f"chapter_html={cfg.chapter_html_path};memory_dir={cfg.memory_dir}",
    )
    config = cfg.to_config_artifact(model_profile="gemma-fake")
    planner = ChunkPlanner(
        target_words=cfg.target_chunk_words, min_words=cfg.min_chunk_words,
        max_words=cfg.max_chunk_words,
    )
    plans = planner.plan(blocks, snapshot_hash=snapshot.snapshot_hash,
                          following_blocks=cfg.right_context_pids)
    chunk_plan = ChunkPlanArtifact.create(snapshot, tuple(plans))
    chunk0 = chunk_plan.chunks[0]

    candidates, rows = _audit_candidate_map(
        selection_records=[
            {"chunk_id": chunk0.chunk_id, "status": "quarantined",
             "quarantine_reason": "No candidate passed both gates."}
        ],
        selected_text_by_chunk={},
        generation_records=[],  # no recoverable variants
        chunk_plan=chunk_plan, source=source, snapshot=snapshot, config=config,
    )
    assert candidates == {}
    row = rows[0]
    assert row["status"] == "quarantined"
    assert row["committed"] is False
    assert row["audited_candidate_id"] is None
    assert row["best_variant_rule"] is None
    assert row["available_variants"] == []
    assert row["uncovered_pids"] == list(chunk0.pids)


def test_step6_handoff_audit_status_marks_failed_units(tmp_path: Path):
    # Review PR #108, issue 1: a chunk whose model unit failed must be
    # distinguishable from a clean audit in the handoff itself. uncovered_pids
    # stays structural ([] for a committed chunk); audit_status carries the
    # audit-completeness signal.
    cfg = _make_cfg(tmp_path, n_paragraphs=24)

    class _FailChunk2Gemma(StubGemmaAudit):
        def __call__(self, *, chunk_id, translation):
            if chunk_id == "chunk0002":
                raise RuntimeError("gemma timeout")
            return json.dumps({"issues": []})

    result, _router = _run(cfg, gemma_audit=_FailChunk2Gemma())
    assert result.step6["status"] == "incomplete"
    handoff = json.loads((result.out_dir / "b2_handoff.json").read_text(encoding="utf-8"))
    by_id = {row["chunk_id"]: row for row in handoff["chunks"]}
    assert by_id["chunk0001"]["audit_status"] == "clean"
    assert by_id["chunk0002"]["audit_status"] == "unit_failed"
    assert by_id["chunk0002"]["committed"] is True
    assert by_id["chunk0002"]["uncovered_pids"] == []
    # step6.status is per-audit-run; the per-chunk truth lives in the handoff.
    assert by_id["chunk0001"]["status"] == "audited"
    assert by_id["chunk0002"]["status"] == "audited"


def test_step6_handoff_audit_status_marks_findings_present(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=24)

    class _FlaggingQwenAudit(StubQwenAudit):
        def __call__(self, *, chunk_id, source, translation):
            if chunk_id == "chunk0001":
                pid = next(iter(translation))
                return json.dumps({"issues": [
                    {"pid": pid, "category": "omission", "note": "dropped clause"}
                ]})
            return json.dumps({"issues": []})

    result, _router = _run(cfg, qwen_audit=_FlaggingQwenAudit())
    assert result.step6["status"] == "complete"
    handoff = json.loads((result.out_dir / "b2_handoff.json").read_text(encoding="utf-8"))
    by_id = {row["chunk_id"]: row for row in handoff["chunks"]}
    assert by_id["chunk0001"]["audit_status"] == "findings_present"
    assert by_id["chunk0002"]["audit_status"] == "clean"
    assert by_id["chunk0001"]["status"] == "audited"
    assert by_id["chunk0001"]["committed"] is True


def test_step6_resume_reloads_generation_outcomes_for_quarantined_chunks(tmp_path: Path):
    # Run 1: chunk0001 quarantined (halted). Run 2 (resume): chunk0001's
    # journal entry is replayed and chunk0002 is selected. Step 6 must load
    # the persisted generation_outcomes.json so chunk0001's best-variant is
    # recoverable from the *previous* session, and must hand off both chunks.
    cfg = _make_cfg(tmp_path, n_paragraphs=24, max_consecutive=1)
    first_result, _router1 = _run(cfg, qwen=StubQwen(passed=False, reason="meaning drift"))
    assert first_result.quarantined_count == 1
    assert first_result.processed_count == 1

    resumed_cfg = StrictRunConfig(
        chapter_id=cfg.chapter_id, chapter_html_path=cfg.chapter_html_path,
        memory_dir=cfg.memory_dir, out_dir=cfg.out_dir, backend=cfg.backend,
        max_consecutive_terminal_nonselections=3,
    )
    second_result, _router2 = _run(resumed_cfg, qwen=StubQwen(passed=True))
    assert second_result.resumed_from_index == 1
    assert second_result.selected_count == 1
    assert second_result.step6["status"] == "complete"
    assert second_result.step6["covered_chunks"] == 2

    handoff = json.loads((second_result.out_dir / "b2_handoff.json").read_text(encoding="utf-8"))
    by_id = {row["chunk_id"]: row for row in handoff["chunks"]}
    assert by_id["chunk0001"]["status"] == "quarantined"
    assert by_id["chunk0001"]["committed"] is False
    assert by_id["chunk0001"]["audited_candidate_id"] is not None  # reloaded from prior session
    # The quarantine reason from run 1 survives the resume via the cumulative
    # selection_meta.json sidecar (the journal v1 does not persist it).
    assert by_id["chunk0001"]["quarantine_reason"] is not None
    assert "meaning drift" in by_id["chunk0001"]["quarantine_reason"]
    assert by_id["chunk0002"]["status"] == "audited"
    assert by_id["chunk0002"]["committed"] is True

    # The persisted generation_outcomes.json is now cumulative: both chunks'
    # records are present for a future resume.
    gen = json.loads(
        (second_result.out_dir / "generation_outcomes.json").read_text(encoding="utf-8")
    )
    assert {rec["chunk_id"] for rec in gen["outcomes"]} == {"chunk0001", "chunk0002"}
    # selection_results.json is cumulative and rich too: chunk0001's record
    # carries its original quarantine_reason even after the resume.
    sel = json.loads(
        (second_result.out_dir / "selection_results.json").read_text(encoding="utf-8")
    )
    chunk1_sel = next(r for r in sel["results"] if r["chunk_id"] == "chunk0001")
    assert chunk1_sel["status"] == "quarantined"
    assert "meaning drift" in chunk1_sel["quarantine_reason"]


def test_step6_audit_rejects_foreign_audit_cache_on_resume(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=24)
    _run(cfg)

    # Tamper the persisted audit cache's backend identity. The journal is
    # untouched, so resume passes the journal check; the Step 6 cache load
    # must then refuse the foreign cache instead of silently reusing it.
    cache_path = cfg.out_dir / "audit_cache.json"
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    payload["backend_identity_hash"] = "deadbeef" * 8
    cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    resumed_cfg = StrictRunConfig(
        chapter_id=cfg.chapter_id, chapter_html_path=cfg.chapter_html_path,
        memory_dir=cfg.memory_dir, out_dir=cfg.out_dir, backend=cfg.backend,
    )
    result, _router = _run(resumed_cfg)
    assert result.step6["status"] == "failed"
    assert "Foreign identity: audit cache" in result.step6["error"]


def test_step6_audit_skipped_on_incomplete_translation(tmp_path: Path):
    # A chunk is selected in the journal but its committed text no longer
    # covers the plan's PIDs (e.g. prior translations.json was tampered or a
    # resume reconstruction came up short). The audit must refuse to assemble
    # a partial chunk with a distinct skip reason, not misread it as a clean
    # chapter or crash with a generic ownership error.
    cfg = _make_cfg(tmp_path, n_paragraphs=24)
    first_result, _router = _run(cfg)
    assert first_result.step6["status"] == "complete"

    # Drop one PID owned by chunk0001 from the committed translations.
    plan_payload = json.loads(
        (cfg.out_dir / "chunk_plan.json").read_text(encoding="utf-8")
    )
    chunk0001 = next(c for c in plan_payload["chunks"] if c["chunk_id"] == "chunk0001")
    missing_pid = chunk0001["pids"][0]
    translations_path = cfg.out_dir / "translations.json"
    translations = json.loads(translations_path.read_text(encoding="utf-8"))
    del translations[missing_pid]
    translations_path.write_text(
        json.dumps(translations, ensure_ascii=False), encoding="utf-8"
    )

    resumed_cfg = StrictRunConfig(
        chapter_id=cfg.chapter_id, chapter_html_path=cfg.chapter_html_path,
        memory_dir=cfg.memory_dir, out_dir=cfg.out_dir, backend=cfg.backend,
    )
    result, _router = _run(resumed_cfg)
    assert result.step6["status"] == "skipped"
    assert result.step6["reason"] == "incomplete_translation"
    assert "chunk0001" in result.step6["detail"]


def test_runners_do_not_import_helpers_from_draft_runner():
    """A2 decoupling guard: strict/sequential must not depend on the draft runner.

    ``v4_phase12_draft_runner`` is being demoted to a reference/fixture
    (strict is the production v4 architecture, ``DECISIONS.md``
    2026-08-01). The shared helpers now live in
    ``pact_v4.pipeline._shared_runner_helpers``; if any runner imports them
    from ``v4_phase12_draft_runner`` again, archiving that fixture would
    silently break the production driver.
    """
    _IMPORT_FROM_DRAFT = re.compile(
        r"^\s*from\s+pact_v4\.pipeline\.v4_phase12_draft_runner\s+import",
        re.MULTILINE,
    )
    for module in (v4_phase12_sequential_runner, v4_phase12_strict_runner):
        source = inspect.getsource(module)
        assert _IMPORT_FROM_DRAFT.search(source) is None, (
            f"{module.__name__} still imports helpers from v4_phase12_draft_runner"
        )
    for name in (
        "_glossary_entries",
        "_left_ru_for_chunk",
        "_record_selection",
        "_risk_for_chunk",
        "_serialize_generation_outcome",
    ):
        assert hasattr(_shared_runner_helpers, name), name
