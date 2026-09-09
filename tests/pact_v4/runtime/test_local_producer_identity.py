from pact_v4.runtime.runtime_config import RoleCallPolicy, OutputBudgetPolicy, derive_max_output_tokens
from pact_v4.runtime.backend_protocol import CompletionRequest, Message, JSON_OBJECT_SCHEMA, BackendDescriptor
from pact_v4.runtime.backend_role_adapters import BackendModelCaller, BackendModelCallerConfig, BackendQwenEvaluator, BackendQwenEvaluatorConfig

class FakeBackend:
    def __init__(self, bindings):
        self.descriptor=BackendDescriptor(kind="local_llama", transport_version="v1", endpoint_family="openai_chat_completions", public_endpoint="http://127.0.0.1:8093", model_bindings=bindings, effective_options={})
        self.last=None
    def complete(self, req):
        self.last=req
        from pact_v4.runtime.backend_protocol import CompletionResponse
        return CompletionResponse(text='{"verdict":"pass","reason":"ok"}')
    def close(self): pass
    def call_records(self): return []

def test_producer_identity_changes_with_policy():
    pol1=RoleCallPolicy(model_key="gemma", request={"temperature":0.2, "seed":7, "max_output_tokens":70000})
    pol2=RoleCallPolicy(model_key="gemma", request={"temperature":0.9, "seed":7, "max_output_tokens":70000})
    assert pol1.policy_hash != pol2.policy_hash
    assert derive_max_output_tokens(pol1)==70000
    b=FakeBackend({"generator":"m"})
    c1=BackendModelCaller(b, config=BackendModelCallerConfig(role_policy=pol1))
    c2=BackendModelCaller(b, config=BackendModelCallerConfig(role_policy=pol2))
    assert c1._max_tokens != c2._max_tokens or pol1.policy_hash!=pol2.policy_hash

def test_qwen_evaluator_uses_policy():
    pol=RoleCallPolicy(model_key="qwen", request={"temperature":0.0, "max_output_tokens":16384}, output_budget=OutputBudgetPolicy(mode="floor_plus_per_item", floor_tokens=16384, per_item_tokens=128, ceiling=24576))
    b=FakeBackend({"fidelity_reviewer":"qwen-model"})
    ev=BackendQwenEvaluator(b, config=BackendQwenEvaluatorConfig(role_policy=pol))
    ev({"p1":"hello"}, {"p1":"privet"})
    assert b.last.temperature==0.0
    assert b.last.max_output_tokens>16384
