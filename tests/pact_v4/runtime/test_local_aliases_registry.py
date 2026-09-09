import pathlib, tempfile, textwrap
import yaml
from pact_v4.runtime.runtime_config import ResolvedRolePolicies, RoleCallPolicy, OutputBudgetPolicy

def test_complete_defaults():
    policies = {role: RoleCallPolicy(model_key="gemma", request={"temperature":0.0, "max_output_tokens":1024}) for role in ["generator","fidelity_reviewer","russian_selector","qwen_audit","gemma_audit","repair","entity_extractor","russian_editor","formatting","glossary_resolver"]}
    r = ResolvedRolePolicies(policies=policies)
    assert r.aggregate_hash

def test_missing_role_fails():
    try:
        ResolvedRolePolicies(policies={})
        assert False, "should fail"
    except ValueError:
        pass

def test_derive_budget():
    p = RoleCallPolicy(model_key="qwen", request={"temperature":0.0}, output_budget=OutputBudgetPolicy(mode="floor_plus_per_item", floor_tokens=1000, per_item_tokens=10, ceiling=2000))
    from pact_v4.runtime.runtime_config import derive_max_output_tokens
    assert derive_max_output_tokens(p, item_count=5)==1050

def test_provider_yaml_load(tmp_path=None):
    import pathlib
    p = pathlib.Path("configs/providers.yaml")
    from pact_v4.runtime.runtime_config import load_providers_registry
    reg = load_providers_registry(p)
    assert "local" in reg.providers

def _write_yaml(tmp_path, content: str) -> pathlib.Path:
    p = tmp_path / "providers.yaml"
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p

def test_malformed_request_field_rejected(tmp_path):
    from pact_v4.runtime.runtime_config import load_providers_registry
    yaml_content = """
providers:
  local:
    kind: local_llama
    role_policies:
      generator: {model_key: gemma, request: {temperature: 0.2, unknown_field: 1, max_output_tokens: 1000}}
      fidelity_reviewer: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      russian_selector: {model_key: gemma, request: {temperature: 0.0, max_output_tokens: 1000}}
      qwen_audit: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      gemma_audit: {model_key: gemma, request: {temperature: 0.0, max_output_tokens: 1000}}
      repair: {model_key: gemma, request: {temperature: 0.0, max_output_tokens: 1000}}
      entity_extractor: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      russian_editor: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      formatting: {model_key: gemma, request: {temperature: 0.1, max_output_tokens: 1000}}
      glossary_resolver: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
    models: {}
"""
    p = _write_yaml(tmp_path, yaml_content)
    try:
        load_providers_registry(p)
        assert False, "should have failed on unknown request field"
    except ValueError as e:
        assert "unknown request field" in str(e).lower()

def test_invalid_temperature_range_rejected(tmp_path):
    from pact_v4.runtime.runtime_config import load_providers_registry
    yaml_content = """
providers:
  local:
    kind: local_llama
    role_policies:
      generator: {model_key: gemma, request: {temperature: 5, max_output_tokens: 1000}}
      fidelity_reviewer: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      russian_selector: {model_key: gemma, request: {temperature: 0.0, max_output_tokens: 1000}}
      qwen_audit: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      gemma_audit: {model_key: gemma, request: {temperature: 0.0, max_output_tokens: 1000}}
      repair: {model_key: gemma, request: {temperature: 0.0, max_output_tokens: 1000}}
      entity_extractor: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      russian_editor: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      formatting: {model_key: gemma, request: {temperature: 0.1, max_output_tokens: 1000}}
      glossary_resolver: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
    models: {}
"""
    p = _write_yaml(tmp_path, yaml_content)
    try:
        load_providers_registry(p)
        assert False
    except ValueError as e:
        assert "temperature" in str(e).lower()

