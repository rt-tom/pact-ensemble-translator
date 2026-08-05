"""Tests for the backend-neutral role adapters.

A fake ``CompletionBackend`` (in-memory, no HTTP) supplies scripted
``CompletionResponse`` objects; the adapters must render the same prompts,
send the same requests, and parse results identically to the previous
``Http*`` adapters.
"""
from __future__ import annotations

import json
from typing import Mapping, Optional, Sequence, Tuple

import pytest

from pact_v4.phase1.models import GateResult
from pact_v4.phase2.generation import GenerationParams, PromptBundle
from pact_v4.phase2.prompts import FIDELITY_FIRST_V1, render_prompt
from pact_v4.runtime.backend_protocol import (
    BackendCallRecord,
    BackendDescriptor,
    CompletionBackend,
    CompletionError,
    CompletionRequest,
    CompletionResponse,
    Message,
)
from pact_v4.runtime.backend_role_adapters import (
    BackendGemmaAuditEvaluator,
    BackendGemmaAuditEvaluatorConfig,
    BackendGemmaSelector,
    BackendGemmaSelectorConfig,
    BackendModelCaller,
    BackendModelCallerConfig,
    BackendQwenAuditEvaluator,
    BackendQwenAuditEvaluatorConfig,
    BackendQwenEvaluator,
    BackendQwenEvaluatorConfig,
    BackendRegionFidelityGate,
    BackendRegionFidelityGateConfig,
    BackendRepairCaller,
    BackendRepairCallerConfig,
)
from pact_v4.runtime.json_resilience import (
    EmptyResponseError,
    JsonRetryPolicy,
    TruncatedJSONError,
)
from pact_v4.runtime.prompts_runtime import (
    render_gemma_audit_prompt,
    render_gemma_preference_prompt,
    render_qwen_audit_prompt,
    render_qwen_review_prompt,
)


def _hash(seed: str) -> str:
    from pact_v4.phase1.models import canonical_json_hash

    return canonical_json_hash({"seed": seed})


class ScriptedBackend:
    """In-memory ``CompletionBackend`` returning scripted responses."""

    _DEFAULT_BINDINGS = {
        "default": "gemma-4-26B",
        "generator": "gemma-4-26B",
        "fidelity_reviewer": "qwen-3",
        "russian_selector": "gemma-4-26B",
        "qwen_audit": "qwen-3",
        "gemma_audit": "gemma-4-26B",
    }

    def __init__(
        self,
        script: Sequence[CompletionResponse],
        model_bindings: Optional[Mapping[str, str]] = None,
    ):
        self._script = list(script)
        self._model_bindings = model_bindings if model_bindings is not None else self._DEFAULT_BINDINGS
        self.requests: list[CompletionRequest] = []
        self._closed = False

    @property
    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(
            kind="local_llama",
            transport_version="openai-chat-completions/v1",
            endpoint_family="openai_chat_completions",
            public_endpoint="http://127.0.0.1:8080/v1/chat/completions",
            model_bindings=self._model_bindings,
            effective_options={"temperature": 0.0},
        )

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        if not self._script:
            raise AssertionError("ScriptedBackend: script exhausted")
        return self._script.pop(0)

    def close(self) -> None:
        self._closed = True

    def call_records(self) -> Sequence[BackendCallRecord]:
        return []


def _text_response(text: str) -> CompletionResponse:
    return CompletionResponse(text=text, model="gemma-4-26B")


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
# BackendModelCaller
# ---------------------------------------------------------------------------


def test_model_caller_returns_text_and_sends_rendered_prompt():
    canned = json.dumps({"p00001": "Один.", "p00002": "Два."})
    backend = ScriptedBackend([_text_response(canned)])
    caller = BackendModelCaller(backend)
    out = caller(_bundle())
    assert json.loads(out) == {"p00001": "Один.", "p00002": "Два."}
    assert len(backend.requests) == 1
    request = backend.requests[0]
    assert len(request.messages) == 1
    assert request.messages[0].role == "user"
    # Rendered prompt must equal what render_prompt(bundle) produces.
    assert request.messages[0].content == render_prompt(_bundle())
    assert request.temperature == pytest.approx(0.2)
    assert request.label == "phase2b/fidelity_first/chunk0001"
    assert request.response_schema is not None


