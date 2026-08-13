"""B1 contract tests for the chunked Qwen audit (``pact_v4.audit.chunked_audit``).

Everything runs against a scripted in-memory ``CompletionBackend`` — zero
real model calls, zero HTTP (B1 acceptance: ``0 реальных вызовов``). The
suite pins the four B1 acceptance contracts:

* gold suite (§6 of ``V4_1_AUDIT_B1_RU.md``): 8 must-find + 6 must-not-find;
* chunking chapter 0001 = exactly 8 chunks (greedy, max_input=3600);
* fail-closed: a mock LENGTH / INVALID_JSON chunk makes ``audit_complete``
  false (a failed chunk is never read as ``issues=[]``);
* full input budget (entity soft 500 / hard 800, ``calibrated_total``).

The gold PIDs mirror the real chapter-0001 cases but the pair text is
synthetic (hermetic; no chapter data in the repo, per data restrictions).
"""
from __future__ import annotations

import json
from typing import List, Mapping, Optional, Sequence

import pytest

from pact_v4.audit.chunked_audit import (
    AUDIT_V4_CATEGORIES,
    AuditPair,
    BudgetOverflowError,
    ChunkedAuditConfig,
    ChunkedAuditEvaluator,
    ChunkedAuditOutcome,
    CoverageError,
    build_greedy_chunks,
    build_narrator_context,
    classify_chunk,
    dedupe_issues,
    get_overlap_context,
    pairs_from_maps,
    pair_token_estimate,
    validate_chunk_json,
    validate_input_budget,
)
from pact_v4.runtime.backend_protocol import (
    BackendCallRecord,
    BackendDescriptor,
    CompletionBackend,
    CompletionError,
    CompletionRequest,
    CompletionResponse,
)
from pact_v4.runtime.prompts_runtime import (
    QWEN_AUDIT_V4_1,
    render_chunked_audit_prompt,
)

# ---------------------------------------------------------------------------
# Fake backend (scripted, in-memory)
# ---------------------------------------------------------------------------


class ScriptedBackend(CompletionBackend):
    """In-memory ``CompletionBackend`` returning scripted responses."""

    _BINDINGS = {
        "default": "qwen-3.6-35b",
        "qwen_audit": "qwen-3.6-35b",
        "fidelity_reviewer": "qwen-3.6-35b",
        "qwen_fidelity": "qwen-3.6-35b",
    }

    def __init__(self, script: Sequence[CompletionResponse]):
        self._script = list(script)
        self.requests: List[CompletionRequest] = []

    @property
    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(
            kind="local_llama",
            transport_version="openai-chat-completions/v1",
            endpoint_family="openai_chat_completions",
            public_endpoint="http://127.0.0.1:8094/v1/chat/completions",
            model_bindings=dict(self._BINDINGS),
            effective_options={"temperature": 0.0, "context_size": 49152},
        )

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        if not self._script:
            raise AssertionError("ScriptedBackend: script exhausted")
        return self._script.pop(0)

    def close(self) -> None:
        pass

    def call_records(self) -> Sequence[BackendCallRecord]:
        return []


class _TransportFailingBackend(ScriptedBackend):
    """ScriptedBackend that raises ``CompletionError`` on selected calls.

    ``fail_on`` is a set of 1-based call indices to fail with a transport
    error; the remaining calls consume the script normally. Used to pin the
    fail-closed transport contract: a ``CompletionError`` from ``complete``
    must become a failed ``TRANSPORT_ERROR`` chunk (audit_complete=false),
    never escape the evaluator.
    """

    def __init__(
        self,
        script: Sequence[CompletionResponse],
        fail_on: Sequence[int] = (1,),
    ) -> None:
        super().__init__(script)
        self._fail_on = set(fail_on)
        self._call_index = 0

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self._call_index += 1
        self.requests.append(request)
        if self._call_index in self._fail_on:
            raise CompletionError(f"simulated transport failure (call {self._call_index})")
        if not self._script:
            raise AssertionError("ScriptedBackend: script exhausted")
        return self._script.pop(0)


def _ok_response(issues: Sequence[Mapping[str, str]], finish: Optional[str] = "stop") -> CompletionResponse:
    return CompletionResponse(
        text=json.dumps({"issues": list(issues)}, ensure_ascii=False),
        model="qwen-3.6-35b",
        finish_reason=finish,
    )


def _issue(pid: str, category: str = "omission", severity: str = "major",
           confidence: str = "high", note: str = "test") -> Mapping[str, str]:
    return {
        "id": pid, "category": category, "severity": severity,
        "confidence": confidence, "note": note,
    }


def _pair(pid: str, src: str = "The steward opened the door.",
          tr: str = "Стюард открыл дверь.") -> AuditPair:
    return AuditPair(pid=pid, source=src, translation=tr)


def _pairs(prefix: str, count: int) -> List[AuditPair]:
    return [_pair(f"{prefix}{i:05d}") for i in range(1, count + 1)]


def _big_pair(pid: str) -> AuditPair:
    """A pair with ~100 estimated tokens (forces multi-chunk layouts)."""
    return AuditPair(
        pid=pid,
        source="w" * 200,        # 50 est tokens
        translation="ы" * 150,    # 50 est tokens
    )


