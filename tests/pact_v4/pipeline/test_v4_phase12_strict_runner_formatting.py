"""Strict-driver Phase 5 formatting (B3) integration tests.

These run the full ``run_chapter_strict`` driver with Phase 4 repair + Phase
5 formatting adapters injected, verifying:

  * formatting runs **between** Step 7 (convergence) and Step 8 (terminal):
    the formatting report and the repair report's ``final_translation`` carry
    the formatted text, so Step 8 and the terminal transition see the same
    text that goes into ``complete``;
  * an unresolved required span is a blocking incident: with the production
    default ``max_formatting_incidents=0`` the chapter degrades to
    ``accepted_degraded`` (valid PID map + debt trace), never ``failed`` from
    a formatting transport failure alone;
  * without formatting adapters the step is skipped (backward compatible);
  * the formatting artifacts carry the backend identity;
  * dual-mode parity (§14.3): local fake backend vs fake OpenCode server —
    the same canned formatting output produces identical formatted text and
    final integrity result, and the model-fallback tier runs only through the
    Backend boundary (no local lifecycle adapters).
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
    BackendFormattingCaller,
    BackendGemmaAuditEvaluator,
    BackendGemmaSelector,
    BackendModelCaller,
    BackendQwenAuditEvaluator,
    BackendQwenEvaluator,
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
    # a span contract to restore (the stub generation drops the emphasised
    # word, forcing the model-fallback tier deterministically).
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


class StubRepairCaller:
    """Phase 4 repair caller; the audit produces no findings so it is unused."""

    def __call__(self, *, chunk_id, source, translation, region, findings) -> str:
        raise AssertionError("no findings expected in the formatting integration fixture")


class CannedFormattingCaller:
    """Fake ``FormattingCaller``: span i -> word i of the translation.

    Mirrors ``tests.pact_v4.runtime.opencode_dynamic_fake._formatting_response``
    so local and remote fake runs produce identical mappings.
    """

    def __init__(self, *, fail: Exception | None = None, empty: bool = False) -> None:
        self.fail = fail
        self.empty = empty
        self.calls: list = []

    def __call__(self, *, pid, source_text, translation, spans) -> str:
        self.calls.append((pid, translation))
        if self.fail is not None:
            raise self.fail
        words = translation.split()
        mappings = []
        for index, span in enumerate(spans):
            target = "" if self.empty else (words[index] if index < len(words) else "")
            mappings.append({
                "pid": pid, "span_id": span["span_id"],
                "target_text": target, "occurrence": 1,
            })
        return json.dumps({"mappings": mappings}, ensure_ascii=False)


def _clean_repair_adapters() -> tuple:
    repair_caller = StubRepairCaller()
    repair_qwen = StubQwen(passed=True, reason="regate")

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


def _run_local(
    cfg: StrictRunConfig,
    *,
    formatting_caller: Any = None,
    formatting_adapters: Any = None,
):
    router = _make_router()
    model_caller = _LifecycleAwareModelCaller(router, StubModelCaller())
    qwen_evaluator = _LifecycleAwareQwen(router, StubQwen())
    gemma_selector = _LifecycleAwareGemmaSelector(router, StubGemma())
    qwen_audit_evaluator = _LifecycleAwareQwenAudit(router, StubQwenAudit())
    gemma_audit_evaluator = _LifecycleAwareGemmaAudit(router, StubGemmaAudit())
    if formatting_adapters is None:
        formatting_adapters = (formatting_caller,) if formatting_caller is not None else None
    result = run_chapter_strict(
        cfg, router=router, model_caller=model_caller,
        qwen_evaluator=qwen_evaluator, gemma_selector=gemma_selector,
        qwen_audit_evaluator=qwen_audit_evaluator,
        gemma_audit_evaluator=gemma_audit_evaluator,
        repair_adapters=_clean_repair_adapters(),
        formatting_adapters=formatting_adapters,
    )
    return result


def _load_report(out_dir: Path, name: str) -> dict:
    return json.loads((out_dir / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Formatting runs between Step 7 and Step 8; terminal sees the final text
# ---------------------------------------------------------------------------


def test_formatting_runs_between_step7_and_step8(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=8)
    result = _run_local(cfg, formatting_caller=CannedFormattingCaller())

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
    assert fmt_report["outcome"]["model_fallback_count"] > 0
    # Terminal sees the same text: integrity's frozen hash covers formatted.
    assert result.step8["status"] == result.step7["terminal"]


def test_formatting_incident_yields_accepted_degraded(tmp_path: Path):
    # The formatting caller reports "no fragment" for every span -> blocking
    # incidents -> with max_formatting_incidents=0 the chapter degrades to
    # accepted_degraded (valid PID map + debt trace), never `failed`.
    cfg = _make_cfg(tmp_path, n_paragraphs=8)
    result = _run_local(cfg, formatting_caller=CannedFormattingCaller(empty=True))
    assert result.step7["status"] == "accepted_degraded"
    assert result.step8["status"] == "accepted_degraded"
    assert result.step7["formatting"]["status"] == "blocking"
    assert result.step7["formatting"]["incident_count"] > 0
    report = _load_report(cfg.out_dir, "repair_report.json")
    assert any("formatting:" in reason for reason in report["debt_trace"])
    fmt_report = _load_report(cfg.out_dir, "formatting_report.json")
    assert fmt_report["outcome"]["blocking"] is True


def test_formatting_transport_failure_is_debt_not_failed(tmp_path: Path):
    # A transport failure at the model fallback is recorded as debt, so the
    # terminal is accepted_degraded (valid PID map), never `failed` because
    # of transport — "transport failure != semantic gate failure".
    cfg = _make_cfg(tmp_path, n_paragraphs=8)
    result = _run_local(cfg, formatting_caller=CannedFormattingCaller(
        fail=RuntimeError("network down"),
    ))
    assert result.step7["status"] == "accepted_degraded"
    fmt_report = _load_report(cfg.out_dir, "formatting_report.json")
    assert fmt_report["outcome"]["incidents"][0]["reason"] == "transport_error"
    assert result.step8["status"] == "accepted_degraded"


def test_formatting_skipped_without_formatting_adapters(tmp_path: Path):
    # Backward compatibility: without formatting adapters the step is not run.
    cfg = _make_cfg(tmp_path, n_paragraphs=8)
    result = _run_local(cfg)
    assert result.step7["formatting"] is None
    assert result.step8["formatting"] is None
    assert not (cfg.out_dir / "formatting_report.json").exists()
    # And the repair report's final_translation is plain (unformatted).
    report = _load_report(cfg.out_dir, "repair_report.json")
    assert all("<em>" not in text for _pid, text in report["final_translation"])


def test_formatting_skipped_when_formatting_not_required(tmp_path: Path):
    # ``formatting_required=False`` is the runtime master switch (§6.1
    # ``formatting.required=true``): even with formatting adapters wired, the
    # step is skipped entirely — adapters alone never trigger it.
    cfg = _make_cfg(tmp_path, n_paragraphs=8)
    cfg = StrictRunConfig(
        chapter_id=cfg.chapter_id, chapter_html_path=cfg.chapter_html_path,
        memory_dir=cfg.memory_dir, out_dir=cfg.out_dir, backend=cfg.backend,
        formatting_required=False,
    )
    result = _run_local(cfg, formatting_caller=CannedFormattingCaller())
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
        formatting_adapters=(CannedFormattingCaller(),),
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
    "formatting": "opencode-go/deepseek-v4-flash",
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
    # Same chapter, same canned formatting behaviour; only the transport
    # differs. The formatting step must produce byte-identical formatted text
    # and the same final integrity result through local and remote fakes.
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
    local_result = _run_local(local_cfg, formatting_caller=CannedFormattingCaller())
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
            BackendQwenEvaluator(backend),
            BackendQwenAuditEvaluator(backend),
            BackendGemmaAuditEvaluator(backend),
        ),
        formatting_adapters=(BackendFormattingCaller(backend),),
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
    # The remote formatting report carries the remote backend identity; the
    # model fallback went through the Backend boundary (fake OpenCode server
    # answered the Phase 5 prompt).
    assert remote_fmt["backend_identity_hash"] == remote_cfg.backend.identity_hash
    assert all(
        "<em>" in text for _pid, text in remote_fmt["outcome"]["formatted_text"]
    ), "the remote fake server answered the formatting prompt"