def test_model_caller_propagates_completion_error():
    class _FailingBackend(ScriptedBackend):
        def complete(self, request):
            raise CompletionError("network unreachable")

    caller = BackendModelCaller(_FailingBackend([]))
    with pytest.raises(CompletionError, match="network unreachable"):
        caller(_bundle())


def test_model_caller_uses_role_model_ref_from_descriptor():
    backend = ScriptedBackend([_text_response("{}")])
    caller = BackendModelCaller(backend)
    caller(_bundle())
    assert backend.requests[0].model_ref == "gemma-4-26B"


def test_model_caller_fails_loudly_on_missing_model_binding():
    # A role without a model binding must not silently fall back to the
    # transport's model: the request construction rejects it.
    backend = ScriptedBackend([_text_response("{}")], model_bindings={})
    caller = BackendModelCaller(backend)
    with pytest.raises(ValueError, match="no model binding"):
        caller(_bundle())
    assert backend.requests == []


# ---------------------------------------------------------------------------
# BackendQwenEvaluator
# ---------------------------------------------------------------------------


def _pass_verdict() -> str:
    return json.dumps({
        "faithful_to_source": True,
        "completeness": True,
        "introduced_errors": False,
        "confidence": "high",
        "reason": "All PIDs translated faithfully.",
        "passed": True,
    })


def test_qwen_evaluator_parses_verdict_and_sends_review_prompt():
    backend = ScriptedBackend([_text_response(_pass_verdict())])
    evaluator = BackendQwenEvaluator(backend)
    source = {"p00001": "Hello, world.", "p00002": "Goodbye."}
    translation = {"p00001": "Привет, мир.", "p00002": "Прощай."}
    result = evaluator(source, translation)
    assert result.passed is True
    assert result.gate == "qwen_fidelity"
    request = backend.requests[0]
    assert request.messages[0].content == render_qwen_review_prompt(
        source=source, translation=translation
    )
    assert request.temperature == 0.0
    assert request.label == "phase2c/qwen_fidelity"
    # max_tokens scales with chunk size on top of the floor.
    assert request.max_output_tokens >= 16384


def test_qwen_evaluator_returns_failed_gate_on_completion_error():
    class _FailingBackend(ScriptedBackend):
        def complete(self, request):
            raise CompletionError("connection refused")

    evaluator = BackendQwenEvaluator(_FailingBackend([]))
    result = evaluator({"p1": "Hi."}, {"p1": "Привет."})
    assert isinstance(result, GateResult)
    assert result.passed is False
    assert "API failure" in result.detail


# ---------------------------------------------------------------------------
# BackendGemmaSelector
# ---------------------------------------------------------------------------


def test_gemma_selector_parses_preference_and_sends_prompt():
    canned = json.dumps({"preferred_candidate_id": "B", "reason": "more idiomatic"})
    backend = ScriptedBackend([_text_response(canned)])
    selector = BackendGemmaSelector(backend)
    candidates = [
        ("A", {"p00001": "Стюард открыл дверь."}),
        ("B", {"p00001": "Управляющий распахнул дверь."}),
    ]
    result = selector(candidates)
    assert result.passed is True
    assert result.detail == "B"
    request = backend.requests[0]
    assert request.messages[0].content == render_gemma_preference_prompt(candidates=candidates)
    assert request.temperature == 0.0
    assert request.label == "phase2c/gemma_russian_preference"


def test_gemma_selector_fails_on_empty_candidate_set_without_calling_backend():
    backend = ScriptedBackend([])
    selector = BackendGemmaSelector(backend)
    result = selector([])
    assert result.passed is False
    assert "empty candidate set" in result.detail
    assert backend.requests == []


def test_gemma_selector_returns_failed_gate_on_completion_error():
    class _FailingBackend(ScriptedBackend):
        def complete(self, request):
            raise CompletionError("connection refused")

    selector = BackendGemmaSelector(_FailingBackend([]))
    result = selector([("A", {"p1": "Привет."})])
    assert result.passed is False
    assert "API failure" in result.detail


# ---------------------------------------------------------------------------
# BackendQwenAuditEvaluator (Phase 3B Step 6)
# ---------------------------------------------------------------------------


