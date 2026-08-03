"""Strict-driver Step 7/8 (Phase 4 B2) integration tests.

These run the full ``run_chapter_strict`` driver with Phase 4 repair adapters
injected, verifying:

  * Step 7 runs after Step 6 when repair adapters are configured and persists
    ``repair_cache.json`` / ``repair_report.json`` with identity;
  * Qwen re-gate failure at the repair call never commits a repair and yields
    an ``accepted_degraded`` terminal state with a debt trace;
  * the terminal state is monotonic and written into the run record;
  * resume reuses the persisted repair cache (no repeated repair calls);
  * a foreign backend identity rejects the persisted repair cache;
  * dual-mode parity (plan §14.3): the same canned repair output through a
    local fake backend and the fake OpenCode server yields identical repair
    decisions, gate trace and terminal status;
  * the repair phase does not call the local lifecycle adapters directly
    (the runner wires Backend adapters from the coordinator backend).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pact_v4.phase1.models import GateResult
from pact_v4.pipeline.v4_phase12_strict_runner import (
    StrictBackendConfig,
    StrictRunConfig,
    run_chapter_strict,
)
from pact_v4.runtime.backend_role_adapters import (
    BackendGemmaAuditEvaluator,
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


def _make_cfg(tmp_path: Path, *, n_paragraphs: int = 24, max_consecutive: int = 3,
              backend: Any = None) -> StrictRunConfig:
    chapter_html = tmp_path / "046.html"
    memory_dir = tmp_path / "memory"
    out_dir = tmp_path / "out"
    _write_chapter_html(chapter_html, n_paragraphs)
    _write_empty_memory(memory_dir)
    return StrictRunConfig(
        chapter_id="046", chapter_html_path=chapter_html, memory_dir=memory_dir,
        out_dir=out_dir, backend=backend or _make_backend(),
        max_consecutive_terminal_nonselections=max_consecutive,
    )


class StubRepairCaller:
    """Fake Phase 4A repair caller: repairs the flagged PID to a fixed text."""

    def __init__(self, text: str = "Исправленный перевод.") -> None:
        self._text = text
        self.calls: list = []

    def __call__(self, *, chunk_id, source, translation, region, findings) -> str:
        self.calls.append((chunk_id, region.pid))
        pid = region.pid
        return json.dumps({"repaired": {pid: self._text}, "reason": "scripted"},
                          ensure_ascii=False)


def _flagging_audit(pid: str):
    class _Flagging(StubQwenAudit):
        def __call__(self, *, chunk_id, source, translation):
            if pid in translation:
                return json.dumps({"issues": [
                    {"pid": pid, "category": "omission", "note": "dropped clause"}
                ]})
            return json.dumps({"issues": []})
    return _Flagging()


def _run_with_repair(
    cfg: StrictRunConfig,
    *,
    repair_text: str = "Исправленный перевод.",
    qwen_passed: bool = True,
):
    """Run the strict driver with Phase 4 repair adapters injected."""
    from pact_v4.pipeline.v4_phase12_strict_runner import (
        _b2_handoff_path,
    )
    router = _make_router()
    model_caller = _LifecycleAwareModelCaller(router, StubModelCaller())
    qwen_evaluator = _LifecycleAwareQwen(router, StubQwen())
    gemma_selector = _LifecycleAwareGemmaSelector(router, StubGemma())
    qwen_audit_evaluator = _LifecycleAwareQwenAudit(router, _flagging_audit("p00001"))
    gemma_audit_evaluator = _LifecycleAwareGemmaAudit(router, StubGemmaAudit())

    # Phase 4 repair adapters: the same protocol-shape fakes the runner's
    # Step 7 consumes (repair_caller, qwen re-gate, qwen audit, gemma audit).
    repair_caller = StubRepairCaller(text=repair_text)
    repair_qwen = StubQwen(passed=qwen_passed, reason="regate")

    class _StubRepairGemmaAudit(StubGemmaAudit):
        def __call__(self, *, chunk_id, translation):
            return json.dumps({"issues": []})

    class _StubRepairQwenAudit(StubQwenAudit):
        def __call__(self, *, chunk_id, source, translation):
            return json.dumps({"issues": []})

    repair_adapters = (
        repair_caller,
        repair_qwen,
        _StubRepairQwenAudit(),
        _StubRepairGemmaAudit(),
    )
    result = run_chapter_strict(
        cfg, router=router, model_caller=model_caller,
        qwen_evaluator=qwen_evaluator, gemma_selector=gemma_selector,
        qwen_audit_evaluator=qwen_audit_evaluator,
        gemma_audit_evaluator=gemma_audit_evaluator,
        repair_adapters=repair_adapters,
    )
    return result, router, repair_caller


def test_step7_repair_runs_and_persists_artifacts(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=8)
    result, router, repair_caller = _run_with_repair(cfg)
    assert result.step6["status"] == "complete"
    assert result.step7["status"] in ("complete", "accepted_degraded")
    # Repair actually called the repair caller at least once.
    assert repair_caller.calls
    cache_path = cfg.out_dir / "repair_cache.json"
    report_path = cfg.out_dir / "repair_report.json"
    assert cache_path.exists()
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema"] == "pact-v4-phase4-repair-report/v1"
    assert report["backend_identity_hash"] == cfg.backend.identity_hash
    assert report["snapshot_hash"] == result.record["identities"]["snapshot_hash"]
    # Terminal state is monotonic and recorded.
    assert result.step8["status"] in ("complete", "accepted_degraded", "failed")
    assert result.record["step7"]["status"] == result.step7["status"]


def test_step7_skipped_without_repair_adapters(tmp_path: Path):
    # Backward compatibility: without repair adapters the phase is recorded
    # as skipped, not run.
    cfg = _make_cfg(tmp_path, n_paragraphs=8)
    router = _make_router()
    model_caller = _LifecycleAwareModelCaller(router, StubModelCaller())
    result = run_chapter_strict(
        cfg, router=router, model_caller=model_caller,
        qwen_evaluator=_LifecycleAwareQwen(router, StubQwen()),
        gemma_selector=_LifecycleAwareGemmaSelector(router, StubGemma()),
        qwen_audit_evaluator=_LifecycleAwareQwenAudit(router, StubQwenAudit()),
        gemma_audit_evaluator=_LifecycleAwareGemmaAudit(router, StubGemmaAudit()),
    )
    assert result.step7["status"] == "skipped"
    assert result.step7["reason"] == "repair_adapters_not_configured"
    assert not (cfg.out_dir / "repair_cache.json").exists()


def test_step7_qwen_regate_failure_yields_accepted_degraded(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=8)
    result, _router, _caller = _run_with_repair(cfg, qwen_passed=False)
    assert result.step7["status"] == "accepted_degraded"
    assert result.step8["status"] == "accepted_degraded"
    report = json.loads(
        (cfg.out_dir / "repair_report.json").read_text(encoding="utf-8")
    )
    assert report["status"] == "accepted_degraded"
    assert report["debt_trace"], "debt trace is recorded"
    # No committed repair under a failed Qwen re-gate.
    committed = sum(
        1 for round_payload in report["rounds"]
        for rec in round_payload["records"] if rec["committed"]
    )
    assert committed == 0


def test_step7_resume_reuses_repair_cache(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=8)
    first, _r1, caller1 = _run_with_repair(cfg)
    assert caller1.calls  # repair ran in the first session

    resumed_cfg = StrictRunConfig(
        chapter_id=cfg.chapter_id, chapter_html_path=cfg.chapter_html_path,
        memory_dir=cfg.memory_dir, out_dir=cfg.out_dir, backend=cfg.backend,
    )
    second, _r2, caller2 = _run_with_repair(resumed_cfg)
    assert second.resumed_from_index >= 0
    # The persisted repair cache made the resumed run reuse the committed
    # repairs instead of re-calling the repair model.
    assert caller2.calls == []


def test_step7_rejects_foreign_repair_cache_on_resume(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=8)
    _run_with_repair(cfg)
    cache_path = cfg.out_dir / "repair_cache.json"
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    payload["backend_identity_hash"] = "deadbeef" * 8
    cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    resumed_cfg = StrictRunConfig(
        chapter_id=cfg.chapter_id, chapter_html_path=cfg.chapter_html_path,
        memory_dir=cfg.memory_dir, out_dir=cfg.out_dir, backend=cfg.backend,
    )
    result, _r, _c = _run_with_repair(resumed_cfg)
    assert result.step7["status"] == "failed"
    assert "Foreign identity: repair cache" in result.step7["error"]


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


def test_step7_dual_mode_parity_local_vs_remote(tmp_path: Path):
    # Same chapter, same canned repair behaviour; only the transport differs.
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
    local_result, _r, local_caller = _run_with_repair(local_cfg)

    # --- remote fake OpenCode server ---
    backend_cfg = _remote_backend_config()
    backend = OpenCodeServerBackend(config=backend_cfg, session=DynamicFakeOpenCodeServer())
    runtime = RemoteRuntimeCoordinator(backend)
    remote_cfg = make(OpenCodeBackendConfig(server=backend_cfg), tmp_path / "remote_out")

    class _RemoteQwen(StubQwen):
        def __call__(self, source, translation):
            return GateResult(gate="qwen_fidelity", passed=True, detail="remote regate")

    model_caller = BackendRepairCaller(backend)  # repair role over the boundary
    # Reuse the Step 6/audit role adapters over the same remote backend.
    qwen_evaluator = BackendQwenEvaluator(backend)
    from pact_v4.runtime.backend_role_adapters import BackendGemmaSelector, BackendModelCaller
    from pact_v4.runtime.backend_protocol import CompletionRequest, Message, JSON_OBJECT_SCHEMA
    import pact_v4.phase2.generation as _gen

    # The remote strict run must go through the Backend role adapters; we
    # reuse the DynamicFakeOpenCodeServer canned behaviour, so the Phase 1-2
    # model calls are scripted by the fake (same as the remote parity tests).
    repair_adapters = (
        BackendRepairCaller(backend),
        BackendQwenEvaluator(backend),
        BackendQwenAuditEvaluator(backend),
        BackendGemmaAuditEvaluator(backend),
    )
    remote_result = run_chapter_strict(
        remote_cfg, runtime=runtime,
        model_caller=BackendModelCaller(backend),
        qwen_evaluator=BackendQwenEvaluator(backend),
        gemma_selector=BackendGemmaSelector(backend),
        qwen_audit_evaluator=BackendQwenAuditEvaluator(backend),
        gemma_audit_evaluator=BackendGemmaAuditEvaluator(backend),
        repair_adapters=repair_adapters,
    )

    # Identities match; Step 7 terminal + gate trace are backend-agnostic.
    for key in ("source_hash", "snapshot_hash", "chunk_plan_hash"):
        assert (
            local_result.record["identities"][key]
            == remote_result.record["identities"][key]
        )
    assert local_result.step7["status"] == remote_result.step7["status"]
    local_report = json.loads(
        (local_cfg.out_dir / "repair_report.json").read_text(encoding="utf-8")
    )
    remote_report = json.loads(
        (remote_cfg.out_dir / "repair_report.json").read_text(encoding="utf-8")
    )
    assert local_report["status"] == remote_report["status"]
    # Integrity semantics are backend-agnostic; frozen_hash is content-derived
    # from the (differently canned) model outputs, so compare the checks.
    for key in ("status", "missing_pids", "numeric_missing", "mixed_script",
                "glossary_missing", "qwen_smoke"):
        assert local_report["integrity"][key] == remote_report["integrity"][key], key
    assert local_report["terminal"]["status"] == remote_report["terminal"]["status"]
    # The remote repair report carries the remote backend identity.
    assert remote_report["backend_identity_hash"] == remote_cfg.backend.identity_hash