# ---------------------------------------------------------------------------
# Greedy chunking + overlap (pure functions)
# ---------------------------------------------------------------------------


def test_greedy_chunks_never_exceed_budget_and_cover_all_pairs() -> None:
    pairs = _pairs("p", 40)
    chunks = build_greedy_chunks(pairs, max_input=3600)
    flattened = [p.pid for chunk in chunks for p in chunk]
    assert flattened == [p.pid for p in pairs]
    for chunk in chunks:
        total = sum(pair_token_estimate(p.source, p.translation) for p in chunk)
        assert total <= 3600


def test_chunking_chapter_0001_is_exactly_8_chunks() -> None:
    """B1 acceptance: chapter 0001 chunks into exactly 8 greedy chunks.

    Uses the real chapter 0001 source/translation artifacts when the
    ``PACT_B1_CH0001_SOURCE`` / ``PACT_B1_CH0001_TRANSLATION`` env vars point
    at them (skipped otherwise — the artifacts are not part of the repo);
    plus a hermetic 400-pair synthetic chapter with the same token profile so
    the contract is pinned even without the real files.
    """
    import os

    src_path = os.environ.get("PACT_B1_CH0001_SOURCE")
    tr_path = os.environ.get("PACT_B1_CH0001_TRANSLATION")
    if src_path and tr_path:
        source = json.loads(open(src_path, encoding="utf-8").read())
        translation = json.loads(open(tr_path, encoding="utf-8").read())
        pairs = pairs_from_maps(source, translation)
        assert len(pairs) == 400
        chunks = build_greedy_chunks(pairs, max_input=3600)
        assert len(chunks) == 8
        # harness layout (verified against audit_v4_qwen_r8192_v42.json):
        assert [len(c) for c in chunks] == [34, 46, 55, 48, 47, 72, 56, 42]

    # Hermetic mirror: 400 pairs with ~69.25 est tokens each (real chapter
    # total ~27700) -> greedy @3600 = exactly 8 chunks.
    synth = [
        AuditPair(
            pid=f"p{i:05d}",
            source="w" * 160,   # 40 est tokens
            translation="ы" * 88,  # ~29.3 est tokens
        )
        for i in range(1, 401)
    ]
    chunks = build_greedy_chunks(synth, max_input=3600)
    assert len(chunks) == 8


def test_overlap_context_from_original_chapter() -> None:
    pairs = _pairs("p", 30)
    # chunk starting at p00020 gets preceding pairs from the ORIGINAL chapter
    overlap = get_overlap_context(pairs, "p00020", max_tokens=400)
    assert overlap
    assert overlap[0].pid == "p00001" or overlap[0].pid == "p00014"
    assert len(overlap) <= 6
    # first chunk (no preceding pairs) gets empty context
    assert get_overlap_context(pairs, "p00001", max_tokens=400) == []
    # min 2 pairs even for a tiny budget
    tiny = get_overlap_context(pairs, "p00003", max_tokens=1)
    assert len(tiny) >= 2


# ---------------------------------------------------------------------------
# Fail-closed coverage: missing PIDs / empty input (RV fix)
# ---------------------------------------------------------------------------


def test_pairs_from_maps_missing_translation_raises_coverage_error() -> None:
    """A source PID without a translation must raise (never a silent
    partial audit that claims audit_complete=True)."""
    with pytest.raises(CoverageError) as excinfo:
        pairs_from_maps({"p1": "s1", "p2": "s2"}, {"p1": "t1"})
    assert "p2" in str(excinfo.value)


def test_pairs_from_maps_preserves_source_insertion_order() -> None:
    """PIDs are emitted in SOURCE insertion order, not sorted() — the
    declared "source order" must hold for non-zero-padded / non-lexical
    PID maps."""
    source = {"ch7": "a", "ch2": "b", "ch10": "c"}
    translation = {"ch7": "x", "ch2": "y", "ch10": "z"}
    pairs = pairs_from_maps(source, translation)
    assert [p.pid for p in pairs] == ["ch7", "ch2", "ch10"]


def test_evaluator_rejects_empty_pairs_before_model_call() -> None:
    """Empty input is rejected before any model call (never
    audit_complete=True with 0 chunks)."""
    backend = ScriptedBackend([])
    evaluator = ChunkedAuditEvaluator(backend)
    with pytest.raises(CoverageError):
        evaluator(chapter_id="0001", pairs=[])
    assert backend.requests == []


# ---------------------------------------------------------------------------
# Strict validation + classification (fail-closed)
# ---------------------------------------------------------------------------


def test_validate_chunk_json_accepts_gold_issue_shape() -> None:
    issues = [_issue("p00010", category="invented_gender")]
    result = validate_chunk_json({"issues": issues}, ["p00010"])
    assert result.valid
    assert len(result.issues) == 1
    assert result.errors == ()


@pytest.mark.parametrize("bad", [
    {"id": "p00010", "category": "scene", "severity": "major", "confidence": "high"},
    {"id": "p00010", "category": "omission", "severity": "fatal", "confidence": "high"},
    {"id": "p00010", "category": "omission", "severity": "major", "confidence": "certain"},
    {"id": "p99999", "category": "omission", "severity": "major", "confidence": "high"},
])
def test_validate_chunk_json_rejects_invalid_issue(bad) -> None:
    result = validate_chunk_json({"issues": [bad]}, ["p00010"])
    assert not result.valid
    assert result.issues == ()
    assert result.errors


