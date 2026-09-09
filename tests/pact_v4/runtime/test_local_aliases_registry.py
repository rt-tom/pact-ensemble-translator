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
