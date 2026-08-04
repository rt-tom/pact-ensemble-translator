"""Offline tests for the C3 role-backend bridge (PR 4).

Covers ``build_role_backend`` / ``build_role_adapters``
(``pact_v4.runtime.runtime_config``), the composite coordinator's attached
``CompletionBackend``, composite routing by model_ref, and the
``BackendModelCaller`` generator-alias resolution that makes the plan §8
config shape (``roles.generator``) work. No network, no subprocess, no
``llama-server``: the local router is wired to a fake lifecycle adapter and
remote backends are constructed but never called.
"""
from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

import pytest

from pact_v4.phase2.generation import GenerationParams, PromptBundle
from pact_v4.phase2.prompts import FIDELITY_FIRST_V1
from pact_v4.runtime.backend_protocol import (
    BackendCallRecord,
    BackendDescriptor,
    CompletionError,
    CompletionRequest,
    CompletionResponse,
    Message,
)
from pact_v4.runtime.backend_role_adapters import (
    BackendGemmaAuditEvaluator,
    BackendGemmaSelector,
    BackendModelCaller,
    BackendQwenAuditEvaluator,
    BackendQwenEvaluator,
)
from pact_v4.runtime.model_lifecycle import ModelRouter
from pact_v4.runtime.opencode_backend import (
    OpenCodeServerBackend,
    OpenCodeServerBackendConfig,
)
from pact_v4.runtime.runtime_config import (
    CompositeBackendConfig,
    CompositeCompletionBackend,
    LocalLlamaBackendConfig,
    LocalRoutingBackend,
    OpenCodeBackendConfig,
    build_formatting_adapters,
    build_repair_adapters,
    build_role_adapters,
    build_role_backend,
)
from pact_v4.runtime.runtime_coordinator import (
    CompositeRuntimeCoordinator,
    LocalLifecycleCoordinator,
    RemoteRuntimeCoordinator,
)


class _FakeLifecycleAdapter:
    def __init__(self) -> None:
        self.calls: list = []

    def start(self, model_key, profile, extra_args, retries=1):
        self.calls.append(("start", model_key))
        return 1.5, 0

    def stop(self):
        self.calls.append(("stop", ""))
        return 0.5, True, 0

    def sample_vram(self):
        return 1024 * 1024 * 100


def _make_router() -> ModelRouter:
    return ModelRouter(
        _FakeLifecycleAdapter(),
        role_profile_names={"gemma": "Gemma", "qwen": "Qwen"},
        role_args={"gemma": [], "qwen": []},
    )


def _local_cfg() -> LocalLlamaBackendConfig:
    return LocalLlamaBackendConfig(
        exe=Path("C:/fake/llama-server.exe"), device="FAKE0", host="127.0.0.1",
        model_paths={"gemma": Path("C:/fake/gemma.gguf"), "qwen": Path("C:/fake/qwen.gguf")},
        model_names={"gemma": "gemma-fake", "qwen": "qwen-fake"},
        server_args={"gemma": [], "qwen": []},
        port=8093,
    )


def _remote_cfg(*, managed: bool = False) -> OpenCodeBackendConfig:
    return OpenCodeBackendConfig(
        server=OpenCodeServerBackendConfig(
            base_url="http://127.0.0.1:4096",
            model_bindings={
                "generator": "opencode-go/deepseek-v4-flash",
                "fidelity_reviewer": "opencode-go/qwen3.7-plus",
                "russian_selector": "opencode-go/qwen3.7-plus",
            },
        ),
        server_mode="managed" if managed else "external",
    )


def _composite_cfg() -> CompositeBackendConfig:
    return CompositeBackendConfig(
        backends={"local": _local_cfg(), "opencode": _remote_cfg()},
        role_backend_map={
            "generator": "opencode",
            "fidelity_reviewer": "opencode",
            "russian_selector": "local",
        },
    )


# ---------------------------------------------------------------------------
# build_role_backend
# ---------------------------------------------------------------------------


def test_local_role_backend_is_local_routing_over_router():
    cfg = _local_cfg()
    runtime = cfg.build_runtime(log_dir=Path("C:/fake/logs"))
    backend = build_role_backend(cfg, runtime)
    assert isinstance(backend, LocalRoutingBackend)
    # Descriptor identity matches the config; generator routes to the local gemma.
    assert backend.descriptor.identity_hash == cfg.identity_hash
    assert backend.descriptor.model_bindings["generator"] == "gemma-fake"
    runtime.close()


def test_remote_role_backend_is_the_coordinator_backend():
    cfg = _remote_cfg()
    runtime = cfg.build_runtime(log_dir=Path("C:/fake/logs"))
    backend = build_role_backend(cfg, runtime)
    assert isinstance(backend, OpenCodeServerBackend)
    assert backend is runtime.backend
    runtime.close()