def test_validate_chunk_json_rejects_non_object_or_missing_issues() -> None:
    assert not validate_chunk_json(None, ["p00010"]).valid
    assert not validate_chunk_json({"nope": 1}, ["p00010"]).valid
    assert not validate_chunk_json({"issues": "x"}, ["p00010"]).valid


def test_classify_chunk_statuses() -> None:
    assert classify_chunk("length", "x", "", True) == "LENGTH"
    assert classify_chunk("stop", "  ", "", True) == "EMPTY"
    assert classify_chunk("stop", '{"issues": []}', "", True) == "GOOD"
    assert classify_chunk("stop", "pass, faithful, source", "reasoning...", False) == "SPILL"
    assert classify_chunk("stop", "not json at all", "", False) == "INVALID_JSON"


def test_dedupe_by_id_category_high_confidence_wins() -> None:
    low = _issue("p00010", category="omission", confidence="low")
    high = _issue("p00010", category="omission", confidence="high", note="better")
    other_cat = _issue("p00010", category="addition", confidence="medium")
    deduped = dedupe_issues([low, high, other_cat])
    assert len(deduped) == 2
    by_cat = {i["category"]: i for i in deduped}
    assert by_cat["omission"]["confidence"] == "high"
    assert by_cat["omission"]["note"] == "better"
    assert by_cat["addition"]["confidence"] == "medium"


# ---------------------------------------------------------------------------
# Gold suite (§6): 8 must-find + 6 must-not-find (mock backend)
# ---------------------------------------------------------------------------

# Gold must-find (chapter 0001, concept §6): category is the contract the
# v4.1 prompt must catch; the mock returns exactly these as the model output.
GOLD_MUST_FIND = [
    ("p00010", "invented_gender"),   # wannabe-architect -> девушкой
    ("p00013", "changed_fact"),      # printed -> вышито
    ("p00032", "invented_gender"),   # youngest -> младшему (морфология)
    ("p00035", "changed_fact"),      # preoccupied -> поглощена собой
    ("p00093", "negation"),          # didn't already know -> уже не знал
    ("p00132", "addition"),          # в гости в гости
    ("p00193", "invented_gender"),   # grandchild -> внук
    ("p00236", "changed_fact"),      # motorcycle -> велосипед (entity)
]

# Gold must-not-find: the model must PASS these (mock returns no issues);
# a would-be issue for them must never be silently accepted.
GOLD_MUST_NOT_FIND = [
    "p00075", "p00106", "p00136", "p00151", "p00184", "p00309",
]


def test_gold_suite_8_must_find_are_collected() -> None:
    pairs = [
        _pair(pid) for pid, _ in GOLD_MUST_FIND
    ]
    gold_issues = [
        _issue(pid, category=cat, note=f"gold {pid}")
        for pid, cat in GOLD_MUST_FIND
    ]
    backend = ScriptedBackend([_ok_response(gold_issues)])
    evaluator = ChunkedAuditEvaluator(backend)
    outcome = evaluator(chapter_id="0001", pairs=pairs)

    assert outcome.audit_complete
    assert outcome.issue_count == 8
    found = {(i["id"], i["category"]) for i in outcome.issues}
    assert found == set(GOLD_MUST_FIND)
    # every collected issue carries _debug {chunk, reasoning_file}
    for issue in outcome.issues:
        assert "_debug" in issue
        assert issue["_debug"]["chunk"] == 1
        assert issue["_debug"]["reasoning_file"]


def test_gold_suite_6_must_not_find_are_rejected() -> None:
    # Model correctly passes all six negative cases: empty issue list.
    pairs = [_pair(pid) for pid in GOLD_MUST_NOT_FIND]
    backend = ScriptedBackend([_ok_response([])])
    evaluator = ChunkedAuditEvaluator(backend)
    outcome = evaluator(chapter_id="0001", pairs=pairs)
    assert outcome.audit_complete
    assert outcome.issue_count == 0
    assert all(i["id"] not in GOLD_MUST_NOT_FIND for i in outcome.issues)


def test_gold_must_not_find_issue_in_context_only_is_fail_closed() -> None:
    # A would-be FP that references a CONTEXT_ONLY pair (in the overlap, not
    # in the chunk's AUDIT_PAIRS) must be rejected by validation -> the chunk
    # fails closed (audit_complete=false), never silently accepted.
    # 40 big pairs (~4000 est tokens) -> 2 chunks (36 + 4); chunk 2's
    # CONTEXT_ONLY = preceding pairs (p00033..p00036, ~400 tokens); the model
    # reports an issue for p00034 (context-only id) -> invalid for chunk 2.
    pairs = [_big_pair(f"p{i:05d}") for i in range(1, 41)]
    backend = ScriptedBackend([
        _ok_response([]),                                        # chunk 1 GOOD
        _ok_response([_issue("p00034", category="omission")]),   # chunk 2 would-be FP
    ])
    evaluator = ChunkedAuditEvaluator(
        backend, config=ChunkedAuditConfig(retry_shrink=False),
    )
    outcome = evaluator(chapter_id="0001", pairs=pairs)

    assert len(backend.requests) == 2
    second_prompt = backend.requests[1].messages[0].content
    assert "CONTEXT_ONLY (for resolving" in second_prompt
    assert "<PAIR id=\"p00034\"" in second_prompt  # p00034 is context-only for chunk 2
    assert not outcome.audit_complete
    assert outcome.failed_chunks == (2,)
    # the would-be FP never reaches the collected issues
    assert all(i["id"] != "p00034" for i in outcome.issues)


