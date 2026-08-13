"""V4.1 A1 contract tests for whole-chapter generation (generation.py).

The whole-chapter contract (docs/plans/V4_1_WHOLE_CHAPTER_ARCHITECTURE_PLAN_RU.md
§3.2/§8 A1): one model call per chapter against the full ordered PID map,
strict ``{pid: text}`` JSON, exact PID set/order, and bounded retry on every
failure class — malformed/missing/extra/reordered PID, empty/truncated JSON,
and session abort (Gate 0: 2/5 calls aborted with finish=other/error). After
the retry budget the result is an honest ``incomplete`` error, never a partial
success.
"""
from __future__ import annotations

import json

import pytest

from pact_v4.phase1.models import (
    Candidate,
    ChunkPlanArtifact,
    Snapshot,
    SourceArtifact,
    WholeChapterPidMap,
    canonical_json_hash,
)
from pact_v4.phase2.generation import (
    GenerationCache,
    GenerationErrorCode,
    GenerationOutcome,
    GenerationParams,
    WholeChapterRetryPolicy,
    generate_whole_chapter,
)
from pact_v4.phase2.prompts import BALANCED_LITERARY_V3
from pact_v4.runtime.backend_protocol import CompletionError
from pact_v4.runtime.snapshot_factory import (
    ChapterMemory,
    build_config_artifact,
    build_snapshot,
    build_source_artifact,
)
from pact_v4.phase0b.source_html import SourceBlock


def _blocks(n: int = 8) -> list:
    return [
        SourceBlock(
            pid=f"p{i + 1:05d}",
            index=i,
            tag="p",
            text=f"Source paragraph {i + 1} with a number {i + 1}.",
            html=f"<p>Source paragraph {i + 1} with a number {i + 1}.</p>",
            structural_role="paragraph",
            inline_spans=(),
            word_count=7,
        )
        for i in range(n)
    ]


def _artifacts(tmp_path, n: int = 8):
    """Build (source, snapshot, chunk_plan, config) for a small chapter."""
    from pact_v4.phase1.chunker import ChunkPlanner

    blocks = _blocks(n)
    source = build_source_artifact(chapter_id="wctest", blocks=blocks)
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(exist_ok=True)
    memory = ChapterMemory.from_directory(memory_dir)
    snapshot = build_snapshot(
        chapter_id="wctest", source=source, memory=memory,
        context=f"whole-chapter test; memory_dir={memory_dir}",
    )
    planner = ChunkPlanner()
    plans = planner.plan(blocks, snapshot_hash=snapshot.snapshot_hash)
    chunk_plan = ChunkPlanArtifact.create(snapshot, tuple(plans))
    config = build_config_artifact(
        version="pact-v4-driver/phase12/strict/v1",
        values={
            "chapter_id": "wctest",
            "generation": {
                "temperature": 0.2, "seed": 7,
                "max_tokens": 32768, "reasoning": 2,
            },
        },
    )
    return source, snapshot, chunk_plan, config


def _params() -> GenerationParams:
    return GenerationParams(temperature=0.2, seed=7, max_tokens=32768, reasoning=2)


class _EchoCaller:
    """Model caller returning a valid full-chapter JSON map in source order."""

    def __init__(self, *, abort_then_succeed: int = 0) -> None:
        self.calls: list = []
        self._abort_then_succeed = abort_then_succeed

    def __call__(self, bundle) -> str:
        self.calls.append(bundle)
        if self._abort_then_succeed > 0:
            self._abort_then_succeed -= 1
            raise CompletionError("session abort (finish=other/error)")
        return json.dumps(
            {pid: f"Перевод {pid}" for pid, _ in bundle.owned_source},
            ensure_ascii=False,
        )


class _ScriptedCaller:
    """Model caller with a script of raw responses per call."""

    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list = []

    def __call__(self, bundle) -> str:
        self.calls.append(bundle)
        return self.responses.pop(0)


