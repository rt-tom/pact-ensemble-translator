"""Tests for ``pact_v4.runtime.api_client``.

No network: ``requests.Session`` is replaced with a fake that records
calls and returns scripted ``Response`` objects. This is the offline
regression net for the production-flavoured HTTP client used by all
three Phase 2 adapters.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from pact_v4.runtime.api_client import (
    ApiClient,
    ApiClientConfig,
    ApiClientError,
    CallRecord,
)


class _FakeResponse:
    def __init__(self, *, status_code: int, text: str, json_payload: Optional[Dict[str, Any]] = None):
        self.status_code = status_code
        self.text = text
        self.reason = "OK" if 200 <= status_code < 300 else "Error"
        self.ok = 200 <= status_code < 300
        self._payload = json_payload

    def json(self) -> Dict[str, Any]:
        if self._payload is None:
            raise ValueError("no JSON payload")
        return self._payload


class _FakeSession:
    """In-memory stand-in for ``requests.Session``.

    Each entry in ``script`` is one of:
      * a ``_FakeResponse`` — return immediately,
      * an ``Exception`` instance — raise it,
      * a tuple ``(status_code, text, json_payload)`` — return a
        ``_FakeResponse`` built from those.
    """

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


def _ok_text_response(text: str) -> _FakeResponse:
    return _FakeResponse(
        status_code=200, text=text,
        json_payload={
            "choices": [
                {
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    )


def _ok_reasoning_response(text: str, reasoning: str) -> _FakeResponse:
    """llama-server style response: reasoning stream in ``reasoning_content``."""
    return _FakeResponse(
        status_code=200, text=text,
        json_payload={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": text,
                        "reasoning_content": reasoning,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    )


# ---------------------------------------------------------------------------
# Basic happy path
# ---------------------------------------------------------------------------


def test_complete_returns_assistant_text_and_records_call():
    session = _FakeSession([_ok_text_response("hello")])
    client = ApiClient(ApiClientConfig(), session=session)
    out = client.complete(
        [{"role": "user", "content": "ping"}],
        max_tokens=10,
        temperature=0.1,
        label="unit",
    )
    assert out == "hello"
    assert len(client.calls) == 1
    record = client.calls[0]
    assert record.label == "unit"
    assert record.http_status == 200
    assert record.finish_reason == "stop"
    assert record.wall_seconds >= 0
    assert record.reasoning == ""


def test_complete_records_reasoning_content():
    """Regression (2026-08-10): llama-server returns the reasoning stream in
    ``message.reasoning_content``; it must be captured in the call record so
    audit ``_reasoning.txt`` artifacts are not empty."""
    session = _FakeSession([_ok_reasoning_response("{\"ok\": true}", "thinking...")])
    client = ApiClient(ApiClientConfig(), session=session)
    out = client.complete(
        [{"role": "user", "content": "ping"}],
        max_tokens=64,
        label="unit",
    )
    assert out == "{\"ok\": true}"
    assert client.calls[0].reasoning == "thinking..."


def test_complete_emits_json_object_response_format_by_default():
    session = _FakeSession([_ok_text_response("{}")])
    client = ApiClient(ApiClientConfig(), session=session)
    client.complete([{"role": "user", "content": "x"}], max_tokens=10)
    payload = session.posts[0]["json"]
    assert payload.get("response_format") == {"type": "json_object"}


def test_complete_can_disable_response_format():
    session = _FakeSession([_ok_text_response("plain text")])
    client = ApiClient(ApiClientConfig(), session=session)
    out = client.complete(
        [{"role": "user", "content": "x"}],
        max_tokens=10, response_format_json=False,
    )
    assert out == "plain text"
    assert "response_format" not in session.posts[0]["json"]


# ---------------------------------------------------------------------------
# Gemma grammar-reject fallback
# ---------------------------------------------------------------------------


def test_complete_falls_back_when_server_rejects_json_response_format():
    reject_text = (
        "error: response does not match the expected peg-gemma4 format"
    )
    second_ok = _ok_text_response("{}")

    session = _FakeSession([
        _FakeResponse(status_code=400, text=reject_text),
        second_ok,
    ])
    client = ApiClient(
        ApiClientConfig(http_retries=3, retry_delay_seconds=0.0),
        session=session,
    )
    out = client.complete(
        [{"role": "user", "content": "x"}],
        max_tokens=10,
    )
    assert out == "{}"
    # First attempt: with response_format. Second attempt: without.
    assert "response_format" in session.posts[0]["json"]
    assert "response_format" not in session.posts[1]["json"]
    # The client remembered the rejection for the rest of its life.
    assert client._json_response_format_supported is False


def test_complete_subsequent_calls_skip_response_format_after_rejection():
    reject_text = (
        "error: response does not match the expected peg-gemma4 format"
    )
    session = _FakeSession([
        _FakeResponse(status_code=400, text=reject_text),
        _ok_text_response("{}"),
        _ok_text_response("{}"),
    ])
    client = ApiClient(
        ApiClientConfig(http_retries=3, retry_delay_seconds=0.0),
        session=session,
    )
    client.complete([{"role": "user", "content": "x"}], max_tokens=10)
    client.complete([{"role": "user", "content": "y"}], max_tokens=10)
    # Second call should not re-add response_format; the client has
    # disabled it permanently.
    assert "response_format" not in session.posts[1]["json"]
    assert "response_format" not in session.posts[2]["json"]


def test_complete_records_attempt_count_for_grammar_fallback():
    reject_text = (
        "error: response does not match the expected peg-gemma4 format"
    )
    session = _FakeSession([
        _FakeResponse(status_code=400, text=reject_text),
        _ok_text_response("{}"),
    ])
    client = ApiClient(
        ApiClientConfig(http_retries=3, retry_delay_seconds=0.0),
        session=session,
    )
    client.complete([{"role": "user", "content": "x"}], max_tokens=10)
    assert len(session.posts) == 2
    assert client.calls[0].attempt_count == 2


def test_complete_records_attempt_count_for_transient_retry():
    session = _FakeSession([
        _FakeResponse(status_code=503, text="unavailable"),
        _ok_text_response("ok"),
    ])
    client = ApiClient(
        ApiClientConfig(http_retries=3, retry_delay_seconds=0.0),
        session=session,
    )
    client.complete([{"role": "user", "content": "x"}], max_tokens=10)
    assert len(session.posts) == 2
    assert client.calls[0].attempt_count == 2


def test_complete_records_attempt_count_one_for_single_call():
    session = _FakeSession([_ok_text_response("ok")])
    client = ApiClient(ApiClientConfig(), session=session)
    client.complete([{"role": "user", "content": "x"}], max_tokens=10)
    assert client.calls[0].attempt_count == 1


# ---------------------------------------------------------------------------
# Error propagation
# ---------------------------------------------------------------------------


def test_complete_propagates_unhandled_4xx_as_apiclient_error():
    session = _FakeSession([_FakeResponse(status_code=422, text="unprocessable")])
    client = ApiClient(ApiClientConfig(), session=session)
    with pytest.raises(ApiClientError, match="HTTP 422"):
        client.complete([{"role": "user", "content": "x"}], max_tokens=10)


def test_complete_retries_5xx_then_succeeds():
    session = _FakeSession([
        _FakeResponse(status_code=503, text="unavailable"),
        _ok_text_response("ok"),
    ])
    client = ApiClient(
        ApiClientConfig(http_retries=3, retry_delay_seconds=0.0),
        session=session,
    )
    out = client.complete([{"role": "user", "content": "x"}], max_tokens=10)
    assert out == "ok"
    assert len(session.posts) == 2


def test_complete_gives_up_after_http_retries():
    session = _FakeSession([
        _FakeResponse(status_code=500, text="boom"),
        _FakeResponse(status_code=500, text="boom"),
        _FakeResponse(status_code=500, text="boom"),
    ])
    client = ApiClient(
        ApiClientConfig(http_retries=3, retry_delay_seconds=0.0),
        session=session,
    )
    with pytest.raises(ApiClientError, match="after 3 attempts"):
        client.complete([{"role": "user", "content": "x"}], max_tokens=10)


def test_complete_grammar_reject_does_not_consume_retry_slot_with_http_retries_1():
    """Regression: with http_retries=1, the Gemma peg-gemma4 grammar-
    reject fallback must still take effect, because that fallback is a
    permanent client-level recovery, not a transient retry. The
    previous implementation used ``continue`` inside the same retry
    loop, which silently consumed the only attempt and surfaced a
    misleading ``API failed after 1 attempts: None`` error."""
    reject_text = "error: response does not match the expected peg-gemma4 format"
    session = _FakeSession([
        _FakeResponse(status_code=400, text=reject_text),
        _ok_text_response("ok"),
    ])
    client = ApiClient(
        ApiClientConfig(http_retries=1, retry_delay_seconds=0.0),
        session=session,
    )
    out = client.complete(
        [{"role": "user", "content": "x"}], max_tokens=10,
    )
    assert out == "ok"
    # First attempt: with response_format. Second attempt: without.
    # The second attempt must NOT have consumed a retry slot — the
    # fallback is free.
    assert "response_format" in session.posts[0]["json"]
    assert "response_format" not in session.posts[1]["json"]


def test_complete_retries_network_errors():
    from requests.exceptions import ConnectionError as RequestsConnectionError
    session = _FakeSession([
        RequestsConnectionError("reset"),
        _ok_text_response("ok"),
    ])
    client = ApiClient(
        ApiClientConfig(http_retries=3, retry_delay_seconds=0.0),
        session=session,
    )
    out = client.complete([{"role": "user", "content": "x"}], max_tokens=10)
    assert out == "ok"
    assert len(session.posts) == 2


def test_complete_raises_on_malformed_json_response():
    session = _FakeSession([_FakeResponse(status_code=200, text="not json", json_payload=None)])
    client = ApiClient(ApiClientConfig(), session=session)
    with pytest.raises(ApiClientError, match="non-JSON response body"):
        client.complete([{"role": "user", "content": "x"}], max_tokens=10)


def test_complete_raises_on_empty_messages():
    session = _FakeSession([])
    client = ApiClient(ApiClientConfig(), session=session)
    with pytest.raises(ApiClientError, match="empty messages"):
        client.complete([], max_tokens=10)


def test_complete_raises_on_malformed_choices_payload():
    session = _FakeSession([_FakeResponse(
        status_code=200, text="{}", json_payload={"choices": "not-a-list"},
    )])
    client = ApiClient(ApiClientConfig(), session=session)
    with pytest.raises(ApiClientError, match="Malformed API response"):
        client.complete([{"role": "user", "content": "x"}], max_tokens=10)