# ---------------------------------------------------------------------------
# End-to-end chunked run (mock): prompts, overlap, dedup, aggregation
# ---------------------------------------------------------------------------


def test_chunked_run_sends_v41_prompt_with_context_blocks() -> None:
    pairs = [_big_pair(f"p{i:05d}") for i in range(1, 41)]  # 2 chunks (36+4)
    backend = ScriptedBackend([_ok_response([]), _ok_response([])])
    evaluator = ChunkedAuditEvaluator(backend)
    evaluator(
        chapter_id="0001",
        pairs=pairs,
        narrator_context="narrator: Blake Thorburn (gender male)\n",
        entity_context="- entity: Blake's vehicle\n  established_type: motorcycle\n",
    )
    assert len(backend.requests) == 2
    for request in backend.requests:
        prompt = request.messages[0].content
        assert "BOOK CONTEXT - FALLBACK ONLY" in prompt
        assert "CHAPTER ENTITY FACTS - SOURCE-DERIVED" in prompt
        assert "<PAIR id=" in prompt
        assert "AUDIT_PAIRS (chunk " in prompt
        assert request.temperature == 0.0
        assert request.max_output_tokens == 12000
        # reasoning budget must NOT travel via request_options (server arg)
        assert request.request_options == {}


def test_chunked_run_second_chunk_gets_context_only_overlap() -> None:
    pairs = [_big_pair(f"p{i:05d}") for i in range(1, 41)]  # 2 chunks (36+4)
    # second chunk's prompt must contain CONTEXT_ONLY preceding pairs
    backend = ScriptedBackend([_ok_response([]), _ok_response([])])
    evaluator = ChunkedAuditEvaluator(backend)
    evaluator(chapter_id="0001", pairs=pairs)
    assert len(backend.requests) == 2
    second_prompt = backend.requests[1].messages[0].content
    assert "CONTEXT_ONLY (for resolving" in second_prompt
    assert "NEVER report an issue for a CONTEXT_ONLY pair" in second_prompt
    assert "<PAIR id=\"p00033\"" in second_prompt  # overlap from ORIGINAL chapter
    first_prompt = backend.requests[0].messages[0].content
    assert "CONTEXT_ONLY (for resolving" not in first_prompt


def test_chunked_run_aggregates_dedup_and_debug() -> None:
    # Dedup by id+category within one chunk: two issues, same id+category,
    # different confidence -> the high-confidence one wins.
    pairs = [_big_pair(f"p{i:05d}") for i in range(1, 25)]  # 1 chunk
    backend = ScriptedBackend([
        _ok_response([
            _issue("p00001", category="omission", confidence="low"),
            _issue("p00001", category="omission", confidence="high"),
        ]),
    ])
    evaluator = ChunkedAuditEvaluator(backend)
    outcome = evaluator(chapter_id="0001", pairs=pairs)
    assert outcome.audit_complete
    assert outcome.issue_count == 1
    assert outcome.issues[0]["confidence"] == "high"


# ---------------------------------------------------------------------------
# Fail-closed acceptance: LENGTH / INVALID_JSON -> audit_complete=false
# ---------------------------------------------------------------------------


def test_fail_closed_on_length() -> None:
    pairs = [_big_pair(f"p{i:05d}") for i in range(1, 41)]  # 2 chunks (36+4)
    # chunk 1 truncated (finish_reason=length), no retry-shrink
    backend = ScriptedBackend([
        _ok_response([], finish="length"),
        _ok_response([]),
    ])
    evaluator = ChunkedAuditEvaluator(
        backend, config=ChunkedAuditConfig(retry_shrink=False),
    )
    outcome = evaluator(chapter_id="0001", pairs=pairs)
    assert not outcome.audit_complete
    assert outcome.failed_chunks == (1,)
    assert outcome.successful_chunks == 1
    assert outcome.chunks[0]["status"] == "LENGTH"


def test_fail_closed_on_invalid_json() -> None:
    pairs = [_big_pair(f"p{i:05d}") for i in range(1, 41)]  # 2 chunks (36+4)
    backend = ScriptedBackend([
        CompletionResponse(text="this is not json", model="qwen", finish_reason="stop"),
        _ok_response([]),
    ])
    evaluator = ChunkedAuditEvaluator(
        backend, config=ChunkedAuditConfig(retry_shrink=False),
    )
    outcome = evaluator(chapter_id="0001", pairs=pairs)
    assert not outcome.audit_complete
    assert outcome.failed_chunks == (1,)
    assert outcome.chunks[0]["status"] == "INVALID_JSON"


