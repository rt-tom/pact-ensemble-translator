"""B4 JSON-resilience tests: empty/truncated-JSON retry for qwen-audit & repair.

Covers the B4 acceptance criteria (``docs/plans/V4_B4_JSON_RESILIENCE_TASK_RU.md``):

  * unit: empty response -> retry -> success; empty response -> retry
    exhausted -> failed unit; truncated JSON -> retry -> success; truncated
    JSON -> retry exhausted -> debt;
  * integration: a fake ``CompletionBackend`` returns empty/truncated JSON on
    the first call and valid JSON on the second — Step 6 audit / Step 7
    repair pass;
  * transport failure is a separate error class and is never retried as a
    JSON error;
  * a retry re-issues the identical request, so identity (prompt/backend,
    unit hash, backend identity) is unchanged.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional, Sequence

import pytest

from pact_v4.runtime.backend_protocol import (
    BackendDescriptor,
    CompletionError,
    CompletionRequest,
    CompletionResponse,
)
from pact_v4.runtime.backend_role_adapters import (
    BackendQwenAuditEvaluator,
    BackendQwenAuditEvaluatorConfig,
    BackendRepairCaller,
    BackendRepairCallerConfig,
)
from pact_v4.runtime.json_resilience import (
    EmptyResponseError,
    JsonRetryPolicy,
    TruncatedJSONError,
    classify_response_text,
    parse_json_response,
    retry_json_call,
)
from tests.pact_v4.phase3.test_audit import _env as _audit_env
from tests.pact_v4.phase4.test_repair import (
    _env as _repair_env,
)
from tests.pact_v4.phase4.test_repair import (
    _finding,
    _run_single_repair,
)
from tests.pact_v4.phase4.test_repair import (
    ScriptedGemmaAudit,
    ScriptedQwenGate,
)


def _descriptor() -> BackendDescriptor:
    return BackendDescriptor(
        kind="local_llama",
        transport_version="openai-chat-completions/v1",
        endpoint_family="openai_chat_completions",
        public_endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model_bindings={
            "default": "gemma-4-26B",
            "generator": "gemma-4-26B",
            "qwen_audit": "qwen-3",
        },
        effective_options={"temperature": 0.0},
    )


class ScriptedBackend:
    """In-memory ``CompletionBackend`` returning scripted responses."""

    def __init__(self, script: Sequence[CompletionResponse]) -> None:
        self._script = list(script)
        self.requests: list[CompletionRequest] = []

    @property
    def descriptor(self) -> BackendDescriptor:
        return _descriptor()

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        if not self._script:
            raise AssertionError("ScriptedBackend: script exhausted")
        return self._script.pop(0)

    def close(self) -> None:
        pass

    def call_records(self) -> Sequence[Any]:
        return []


def _text_response(text: str) -> CompletionResponse:
    return CompletionResponse(text=text, model="qwen-3")


def _retry(max_retries: int = 2) -> JsonRetryPolicy:
    return JsonRetryPolicy(max_retries=max_retries, base_delay_seconds=0.0)


# ---------------------------------------------------------------------------
# classify_response_text / JsonRetryPolicy
# ---------------------------------------------------------------------------


def test_classify_response_text_rejects_empty_body():
    with pytest.raises(EmptyResponseError):
        classify_response_text("")
    with pytest.raises(EmptyResponseError):
        classify_response_text("   \n ")


def test_classify_response_text_rejects_truncated_json():
    with pytest.raises(TruncatedJSONError):
        classify_response_text('{"issues": [{"pid": "p00001"')


def test_classify_response_text_accepts_any_parseable_json():
    assert classify_response_text(json.dumps({"issues": []})) is None
    # A parseable-but-wrong-shape body is NOT a retry trigger: downstream
    # validation rejects it (B4: retry only empty/truncated JSON).
    assert classify_response_text('"just a string"') is None


# ---------------------------------------------------------------------------
# parse_json_response (RESILIENCE t_406fc48c: fences/BOM/prose tolerance in
# R / repair / re-audit / entity extraction)
# ---------------------------------------------------------------------------


def test_parse_json_response_plain_dict():
    assert parse_json_response('{"edits": []}') == {"edits": []}


def test_parse_json_response_strips_markdown_fences():
    good = '{"edits": []}'
    assert parse_json_response(f"```json\n{good}\n```") == {"edits": []}
    assert parse_json_response(f"```\n{good}\n```") == {"edits": []}
    # Fence with BOM and extra whitespace.
    assert parse_json_response(f"\ufeff  ```json\n{good}\n```  ") == {"edits": []}


def test_parse_json_response_extracts_first_balanced_block_from_prose():
    # Models wrap the payload in prose ('Here is the JSON: {...}').
    assert parse_json_response('Here is the JSON: {"edits": []}') == {"edits": []}
    assert parse_json_response('Sure! {"edits": []} Hope that helps.') == {"edits": []}
    # Braces inside a JSON string must not unbalance the block.
    payload = '{"note": "brace { inside string", "edits": []}'
    assert parse_json_response(f"prefix {payload} suffix") == json.loads(payload)


def test_parse_json_response_rejects_truncated_json_retry_zone():
    # Broken/truncated JSON is NOT repaired here — it is the B4 retry zone.
    with pytest.raises(TruncatedJSONError):
        parse_json_response('{"edits": [')
    with pytest.raises(TruncatedJSONError):
        parse_json_response('{"edits": [{"pid": "p00001"')
    with pytest.raises(TruncatedJSONError):
        parse_json_response("plain prose with no JSON object")


def test_parse_json_response_rejects_empty_body():
    with pytest.raises(EmptyResponseError):
        parse_json_response("")
    with pytest.raises(EmptyResponseError):
        parse_json_response("   \n ")


def test_parse_json_response_rejects_wrong_shape():
    # Valid JSON that is not an object is a downstream validation concern,
    # NOT a retry trigger (plain ValueError, not TruncatedJSONError).
    with pytest.raises(ValueError) as excinfo:
        parse_json_response("[1, 2, 3]")
    assert not isinstance(excinfo.value, TruncatedJSONError)
    with pytest.raises(ValueError):
        parse_json_response('"just a string"')


def test_classify_response_text_accepts_fenced_json():
    # RESILIENCE: a fence-wrapped valid body is NOT truncated — it must not
    # trigger a retry.
    fenced = "```json\n{\"issues\": []}\n```"
    assert classify_response_text(fenced) is None


def test_parse_json_response_regression_run_remote_001_chunk1_raw(tmp_path):
    """Regression on the run_remote_001 chunk1 raw artifact: the Qwen
    Russian-editor chunk1 response was wrapped in ```json fences and the R
    phase failed with 'response is not valid JSON: Expecting value: line 1
    column 1 (char 0)'. The tolerant utility must parse it."""
    raw = (
        "```json\n"
        "{\n"
        "  \"edits\": [\n"
        "    {\n"
        "      \"pid\": \"p00003\",\n"
        "      \"original\": \"Во въезде, прямо посередине\",\n"
        "      \"rewritten\": \"На въезде, прямо посередине\",\n"
        "      \"reason\": \"Неверный предлог: по-русски говорят «на въезде».\",\n"
        "      \"class\": \"preposition\"\n"
        "    },\n"
        "    {\n"
        "      \"pid\": \"p00005\",\n"
        "      \"original\": \"На приличное расстояние в любую сторону\",\n"
        "      \"rewritten\": \"На приличном расстоянии в любую сторону\",\n"
        "      \"reason\": \"Предложный падеж.\",\n"
        "      \"class\": \"grammar\"\n"
        "    }\n"
        "  ]\n"
        "}\n"
        "```"
    )
    parsed = parse_json_response(raw)
    assert isinstance(parsed, dict)
    assert parsed["edits"][0]["pid"] == "p00003"
    assert parsed["edits"][0]["class"] == "preposition"
    assert len(parsed["edits"]) == 2