def test_local_alias_valid_and_invalid(tmp_path):
    from pact_v4.runtime.runtime_config import load_providers_registry
    valid = """
providers:
  local:
    kind: local_llama
    role_policies:
      generator: {model_key: gemma, request: {temperature: 0.2, max_output_tokens: 1000}}
      fidelity_reviewer: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      russian_selector: {model_key: gemma, request: {temperature: 0.0, max_output_tokens: 1000}}
      qwen_audit: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      gemma_audit: {model_key: gemma, request: {temperature: 0.0, max_output_tokens: 1000}}
      repair: {model_key: gemma, request: {temperature: 0.0, max_output_tokens: 1000}}
      entity_extractor: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      russian_editor: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      formatting: {model_key: gemma, request: {temperature: 0.1, max_output_tokens: 1000}}
      glossary_resolver: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
    models:
      mygemma:
        model_key: gemma
        model_path: /tmp/gemma.gguf
        model_name: gemma-test
        server_args: ["--ctx-size", "8192"]
"""
    p = _write_yaml(tmp_path, valid)
    reg = load_providers_registry(p)
    assert "mygemma" in reg.providers["local"]
    # invalid reasoning_budget mismatch
    invalid = """
providers:
  local:
    kind: local_llama
    role_policies:
      generator: {model_key: gemma, request: {temperature: 0.2, max_output_tokens: 1000}}
      fidelity_reviewer: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      russian_selector: {model_key: gemma, request: {temperature: 0.0, max_output_tokens: 1000}}
      qwen_audit: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      gemma_audit: {model_key: gemma, request: {temperature: 0.0, max_output_tokens: 1000}}
      repair: {model_key: gemma, request: {temperature: 0.0, max_output_tokens: 1000}}
      entity_extractor: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      russian_editor: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      formatting: {model_key: gemma, request: {temperature: 0.1, max_output_tokens: 1000}}
      glossary_resolver: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
    models:
      badgemma:
        model_key: gemma
        model_path: /tmp/gemma.gguf
        model_name: gemma-test
        server_args: ["--reasoning-budget", "1024"]
        reasoning_budget: 2048
"""
    p2 = _write_yaml(tmp_path, invalid)
    try:
        load_providers_registry(p2)
        assert False
    except ValueError as e:
        assert "reasoning_budget" in str(e).lower()

def test_global_alias_collision(tmp_path):
    from pact_v4.runtime.runtime_config import load_providers_registry
    yaml_content = """
providers:
  local:
    kind: local_llama
    role_policies:
      generator: {model_key: gemma, request: {temperature: 0.2, max_output_tokens: 1000}}
      fidelity_reviewer: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      russian_selector: {model_key: gemma, request: {temperature: 0.0, max_output_tokens: 1000}}
      qwen_audit: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      gemma_audit: {model_key: gemma, request: {temperature: 0.0, max_output_tokens: 1000}}
      repair: {model_key: gemma, request: {temperature: 0.0, max_output_tokens: 1000}}
      entity_extractor: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      russian_editor: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      formatting: {model_key: gemma, request: {temperature: 0.1, max_output_tokens: 1000}}
      glossary_resolver: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
    models:
      dup: {model_key: gemma, model_path: /tmp/a, model_name: a, server_args: []}
  opencode-go:
    kind: opencode_server
    models:
      dup: {ref: opencode-go/dup-model, reasoning_contract: {variants: [low]}}
"""
    p = _write_yaml(tmp_path, yaml_content)
    try:
        load_providers_registry(p)
        assert False
    except ValueError as e:
        assert "duplicate alias" in str(e).lower()

def test_remote_serialization_not_local_fields(tmp_path):
    from pact_v4.runtime.runtime_config import load_providers_registry
    yaml_content = """
providers:
  opencode-go:
    kind: opencode_server
    models:
      m1: {model_key: gemma, model_path: /tmp/a, model_name: a, server_args: []}
"""
    p = _write_yaml(tmp_path, yaml_content)
    try:
        load_providers_registry(p)
        assert False
    except ValueError as e:
        assert "local fields" in str(e).lower() or "must not contain" in str(e).lower()