def test_fail_closed_on_validation_error() -> None:
    pairs = [_big_pair(f"p{i:05d}") for i in range(1, 41)]  # 2 chunks (36+4)
    # chunk 1 returns an issue with a foreign id (not in chunk) -> invalid
    backend = ScriptedBackend([
        _ok_response([_issue("p99999")]),
        _ok_response([]),
    ])
    evaluator = ChunkedAuditEvaluator(
        backend, config=ChunkedAuditConfig(retry_shrink=False),
    )
    outcome = evaluator(chapter_id="0001", pairs=pairs)
    assert not outcome.audit_complete
    assert outcome.failed_chunks == (1,)


# ---------------------------------------------------------------------------
# Fail-closed transport: CompletionError -> TRANSPORT_ERROR chunk (RV fix)
# ---------------------------------------------------------------------------


def test_transport_failure_is_fail_closed_not_escaped() -> None:
    """A ``CompletionError`` from ``complete`` must become a failed chunk
    (audit_complete=false), never escape the evaluator. Pinned to
    ``transport_max_retries=0`` (R-RETRY t_8ab8ab35): the default transport
    retry would re-issue the call, which is covered by
    ``test_transport_failure_retried_with_new_session``."""
    pairs = [_big_pair(f"p{i:05d}") for i in range(1, 41)]  # 2 chunks (36+4)
    backend = _TransportFailingBackend(
        script=[_ok_response([])],  # consumed by chunk 2
        fail_on=(1,),               # chunk 1 transport failure
    )
    evaluator = ChunkedAuditEvaluator(
        backend, config=ChunkedAuditConfig(transport_max_retries=0),
    )
    outcome = evaluator(chapter_id="0001", pairs=pairs)
    assert not outcome.audit_complete
    assert outcome.failed_chunks == (1,)
    assert outcome.successful_chunks == 1
    assert outcome.chunks[0]["status"] == "TRANSPORT_ERROR"
    assert outcome.issue_count == 0  # failed chunk is never issues=[]
    # the diagnostic is recorded on the failed chunk meta
    assert outcome.chunks[0]["reasoning_file"].endswith("_reasoning.txt")


def test_transport_failure_retried_with_new_session() -> None:
    """R-RETRY (t_8ab8ab35, operator extension): a TRANSPORT_ERROR is
    retried with a NEW session (bounded, backoff) — a transient failure
    recovers and the chunk is GOOD, not failed. Only after the bounded
    retries are exhausted does it become a failed TRANSPORT_ERROR chunk."""
    pairs = [_big_pair(f"p{i:05d}") for i in range(1, 41)]  # 2 chunks (36+4)
    # chunk 1: first call raises (call 1), retry succeeds (call 2 consumes
    # script[0]); chunk 2 consumes script[1].
    backend = _TransportFailingBackend(
        script=[
            _ok_response([_issue("p00001", category="omission")]),
            _ok_response([]),
        ],
        fail_on=(1,),
    )
    evaluator = ChunkedAuditEvaluator(
        backend, config=ChunkedAuditConfig(transport_base_delay_seconds=0.0),
    )
    outcome = evaluator(chapter_id="0001", pairs=pairs)
    assert outcome.audit_complete
    assert outcome.failed_chunks == ()
    assert outcome.successful_chunks == 2
    # chunk 1 recovered after 1 retry (2 backend calls: fail + GOOD)
    assert outcome.chunks[0]["status"] == "GOOD"
    assert len(backend.requests) == 3  # 2 for chunk1 + 1 for chunk2


def test_transport_failure_retries_exhausted_still_fail_closed() -> None:
    """R-RETRY (t_8ab8ab35): after the bounded transport retries are
    exhausted the chunk is a failed TRANSPORT_ERROR — fail-closed."""
    pairs = [_big_pair(f"p{i:05d}") for i in range(1, 41)]  # 2 chunks (36+4)
    backend = _TransportFailingBackend(
        script=[_ok_response([])],
        fail_on=(1, 2, 3),  # all 3 attempts of chunk 1 fail
    )
    evaluator = ChunkedAuditEvaluator(
        backend, config=ChunkedAuditConfig(transport_base_delay_seconds=0.0),
    )
    outcome = evaluator(chapter_id="0001", pairs=pairs)
    assert not outcome.audit_complete
    assert outcome.failed_chunks == (1,)
    assert outcome.chunks[0]["status"] == "TRANSPORT_ERROR"
    # 3 attempts for chunk 1 + 1 for chunk 2, no RetryShrink dead calls
    assert len(backend.requests) == 4


def test_transport_failure_continues_other_chunks() -> None:
    """Transport failure on chunk 2 must not abort chunk 1's findings."""
    pairs = [_big_pair(f"p{i:05d}") for i in range(1, 41)]  # 2 chunks (36+4)
    backend = _TransportFailingBackend(
        script=[_ok_response([_issue("p00001", category="omission")])],
        fail_on=(2,),
    )
    evaluator = ChunkedAuditEvaluator(
        backend, config=ChunkedAuditConfig(retry_shrink=False),
    )
    outcome = evaluator(chapter_id="0001", pairs=pairs)
    assert not outcome.audit_complete
    assert outcome.failed_chunks == (2,)
    # chunk 1's finding is still collected (with its own _debug)
    assert outcome.issue_count == 1
    assert outcome.issues[0]["id"] == "p00001"
    assert outcome.issues[0]["_debug"]["chunk"] == 1