def _audit_source() -> dict:
    return {"p00001": "The steward opened the door.", "p00002": "Do not forget."}


def _audit_translation() -> dict:
    return {"p00001": "Стюард открыл дверь.", "p00002": "Не забудь."}


def test_qwen_audit_evaluator_sends_rendered_prompt_and_returns_raw_text():
    canned = json.dumps({"issues": []}, ensure_ascii=False)
    backend = ScriptedBackend([_text_response(canned)])
    evaluator = BackendQwenAuditEvaluator(backend)
    out = evaluator(
        chunk_id="chunk0001", source=_audit_source(), translation=_audit_translation()
    )
    assert out == canned
    assert len(backend.requests) == 1
    request = backend.requests[0]
    assert len(request.messages) == 1
    assert request.messages[0].content == render_qwen_audit_prompt(
        chunk_id="chunk0001", source=_audit_source(), translation=_audit_translation()
    )
    assert request.model_ref == "qwen-3"
    assert request.temperature == 0.0
    assert request.label == "phase3/qwen_chapter_audit"
    assert request.response_schema is not None


def test_qwen_audit_evaluator_uses_max_tokens_floor_with_per_pid_headroom():
    # The Qwen max_tokens fix (PR #96) applies to the audit too: the floor
    # is 16384 and max_tokens scales with chunk size on top of it, capped at
    # MAX_TOKENS_CEILING -- a large chunk's audit response must not truncate.
    canned = json.dumps({"issues": []}, ensure_ascii=False)
    backend = ScriptedBackend([_text_response(canned)])
    evaluator = BackendQwenAuditEvaluator(backend)
    evaluator(chunk_id="c", source=_audit_source(), translation=_audit_translation())
    request = backend.requests[0]
    assert request.max_output_tokens >= 16384
    # 2 PIDs * 128 headroom added to the 16384 floor.
    assert request.max_output_tokens == 16384 + 128 * 2


def test_qwen_audit_evaluator_retries_truncated_json_then_succeeds():
    # B4 (JSON resilience): a truncated JSON body is *not* returned raw for
    # the audit layer to fail on — the adapter retries (bounded, no backoff
    # here) by re-issuing the identical request and returns the valid body
    # from the second attempt.
    truncated = '{"issues": [{"pid": "p00001", "category": "omission", "note": "x'
    canned = json.dumps({"issues": []}, ensure_ascii=False)
    backend = ScriptedBackend([_text_response(truncated), _text_response(canned)])
    evaluator = BackendQwenAuditEvaluator(
        backend,
        config=BackendQwenAuditEvaluatorConfig(
            retry=JsonRetryPolicy(max_retries=1, base_delay_seconds=0.0)
        ),
    )
    out = evaluator(chunk_id="c", source=_audit_source(), translation=_audit_translation())
    assert out == canned
    # Retry re-issued the exact same request (identity unchanged).
    assert len(backend.requests) == 2
    assert backend.requests[0] == backend.requests[1]


def test_qwen_audit_evaluator_raises_after_truncated_retries_exhausted():
    # B4: when the bounded retry budget is exhausted the adapter re-raises
    # TruncatedJSONError — run_chapter_audit records that unit as failed
    # (resumable), never as "no issues" and never as a semantic verdict.
    truncated = '{"issues": [{"pid": "p00001", "category": "omission", "note": "x'
    backend = ScriptedBackend([_text_response(truncated)])
    evaluator = BackendQwenAuditEvaluator(
        backend,
        config=BackendQwenAuditEvaluatorConfig(
            retry=JsonRetryPolicy(max_retries=0, base_delay_seconds=0.0)
        ),
    )
    with pytest.raises(TruncatedJSONError):
        evaluator(chunk_id="c", source=_audit_source(), translation=_audit_translation())
    assert len(backend.requests) == 1


def test_qwen_audit_evaluator_retries_empty_response_then_succeeds():
    # B4: an empty response (the run_001 qwen-audit failure mode) is retried
    # with the identical request; the second attempt's body is returned.
    canned = json.dumps({"issues": []}, ensure_ascii=False)
    backend = ScriptedBackend([_text_response(""), _text_response(canned)])
    evaluator = BackendQwenAuditEvaluator(
        backend,
        config=BackendQwenAuditEvaluatorConfig(
            retry=JsonRetryPolicy(max_retries=1, base_delay_seconds=0.0)
        ),
    )
    out = evaluator(chunk_id="c", source=_audit_source(), translation=_audit_translation())
    assert out == canned
    assert len(backend.requests) == 2
    assert backend.requests[0] == backend.requests[1]


