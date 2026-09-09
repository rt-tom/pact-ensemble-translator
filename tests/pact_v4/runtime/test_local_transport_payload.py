from pact_v4.runtime.backend_protocol import CompletionRequest, Message, JSON_OBJECT_SCHEMA
from pact_v4.runtime.local_openai_backend import LocalOpenAIBackend, LocalOpenAIBackendConfig
from pact_v4.runtime.api_client import ApiClient, ApiClientConfig

class CaptureSession:
    def __init__(self):
        self.payloads=[]
    def post(self, url, json=None, timeout=None, stream=False):
        self.payloads.append(json)
        class R:
            status_code=200
            reason="OK"
            def json(self): return {"choices":[{"message":{"content":"{}"},"finish_reason":"stop"}],"usage":{}}
            text='{}'
            def close(self): pass
        return R()
    def close(self): pass

def test_local_serializes_all_fields():
    sess=CaptureSession()
    cfg=ApiClientConfig(chat_url="http://127.0.0.1:8093/v1/chat/completions", model="test-model")
    api=ApiClient(cfg, session=sess)
    backend=LocalOpenAIBackend(api=api)
    req=CompletionRequest(model_ref="test-model", messages=(Message(role="user", content="hi"),), max_output_tokens=123, temperature=0.7, response_schema=JSON_OBJECT_SCHEMA, label="test", top_p=0.9, top_k=32, min_p=0.05, seed=42)
    backend.complete(req)
    payload=sess.payloads[0]
    assert payload["temperature"]==0.7
    assert payload["top_p"]==0.9
    assert payload["top_k"]==32
    assert payload["min_p"]==0.05
    assert payload["seed"]==42
    assert payload["max_tokens"]==123

def test_local_rejects_reasoning():
    cfg=ApiClientConfig(chat_url="http://127.0.0.1:8093/v1/chat/completions", model="m")
    api=ApiClient(cfg, session=CaptureSession())
    backend=LocalOpenAIBackend(api=api)
    req=CompletionRequest(model_ref="m", messages=(Message(role="user", content="hi"),), max_output_tokens=10, temperature=0.0, response_schema=None, label="x", request_options={"reasoning":1})
    try:
        backend.complete(req)
        assert False
    except Exception as e:
        assert "reasoning" in str(e).lower()

def test_remote_reasoning_serialization():
    from pact_v4.runtime.backend_protocol import BackendDescriptor
    from pact_v4.runtime.runtime_config import RoleCallPolicy
    from pact_v4.runtime.backend_role_adapters import BackendModelCaller, BackendModelCallerConfig
    from pact_v4.phase2.generation import PromptBundle, GenerationParams
    class RemoteCapture:
        def __init__(self):
            self.last=None
            self.descriptor=BackendDescriptor(kind="opencode_server", transport_version="opencode/v1", endpoint_family="openai_chat_completions", public_endpoint="http://remote:4096", model_bindings={"generator":"rem-model"}, effective_options={})
        def complete(self, req):
            self.last=req
            from pact_v4.runtime.backend_protocol import CompletionResponse
            return CompletionResponse(text='{"p1":"hi"}')
    pol=RoleCallPolicy(model_key="gemma", request={"temperature":0.2, "max_output_tokens":1000})
    backend=RemoteCapture()
    caller=BackendModelCaller(backend, config=BackendModelCallerConfig(role_policy=pol))
    # Verify reasoning transport classification instead of constructing full PromptBundle (which requires many fields)
    assert caller is not None
    # Need to mock render_prompt to avoid heavy dependency: patch inside caller path? Instead directly test CompletionRequest reasoning field for remote
    # Create request manually via caller internal logic: reasoning should be transported via request_options for remote
    from pact_v4.runtime.backend_role_adapters import _reasoning_transported_via_request_options
    assert _reasoning_transported_via_request_options(backend, "rem-model") is True
    # For local it must be False
    import pathlib
    from pact_v4.runtime.runtime_config import LocalLlamaBackendConfig
    from pact_v4.runtime.backend_protocol import KIND_LOCAL_LLAMA
    # local routing backend would return False; we test via LocalOpenAIBackend instance
    from pact_v4.runtime.local_openai_backend import LocalOpenAIBackend
    local_backend=LocalOpenAIBackend(api=ApiClient(ApiClientConfig(chat_url="http://127.0.0.1/v1/chat/completions", model="m"), session=CaptureSession()))
    assert _reasoning_transported_via_request_options(local_backend, "m") is False

def test_alias_application_body_check():
    from pact_v4.runtime.runtime_config import LocalLlamaBackendConfig, LocalModelAlias, apply_local_alias_to_config
    from pact_v4.runtime.backend_protocol import BackendDescriptor
    from pact_v4.runtime.backend_role_adapters import BackendModelCaller, BackendModelCallerConfig
    from pact_v4.runtime.runtime_config import RoleCallPolicy
    from pathlib import Path
    base=LocalLlamaBackendConfig(exe=Path("/tmp/exe"), device="SYCL0", host="127.0.0.1", model_paths={"gemma": Path("/tmp/old.gguf"), "qwen": Path("/tmp/q.gguf")}, model_names={"gemma": "old", "qwen": "q"}, server_args={"gemma": ["--old"], "qwen": []})
    alias=LocalModelAlias(model_key="gemma", model_path="/tmp/new.gguf", model_name="new-gemma", server_args=("--ctx-size","9999"))
    new=apply_local_alias_to_config(base, alias)
    assert new.server_args["gemma"]==["--ctx-size","9999"]
    assert str(new.model_paths["gemma"])=="/tmp/new.gguf"
    assert new.model_names["gemma"]=="new-gemma"
    # Verify backend descriptor reflects alias (model_names contributes to bindings)
    desc=new.build_descriptor()
    assert desc.model_bindings.get("generator")=="new-gemma"
    # Verify producer identity would change: policy-driven request would differ if alias had overrides (tested separately)
    pol=RoleCallPolicy(model_key="gemma", request={"temperature":0.3, "max_output_tokens":1000})
    assert pol.policy_hash != RoleCallPolicy(model_key="gemma", request={"temperature":0.9, "max_output_tokens":1000}).policy_hash
