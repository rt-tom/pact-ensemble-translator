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
    _model_ref_for,
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
    # B4 §5 / B10: the JSON-resilience retry policy is overridable through the
    # runtime-config build hook (default max_retries=2) and is propagated to
    # EVERY role adapter — generation, Qwen fidelity, Gemma selector, Qwen
    # audit, Gemma audit (previously only the Qwen audit adapter got it).
    from pact_v4.runtime.json_resilience import JsonRetryPolicy

    cfg = _remote_cfg()
    runtime = cfg.build_runtime(log_dir=Path("C:/fake/logs"))
    policy = JsonRetryPolicy(max_retries=5, base_delay_seconds=0.5)
    model_caller, qwen, gemma, qwen_audit, gemma_audit = build_role_adapters(
        cfg, runtime, json_retry_policy=policy,
    )
    for adapter in (model_caller, qwen, gemma, qwen_audit, gemma_audit):
        assert adapter._config.retry == policy
    runtime.close()


def test_build_repair_adapters_json_retry_policy_override():
    from pact_v4.runtime.json_resilience import JsonRetryPolicy

    cfg = _remote_cfg()
    runtime = cfg.build_runtime(log_dir=Path("C:/fake/logs"))
    policy = JsonRetryPolicy(max_retries=0, base_delay_seconds=0.0)
    repair_caller, region_gate, qwen_audit, gemma_audit = build_repair_adapters(
        cfg, runtime, json_retry_policy=policy,
    )
    for adapter in (repair_caller, region_gate, qwen_audit, gemma_audit):
        assert adapter._config.retry == policy
    runtime.close()


def test_build_role_adapters_retry_defaults_to_json_retry_policy():
    # B10: without an explicit override every role adapter still gets a
    # JsonRetryPolicy (the dataclass default) — the hooks must not pass None.
    from pact_v4.runtime.json_resilience import JsonRetryPolicy

    cfg = _remote_cfg()
    runtime = cfg.build_runtime(log_dir=Path("C:/fake/logs"))
    model_caller, qwen, gemma, qwen_audit, gemma_audit = build_role_adapters(cfg, runtime)
    for adapter in (model_caller, qwen, gemma, qwen_audit, gemma_audit):
        assert isinstance(adapter._config.retry, JsonRetryPolicy)
        assert adapter._config.retry.max_retries == 2
    runtime.close()


def test_build_repair_adapters_retry_defaults_to_json_retry_policy():
    from pact_v4.runtime.json_resilience import JsonRetryPolicy

    cfg = _remote_cfg()
    runtime = cfg.build_runtime(log_dir=Path("C:/fake/logs"))
    repair_caller, region_gate, qwen_audit, gemma_audit = build_repair_adapters(cfg, runtime)
    for adapter in (repair_caller, region_gate, qwen_audit, gemma_audit):
        assert isinstance(adapter._config.retry, JsonRetryPolicy)
        assert adapter._config.retry.max_retries == 2
    runtime.close()


def test_no_formatting_adapters_builder_exists():
    # Card C removed the Phase 5 formatting adapters entirely: formatting is
    # model-free, so there is no formatting caller to build.
    import pact_v4.runtime.runtime_config as rc
    assert not hasattr(rc, "build_formatting_adapters")
    from pact_v4.runtime import backend_role_adapters as bra
    assert not hasattr(bra, "BackendFormattingCaller")


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


def _composite_descriptor(
    routing: Mapping[str, str], bindings: Mapping[str, str]
) -> BackendDescriptor:
    """A composite descriptor carrying the role map in effective_options.

    Mirrors ``CompositeBackendConfig.build_descriptor`` (the routing map is
    part of ``effective_options``); ``CompositeCompletionBackend`` reads it
    to route each model_ref to the backend that actually serves the role.
    """
    return BackendDescriptor(
        kind="composite",
        transport_version="composite/v1",
        endpoint_family="pact_composite",
        public_endpoint="",
        model_bindings=dict(bindings),
        effective_options={"routing": dict(routing)},
    )


def test_composite_backend_duplicate_role_routes_to_mapped_backend():
    # RV2 t_7b26974e HIGH: generator is declared by BOTH sub-backends with
    # DIFFERENT refs and the role map routes it to ``b``. The role adapters
    # must resolve the MAPPED backend's ref (via the descriptor) and the
    # request must reach that backend — not the first declarer.
    a = _FakeCompletionBackend("a", {"generator": "m-a-gen", "qwen_audit": "m-a-qa"})
    b = _FakeCompletionBackend(
        "b", {"generator": "m-b-gen", "fidelity_reviewer": "m-b-fid"}
    )
    routing = {"generator": "b", "qwen_audit": "a", "fidelity_reviewer": "b"}
    composite = CompositeCompletionBackend(
        {"a": a, "b": b},
        _composite_descriptor(routing, {
            "generator": "m-b-gen",
            "qwen_audit": "m-a-qa",
            "fidelity_reviewer": "m-b-fid",
        }),
    )
    # _model_ref_for (what the role adapters consume) resolves generator to
    # the mapped backend's ref.
    ref = _model_ref_for(composite, ("generator", "fidelity_first"))
    assert ref == "m-b-gen"
    composite.complete(_req(ref))
    assert b.seen == ["m-b-gen"]
    assert a.seen == []
    # The other sub-backend's generator ref stays routable to it directly.
    composite.complete(_req("m-a-gen"))
    assert a.seen == ["m-a-gen"]
    # serving_backend (reasoning-transport decision) follows the same map.
    assert composite.serving_backend("m-b-gen") is b
    assert composite.serving_backend("m-a-gen") is a