def test_json_retry_policy_defaults_and_validation():
    policy = JsonRetryPolicy()
    assert policy.max_retries == 2
    assert policy.delay_for(0) == 1.0
    assert policy.delay_for(1) == 2.0
    with pytest.raises(ValueError):
        JsonRetryPolicy(max_retries=-1)
    with pytest.raises(ValueError):
        JsonRetryPolicy(base_delay_seconds=-0.1)
    # bool is an int subclass; reject it explicitly (True would otherwise
    # silently mean max_retries=1).
    with pytest.raises(ValueError):
        JsonRetryPolicy(max_retries=True)


# ---------------------------------------------------------------------------
# retry_json_call unit tests
# ---------------------------------------------------------------------------


def test_retry_json_call_empty_then_success():
    attempts = []

    def _call():
        attempts.append(1)
        if len(attempts) == 1:
            return ""
        return '{"issues": []}'

    out = retry_json_call(_call, _retry(), label="test")
    assert out == '{"issues": []}'
    assert len(attempts) == 2


def test_retry_json_call_truncated_then_success():
    attempts = []

    def _call():
        attempts.append(1)
        if len(attempts) == 1:
            return '{"issues": ['
        return '{"issues": []}'

    out = retry_json_call(_call, _retry(max_retries=1), label="test")
    assert out == '{"issues": []}'
    assert len(attempts) == 2


def test_retry_json_call_exhausted_raises_empty():
    attempts = []

    def _call():
        attempts.append(1)
        return ""

    with pytest.raises(EmptyResponseError):
        retry_json_call(_call, _retry(max_retries=2), label="test")
    # 1 initial + 2 retries.
    assert len(attempts) == 3


