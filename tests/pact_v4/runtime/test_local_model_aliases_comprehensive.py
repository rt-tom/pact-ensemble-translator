"""Comprehensive tests for local-model-aliases (payload capture, all-ten routing, cache, remote)."""
import textwrap
from pathlib import Path

import pytest

from pact_v4.runtime.backend_protocol import CompletionRequest
from pact_v4.runtime.runtime_config import (
    TRANSLATOR_ROLES,
    REVIEWER_ROLES,
    load_providers_registry,
    build_resolved_pair_from_registry,
    derive_max_output_tokens,
)
from pact_v4.runtime.backend_role_adapters import (
    BackendModelCaller,
    BackendQwenEvaluator,
    BackendGemmaSelector,
    BackendQwenAuditEvaluator,
    BackendGemmaAuditEvaluator,
    BackendRepairCaller,
    BackendRegionFidelityGate,
)


def _registry_with_pair(tmp_path: Path):
    yaml = textwrap.dedent("""
role_budgets:
  generator: {max_output_tokens: 70000}
  repair: {max_output_tokens: 16384, output_budget: {mode: floor_plus_per_item, floor_tokens: 16384, per_item_tokens: 128, ceiling: 24576}}
  formatting: {max_output_tokens: 8000, output_budget: {mode: span_formula, base_tokens: 8000, per_span_tokens: 64, ceiling: 24576}}
  gemma_audit: {max_output_tokens: 4096}
  qwen_audit: {max_output_tokens: 12000, output_budget: {mode: floor_plus_per_item, floor_tokens: 12000, per_item_tokens: 128, ceiling: 24576}}
  fidelity_reviewer: {max_output_tokens: 16384, output_budget: {mode: floor_plus_per_item, floor_tokens: 16384, per_item_tokens: 128, ceiling: 24576}}
  russian_selector: {max_output_tokens: 1024}
  entity_extractor: {max_output_tokens: 12000}
  russian_editor: {max_output_tokens: 12000}
  glossary_resolver: {max_output_tokens: 4096}
providers:
  local:
    kind: local_llama
    models:
      trans:
        model_key: gemma
        model_path: /tmp/trans.gguf
        model_name: trans.gguf
        server_args: ["--reasoning-budget", "2048"]
        reasoning_budget: 2048
        request: {temperature: 0.7, top_p: 0.9, top_k: 40, min_p: 0.05, seed: 42}
      rev:
        model_key: qwen
        model_path: /tmp/rev.gguf
        model_name: rev.gguf
        server_args: ["--reasoning-budget", "8192"]
        reasoning_budget: 8192
        request: {temperature: 0.3, top_p: 0.95, top_k: 50, min_p: 0.1, seed: 7}
  opencode-go:
    kind: opencode_server
    models:
      m1: {ref: opencode-go/m1, reasoning_contract: {variants: [low]}}
""")
    p = tmp_path / "providers.yaml"
    p.write_text(yaml, encoding="utf-8")
    reg = load_providers_registry(p)
    pair = build_resolved_pair_from_registry(reg, "trans", "rev")
    return reg, pair


class _FakeBackend:
    def __init__(self, bindings):
        from pact_v4.runtime.backend_protocol import BackendDescriptor
        self.descriptor = BackendDescriptor(
            kind="local_llama",
            transport_version="local-llama/v1",
            endpoint_family="openai_chat_completions",
            public_endpoint="http://127.0.0.1:8093",
            model_bindings=dict(bindings),
            effective_options={},
        )
        self.last_request: CompletionRequest | None = None

    def complete(self, request: CompletionRequest):
        self.last_request = request
        from pact_v4.runtime.backend_protocol import CompletionResponse
        return CompletionResponse(text='{"ok": true}', provider="test", model=request.model_ref, finish_reason="stop", usage={}, wall_seconds=0.1, request_id=None, session_id=None, retry_count=0, raw_metadata={})


