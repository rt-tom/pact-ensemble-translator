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

    def close(self) -> None:
        pass


class _FakeStreamResponse:
    """Minimal SSE stand-in: ``iter_lines`` yields ``data: {...}`` lines."""

    def __init__(self, *, status_code: int, lines: List[str]):
        self.status_code = status_code
        self.reason = "OK" if 200 <= status_code < 300 else "Error"
        self.ok = 200 <= status_code < 300
        self.text = "\n".join(lines)
        self._lines = list(lines)

    def iter_lines(self, decode_unicode: bool = False):
        for line in self._lines:
            yield line

    def close(self) -> None:
        pass


class _FakeSession:
    """In-memory stand-in for ``requests.Session``.

    Each entry in ``script`` is one of:
      * a ``_FakeResponse`` — return immediately,
      * a ``_FakeStreamResponse`` — return immediately (SSE),
      * an ``Exception`` instance — raise it,
      * a tuple ``(status_code, text, json_payload)`` — return a
        ``_FakeResponse`` built from those.
    """

    def __init__(self, script: List[Any]):
        self._script = list(script)
        self.posts: List[Dict[str, Any]] = []

    def post(self, url: str, *, json: Dict[str, Any], timeout: float, stream: bool = False):
        self.posts.append({"url": url, "json": json, "timeout": timeout, "stream": stream})
        if not self._script:
            raise AssertionError("FakeSession: script exhausted")
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, (_FakeResponse, _FakeStreamResponse)):
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


# ---------------------------------------------------------------------------
# REASONING-STREAM: SSE streaming via on_reasoning_chunk
# ---------------------------------------------------------------------------


def _sse_lines(*chunks: str) -> List[str]:
    """llama-server-style SSE lines: reasoning deltas then content + [DONE]."""
    lines = []
    for rc in chunks:
        lines.append(
            "data: " + json.dumps({
                "choices": [{"delta": {"reasoning_content": rc}, "finish_reason": None}]
            })
        )
    lines.append(
        "data: " + json.dumps({
            "choices": [{"delta": {"content": "{\"ok\": true}"}, "finish_reason": None}]
        })
    )
    lines.append(
        "data: " + json.dumps({
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 5},
        })
    )
    lines.append("data: [DONE]")
    return lines


def test_complete_streams_reasoning_live_via_callback():
    """REASONING-STREAM acceptance: with on_reasoning_chunk the client uses
    the SSE transport and the callback receives each reasoning delta BEFORE
    complete() returns (the phase writer grows the file during the call)."""
    session = _FakeSession([
        _FakeStreamResponse(status_code=200, lines=_sse_lines("think ", "more"))
    ])
    client = ApiClient(ApiClientConfig(), session=session)
    received: List[str] = []

    out = client.complete(
        [{"role": "user", "content": "x"}],
        max_tokens=10,
        label="stream-test",
        on_reasoning_chunk=received.append,
    )

    assert out == '{"ok": true}'
    # Callback fired during the call, chunk-by-chunk.
    assert received == ["think ", "more"]
    record = client.calls[0]
    assert record.reasoning == "think more"
    assert record.streamed is True
    assert record.finish_reason == "stop"
    assert record.usage["completion_tokens"] == 5
    # The wire request really used stream=True.
    assert session.posts[0]["stream"] is True