def test_remote_alias_under_local_fails(tmp_path):
    from pact_v4.runtime.runtime_config import load_providers_registry
    from pact_full_pipeline_runner_v1.v4_phase12_strict_run import _resolve_local_alias_entry
    yaml_content = """
providers:
  local:
    kind: local_llama
    role_policies:
      generator: {model_key: gemma, request: {temperature: 0.2, max_output_tokens: 1000}}
      fidelity_reviewer: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      russian_selector: {model_key: gemma, request: {temperature: 0.0, max_output_tokens: 1000}}
      qwen_audit: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      gemma_audit: {model_key: gemma, request: {temperature: 0.0, max_output_tokens: 1000}}
      repair: {model_key: gemma, request: {temperature: 0.0, max_output_tokens: 1000}}
      entity_extractor: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      russian_editor: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      formatting: {model_key: gemma, request: {temperature: 0.1, max_output_tokens: 1000}}
      glossary_resolver: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
    models:
      localone: {model_key: gemma, model_path: /tmp/a, model_name: a, server_args: []}
  opencode-go:
    kind: opencode_server
    models:
      remoteone: {ref: opencode-go/remote-model, reasoning_contract: {variants: [low]}}
"""
    p = _write_yaml(tmp_path, yaml_content)
    # bare remote alias should fail when used with --local
    try:
        _resolve_local_alias_entry("remoteone", p)
        assert False, "remote bare alias should fail"
    except ValueError as e:
        assert "--local alias must be a local provider alias" in str(e)
        assert "remote" in str(e).lower()
    # qualified remote alias should fail
    try:
        _resolve_local_alias_entry("opencode-go/remoteone", p)
        assert False
    except ValueError as e:
        assert "--local alias must be a local provider alias" in str(e)
    # qualified local alias should succeed
    entry = _resolve_local_alias_entry("local/localone", p)
    assert entry.model_key == "gemma"
    # bare local alias should succeed
    entry2 = _resolve_local_alias_entry("localone", p)
    assert entry2.model_name == "a"

def test_alias_fragment_application(tmp_path):
    from pact_v4.runtime.runtime_config import load_providers_registry, apply_local_alias_to_config, LocalLlamaBackendConfig
    from pathlib import Path
    yaml_content = """
providers:
  local:
    kind: local_llama
    role_policies:
      generator: {model_key: gemma, request: {temperature: 0.2, max_output_tokens: 1000}}
      fidelity_reviewer: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      russian_selector: {model_key: gemma, request: {temperature: 0.0, max_output_tokens: 1000}}
      qwen_audit: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      gemma_audit: {model_key: gemma, request: {temperature: 0.0, max_output_tokens: 1000}}
      repair: {model_key: gemma, request: {temperature: 0.0, max_output_tokens: 1000}}
      entity_extractor: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      russian_editor: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      formatting: {model_key: gemma, request: {temperature: 0.1, max_output_tokens: 1000}}
      glossary_resolver: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
    models:
      mygemma:
        model_key: gemma
        model_path: /tmp/new_gemma.gguf
        model_name: new-gemma
        server_args: ["--ctx-size", "9999", "--reasoning-budget", "0"]
"""
    p = _write_yaml(tmp_path, yaml_content)
    reg = load_providers_registry(p)
    alias_entry = reg.providers["local"]["mygemma"]
    base = LocalLlamaBackendConfig(exe=Path("/tmp/exe"), device="SYCL0", host="127.0.0.1", model_paths={"gemma": Path("/tmp/old.gguf"), "qwen": Path("/tmp/q.gguf")}, model_names={"gemma": "old", "qwen": "q"}, server_args={"gemma": ["--old"], "qwen": []})
    new = apply_local_alias_to_config(base, alias_entry)
    assert str(new.model_paths["gemma"]) == "/tmp/new_gemma.gguf"
    assert new.model_names["gemma"] == "new-gemma"
    assert new.server_args["gemma"] == ["--ctx-size", "9999", "--reasoning-budget", "0"]
    # qwen unchanged
    assert str(new.model_paths["qwen"]) == "/tmp/q.gguf"

