"""Tests for ``pact_v4.runtime.qwen_evaluator``.

The HTTP wiring is in ``api_client``; the Qwen adapter only renders the
review prompt, sends it via the injected ``ApiClient``, and parses the
JSON verdict into a ``GateResult``. The stub ``ApiClient`` records the
prompt text and returns a scripted JSON reply.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from pact_v4.phase1.models import GateResult
from pact_v4.runtime.api_client import ApiClient, ApiClientConfig, ApiClientError
from pact_v4.runtime.qwen_evaluator import (
    HttpQwenEvaluator,
    HttpQwenEvaluatorConfig,
    _parse_qwen_verdict,
)


class _StubApiClient:
    def __init__(self, script: List[str]) -> None:
        self.script = list(script)
        self.calls: List[Dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "stub-qwen"

    @property
    def config(self) -> ApiClientConfig:
        return ApiClientConfig()

    def complete(
        self,
        messages: List[Dict[str, str]],
        *,
        max_tokens: int,
        temperature: Optional[float] = None,
        response_format_json: bool = True,
        label: str = "stub",
    ) -> str:
        self.calls.append({
            "messages": list(messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "label": label,
        })
        if not self.script:
            raise AssertionError("StubApiClient: script exhausted")
        return self.script.pop(0)


def _pass_verdict() -> str:
    return json.dumps({
        "faithful_to_source": True,
        "completeness": True,
        "introduced_errors": False,
        "confidence": "high",
        "reason": "All PIDs translated faithfully.",
        "passed": True,
    })


# ---------------------------------------------------------------------------
# _parse_qwen_verdict — strict JSON shape handling
# ---------------------------------------------------------------------------


def test_parse_qwen_verdict_pass():
    raw = json.dumps({
        "faithful_to_source": True,
        "completeness": True,
        "introduced_errors": False,
        "confidence": "high",
        "reason": "OK",
        "passed": True,
    })
    result = _parse_qwen_verdict(raw)
    assert result.gate == "qwen_fidelity"
    assert result.passed is True
    assert "faithful=True" in result.detail
    assert "confidence=high" in result.detail


def test_parse_qwen_verdict_implied_passed_when_key_missing():
    """The protocol says 'passed' is implied or explicit. When omitted,
    it must default to (faithful AND complete AND NOT introduced_errors),
    which here is True only because all three sub-fields are True."""
    raw = json.dumps({
        "faithful_to_source": True,
        "completeness": True,
        "introduced_errors": False,
        "confidence": "medium",
        "reason": "OK",
    })
    result = _parse_qwen_verdict(raw)
    assert result.passed is True


def test_parse_qwen_verdict_implied_passed_defaults_to_false_on_error():
    raw = json.dumps({
        "faithful_to_source": True,
        "completeness": True,
        "introduced_errors": True,
        "confidence": "low",
        "reason": "Numbers were dropped.",
    })
    result = _parse_qwen_verdict(raw)
    assert result.passed is False
    assert "low" in result.detail


def test_parse_qwen_verdict_invalid_confidence_fails():
    raw = json.dumps({
        "faithful_to_source": True,
        "completeness": True,
        "introduced_errors": False,
        "confidence": "very high",  # not in the allowed set
        "reason": "OK",
        "passed": True,
    })
    result = _parse_qwen_verdict(raw)
    assert result.passed is False
    assert "invalid confidence" in result.detail


def test_parse_qwen_verdict_rejects_non_json():
    result = _parse_qwen_verdict("not json at all")
    assert result.passed is False
    assert "non-JSON" in result.detail


def test_parse_qwen_verdict_rejects_non_object_json():
    result = _parse_qwen_verdict(json.dumps([1, 2, 3]))
    assert result.passed is False
    assert "not a JSON object" in result.detail


# ---------------------------------------------------------------------------
# HttpQwenEvaluator — wiring
# ---------------------------------------------------------------------------


def test_evaluator_sends_single_user_message_with_source_and_translation():
    stub = _StubApiClient(script=[_pass_verdict()])
    evaluator = HttpQwenEvaluator(api=stub)  # type: ignore[arg-type]
    result = evaluator(
        source={"p00001": "Hello, world.", "p00002": "Goodbye."},
        translation={"p00001": "Привет, мир.", "p00002": "Прощай."},
    )
    assert result.passed is True
    assert len(stub.calls) == 1
    prompt = stub.calls[0]["messages"][0]["content"]
    assert "Hello, world." in prompt
    assert "Goodbye." in prompt
    assert "Привет, мир." in prompt
    assert "Прощай." in prompt


def test_evaluator_returns_failed_gate_on_api_error():
    class _FailingStub(_StubApiClient):
        def complete(self, *args, **kwargs):  # type: ignore[override]
            raise ApiClientError("connection refused")

    evaluator = HttpQwenEvaluator(api=_FailingStub(script=[]))  # type: ignore[arg-type]
    result = evaluator(
        source={"p1": "Hi."}, translation={"p1": "Привет."},
    )
    assert isinstance(result, GateResult)
    assert result.gate == "qwen_fidelity"
    assert result.passed is False
    assert "API failure" in result.detail


def test_evaluator_raises_after_malformed_reply_retries_exhausted():
    # B10: a non-JSON (unparseable) reply is now a JSON-resilience retry
    # trigger (TruncatedJSONError) instead of an immediate failed gate.
    # With the default JsonRetryPolicy(max_retries=2) the adapter re-issues
    # the identical request up to 3 times; when the budget is exhausted the
    # last error is re-raised (never a semantic verdict).
    stub = _StubApiClient(script=["not a json object"] * 3)
    evaluator = HttpQwenEvaluator(api=stub)  # type: ignore[arg-type]
    from pact_v4.runtime.json_resilience import TruncatedJSONError

    with pytest.raises(TruncatedJSONError):
        evaluator(
            source={"p1": "Hi."}, translation={"p1": "Привет."},
        )
    assert len(stub.calls) == 3
    # The retried requests are identical (identity unchanged by a retry).
    assert stub.calls[0]["messages"] == stub.calls[1]["messages"] == stub.calls[2]["messages"]


def test_evaluator_default_constructor_creates_real_api_client():
    evaluator = HttpQwenEvaluator(config=HttpQwenEvaluatorConfig())
    assert isinstance(evaluator.api, ApiClient)