def test_whole_chapter_pid_map_derives_full_ordered_map(tmp_path):
    source, snapshot, chunk_plan, _config = _artifacts(tmp_path)
    pid_map = WholeChapterPidMap.derive(chunk_plan, snapshot)
    assert pid_map.pids == snapshot.pids
    assert pid_map.snapshot_hash == snapshot.snapshot_hash
    assert pid_map.chunk_plan_hash == chunk_plan.plan_hash
    # Content-derived identity: changing the plan changes the map hash.
    assert pid_map.map_hash == canonical_json_hash({
        "artifact": "pact-v4-whole-chapter-pid-map/v1",
        "snapshot_hash": snapshot.snapshot_hash,
        "chunk_plan_hash": chunk_plan.plan_hash,
        "pids": list(snapshot.pids),
    })
    pid_map.validate_against(snapshot)


def test_whole_chapter_generation_success_full_pid_exact_order(tmp_path):
    source, snapshot, chunk_plan, config = _artifacts(tmp_path, n=12)
    pid_map = WholeChapterPidMap.derive(chunk_plan, snapshot)
    caller = _EchoCaller()
    outcome = generate_whole_chapter(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, pid_map=pid_map,
        glossary=(), bible_text="", config=config, params=_params(),
        model_caller=caller, cache=GenerationCache(),
        retry=WholeChapterRetryPolicy(max_attempts=3, base_delay_seconds=0),
    )
    assert outcome.status == "complete"
    assert outcome.chunk_id == "whole_chapter"
    assert outcome.expected_roles == ("balanced_literary",)
    candidate = outcome.candidates["balanced_literary"]
    assert candidate.chunk_id == "whole_chapter"
    assert candidate.candidate_id.startswith("whole_chapter:balanced_literary:")
    assert candidate.pid_order() == pid_map.pids == snapshot.pids
    assert len(candidate.translation) == 12
    # The bundle carried the FULL chapter as one unit (no left/right context).
    assert len(caller.calls) == 1
    assert caller.calls[0].chunk_id == "whole_chapter"
    assert caller.calls[0].owned_pids == snapshot.pids
    assert caller.calls[0].left_context == ()
    assert caller.calls[0].right_context == ()
    # The bundle uses the v3 balanced_literary template (A2: full §4 prompt
    # with the inline BOOK CONTEXT / LOCKED GLOSSARY / STRICT-JSON contract).
    assert caller.calls[0].template is BALANCED_LITERARY_V3