def test_qwen_audit_evaluator_raises_after_empty_retries_exhausted():
    backend = ScriptedBackend([_text_response("")])
    evaluator = BackendQwenAuditEvaluator(
        backend,
        config=BackendQwenAuditEvaluatorConfig(
            retry=JsonRetryPolicy(max_retries=0, base_delay_seconds=0.0)
        ),
    )
    with pytest.raises(EmptyResponseError):
        evaluator(chunk_id="c", source=_audit_source(), translation=_audit_translation())
    assert len(backend.requests) == 1


def test_qwen_audit_evaluator_does_not_retry_transport_failure_as_json_error():
    # B4 §3: a transport failure (CompletionError / C1 OpenCodeError) is a
    # separate error class and must NOT be retried by the JSON retry — it is
    # the transport's own bounded-retry domain.
    attempts = []

    class _FailingBackend(ScriptedBackend):
        def complete(self, request):
            attempts.append(request)
            raise CompletionError("connection refused")

    evaluator = BackendQwenAuditEvaluator(
        _FailingBackend([]),
        config=BackendQwenAuditEvaluatorConfig(
            retry=JsonRetryPolicy(max_retries=3, base_delay_seconds=0.0)
        ),
    )
    with pytest.raises(CompletionError, match="connection refused"):
        evaluator(chunk_id="c", source=_audit_source(), translation=_audit_translation())
    assert len(attempts) == 1


def test_qwen_audit_evaluator_propagates_completion_error():
    class _FailingBackend(ScriptedBackend):
        def complete(self, request):
            raise CompletionError("connection refused")

    evaluator = BackendQwenAuditEvaluator(_FailingBackend([]))
    with pytest.raises(CompletionError, match="connection refused"):
        evaluator(chunk_id="c", source=_audit_source(), translation=_audit_translation())


# ---------------------------------------------------------------------------
# BackendGemmaAuditEvaluator (Phase 3B Step 6)
# ---------------------------------------------------------------------------


def test_gemma_audit_evaluator_sends_rendered_prompt_and_returns_raw_text():
    canned = json.dumps({"issues": []}, ensure_ascii=False)
    backend = ScriptedBackend([_text_response(canned)])
    evaluator = BackendGemmaAuditEvaluator(backend)
    out = evaluator(chunk_id="chunk0001", translation=_audit_translation())
    assert out == canned
    request = backend.requests[0]
    assert request.messages[0].content == render_gemma_audit_prompt(
        chunk_id="chunk0001", translation=_audit_translation()
    )
    assert request.model_ref == "gemma-4-26B"
    assert request.temperature == 0.0
    assert request.label == "phase3/gemma_russian_review"


def test_gemma_audit_evaluator_never_receives_source():
    # Spec: "Russian-only review без оригинала". The request prompt must not
    # carry the source PID map, and the adapter takes no source argument.
    canned = json.dumps({"issues": []}, ensure_ascii=False)
    backend = ScriptedBackend([_text_response(canned)])
    evaluator = BackendGemmaAuditEvaluator(backend)
    evaluator(chunk_id="c", translation=_audit_translation())
    content = backend.requests[0].messages[0].content
    assert "SOURCE (PID -> English text)" not in content
    for pid in _audit_source():
        # The translation PID map only carries p00001/p00002 anyway, but the
        # source section is what must be absent.
        assert f"{pid}: {_audit_source()[pid]}" not in content


def test_gemma_audit_evaluator_propagates_completion_error():
    class _FailingBackend(ScriptedBackend):
        def complete(self, request):
            raise CompletionError("connection refused")

    evaluator = BackendGemmaAuditEvaluator(_FailingBackend([]))
    with pytest.raises(CompletionError, match="connection refused"):
        evaluator(chunk_id="c", translation=_audit_translation())


# ---------------------------------------------------------------------------
# BackendRepairCaller (Phase 4A region repair; B4 JSON retry)
# ---------------------------------------------------------------------------


