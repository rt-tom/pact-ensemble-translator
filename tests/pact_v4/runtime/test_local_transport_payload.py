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