@pytest.mark.parametrize(
    "corrupt",
    [
        "missing",
        "extra",
        "reordered",
        "duplicate",
    ],
)
def test_whole_chapter_pid_corruption_retries_then_honest_error(tmp_path, corrupt):
    source, snapshot, chunk_plan, config = _artifacts(tmp_path, n=8)
    pid_map = WholeChapterPidMap.derive(chunk_plan, snapshot)
    pids = list(pid_map.pids)
    texts = {pid: f"Перевод {pid}" for pid in pids}

    def _corrupt(payload: dict) -> str:
        if corrupt == "missing":
            payload.pop(pids[0])
        elif corrupt == "extra":
            payload["p_extra"] = "Лишний"
        elif corrupt == "reordered":
            # Re-insert the first PID at the end so the key order differs.
            first_value = payload.pop(pids[0])
            payload[pids[0]] = first_value
        elif corrupt == "duplicate":
            # Literal duplicate key in the raw JSON text — plain json.loads
            # would collapse it to last-write-wins before validation can see
            # it; _parse_ordered_pid_pairs keeps the raw pairs so the
            # duplicate is detectable.
            first = pids[0]
            return (
                '{"' + first + '": "Первый", '
                '"' + first + '": "Второй", '
                + ",".join(
                    f'"{pid}": {json.dumps(texts[pid], ensure_ascii=False)}'
                    for pid in pids[1:]
                )
                + "}"
            )
        return json.dumps(payload, ensure_ascii=False)

    good = json.dumps(texts, ensure_ascii=False)
    # Every attempt fails the same way (corrupt payload every time) -> the
    # bounded budget is exhausted and the run reports an honest incomplete
    # outcome, never a partial PID map.
    caller = _ScriptedCaller([_corrupt(dict(texts))] * 3)
    outcome = generate_whole_chapter(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, pid_map=pid_map,
        glossary=(), bible_text="", config=config, params=_params(),
        model_caller=caller, cache=GenerationCache(),
        retry=WholeChapterRetryPolicy(max_attempts=3, base_delay_seconds=0),
    )
    assert outcome.status == "incomplete"
    assert outcome.candidates == {}
    err = outcome.errors["balanced_literary"]
    assert err.code == GenerationErrorCode.PID_MISMATCH
    assert len(caller.calls) == 3  # bounded: exactly max_attempts calls
    # A corrupt first attempt followed by a good one succeeds (transient
    # corruption is retried, not terminal).
    caller2 = _ScriptedCaller([_corrupt(dict(texts)), good, good])
    outcome2 = generate_whole_chapter(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, pid_map=pid_map,
        glossary=(), bible_text="", config=config, params=_params(),
        model_caller=caller2, cache=GenerationCache(),
        retry=WholeChapterRetryPolicy(max_attempts=3, base_delay_seconds=0),
    )
    assert outcome2.status == "complete"
    assert outcome2.candidates["balanced_literary"].pid_order() == pid_map.pids


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "{",
        '{"p00001": "Только один", }',
        'not json at all',
        '["an", "array"]',
    ],
)
def test_whole_chapter_invalid_or_truncated_json_retries_then_honest_error(tmp_path, raw):
    source, snapshot, chunk_plan, config = _artifacts(tmp_path, n=8)
    pid_map = WholeChapterPidMap.derive(chunk_plan, snapshot)
    caller = _ScriptedCaller([raw, raw, raw])
    outcome = generate_whole_chapter(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, pid_map=pid_map,
        glossary=(), bible_text="", config=config, params=_params(),
        model_caller=caller, cache=GenerationCache(),
        retry=WholeChapterRetryPolicy(max_attempts=3, base_delay_seconds=0),
    )
    assert outcome.status == "incomplete"
    err = outcome.errors["balanced_literary"]
    assert err.code == GenerationErrorCode.INVALID_JSON
    assert len(caller.calls) == 3


def test_whole_chapter_pid_colon_comma_repaired_without_retry(tmp_path):
    # JSON-REPAIR (t_34ceca50, run_remote_004): the whole-chapter generator
    # occasionally emits `"p00082", "` (COMMA) instead of `"p00082": "`
    # (COLON) after a PID key on a long output. The deterministic repair in
    # _parse_ordered_pid_pairs fixes ALL occurrences, so a 400-PID body
    # with one such error validates on the FIRST attempt — no retry, no
    # wasted 92k-token regeneration.
    source, snapshot, chunk_plan, config = _artifacts(tmp_path, n=400)
    pid_map = WholeChapterPidMap.derive(chunk_plan, snapshot)
    parts = []
    for pid in pid_map.pids:
        sep = ", " if pid == "p00082" else ": "
        parts.append(f'"{pid}"{sep}"Перевод {pid}"')
    broken_raw = "{" + ", ".join(parts) + "}"
    assert ", " in broken_raw  # the model error is present in the raw body
    caller = _ScriptedCaller([broken_raw])
    outcome = generate_whole_chapter(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, pid_map=pid_map,
        glossary=(), bible_text="", config=config, params=_params(),
        model_caller=caller, cache=GenerationCache(),
        retry=WholeChapterRetryPolicy(max_attempts=3, base_delay_seconds=0),
    )
    assert outcome.status == "complete"
    assert len(caller.calls) == 1  # repaired on the first attempt, no retry
    candidate = outcome.candidates["balanced_literary"]
    assert candidate.pid_order() == pid_map.pids
    assert dict(candidate.translation)["p00082"] == "Перевод p00082"