def test_transport_failure_skips_retry_shrink() -> None:
    """RetryShrink is an input-size strategy: a TRANSPORT_ERROR chunk is
    recorded as failed as-is (no dead sub-chunk calls). Pinned to
    ``transport_max_retries=0`` (R-RETRY t_8ab8ab35) — the retry behavior
    is covered by ``test_transport_failure_retried_with_new_session``."""
    pairs = [_big_pair(f"p{i:05d}") for i in range(1, 41)]  # 2 chunks (36+4)
    backend = _TransportFailingBackend(
        script=[_ok_response([])],
        fail_on=(1,),
    )
    evaluator = ChunkedAuditEvaluator(
        backend,
        config=ChunkedAuditConfig(transport_max_retries=0),  # retry_shrink=True default
    )
    outcome = evaluator(chapter_id="0001", pairs=pairs)
    assert not outcome.audit_complete
    assert outcome.chunks[0]["status"] == "TRANSPORT_ERROR"
    # exactly one call per chunk: no shrink sub-chunks against a dead transport
    assert len(backend.requests) == 2


def test_transport_failure_during_retry_shrink_is_fail_closed() -> None:
    """A transport error inside RetryShrink marks the chunk failed and does
    not re-queue the sub (no dead sub-chunk call multiplication). Pinned to
    ``transport_max_retries=0`` (R-RETRY t_8ab8ab35)."""
    pairs = [_big_pair(f"p{i:05d}") for i in range(1, 26)]  # 1 chunk (25 pairs)
    # parent LENGTH -> shrink level 1: sub1 (p00001..18) transport-fails,
    # sub2 (p00019..25) GOOD -> chunk still failed (sub1 not audited)
    backend = _TransportFailingBackend(
        script=[
            _ok_response([], finish="length"),               # parent LENGTH
            _ok_response([_issue("p00019", category="omission")]),  # lvl1 sub2 GOOD
        ],
        fail_on=(2,),  # lvl1 sub1 transport failure
    )
    evaluator = ChunkedAuditEvaluator(
        backend, config=ChunkedAuditConfig(transport_max_retries=0),
    )
    outcome = evaluator(chapter_id="0001", pairs=pairs)
    assert not outcome.audit_complete
    assert outcome.failed_chunks == (1,)
    assert outcome.chunks[0]["status"] == "FAILED_RETRIED"
    # the audited sub's issue is kept, the failed sub contributes nothing
    assert outcome.issue_count == 1
    assert outcome.issues[0]["id"] == "p00019"


# ---------------------------------------------------------------------------
# RetryShrink (by input: /2 then /3; overlap from ORIGINAL chapter)
# ---------------------------------------------------------------------------


def test_retry_shrink_success_marks_good_retried_and_keeps_issues() -> None:
    # 25 big pairs (~2500 est tokens) = 1 chunk; parent LENGTH -> shrink at
    # max_input/2=1800 -> 2 sub-chunks (18+7 pairs), both GOOD -> GOOD_RETRIED.
    pairs = [_big_pair(f"p{i:05d}") for i in range(1, 26)]
    backend = ScriptedBackend([
        _ok_response([], finish="length"),  # parent LENGTH
        _ok_response([_issue("p00001", category="omission")]),  # sub1 GOOD (p00001..18)
        _ok_response([_issue("p00019", category="addition")]),  # sub2 GOOD (p00019..25)
    ])
    evaluator = ChunkedAuditEvaluator(backend)
    outcome = evaluator(chapter_id="0001", pairs=pairs)
    assert outcome.audit_complete  # GOOD_RETRIED counts as successful
    assert outcome.issue_count == 2
    assert outcome.chunks[0]["status"] == "GOOD_RETRIED"
    # sub-chunk issues carry their own reasoning_file in _debug
    files = {i["_debug"]["reasoning_file"] for i in outcome.issues}
    assert len(files) == 2
    assert all("lvl1_sub" in f for f in files)


def test_retry_shrink_still_failed_after_shrink_is_fail_closed() -> None:
    # parent LENGTH, then every sub fails at both levels -> FAILED_RETRIED.
    # 25 big pairs: lvl1 (1800) -> 2 subs; lvl2 (1200) -> 3 subs; all LENGTH.
    pairs = [_big_pair(f"p{i:05d}") for i in range(1, 26)]
    backend = ScriptedBackend([
        _ok_response([], finish="length"),   # parent
        _ok_response([], finish="length"),   # lvl1 sub1
        _ok_response([], finish="length"),   # lvl1 sub2
        _ok_response([], finish="length"),   # lvl2 sub1
        _ok_response([], finish="length"),   # lvl2 sub2
        _ok_response([], finish="length"),   # lvl2 sub3
    ])
    evaluator = ChunkedAuditEvaluator(backend)
    outcome = evaluator(chapter_id="0001", pairs=pairs)
    assert not outcome.audit_complete
    assert outcome.chunks[0]["status"] == "FAILED_RETRIED"