def _repair_kwargs():
    from pact_v4.phase1.models import Region

    return dict(
        chunk_id="chunk0001",
        source=_audit_source(),
        translation=_audit_translation(),
        region=Region(pid="p00001", start=0, end=10),
        findings=[{"category": "omission", "note": "dropped clause"}],
    )


def _repair_ok() -> str:
    return json.dumps(
        {"repaired": {"p00001": "Исправленный перевод."}, "reason": "scripted"},
        ensure_ascii=False,
    )


def test_repair_caller_retries_truncated_json_then_succeeds():
    # B4: a truncated repair JSON body is retried (bounded) with the identical
    # request and the second attempt's body is returned — the repair layer
    # then commits normally instead of recording a spurious debt.
    truncated = '{"repaired": {"p00001": "Исправленный пере'
    backend = ScriptedBackend([_text_response(truncated), _text_response(_repair_ok())])
    caller = BackendRepairCaller(
        backend,
        config=BackendRepairCallerConfig(
            retry=JsonRetryPolicy(max_retries=1, base_delay_seconds=0.0)
        ),
    )
    out = caller(**_repair_kwargs())
    assert out == _repair_ok()
    assert len(backend.requests) == 2
    assert backend.requests[0] == backend.requests[1]


def test_repair_caller_raises_after_truncated_retries_exhausted():
    # B4: exhausted budget re-raises TruncatedJSONError — repair_region /
    # _apply_region_edit converts it into a non-committed (debt) outcome,
    # never a semantic terminal status.
    truncated = '{"repaired": {"p00001": "Исправленный пере'
    backend = ScriptedBackend([_text_response(truncated)])
    caller = BackendRepairCaller(
        backend,
        config=BackendRepairCallerConfig(
            retry=JsonRetryPolicy(max_retries=0, base_delay_seconds=0.0)
        ),
    )
    with pytest.raises(TruncatedJSONError):
        caller(**_repair_kwargs())
    assert len(backend.requests) == 1


def test_repair_caller_does_not_retry_transport_failure_as_json_error():
    attempts = []

    class _FailingBackend(ScriptedBackend):
        def complete(self, request):
            attempts.append(request)
            raise CompletionError("connection refused")

    caller = BackendRepairCaller(
        _FailingBackend([]),
        config=BackendRepairCallerConfig(
            retry=JsonRetryPolicy(max_retries=3, base_delay_seconds=0.0)
        ),
    )
    with pytest.raises(CompletionError, match="connection refused"):
        caller(**_repair_kwargs())
    assert len(attempts) == 1


# ---------------------------------------------------------------------------
# B10: B4 JSON-resilience extended to ALL role adapters
# (generation, Qwen fidelity, Gemma selector, Gemma audit, region gate).
# Empty/truncated JSON is retried with the identical request; transport
# failures are never retried; exhaustion re-raises the last error — never a
# semantic verdict.
# ---------------------------------------------------------------------------


def _no_backoff() -> JsonRetryPolicy:
    return JsonRetryPolicy(max_retries=2, base_delay_seconds=0.0)


def _empty_then(ok_text: str):
    return ScriptedBackend([_text_response(""), _text_response(ok_text)])


# --- BackendModelCaller (Phase 2B generation) -----------------------------


def test_model_caller_retries_empty_response_then_succeeds():
    canned = json.dumps({"p00001": "Один.", "p00002": "Два."})
    backend = _empty_then(canned)
    caller = BackendModelCaller(
        backend, config=BackendModelCallerConfig(retry=_no_backoff()),
    )
    out = caller(_bundle())
    assert json.loads(out) == {"p00001": "Один.", "p00002": "Два."}
    # Empty body on the first call -> retry -> success on the second.
    assert len(backend.requests) == 2
    assert backend.requests[0] == backend.requests[1]


def test_model_caller_raises_after_empty_retries_exhausted():
    backend = ScriptedBackend([_text_response("")] * 3)
    caller = BackendModelCaller(
        backend, config=BackendModelCallerConfig(retry=_no_backoff()),
    )
    with pytest.raises(EmptyResponseError):
        caller(_bundle())
    assert len(backend.requests) == 3


