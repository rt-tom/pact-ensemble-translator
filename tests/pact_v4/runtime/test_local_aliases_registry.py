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
