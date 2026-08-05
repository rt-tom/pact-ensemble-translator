"""Fake OpenCode server contract suite (plan §14.2, offline).

``OpenCodeServerBackend`` is exercised against the scriptable in-process
``FakeOpenCodeServer`` harness — no real network, no paid calls. Covers:
health success/version mismatch; provider/model missing; session
create/message/result; tools really disabled; JSON text response; structured
output response; malformed response; timeout/429/5xx/auth failure; retry
budget; failed session retention; cleanup of only own sessions.
"""
from __future__ import annotations

import json

import pytest
import requests

from pact_v4.runtime.backend_protocol import (
    JSON_OBJECT_SCHEMA,
    CompletionRequest,
    Message,
)
from pact_v4.runtime.opencode_backend import (
    ERROR_INVALID_MODEL_OUTPUT,
    ERROR_PROVIDER_5XX,
    ERROR_PROVIDER_AUTH,
    ERROR_PROVIDER_MODEL_UNAVAILABLE,
    ERROR_REMOTE_BUDGET_EXHAUSTED,
    ERROR_REQUEST_NOT_SUPPORTED,
    ERROR_SERVER_VERSION_UNSUPPORTED,
    ERROR_STRUCTURED_OUTPUT_FAILED,
    ERROR_TRANSPORT_NETWORK,
    ERROR_TRANSPORT_TIMEOUT,
    OpenCodeError,
    OpenCodeServerBackend,
    OpenCodeServerBackendConfig,
    RemoteBudget,
)
from tests.pact_v4.runtime.opencode_fake_server import (
    FakeOpenCodeServer,
    FakeResponse,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _request(**overrides) -> CompletionRequest:
    values = {
        "model_ref": "opencode-go/deepseek-v4-flash",
        "messages": (Message(role="user", content="Translate: Hello."),),
        "max_output_tokens": 8192,
        "temperature": 0.2,
        "response_schema": JSON_OBJECT_SCHEMA,
        "label": "unit",
        "request_options": {},
    }
    values.update(overrides)
    return CompletionRequest(**values)


def _text_message(text: str, **info_overrides) -> dict:
    info = {
        "id": "msg_fake",
        "role": "assistant",
        "providerID": "opencode-go",
        "modelID": "deepseek-v4-flash",
        "finish": "end_turn",
        "cost": 0.01,
        "tokens": {"input": 10, "output": 20, "reasoning": 0, "cache": {"read": 5, "write": 0}},
    }
    info.update(info_overrides)
    return {"info": info, "parts": [{"id": "p1", "type": "text", "text": text}]}


def _cfg(**overrides) -> OpenCodeServerBackendConfig:
    values = {
        "base_url": "http://127.0.0.1:4096",
        "model_bindings": {
            "generator": "opencode-go/deepseek-v4-flash",
            "fidelity_reviewer": "opencode-go/qwen3.7-plus",
            "russian_selector": "opencode-go/qwen3.7-plus",
        },
        "retry_delay_seconds": 0.0,
    }
    values.update(overrides)
    return OpenCodeServerBackendConfig(**values)


def _backend(fake: FakeOpenCodeServer, **cfg_overrides) -> OpenCodeServerBackend:
    return OpenCodeServerBackend(_cfg(**cfg_overrides), session=fake)


# ---------------------------------------------------------------------------
# Preflight: health + version
# ---------------------------------------------------------------------------


def test_health_success_preflights_and_completes():
    fake = FakeOpenCodeServer()
    fake.script_message(_text_message("hello"))
    backend = _backend(fake)
    assert backend.complete(_request()).text == "hello"
    # Preflight performed exactly one GET /global/health and one GET /provider.
    gets = [p for m, p, _ in fake.requests_log if m == "GET"]
    assert gets.count("/global/health") == 1
    assert gets.count("/provider") == 1


def test_version_mismatch_fails_preflight_with_specific_error():
    fake = FakeOpenCodeServer(version="1.5.0")
    backend = _backend(fake)
    with pytest.raises(OpenCodeError) as exc_info:
        backend.complete(_request())
    assert exc_info.value.error_class == ERROR_SERVER_VERSION_UNSUPPORTED
    assert "1.5.0" in str(exc_info.value)
    # No model call was made.
    posts = [p for m, p, _ in fake.requests_log if m == "POST"]
    assert posts == []


def test_exact_version_policy_accepts_pinned_rejects_patch():
    fake = FakeOpenCodeServer(version="1.4.7")
    fake.script_message(_text_message("ok"))
    backend = _backend(fake, server_version_policy="exact")
    assert backend.complete(_request()).text == "ok"

    fake2 = FakeOpenCodeServer(version="1.4.8")
    backend2 = _backend(fake2, server_version_policy="exact")
    with pytest.raises(OpenCodeError) as exc_info:
        backend2.complete(_request())
    assert exc_info.value.error_class == ERROR_SERVER_VERSION_UNSUPPORTED


def test_compatible_minor_accepts_patch_release():
    fake = FakeOpenCodeServer(version="1.4.8")
    fake.script_message(_text_message("ok"))
    backend = _backend(fake, server_version_policy="compatible_minor")
    assert backend.complete(_request()).text == "ok"


def test_unhealthy_server_fails_preflight():
    fake = FakeOpenCodeServer(healthy=False)
    backend = _backend(fake)
    with pytest.raises(OpenCodeError) as exc_info:
        backend.complete(_request())
    assert exc_info.value.error_class == ERROR_TRANSPORT_NETWORK


# ---------------------------------------------------------------------------
# Preflight: provider / model
# ---------------------------------------------------------------------------


def test_provider_not_connected_fails_loudly():
    fake = FakeOpenCodeServer(connected=["google"])
    backend = _backend(fake)
    with pytest.raises(OpenCodeError) as exc_info:
        backend.complete(_request())
    assert exc_info.value.error_class == ERROR_PROVIDER_MODEL_UNAVAILABLE
    assert "not connected" in str(exc_info.value)


def test_model_missing_fails_loudly():
    fake = FakeOpenCodeServer()
    backend = _backend(
        fake,
        model_bindings={"generator": "opencode-go/does-not-exist"},
    )
    with pytest.raises(OpenCodeError) as exc_info:
        backend.complete(_request(model_ref="opencode-go/does-not-exist"))
    assert exc_info.value.error_class == ERROR_PROVIDER_MODEL_UNAVAILABLE
    assert "does not exist" in str(exc_info.value)


def test_model_ref_without_slash_is_rejected():
    fake = FakeOpenCodeServer()
    backend = _backend(fake)
    with pytest.raises(Exception, match="provider/model"):
        backend.complete(_request(model_ref="bare-model"))


def test_preflight_is_cached_across_calls():
    fake = FakeOpenCodeServer()
    fake.script_message(_text_message("a"), _text_message("b"))
    backend = _backend(fake)
    backend.complete(_request(label="one"))
    backend.complete(_request(label="two"))
    gets = [p for m, p, _ in fake.requests_log if m == "GET"]
    assert gets.count("/global/health") == 1
    assert gets.count("/provider") == 1
    assert gets.count("/experimental/tool/ids") == 1


# ---------------------------------------------------------------------------
# Session / message lifecycle
# ---------------------------------------------------------------------------


def test_each_work_unit_gets_its_own_session():
    fake = FakeOpenCodeServer()
    fake.script_message(_text_message("a"), _text_message("b"))
    backend = _backend(fake)
    r1 = backend.complete(_request(label="one"))
    r2 = backend.complete(_request(label="two"))
    assert r1.session_id != r2.session_id
    assert r1.request_id and r2.request_id
    assert [r.label for r in backend.call_records()] == ["one", "two"]
    # Sessions are created with a Pact label title.
    titles = fake.created_session_titles()
    assert titles == ["pact-v4:one", "pact-v4:two"]


def test_message_body_carries_explicit_provider_model_and_system():
    fake = FakeOpenCodeServer()
    fake.script_message(_text_message("ok"))
    backend = _backend(fake, agent="pact-translator")
    backend.complete(_request(model_ref="opencode-go/deepseek-v4-flash"))
    body = fake.last_message_body()
    assert body["model"] == {"providerID": "opencode-go", "modelID": "deepseek-v4-flash"}
    assert body["agent"] == "pact-translator"
    assert "system" in body and body["system"]
    assert body["parts"] == [{"type": "text", "text": "Translate: Hello."}]


def test_tools_really_disabled():
    fake = FakeOpenCodeServer()
    fake.script_message(_text_message("ok"))
    backend = _backend(fake)
    backend.complete(_request())
    body = fake.last_message_body()
    tools = body["tools"]
    assert tools, "tools map must be present"
    assert all(value is False for value in tools.values())
    assert "bash" in tools and "read" in tools and "edit" in tools
    assert "grep" in tools and "webfetch" in tools and "task" in tools


def test_malformed_session_response_raises_transport_network():
    fake = FakeOpenCodeServer()
    fake.session_create_response = ["not-an-object"]
    backend = _backend(fake)
    with pytest.raises(OpenCodeError) as exc_info:
        backend.complete(_request())
    assert exc_info.value.error_class == ERROR_TRANSPORT_NETWORK
    assert "not an object" in str(exc_info.value)


def test_success_session_cleanup_when_retain_success_false():
    fake = FakeOpenCodeServer()
    fake.script_message(_text_message("ok"))
    backend = _backend(fake, retain_success_sessions=False)
    backend.complete(_request())
    deletes = [p for m, p, _ in fake.requests_log if m == "DELETE"]
    assert len(deletes) == 1
    assert fake.sessions == {}


def test_success_session_retained_when_retain_success_true():
    fake = FakeOpenCodeServer()
    fake.script_message(_text_message("ok"))
    backend = _backend(fake, retain_success_sessions=True)
    backend.complete(_request())
    deletes = [p for m, p, _ in fake.requests_log if m == "DELETE"]
    assert deletes == []
    assert len(fake.sessions) == 1


def test_failed_session_retained_when_retain_failed_true():
    fake = FakeOpenCodeServer()
    fake.script_message(requests.exceptions.ConnectionError("reset"))
    backend = _backend(fake, http_retries=0, retain_failed_sessions=True)
    with pytest.raises(OpenCodeError):
        backend.complete(_request())
    # The failed session stays on the server for diagnosis.
    assert len(fake.sessions) == 1


def test_failed_session_deleted_when_retain_failed_false():
    fake = FakeOpenCodeServer()
    fake.script_message(requests.exceptions.ConnectionError("reset"))
    backend = _backend(fake, http_retries=0, retain_failed_sessions=False)
    with pytest.raises(OpenCodeError):
        backend.complete(_request())
    assert fake.sessions == {}


def test_cleanup_only_own_sessions_on_close():
    fake = FakeOpenCodeServer()
    # A pre-existing foreign session must never be touched.
    fake.sessions["ses_foreign"] = {"id": "ses_foreign", "messages": []}
    fake.script_message(_text_message("ok"))
    backend = _backend(fake, retain_success_sessions=True)
    backend.complete(_request())
    backend.close()
    # Foreign session survives; the backend's own retained one is cleaned
    # only if policy allows (retain_success=True -> it stays).
    assert "ses_foreign" in fake.sessions
    assert len(fake.sessions) == 2


def test_close_is_idempotent_and_closes_session():
    fake = FakeOpenCodeServer()
    backend = _backend(fake)
    backend.close()
    assert fake.closed
    backend.close()  # must not raise
    assert fake.closed


def test_complete_after_close_fails():
    fake = FakeOpenCodeServer()
    backend = _backend(fake)
    backend.close()
    with pytest.raises(Exception, match="closed"):
        backend.complete(_request())


# ---------------------------------------------------------------------------
# JSON text response / usage normalization
# ---------------------------------------------------------------------------


def test_returns_normalized_text_usage_and_provenance():
    fake = FakeOpenCodeServer()
    fake.script_message(
        _text_message(
            '{"p1": "\u041f\u0440\u0438\u0432\u0435\u0442"}',
            id="msg_abc",
            finish="end_turn",
            cost=0.05,
            tokens={"input": 12, "output": 30, "reasoning": 0, "cache": {"read": 6, "write": 1}},
        )
    )
    backend = _backend(fake)
    response = backend.complete(_request())
    assert json.loads(response.text) == {"p1": "Привет"}
    assert response.finish_reason == "end_turn"
    assert response.usage["input_tokens"] == 12
    assert response.usage["output_tokens"] == 30
    assert response.usage["cached_input_tokens"] == 6
    assert response.usage["reported_cost"] == 0.05
    assert response.request_id == "msg_abc"
    assert response.retry_count == 0
    record = backend.call_records()[0]
    assert record.request_id == "msg_abc"
    assert record.finish_reason == "end_turn"
    assert record.usage["reported_cost"] == 0.05


def test_missing_usage_is_null_not_invented():
    fake = FakeOpenCodeServer()
    fake.script_message(_text_message("ok", cost=None, tokens=None))
    backend = _backend(fake)
    response = backend.complete(_request())
    assert response.usage == {}
    assert "reported_cost" not in response.usage


def test_multiple_text_parts_are_concatenated():
    fake = FakeOpenCodeServer()
    fake.script_message(
        {
            "info": {
                "id": "m1",
                "role": "assistant",
                "providerID": "opencode-go",
                "modelID": "deepseek-v4-flash",
                "tokens": {"input": 1, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            },
            "parts": [
                {"id": "a", "type": "text", "text": "part-one "},
                {"id": "b", "type": "text", "text": "part-two"},
                {"id": "c", "type": "text", "text": "synthetic-skip", "synthetic": True},
            ],
        }
    )
    backend = _backend(fake)
    assert backend.complete(_request()).text == "part-one part-two"


# ---------------------------------------------------------------------------
# Structured output
# ---------------------------------------------------------------------------


def test_structured_output_response_sets_structured_and_canonical_text():
    fake = FakeOpenCodeServer()
    fake.script_message(
        {
            "info": {
                "id": "m1",
                "role": "assistant",
                "providerID": "opencode-go",
                "modelID": "deepseek-v4-flash",
                "structured": {"p1": "Привет", "p2": "Мир"},
                "tokens": {"input": 1, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            },
            "parts": [{"id": "a", "type": "text", "text": "ignored body text"}],
        }
    )
    backend = _backend(fake, structured_output_mode="json_schema")
    response = backend.complete(_request())
    assert response.structured == {"p1": "Привет", "p2": "Мир"}
    assert json.loads(response.text) == {"p1": "Привет", "p2": "Мир"}
    assert response.text.startswith('{"p1"')


def test_structured_output_request_sends_format():
    fake = FakeOpenCodeServer()
    fake.script_message(
        {
            "info": {
                "id": "m1",
                "role": "assistant",
                "providerID": "opencode-go",
                "modelID": "deepseek-v4-flash",
                "structured": {"p1": "x"},
                "tokens": {"input": 1, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            },
            "parts": [],
        }
    )
    backend = _backend(fake, structured_output_mode="json_schema")
    backend.complete(_request())
    body = fake.last_message_body()
    assert body["format"]["type"] == "json_schema"
    assert body["format"]["schema"] == dict(JSON_OBJECT_SCHEMA)
    assert body["format"]["retryCount"] == 2


def test_structured_output_failure_is_bounded_retry_then_structured_error():
    fake = FakeOpenCodeServer()
    # First two attempts fail (StructuredOutputError), third succeeds.
    for _ in range(2):
        fake.script_message(
            {
                "info": {
                    "id": "m_err",
                    "role": "assistant",
                    "providerID": "opencode-go",
                    "modelID": "deepseek-v4-flash",
                    "error": {
                        "name": "StructuredOutputError",
                        "data": {"message": "Model did not produce structured output", "retries": 0},
                    },
                    "tokens": {"input": 1, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}},
                },
                "parts": [],
            }
        )
    fake.script_message(
        {
            "info": {
                "id": "m_ok",
                "role": "assistant",
                "providerID": "opencode-go",
                "modelID": "deepseek-v4-flash",
                "structured": {"p1": "Привет"},
                "tokens": {"input": 1, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            },
            "parts": [],
        }
    )
    backend = _backend(
        fake,
        structured_output_mode="json_schema",
        structured_output_retry_count=2,
    )
    response = backend.complete(_request())
    assert response.structured == {"p1": "Привет"}
    assert response.retry_count == 2
    assert len(fake.message_bodies()) == 3
    assert backend.call_records()[0].retry_count == 2


def test_structured_output_exhausted_raises_structured_failed():
    fake = FakeOpenCodeServer()
    for _ in range(3):
        fake.script_message(
            {
                "info": {
                    "id": "m_err",
                    "role": "assistant",
                    "providerID": "opencode-go",
                    "modelID": "deepseek-v4-flash",
                    "error": {
                        "name": "StructuredOutputError",
                        "data": {"message": "Model did not produce structured output", "retries": 0},
                    },
                    "tokens": {"input": 1, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}},
                },
                "parts": [],
            }
        )
    backend = _backend(
        fake,
        structured_output_mode="json_schema",
        structured_output_retry_count=2,
    )
    with pytest.raises(OpenCodeError) as exc_info:
        backend.complete(_request())
    assert exc_info.value.error_class == ERROR_STRUCTURED_OUTPUT_FAILED
    # Not a semantic-gate failure: it is a transport/schema error.
    assert exc_info.value.error_class != "semantic_gate_failed"


def test_prompt_only_mode_does_not_send_format():
    fake = FakeOpenCodeServer()
    fake.script_message(_text_message("plain text"))
    backend = _backend(fake, structured_output_mode="prompt_only")
    backend.complete(_request())
    assert "format" not in fake.last_message_body()


# ---------------------------------------------------------------------------
# Errors: timeout / network / 429 / 5xx / auth
# ---------------------------------------------------------------------------


def test_network_error_normalizes_and_bounded_retries():
    fake = FakeOpenCodeServer()
    fake.script_message(
        requests.exceptions.ConnectionError("connection reset"),
        requests.exceptions.ConnectionError("connection reset"),
        _text_message("recovered"),
    )
    backend = _backend(fake, http_retries=2, retry_delay_seconds=0.0)
    response = backend.complete(_request())
    assert response.text == "recovered"
    assert response.retry_count == 2
    assert len(fake.message_bodies()) == 3


def test_timeout_error_normalizes_as_transport_timeout():
    fake = FakeOpenCodeServer()
    fake.script_message(requests.exceptions.Timeout("timed out"))
    backend = _backend(fake, http_retries=0)
    with pytest.raises(OpenCodeError) as exc_info:
        backend.complete(_request())
    assert exc_info.value.error_class == ERROR_TRANSPORT_TIMEOUT


def test_429_retries_with_retry_after():
    fake = FakeOpenCodeServer()
    fake.script_message(
        FakeResponse(429, {"error": "rate limited"}, headers={"Retry-After": "1"}),
        _text_message("ok"),
    )
    backend = _backend(fake, http_retries=1, retry_delay_seconds=0.0)
    response = backend.complete(_request())
    assert response.text == "ok"
    assert response.retry_count == 1


def test_5xx_retries_bounded_then_fails():
    fake = FakeOpenCodeServer()
    for _ in range(3):
        fake.script_message(FakeResponse(503, {"error": "unavailable"}))
    backend = _backend(fake, http_retries=2, retry_delay_seconds=0.0)
    with pytest.raises(OpenCodeError) as exc_info:
        backend.complete(_request())
    assert exc_info.value.error_class == ERROR_PROVIDER_5XX
    assert len(fake.message_bodies()) == 3


def test_auth_failure_is_not_retried():
    fake = FakeOpenCodeServer()
    fake.script_message(FakeResponse(401, {"error": "unauthorized"}))
    backend = _backend(fake, http_retries=5)
    with pytest.raises(OpenCodeError) as exc_info:
        backend.complete(_request())
    assert exc_info.value.error_class == ERROR_PROVIDER_AUTH
    assert len(fake.message_bodies()) == 1


def test_403_failure_is_not_retried():
    fake = FakeOpenCodeServer()
    fake.script_message(FakeResponse(403, {"error": "forbidden"}))
    backend = _backend(fake, http_retries=5)
    with pytest.raises(OpenCodeError) as exc_info:
        backend.complete(_request())
    assert exc_info.value.error_class == ERROR_PROVIDER_AUTH
    assert len(fake.message_bodies()) == 1


def test_message_level_auth_error_normalizes():
    fake = FakeOpenCodeServer()
    fake.script_message(
        {
            "info": {
                "id": "m1",
                "role": "assistant",
                "providerID": "opencode-go",
                "modelID": "deepseek-v4-flash",
                "error": {
                    "name": "ProviderAuthError",
                    "data": {"providerID": "opencode-go", "message": "invalid key"},
                },
                "tokens": {"input": 1, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            },
            "parts": [],
        }
    )
    backend = _backend(fake)
    with pytest.raises(OpenCodeError) as exc_info:
        backend.complete(_request())
    assert exc_info.value.error_class == ERROR_PROVIDER_AUTH


def test_malformed_response_is_transport_network_not_retried_as_semantic():
    fake = FakeOpenCodeServer()
    fake.script_message(FakeResponse(200, {"unexpected": "shape"}))
    backend = _backend(fake, http_retries=0)
    with pytest.raises(OpenCodeError) as exc_info:
        backend.complete(_request())
    assert exc_info.value.error_class == ERROR_TRANSPORT_NETWORK
    assert "info" in str(exc_info.value)


def test_malformed_json_body_is_transport_network():
    fake = FakeOpenCodeServer()
    fake.script_message(FakeResponse(200, None, text="not-json{{"))
    backend = _backend(fake, http_retries=0)
    with pytest.raises(OpenCodeError) as exc_info:
        backend.complete(_request())
    assert exc_info.value.error_class == ERROR_TRANSPORT_NETWORK


def test_invalid_model_output_error_class_for_output_length():
    fake = FakeOpenCodeServer()
    fake.script_message(
        {
            "info": {
                "id": "m1",
                "role": "assistant",
                "providerID": "opencode-go",
                "modelID": "deepseek-v4-flash",
                "error": {
                    "name": "MessageOutputLengthError",
                    "data": {"message": "truncated"},
                },
                "tokens": {"input": 1, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            },
            "parts": [],
        }
    )
    backend = _backend(fake)
    with pytest.raises(OpenCodeError) as exc_info:
        backend.complete(_request())
    assert exc_info.value.error_class == ERROR_INVALID_MODEL_OUTPUT


# ---------------------------------------------------------------------------
# Retry budget
# ---------------------------------------------------------------------------


def test_retry_budget_limits_retries():
    fake = FakeOpenCodeServer()
    fake.script_message(
        requests.exceptions.ConnectionError("x"),
        requests.exceptions.ConnectionError("x"),
        requests.exceptions.ConnectionError("x"),
        _text_message("ok"),
    )
    backend = _backend(
        fake,
        http_retries=10,
        retry_delay_seconds=0.0,
        remote_budget=RemoteBudget(max_retry_requests_per_chapter=2),
    )
    # Retry budget of 2 allows exactly 2 retries; the 3rd attempt is refused.
    with pytest.raises(OpenCodeError) as exc_info:
        backend.complete(_request())
    assert exc_info.value.error_class == ERROR_REMOTE_BUDGET_EXHAUSTED


def test_request_budget_limits_total_calls():
    fake = FakeOpenCodeServer()
    fake.script_message(_text_message("ok"))
    backend = _backend(
        fake,
        http_retries=0,
        remote_budget=RemoteBudget(max_requests_per_chapter=1),
    )
    backend.complete(_request(label="one"))
    with pytest.raises(OpenCodeError) as exc_info:
        backend.complete(_request(label="two"))
    assert exc_info.value.error_class == ERROR_REMOTE_BUDGET_EXHAUSTED


def test_rate_limit_wait_budget_respected():
    fake = FakeOpenCodeServer()
    fake.script_message(FakeResponse(429, {"error": "rl"}, headers={"Retry-After": "1"}), _text_message("ok"))
    backend = _backend(
        fake,
        http_retries=1,
        retry_delay_seconds=0.0,
        remote_budget=RemoteBudget(max_wait_seconds_on_rate_limit=0.0),
    )
    with pytest.raises(OpenCodeError) as exc_info:
        backend.complete(_request())
    assert exc_info.value.error_class == ERROR_REMOTE_BUDGET_EXHAUSTED


def test_default_remote_budget_500_covers_full_chapter_cycle():
    # B11: the default per-chapter remote budget was raised 250 -> 500 so a
    # complete chapter cycle fits with reserve. run_003_remote exhausted 250
    # at the end of the cycle (convergence re-audit, repair 183 debt,
    # formatting 102 incidents, quarantined retry all went to debt); a full
    # cycle needs ~350-400. Estimate: 16 chunks x (2 generation + 2
    # fidelity + ~0.5 selection) + Step 6 audit 2x16 + convergence + repair
    # + formatting + quarantined retry + retry reserve. The default must
    # cover that estimate.
    budget = RemoteBudget()
    assert budget.max_requests_per_chapter == 500
    chunks = 16
    generation_per_chunk = 2
    fidelity_per_chunk = 2
    selection_per_chunk = 0.5
    audit_units = 2 * chunks  # Qwen + Gemma per chunk
    convergence_reserve = 2 * chunks  # re-audit of unedited PIDs
    repair_reserve = 16
    formatting_reserve = 16
    retry_reserve = 16
    estimate = (
        chunks * (generation_per_chunk + fidelity_per_chunk + selection_per_chunk)
        + audit_units + convergence_reserve + repair_reserve
        + formatting_reserve + retry_reserve
    )
    assert budget.max_requests_per_chapter >= estimate


def test_default_remote_budget_part_of_backend_identity():
    # B10/B11: remote_budget is identity-bound (build_opencode_descriptor),
    # so a config with the raised default carries max_requests_per_chapter=500
    # in its identity payload.
    cfg = _cfg()
    from pact_v4.runtime.opencode_backend import build_opencode_descriptor

    desc = build_opencode_descriptor(cfg)
    assert desc.effective_options["remote_budget"]["max_requests_per_chapter"] == 500


# ---------------------------------------------------------------------------
# request_options / identity
# ---------------------------------------------------------------------------


def test_request_options_are_rejected_not_silently_dropped():
    fake = FakeOpenCodeServer()
    fake.script_message(_text_message("ok"))
    backend = _backend(fake)
    with pytest.raises(OpenCodeError) as exc_info:
        backend.complete(_request(request_options={"top_p": 0.9}))
    assert exc_info.value.error_class == ERROR_REQUEST_NOT_SUPPORTED
    assert "not supported" in str(exc_info.value)


def test_json_schema_mode_requires_response_schema():
    fake = FakeOpenCodeServer()
    backend = _backend(fake, structured_output_mode="json_schema")
    with pytest.raises(OpenCodeError) as exc_info:
        backend.complete(_request(response_schema=None))
    assert exc_info.value.error_class == ERROR_REQUEST_NOT_SUPPORTED
    assert "response_schema" in str(exc_info.value)
    # No network call was made.
    assert [p for m, p, _ in fake.requests_log if m != "GET"] == []


def test_request_model_ref_outside_bindings_fails_loudly():
    fake = FakeOpenCodeServer()
    backend = _backend(fake, model_bindings={"generator": "opencode-go/deepseek-v4-flash"})
    with pytest.raises(OpenCodeError) as exc_info:
        backend.complete(_request(model_ref="opencode-go/qwen3.7-plus"))
    assert exc_info.value.error_class == ERROR_REQUEST_NOT_SUPPORTED
    assert "not bound" in str(exc_info.value)


def test_descriptor_identity_excludes_credentials_and_is_deterministic():
    fake = FakeOpenCodeServer()
    a = _backend(fake, username="user", password="hunter2")
    b = _backend(fake, username="user", password="hunter2")
    assert a.descriptor.identity_hash == b.descriptor.identity_hash

    with_other_password = _backend(fake, username="user", password="different")
    # Credential rotation must not change identity (plan §11).
    assert a.descriptor.identity_hash == with_other_password.descriptor.identity_hash

    record = a.descriptor.public_record()
    text = repr(record)
    assert "hunter2" not in text
    assert "password" not in text
    assert record["kind"] == "opencode_server"
    assert record["endpoint_family"] == "opencode_http"


def test_descriptor_identity_changes_with_model_binding():
    fake = FakeOpenCodeServer()
    a = _backend(fake, model_bindings={"generator": "opencode-go/deepseek-v4-flash"})
    b = _backend(fake, model_bindings={"generator": "opencode-go/qwen3.7-plus"})
    assert a.descriptor.identity_hash != b.descriptor.identity_hash


def test_descriptor_identity_changes_with_structured_output_mode():
    fake = FakeOpenCodeServer()
    a = _backend(fake, structured_output_mode="prompt_only")
    b = _backend(fake, structured_output_mode="json_schema")
    assert a.descriptor.identity_hash != b.descriptor.identity_hash


def test_descriptor_identity_changes_with_temperature():
    # Plan §5.4: sampling fields belong in identity even when the wire
    # contract cannot send them (B1).
    fake = FakeOpenCodeServer()
    a = _backend(fake, default_temperature=0.0)
    b = _backend(fake, default_temperature=0.2)
    assert a.descriptor.identity_hash != b.descriptor.identity_hash


def test_descriptor_identity_changes_with_max_output_tokens():
    fake = FakeOpenCodeServer()
    a = _backend(fake, default_max_output_tokens=512)
    b = _backend(fake, default_max_output_tokens=8192)
    assert a.descriptor.identity_hash != b.descriptor.identity_hash


def test_descriptor_identity_stable_when_sampling_unset():
    fake = FakeOpenCodeServer()
    a = _backend(fake)
    b = _backend(fake)
    assert a.descriptor.identity_hash == b.descriptor.identity_hash


def test_descriptor_identity_changes_with_agent_and_system():
    fake = FakeOpenCodeServer()
    a = _backend(fake, agent="pact-translator")
    b = _backend(fake, agent=None)
    assert a.descriptor.identity_hash != b.descriptor.identity_hash

    c = _backend(fake, system_prompt="different neutral prompt")
    assert a.descriptor.identity_hash != c.descriptor.identity_hash


def test_public_record_never_contains_basic_auth():
    fake = FakeOpenCodeServer()
    backend = _backend(fake, username="opencode", password="secretpw")
    assert backend._auth() == ("opencode", "secretpw")
    record = repr(backend.descriptor.public_record())
    assert "secretpw" not in record
    assert "secret" not in record