# ---------------------------------------------------------------------------
# Input budget (concept §2): entity soft/hard, calibrated_total
# ---------------------------------------------------------------------------


def test_entity_context_hard_cap_fails_closed() -> None:
    pairs = _pairs("p", 5)
    huge_entity = "e" * (800 * 4 + 100)  # > 800 est tokens
    backend = ScriptedBackend([])
    evaluator = ChunkedAuditEvaluator(backend)
    with pytest.raises(BudgetOverflowError):
        evaluator(chapter_id="0001", pairs=pairs, entity_context=huge_entity)
    assert backend.requests == []  # no model call on hard budget failure


def test_input_budget_ok_within_limits() -> None:
    pairs = _pairs("p", 5)
    entity = "e" * 500  # ~125 est tokens
    backend = ScriptedBackend([_ok_response([])])
    evaluator = ChunkedAuditEvaluator(backend)
    outcome = evaluator(chapter_id="0001", pairs=pairs, entity_context=entity)
    assert outcome.audit_complete


# ---------------------------------------------------------------------------
# 3-level context: narrator builder (canonical names only, generic excluded)
# ---------------------------------------------------------------------------


def test_build_narrator_context_canonical_only_generic_excluded() -> None:
    book_memory = {
        "pov": {"source_name": "Blake Thorburn", "gender": "male"},
        "characters": {
            "Blake Thorburn": {"gender": "male"},
            "Molly Walker": {"gender": "female"},
            "the nurse": {"gender": "female"},   # generic -> must be excluded
            "Callan": {"gender": "male"},
        },
    }
    source_text = (
        "Blake Thorburn looked at Molly Walker. Callan was there. "
        "The nurse smiled up at him."
    )
    context = build_narrator_context(book_memory, source_text)
    assert "narrator: Blake Thorburn (gender male)" in context
    assert "Blake Thorburn: male" in context
    assert "Molly Walker: female" in context
    assert "Callan: male" in context
    assert "the nurse" not in context  # generic role excluded


def test_build_narrator_context_empty_without_memory() -> None:
    assert build_narrator_context({}, "some text") == ""


def test_build_narrator_context_word_boundary_no_substring_false_matches() -> None:
    """Canonical names match as whole words, never as substrings: ``Ann``
    must not match inside ``announced``, ``Rich`` must not match inside
    ``richness`` (poisoned-context regression)."""
    book_memory = {
        "pov": {"source_name": "Blake Thorburn", "gender": "male"},
        "characters": {
            "Ann": {"gender": "female"},
            "Rich": {"gender": "male"},
            "Blake Thorburn": {"gender": "male"},
        },
    }
    # "Ann" appears only inside "announced", "Rich" only inside "richness"
    source_text = "The man announced a plan about the richness of the soil."
    context = build_narrator_context(book_memory, source_text)
    # narrator is added unconditionally (the narrator IS present by definition)
    assert "narrator: Blake Thorburn (gender male)" in context
    assert "Ann" not in context
    assert "Rich" not in context


def test_build_narrator_context_word_boundary_standalone_matches() -> None:
    """The same names DO match as standalone words (true positives kept)."""
    book_memory = {
        "pov": {"source_name": "Blake Thorburn", "gender": "male"},
        "characters": {
            "Ann": {"gender": "female"},
            "Rich": {"gender": "male"},
            "Molly Walker": {"gender": "female"},
        },
    }
    source_text = "Ann called Rich, and Molly Walker followed."
    context = build_narrator_context(book_memory, source_text)
    assert "Ann: female" in context
    assert "Rich: male" in context
    assert "Molly Walker: female" in context


def test_build_narrator_context_multiworld_and_unicode_boundaries() -> None:
    """Multi-word names and non-ASCII names respect word boundaries."""
    book_memory = {
        "pov": {"source_name": "Blake Thorburn", "gender": "male"},
        "characters": {
            "Jean-Luc": {"gender": "male"},
            "Марина": {"gender": "female"},
        },
    }
    source_text = "Jean-Luc answered; Марина laughed. Jean-Lucasse was not here."
    context = build_narrator_context(book_memory, source_text)
    assert "Jean-Luc: male" in context
    assert "Марина: female" in context
    # "Jean-Lucasse" must not match "Jean-Luc" as a substring
    assert "Jean-Lucasse" not in context


# ---------------------------------------------------------------------------
# Prompt renderer contract
# ---------------------------------------------------------------------------


def test_render_chunked_audit_prompt_block_order() -> None:
    pairs = [_pair("p00001"), _pair("p00002")]
    ctx = [_pair("p00000", src="preceding", tr="предыдущая")]
    prompt = render_chunked_audit_prompt(
        chunk_id="0001", audit_pairs=pairs, context_pairs=ctx,
        narrator_context="narrator: X (gender male)",
        entity_context="- entity: Y",
        chunk_index=2, chunk_total=4,
    )
    assert prompt.startswith(QWEN_AUDIT_V4_1.instructions)
    ctx_marker = "CONTEXT_ONLY (for resolving"
    audit_marker = "AUDIT_PAIRS (chunk 2 of 4):"
    assert prompt.index("BOOK CONTEXT - FALLBACK ONLY") < prompt.index(
        "CHAPTER ENTITY FACTS - SOURCE-DERIVED")
    assert prompt.index("CHAPTER ENTITY FACTS - SOURCE-DERIVED") < prompt.index(
        ctx_marker)
    assert prompt.index(ctx_marker) < prompt.index(audit_marker)
    assert "<PAIR id=\"p00000\"" in prompt
    assert "<PAIR id=\"p00001\"" in prompt
    # instructions mention CONTEXT_ONLY as a concept, but the block marker is
    # only emitted when context_pairs are provided
    no_ctx = render_chunked_audit_prompt(
        chunk_id="0001", audit_pairs=[_pair("p00001")],
    )
    assert ctx_marker not in no_ctx