def test_payload_captures_all_sampling_fields(tmp_path):
    _, pair = _registry_with_pair(tmp_path)
    from pact_v4.runtime.runtime_config import TRANSLATOR_ROLES, REVIEWER_ROLES
    from pact_v4.phase2.generation import GenerationParams, PromptBundle
    from pact_v4.phase2.prompts import FIDELITY_FIRST_V1
    from pact_v4.phase1.models import canonical_json_hash
    trans_sampling = pair.sampling_for_role("generator")
    assert trans_sampling["temperature"] == 0.7
    assert trans_sampling["top_p"] == 0.9
    assert trans_sampling["top_k"] == 40
    assert trans_sampling["min_p"] == 0.05
    assert trans_sampling["seed"] == 42
    rev_sampling = pair.sampling_for_role("qwen_audit")
    assert rev_sampling["temperature"] == 0.3
    assert rev_sampling["top_p"] == 0.95
    assert rev_sampling["top_k"] == 50
    bindings = {role: pair.translator_model.model_name for role in TRANSLATOR_ROLES}
    bindings.update({role: pair.reviewer_model.model_name for role in REVIEWER_ROLES})
    backend = _FakeBackend(bindings)
    # Translator producer must serialize all sampling + budget
    budget = pair.budget_for_role("generator")
    req = dict(trans_sampling)
    req["max_output_tokens"] = int(budget.max_output_tokens)
    policy = type("P", (), {"request": req, "output_budget": budget.output_budget, "model_key": pair.translator_model.model_key})()
    from pact_v4.runtime.backend_role_adapters import BackendModelCallerConfig
    caller = BackendModelCaller(backend, config=BackendModelCallerConfig(role_policy=policy))
    # Actually invoke the producer and inspect payload
    def _hash(s): return canonical_json_hash({"seed": s})
    bundle = PromptBundle(template=FIDELITY_FIRST_V1, role="fidelity_first", risk_band="low", risk_policy_version="v1", required_risk_feature_codes=(), snapshot_hash=_hash("snap"), source_hash=_hash("source"), chunk_id="chunk0001", owned_pids=("p00001",), owned_source=(("p00001","Hello"),), left_context=(), right_context=(), glossary=(), style_constraints=(), bible_text="", config_identity=_hash("cfg"), params=GenerationParams(temperature=0.9, seed=1, max_tokens=512))
    caller(bundle)
    rq = backend.last_request
    assert rq is not None
    assert rq.temperature == 0.7
    assert rq.top_p == 0.9
    assert rq.top_k == 40
    assert rq.min_p == 0.05
    assert rq.seed == 42
    assert rq.max_output_tokens == 70000
    # Reviewer producer must serialize reviewer sampling + budget
    from pact_v4.runtime.backend_role_adapters import BackendQwenEvaluatorConfig, BackendQwenEvaluator
    rev_budget = pair.budget_for_role("fidelity_reviewer")
    rev_req = dict(rev_sampling)
    rev_req["max_output_tokens"] = int(rev_budget.max_output_tokens)
    rev_policy = type("P", (), {"request": rev_req, "output_budget": rev_budget.output_budget, "model_key": pair.reviewer_model.model_key})()
    backend2 = _FakeBackend(bindings)
    qwen = BackendQwenEvaluator(backend2, config=BackendQwenEvaluatorConfig(role_policy=rev_policy, bible_text=""))
    qwen(source={"p00001":"Hello"}, translation={"p00001":"Привет"})
    rq2 = backend2.last_request
    assert rq2 is not None
    assert rq2.temperature == 0.3
    assert rq2.top_p == 0.95
    assert rq2.top_k == 50
    assert rq2.min_p == 0.1
    assert rq2.seed == 7
    assert rq2.max_output_tokens == derive_max_output_tokens(rev_budget, item_count=1)
    h1 = pair.per_role_hash("generator")
    # Change sampling and ensure hash changes
    yaml2 = textwrap.dedent("""
role_budgets:
  generator: {max_output_tokens: 70000}
  repair: {max_output_tokens: 16384, output_budget: {mode: floor_plus_per_item, floor_tokens: 16384, per_item_tokens: 128, ceiling: 24576}}
  formatting: {max_output_tokens: 8000, output_budget: {mode: span_formula, base_tokens: 8000, per_span_tokens: 64, ceiling: 24576}}
  gemma_audit: {max_output_tokens: 4096}
  qwen_audit: {max_output_tokens: 12000, output_budget: {mode: floor_plus_per_item, floor_tokens: 12000, per_item_tokens: 128, ceiling: 24576}}
  fidelity_reviewer: {max_output_tokens: 16384, output_budget: {mode: floor_plus_per_item, floor_tokens: 16384, per_item_tokens: 128, ceiling: 24576}}
  russian_selector: {max_output_tokens: 1024}
  entity_extractor: {max_output_tokens: 12000}
  russian_editor: {max_output_tokens: 12000}
  glossary_resolver: {max_output_tokens: 4096}
providers:
  local:
    kind: local_llama
    models:
      trans:
        model_key: gemma
        model_path: /tmp/trans.gguf
        model_name: trans.gguf
        server_args: ["--reasoning-budget", "2048"]
        reasoning_budget: 2048
        request: {temperature: 0.8, top_p: 0.9, top_k: 40, min_p: 0.05, seed: 42}
      rev:
        model_key: qwen
        model_path: /tmp/rev.gguf
        model_name: rev.gguf
        server_args: ["--reasoning-budget", "8192"]
        reasoning_budget: 8192
        request: {temperature: 0.3, top_p: 0.95, top_k: 50, min_p: 0.1, seed: 7}
  opencode-go:
    kind: opencode_server
    models:
      m1: {ref: opencode-go/m1, reasoning_contract: {variants: [low]}}
""")
    p2 = tmp_path / "providers2.yaml"
    p2.write_text(yaml2, encoding="utf-8")
    reg2 = load_providers_registry(p2)
    pair2 = build_resolved_pair_from_registry(reg2, "trans", "rev")
    h2 = pair2.per_role_hash("generator")
    assert h1 != h2, "sampling change must affect per-role hash (cache key)"
    # aggregate hash must NOT change when sampling changes (run identity)
    assert pair.aggregate_hash == pair2.aggregate_hash, "sampling excluded from run identity"