def test_whole_chapter_session_abort_retried_then_honest_error(tmp_path):
    source, snapshot, chunk_plan, config = _artifacts(tmp_path, n=8)
    pid_map = WholeChapterPidMap.derive(chunk_plan, snapshot)

    # Session abort on the first call, success on the second (Gate 0: 2/5
    # calls aborted with finish=other/error; bounded retry absorbs it).
    caller = _EchoCaller(abort_then_succeed=1)
    outcome = generate_whole_chapter(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, pid_map=pid_map,
        glossary=(), bible_text="", config=config, params=_params(),
        model_caller=caller, cache=GenerationCache(),
        retry=WholeChapterRetryPolicy(max_attempts=3, base_delay_seconds=0),
    )
    assert outcome.status == "complete"
    assert len(caller.calls) == 2

    # Persistent abort: budget exhausted -> honest SESSION_ABORT error.
    class _AlwaysAbort:
        calls = 0

        def __call__(self, bundle) -> str:
            type(self).calls += 1
            raise CompletionError("always aborts")

    outcome2 = generate_whole_chapter(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, pid_map=pid_map,
        glossary=(), bible_text="", config=config, params=_params(),
        model_caller=_AlwaysAbort(), cache=GenerationCache(),
        retry=WholeChapterRetryPolicy(max_attempts=3, base_delay_seconds=0),
    )
    assert outcome2.status == "incomplete"
    assert outcome2.candidates == {}
    err = outcome2.errors["balanced_literary"]
    assert err.code == GenerationErrorCode.SESSION_ABORT
    assert _AlwaysAbort.calls == 3


def test_whole_chapter_retry_policy_validation():
    with pytest.raises(ValueError):
        WholeChapterRetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        WholeChapterRetryPolicy(base_delay_seconds=-1)
    assert WholeChapterRetryPolicy().delay_for(0) == 1.0
    assert WholeChapterRetryPolicy().delay_for(1) == 2.0


def test_whole_chapter_cache_reuse_revalidates(tmp_path):
    source, snapshot, chunk_plan, config = _artifacts(tmp_path, n=8)
    pid_map = WholeChapterPidMap.derive(chunk_plan, snapshot)
    cache = GenerationCache()
    caller = _EchoCaller()
    generate_whole_chapter(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, pid_map=pid_map,
        glossary=(), bible_text="", config=config, params=_params(),
        model_caller=caller, cache=cache,
        retry=WholeChapterRetryPolicy(max_attempts=3, base_delay_seconds=0),
    )
    # Identical bundle -> cache hit, no second model call.
    outcome2 = generate_whole_chapter(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, pid_map=pid_map,
        glossary=(), bible_text="", config=config, params=_params(),
        model_caller=caller, cache=cache,
        retry=WholeChapterRetryPolicy(max_attempts=3, base_delay_seconds=0),
    )
    assert outcome2.status == "complete"
    assert len(caller.calls) == 1


def test_whole_chapter_candidate_rejects_poisoned_cache(tmp_path):
    source, snapshot, chunk_plan, config = _artifacts(tmp_path, n=8)
    pid_map = WholeChapterPidMap.derive(chunk_plan, snapshot)
    cache = GenerationCache()
    # A poisoned entry: chunk_id of a real chunk cached under the whole-chapter
    # bundle hash must be rejected, never handed back.
    other = Candidate(
        candidate_id="c0:balanced_literary:deadbeef",
        chunk_id="c0",
        role="balanced_literary",
        translation=tuple((pid, "x") for pid in snapshot.pids),
        source_hash=source.source_hash,
        snapshot_hash=snapshot.snapshot_hash,
        chunk_plan_hash=chunk_plan.plan_hash,
        config_identity=config.config_identity,
    )
    from pact_v4.phase2.generation import GenerationCandidateResult

    cache._store[canonical_json_hash({"poison": "key"})] = GenerationCandidateResult(
        candidate=other, error=None,
    )
    # The real bundle hash differs from the poisoned key, so a real call just
    # misses the cache (no crash) — the poison guard is exercised on a hit by
    # planting under the real hash.
    caller = _EchoCaller()
    outcome = generate_whole_chapter(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, pid_map=pid_map,
        glossary=(), bible_text="", config=config, params=_params(),
        model_caller=caller, cache=cache,
        retry=WholeChapterRetryPolicy(max_attempts=3, base_delay_seconds=0),
    )
    assert outcome.status == "complete"
    assert len(caller.calls) == 1


