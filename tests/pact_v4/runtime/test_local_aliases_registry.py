import pathlib, tempfile, textwrap
import yaml
from pact_v4.runtime.runtime_config import RoleBudget, OutputBudgetPolicy, ResolvedModelPair, LocalModelSpec, load_providers_registry, build_resolved_pair_from_registry, derive_max_output_tokens

def test_role_budgets_and_pair():
    # New model-centric: role_budgets top-level + models request
    from pathlib import Path
    p = Path("configs/providers.yaml")
    reg = load_providers_registry(p)
    assert "local" in reg.providers
    assert len(reg.role_budgets) == 10
    pair = build_resolved_pair_from_registry(reg, "gemma", "qwen")
    assert pair.translator_model.model_key == "gemma"
    assert pair.reviewer_model.model_key == "qwen"
    assert pair.aggregate_hash

def test_missing_role_budgets_fallback():
    # role_budgets is required fail-closed (no defaults)
    import tempfile, textwrap
    from pathlib import Path
    from pact_v4.runtime.runtime_config import load_providers_registry
    content = textwrap.dedent("""
providers:
  opencode-go:
    kind: opencode_server
    models:
      m1:
        ref: opencode-go/m1
        reasoning_contract: {variants: [low]}
""")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "providers.yaml"
        p.write_text(content, encoding="utf-8")
        try:
            load_providers_registry(p)
            assert False, "should fail when role_budgets missing"
        except ValueError as e:
            assert "role_budgets" in str(e).lower()

def test_derive_budget():
    from pact_v4.runtime.runtime_config import RoleBudget, OutputBudgetPolicy, derive_max_output_tokens
    b = RoleBudget(max_output_tokens=1000, output_budget=OutputBudgetPolicy(mode="floor_plus_per_item", floor_tokens=1000, per_item_tokens=10, ceiling=2000))
    assert derive_max_output_tokens(b, item_count=5)==1050

def test_malformed_model_request_rejected(tmp_path):
    from pact_v4.runtime.runtime_config import load_providers_registry
    yaml_content = textwrap.dedent("""
role_budgets:
  generator: {max_output_tokens: 1000}
  repair: {max_output_tokens: 1000}
  formatting: {max_output_tokens: 1000}
  gemma_audit: {max_output_tokens: 1000}
  qwen_audit: {max_output_tokens: 1000}
  fidelity_reviewer: {max_output_tokens: 1000}
  russian_selector: {max_output_tokens: 1000}
  entity_extractor: {max_output_tokens: 1000}
  russian_editor: {max_output_tokens: 1000}
  glossary_resolver: {max_output_tokens: 1000}
providers:
  local:
    kind: local_llama
    models:
      badmodel:
        model_key: gemma
        model_path: /tmp/a
        model_name: a
        server_args: ["--reasoning-budget", "2000"]
        reasoning_budget: 2000
        request: {unknown_field: 1}
""")
    p = tmp_path / "providers.yaml"
    p.write_text(yaml_content, encoding="utf-8")
    try:
        load_providers_registry(p)
        assert False, "should have failed on unknown request field"
    except ValueError as e:
        assert "unknown request field" in str(e).lower()

def test_model_request_forbids_max_output_tokens(tmp_path):
    from pact_v4.runtime.runtime_config import load_providers_registry
    yaml_content = textwrap.dedent("""
role_budgets:
  generator: {max_output_tokens: 1000}
  repair: {max_output_tokens: 1000}
  formatting: {max_output_tokens: 1000}
  gemma_audit: {max_output_tokens: 1000}
  qwen_audit: {max_output_tokens: 1000}
  fidelity_reviewer: {max_output_tokens: 1000}
  russian_selector: {max_output_tokens: 1000}
  entity_extractor: {max_output_tokens: 1000}
  russian_editor: {max_output_tokens: 1000}
  glossary_resolver: {max_output_tokens: 1000}
providers:
  local:
    kind: local_llama
    models:
      badmodel:
        model_key: gemma
        model_path: /tmp/a
        model_name: a
        server_args: ["--reasoning-budget", "2000"]
        reasoning_budget: 2000
        request: {temperature: 0.2, max_output_tokens: 1000}
""")
    p = tmp_path / "providers.yaml"
    p.write_text(yaml_content, encoding="utf-8")
    try:
        load_providers_registry(p)
        assert False
    except ValueError as e:
        assert "max_output_tokens" in str(e).lower()