def test_composite_role_backend_is_attached_to_coordinator():
    cfg = _composite_cfg()
    runtime = cfg.build_runtime(log_dir=Path("C:/fake/logs"))
    backend = build_role_backend(cfg, runtime)
    assert isinstance(backend, CompositeCompletionBackend)
    assert backend is runtime.backend
    assert runtime.backend_descriptor.identity_hash == cfg.identity_hash
    runtime.close()


def test_composite_coordinator_backend_raises_when_not_attached():
    local = LocalLifecycleCoordinator(_make_router(), descriptor=_make_descriptor("local_llama"))
    remote = RemoteRuntimeCoordinator(_FakeCompletionBackend("remote", {}))
    composite = CompositeRuntimeCoordinator(local, remote, _make_descriptor("composite"))
    with pytest.raises(ValueError, match="no composite CompletionBackend"):
        composite.backend


# ---------------------------------------------------------------------------
# build_role_adapters
# ---------------------------------------------------------------------------


def test_build_role_adapters_returns_five_backend_adapters_over_remote():
    cfg = _remote_cfg()
    runtime = cfg.build_runtime(log_dir=Path("C:/fake/logs"))
    caller, qwen, gemma, qwen_audit, gemma_audit = build_role_adapters(cfg, runtime)
    assert isinstance(caller, BackendModelCaller)
    assert isinstance(qwen, BackendQwenEvaluator)
    assert isinstance(gemma, BackendGemmaSelector)
    assert isinstance(qwen_audit, BackendQwenAuditEvaluator)
    assert isinstance(gemma_audit, BackendGemmaAuditEvaluator)
    for adapter in (caller, qwen, gemma, qwen_audit, gemma_audit):
        assert adapter.backend is runtime.backend
    runtime.close()


def test_build_repair_adapters_returns_backend_repair_caller_over_remote():
    # Phase 4 repair adapters (B2) are Backend adapters over the same
    # coordinator backend — never local lifecycle adapters. The second
    # element is the L2b narrow per-region re-gate (``region_fidelity_gate``),
    # not the full-chunk fidelity evaluator.
    from pact_v4.runtime.backend_role_adapters import (
        BackendGemmaAuditEvaluator,
        BackendQwenAuditEvaluator,
        BackendRegionFidelityGate,
        BackendRepairCaller,
    )

    cfg = _remote_cfg()
    runtime = cfg.build_runtime(log_dir=Path("C:/fake/logs"))
    repair_caller, region_gate, qwen_audit, gemma_audit = build_repair_adapters(cfg, runtime)
    assert isinstance(repair_caller, BackendRepairCaller)
    assert isinstance(region_gate, BackendRegionFidelityGate)
    assert isinstance(qwen_audit, BackendQwenAuditEvaluator)
    assert isinstance(gemma_audit, BackendGemmaAuditEvaluator)
    for adapter in (repair_caller, region_gate, qwen_audit, gemma_audit):
        assert adapter.backend is runtime.backend
    runtime.close()


def test_build_role_adapters_json_retry_policy_override():
    # B4 §5: the JSON-resilience retry policy is overridable through the
    # runtime-config build hook (default max_retries=2).
    from pact_v4.runtime.json_resilience import JsonRetryPolicy

    cfg = _remote_cfg()
    runtime = cfg.build_runtime(log_dir=Path("C:/fake/logs"))
    policy = JsonRetryPolicy(max_retries=5, base_delay_seconds=0.5)
    _caller, _qwen, _gemma, qwen_audit, _gemma_audit = build_role_adapters(
        cfg, runtime, json_retry_policy=policy,
    )
    assert qwen_audit._config.retry == policy
    runtime.close()


def test_build_repair_adapters_json_retry_policy_override():
    from pact_v4.runtime.json_resilience import JsonRetryPolicy

    cfg = _remote_cfg()
    runtime = cfg.build_runtime(log_dir=Path("C:/fake/logs"))
    policy = JsonRetryPolicy(max_retries=0, base_delay_seconds=0.0)
    repair_caller, _region_gate, qwen_audit, _gemma_audit = build_repair_adapters(
        cfg, runtime, json_retry_policy=policy,
    )
    assert repair_caller._config.retry == policy
    assert qwen_audit._config.retry == policy
    runtime.close()