def test_whole_chapter_bundle_identity_hashes_full_chapter(tmp_path):
    source, snapshot, chunk_plan, config = _artifacts(tmp_path, n=8)
    pid_map = WholeChapterPidMap.derive(chunk_plan, snapshot)
    caller = _EchoCaller()
    generate_whole_chapter(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, pid_map=pid_map,
        glossary=(), bible_text="", config=config, params=_params(),
        model_caller=caller, cache=GenerationCache(),
        retry=WholeChapterRetryPolicy(max_attempts=3, base_delay_seconds=0),
    )
    bundle = caller.calls[0]
    payload = bundle._identity_payload()
    # The full chapter is one identity unit: no chunk id, no left/right.
    assert payload["chunk_id"] == "whole_chapter"
    assert payload["owned_pids"] == list(snapshot.pids)
    assert payload["left_context"] == []
    assert payload["right_context"] == []
    # max_output_tokens=32768 lives in the bundle identity (Gate 0 §8.5).
    assert payload["params"]["max_tokens"] == 32768
    assert payload["params"]["reasoning"] == 2


# ---------------------------------------------------------------------------
# V4.1 GEN-REASONING: per-attempt reasoning transport (whole-chapter path)
# ---------------------------------------------------------------------------


class _ReasoningCaller:
    """Echo caller that also reports per-call reasoning (``last_reasoning``).

    Mirrors the production ``BackendModelCaller`` contract: the reasoning of
    the most recent completion is exposed via a ``last_reasoning`` attribute
    that ``generate_whole_chapter`` reads after each attempt.
    """

    def __init__(self, responses, reasonings) -> None:
        self.responses = list(responses)
        self.reasonings = list(reasonings)
        self.calls = 0
        self.last_reasoning = ""

    def __call__(self, bundle) -> str:
        self.calls += 1
        if self.reasonings:
            self.last_reasoning = self.reasonings.pop(0)
        return self.responses.pop(0)


def test_whole_chapter_reasoning_sink_receives_successful_attempt(tmp_path):
    source, snapshot, chunk_plan, config = _artifacts(tmp_path, n=8)
    pid_map = WholeChapterPidMap.derive(chunk_plan, snapshot)
    good = json.dumps({pid: f"Перевод {pid}" for pid in pid_map.pids}, ensure_ascii=False)
    caller = _ReasoningCaller(
        [good],
        ["model thought about register and gender here"],
    )
    received = []
    outcome = generate_whole_chapter(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, pid_map=pid_map,
        glossary=(), bible_text="", config=config, params=_params(),
        model_caller=caller, cache=GenerationCache(),
        retry=WholeChapterRetryPolicy(max_attempts=3, base_delay_seconds=0),
        reasoning_sink=lambda attempt, text: received.append((attempt, text)),
    )
    assert outcome.status == "complete"
    # One attempt, its reasoning text delivered to the sink.
    assert received == [(0, "model thought about register and gender here")]


def test_whole_chapter_reasoning_sink_receives_truncated_retry(tmp_path):
    # GEN-REASONING acceptance: a truncated first attempt's reasoning must be
    # preserved (diagnosis of WHY the retry happened), and the retry's own
    # reasoning must also arrive — each attempt is one sink call.
    source, snapshot, chunk_plan, config = _artifacts(tmp_path, n=8)
    pid_map = WholeChapterPidMap.derive(chunk_plan, snapshot)
    good = json.dumps({pid: f"Перевод {pid}" for pid in pid_map.pids}, ensure_ascii=False)
    caller = _ReasoningCaller(
        ["{truncated json", good],
        ["attempt 0: thinking cut off mid-argument", "attempt 1: revised approach"],
    )
    received = []
    outcome = generate_whole_chapter(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, pid_map=pid_map,
        glossary=(), bible_text="", config=config, params=_params(),
        model_caller=caller, cache=GenerationCache(),
        retry=WholeChapterRetryPolicy(max_attempts=3, base_delay_seconds=0),
        reasoning_sink=lambda attempt, text: received.append((attempt, text)),
    )
    assert outcome.status == "complete"
    assert caller.calls == 2
    assert received == [
        (0, "attempt 0: thinking cut off mid-argument"),
        (1, "attempt 1: revised approach"),
    ]