def test_complete_streams_cyrillic_reasoning_with_embedded_newline():
    """SSE-fix (architect, book_run 2026-08-13): llama-server streams
    reasoning_content as raw UTF-8 (no charset) and the reasoning may embed
    real newlines that split one JSON event across physical lines. The old
    per-line json.loads treated each line as a complete event: Cyrillic
    decoded as Latin-1 became mojibake and an embedded newline broke JSON
    (malformed SSE -> batch fallback -> whole-chapter prompt re-processed).
    The buffer-based reader must reconstruct the split event, decode UTF-8
    correctly, and deliver the full reasoning to the sink."""
    # One JSON event physically split by a RAW newline inside reasoning:
    # llama-server emits data: {json} where reasoning_content contains real
    # line breaks, so iter_lines yields two physical lines for one event.
    event = (
        'data: {"choices": [{"delta": {"reasoning_content": "Думаю о '
        'переводе главы,'
    )
    event2 = 'перенося строку"}, "finish_reason": null}]}'
    lines = [
        event,
        event2,
        'data: {"choices": [{"delta": {"content": "{\\"ok\\": true}"}, "finish_reason": null}]}',
        'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}',
        "data: [DONE]",
    ]
    session = _FakeSession([
        _FakeStreamResponse(status_code=200, lines=lines)
    ])
    client = ApiClient(ApiClientConfig(), session=session)
    received: List[str] = []

    out = client.complete(
        [{"role": "user", "content": "x"}],
        max_tokens=10,
        label="stream-cyrillic",
        on_reasoning_chunk=received.append,
    )

    assert out == '{"ok": true}'
    assert received == ["Думаю о переводе главы,\nперенося строку"]
    record = client.calls[0]
    assert record.reasoning == "Думаю о переводе главы,\nперенося строку"
    assert record.streamed is True


def test_complete_without_callback_stays_batch():
    """No on_reasoning_chunk -> historical batch behaviour (stream=False),
    reasoning still captured from the batch message body."""
    session = _FakeSession([
        _ok_reasoning_response('{"ok": true}', "thinking...")
    ])
    client = ApiClient(ApiClientConfig(), session=session)
    out = client.complete(
        [{"role": "user", "content": "x"}], max_tokens=10, label="batch-test"
    )
    assert out == '{"ok": true}'
    assert session.posts[0]["stream"] is False
    assert client.calls[0].reasoning == "thinking..."
    assert client.calls[0].streamed is False


def test_complete_stream_falls_back_to_batch_on_http_error():
    """If the SSE stream fails (e.g. 500), the call falls back to the batch
    path and the callback receives the full reasoning once after completion
    (documented fallback)."""
    session = _FakeSession([
        _FakeResponse(status_code=500, text="boom"),
        _FakeResponse(status_code=500, text="boom"),
        _ok_reasoning_response('{"ok": true}', "batch-thinking"),
    ])
    client = ApiClient(
        ApiClientConfig(http_retries=2, retry_delay_seconds=0.0),
        session=session,
    )
    received: List[str] = []

    out = client.complete(
        [{"role": "user", "content": "x"}],
        max_tokens=10,
        on_reasoning_chunk=received.append,
    )

    assert out == '{"ok": true}'
    assert received == ["batch-thinking"]  # one post-completion delivery
    assert client.calls[0].streamed is False
    assert client.calls[0].reasoning == "batch-thinking"
    # First two POSTs were the stream attempts, third the batch fallback.
    assert session.posts[0]["stream"] is True
    assert session.posts[1]["stream"] is True
    assert session.posts[2]["stream"] is False


def test_complete_stream_falls_back_to_batch_on_non_sse_body():
    """A 200 response that is not SSE (no data: lines) must not silently
    produce empty text — fall back to the batch path."""
    session = _FakeSession([
        _FakeStreamResponse(status_code=200, lines=["not an sse body"]),
        _ok_reasoning_response('{"ok": true}', "batch-thinking"),
    ])
    client = ApiClient(ApiClientConfig(), session=session)
    received: List[str] = []

    out = client.complete(
        [{"role": "user", "content": "x"}],
        max_tokens=10,
        on_reasoning_chunk=received.append,
    )

    assert out == '{"ok": true}'
    assert received == ["batch-thinking"]
    assert client.calls[0].streamed is False


class _RollbackSink:
    """Callable reasoning sink that also supports transactional rollback.

    Mirrors the production phase writer (``open_reasoning_writer``): chunks
    are recorded on call; ``rollback()`` discards everything recorded so far
    (like truncating the reasoning file back to its pre-call state).
    """

    def __init__(self) -> None:
        self.received: List[str] = []
        self.rollbacks = 0

    def __call__(self, chunk: str) -> None:
        self.received.append(chunk)

    def rollback(self) -> None:
        self.rollbacks += 1
        self.received.clear()


