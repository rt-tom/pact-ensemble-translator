"""Tests for ``pact_v4.runtime.model_caller``.

The ``HttpModelCaller`` is the production-flavoured ``ModelCaller``
(Phase 2B). Tests verify it against a fake ``ApiClient`` so the run is
fully offline: the wiring to the real ``llama-server`` lives in
``api_client``, which has its own dedicated test file.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from pact_v4.phase1.models import canonical_json_hash
from pact_v4.phase2.generation import GenerationParams, PromptBundle
from pact_v4.phase2.prompts import FIDELITY_FIRST_V1
from pact_v4.runtime.api_client import ApiClient, ApiClientConfig
from pact_v4.runtime.model_caller import HttpModelCaller, HttpModelCallerConfig


# ---------------------------------------------------------------------------
# Stub ApiClient (no real HTTP)
# ---------------------------------------------------------------------------


class _StubApiClient:
    """A drop-in stub that records every call and returns scripted text."""

    def __init__(self, script: List[str]) -> None:
        self.script = list(script)
        self.calls: List[Dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "stub-api"

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
            "response_format_json": response_format_json,
            "label": label,
        })
        if not self.script:
            raise AssertionError("StubApiClient: script exhausted")
        return self.script.pop(0)


# ---------------------------------------------------------------------------
# Bundle fixture
# ---------------------------------------------------------------------------


def _hash(seed: str) -> str:
    return canonical_json_hash({"seed": seed})


def _bundle() -> PromptBundle:
    return PromptBundle(
        template=FIDELITY_FIRST_V1,
        role="fidelity_first",
        risk_band="low",
        risk_policy_version="pact-v4-risk-source-en/v1",
        required_risk_feature_codes=(),
        snapshot_hash=_hash("snap"),
        source_hash=_hash("source"),
        chunk_id="chunk0001",
        owned_pids=("p00001", "p00002"),
        owned_source=(
            ("p00001", "First English sentence."),
            ("p00002", "Second English sentence."),
        ),
        left_context=(),
        right_context=(),
        glossary=(("Alice", "Алиса"),),
        style_constraints=(),
        bible_text="",
        config_identity=_hash("config"),
        params=GenerationParams(temperature=0.2, seed=7, max_tokens=512),
    )


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------


def test_http_model_caller_returns_model_text_unchanged():
    stub = _StubApiClient(script=[json.dumps({"p00001": "Один.", "p00002": "Два."})])
    caller = HttpModelCaller(api=stub)  # type: ignore[arg-type]
    out = caller(_bundle())
    assert json.loads(out) == {"p00001": "Один.", "p00002": "Два."}


def test_http_model_caller_sends_single_user_message_with_rendered_prompt():
    stub = _StubApiClient(script=["{}"])
    caller = HttpModelCaller(api=stub)  # type: ignore[arg-type]
    caller(_bundle())
    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert len(call["messages"]) == 1
    msg = call["messages"][0]
    assert msg["role"] == "user"
    # The rendered prompt must include the template instructions,
    # the owned PIDs, and their English text.
    assert "translate" in msg["content"].casefold()
    assert "p00001" in msg["content"]
    assert "First English sentence." in msg["content"]
    assert "p00002" in msg["content"]


def test_http_model_caller_propagates_temperature_from_bundle():
    stub = _StubApiClient(script=["{}"])
    caller = HttpModelCaller(api=stub)  # type: ignore[arg-type]
    caller(_bundle())
    assert stub.calls[0]["temperature"] == pytest.approx(0.2)


def test_http_model_caller_label_identifies_chunk_and_role():
    stub = _StubApiClient(script=["{}"])
    caller = HttpModelCaller(api=stub)  # type: ignore[arg-type]
    caller(_bundle())
    label = stub.calls[0]["label"]
    assert "phase2b" in label
    assert "fidelity_first" in label
    assert "chunk0001" in label


def test_http_model_caller_propagates_apiclient_errors():
    from pact_v4.runtime.api_client import ApiClientError

    class _FailingStub(_StubApiClient):
        def complete(self, *args, **kwargs):  # type: ignore[override]
            raise ApiClientError("network unreachable")

    caller = HttpModelCaller(api=_FailingStub(script=[]))  # type: ignore[arg-type]
    with pytest.raises(ApiClientError, match="network unreachable"):
        caller(_bundle())


def test_http_model_caller_default_constructor_creates_real_api_client():
    """When neither ``api`` nor ``config`` is supplied, the default
    constructor still wires a real ``ApiClient`` (whose HTTP is unused
    in this test) — this is the contract a production caller relies on
    when only a config dict is available."""
    caller = HttpModelCaller(config=HttpModelCallerConfig())
    assert isinstance(caller.api, ApiClient)