def test_model_caller_does_not_retry_transport_failure():
    attempts = []

    class _FailingBackend(ScriptedBackend):
        def complete(self, request):
            attempts.append(request)
            raise CompletionError("connection refused")

    caller = BackendModelCaller(
        _FailingBackend([]), config=BackendModelCallerConfig(retry=_no_backoff()),
    )
    with pytest.raises(CompletionError, match="connection refused"):
        caller(_bundle())
    assert len(attempts) == 1


# --- BackendQwenEvaluator (Phase 2C fidelity gate) ------------------------


def test_qwen_evaluator_retries_empty_response_then_succeeds():
    backend = _empty_then(_pass_verdict())
    evaluator = BackendQwenEvaluator(
        backend, config=BackendQwenEvaluatorConfig(retry=_no_backoff()),
    )
    result = evaluator({"p1": "Hi."}, {"p1": "Привет."})
    assert result.passed is True
    assert len(backend.requests) == 2
    assert backend.requests[0] == backend.requests[1]


def test_qwen_evaluator_raises_after_empty_retries_exhausted():
    backend = ScriptedBackend([_text_response("")] * 3)
    evaluator = BackendQwenEvaluator(
        backend, config=BackendQwenEvaluatorConfig(retry=_no_backoff()),
    )
    with pytest.raises(EmptyResponseError):
        evaluator({"p1": "Hi."}, {"p1": "Привет."})
    assert len(backend.requests) == 3


def test_qwen_evaluator_does_not_retry_transport_failure():
    attempts = []

    class _FailingBackend(ScriptedBackend):
        def complete(self, request):
            attempts.append(request)
            raise CompletionError("connection refused")

    evaluator = BackendQwenEvaluator(
        _FailingBackend([]), config=BackendQwenEvaluatorConfig(retry=_no_backoff()),
    )
    result = evaluator({"p1": "Hi."}, {"p1": "Привет."})
    assert isinstance(result, GateResult)
    assert result.passed is False
    assert "API failure" in result.detail
    assert len(attempts) == 1


# --- BackendGemmaSelector (Phase 2C Russian preference) -------------------


def _preference_ok() -> str:
    return json.dumps({"preferred_candidate_id": "B", "reason": "more idiomatic"})


def test_gemma_selector_retries_empty_response_then_succeeds():
    backend = _empty_then(_preference_ok())
    selector = BackendGemmaSelector(
        backend, config=BackendGemmaSelectorConfig(retry=_no_backoff()),
    )
    candidates = [
        ("A", {"p00001": "Стюард открыл дверь."}),
        ("B", {"p00001": "Управляющий распахнул дверь."}),
    ]
    result = selector(candidates)
    assert result.passed is True
    assert result.detail == "B"
    assert len(backend.requests) == 2
    assert backend.requests[0] == backend.requests[1]


def test_gemma_selector_raises_after_empty_retries_exhausted():
    backend = ScriptedBackend([_text_response("")] * 3)
    selector = BackendGemmaSelector(
        backend, config=BackendGemmaSelectorConfig(retry=_no_backoff()),
    )
    with pytest.raises(EmptyResponseError):
        selector([("A", {"p1": "Привет."})])
    assert len(backend.requests) == 3


def test_gemma_selector_does_not_retry_transport_failure():
    attempts = []

    class _FailingBackend(ScriptedBackend):
        def complete(self, request):
            attempts.append(request)
            raise CompletionError("connection refused")

    selector = BackendGemmaSelector(
        _FailingBackend([]), config=BackendGemmaSelectorConfig(retry=_no_backoff()),
    )
    result = selector([("A", {"p1": "Привет."})])
    assert isinstance(result, GateResult)
    assert result.passed is False
    assert "API failure" in result.detail
    assert len(attempts) == 1


# --- BackendGemmaAuditEvaluator (Step 6 Gemma audit) ----------------------


def test_gemma_audit_retries_empty_response_then_succeeds():
    canned = json.dumps({"issues": []}, ensure_ascii=False)
    backend = _empty_then(canned)
    evaluator = BackendGemmaAuditEvaluator(
        backend, config=BackendGemmaAuditEvaluatorConfig(retry=_no_backoff()),
    )
    out = evaluator(chunk_id="c", translation=_audit_translation())
    assert out == canned
    assert len(backend.requests) == 2
    assert backend.requests[0] == backend.requests[1]