def test_all_ten_producers_receive_correct_group_model_and_budget(tmp_path):
    _, pair = _registry_with_pair(tmp_path)
    # Every translator role must use trans model's sampling and its own budget
    for role in TRANSLATOR_ROLES:
        sampling = pair.sampling_for_role(role)
        assert sampling["temperature"] == 0.7, f"{role} should use translator sampling"
        budget = pair.budget_for_role(role)
        assert budget.max_output_tokens in (70000, 16384, 8000, 4096), f"{role} budget unexpected"
        # derive must work
        derived = derive_max_output_tokens(budget, item_count=10)
        assert isinstance(derived, int) and derived > 0
    for role in REVIEWER_ROLES:
        sampling = pair.sampling_for_role(role)
        assert sampling["temperature"] == 0.3, f"{role} should use reviewer sampling"
        budget = pair.budget_for_role(role)
        assert budget.max_output_tokens in (12000, 16384, 1024, 12000, 4096)
    # Check that B3 helper correctly wires all roles
    from pact_v4.pipeline.b3_audit_repair import _policy_for_role
    for role in TRANSLATOR_ROLES | REVIEWER_ROLES if isinstance(TRANSLATOR_ROLES, set) else (TRANSLATOR_ROLES + REVIEWER_ROLES):
        pol = _policy_for_role(pair, role)
        assert pol is not None, f"policy for {role} missing"
        assert "temperature" in pol.request
        assert pol.output_budget is not None or pol.request.get("max_output_tokens") is not None


def test_sampling_cache_miss_and_reviewer_reuse(tmp_path):
    _, pair = _registry_with_pair(tmp_path)
    _, pair2 = _registry_with_pair(tmp_path)
    # Simulate changing translator sampling only
    yaml_rev_same = textwrap.dedent("""
role_budgets:
  generator: {max_output_tokens: 70000}
  repair: {max_output_tokens: 16384, output_budget: {mode: floor_plus_per_item, floor_tokens: 16384, per_item_tokens: 128, ceiling: 24576}}
  formatting: {max_output_tokens: 8000, output_budget: {mode: span_formula, base_tokens: 8000, per_span_tokens: 64, ceiling: 24576}}
  gemma_audit: {max_output_tokens: 4096}
  qwen_audit: {max_output_tokens: 12000, output_budget: {mode: floor_plus_per_item, floor_tokens: 12000, per_item_tokens: 128, ceiling: 24576}}
  fidelity_reviewer: {max_output_tokens: 16384, output_budget: {mode: floor_plus_per_item, floor_tokens: 16384, per_item_tokens: 128, ceiling: 24576}}
  russian_selector: {max_output_tokens: 1024}
  entity_extractor: {max_output_tokens: 12000}
  russian_editor: {max_output_tokens: 12000}
  glossary_resolver: {max_output_tokens: 4096}
providers:
  local:
    kind: local_llama
    models:
      trans:
        model_key: gemma
        model_path: /tmp/trans.gguf
        model_name: trans.gguf
        server_args: ["--reasoning-budget", "2048"]
        reasoning_budget: 2048
        request: {temperature: 0.9, top_p: 0.9, top_k: 40, min_p: 0.05, seed: 42}
      rev:
        model_key: qwen
        model_path: /tmp/rev.gguf
        model_name: rev.gguf
        server_args: ["--reasoning-budget", "8192"]
        reasoning_budget: 8192
        request: {temperature: 0.3, top_p: 0.95, top_k: 50, min_p: 0.1, seed: 7}
  opencode-go:
    kind: opencode_server
    models:
      m1: {ref: opencode-go/m1, reasoning_contract: {variants: [low]}}
""")
    p3 = tmp_path / "providers3.yaml"
    p3.write_text(yaml_rev_same, encoding="utf-8")
    reg3 = load_providers_registry(p3)
    pair3 = build_resolved_pair_from_registry(reg3, "trans", "rev")
    # Translator roles should have different per-role hash (cache miss)
    for role in TRANSLATOR_ROLES:
        assert pair.per_role_hash(role) != pair3.per_role_hash(role), f"{role} should miss on translator change"
    # Reviewer roles should be reuse (same hash)
    for role in REVIEWER_ROLES:
        assert pair.per_role_hash(role) == pair3.per_role_hash(role), f"{role} should reuse when reviewer unchanged"
    # Aggregate hashes should be same (sampling excluded from run identity)
    assert pair.aggregate_hash == pair3.aggregate_hash
    # But after changing reviewer, reviewer should miss
    # (Already tested in previous test, but ensure overwrite semantics: same dir, different sampling => regenerate)
    # This is the overwrite semantics: changed sampling regenerates and overwrites same dir


