"""Tests for ``pact_v4.runtime.gemma_selector``.

The selector receives a list of ``(candidate_id, PID -> RU text)`` and
returns a ``GateResult`` whose ``detail`` carries the preferred
candidate_id. The library's cascade (``select_candidate``) treats an
empty or unknown ``detail`` as a "cannot choose" failure and quarantines
the chunk. The adapter's parser enforces the same rule.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import pytest

from pact_v4.phase1.models import GateResult
from pact_v4.runtime.api_client import ApiClient, ApiClientConfig, ApiClientError
from pact_v4.runtime.gemma_selector import (
    HttpGemmaSelector,
    HttpGemmaSelectorConfig,
    _parse_gemma_preference,
)


class _StubApiClient:
    def __init__(self, script: List[str]) -> None:
        self.script = list(script)
        self.calls: List[Dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "stub-gemma"

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
        on_reasoning_chunk=None,
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


# ---------------------------------------------------------------------------
# _parse_gemma_preference — strict shape handling
# ---------------------------------------------------------------------------


def test_parse_preference_with_valid_id_passes():
    raw = json.dumps({
        "preferred_candidate_id": "B",
        "reason": "B has more idiomatic Russian.",
    })
    result = _parse_gemma_preference(raw, valid_candidate_ids=["A", "B"])
    assert result.passed is True
    assert result.detail == "B"  # cascade reads this as the preferred id


def test_parse_preference_with_empty_id_fails():
    raw = json.dumps({"preferred_candidate_id": "", "reason": "tie"})
    result = _parse_gemma_preference(raw, valid_candidate_ids=["A", "B"])
    assert result.passed is False
    assert "no preference" in result.detail


def test_parse_preference_with_unknown_id_fails():
    raw = json.dumps({
        "preferred_candidate_id": "C",
        "reason": "wrong",
    })
    result = _parse_gemma_preference(raw, valid_candidate_ids=["A", "B"])
    assert result.passed is False
    assert "not in" in result.detail
    assert "C" in result.detail


def test_parse_preference_rejects_non_json():
    result = _parse_gemma_preference(
        "nope", valid_candidate_ids=["A", "B"],
    )
    assert result.passed is False
    assert "non-JSON" in result.detail


def test_parse_preference_rejects_non_object_json():
    result = _parse_gemma_preference(
        json.dumps([1, 2, 3]), valid_candidate_ids=["A", "B"],
    )
    assert result.passed is False
    assert "not a JSON object" in result.detail


# ---------------------------------------------------------------------------
# HttpGemmaSelector — wiring
# ---------------------------------------------------------------------------


def test_selector_renders_candidate_ids_and_russian_text():
    stub = _StubApiClient(script=[json.dumps({
        "preferred_candidate_id": "B",
        "reason": "B is more idiomatic.",
    })])
    selector = HttpGemmaSelector(api=stub)  # type: ignore[arg-type]
    result = selector([
        ("A", {"p00001": "Стюард открыл дверь."}),
        ("B", {"p00001": "Управляющий распахнул дверь."}),
    ])
    assert result.passed is True
    assert result.detail == "B"
    assert len(stub.calls) == 1
    prompt = stub.calls[0]["messages"][0]["content"]
    assert "candidate_id=A" in prompt
    assert "candidate_id=B" in prompt
    assert "Стюард открыл дверь." in prompt
    assert "Управляющий распахнул дверь." in prompt


def test_selector_fails_when_empty_candidate_set():
    selector = HttpGemmaSelector(api=_StubApiClient(script=[]))  # type: ignore[arg-type]
    result = selector([])
    assert result.passed is False
    assert "empty candidate set" in result.detail


def test_selector_surfaces_api_error_as_failed_gate():
    class _FailingStub(_StubApiClient):
        def complete(self, *args, **kwargs):  # type: ignore[override]
            raise ApiClientError("connection refused")

    selector = HttpGemmaSelector(api=_FailingStub(script=[]))  # type: ignore[arg-type]
    result = selector([("A", {"p1": "Привет."})])
    assert result.passed is False
    assert "API failure" in result.detail


def test_selector_rejects_unknown_preferred_id_from_model():
    stub = _StubApiClient(script=[json.dumps({
        "preferred_candidate_id": "Z",
        "reason": "model hallucinated",
    })])
    selector = HttpGemmaSelector(api=stub)  # type: ignore[arg-type]
    result = selector([("A", {"p1": "Привет."})])
    assert result.passed is False
    assert "Z" in result.detail


def test_selector_default_constructor_creates_real_api_client():
    selector = HttpGemmaSelector(config=HttpGemmaSelectorConfig())
    assert isinstance(selector.api, ApiClient)