def test_build_formatting_adapters_returns_backend_caller_over_remote():
    # Phase 5 formatting adapters (B3) are Backend adapters over the same
    # coordinator backend — the model-fallback tier never uses a local
    # lifecycle adapter (dual-mode rule).
    from pact_v4.runtime.backend_role_adapters import BackendFormattingCaller

    cfg = _remote_cfg()
    runtime = cfg.build_runtime(log_dir=Path("C:/fake/logs"))
    (formatting_caller,) = build_formatting_adapters(cfg, runtime)
    assert isinstance(formatting_caller, BackendFormattingCaller)
    assert formatting_caller.backend is runtime.backend
    runtime.close()


# ---------------------------------------------------------------------------
# CompositeCompletionBackend routing (offline, fake sub-backends)
# ---------------------------------------------------------------------------


class _FakeCompletionBackend:
    """Minimal scripted ``CompletionBackend`` for routing tests."""

    def __init__(self, name: str, bindings: Mapping[str, str]) -> None:
        self.name = name
        self.descriptor = BackendDescriptor(
            kind="test",
            transport_version="test/v1",
            endpoint_family="test",
            public_endpoint=f"http://{name}",
            model_bindings=dict(bindings),
            effective_options={},
        )
        self.seen: list[str] = []
        self._closed = False

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.seen.append(request.model_ref)
        return CompletionResponse(text="{}", model=request.model_ref)

    def close(self) -> None:
        self._closed = True

    def call_records(self) -> Sequence[BackendCallRecord]:
        return []


def test_composite_backend_routes_by_model_ref():
    remote = _FakeCompletionBackend("remote", {
        "generator": "opencode-go/deepseek-v4-flash",
        "fidelity_reviewer": "opencode-go/qwen3.7-plus",
    })
    local = _FakeCompletionBackend("local", {
        "russian_selector": "gemma-fake",
        "gemma_audit": "gemma-fake",
    })
    composite = CompositeCompletionBackend(
        {"remote": remote, "local": local},
        BackendDescriptor(
            kind="composite",
            transport_version="composite/v1",
            endpoint_family="pact_composite",
            public_endpoint="",
            model_bindings={
                "generator": "opencode-go/deepseek-v4-flash",
                "fidelity_reviewer": "opencode-go/qwen3.7-plus",
                "russian_selector": "gemma-fake",
            },
            effective_options={},
        ),
    )
    composite.complete(_req("opencode-go/deepseek-v4-flash"))
    composite.complete(_req("opencode-go/qwen3.7-plus"))
    composite.complete(_req("gemma-fake"))
    assert remote.seen == ["opencode-go/deepseek-v4-flash", "opencode-go/qwen3.7-plus"]
    assert local.seen == ["gemma-fake"]

    with pytest.raises(CompletionError, match="no backend serves model_ref"):
        composite.complete(_req("somewhere/else"))
    composite.close()
    assert remote._closed and local._closed


def _req(model_ref: str) -> CompletionRequest:
    return CompletionRequest(
        model_ref=model_ref,
        messages=(Message(role="user", content="test"),),
        max_output_tokens=64,
        temperature=0.0,
        response_schema=None,
        label="test",
    )


# ---------------------------------------------------------------------------
# BackendModelCaller generator-alias resolution (plan §8 config shape)
# ---------------------------------------------------------------------------


def _bundle() -> PromptBundle:
    def _hash(seed: str) -> str:
        from pact_v4.phase1.models import canonical_json_hash

        return canonical_json_hash({"seed": seed})

    return PromptBundle(
        template=FIDELITY_FIRST_V1,
        role="fidelity_first",
        risk_band="low",
        risk_policy_version="pact-v4-risk-source-en/v1",
        required_risk_feature_codes=(),
        snapshot_hash=_hash("snap"),
        source_hash=_hash("source"),
        chunk_id="chunk0001",
        owned_pids=("p00001",),
        owned_source=(("p00001", "First English sentence."),),
        left_context=(),
        right_context=(),
        glossary=(),
        style_constraints=(),
        bible_text="",
        config_identity=_hash("config"),
        params=GenerationParams(temperature=0.2, seed=7, max_tokens=512),
    )


def test_model_caller_resolves_generator_alias_binding():
    # The plan §8 config binds the generation model under ``generator``, not
    # the bundle role ("fidelity_first"). C3: the caller must resolve the
    # alias instead of failing loudly.
    backend = _FakeCompletionBackend("remote", {
        "generator": "opencode-go/deepseek-v4-flash",
    })
    caller = BackendModelCaller(backend)
    out = caller(_bundle())
    assert out == "{}"
    assert backend.seen == ["opencode-go/deepseek-v4-flash"]


def _make_descriptor(kind: str) -> BackendDescriptor:
    return BackendDescriptor(
        kind=kind,
        transport_version="test/v1",
        endpoint_family="test",
        public_endpoint="http://127.0.0.1:9",
        model_bindings={"generator": "gemma"},
        effective_options={},
    )