def test_gemma_audit_raises_after_empty_retries_exhausted():
    backend = ScriptedBackend([_text_response("")] * 3)
    evaluator = BackendGemmaAuditEvaluator(
        backend, config=BackendGemmaAuditEvaluatorConfig(retry=_no_backoff()),
    )
    with pytest.raises(EmptyResponseError):
        evaluator(chunk_id="c", translation=_audit_translation())
    assert len(backend.requests) == 3


def test_gemma_audit_does_not_retry_transport_failure():
    attempts = []

    class _FailingBackend(ScriptedBackend):
        def complete(self, request):
            attempts.append(request)
            raise CompletionError("connection refused")

    evaluator = BackendGemmaAuditEvaluator(
        _FailingBackend([]), config=BackendGemmaAuditEvaluatorConfig(retry=_no_backoff()),
    )
    with pytest.raises(CompletionError, match="connection refused"):
        evaluator(chunk_id="c", translation=_audit_translation())
    assert len(attempts) == 1


# --- BackendRegionFidelityGate (Phase 4A L2b narrow re-gate) --------------


def _region_gate_kwargs():
    from pact_v4.phase1.models import Region

    return dict(
        source_text="Hello, world.",
        repaired_text="Здравствуй, мир.",
        region=Region(pid="p1", start=0, end=6),
    )


def test_region_fidelity_gate_retries_empty_response_then_succeeds():
    backend = _empty_then(_pass_verdict())
    gate = BackendRegionFidelityGate(
        backend, config=BackendRegionFidelityGateConfig(retry=_no_backoff()),
    )
    result = gate(**_region_gate_kwargs())
    assert result.passed is True
    assert len(backend.requests) == 2
    assert backend.requests[0] == backend.requests[1]


def test_region_fidelity_gate_raises_after_empty_retries_exhausted():
    backend = ScriptedBackend([_text_response("")] * 3)
    gate = BackendRegionFidelityGate(
        backend, config=BackendRegionFidelityGateConfig(retry=_no_backoff()),
    )
    with pytest.raises(EmptyResponseError):
        gate(**_region_gate_kwargs())
    assert len(backend.requests) == 3


def test_region_fidelity_gate_does_not_retry_transport_failure():
    attempts = []

    class _FailingBackend(ScriptedBackend):
        def complete(self, request):
            attempts.append(request)
            raise CompletionError("connection refused")

    gate = BackendRegionFidelityGate(
        _FailingBackend([]), config=BackendRegionFidelityGateConfig(retry=_no_backoff()),
    )
    result = gate(**_region_gate_kwargs())
    assert isinstance(result, GateResult)
    assert result.passed is False
    assert "API failure" in result.detail
    assert len(attempts) == 1


# --- GenerationCache interaction (B10: retry must not break cache.put) ----


def test_generation_cache_put_after_retried_success():
    """B10: a generation that succeeds only after a JSON retry still writes
    the candidate into the GenerationCache (cache.put happens on success,
    never skipped because of the retry)."""
    from tests.pact_v4.phase2.test_generation import (
        RiskBand,
        make_env,
        make_params,
        make_risk,
        valid_output_for,
    )

    source, snapshot, chunk_plan, chunk, config = make_env(pid_count=2)
    canned = valid_output_for(chunk)
    backend = _empty_then(canned)
    caller = BackendModelCaller(
        backend, config=BackendModelCallerConfig(retry=_no_backoff()),
    )
    from pact_v4.phase2.generation import GenerationCache, generate_for_chunk

    cache = GenerationCache()
    kwargs = dict(
        chunk_id=chunk.chunk_id,
        risk=make_risk(RiskBand.LOW),
        source=source,
        snapshot=snapshot,
        chunk_plan=chunk_plan,
        config=config,
        params=make_params(),
        model_caller=caller,
        cache=cache,
    )
    outcome = generate_for_chunk(**kwargs)
    assert outcome.status == "complete"
    assert len(backend.requests) == 2  # empty -> retry -> success
    # The retried success was cached exactly like a first-try success: the
    # second identical call is served from the cache (no third backend call).
    outcome2 = generate_for_chunk(**kwargs)
    assert outcome2.status == "complete"
    assert len(backend.requests) == 2