def test_local_alias_valid_and_invalid(tmp_path):
    from pact_v4.runtime.runtime_config import load_providers_registry, build_resolved_pair_from_registry
    valid = textwrap.dedent("""
role_budgets:
  generator: {max_output_tokens: 1000}
  repair: {max_output_tokens: 1000}
  formatting: {max_output_tokens: 1000}
  gemma_audit: {max_output_tokens: 1000}
  qwen_audit: {max_output_tokens: 1000}
  fidelity_reviewer: {max_output_tokens: 1000}
  russian_selector: {max_output_tokens: 1000}
  entity_extractor: {max_output_tokens: 1000}
  russian_editor: {max_output_tokens: 1000}
  glossary_resolver: {max_output_tokens: 1000}
providers:
  local:
    kind: local_llama
    models:
      mygemma:
        model_key: gemma
        model_path: /tmp/gemma.gguf
        model_name: gemma-test
        server_args: ["--ctx-size", "8192", "--reasoning-budget", "2000"]
        reasoning_budget: 2000
        request: {temperature: 0.2}
      myqwen:
        model_key: qwen
        model_path: /tmp/qwen.gguf
        model_name: qwen-test
        server_args: ["--ctx-size", "8192", "--reasoning-budget", "8192"]
        reasoning_budget: 8192
        request: {temperature: 0.0}
""")
    p = tmp_path / "providers.yaml"
    p.write_text(valid, encoding="utf-8")
    reg = load_providers_registry(p)
    assert "mygemma" in reg.providers["local"]
    pair = build_resolved_pair_from_registry(reg, "MYGEMMA", "myqwen")  # case-insensitive
    assert pair.translator_model.model_name == "gemma-test"
    # invalid reasoning_budget mismatch
    invalid = textwrap.dedent("""
role_budgets:
  generator: {max_output_tokens: 1000}
  repair: {max_output_tokens: 1000}
  formatting: {max_output_tokens: 1000}
  gemma_audit: {max_output_tokens: 1000}
  qwen_audit: {max_output_tokens: 1000}
  fidelity_reviewer: {max_output_tokens: 1000}
  russian_selector: {max_output_tokens: 1000}
  entity_extractor: {max_output_tokens: 1000}
  russian_editor: {max_output_tokens: 1000}
  glossary_resolver: {max_output_tokens: 1000}
providers:
  local:
    kind: local_llama
    models:
      badgemma:
        model_key: gemma
        model_path: /tmp/gemma.gguf
        model_name: gemma-test
        server_args: ["--reasoning-budget", "1024"]
        reasoning_budget: 2048
        request: {temperature: 0.2}
""")
    p2 = tmp_path / "providers2.yaml"
    p2.write_text(invalid, encoding="utf-8")
    try:
        load_providers_registry(p2)
        assert False
    except ValueError as e:
        assert "reasoning_budget" in str(e).lower()

def test_global_alias_collision(tmp_path):
    from pact_v4.runtime.runtime_config import load_providers_registry
    yaml_content = textwrap.dedent("""
role_budgets:
  generator: {max_output_tokens: 1000}
  repair: {max_output_tokens: 1000}
  formatting: {max_output_tokens: 1000}
  gemma_audit: {max_output_tokens: 1000}
  qwen_audit: {max_output_tokens: 1000}
  fidelity_reviewer: {max_output_tokens: 1000}
  russian_selector: {max_output_tokens: 1000}
  entity_extractor: {max_output_tokens: 1000}
  russian_editor: {max_output_tokens: 1000}
  glossary_resolver: {max_output_tokens: 1000}
providers:
  local:
    kind: local_llama
    models:
      dup: {model_key: gemma, model_path: /tmp/a, model_name: a, server_args: ["--reasoning-budget", "2000"], reasoning_budget: 2000, request: {temperature: 0.0}}
  opencode-go:
    kind: opencode_server
    models:
      dup: {ref: opencode-go/dup-model, reasoning_contract: {variants: [low]}}
""")
    p = tmp_path / "providers.yaml"
    p.write_text(yaml_content, encoding="utf-8")
    try:
        load_providers_registry(p)
        assert False
    except ValueError as e:
        assert "duplicate alias" in str(e).lower()

def test_pair_single_alias_rejected(tmp_path):
    from pact_v4.runtime.runtime_config import parse_local_pair_arg
    try:
        parse_local_pair_arg("gemma")
        assert False
    except ValueError as e:
        assert "pair required" in str(e).lower()

def test_pair_lookup_case_insensitive_and_remote_rejected(tmp_path):
    from pact_v4.runtime.runtime_config import load_providers_registry, build_resolved_pair_from_registry
    yaml_content = textwrap.dedent("""
role_budgets:
  generator: {max_output_tokens: 1000}
  repair: {max_output_tokens: 1000}
  formatting: {max_output_tokens: 1000}
  gemma_audit: {max_output_tokens: 1000}
  qwen_audit: {max_output_tokens: 1000}
  fidelity_reviewer: {max_output_tokens: 1000}
  russian_selector: {max_output_tokens: 1000}
  entity_extractor: {max_output_tokens: 1000}
  russian_editor: {max_output_tokens: 1000}
  glossary_resolver: {max_output_tokens: 1000}
providers:
  local:
    kind: local_llama
    models:
      localone: {model_key: gemma, model_path: /tmp/a, model_name: a, server_args: ["--reasoning-budget", "2000"], reasoning_budget: 2000, request: {temperature: 0.0}}
      localtwo: {model_key: qwen, model_path: /tmp/b, model_name: b, server_args: ["--reasoning-budget", "8192"], reasoning_budget: 8192, request: {temperature: 0.0}}
  opencode-go:
    kind: opencode_server
    models:
      remoteone: {ref: opencode-go/remote-model, reasoning_contract: {variants: [low]}}
""")
    p = tmp_path / "providers.yaml"
    p.write_text(yaml_content, encoding="utf-8")
    reg = load_providers_registry(p)
    # remote alias as part of pair should fail
    try:
        build_resolved_pair_from_registry(reg, "remoteone", "localtwo")
        assert False
    except ValueError as e:
        assert "not found" in str(e).lower() or "remote" in str(e).lower()
    # case-insensitive success
    pair = build_resolved_pair_from_registry(reg, "LOCALONE", "localtwo")
    assert pair.translator_model.model_key == "gemma"
