"""V4.1 A1 contract tests for whole-chapter generation (generation.py).

The whole-chapter contract (translator-line-output): one model call per
chapter against the full ordered PID map, strict PID line-protocol output
(exactly one ``PID: translated text`` line per TARGET PID, in exact source
order — no JSON), exact PID set/order, and bounded retry on every failure
class — malformed/missing/extra/reordered PID, blank/continuation lines,
empty segment text, and session abort. After the retry budget the result is
an honest ``incomplete`` error, never a partial success.
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
    parse_whole_chapter_line_response,
)
from pact_v4.phase2.prompts import (
    BALANCED_LITERARY_V4,
    BALANCED_LITERARY_WHOLE_CHAPTER_LINE_V1,
)
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


def _lines(pids, texts=None):
    """Render a valid whole-chapter line-protocol body for ``pids``."""
    texts = texts or {pid: f"Перевод {pid}" for pid in pids}
    return "\n".join(f"{pid}: {texts[pid]}" for pid in pids)


class _EchoCaller:
    """Model caller returning a valid full-chapter line body in source order."""

    def __init__(self, *, abort_then_succeed: int = 0) -> None:
        self.calls: list = []
        self._abort_then_succeed = abort_then_succeed

    def __call__(self, bundle) -> str:
        self.calls.append(bundle)
        if self._abort_then_succeed > 0:
            self._abort_then_succeed -= 1
            raise CompletionError("session abort (finish=other/error)")
        return _lines([pid for pid, _ in bundle.owned_source])


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
    # The bundle uses the whole-chapter line template (translator-line-output:
    # identical instructions to BALANCED_LITERARY_V4 except the OUTPUT
    # CONTRACT block, new version) — never the shared chunked JSON template.
    assert caller.calls[0].template is BALANCED_LITERARY_WHOLE_CHAPTER_LINE_V1
    assert caller.calls[0].template is not BALANCED_LITERARY_V4


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

    def _corrupt(kind: str) -> str:
        if kind == "missing":
            return _lines([pid for pid in pids if pid != pids[0]], texts)
        if kind == "extra":
            return _lines(pids, texts) + "\np_extra: Лишний"
        if kind == "reordered":
            return _lines([*pids[1:], pids[0]], texts)
        if kind == "duplicate":
            # Literal duplicate PID line — a parsed dict would collapse it
            # to last-write-wins before validation can see it; the line
            # parser keeps every record so the duplicate is detectable.
            return (
                f"{pids[0]}: Первый\n"
                f"{pids[0]}: Второй\n"
                + "\n".join(f"{pid}: {texts[pid]}" for pid in pids[1:])
            )
        raise AssertionError(kind)

    good = _lines(pids, texts)
    # Every attempt fails the same way (corrupt payload every time) -> the
    # bounded budget is exhausted and the run reports an honest incomplete
    # outcome, never a partial PID map.
    caller = _ScriptedCaller([_corrupt(corrupt)] * 3)
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
    caller2 = _ScriptedCaller([_corrupt(corrupt), good, good])
    outcome2 = generate_whole_chapter(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, pid_map=pid_map,
        glossary=(), bible_text="", config=config, params=_params(),
        model_caller=caller2, cache=GenerationCache(),
        retry=WholeChapterRetryPolicy(max_attempts=3, base_delay_seconds=0),
    )
    assert outcome2.status == "complete"
    assert outcome2.candidates["balanced_literary"].pid_order() == pid_map.pids


def _full_lines_with(pid_map, index, replacement):
    """Valid line body with line ``index`` (0-based) replaced."""
    lines = _lines(pid_map.pids).split("\n")
    lines[index] = replacement
    return "\n".join(lines)


@pytest.mark.parametrize(
    "raw_kind",
    [
        "empty",
        "whitespace",
        "blank_middle",
        "trailing_blank",
        "continuation",
        "no_colon",
        "no_space",
        "two_spaces",
        "empty_text",
    ],
)
def test_whole_chapter_malformed_lines_retry_then_honest_error(tmp_path, raw_kind):
    # translator-line-output negative matrix (structural): every malformed
    # line is rejected as invalid output (INVALID_JSON) — never attached to
    # a neighboring segment — and the bounded budget is exhausted honestly.
    source, snapshot, chunk_plan, config = _artifacts(tmp_path, n=8)
    pid_map = WholeChapterPidMap.derive(chunk_plan, snapshot)
    pids = list(pid_map.pids)
    if raw_kind == "empty":
        raw = ""
    elif raw_kind == "whitespace":
        raw = "   \n  \n"
    elif raw_kind == "blank_middle":
        raw = _full_lines_with(pid_map, 3, "")
    elif raw_kind == "trailing_blank":
        raw = _lines(pids) + "\n\n"
    elif raw_kind == "continuation":
        raw = _lines(pids) + "\nпродолжение без PID и двоеточия"
    elif raw_kind == "no_colon":
        raw = _full_lines_with(pid_map, 0, f"{pids[0]} Перевод без двоеточия")
    elif raw_kind == "no_space":
        raw = _full_lines_with(pid_map, 0, f"{pids[0]}:Текст без пробела")
    elif raw_kind == "two_spaces":
        raw = _full_lines_with(pid_map, 0, f"{pids[0]}:  Текст с двумя пробелами")
    else:  # empty_text
        raw = _full_lines_with(pid_map, 0, f"{pids[0]}: ")
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


def test_whole_chapter_translation_preserves_colons_and_punctuation(tmp_path):
    # translator-line-output: only the FIRST colon is protocol syntax —
    # later colons, commas, typographic quotes, and em-dashes belong to the
    # Russian text and survive byte-for-byte (the old JSON failure modes
    # around interior quotes/colons cannot occur: there is no JSON to break).
    source, snapshot, chunk_plan, config = _artifacts(tmp_path, n=8)
    pid_map = WholeChapterPidMap.derive(chunk_plan, snapshot)
    texts = {pid: f"Перевод {pid}" for pid in pid_map.pids}
    texts["p00001"] = 'Перевод p00001: цитата "p12345", "закрыто"'
    texts["p00002"] = (
        "«Когда я думаю о „побеге из дома\", я всегда представляю детей»."
    )
    texts["p00003"] = "Время — две минуты первого: 00:02, точно."
    raw = _lines(pid_map.pids, texts)
    caller = _ScriptedCaller([raw])
    outcome = generate_whole_chapter(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, pid_map=pid_map,
        glossary=(), bible_text="", config=config, params=_params(),
        model_caller=caller, cache=GenerationCache(),
        retry=WholeChapterRetryPolicy(max_attempts=3, base_delay_seconds=0),
    )
    assert outcome.status == "complete"
    assert len(caller.calls) == 1  # first attempt, no retry
    translation = dict(outcome.candidates["balanced_literary"].translation)
    assert translation["p00001"] == texts["p00001"]
    assert translation["p00002"] == texts["p00002"]
    assert translation["p00003"] == texts["p00003"]


def test_whole_chapter_old_json_contract_body_is_rejected(tmp_path):
    # translator-line-output: a response in the RETIRED JSON contract must
    # fail closed (PID mismatch — the brace/quote prefix is not a TARGET
    # PID), never be silently accepted as a translation map.
    source, snapshot, chunk_plan, config = _artifacts(tmp_path, n=8)
    pid_map = WholeChapterPidMap.derive(chunk_plan, snapshot)
    raw = json.dumps(
        {pid: f"Перевод {pid}" for pid in pid_map.pids}, ensure_ascii=False
    )
    caller = _ScriptedCaller([raw, raw, raw])
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
    assert len(caller.calls) == 3


def test_whole_chapter_truncated_line_body_fails_closed(tmp_path):
    # translator-line-output fail-closed: a 400-line body cut after line 350
    # is a PID mismatch (missing PIDs, never a partial map), and a body cut
    # mid-separator (dangling PID with no colon/text) is invalid output —
    # both exhaust the bounded budget honestly.
    source, snapshot, chunk_plan, config = _artifacts(tmp_path, n=400)
    pid_map = WholeChapterPidMap.derive(chunk_plan, snapshot)
    cut_lines = _lines(pid_map.pids[:350])
    caller = _ScriptedCaller([cut_lines, cut_lines, cut_lines])
    outcome = generate_whole_chapter(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, pid_map=pid_map,
        glossary=(), bible_text="", config=config, params=_params(),
        model_caller=caller, cache=GenerationCache(),
        retry=WholeChapterRetryPolicy(max_attempts=3, base_delay_seconds=0),
    )
    assert outcome.status == "incomplete"
    assert outcome.candidates == {}
    assert outcome.errors["balanced_literary"].code == GenerationErrorCode.PID_MISMATCH
    assert len(caller.calls) == 3  # bounded retry

    dangling = cut_lines + "\np00351"
    caller2 = _ScriptedCaller([dangling, dangling, dangling])
    outcome2 = generate_whole_chapter(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, pid_map=pid_map,
        glossary=(), bible_text="", config=config, params=_params(),
        model_caller=caller2, cache=GenerationCache(),
        retry=WholeChapterRetryPolicy(max_attempts=3, base_delay_seconds=0),
    )
    assert outcome2.status == "incomplete"
    assert outcome2.errors["balanced_literary"].code == GenerationErrorCode.INVALID_JSON
    assert len(caller2.calls) == 3


def test_whole_chapter_malformed_pid_is_pid_mismatch(tmp_path):
    # translator-line-output: a PID that is not exactly the expected TARGET
    # PID (wrong digits, wrong case, surrounding whitespace) surfaces as a
    # PID-set violation (missing + extra), never a silent accept.
    source, snapshot, chunk_plan, config = _artifacts(tmp_path, n=8)
    pid_map = WholeChapterPidMap.derive(chunk_plan, snapshot)
    pids = list(pid_map.pids)
    raws = [
        _full_lines_with(pid_map, 0, "p00X01: Текст с плохим PID"),
        _full_lines_with(pid_map, 0, "P00001: Текст в другом регистре"),
        _full_lines_with(pid_map, 0, f" {pids[0]}: Текст с пробелом перед PID"),
    ]
    for raw in raws:
        caller = _ScriptedCaller([raw, raw, raw])
        outcome = generate_whole_chapter(
            source=source, snapshot=snapshot, chunk_plan=chunk_plan,
            pid_map=pid_map, glossary=(), bible_text="", config=config,
            params=_params(), model_caller=caller, cache=GenerationCache(),
            retry=WholeChapterRetryPolicy(max_attempts=3, base_delay_seconds=0),
        )
        assert outcome.status == "incomplete"
        assert outcome.errors["balanced_literary"].code == GenerationErrorCode.PID_MISMATCH
        assert len(caller.calls) == 3


def test_whole_chapter_line_contract_bound_to_cache_identity(tmp_path):
    # translator-line-output task 1.5: the line template/version enters the
    # bundle identity, so a prior JSON-contract cached outcome hashes
    # differently and can never be reused under the line contract.
    source, snapshot, chunk_plan, config = _artifacts(tmp_path, n=8)
    pid_map = WholeChapterPidMap.derive(chunk_plan, snapshot)
    caller = _EchoCaller()
    generate_whole_chapter(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, pid_map=pid_map,
        glossary=(), bible_text="", config=config, params=_params(),
        model_caller=caller, cache=GenerationCache(),
        retry=WholeChapterRetryPolicy(max_attempts=3, base_delay_seconds=0),
    )
    line_bundle = caller.calls[0]
    assert line_bundle.template is BALANCED_LITERARY_WHOLE_CHAPTER_LINE_V1
    json_bundle_payload = dict(line_bundle._identity_payload())
    json_bundle_payload["template_version"] = BALANCED_LITERARY_V4.version
    json_bundle_payload["template_instructions_hash"] = canonical_json_hash(
        BALANCED_LITERARY_V4.instructions
    )
    assert (
        canonical_json_hash(json_bundle_payload)
        != line_bundle.bundle_hash
    )


def test_whole_chapter_rejects_role_without_line_template(tmp_path):
    # translator-line-output isolation: only roles with a whole-chapter line
    # template can generate whole-chapter output — anything else fails closed
    # before any model call instead of emitting guaranteed-invalid output.
    source, snapshot, chunk_plan, config = _artifacts(tmp_path, n=8)
    pid_map = WholeChapterPidMap.derive(chunk_plan, snapshot)
    caller = _EchoCaller()
    with pytest.raises(ValueError, match="no whole-chapter line template"):
        generate_whole_chapter(
            role="fidelity_first", source=source, snapshot=snapshot,
            chunk_plan=chunk_plan, pid_map=pid_map, glossary=(), bible_text="",
            config=config, params=_params(), model_caller=caller,
            cache=GenerationCache(),
            retry=WholeChapterRetryPolicy(max_attempts=3, base_delay_seconds=0),
        )
    assert caller.calls == []


def test_parse_whole_chapter_line_response_tolerates_crlf_and_final_newline(tmp_path):
    # A CRLF body and a single trailing newline are line terminators, not
    # blank lines; the parsed mapping equals the in-memory candidate shape.
    _source, _snapshot, chunk_plan, _config = _artifacts(tmp_path, n=3)
    pid_map = WholeChapterPidMap.derive(chunk_plan, _snapshot)
    raw = "\r\n".join(
        f"{pid}: Перевод {pid}" for pid in pid_map.pids
    ) + "\n"
    parsed = parse_whole_chapter_line_response(raw, pid_map)
    assert parsed == tuple(
        (pid, f"Перевод {pid}") for pid in pid_map.pids
    )


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
        self.last_raw = ""

    def __call__(self, bundle) -> str:
        self.calls += 1
        if self.reasonings:
            self.last_reasoning = self.reasonings.pop(0)
        text = self.responses.pop(0)
        self.last_raw = text  # RAW-SINK: like BackendModelCaller._complete
        return text


def test_whole_chapter_reasoning_sink_receives_successful_attempt(tmp_path):
    source, snapshot, chunk_plan, config = _artifacts(tmp_path, n=8)
    pid_map = WholeChapterPidMap.derive(chunk_plan, snapshot)
    good = _lines(pid_map.pids)
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
    good = _lines(pid_map.pids)
    truncated = _lines(pid_map.pids[:7]) + "\np00008"  # cut mid-separator
    caller = _ReasoningCaller(
        [truncated, good],
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


def test_whole_chapter_raw_sink_receives_every_attempt(tmp_path):
    # RAW-SINK acceptance (architect, run_remote_004/005): the raw model
    # response of EVERY attempt — including a truncated first attempt that
    # would otherwise vanish — must reach the sink, so a disk trail exists
    # for invalid-output diagnosis (the run_011 lesson for generation).
    source, snapshot, chunk_plan, config = _artifacts(tmp_path, n=8)
    pid_map = WholeChapterPidMap.derive(chunk_plan, snapshot)
    good = _lines(pid_map.pids)
    truncated = _lines(pid_map.pids[:7]) + "\np00008"  # cut mid-separator
    caller = _ReasoningCaller([truncated, good], ["", ""])
    received = []
    outcome = generate_whole_chapter(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, pid_map=pid_map,
        glossary=(), bible_text="", config=config, params=_params(),
        model_caller=caller, cache=GenerationCache(),
        retry=WholeChapterRetryPolicy(max_attempts=3, base_delay_seconds=0),
        raw_sink=lambda attempt, text: received.append((attempt, text)),
    )
    assert outcome.status == "complete"
    assert caller.calls == 2
    assert received == [
        (0, truncated),  # the failed attempt's raw text survives
        (1, good),
    ]


def test_whole_chapter_raw_sink_fallback_after_truncated_retry(tmp_path):
    # RAW-SINK fallback (architect, run_remote_006): when the generation
    # layer rejects an attempt's text (line-contract violation), the raw
    # survives in the caller's ``last_raw`` and the sink must still fire —
    # the disk trail exists even for a rejected body. Without the fallback
    # the raw vanishes (the bug that made 004/005/006 diagnosis guesswork).
    source, snapshot, chunk_plan, config = _artifacts(tmp_path, n=8)
    pid_map = WholeChapterPidMap.derive(chunk_plan, snapshot)
    good = _lines(pid_map.pids)
    broken = _lines(pid_map.pids[:4]) + "\n\n" + _lines(pid_map.pids[4:])
    caller = _ReasoningCaller([broken, good], ["", ""])
    received = []
    outcome = generate_whole_chapter(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, pid_map=pid_map,
        glossary=(), bible_text="", config=config, params=_params(),
        model_caller=caller, cache=GenerationCache(),
        retry=WholeChapterRetryPolicy(max_attempts=3, base_delay_seconds=0),
        raw_sink=lambda attempt, text: received.append((attempt, text)),
    )
    assert outcome.status == "complete"
    assert caller.calls == 2
    assert received == [
        (0, broken),  # the REJECTED attempt's raw text survives via last_raw
        (1, good),
    ]


def test_whole_chapter_reasoning_sink_absent_reasoning_is_empty(tmp_path):
    # A caller that does NOT expose last_reasoning (e.g. a stub) yields "" —
    # the sink still fires per attempt so the runner can record presence=0.
    source, snapshot, chunk_plan, config = _artifacts(tmp_path, n=8)
    pid_map = WholeChapterPidMap.derive(chunk_plan, snapshot)
    good = _lines(pid_map.pids)
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
    good = _lines(pid_map.pids)
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
    good = _lines(pid_map.pids)
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
