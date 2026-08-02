"""Tests for ``pact_v4.runtime.local_openai_backend``.

``LocalOpenAIBackend`` is the ``CompletionBackend`` adapter over the
existing ``ApiClient``. No network: ``requests.Session`` is replaced with
a fake, exactly as ``test_api_client.py`` does.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from pact_v4.runtime.api_client import ApiClient, ApiClientConfig
from pact_v4.runtime.backend_protocol import (
    CompletionError,
    CompletionRequest,
    CompletionResponse,
    Message,
)
from pact_v4.runtime.local_openai_backend import (
    LocalOpenAIBackend,
    LocalOpenAIBackendConfig,
)


class _FakeResponse:
    def __init__(self, *, status_code: int, text: str, json_payload: Optional[Dict[str, Any]] = None):
        self.status_code = status_code
        self.text = text
        self.reason = "OK" if 200 <= status_code < 300 else "Error"
        self._payload = json_payload

    def json(self) -> Dict[str, Any]:
        if self._payload is None:
            raise ValueError("no JSON payload")
        return self._payload


class _FakeSession:
    def __init__(self, script: List[Any]):
        self._script = list(script)
        self.posts: List[Dict[str, Any]] = []

    def post(self, url: str, *, json: Dict[str, Any], timeout: float):
        self.posts.append({"url": url, "json": json, "timeout": timeout})
        if not self._script:
            raise AssertionError("FakeSession: script exhausted")
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, _FakeResponse):
            return item
        status, text, payload = item
        return _FakeResponse(status_code=status, text=text, json_payload=payload)


def _ok(text: str) -> _FakeResponse:
    return _FakeResponse(
        status_code=200, text=text,
        json_payload={
            "choices": [
                {
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        },
    )


def _client(cfg: Optional[ApiClientConfig] = None, script: Optional[List[Any]] = None):
    return ApiClient(
        cfg or ApiClientConfig(retry_delay_seconds=0.0),
        session=_FakeSession(script or []),
    )


def _request(**overrides) -> CompletionRequest:
    values = {
        "model_ref": ApiClientConfig().model,
        "messages": (Message(role="user", content="ping"),),
        "max_output_tokens": 256,
        "temperature": 0.0,
        "response_schema": {"type": "object"},
        "label": "unit",
        "request_options": {},
    }
    values.update(overrides)
    return CompletionRequest(**values)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_complete_returns_normalized_text_and_usage():
    backend = LocalOpenAIBackend(api=_client(script=[_ok("hello")]))
    response = backend.complete(_request())
    assert isinstance(response, CompletionResponse)
    assert response.text == "hello"
    assert response.finish_reason == "stop"
    assert response.usage["prompt_tokens"] == 10
    assert response.usage["completion_tokens"] == 20
    assert response.wall_seconds >= 0
    assert response.request_id is None
    assert response.session_id is None


def test_complete_requests_json_object_when_response_schema_present():
    client = _client(script=[_ok("{}")])
    backend = LocalOpenAIBackend(api=client)
    backend.complete(_request(response_schema={"type": "object"}))
    assert client._session.posts[0]["json"].get("response_format") == {"type": "json_object"}


def test_complete_skips_json_object_when_no_response_schema():
    client = _client(script=[_ok("plain")])
    backend = LocalOpenAIBackend(api=client)
    backend.complete(_request(response_schema=None))
    assert "response_format" not in client._session.posts[0]["json"]


def test_complete_passes_max_tokens_and_temperature_and_label():
    client = _client(script=[_ok("ok")])
    backend = LocalOpenAIBackend(api=client)
    backend.complete(_request(max_output_tokens=777, temperature=0.3, label="phase2c/qwen_fidelity"))
    payload = client._session.posts[0]["json"]
    assert payload["max_tokens"] == 777
    assert payload["temperature"] == pytest.approx(0.3)
    assert payload["model"] == ApiClientConfig().model


def test_complete_records_call_records():
    backend = LocalOpenAIBackend(api=_client(script=[_ok("a"), _ok("b")]))
    backend.complete(_request(label="one"))
    backend.complete(_request(label="two"))
    records = backend.call_records()
    assert len(records) == 2
    assert [r.label for r in records] == ["one", "two"]
    assert records[0].model_ref == ApiClientConfig().model


def test_payload_model_matches_backend_config():
    client = _client(script=[_ok("ok")])
    backend = LocalOpenAIBackend(api=client)
    backend.complete(_request())
    assert client._session.posts[0]["json"]["model"] == ApiClientConfig().model


def test_complete_rejects_model_ref_mismatch():
    # The backend is serving ApiClientConfig().model; claiming another model
    # for a role must be refused, never silently re-routed.
    backend = LocalOpenAIBackend(api=_client(script=[_ok("ok")]))
    with pytest.raises(CompletionError, match="does not match"):
        backend.complete(_request(model_ref="opencode-go/deepseek-v4-flash"))


# ---------------------------------------------------------------------------
# retry_count provenance
# ---------------------------------------------------------------------------


def test_retry_count_zero_on_single_call():
    backend = LocalOpenAIBackend(api=_client(script=[_ok("ok")]))
    response = backend.complete(_request())
    assert response.retry_count == 0
    assert backend.call_records()[0].retry_count == 0


def test_retry_count_reflects_transient_retries():
    client = _client(
        ApiClientConfig(http_retries=3, retry_delay_seconds=0.0),
        script=[_FakeResponse(status_code=503, text="unavailable"), _ok("ok")],
    )
    backend = LocalOpenAIBackend(api=client)
    response = backend.complete(_request())
    assert len(client._session.posts) == 2
    assert response.retry_count == 1
    assert backend.call_records()[0].retry_count == 1


def test_retry_count_reflects_grammar_reject_fallback():
    reject = "error: response does not match the expected peg-gemma4 format"
    client = _client(
        ApiClientConfig(http_retries=3, retry_delay_seconds=0.0),
        script=[_FakeResponse(status_code=400, text=reject), _ok("{}")],
    )
    backend = LocalOpenAIBackend(api=client)
    response = backend.complete(_request())
    assert len(client._session.posts) == 2
    assert response.retry_count == 1


# ---------------------------------------------------------------------------
# Preserved ApiClient behaviour (grammar-reject fallback, bounded retries)
# ---------------------------------------------------------------------------


def test_preserves_gemma_grammar_reject_fallback():
    reject = "error: response does not match the expected peg-gemma4 format"
    client = _client(
        ApiClientConfig(http_retries=3, retry_delay_seconds=0.0),
        script=[_FakeResponse(status_code=400, text=reject), _ok("{}")],
    )
    backend = LocalOpenAIBackend(api=client)
    response = backend.complete(_request())
    assert response.text == "{}"
    assert "response_format" in client._session.posts[0]["json"]
    assert "response_format" not in client._session.posts[1]["json"]


def test_preserves_bounded_transient_retries():
    client = _client(
        ApiClientConfig(http_retries=3, retry_delay_seconds=0.0),
        script=[_FakeResponse(status_code=503, text="unavailable"), _ok("ok")],
    )
    backend = LocalOpenAIBackend(api=client)
    assert backend.complete(_request()).text == "ok"
    assert len(client._session.posts) == 2


# ---------------------------------------------------------------------------
# Errors / lifecycle
# ---------------------------------------------------------------------------


def test_complete_wraps_apiclient_error_as_completion_error():
    from requests.exceptions import ConnectionError as RequestsConnectionError

    # http_retries=1 so a single network failure exhausts the retry budget.
    backend = LocalOpenAIBackend(
        api=_client(ApiClientConfig(http_retries=1), script=[RequestsConnectionError("reset")])
    )
    with pytest.raises(CompletionError, match="reset"):
        backend.complete(_request())


def test_complete_rejects_unsupported_request_options():
    backend = LocalOpenAIBackend(api=_client(script=[_ok("ok")]))
    with pytest.raises(CompletionError, match="unsupported request option"):
        backend.complete(_request(request_options={"top_p": 0.9}))


def test_close_is_idempotent():
    backend = LocalOpenAIBackend(api=_client(script=[_ok("ok")]))
    backend.close()
    backend.close()  # must not raise


def test_complete_after_close_fails():
    backend = LocalOpenAIBackend(api=_client(script=[_ok("ok")]))
    backend.close()
    with pytest.raises(CompletionError, match="closed"):
        backend.complete(_request())


# ---------------------------------------------------------------------------
# Descriptor / identity
# ---------------------------------------------------------------------------


def test_descriptor_identity_stable_across_backends_with_same_config():
    a = LocalOpenAIBackend(api=_client())
    b = LocalOpenAIBackend(api=_client())
    assert a.descriptor.identity_hash == b.descriptor.identity_hash


def test_descriptor_identity_changes_when_sampling_changes():
    a = LocalOpenAIBackend(api=_client(ApiClientConfig(temperature=0.2)))
    b = LocalOpenAIBackend(api=_client(ApiClientConfig(temperature=0.0)))
    assert a.descriptor.identity_hash != b.descriptor.identity_hash


def test_descriptor_public_record_has_no_credentials():
    record = LocalOpenAIBackend(api=_client()).descriptor.public_record()
    assert record["kind"] == "local_llama"
    assert "public_endpoint" in record
    assert "identity_hash" in record
    text = repr(record)
    assert "api_key" not in text and "password" not in text


def test_default_constructor_wires_real_api_client():
    backend = LocalOpenAIBackend()
    assert isinstance(backend.api, ApiClient)


def test_config_constructor_wires_api_client_with_config():
    cfg = LocalOpenAIBackendConfig(
        api=ApiClientConfig(model="qwen-custom"),
        model_bindings={"generator": "qwen-custom"},
    )
    backend = LocalOpenAIBackend(config=cfg)
    assert backend.api.config.model == "qwen-custom"
    assert backend.descriptor.model_bindings == {"generator": "qwen-custom"}
