"""Remote/composite strict-driver tests over the fake OpenCode server.

V4 C2 / PR 3 gate: ``run_chapter_strict`` runs the *same* chapter through
the real ``OpenCodeServerBackend`` wire contract (backed by the offline
``DynamicFakeOpenCodeServer``) as it does through the local fakes, and the
resume/identity rules of plan §11 / §14.4 hold:

* remote run completes with a v2 record (``backend`` + ``runtime`` blocks,
  usage/cost aggregated, ``null`` when not reported);
* resume skips committed chunks and needs no old OpenCode session;
* a model-binding change rejects the journal;
* a structured-output policy change rejects the journal;
* an API-key rotation does **not** invalidate resume;
* a composite profile never reuses artifacts of a different routing map;
* parity (§14.3): source/snapshot/plan identities and committed
  translations match between local and remote fake runs.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from pact_v4.pipeline.v4_phase12_strict_runner import (
    StrictRunConfig,
    run_chapter_strict,
)
from pact_v4.runtime.backend_role_adapters import (
    BackendGemmaAuditEvaluator,
    BackendGemmaSelector,
    BackendModelCaller,
    BackendQwenAuditEvaluator,
    BackendQwenEvaluator,
)
from pact_v4.runtime.opencode_backend import (
    OpenCodeServerBackend,
    OpenCodeServerBackendConfig,
)
from pact_v4.runtime.runtime_config import (
    CompositeBackendConfig,
    LocalLlamaBackendConfig,
    OpenCodeBackendConfig,
)
from pact_v4.runtime.runtime_coordinator import RemoteRuntimeCoordinator
from tests.pact_v4.pipeline.test_v4_phase12_strict_runner import (
    FakeLifecycleAdapter,
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

ROLE_BINDINGS = {
    "default": "opencode-go/deepseek-v4-flash",
    "generator": "opencode-go/deepseek-v4-flash",
    "fidelity_reviewer": "opencode-go/qwen3.7-plus",
    "russian_selector": "opencode-go/qwen3.7-plus",
    "qwen_audit": "opencode-go/qwen3.7-plus",
    "gemma_audit": "opencode-go/qwen3.7-plus",
}


def _write_chapter_html(path: Path, n_paragraphs: int) -> None:
    paragraph_text = " ".join(f"word{i}" for i in range(WORDS_PER_PARAGRAPH))
    body = "\n".join(f"<p>{paragraph_text}</p>" for _ in range(n_paragraphs))
    path.write_text("<html><body>" + body + "</body></html>", encoding="utf-8")


def _write_empty_memory(dir_path: Path) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "glossary.json").write_text("{}", encoding="utf-8")
    (dir_path / "book_memory.json").write_text("{}", encoding="utf-8")


def _local_backend() -> LocalLlamaBackendConfig:
    return LocalLlamaBackendConfig(
        exe=Path("C:/fake/llama-server.exe"), device="FAKE0", host="127.0.0.1",
        model_paths={"gemma": Path("C:/fake/gemma.gguf"), "qwen": Path("C:/fake/qwen.gguf")},
        model_names={"gemma": "gemma-fake", "qwen": "qwen-fake"},
        server_args={"gemma": [], "qwen": []}, port=0,
    )


def _remote_backend_config(
    *, bindings: Optional[dict] = None, password: str = "secret-A",
    structured_output_mode: str = "prompt_only",
) -> OpenCodeServerBackendConfig:
    return OpenCodeServerBackendConfig(
        base_url="http://127.0.0.1:4096",
        username="pact",
        password=password,
        model_bindings=dict(bindings or ROLE_BINDINGS),
        structured_output_mode=structured_output_mode,
    )


def _make_cfg(
    tmp_path: Path, *, backend: Any, n_paragraphs: int = 24,
) -> StrictRunConfig:
    chapter_html = tmp_path / "046.html"
    memory_dir = tmp_path / "memory"
    out_dir = tmp_path / "out"
    chapter_html.parent.mkdir(parents=True, exist_ok=True)
    memory_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_chapter_html(chapter_html, n_paragraphs)
    _write_empty_memory(memory_dir)
    return StrictRunConfig(
        chapter_id="046", chapter_html_path=chapter_html, memory_dir=memory_dir,
        out_dir=out_dir, backend=backend,
    )


def _remote_wiring(fake: DynamicFakeOpenCodeServer, backend_cfg: OpenCodeServerBackendConfig):
    backend = OpenCodeServerBackend(config=backend_cfg, session=fake)
    runtime = RemoteRuntimeCoordinator(backend)
    return runtime, backend, [
        BackendModelCaller(backend),
        BackendQwenEvaluator(backend),
        BackendGemmaSelector(backend),
        BackendQwenAuditEvaluator(backend),
        BackendGemmaAuditEvaluator(backend),
    ]


def _run_remote(
    cfg: StrictRunConfig,
    *,
    fake: Optional[DynamicFakeOpenCodeServer] = None,
    backend_cfg: Optional[OpenCodeServerBackendConfig] = None,
):
    fake = fake or DynamicFakeOpenCodeServer()
    backend_cfg = backend_cfg or _remote_backend_config()
    runtime, _backend, adapters = _remote_wiring(fake, backend_cfg)
    model_caller, qwen_evaluator, gemma_selector, qwen_audit, gemma_audit = adapters
    result = run_chapter_strict(
        cfg, runtime=runtime, model_caller=model_caller,
        qwen_evaluator=qwen_evaluator, gemma_selector=gemma_selector,
        qwen_audit_evaluator=qwen_audit, gemma_audit_evaluator=gemma_audit,
    )
    return result, fake


def _run_local(cfg: StrictRunConfig):
    router = _make_router()
    model_caller = _LifecycleAwareModelCaller(router, StubModelCaller())
    qwen_evaluator = _LifecycleAwareQwen(router, StubQwen())
    gemma_selector = _LifecycleAwareGemmaSelector(router, StubGemma())
    qwen_audit = _LifecycleAwareQwenAudit(router, StubQwenAudit())
    gemma_audit = _LifecycleAwareGemmaAudit(router, StubGemmaAudit())
    result = run_chapter_strict(
        cfg, router=router, model_caller=model_caller,
        qwen_evaluator=qwen_evaluator, gemma_selector=gemma_selector,
        qwen_audit_evaluator=qwen_audit, gemma_audit_evaluator=gemma_audit,
    )
    return result


# ---------------------------------------------------------------------------
# Remote end-to-end (PR 3 gate)
# ---------------------------------------------------------------------------


def test_remote_run_completes_same_chapter_as_local(tmp_path: Path):
    remote_cfg = _make_cfg(tmp_path, backend=OpenCodeBackendConfig(
        server=_remote_backend_config(),
    ))
    result, fake = _run_remote(remote_cfg)
    assert result.chunk_count == 2
    assert result.selected_count == 2
    assert result.processed_count == 2
    assert result.step6["status"] == "complete"
    # Every call went through the real OpenCode wire contract (fake server).
    assert fake.message_count() >= 4
    # Successful sessions are deleted per session policy (per_request).
    assert fake.sessions == {}


def test_remote_record_v2_has_backend_and_runtime_blocks(tmp_path: Path):
    remote_cfg = _make_cfg(tmp_path, backend=OpenCodeBackendConfig(
        server=_remote_backend_config(),
    ))
    result, _fake = _run_remote(remote_cfg)
    record = result.record
    assert record["schema"] == "pact-v4-strict-chapter-trial/v2"
    # Backend block (plan §9.3).
    assert record["backend"]["kind"] == "opencode_server"
    assert record["backend"]["identity_hash"] == remote_cfg.backend.identity_hash
    assert record["backend"]["public_endpoint"] == "http://127.0.0.1:4096"
    assert record["backend"]["model_bindings"]["generator"] == "opencode-go/deepseek-v4-flash"
    # Runtime block: local_lifecycle null for remote, remote_calls aggregated.
    assert record["runtime"]["local_lifecycle"] is None
    remote_calls = record["runtime"]["remote_calls"]
    assert remote_calls["count"] >= 4  # gen + qwen + audit calls for 2 chunks
    assert remote_calls["input_tokens"] > 0
    assert remote_calls["output_tokens"] > 0
    assert remote_calls["reported_cost"] is not None  # provider reported cost
    # The legacy lifecycle block stays a valid (empty) shape for old readers.
    assert record["lifecycle"]["startup_count"] == 0
    assert record["lifecycle"]["switches"] == []


def test_remote_journal_has_backend_event_indices_and_empty_switch_indices(tmp_path: Path):
    remote_cfg = _make_cfg(tmp_path, backend=OpenCodeBackendConfig(
        server=_remote_backend_config(),
    ))
    result, _fake = _run_remote(remote_cfg)
    entries = [
        json.loads(line)
        for line in result.journal_path.read_text(encoding="utf-8").splitlines()
    ]
    assert entries
    assert entries[0]["schema"] == "pact-v4-strict-chapter-trial-journal/v2"
    for entry in entries:
        # Remote runs have no local switches; indices reference call events.
        assert entry["switch_indices"] == []
        assert isinstance(entry["backend_event_indices"], list)
        assert entry["backend_event_indices"], "at least one call event per chunk"


# ---------------------------------------------------------------------------
# Resume (§14.4)
# ---------------------------------------------------------------------------


def test_remote_resume_skips_committed_chunks_and_needs_no_old_session(tmp_path: Path):
    backend_cfg = _remote_backend_config()
    remote_cfg = _make_cfg(tmp_path, backend=OpenCodeBackendConfig(server=backend_cfg))
    first, fake1 = _run_remote(remote_cfg)
    assert first.processed_count == 2
    # Per-request sessions are gone after the first run; the resumed run must
    # not depend on them.
    assert fake1.sessions == {}

    resumed_cfg = _make_cfg(tmp_path, backend=OpenCodeBackendConfig(server=backend_cfg))
    second, fake2 = _run_remote(resumed_cfg, fake=DynamicFakeOpenCodeServer())
    assert second.resumed_from_index == 2
    assert second.processed_count == 2
    # No committed chunk is redone and the audit cache is reused: zero calls.
    assert fake2.message_count() == 0


def test_remote_resume_rejects_model_binding_change(tmp_path: Path):
    base_cfg = _remote_backend_config()
    remote_cfg = _make_cfg(tmp_path, backend=OpenCodeBackendConfig(server=base_cfg))
    _run_remote(remote_cfg)

    changed_bindings = dict(ROLE_BINDINGS)
    changed_bindings["fidelity_reviewer"] = "opencode-go/deepseek-v4-flash"
    changed_cfg = _remote_backend_config(bindings=changed_bindings)
    resumed_cfg = _make_cfg(tmp_path, backend=OpenCodeBackendConfig(server=changed_cfg))
    try:
        _run_remote(resumed_cfg)
    except ValueError as exc:
        assert "Foreign identity" in str(exc)
    else:
        raise AssertionError("expected Foreign identity ValueError on model-binding change")


def test_remote_resume_rejects_structured_output_policy_change(tmp_path: Path):
    remote_cfg = _make_cfg(tmp_path, backend=OpenCodeBackendConfig(
        server=_remote_backend_config(structured_output_mode="prompt_only"),
    ))
    _run_remote(remote_cfg)

    changed_cfg = _remote_backend_config(structured_output_mode="json_schema")
    resumed_cfg = _make_cfg(tmp_path, backend=OpenCodeBackendConfig(server=changed_cfg))
    try:
        _run_remote(resumed_cfg)
    except ValueError as exc:
        assert "Foreign identity" in str(exc)
    else:
        raise AssertionError("expected Foreign identity ValueError on policy change")


def test_remote_resume_api_key_rotation_does_not_invalidate(tmp_path: Path):
    remote_cfg = _make_cfg(tmp_path, backend=OpenCodeBackendConfig(
        server=_remote_backend_config(password="secret-A"),
    ))
    first, _fake = _run_remote(remote_cfg)
    # No artifact/log of the first run contains the credential value.
    for path in (first.record_path, first.journal_path, first.out_dir / "translations.json"):
        blob = path.read_text(encoding="utf-8")
        assert "secret-A" not in blob, f"credential leaked into {path}"

    rotated_cfg = _remote_backend_config(password="rotated-secret-B")
    resumed_cfg = _make_cfg(tmp_path, backend=OpenCodeBackendConfig(server=rotated_cfg))
    result, _fake = _run_remote(resumed_cfg)
    assert result.resumed_from_index == 2
    assert result.processed_count == 2


def test_composite_does_not_reuse_artifacts_of_other_routing_map(tmp_path: Path):
    remote_sub = OpenCodeBackendConfig(server=_remote_backend_config())
    local_sub = _local_backend()
    backends = {"local": local_sub, "remote": remote_sub}
    map_a = {
        "generator": "remote", "fidelity_reviewer": "remote",
        "russian_selector": "local", "qwen_audit": "remote", "gemma_audit": "local",
    }
    map_b = {
        "generator": "remote", "fidelity_reviewer": "remote",
        "russian_selector": "remote", "qwen_audit": "remote", "gemma_audit": "remote",
    }
    composite_a = CompositeBackendConfig(backends=backends, role_backend_map=map_a)
    cfg_a = _make_cfg(tmp_path, backend=composite_a)
    # First composite run completes (injected remote runtime serves all roles;
    # the composite *identity* recorded in the journal is what matters here).
    _run_remote(cfg_a)

    composite_b = CompositeBackendConfig(backends=backends, role_backend_map=map_b)
    cfg_b = _make_cfg(tmp_path, backend=composite_b)
    assert composite_a.identity_hash != composite_b.identity_hash
    try:
        _run_remote(cfg_b)
    except ValueError as exc:
        assert "Foreign identity" in str(exc)
    else:
        raise AssertionError(
            "expected Foreign identity ValueError when routing map changes"
        )


# ---------------------------------------------------------------------------
# Parity (§14.3): same chapter, local vs remote fake
# ---------------------------------------------------------------------------


def test_local_remote_parity_identities_and_translations(tmp_path: Path):
    # Same chapter/memory for both runs; only the out dir and backend differ.
    chapter_html = tmp_path / "046.html"
    memory_dir = tmp_path / "memory"
    _write_chapter_html(chapter_html, 24)
    _write_empty_memory(memory_dir)

    def make(backend: Any, out_dir: Path) -> StrictRunConfig:
        return StrictRunConfig(
            chapter_id="046", chapter_html_path=chapter_html, memory_dir=memory_dir,
            out_dir=out_dir, backend=backend,
        )

    local_cfg = make(_local_backend(), tmp_path / "local_out")
    remote_cfg = make(
        OpenCodeBackendConfig(server=_remote_backend_config()),
        tmp_path / "remote_out",
    )
    local_result = _run_local(local_cfg)
    remote_result, _fake = _run_remote(remote_cfg)

    # Source/snapshot/chunk-plan identities must not depend on the backend.
    for key in ("source_hash", "snapshot_hash", "chunk_plan_hash"):
        assert local_result.record["identities"][key] == remote_result.record["identities"][key]
    # Committed translations are byte-identical (same canned behaviour).
    local_text = json.loads(local_result.translations_path.read_text(encoding="utf-8"))
    remote_text = json.loads(remote_result.translations_path.read_text(encoding="utf-8"))
    assert local_text == remote_text
    assert all(text.startswith("Перевод номер") for text in remote_text.values())


def test_remote_run_config_round_trips_loaded_remote_budget(tmp_path: Path):
    """B11 regression: a runtime-config payload with ``remote_budget`` loads
    through the real ``load_runtime_config`` path, flows into a
    ``StrictRunConfig``, and the run's journal/record identity round-trips
    against the config's backend identity (budget is identity-bound, so the
    loaded 500 must be the identity the journal is written under)."""
    from pact_v4.runtime.runtime_config import load_runtime_config

    payload = {
        "kind": "opencode_server",
        "server_mode": "external",
        "base_url": "http://127.0.0.1:4096",
        "model_bindings": dict(ROLE_BINDINGS),
        "remote_budget": {"max_requests_per_chapter": 500},
    }
    backend = load_runtime_config(payload)
    assert isinstance(backend, OpenCodeBackendConfig)
    assert backend.server.remote_budget.max_requests_per_chapter == 500
    cfg = _make_cfg(tmp_path, backend=backend)
    # Round-trip through the same loader must give the same identity.
    reloaded = load_runtime_config(payload)
    assert reloaded.identity_hash == backend.identity_hash
    assert reloaded.server.remote_budget.max_requests_per_chapter == 500

    result, _fake = _run_remote(cfg)
    # The record's backend identity equals the config identity (the budget
    # participates), and the loaded budget made it into the descriptor.
    assert result.record["backend"]["identity_hash"] == cfg.backend.identity_hash
    assert result.record["backend"]["identity_hash"] == backend.identity_hash
    desc = cfg.backend.build_descriptor()
    assert desc.effective_options["remote_budget"]["max_requests_per_chapter"] == 500