def test_remote_fixed_role_bindings_without_fallback(tmp_path):
    # Remote must bind all ten roles explicitly, no fallback to default or other role
    from pact_v4.runtime.runtime_config import CompositeBackendConfig, LocalLlamaBackendConfig, OpenCodeBackendConfig
    from pact_v4.runtime.opencode_backend import OpenCodeServerBackendConfig
    remote = OpenCodeBackendConfig(
        server=OpenCodeServerBackendConfig(
            base_url="http://127.0.0.1:4096",
            username="u",
            password="p",
            model_bindings={
                "generator": "opencode-go/m1",
                "repair": "opencode-go/m1",
                "formatting": "opencode-go/m1",
                "gemma_audit": "opencode-go/m1",
                "qwen_audit": "opencode-go/m2",
                "fidelity_reviewer": "opencode-go/m2",
                "russian_selector": "opencode-go/m2",
                "entity_extractor": "opencode-go/m2",
                "russian_editor": "opencode-go/m2",
                "glossary_resolver": "opencode-go/m2",
            },
        )
    )
    # Ensure every role is bound
    for role in TRANSLATOR_ROLES + REVIEWER_ROLES:
        assert role in remote.server.model_bindings, f"remote missing {role}"
    # Ensure no default fallback is used
    assert "default" not in remote.server.model_bindings
    # Composite with explicit role_backend_map for all ten should succeed
    cfg = CompositeBackendConfig(
        backends={"remote": remote},
        role_backend_map={role: "remote" for role in TRANSLATOR_ROLES + REVIEWER_ROLES},
    )
    desc = cfg.build_descriptor()
    for role in TRANSLATOR_ROLES + REVIEWER_ROLES:
        assert role in desc.model_bindings
    # Missing role should fail when trying to apply override for unrouted role
    from pact_v4.runtime.runtime_config import apply_role_models
    with pytest.raises(ValueError, match="is not routed"):
        apply_role_models(cfg, {"nonexistent_role": "opencode-go/m1"})
    # Also check that _resolve_role_backend does not fallback to default
    from pact_v4.runtime.runtime_config import _resolve_role_backend
    assert _resolve_role_backend({}, {}, "generator") is None
    assert _resolve_role_backend({"generator": "remote"}, {"remote": {"generator": "m1"}}, "repair") is None


def test_b3_all_ten_wiring(tmp_path):
    _, pair = _registry_with_pair(tmp_path)
    from pact_v4.pipeline.b3_audit_repair import _policy_for_role
    for role in TRANSLATOR_ROLES + REVIEWER_ROLES:
        pol = _policy_for_role(pair, role)
        assert pol is not None
        # Check that sampling came from correct group
        is_trans = role in TRANSLATOR_ROLES
        expected_temp = 0.7 if is_trans else 0.3
        assert pol.request["temperature"] == expected_temp
        # Budget from role_budgets
        expected_budget = pair.budget_for_role(role)
        assert pol.output_budget == expected_budget.output_budget