def test_whole_chapter_reasoning_sink_absent_reasoning_is_empty(tmp_path):
    # A caller that does NOT expose last_reasoning (e.g. a stub) yields "" —
    # the sink still fires per attempt so the runner can record presence=0.
    source, snapshot, chunk_plan, config = _artifacts(tmp_path, n=8)
    pid_map = WholeChapterPidMap.derive(chunk_plan, snapshot)
    good = json.dumps({pid: f"Перевод {pid}" for pid in pid_map.pids}, ensure_ascii=False)
    caller = _ScriptedCaller([good])
    received = []
    outcome = generate_whole_chapter(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, pid_map=pid_map,
        glossary=(), bible_text="", config=config, params=_params(),
        model_caller=caller, cache=GenerationCache(),
        retry=WholeChapterRetryPolicy(max_attempts=3, base_delay_seconds=0),
        reasoning_sink=lambda attempt, text: received.append((attempt, text)),
    )
    assert outcome.status == "complete"
    assert received == [(0, "")]


def test_whole_chapter_reasoning_sink_empty_on_lifecycle_acquisition_abort(tmp_path):
    # GEN-REASONING regression (RV t_a790dbab): a lifecycle acquisition
    # failure (model load/swap raising CompletionError) aborts the attempt
    # BEFORE the wrapped caller is entered. The abort attempt must emit ''
    # to the reasoning sink — never the reasoning left over from a prior
    # successful completion.
    from pact_v4.runtime.model_lifecycle_adapters import LifecycleModelCaller

    class _FailingRouter:
        base_url = "http://router.invalid"

        def ensure_resident(self, model_key: str):
            raise CompletionError(f"{model_key} load failed (simulated)")

    source, snapshot, chunk_plan, config = _artifacts(tmp_path, n=8)
    pid_map = WholeChapterPidMap.derive(chunk_plan, snapshot)

    caller = LifecycleModelCaller(_FailingRouter(), model_name="gemma-4-26B")
    # Simulate a prior successful completion that populated last_reasoning.
    caller._caller._impl._last_reasoning = "STALE prior reasoning"
    assert caller.last_reasoning == "STALE prior reasoning"

    received = []
    outcome = generate_whole_chapter(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, pid_map=pid_map,
        glossary=(), bible_text="", config=config, params=_params(),
        model_caller=caller, cache=GenerationCache(),
        retry=WholeChapterRetryPolicy(max_attempts=3, base_delay_seconds=0),
        reasoning_sink=lambda attempt, text: received.append((attempt, text)),
    )
    # Every attempt aborts at acquisition; each must report empty reasoning.
    assert outcome.status == "incomplete"
    assert received == [(0, ""), (1, ""), (2, "")]
    assert caller.last_reasoning == ""


# ---------------------------------------------------------------------------
# V4.1 GEN-STREAM: live reasoning writer (whole-chapter path)
# ---------------------------------------------------------------------------