def test_complete_truncated_stream_falls_back_to_batch():
    """Regression (RV t_df24524d HIGH + RV2 t_a7c14251 HIGH): an SSE stream
    that ends before [DONE] / terminal finish_reason is a truncated response
    — ApiClientError must be raised so complete() performs the batch
    fallback instead of returning a partial/empty streamed response as
    success. The fallback is transactional: the partial streamed delta is
    rolled back (sink.rollback()), so the sink receives the full batch
    reasoning EXACTLY ONCE — no partial+full duplicate artifact."""
    lines = [
        "data: " + json.dumps({
            "choices": [{"delta": {"reasoning_content": "partial "}, "finish_reason": None}]
        }),
        "data: " + json.dumps({
            "choices": [{"delta": {"content": "{\"partial\":"}, "finish_reason": None}]
        }),
        # stream dies here: no finish_reason chunk, no data: [DONE]
    ]
    session = _FakeSession([
        _FakeStreamResponse(status_code=200, lines=lines),
        _ok_reasoning_response('{"ok": true}', "batch-thinking"),
    ])
    client = ApiClient(ApiClientConfig(), session=session)
    sink = _RollbackSink()

    out = client.complete(
        [{"role": "user", "content": "x"}],
        max_tokens=10,
        on_reasoning_chunk=sink,
    )

    # Fallback happened: batch result wins, the sink got the batch reasoning
    # exactly once (the tentative "partial " delta was rolled back).
    assert out == '{"ok": true}'
    assert sink.received == ["batch-thinking"]
    assert sink.rollbacks == 1
    record = client.calls[0]
    assert record.streamed is False
    assert record.reasoning == "batch-thinking"
    assert record.finish_reason == "stop"
    # One stream POST (ApiClientError is not retried) + one batch POST.
    assert len(session.posts) == 2
    assert session.posts[0]["stream"] is True
    assert session.posts[1]["stream"] is False


def test_complete_truncated_stream_plain_sink_keeps_partial():
    """Documented limitation (RV2 t_a7c14251 HIGH): a sink WITHOUT a
    rollback() method cannot discard the tentative streamed delta — it keeps
    the partial chunk AND the full batch reasoning. The batch fallback still
    delivers the full reasoning once; only rollback-capable sinks (the
    production file writer) get the transactional exactly-once artifact."""
    lines = [
        "data: " + json.dumps({
            "choices": [{"delta": {"reasoning_content": "partial "}, "finish_reason": None}]
        }),
        # stream dies here
    ]
    session = _FakeSession([
        _FakeStreamResponse(status_code=200, lines=lines),
        _ok_reasoning_response('{"ok": true}', "batch-thinking"),
    ])
    client = ApiClient(ApiClientConfig(), session=session)
    received: List[str] = []

    out = client.complete(
        [{"role": "user", "content": "x"}],
        max_tokens=10,
        on_reasoning_chunk=received.append,
    )

    assert out == '{"ok": true}'
    assert received == ["partial ", "batch-thinking"]
    assert client.calls[0].streamed is False
    assert client.calls[0].reasoning == "batch-thinking"


def test_complete_midstream_retry_rolls_back_tentative_chunks():
    """Regression (RV2 t_a7c14251 HIGH): when a stream attempt fails
    mid-stream (connection drop) after delivering tentative reasoning
    chunks, the retry must start from a clean sink — the failed attempt's
    chunks are rolled back so only the retry's reasoning survives."""
    import requests as _requests

    class _DroppingStream(_FakeStreamResponse):
        def iter_lines(self, decode_unicode: bool = False):
            for i, line in enumerate(self._lines):
                if i == 1:
                    raise _requests.ConnectionError("connection dropped")
                yield line

    session = _FakeSession([
        _DroppingStream(status_code=200, lines=_sse_lines("tentative-", "garbage")),
        _FakeStreamResponse(status_code=200, lines=_sse_lines("clean-", "reasoning")),
    ])
    client = ApiClient(ApiClientConfig(retry_delay_seconds=0.0), session=session)
    sink = _RollbackSink()

    out = client.complete(
        [{"role": "user", "content": "x"}],
        max_tokens=10,
        on_reasoning_chunk=sink,
    )

    # First attempt streamed "tentative-" then dropped; rollback cleared it;
    # the retry's clean reasoning is the only content.
    assert out == '{"ok": true}'
    assert sink.received == ["clean-", "reasoning"]
    assert sink.rollbacks == 1
    record = client.calls[0]
    assert record.streamed is True
    assert record.reasoning == "clean-reasoning"
    assert len(session.posts) == 2