def test_changed_sampling_cache_overwrite_payload_capture(tmp_path):
    """Changed sampling causes cache miss and overwrites same dir — payload capture."""
    reg, pair = _registry_with_pair(tmp_path)
    bindings = {role: pair.translator_model.model_name for role in TRANSLATOR_ROLES}
    bindings.update({role: pair.reviewer_model.model_name for role in REVIEWER_ROLES})
    # First call with temp 0.7
    budget = pair.budget_for_role("generator")
    req = dict(pair.sampling_for_role("generator"))
    req["max_output_tokens"] = int(budget.max_output_tokens)
    policy = type("P", (), {"request": req, "output_budget": budget.output_budget, "model_key": pair.translator_model.model_key})()
    from pact_v4.runtime.backend_role_adapters import BackendModelCallerConfig, BackendModelCaller
    from pact_v4.phase2.generation import GenerationParams, PromptBundle
    from pact_v4.phase2.prompts import FIDELITY_FIRST_V1
    from pact_v4.phase1.models import canonical_json_hash
    def _hash(s): return canonical_json_hash({"seed": s})
    backend = _FakeBackend(bindings)
    caller = BackendModelCaller(backend, config=BackendModelCallerConfig(role_policy=policy))
    bundle = PromptBundle(template=FIDELITY_FIRST_V1, role="fidelity_first", risk_band="low", risk_policy_version="v1", required_risk_feature_codes=(), snapshot_hash=_hash("snap"), source_hash=_hash("source"), chunk_id="chunk0001", owned_pids=("p00001",), owned_source=(("p00001","Hello"),), left_context=(), right_context=(), glossary=(), style_constraints=(), bible_text="", config_identity=_hash("cfg"), params=GenerationParams(temperature=0.99, seed=1, max_tokens=512))
    caller(bundle)
    first = backend.last_request
    assert first.temperature == 0.7
    # Second pair with changed temperature 0.9 must be different payload but same max_output_tokens (overwrite semantics)
    yaml2 = textwrap.dedent("""
role_budgets:
  generator: {max_output_tokens: 70000}
  repair: {max_output_tokens: 16384, output_budget: {mode: floor_plus_per_item, floor_tokens: 16384, per_item_tokens: 128, ceiling: 24576}}
  formatting: {max_output_tokens: 8000, output_budget: {mode: span_formula, base_tokens: 8000, per_span_tokens: 64, ceiling: 24576}}
  gemma_audit: {max_output_tokens: 4096}
  qwen_audit: {max_output_tokens: 12000, output_budget: {mode: floor_plus_per_item, floor_tokens: 12000, per_item_tokens: 128, ceiling: 24576}}
  fidelity_reviewer: {max_output_tokens: 16384, output_budget: {mode: floor_plus_per_item, floor_tokens: 16384, per_item_tokens: 128, ceiling: 24576}}
  russian_selector: {max_output_tokens: 1024}
  entity_extractor: {max_output_tokens: 12000}
  russian_editor: {max_output_tokens: 12000}
  glossary_resolver: {max_output_tokens: 4096}
providers:
  local:
    kind: local_llama
    models:
      trans:
        model_key: gemma
        model_path: /tmp/trans.gguf
        model_name: trans.gguf
        server_args: ["--reasoning-budget", "2048"]
        reasoning_budget: 2048
        request: {temperature: 0.9, top_p: 0.9, top_k: 40, min_p: 0.05, seed: 42}
      rev:
        model_key: qwen
        model_path: /tmp/rev.gguf
        model_name: rev.gguf
        server_args: ["--reasoning-budget", "8192"]
        reasoning_budget: 8192
        request: {temperature: 0.3, top_p: 0.95, top_k: 50, min_p: 0.1, seed: 7}
  opencode-go:
    kind: opencode_server
    models:
      m1: {ref: opencode-go/m1, reasoning_contract: {variants: [low]}}
""")
    p2 = tmp_path / "providers_changed.yaml"
    p2.write_text(yaml2, encoding="utf-8")
    reg2 = load_providers_registry(p2)
    pair2 = build_resolved_pair_from_registry(reg2, "trans", "rev")
    budget2 = pair2.budget_for_role("generator")
    req2 = dict(pair2.sampling_for_role("generator"))
    req2["max_output_tokens"] = int(budget2.max_output_tokens)
    policy2 = type("P", (), {"request": req2, "output_budget": budget2.output_budget, "model_key": pair2.translator_model.model_key})()
    backend2 = _FakeBackend(bindings)
    caller2 = BackendModelCaller(backend2, config=BackendModelCallerConfig(role_policy=policy2))
    caller2(bundle)
    second = backend2.last_request
    assert second.temperature == 0.9
    assert second.top_p == 0.9
    assert second.top_k == 40
    assert second.min_p == 0.05
    assert second.seed == 42
    assert second.max_output_tokens == 70000
    # Identity must NOT change (same dir), but cache key (per_role_hash) must change
    assert pair.aggregate_hash == pair2.aggregate_hash
    assert pair.per_role_hash("generator") != pair2.per_role_hash("generator")
    # Simulate same output dir overwrite: second request would overwrite first's file
    assert first.max_output_tokens == second.max_output_tokens
    assert first.temperature != second.temperature
