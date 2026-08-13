"""Regression tests for the lifecycle-aware caller wrappers' reasoning reset.

GEN-REASONING (RV t_a790dbab): ``LifecycleModelCaller.__call__`` must clear
the per-attempt reasoning diagnostic BEFORE ``ensure_resident``. When model
load/swap raises ``CompletionError``, the wrapped ``HttpModelCaller`` /
``BackendModelCaller`` is never entered, so its clear-at-start reset cannot
run — without the explicit attempt-boundary reset here, ``last_reasoning``
would keep the previous successful completion's reasoning and the
whole-chapter reasoning sink would attribute old text to the aborted
attempt.
"""
from __future__ import annotations

import json
from typing import Optional, Sequence

import pytest

from pact_v4.phase1.models import canonical_json_hash
from pact_v4.phase2.generation import GenerationParams, PromptBundle
from pact_v4.phase2.prompts import FIDELITY_FIRST_V1
from pact_v4.runtime.backend_protocol import (
    BackendCallRecord,
    BackendDescriptor,
    CompletionBackend,
    CompletionError,
    CompletionRequest,
    CompletionResponse,
)
from pact_v4.runtime.model_lifecycle_adapters import LifecycleModelCaller


class _FailingRouter:
    """ModelRouter stand-in whose acquisition always raises CompletionError."""

    base_url = "http://router.invalid"

    def ensure_resident(self, model_key: str):
        raise CompletionError(f"{model_key} load failed (simulated)")


class _ScriptedBackend:
    """In-memory ``CompletionBackend`` for one successful call."""

    def __init__(self, response: CompletionResponse) -> None:
        self._response = response
        self.requests: list[CompletionRequest] = []

    @property
    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(
            kind="local_llama",
            transport_version="openai-chat-completions/v1",
            endpoint_family="openai_chat_completions",
            public_endpoint="http://router.invalid/v1/chat/completions",
            model_bindings={"default": "gemma-4-26B", "generator": "gemma-4-26B"},
            effective_options={"temperature": 0.0},
        )

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        return self._response

    def close(self) -> None:
        pass

    def call_records(self) -> Sequence[BackendCallRecord]:
        return []


def _bundle() -> PromptBundle:
    return PromptBundle(
        template=FIDELITY_FIRST_V1,
        role="fidelity_first",
        risk_band="low",
        risk_policy_version="pact-v4-risk-source-en/v1",
        required_risk_feature_codes=(),
        snapshot_hash=canonical_json_hash({"seed": "snap"}),
        source_hash=canonical_json_hash({"seed": "source"}),
        chunk_id="chunk0001",
        owned_pids=("p00001", "p00002"),
        owned_source=(
            ("p00001", "First English sentence."),
            ("p00002", "Second English sentence."),
        ),
        left_context=(),
        right_context=(),
        glossary=(),
        style_constraints=(),
        bible_text="",
        config_identity=canonical_json_hash({"seed": "config"}),
        params=GenerationParams(temperature=0.2, seed=7, max_tokens=512),
    )


def test_lifecycle_model_caller_clears_reasoning_before_ensure_resident_failure():
    # GEN-REASONING regression (RV t_a790dbab): a prior successful
    # completion left reasoning behind; the next call's ensure_resident
    # raises CompletionError. The aborted attempt must expose '' — never
    # the previous successful completion's reasoning.
    caller = LifecycleModelCaller(_FailingRouter(), model_name="gemma-4-26B")
    # Simulate a prior successful completion that populated last_reasoning.
    inner = caller._caller._impl  # BackendModelCaller behind the wrapper
    inner._last_reasoning = "STALE prior reasoning"
    assert caller.last_reasoning == "STALE prior reasoning"

    with pytest.raises(CompletionError, match="load failed"):
        caller(_bundle())

    # The abort attempt exposed/emits empty reasoning, not the stale text.
    assert caller.last_reasoning == ""


def test_lifecycle_model_caller_keeps_forwarding_reasoning_after_success():
    # The attempt-boundary reset must not break the happy path: a completed
    # call still forwards the captured reasoning through the wrapper.
    canned = json.dumps({"p00001": "Один.", "p00002": "Два."}, ensure_ascii=False)
    response = CompletionResponse(
        text=canned, model="gemma-4-26B",
        raw_metadata={"reasoning": "обдумал род и регистр"},
    )

    class _WorkingRouter:
        base_url = "http://router.invalid"

        def ensure_resident(self, model_key: str):
            return None

    caller = LifecycleModelCaller(_WorkingRouter(), model_name="gemma-4-26B")
    # Swap the real HTTP backend for the in-memory scripted one so the
    # call completes without a server.
    inner = caller._caller._impl
    inner._backend = _ScriptedBackend(response)

    out = caller(_bundle())
    assert json.loads(out) == {"p00001": "Один.", "p00002": "Два."}
    assert caller.last_reasoning == "обдумал род и регистр"
