"""Unit tests for the backend protocol (plan §14.1).

Covers: canonical backend identity; secrets absent from identity/public
record; unknown request option rejected; usage/finish_reason/request_id
normalization; structured output canonically re-serialized; request
validation.
"""
from __future__ import annotations

import pytest

from pact_v4.runtime.backend_protocol import (
    ALLOWED_REQUEST_OPTIONS,
    BackendCallRecord,
    BackendDescriptor,
    CompletionRequest,
    CompletionResponse,
    Message,
)


def _descriptor(**overrides) -> BackendDescriptor:
    values = {
        "kind": "local_llama",
        "transport_version": "openai-chat-completions/v1",
        "endpoint_family": "openai_chat_completions",
        "public_endpoint": "http://127.0.0.1:8080/v1/chat/completions",
        "model_bindings": {"default": "gemma-4-26B"},
        "effective_options": {"temperature": 0.2, "http_retries": 3},
    }
    values.update(overrides)
    return BackendDescriptor(**values)


# ---------------------------------------------------------------------------
# Canonical identity
# ---------------------------------------------------------------------------


def test_descriptor_identity_hash_is_deterministic_and_content_derived():
    a = _descriptor()
    b = _descriptor()
    assert a.identity_hash == b.identity_hash
    assert len(a.identity_hash) == 64


def test_descriptor_identity_changes_with_model_binding():
    a = _descriptor(model_bindings={"default": "model-a"})
    b = _descriptor(model_bindings={"default": "model-b"})
    assert a.identity_hash != b.identity_hash


def test_descriptor_identity_changes_with_effective_options():
    a = _descriptor(effective_options={"temperature": 0.2, "http_retries": 3})
    b = _descriptor(effective_options={"temperature": 0.0, "http_retries": 3})
    assert a.identity_hash != b.identity_hash


def test_descriptor_identity_changes_with_endpoint_family_and_kind():
    a = _descriptor(kind="local_llama", endpoint_family="openai_chat_completions")
    b = _descriptor(kind="opencode_server", endpoint_family="opencode_http")
    assert a.identity_hash != b.identity_hash


def test_descriptor_identity_excludes_local_tcp_port_and_public_endpoint():
    a = _descriptor(public_endpoint="http://127.0.0.1:8080/v1/chat/completions")
    b = _descriptor(public_endpoint="http://127.0.0.1:18080/v1/chat/completions")
    assert a.identity_hash == b.identity_hash


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


def test_descriptor_identity_excludes_api_key_and_password():
    with_secret = _descriptor(
        effective_options={
            "temperature": 0.2,
            "http_retries": 3,
            "api_key": "sk-super-secret",
            "password": "hunter2",
        }
    )
    without_secret = _descriptor(
        effective_options={"temperature": 0.2, "http_retries": 3}
    )
    # Credential rotation must not change cache/resume identity (plan §11).
    assert with_secret.identity_hash == without_secret.identity_hash


def test_public_record_never_contains_secrets():
    record = _descriptor(
        effective_options={
            "temperature": 0.2,
            "http_retries": 3,
            "authorization": "Bearer abc",
            "api_key": "sk-secret",
        }
    ).public_record()
    text = repr(record)
    assert "sk-secret" not in text
    assert "Bearer abc" not in text
    assert "authorization" not in text
    assert "api_key" not in text
    assert "identity_hash" in record


def test_public_record_contains_public_identity_fields():
    record = _descriptor().public_record()
    assert record["kind"] == "local_llama"
    assert record["transport_version"] == "openai-chat-completions/v1"
    assert record["model_bindings"] == {"default": "gemma-4-26B"}
    assert record["identity_hash"] == _descriptor().identity_hash


# ---------------------------------------------------------------------------
# CompletionRequest validation
# ---------------------------------------------------------------------------


def _request(**overrides) -> CompletionRequest:
    values = {
        "model_ref": "gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf",
        "messages": (Message(role="user", content="Hello"),),
        "max_output_tokens": 512,
        "temperature": 0.2,
        "response_schema": {"type": "object"},
        "label": "unit",
        "request_options": {},
    }
    values.update(overrides)
    return CompletionRequest(**values)


def test_request_rejects_unknown_request_option():
    with pytest.raises(ValueError, match="unknown request option"):
        _request(request_options={"top_p": 0.9, "bogus_option": 1})


def test_request_accepts_allowlisted_request_options():
    request = _request(request_options={"top_p": 0.9, "seed": 7})
    assert request.request_options == {"top_p": 0.9, "seed": 7}


def test_request_rejects_empty_messages():
    with pytest.raises(ValueError, match="messages"):
        _request(messages=())


def test_request_rejects_non_message_tuple():
    with pytest.raises(ValueError, match="Message"):
        _request(messages=({"role": "user", "content": "x"},))


def test_request_rejects_non_positive_max_tokens():
    with pytest.raises(ValueError, match="max_output_tokens"):
        _request(max_output_tokens=0)


def test_request_rejects_empty_label():
    with pytest.raises(ValueError, match="label"):
        _request(label="")


# ---------------------------------------------------------------------------
# CompletionResponse normalization
# ---------------------------------------------------------------------------


def test_response_structured_is_canonically_serialized_to_text():
    response = CompletionResponse(
        structured={"p1": "Привет", "p2": "Мир"},
        provider="opencode",
        model="provider/model",
    )
    import json

    assert json.loads(response.text) == {"p1": "Привет", "p2": "Мир"}
    assert response.text.startswith('{"p1"')  # canonical key order


def test_response_normalizes_none_usage_and_metadata():
    response = CompletionResponse(text="hi", usage=None, raw_metadata=None)
    assert response.usage == {}
    assert response.raw_metadata == {}


def test_response_keeps_explicit_text_over_structured():
    response = CompletionResponse(text="raw", structured={"p1": "x"})
    assert response.text == "raw"


def test_response_normalizes_usage_and_finish_reason_types():
    response = CompletionResponse(
        text="ok",
        finish_reason=None,
        usage={"prompt_tokens": 1, "completion_tokens": 2},
    )
    assert response.finish_reason is None
    assert response.usage["prompt_tokens"] == 1


# ---------------------------------------------------------------------------
# BackendCallRecord
# ---------------------------------------------------------------------------


def test_call_record_keeps_normalized_usage_and_metadata():
    record = BackendCallRecord(
        label="phase2b/fidelity_first/c1",
        model_ref="gemma-4-26B",
        request_id=None,
        session_id=None,
        retry_count=0,
        finish_reason="stop",
        usage=None,
        wall_seconds=1.5,
        raw_metadata={"http_status": 200},
    )
    assert record.usage == {}
    assert record.raw_metadata == {"http_status": 200}
    assert record.retry_count == 0


def test_message_requires_non_empty_role_and_string_content():
    with pytest.raises(ValueError, match="role"):
        Message(role="", content="x")
    with pytest.raises(ValueError, match="content"):
        Message(role="user", content=123)  # type: ignore[arg-type]
