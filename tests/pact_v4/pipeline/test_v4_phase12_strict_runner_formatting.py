"""Strict-driver Phase 5 formatting (B3, card C) integration tests.

These run the full ``run_chapter_strict`` driver with Phase 4 repair
adapters injected (formatting is model-free — there is no formatting
adapter), verifying:

  * formatting runs **between** Step 7 (convergence) and Step 8 (terminal):
    the formatting report and the repair report's ``final_translation`` carry
    the formatted text, so Step 8 and the terminal transition see the same
    text that goes into ``complete``;
  * formatting is **deterministic** (0 model calls): the stub generation
    keeps the emphasized fragment inline in the translated text (the
    whole-chapter case), so the deterministic tiers resolve every span
    without any model call — ``model_call_count`` is 0;
  * an unresolved required span is a blocking incident: with the production
    default ``max_formatting_incidents=0`` the chapter degrades to
    ``accepted_degraded`` (valid PID map + debt trace), never ``failed``.
    "0 model calls" alone is not success when formatting debt degraded the
    chapter;
  * without formatting required (``formatting_required=False``) the step is
    skipped (master switch);
  * the formatting artifacts carry the backend identity;
  * dual-mode parity (§14.3): local fake backend vs fake OpenCode server —
    both produce identical formatted text and final integrity result, and
    neither ever issues a Phase 5 model call (model-free invariant).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pact_v4.pipeline.v4_phase12_strict_runner import (
    StrictBackendConfig,
    StrictRunConfig,
    run_chapter_strict,
)
from pact_v4.runtime.backend_role_adapters import (
    BackendGemmaAuditEvaluator,
    BackendGemmaSelector,
    BackendModelCaller,
    BackendQwenAuditEvaluator,
    BackendQwenEvaluator,
    BackendRegionFidelityGate,
    BackendRepairCaller,
)
from pact_v4.runtime.opencode_backend import (
    OpenCodeServerBackend,
    OpenCodeServerBackendConfig,
)
from pact_v4.runtime.runtime_config import OpenCodeBackendConfig
from pact_v4.runtime.runtime_coordinator import RemoteRuntimeCoordinator
from tests.pact_v4.pipeline.test_v4_phase12_strict_runner import (
    StubGemma,
    StubGemmaAudit,
    StubModelCaller,
    StubQwen,
    StubQwenAudit,
    StubRegionGate,
    _LifecycleAwareGemmaAudit,
    _LifecycleAwareGemmaSelector,
    _LifecycleAwareModelCaller,
    _LifecycleAwareQwen,
    _LifecycleAwareQwenAudit,
    _make_router,
)
from tests.pact_v4.runtime.opencode_dynamic_fake import DynamicFakeOpenCodeServer

WORDS_PER_PARAGRAPH = 35


def _write_chapter_html(path: Path, n_paragraphs: int) -> None:
    # Every paragraph carries one inline <em> span so the formatting step has
    # a span contract to restore.
    words = [f"word{i}" for i in range(WORDS_PER_PARAGRAPH)]
    words[5] = "<em>emphasized</em>"
    paragraph_text = " ".join(words)
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


def _make_cfg(tmp_path: Path, *, n_paragraphs: int = 8, backend: Any = None) -> StrictRunConfig:
    chapter_html = tmp_path / "046.html"
    memory_dir = tmp_path / "memory"
    out_dir = tmp_path / "out"
    _write_chapter_html(chapter_html, n_paragraphs)
    _write_empty_memory(memory_dir)
    return StrictRunConfig(
        chapter_id="046", chapter_html_path=chapter_html, memory_dir=memory_dir,
        out_dir=out_dir, backend=backend or _make_backend(),
    )


class _PreservingModelCaller(StubModelCaller):
    """Stub generator that KEEPS the emphasized fragment inline in the
    translated text (the whole-chapter case, §11 "whole-chapter перевод
    держит <em> 101/101").

    Base ``StubModelCaller`` emits ``Перевод номерN`` (dropping the
    emphasis); this subclass re-appends the source's emphasized word so the
    deterministic ``exact`` tier resolves the span with 0 model calls.
    """

    def __call__(self, bundle) -> str:
        out = json.loads(super().__call__(bundle))
        for pid, text in bundle.owned_source:
            if "emphasized" in text:
                out[pid] = f"{out[pid]} (emphasized)"
        return json.dumps(out, ensure_ascii=False)


class StubRepairCaller:
    """Phase 4 repair caller; the audit produces no findings so it is unused."""

    def __call__(self, *, chunk_id, source, translation, region, findings) -> str:
        raise AssertionError("no findings expected in the formatting integration fixture")


def _clean_repair_adapters() -> tuple:
    repair_caller = StubRepairCaller()
    repair_qwen = StubRegionGate(passed=True, reason="regate")

    class _StubRepairGemmaAudit(StubGemmaAudit):
        def __call__(self, *, chunk_id, translation):
            return json.dumps({"issues": []})

    class _StubRepairQwenAudit(StubQwenAudit):
        def __call__(self, *, chunk_id, source, translation):
            return json.dumps({"issues": []})

    return (
        repair_caller,
        repair_qwen,
        _StubRepairQwenAudit(),
        _StubRepairGemmaAudit(),
    )


def _run_local(cfg: StrictRunConfig, *, drop_emphasis: bool = False):
    router = _make_router()
    stub = StubModelCaller() if drop_emphasis else _PreservingModelCaller()
    model_caller = _LifecycleAwareModelCaller(router, stub)
    qwen_evaluator = _LifecycleAwareQwen(router, StubQwen())
    gemma_selector = _LifecycleAwareGemmaSelector(router, StubGemma())
    qwen_audit_evaluator = _LifecycleAwareQwenAudit(router, StubQwenAudit())
    gemma_audit_evaluator = _LifecycleAwareGemmaAudit(router, StubGemmaAudit())
    result = run_chapter_strict(
        cfg, router=router, model_caller=model_caller,
        qwen_evaluator=qwen_evaluator, gemma_selector=gemma_selector,
        qwen_audit_evaluator=qwen_audit_evaluator,
        gemma_audit_evaluator=gemma_audit_evaluator,
        repair_adapters=_clean_repair_adapters(),
    )
    return result


def _load_report(out_dir: Path, name: str) -> dict:
    return json.loads((out_dir / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Formatting runs between Step 7 and Step 8; terminal sees the final text
# ---------------------------------------------------------------------------


def test_formatting_runs_between_step7_and_step8(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=8)
    result = _run_local(cfg)

    assert result.step6["status"] == "complete"
    assert result.step7["status"] in ("complete", "accepted_degraded")
    # The formatting block is recorded in step7/step8.
    assert result.step7["formatting"]["status"] == "ok"
    assert result.step8["formatting"] == result.step7["formatting"]

    report = _load_report(cfg.out_dir, "repair_report.json")
    # The repair report's final_translation is the FORMATTED text: the
    # restored <em> span is what Step 8 and the terminal transition saw.
    final_texts = [text for _pid, text in report["final_translation"]]
    assert any("<em>" in text for text in final_texts), (
        "the final translation must carry the restored inline markup"
    )
    # The formatting report artifact carries backend identity.
    fmt_report = _load_report(cfg.out_dir, "formatting_report.json")
    assert fmt_report["schema"] == "pact-v4-formatting-report/v1"
    assert fmt_report["backend_identity_hash"] == cfg.backend.identity_hash
    assert fmt_report["outcome"]["schema"] == "pact-v4-formatting-outcome/v1"
    assert fmt_report["outcome"]["resolved_count"] > 0
    assert fmt_report["outcome"]["incident_count"] == 0
    # Card C: formatting is model-free — 0 model calls, always.
    assert fmt_report["outcome"]["model_call_count"] == 0
    assert fmt_report["outcome"]["model_fallback_count"] == 0
    # Terminal sees the same text: integrity's frozen hash covers formatted.
    assert result.step8["status"] == result.step7["terminal"]


def test_formatting_incident_yields_accepted_degraded(tmp_path: Path):
    # The stub generation DROPS the emphasized fragment entirely (no
    # preserved markup, no verbatim fragment) -> blocking incidents -> with
    # max_formatting_incidents=0 the chapter degrades to accepted_degraded
    # (valid PID map + debt trace), never `failed`. "0 model calls" alone is
    # not success when formatting debt degraded the chapter.
    cfg = _make_cfg(tmp_path, n_paragraphs=8)
    result = _run_local(cfg, drop_emphasis=True)
    assert result.step7["status"] == "accepted_degraded"
    assert result.step8["status"] == "accepted_degraded"
    assert result.step7["formatting"]["status"] == "blocking"
    assert result.step7["formatting"]["incident_count"] > 0
    assert result.step7["formatting"]["model_call_count"] == 0
    report = _load_report(cfg.out_dir, "repair_report.json")
    assert any("formatting:" in reason for reason in report["debt_trace"])
    fmt_report = _load_report(cfg.out_dir, "formatting_report.json")
    assert fmt_report["outcome"]["blocking"] is True


def test_formatting_skipped_when_formatting_not_required(tmp_path: Path):
    # ``formatting_required=False`` is the runtime master switch (§6.1
    # ``formatting.required=true``): the deterministic step is skipped
    # entirely.
    cfg = _make_cfg(tmp_path, n_paragraphs=8)
    cfg = StrictRunConfig(
        chapter_id=cfg.chapter_id, chapter_html_path=cfg.chapter_html_path,
        memory_dir=cfg.memory_dir, out_dir=cfg.out_dir, backend=cfg.backend,
        formatting_required=False,
    )
    result = _run_local(cfg)
    assert result.step7["formatting"] is None
    assert result.step8["formatting"] is None
    assert not (cfg.out_dir / "formatting_report.json").exists()
    report = _load_report(cfg.out_dir, "repair_report.json")
    assert all("<em>" not in text for _pid, text in report["final_translation"])


def test_formatting_skipped_when_repair_skipped(tmp_path: Path):
    # Without repair adapters, Step 7/8 (and therefore formatting) are
    # recorded as skipped — formatting has no Step 8 to run before.
    cfg = _make_cfg(tmp_path, n_paragraphs=8)
    router = _make_router()
    result = run_chapter_strict(
        cfg, router=router,
        model_caller=_LifecycleAwareModelCaller(router, StubModelCaller()),
        qwen_evaluator=_LifecycleAwareQwen(router, StubQwen()),
        gemma_selector=_LifecycleAwareGemmaSelector(router, StubGemma()),
        qwen_audit_evaluator=_LifecycleAwareQwenAudit(router, StubQwenAudit()),
        gemma_audit_evaluator=_LifecycleAwareGemmaAudit(router, StubGemmaAudit()),
    )
    assert result.step7["status"] == "skipped"
    assert result.step7["reason"] == "repair_adapters_not_configured"
    assert not (cfg.out_dir / "formatting_report.json").exists()


# ---------------------------------------------------------------------------
# Dual-mode parity (§14.3): local fake backend vs fake OpenCode server
# ---------------------------------------------------------------------------

ROLE_BINDINGS = {
    "default": "opencode-go/deepseek-v4-flash",
    "generator": "opencode-go/deepseek-v4-flash",
    "fidelity_reviewer": "opencode-go/qwen3.7-plus",
    "russian_selector": "opencode-go/qwen3.7-plus",
    "qwen_audit": "opencode-go/qwen3.7-plus",
    "gemma_audit": "opencode-go/qwen3.7-plus",
    "repair": "opencode-go/deepseek-v4-flash",
}


def _remote_backend_config() -> OpenCodeServerBackendConfig:
    return OpenCodeServerBackendConfig(
        base_url="http://127.0.0.1:4096",
        username="pact",
        password="secret-A",
        model_bindings=dict(ROLE_BINDINGS),
        structured_output_mode="prompt_only",
    )


def test_formatting_dual_mode_parity_local_vs_remote(tmp_path: Path):
    # Same chapter; only the transport differs. The formatting step is
    # deterministic (card C), so local and remote produce byte-identical
    # formatted text and the same final integrity result — and, being
    # model-free, neither path ever issues a Phase 5 model call.
    chapter_html = tmp_path / "046.html"
    memory_dir = tmp_path / "memory"
    _write_chapter_html(chapter_html, 24)
    _write_empty_memory(memory_dir)

    def make(backend: Any, out_dir: Path) -> StrictRunConfig:
        return StrictRunConfig(
            chapter_id="046", chapter_html_path=chapter_html, memory_dir=memory_dir,
            out_dir=out_dir, backend=backend,
        )

    # --- local fake backend ---
    local_cfg = make(_make_backend(), tmp_path / "local_out")
    local_result = _run_local(local_cfg)
    local_fmt = _load_report(local_cfg.out_dir, "formatting_report.json")

    # --- remote fake OpenCode server ---
    backend_cfg = _remote_backend_config()
    backend = OpenCodeServerBackend(config=backend_cfg, session=DynamicFakeOpenCodeServer())
    runtime = RemoteRuntimeCoordinator(backend)
    remote_cfg = make(OpenCodeBackendConfig(server=backend_cfg), tmp_path / "remote_out")

    remote_result = run_chapter_strict(
        remote_cfg, runtime=runtime,
        model_caller=BackendModelCaller(backend),
        qwen_evaluator=BackendQwenEvaluator(backend),
        gemma_selector=BackendGemmaSelector(backend),
        qwen_audit_evaluator=BackendQwenAuditEvaluator(backend),
        gemma_audit_evaluator=BackendGemmaAuditEvaluator(backend),
        repair_adapters=(
            BackendRepairCaller(backend),
            BackendRegionFidelityGate(backend),
            BackendQwenAuditEvaluator(backend),
            BackendGemmaAuditEvaluator(backend),
        ),
    )
    remote_fmt = _load_report(remote_cfg.out_dir, "formatting_report.json")

    # Identities match; the formatting step is backend-agnostic.
    for key in ("source_hash", "snapshot_hash", "chunk_plan_hash"):
        assert (
            local_result.record["identities"][key]
            == remote_result.record["identities"][key]
        )
    # Identical formatted text (per-PID, in order) and span mapping.
    assert (
        local_fmt["outcome"]["formatted_text"]
        == remote_fmt["outcome"]["formatted_text"]
    ), "formatted text must be identical across local and remote fakes"
    assert (
        local_fmt["outcome"]["span_mapping"]
        == remote_fmt["outcome"]["span_mapping"]
    )
    assert local_fmt["outcome"]["resolved_count"] == remote_fmt["outcome"]["resolved_count"]
    assert local_fmt["outcome"]["incident_count"] == 0
    assert remote_fmt["outcome"]["incident_count"] == 0
    # Model-free invariant on BOTH paths.
    assert local_fmt["outcome"]["model_call_count"] == 0
    assert remote_fmt["outcome"]["model_call_count"] == 0
    # Same terminal status and final integrity checks.
    assert local_result.step8["status"] == remote_result.step8["status"]
    local_report = _load_report(local_cfg.out_dir, "repair_report.json")
    remote_report = _load_report(remote_cfg.out_dir, "repair_report.json")
    for key in ("status", "missing_pids", "numeric_missing", "mixed_script",
                "glossary_missing", "qwen_smoke"):
        assert (
            local_report["integrity"][key]
            == remote_report["integrity"][key]
        ), key
    # The remote formatting report carries the remote backend identity.
    assert remote_fmt["backend_identity_hash"] == remote_cfg.backend.identity_hash
    assert all(
        "<em>" in text for _pid, text in remote_fmt["outcome"]["formatted_text"]
    ), "the deterministic tiers restored the markup on the remote path"