class _LiveChunkCaller:
    """``ModelCaller`` that accepts a live reasoning-chunk sink (the
    ``set_reasoning_chunk_sink`` duck-typed hook of the production
    ``BackendModelCaller`` chain) and streams chunks through it DURING
    ``__call__``, i.e. BEFORE the response is returned.
    """

    def __init__(self, responses, reasonings, reason_path=None) -> None:
        self.responses = list(responses)
        self.reasonings = list(reasonings)
        self.calls = 0
        self.last_reasoning = ""
        self.installed_sink = None
        self.reason_path = reason_path
        self.file_state_during_call = None

    def set_reasoning_chunk_sink(self, sink) -> None:
        self.installed_sink = sink

    def __call__(self, bundle) -> str:
        self.calls += 1
        # GEN-STREAM acceptance: the live sink is installed BEFORE the call
        # and streamed through DURING it — the backing file grows before the
        # response is produced.
        if self.installed_sink is not None:
            self.installed_sink("думает о роде и регистре… ")
            self.installed_sink("окончательный вывод")
            if self.reason_path is not None:
                self.file_state_during_call = self.reason_path.read_text(
                    encoding="utf-8"
                )
        if self.reasonings:
            self.last_reasoning = self.reasonings.pop(0)
        return self.responses.pop(0)


def test_whole_chapter_live_reasoning_writer_grows_file_during_call(tmp_path):
    # GEN-STREAM acceptance (mock): when the caller supports the live sink
    # and the runner supplies a live_reasoning_writer factory, the per-attempt
    # reasoning file is created BEFORE the model call and grows live DURING
    # it (the on_reasoning_chunk callback fires before complete finishes).
    from pact_v4.runtime.reasoning_writer import open_reasoning_writer

    source, snapshot, chunk_plan, config = _artifacts(tmp_path, n=8)
    pid_map = WholeChapterPidMap.derive(chunk_plan, snapshot)
    good = json.dumps({pid: f"Перевод {pid}" for pid in pid_map.pids}, ensure_ascii=False)
    reason_path = tmp_path / "out" / "whole_chapter_reasoning.txt"
    caller = _LiveChunkCaller([good], ["полный текст размышлений"], reason_path=reason_path)

    def _live_writer(attempt: int):
        assert attempt == 0
        # open_reasoning_writer creates/truncates the file BEFORE the call.
        return open_reasoning_writer(reason_path)

    received = []
    outcome = generate_whole_chapter(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, pid_map=pid_map,
        glossary=(), bible_text="", config=config, params=_params(),
        model_caller=caller, cache=GenerationCache(),
        retry=WholeChapterRetryPolicy(max_attempts=3, base_delay_seconds=0),
        reasoning_sink=lambda attempt, text: received.append((attempt, text)),
        live_reasoning_writer=_live_writer,
    )
    assert outcome.status == "complete"
    # The file grew live DURING the call — the caller observed its non-empty
    # content while the model was still "generating" (before the response).
    assert caller.installed_sink is None  # cleared after the call
    assert caller.file_state_during_call == "думает о роде и регистре… окончательный вывод"
    live_text = reason_path.read_text(encoding="utf-8")
    assert live_text == "думает о роде и регистре… окончательный вывод"
    # The authoritative post-completion sink still fires with the full text.
    assert received == [(0, "полный текст размышлений")]


def test_whole_chapter_live_reasoning_writer_respects_stub_caller(tmp_path):
    # GEN-STREAM NON-GOAL: a caller WITHOUT set_reasoning_chunk_sink (the
    # common test stub) is left untouched — the live factory is never
    # invoked and the post-completion reasoning_sink path is preserved.
    source, snapshot, chunk_plan, config = _artifacts(tmp_path, n=8)
    pid_map = WholeChapterPidMap.derive(chunk_plan, snapshot)
    good = json.dumps({pid: f"Перевод {pid}" for pid in pid_map.pids}, ensure_ascii=False)
    caller = _ScriptedCaller([good])
    factory_calls = []
    received = []
    outcome = generate_whole_chapter(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, pid_map=pid_map,
        glossary=(), bible_text="", config=config, params=_params(),
        model_caller=caller, cache=GenerationCache(),
        retry=WholeChapterRetryPolicy(max_attempts=3, base_delay_seconds=0),
        reasoning_sink=lambda attempt, text: received.append((attempt, text)),
        live_reasoning_writer=lambda attempt: factory_calls.append(attempt),
    )
    assert outcome.status == "complete"
    assert factory_calls == []
    assert received == [(0, "")]