def test_composite_backend_collision_routes_to_mapped_backend():
    # Model-ref collision: the SAME ref is bound to the SAME role by both
    # sub-backends. The role map — not last-wins — decides which backend
    # serves it, so a CompletionRequest carrying the ref reaches the
    # mapped backend.
    a = _FakeCompletionBackend("a", {"generator": "m-shared"})
    b = _FakeCompletionBackend("b", {"generator": "m-shared", "qwen_audit": "m-qa"})
    routing = {"generator": "a", "qwen_audit": "b"}
    composite = CompositeCompletionBackend(
        {"a": a, "b": b},
        _composite_descriptor(routing, {
            "generator": "m-shared", "qwen_audit": "m-qa",
        }),
    )
    composite.complete(_req("m-shared"))
    composite.complete(_req("m-qa"))
    assert a.seen == ["m-shared"]
    assert b.seen == ["m-qa"]
    assert composite.serving_backend("m-shared") is a
    assert composite.serving_backend("m-qa") is b


def test_composite_backend_fallback_role_reaches_generator_backend():
    # Documented fallbacks (repair -> generator, entity_extractor ->
    # qwen_audit): a role declared by NO sub-backend resolves its model_ref
    # through the fallback binding, and the request reaches the SAME
    # concrete backend that serves the fallback role.
    a = _FakeCompletionBackend("a", {"generator": "m-gen", "qwen_audit": "m-qa"})
    b = _FakeCompletionBackend("b", {"fidelity_reviewer": "m-fid"})
    routing = {"generator": "a", "qwen_audit": "a", "fidelity_reviewer": "b"}
    composite = CompositeCompletionBackend(
        {"a": a, "b": b},
        _composite_descriptor(routing, {
            "generator": "m-gen", "qwen_audit": "m-qa", "fidelity_reviewer": "m-fid",
        }),
    )
    # Descriptor carries no synthetic repair binding.
    assert "repair" not in composite.descriptor.model_bindings
    repair_ref = _model_ref_for(composite, ("repair", "generator"))
    assert repair_ref == "m-gen"
    composite.complete(_req(repair_ref))
    assert a.seen == ["m-gen"]
    assert b.seen == []
    # entity_extractor resolves via the qwen_audit binding on the same
    # audit backend ("a" here) — B3 _EntityRoleView does the same.
    entity_ref = _model_ref_for(composite, ("entity_extractor", "qwen_audit"))
    assert entity_ref == "m-qa"
    composite.complete(_req(entity_ref))
    assert a.seen == ["m-gen", "m-qa"]


def test_composite_backend_non_ambiguous_routing_unchanged_with_map():
    # Preserve existing model-ref routing: with every role declared by
    # exactly one sub-backend, adding the routing map changes nothing —
    # each ref still reaches the only backend that declares it.
    remote = _FakeCompletionBackend("remote", {
        "generator": "opencode-go/deepseek-v4-flash",
        "fidelity_reviewer": "opencode-go/qwen3.7-plus",
    })
    local = _FakeCompletionBackend("local", {
        "russian_selector": "gemma-fake",
        "gemma_audit": "gemma-fake",
    })
    routing = {
        "generator": "remote",
        "fidelity_reviewer": "remote",
        "russian_selector": "local",
        "gemma_audit": "local",
    }
    composite = CompositeCompletionBackend(
        {"remote": remote, "local": local},
        _composite_descriptor(routing, {
            "generator": "opencode-go/deepseek-v4-flash",
            "fidelity_reviewer": "opencode-go/qwen3.7-plus",
            "russian_selector": "gemma-fake",
            "gemma_audit": "gemma-fake",
        }),
    )
    composite.complete(_req("opencode-go/deepseek-v4-flash"))
    composite.complete(_req("opencode-go/qwen3.7-plus"))
    composite.complete(_req("gemma-fake"))
    assert remote.seen == [
        "opencode-go/deepseek-v4-flash", "opencode-go/qwen3.7-plus",
    ]
    assert local.seen == ["gemma-fake"]
    with pytest.raises(CompletionError, match="no backend serves model_ref"):
        composite.complete(_req("somewhere/else"))


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