def test_render_chunked_audit_prompt_single_chunk_header() -> None:
    prompt = render_chunked_audit_prompt(
        chunk_id="0001", audit_pairs=[_pair("p00001")],
    )
    assert "AUDIT_PAIRS:\n" in prompt
    assert "of 1" not in prompt


def test_prompt_v41_has_no_pact_chapter_0001_leakage() -> None:
    """Test leakage (concept §9.3): the v4.1 prompt examples must be neutral,
    never Pact chapter 0001 content (Blake's bike / motorcycle evidence, the
    'Slyshala?' example from p00163, the 'Ten'/'Desyati' dialogue, etc.)."""
    instructions = QWEN_AUDIT_V4_1.instructions
    for leaked in (
        "Blake", "motorcycle", "p00007", "Slyshala", "Desyati",
        "youngest", "grandchild", "preoccupied", "embroidered",
        "the nurse", "девяти", "wannabe-architect", "поглощена",
        "Bonds 1.1", "Two past twelve",
    ):
        assert leaked not in instructions, f"Pact-0001 leakage in v4.1 prompt: {leaked!r}"


def test_prompt_v41_has_no_procedural_gender_check() -> None:
    """v4.1 semantics: NO procedural 'MANDATORY GENDER CHECK' rule (v4.2)."""
    instructions = QWEN_AUDIT_V4_1.instructions
    assert "MANDATORY GENDER CHECK" not in instructions
    assert "Identify every human referent in SOURCE." not in instructions


# ---------------------------------------------------------------------------
# Lifecycle wiring (B1 integration: LifecycleQwenAuditEvaluator chunked path)
# ---------------------------------------------------------------------------


class _FakeRouter:
    """Minimal router stub: records ensure_resident, never starts servers."""

    base_url = "http://127.0.0.1:1"  # never contacted (backend replaced)

    def __init__(self) -> None:
        self.resident_calls: List[str] = []

    def ensure_resident(self, model_key: str):
        self.resident_calls.append(model_key)


def test_lifecycle_qwen_audit_routes_pairs_to_chunked_path() -> None:
    from pact_v4.runtime.model_lifecycle_adapters import LifecycleQwenAuditEvaluator

    router = _FakeRouter()
    evaluator = LifecycleQwenAuditEvaluator(router, model_name="qwen-3.6-35b")
    captured: dict = {}

    class _DummyChunked:
        def __call__(self, **kwargs):
            captured.update(kwargs)
            return ChunkedAuditOutcome(
                schema="pact-audit/v4", harness_version="4.1",
                prompt_version="pact-v4-reviewer-qwen-audit/v4.1",
                model="qwen", reasoning_budget=8192, max_input_tokens=3600,
                max_tokens=12000, overlap_tokens=400,
                narrator_context=False, entity_context=False,
                chunk_count=1, successful_chunks=1, failed_chunks=(),
                audit_complete=True, issue_count=0, issues=(),
                chunks=({"chunk": 1, "status": "GOOD"},),
            )

    evaluator._chunked = _DummyChunked()  # type: ignore[attr-defined]
    pairs = [_big_pair(f"p{i:05d}") for i in range(1, 5)]
    out = evaluator(
        chunk_id="0001", pairs=pairs,
        narrator_context="narrator: X (gender male)",
        entity_context="- entity: Y",
    )
    assert isinstance(out, ChunkedAuditOutcome)
    assert router.resident_calls == ["qwen"]
    assert captured["chapter_id"] == "0001"
    assert captured["pairs"] is pairs
    assert "narrator: X" in captured["narrator_context"]
    assert "- entity: Y" in captured["entity_context"]


def test_lifecycle_qwen_audit_context_size_is_49152() -> None:
    from pact_v4.runtime.model_lifecycle_adapters import LifecycleQwenAuditEvaluator

    router = _FakeRouter()
    evaluator = LifecycleQwenAuditEvaluator(router, model_name="qwen-3.6-35b")
    descriptor = evaluator._backend.descriptor
    assert descriptor.effective_options["context_size"] == 49152


def test_lifecycle_qwen_audit_legacy_call_requires_source_and_translation() -> None:
    from pact_v4.runtime.model_lifecycle_adapters import LifecycleQwenAuditEvaluator

    router = _FakeRouter()
    evaluator = LifecycleQwenAuditEvaluator(router, model_name="qwen-3.6-35b")
    with pytest.raises(TypeError):
        evaluator(chunk_id="0001")  # neither pairs nor source/translation
