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

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from pact_v4.phase1.models import GateResult
from pact_v4.phase2.generation import PromptBundle
from pact_v4.pipeline.v4_phase12_strict_runner import (
    StrictBackendConfig,
    StrictRunConfig,
    run_chapter_strict,
)
from pact_v4.runtime.model_lifecycle import ModelRouter

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
          gemma: Optional[StubGemma] = None, router: Optional[ModelRouter] = None):
    router = router or _make_router()
    model_caller = _LifecycleAwareModelCaller(router, StubModelCaller())
    qwen_evaluator = _LifecycleAwareQwen(router, qwen or StubQwen())
    gemma_selector = _LifecycleAwareGemmaSelector(router, gemma or StubGemma())
    result = run_chapter_strict(
        cfg, router=router, model_caller=model_caller,
        qwen_evaluator=qwen_evaluator, gemma_selector=gemma_selector,
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


def test_two_low_risk_chunks_restart_count_matches_2n_minus_1(tmp_path: Path):
    # 24 paragraphs -> 2 chunks (same fixture as the draft_runner tests),
    # neither high-risk -> single candidate each, no Gemma preference
    # ever needed. Expected switches: Ggen(start), Q(gate1), G(gen2),
    # Q(gate2) -- 4 total, restart_count = 3 = 2*2 - 1.
    cfg = _make_cfg(tmp_path, n_paragraphs=24)
    result, router = _run(cfg)
    assert result.chunk_count == 2
    assert result.selected_count == 2
    assert len(router.switches) == 4
    assert result.record["lifecycle"]["restart_count"] == 3


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