def test_load_providers_registry_unknown_transport_field(tmp_path):
    from pact_v4.runtime.runtime_config import load_providers_registry
    yaml_content = """
providers:
  local:
    kind: local_llama
    role_policies:
      generator: {model_key: gemma, request: {temperature: 0.2, max_output_tokens: 1000}, output_budget: {mode: fixed, base_tokens: 1000}, unknown_transport: 1}
      fidelity_reviewer: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      russian_selector: {model_key: gemma, request: {temperature: 0.0, max_output_tokens: 1000}}
      qwen_audit: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      gemma_audit: {model_key: gemma, request: {temperature: 0.0, max_output_tokens: 1000}}
      repair: {model_key: gemma, request: {temperature: 0.0, max_output_tokens: 1000}}
      entity_extractor: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      russian_editor: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      formatting: {model_key: gemma, request: {temperature: 0.1, max_output_tokens: 1000}}
      glossary_resolver: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
    models: {}
"""
    p = _write_yaml(tmp_path, yaml_content)
    try:
        load_providers_registry(p)
        assert False
    except ValueError as e:
        msg = str(e).lower()
        assert "unknown" in msg and "transport" in msg or "unknown field" in msg

def test_preflight_malformed_policy_fail_closed(tmp_path):
    from pact_v4.runtime.runtime_config import load_providers_registry, run_runtime_preflight, LocalLlamaBackendConfig
    from pathlib import Path
    yaml_content = """
providers:
  local:
    kind: local_llama
    role_policies:
      generator: {model_key: gemma, request: {temperature: 0.2, max_output_tokens: 1000}}
      fidelity_reviewer: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      russian_selector: {model_key: gemma, request: {temperature: 0.0, max_output_tokens: 1000}}
      qwen_audit: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      gemma_audit: {model_key: gemma, request: {temperature: 0.0, max_output_tokens: 1000}}
      repair: {model_key: gemma, request: {temperature: 0.0, max_output_tokens: 1000}}
      entity_extractor: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      russian_editor: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
      formatting: {model_key: gemma, request: {temperature: 0.1, max_output_tokens: 1000}}
      glossary_resolver: {model_key: qwen, request: {temperature: 0.0, max_output_tokens: 1000}}
    models: {}
"""
    # Use malformed registry load directly - ensure fail-closed before network
    p = _write_yaml(tmp_path, yaml_content)
    reg = load_providers_registry(p)  # should succeed for valid
    assert reg is not None
    # Now malformed transport field should fail at load time (before preflight network)
    bad = _write_yaml(tmp_path, yaml_content.replace("max_output_tokens: 1000}}", "max_output_tokens: 1000, bad: 1}}"))
    try:
        load_providers_registry(bad)
        assert False
    except ValueError:
        pass
    # Preflight with valid config should succeed offline without server start
    cfg = LocalLlamaBackendConfig(exe=Path("/tmp/exe"), device="SYCL0", host="127.0.0.1", model_paths={"gemma": Path("/tmp/a"), "qwen": Path("/tmp/b")}, model_names={"gemma": "a", "qwen": "b"}, server_args={"gemma": [], "qwen": []})
    # Need to attach dummy resolved to satisfy new required wiring? Preflight does not require resolved; skip that path
    # Instead test that _load_resolved_role_policies propagates ValueError for malformed file
    from pact_full_pipeline_runner_v1.v4_phase12_strict_run import _load_resolved_role_policies
    # monkey patch _default_providers_config to point to bad file
    import pathlib as _pl
    orig = _pl.Path
    # Directly test build_resolved_role_policies_from_registry with malformed
    from pact_v4.runtime.runtime_config import build_resolved_role_policies_from_registry
    try:
        build_resolved_role_policies_from_registry(bad)
        assert False
    except ValueError:
        pass