def test_retry_json_call_exhausted_raises_truncated():
    attempts = []

    def _call():
        attempts.append(1)
        return '{"issues": ['

    with pytest.raises(TruncatedJSONError):
        retry_json_call(_call, _retry(max_retries=0), label="test")
    assert len(attempts) == 1


def test_retry_json_call_never_retries_transport_error():
    attempts = []

    def _call():
        attempts.append(1)
        raise CompletionError("connection refused")

    with pytest.raises(CompletionError, match="connection refused"):
        retry_json_call(_call, _retry(max_retries=3), label="test")
    assert len(attempts) == 1


def test_retry_reissues_identical_inputs():
    # A retry must not change the request: same prompt/backend -> identity
    # unchanged (B4 §4).
    seen = []

    def _call(value):
        seen.append(value)
        if len(seen) == 1:
            return ""
        return '{"issues": []}'

    out = retry_json_call(lambda: _call("same-input"), _retry(max_retries=1), label="test")
    assert out == '{"issues": []}'
    assert seen == ["same-input", "same-input"]


def test_retry_logs_info_on_retry_and_warning_on_exhaustion(caplog):
    # A transient blip that self-heals is INFO (a healthy run is not noisy);
    # only budget exhaustion is WARNING (review recommendation).
    from pact_v4.runtime import json_resilience

    attempts = []

    def _call():
        attempts.append(1)
        if len(attempts) == 1:
            return ""
        return '{"issues": []}'

    with caplog.at_level("INFO", logger="pact_v4.runtime.json_resilience"):
        retry_json_call(_call, _retry(max_retries=1), label="test")
    assert any(
        record.levelname == "INFO" and "transient EmptyResponseError" in record.message
        for record in caplog.records
    )
    assert not any(record.levelname == "WARNING" for record in caplog.records)

    caplog.clear()
    attempts.clear()
    with caplog.at_level("INFO", logger="pact_v4.runtime.json_resilience"):
        with pytest.raises(EmptyResponseError):
            retry_json_call(_call, _retry(max_retries=0), label="test")
    assert any(
        record.levelname == "WARNING" and "retry budget exhausted" in record.message
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# Integration: qwen-audit (Step 6) over a real adapter + fake backend
# ---------------------------------------------------------------------------


def _no_issue_gemma():
    class _Gemma:
        def __call__(self, *, chunk_id, translation) -> str:
            return json.dumps({"issues": []}, ensure_ascii=False)

    return _Gemma()


def _audit_qwen(backend: ScriptedBackend) -> BackendQwenAuditEvaluator:
    return BackendQwenAuditEvaluator(
        backend,
        config=BackendQwenAuditEvaluatorConfig(retry=_retry(max_retries=2)),
    )


def test_audit_completes_when_empty_first_call_then_valid():
    # Integration: fake backend returns empty on the first qwen-audit call for
    # chunk0001 and a valid issues body on the retry — Step 6 must pass, not
    # fail the unit. (Detector-outer iteration: qwen chunk0001, chunk0002,
    # then gemma both chunks.)
    canned = json.dumps({"issues": []}, ensure_ascii=False)
    backend = ScriptedBackend([_text_response(""), _text_response(canned), _text_response(canned)])
    source, snapshot, chunk_plan, _c1, _c2, _cfg, candidates, chapter = _audit_env()
    from pact_v4.phase3.audit import run_chapter_audit

    outcome = run_chapter_audit(
        chapter=chapter, source=source, chunk_plan=chunk_plan, candidates=candidates,
        qwen_evaluator=_audit_qwen(backend),
        gemma_evaluator=_no_issue_gemma(),
    )
    assert outcome.status == "complete"
    assert outcome.failed_units == ()
    # chunk0001's retry re-issued the identical request (identity unchanged:
    # same prompt, model_ref, temperature, request_options); chunk0002 is a
    # different request (different chunk_id in the prompt).
    assert len(backend.requests) == 3
    assert backend.requests[0] == backend.requests[1]
    assert "chunk0001" in backend.requests[1].messages[0].content
    assert "chunk0002" in backend.requests[2].messages[0].content
    for request in backend.requests:
        assert request.model_ref == "qwen-3"
        assert request.temperature == 0.0
        assert request.request_options == {}


def test_audit_failed_unit_when_empty_retries_exhausted():
    # Two qwen-audit chunks, each exhausting max_retries=2 empty responses:
    # 3 attempts per chunk.
    backend = ScriptedBackend([_text_response("") for _ in range(6)])
    source, snapshot, chunk_plan, _c1, _c2, _cfg, candidates, chapter = _audit_env()
    from pact_v4.phase3.audit import run_chapter_audit

    outcome = run_chapter_audit(
        chapter=chapter, source=source, chunk_plan=chunk_plan, candidates=candidates,
        qwen_evaluator=_audit_qwen(backend),
        gemma_evaluator=_no_issue_gemma(),
    )
    # Exhausted retry -> failed unit (incomplete), never a semantic verdict
    # and never "no issues".
    assert outcome.status == "incomplete"
    assert len(outcome.failed_units) == 2
    assert all(detector == "qwen_chapter_audit" for _, detector, _ in outcome.failed_units)
    assert all("empty response" in error for _, _, error in outcome.failed_units)


def test_audit_transport_failure_is_not_retried():
    attempts = []

    class _Failing(ScriptedBackend):
        def complete(self, request):
            attempts.append(request)
            raise CompletionError("connection refused")

    source, snapshot, chunk_plan, _c1, _c2, _cfg, candidates, chapter = _audit_env()
    from pact_v4.phase3.audit import run_chapter_audit

    outcome = run_chapter_audit(
        chapter=chapter, source=source, chunk_plan=chunk_plan, candidates=candidates,
        qwen_evaluator=_audit_qwen(_Failing([])),
        gemma_evaluator=_no_issue_gemma(),
    )
    assert outcome.status == "incomplete"
    # Transport failure is the transport's own retry domain: the JSON retry
    # never re-issued a request — exactly one qwen-audit call per chunk.
    assert len(attempts) == 2


# ---------------------------------------------------------------------------
# Integration: repair (Step 7) over a real adapter + fake backend
# ---------------------------------------------------------------------------


def _repair_caller(backend: ScriptedBackend) -> BackendRepairCaller:
    return BackendRepairCaller(
        backend,
        config=BackendRepairCallerConfig(retry=_retry(max_retries=2)),
    )


def _repair_findings() -> list:
    # Deterministic one-chunk env (ch044): reuse the real snapshot hash so the
    # finding's identity matches the repair env _run_single_repair builds.
    _source, snapshot, _plan, chunk, _cfg, candidate, _cands, _chapter, _handoff = _repair_env()
    return [
        _finding(
            chunk_id=chunk.chunk_id, candidate_id=candidate.candidate_id,
            pid=chunk.pids[0], snapshot_id=snapshot.snapshot_hash,
        )
    ]


def test_repair_commits_when_truncated_first_call_then_valid():
    # Integration: fake backend returns a truncated repair body first and a
    # valid one second — Step 7 repair commits instead of recording debt.
    valid = json.dumps(
        {"repaired": {"p00000": "Исправленный перевод."}, "reason": "scripted"},
        ensure_ascii=False,
    )
    truncated = valid[:-5]
    backend = ScriptedBackend([_text_response(truncated), _text_response(valid)])
    repair_caller = _repair_caller(backend)
    record, _cand, chunk, _src = _run_single_repair(
        repair_caller=repair_caller,
        qwen_gate=ScriptedQwenGate(passed=True),
        gemma_audit=ScriptedGemmaAudit(),
        findings_override=_repair_findings(),
    )
    assert record.committed is True
    assert dict(record.new_translation)[chunk.pids[0]] == "Исправленный перевод."
    # Retry re-issued the identical request (repair backend identity intact).
    assert len(backend.requests) == 2
    assert all(a == backend.requests[0] for a in backend.requests)


def test_repair_debt_when_truncated_retries_exhausted():
    valid = json.dumps(
        {"repaired": {"p00000": "Исправленный перевод."}, "reason": "scripted"},
        ensure_ascii=False,
    )
    truncated = valid[:-5]
    backend = ScriptedBackend([_text_response(truncated)])
    repair_caller = BackendRepairCaller(
        backend,
        config=BackendRepairCallerConfig(retry=_retry(max_retries=0)),
    )
    record, _cand, chunk, _src = _run_single_repair(
        repair_caller=repair_caller,
        qwen_gate=ScriptedQwenGate(passed=True),
        gemma_audit=ScriptedGemmaAudit(),
        findings_override=_repair_findings(),
    )
    # Exhausted retry -> debt (not committed), never a semantic verdict.
    assert record.committed is False
    assert "debt" in record.reason or "failed" in record.reason


def test_repair_transport_failure_is_not_retried():
    attempts = []

    class _Failing(ScriptedBackend):
        def complete(self, request):
            attempts.append(request)
            raise CompletionError("connection refused")

    repair_caller = BackendRepairCaller(
        _Failing([]),
        config=BackendRepairCallerConfig(retry=_retry(max_retries=3)),
    )
    record, _cand, chunk, _src = _run_single_repair(
        repair_caller=repair_caller,
        qwen_gate=ScriptedQwenGate(passed=True),
        gemma_audit=ScriptedGemmaAudit(),
        findings_override=_repair_findings(),
    )
    assert record.committed is False
    assert len(attempts) == 1