def test_complete_malformed_data_line_falls_back_to_batch():
    """Regression (RV t_df24524d HIGH): a data: line that is not valid JSON
    (e.g. ``data: not-json``) is a malformed SSE payload — ApiClientError
    must be raised so complete() falls back to the batch path instead of
    treating the stream as a successful empty response."""
    session = _FakeSession([
        _FakeStreamResponse(status_code=200, lines=["data: not-json"]),
        _ok_reasoning_response('{"ok": true}', "batch-thinking"),
    ])
    client = ApiClient(ApiClientConfig(), session=session)
    received: List[str] = []

    out = client.complete(
        [{"role": "user", "content": "x"}],
        max_tokens=10,
        on_reasoning_chunk=received.append,
    )

    assert out == '{"ok": true}'
    assert received == ["batch-thinking"]  # once, post-completion
    record = client.calls[0]
    assert record.streamed is False
    assert record.reasoning == "batch-thinking"
    assert record.finish_reason == "stop"
    assert len(session.posts) == 2
    assert session.posts[0]["stream"] is True
    assert session.posts[1]["stream"] is False


def test_consume_sse_raises_on_truncated_stream():
    """Direct contract: _consume_sse must raise ApiClientError for a stream
    that ends without a terminal marker, so complete() can do the fallback."""
    lines = [
        "data: " + json.dumps({
            "choices": [{"delta": {"content": "partial"}, "finish_reason": None}]
        }),
    ]
    client = ApiClient(ApiClientConfig())
    with pytest.raises(ApiClientError, match="truncated SSE stream"):
        client._consume_sse(
            _FakeStreamResponse(status_code=200, lines=lines),
            on_reasoning_chunk=lambda s: None,
            http_status=200,
            response_format_attempted=True,
            attempts=1,
        )


def test_consume_sse_raises_on_malformed_data_line():
    """Direct contract: a non-JSON data: payload must raise ApiClientError
    instead of being silently skipped (fail-closed)."""
    client = ApiClient(ApiClientConfig())
    with pytest.raises(ApiClientError, match="malformed SSE data payload"):
        client._consume_sse(
            _FakeStreamResponse(status_code=200, lines=["data: not-json"]),
            on_reasoning_chunk=lambda s: None,
            http_status=200,
            response_format_attempted=True,
            attempts=1,
        )


def test_complete_callback_exception_does_not_fail_stream():
    """Regression (RV t_df24524d MEDIUM): an exception raised inside
    on_reasoning_chunk is a best-effort sink failure — it must be logged and
    swallowed, NOT propagated (which would break the model call / look like
    a transport failure). The stream still completes, reasoning still
    accumulates into the call record, and no batch fallback happens."""
    session = _FakeSession([
        _FakeStreamResponse(status_code=200, lines=_sse_lines("think ", "more"))
    ])
    client = ApiClient(ApiClientConfig(), session=session)

    def _exploding_sink(chunk: str) -> None:
        raise OSError(f"sink disk failure on {chunk!r}")

    out = client.complete(
        [{"role": "user", "content": "x"}],
        max_tokens=10,
        label="sink-fail",
        on_reasoning_chunk=_exploding_sink,
    )

    assert out == '{"ok": true}'
    record = client.calls[0]
    assert record.streamed is True          # no false transport failure
    assert record.reasoning == "think more"  # accumulation stayed correct
    assert record.finish_reason == "stop"
    # Only the stream POST happened — no batch fallback, no exception escape.
    assert len(session.posts) == 1
    assert session.posts[0]["stream"] is True
