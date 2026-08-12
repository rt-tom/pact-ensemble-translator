"""V4.2 R: Russian-only editor stage tests (card t_4707e6e5, contract v4.2-R1).

Covers: the 25-edit run_010 scenario reproduction (SAFE auto-applied with
the diff-gate, REVIEW never applied), chunking (50 PIDs + CONTEXT_ONLY),
fail-closed parsing (unknown pid / original mismatch / unknown class /
no-op diff-gate), and the evaluator over a scripted in-memory backend
(0 real Qwen calls anywhere in this file).
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional, Sequence

import pytest

from pact_v4.audit.russian_editor import (
    ALL_CLASSES,
    DEFAULT_CHUNK_SIZE,
    RUSSIAN_EDITOR_HARNESS_VERSION,
    RUSSIAN_EDITOR_PROMPT_VERSION,
    RUSSIAN_EDITOR_SCHEMA,
    REVIEW_CLASSES,
    SAFE_CLASSES,
    EditorEdit,
    ReviewCandidate,
    RussianEditorConfig,
    RussianEditorEvaluator,
    build_editor_chunks,
    get_editor_overlap,
    parse_editor_edits,
    route_edits,
)
from pact_v4.runtime.backend_protocol import (
    BackendCallRecord,
    BackendDescriptor,
    CompletionBackend,
    CompletionRequest,
    CompletionResponse,
)
from pact_v4.runtime.prompts_runtime import render_russian_editor_prompt


class _MockBackend(CompletionBackend):
    """Scripted backend returning canned edits JSON per call."""

    _BINDINGS = {
        "default": "qwen-3.6-35b",
        "qwen_audit": "qwen-3.6-35b",
        "fidelity_reviewer": "qwen-3.6-35b",
    }

    def __init__(
        self,
        *responses: str,
        fail: bool = False,
    ) -> None:
        self._responses = list(responses)
        self._fail = fail
        self.requests: list = []

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
        if self._fail:
            raise RuntimeError("simulated transport failure")
        text = self._responses.pop(0) if self._responses else '{"edits": []}'
        return CompletionResponse(text=text, model="qwen-3.6-35b", finish_reason="stop")

    def close(self) -> None:
        pass

    def call_records(self) -> Sequence[BackendCallRecord]:
        return []

    def call_count(self) -> int:
        return len(self.requests)

    def prompt_of(self, index: int) -> str:
        return self.requests[index].messages[0].content


def _ok(edits: list) -> str:
    return json.dumps({"edits": edits}, ensure_ascii=False)


def _edit(pid: str, original: str, rewritten: str, reason: str, klass: str) -> dict:
    return {
        "pid": pid, "original": original, "rewritten": rewritten,
        "reason": reason, "class": klass,
    }


def _translation(n: int) -> dict:
    return {f"p{i:05d}": f"Русский текст абзаца {i}." for i in range(1, n + 1)}


# ---------------------------------------------------------------------------
# Chunking + CONTEXT_ONLY (gemma_rewrite_v4.py pattern: chunks of 50)
# ---------------------------------------------------------------------------


def test_build_editor_chunks_fixed_count_50() -> None:
    from pact_v4.audit.russian_editor import TranslationPair

    pairs = [
        TranslationPair(pid=f"p{i:05d}", text=f"текст {i}") for i in range(1, 121)
    ]
    chunks = build_editor_chunks(pairs, chunk_size=DEFAULT_CHUNK_SIZE)
    assert [len(c) for c in chunks] == [50, 50, 20]
    assert chunks[0][0].pid == "p00001"
    assert chunks[1][0].pid == "p00051"


def test_get_editor_overlap_preceding_pairs() -> None:
    from pact_v4.audit.russian_editor import TranslationPair

    pairs = [
        TranslationPair(pid=f"p{i:05d}", text=f"текст {i}") for i in range(1, 20)
    ]
    overlap = get_editor_overlap(pairs, "p00010", max_pairs=6)
    assert [p.pid for p in overlap] == [
        "p00004", "p00005", "p00006", "p00007", "p00008", "p00009",
    ]
    assert get_editor_overlap(pairs, "p00001", max_pairs=6) == []


def test_render_prompt_has_no_english_source() -> None:
    from pact_v4.audit.russian_editor import TranslationPair

    chunk = [TranslationPair(pid="p00001", text="Узы 1.1")]
    ctx = [TranslationPair(pid="p00000", text="предыдущий")]
    prompt = render_russian_editor_prompt(
        chunk_id="0001/chunk1",
        edit_pairs=chunk,
        context_pairs=ctx,
        chunk_index=1,
        chunk_total=3,
    )
    assert "EDIT_PAIRS (chunk 1 of 3):" in prompt
    assert "CONTEXT_ONLY" in prompt
    assert "p00001: Узы 1.1" in prompt
    # The English source must NEVER appear as a source MAP: no <SOURCE>
    # block, no English source text anywhere in the editor prompt (the
    # Russian-only contract — the instructions only say the source is NOT
    # provided).
    assert "<SOURCE>" not in prompt
    assert "The English source is NOT provided" in prompt
    assert "never propose an edit for a context_only pid" in prompt.lower()


# ---------------------------------------------------------------------------
# Parsing (fail-closed)
# ---------------------------------------------------------------------------


def test_parse_valid_edits() -> None:
    current = {"p00001": "Он сказал: «Привет»", "p00002": "Это хорошо хорошо."}
    edits, errors = parse_editor_edits(
        _ok([
            _edit("p00001", "Он сказал: «Привет»", "Он сказал: \"Привет\"",
                  "кавычки", "typo"),
            _edit("p00002", "Это хорошо хорошо.", "Это хорошо.", "дубль",
                  "duplicate"),
        ]),
        ["p00001", "p00002"],
        current,
    )
    assert errors == ()
    assert len(edits) == 2
    assert edits[0].klass == "typo"
    assert edits[1].pid == "p00002"


def test_parse_rejects_unknown_pid_and_context_only() -> None:
    current = {"p00001": "текст"}
    edits, errors = parse_editor_edits(
        _ok([_edit("p00099", "текст", "другой", "r", "typo")]),
        ["p00001"], current,
    )
    assert edits == ()
    assert any("not in the current chunk" in e for e in errors)


def test_parse_rejects_original_mismatch() -> None:
    current = {"p00001": "текст А"}
    edits, errors = parse_editor_edits(
        _ok([_edit("p00001", "текст Б", "текст В", "r", "typo")]),
        ["p00001"], current,
    )
    assert edits == ()
    assert any("not a substring" in e for e in errors)


def test_parse_rejects_original_leading_trailing_whitespace() -> None:
    # RV fd7ee8e + R-FIX2: verbatim substring — ' текст А ' must NOT match
    # 'текст А' (leading/trailing whitespace is not a verbatim substring).
    # The old strip()-based comparison silently accepted a whitespace-wrapped
    # original; strict verbatim fails the WHOLE chunk.
    current = {"p00001": "текст А"}
    edits, errors = parse_editor_edits(
        _ok([_edit("p00001", " текст А ", "текст Б", "r", "typo")]),
        ["p00001"], current,
    )
    assert edits == ()
    assert any("not a substring" in e for e in errors)


def test_parse_rejects_original_trailing_whitespace() -> None:
    # Even a single trailing space is not a verbatim substring.
    current = {"p00001": "текст А"}
    edits, errors = parse_editor_edits(
        _ok([_edit("p00001", "текст А ", "текст Б", "r", "typo")]),
        ["p00001"], current,
    )
    assert edits == ()
    assert any("not a substring" in e for e in errors)


def test_parse_accepts_original_fragment_substring() -> None:
    # R-FIX2 (run_012, p00010-class): the model quotes ONE sentence of a
    # multi-sentence PID as original — a verbatim substring — and the edit
    # is structurally valid (no longer a whole-chunk failure).
    pid_text = (
        "Три этажа, с однокомнатной башней, выступающей на этаж выше "
        "с одного из углов. Стены обшиты серым деревом."
    )
    fragment = "Три этажа, с однокомнатной башней, выступающей на этаж выше с одного из углов."
    current = {"p00010": pid_text}
    edits, errors = parse_editor_edits(
        _ok([
            _edit("p00010", fragment,
                  "Три этажа, с однокомнатной башней, выступающей на один "
                  "этаж выше из одного из углов.",
                  "предлог/уточнение", "grammar"),
        ]),
        ["p00010"], current,
    )
    assert errors == ()
    assert len(edits) == 1
    assert edits[0].original == fragment


def test_parse_rejects_original_not_a_substring_invented() -> None:
    # R-FIX2 fail-closed: a model-INVENTED original (not a substring of the
    # PID) voids the whole chunk — 'not a substring', never applied.
    current = {"p00010": "Три этажа, с однокомнатной башней."}
    edits, errors = parse_editor_edits(
        _ok([
            _edit("p00010", "Совершенно другой текст, которого нет в PID.",
                  "какой-то фикс", "выдумка", "typo"),
        ]),
        ["p00010"], current,
    )
    assert edits == ()
    assert any("not a substring" in e for e in errors)


def test_parse_preserves_exact_rewritten_no_strip() -> None:
    # RV fd7ee8e: an accepted SAFE edit's rewritten is preserved VERBATIM —
    # ' изменён ' must NOT be silently stripped to 'изменён' before
    # auto-apply; route returns the exact rewritten string.
    current = {"p00001": "текст А"}
    edits, errors = parse_editor_edits(
        _ok([_edit("p00001", "текст А", " изменён ", "r", "typo")]),
        ["p00001"], current,
    )
    assert errors == ()
    assert len(edits) == 1
    assert edits[0].original == "текст А"
    assert edits[0].rewritten == " изменён "
    applied, candidates, dropped = route_edits(
        edits, current_by_pid=current
    )
    assert applied == (("p00001", " изменён "),)
    assert candidates == ()
    assert dropped == 0


def test_parse_rejects_unknown_class() -> None:
    current = {"p00001": "текст"}
    edits, errors = parse_editor_edits(
        _ok([_edit("p00001", "текст", "другой", "r", "bogus")]),
        ["p00001"], current,
    )
    assert edits == ()
    assert any("unknown edit class" in e for e in errors)


def test_parse_rejects_duplicate_pid() -> None:
    current = {"p00001": "текст"}
    edits, errors = parse_editor_edits(
        _ok([
            _edit("p00001", "текст", "один", "r", "typo"),
            _edit("p00001", "текст", "два", "r", "grammar"),
        ]),
        ["p00001"], current,
    )
    assert edits == ()
    assert any("duplicate edit pid" in e for e in errors)


def test_parse_rejects_non_json_and_missing_edits() -> None:
    edits, errors = parse_editor_edits("not json", ["p00001"], {"p00001": "x"})
    assert edits == ()
    assert any("not valid JSON" in e for e in errors)
    edits, errors = parse_editor_edits(
        json.dumps({"issues": []}), ["p00001"], {"p00001": "x"}
    )
    assert edits == ()
    assert any("no 'edits' array" in e for e in errors)


# ---------------------------------------------------------------------------
# Routing: SAFE auto-apply with diff-gate, REVIEW never applied
# ---------------------------------------------------------------------------


def test_route_safe_applied_review_candidates() -> None:
    edits = (
        EditorEdit("p00001", "а", "б", "r", "typo"),
        EditorEdit("p00002", "в", "г", "r", "grammar"),
        EditorEdit("p00003", "д", "е", "r", "duplicate"),
        EditorEdit("p00004", "ж", "з", "r", "preposition"),
        EditorEdit("p00005", "и", "й", "r", "calque"),
        EditorEdit("p00006", "к", "л", "r", "logic"),
        EditorEdit("p00007", "м", "н", "r", "ambiguity"),
        EditorEdit("p00008", "о", "п", "r", "unnatural"),
        EditorEdit("p00009", "р", "с", "r", "register"),
    )
    applied, candidates, dropped = route_edits(
        edits, current_by_pid={e.pid: e.original for e in edits}
    )
    assert [p for p, _ in applied] == ["p00001", "p00002", "p00003", "p00004"]
    assert [c.pid for c in candidates] == [
        "p00005", "p00006", "p00007", "p00008", "p00009",
    ]
    assert dropped == 0
    assert all(isinstance(c, ReviewCandidate) for c in candidates)


def test_route_diff_gate_cuts_noop_p00095_false() -> None:
    # run_010 lesson: Qwen proposed an edit equal to the original (the
    # p00095-class false positive). The diff-gate cuts it: never applied,
    # never a candidate.
    edits = (
        EditorEdit("p00095", "Тот же текст", "Тот же текст", "ложное", "typo"),
        EditorEdit("p00001", "а", "б", "r", "grammar"),
    )
    applied, candidates, dropped = route_edits(
        edits,
        current_by_pid={
            "p00095": "Тот же текст",
            "p00001": "а",
        },
    )
    assert applied == (("p00001", "б"),)
    assert candidates == ()
    assert dropped == 1


def test_route_review_noop_also_dropped() -> None:
    edits = (
        EditorEdit("p00005", "одинаково", "одинаково", "noop", "calque"),
    )
    applied, candidates, dropped = route_edits(
        edits, current_by_pid={"p00005": "одинаково"}
    )
    assert applied == ()
    assert candidates == ()
    assert dropped == 1


def test_route_fragment_safe_edit_preserves_rest_of_pid() -> None:
    # R-FIX2 acceptance (run_012 p00010-class): a SAFE edit whose original is
    # a fragment (one sentence of a multi-sentence PID) applies via
    # current.replace(original, rewritten, 1) — ONLY the fragment changes,
    # the rest of the PID text is preserved.
    pid_text = (
        "Три этажа, с однокомнатной башней, выступающей на этаж выше "
        "с одного из углов. Стены обшиты серым деревом."
    )
    fragment = "выступающей на этаж выше с одного из углов"
    edits = (
        EditorEdit("p00010", fragment,
                   "выступающей на один этаж выше из одного из углов",
                   "уточнение/предлог", "grammar"),
    )
    applied, candidates, dropped = route_edits(
        edits, current_by_pid={"p00010": pid_text}
    )
    assert dropped == 0
    assert candidates == ()
    assert applied == ((
        "p00010",
        "Три этажа, с однокомнатной башней, выступающей на один этаж "
        "выше из одного из углов. Стены обшиты серым деревом.",
    ),)


def test_route_fragment_rewritten_full_text_also_works() -> None:
    # R-FIX2: rewritten may itself be the full corrected text — the
    # substring-replace handles it (fragment original + full rewritten).
    pid_text = "Он сказал что придёт позже. Она кивнула."
    edits = (
        EditorEdit("p00042", "Он сказал что придёт позже.",
                   "Он сказал, что придёт позже.",
                   "пунктуация", "typo"),
    )
    applied, candidates, dropped = route_edits(
        edits, current_by_pid={"p00042": pid_text}
    )
    assert applied == (("p00042", "Он сказал, что придёт позже. Она кивнула."),)
    assert candidates == ()
    assert dropped == 0


def test_class_sets_cover_contract() -> None:
    # The contract lists exactly 9 classes: 4 SAFE + 5 REVIEW.
    assert SAFE_CLASSES == frozenset({"typo", "grammar", "duplicate", "preposition"})
    assert REVIEW_CLASSES == frozenset(
        {"calque", "logic", "ambiguity", "unnatural", "register"}
    )
    assert ALL_CLASSES == SAFE_CLASSES | REVIEW_CLASSES
    assert len(ALL_CLASSES) == 9


# ---------------------------------------------------------------------------
# 25-edit run_010 scenario reproduction (acceptance)
# ---------------------------------------------------------------------------


def _run_010_translation() -> dict:
    # A compact stand-in for the run_010 chapter: 25 PIDs with the defects
    # the Russian editor was asked to find on run_010 (typos, grammar,
    # duplicates, prepositions, calques, logic slips, ambiguity, unnatural
    # phrasing, register).
    texts = {
        1: "Он сказал: «Привет» — и улыбнулся.",        # typo: guillemets
        2: "Она пришла домой и и начала готовить.",     # duplicate "и"
        3: "Они пошли в магазин за хлебом.",            # ok
        4: "Я встал рано утром, чтобы успеть на поезд.",  # ok
        5: "Его лицо было бледным, как мел.",           # calque "бледным, как мел" — idiomatic actually
        6: "Она сказала, что это был хороший день.",    # ok
        7: "Мы поехали на дачу в выходные.",            # ok
        8: "Он взял книгу со полки.",                   # preposition: "со полки" -> "с полки"
        9: "Ты прав, это действительно так.",           # grammar/gender (REVIEW in verifier test)
        10: "Они сказали, что придут позже.",           # ok
        11: "Он был очень очень уставшим.",             # duplicate "очень очень"
        12: "Она посмотрела на него и улыбнулась.",     # ok
        13: "Мы должны были уехать еще вчера.",         # ok
        14: "Он любил гулять по парку вечером.",        # ok
        15: "Она не знала, что делать дальше.",         # ok
        16: "Они построили новый дом на окраине.",      # ok
        17: "Я не мог поверить своим ушам.",            # ok
        18: "Он открыл дверь и вошел в комнату.",       # ok
        19: "Она была одета в красное платье.",         # ok
        20: "Мы жили в этом городе десять лет.",        # ok
        21: "Он никогда не опаздывал на работу.",       # ok
        22: "Она умела готовить очень вкусно.",         # ok
        23: "Они решили остаться дома.",                # ok
        24: "Он почувствовал запах кофе.",              # ok
        25: "Она написала письмо своей сестре.",        # ok
    }
    return {f"p{i:05d}": texts[i] for i in sorted(texts)}


def test_run_010_25_edits_scenario_safe_applied_review_not() -> None:
    """Acceptance: the 25-edit run_010 scenario reproduces — SAFE edits are
    auto-applied with the diff-gate, REVIEW edits are never applied and
    become candidates."""
    translation = _run_010_translation()
    pids = list(translation)
    backend = _MockBackend(
        _ok([
            _edit("p00001", translation["p00001"],
                  "Он сказал: \"Привет\" — и улыбнулся.", "кавычки", "typo"),
            _edit("p00002", translation["p00002"],
                  "Она пришла домой и начала готовить.", "дубль «и»", "duplicate"),
            _edit("p00008", translation["p00008"],
                  "Он взял книгу с полки.", "предлог", "preposition"),
            _edit("p00011", translation["p00011"],
                  "Он был очень уставшим.", "дубль", "duplicate"),
            _edit("p00005", translation["p00005"],
                  "Его лицо было белым, как мел.", "калька", "calque"),
            _edit("p00009", translation["p00009"],
                  "Ты права, это действительно так.", "род", "grammar"),
            # p00095-class false positive: a SAFE edit that proposes the
            # SAME text (no-op) — the diff-gate cuts it.
            _edit("p00012", translation["p00012"],
                  translation["p00012"], "ложное", "typo"),
            # A "logic" REVIEW candidate on a PID the editor should not have
            # touched (kept as candidate, never applied).
            _edit("p00003", translation["p00003"],
                  "Они отправились в магазин за хлебом.", "логика", "logic"),
        ]),
    )
    evaluator = RussianEditorEvaluator(backend, config=RussianEditorConfig())
    outcome = evaluator(chapter_id="0001", translation=translation)

    assert outcome.complete is True
    assert outcome.chunk_count == 1
    assert outcome.successful_chunks == 1
    applied = dict(outcome.applied)
    # SAFE edits applied (diff-gated): typo/duplicate/preposition/duplicate.
    assert applied["p00001"].startswith("Он сказал: \"Привет\"")
    assert applied["p00002"] == "Она пришла домой и начала готовить."
    assert applied["p00008"] == "Он взял книгу с полки."
    assert applied["p00011"] == "Он был очень уставшим."
    # REVIEW edits never applied -> candidates.
    by_pid = {c.pid: c for c in outcome.candidates}
    assert "p00005" in by_pid and by_pid["p00005"].klass == "calque"
    assert "p00003" in by_pid and by_pid["p00003"].klass == "logic"
    # p00009 grammar edit: SAFE class (grammar is in SAFE_CLASSES) — applied.
    assert "p00009" in applied and applied["p00009"] == "Ты права, это действительно так."
    assert "p00009" not in by_pid
    # The no-op p00095-class false edit (same text proposed) was cut by the
    # diff-gate.
    assert outcome.dropped == 1
    assert "p00012" not in applied
    # Sanity: total edits parsed = 8, applied 5, candidates 2, dropped 1.
    assert len(outcome.edits) == 8
    assert len(outcome.applied) == 5
    assert len(outcome.candidates) == 2
    assert outcome.dropped == 1


# ---------------------------------------------------------------------------
# Evaluator fail-closed behavior
# ---------------------------------------------------------------------------


def test_evaluator_chunk_failure_marks_incomplete() -> None:
    translation = _translation(60)  # 2 chunks (50 + 10)
    backend = _MockBackend(
        '{"edits": [{"pid": "p00001", "original": "Русский текст абзаца 1.", '
        '"rewritten": "Исправленный текст абзаца 1.", "reason": "r", '
        '"class": "typo"}]}',
        'not-json',  # second chunk invalid -> stage incomplete
    )
    evaluator = RussianEditorEvaluator(backend)
    outcome = evaluator(chapter_id="0001", translation=translation)
    assert outcome.complete is False
    assert outcome.failed_chunks == (2,)
    # The good first chunk's edits are collected for DIAGNOSIS, but the
    # stage is incomplete — the caller (B3) must never apply a partial pass
    # (fail-closed at the stage level; the audit still protects the chapter).
    assert outcome.applied != ()  # diagnostic visibility
    assert outcome.chunk_count == 2


def test_evaluator_fragment_originals_complete_and_preserve_pid() -> None:
    """R-FIX2 acceptance (run_012): a chunk whose edits quote FRAGMENTS of
    their PIDs (one sentence each, verbatim substrings) is GOOD — applied
    SAFE edits change ONLY the quoted fragment, the rest of each PID is
    preserved."""
    pid1 = ("Три этажа, с однокомнатной башней, выступающей на этаж выше "
            "с одного из углов. Стены обшиты серым деревом.")
    pid2 = "Он сказал что придёт позже. Она кивнула."
    translation = {"p00010": pid1, "p00011": pid2}
    backend = _MockBackend(
        _ok([
            _edit("p00010",
                  "Три этажа, с однокомнатной башней, выступающей на этаж "
                  "выше с одного из углов.",
                  "Три этажа, с однокомнатной башней, выступающей на один "
                  "этаж выше из одного из углов.",
                  "уточнение/предлог", "grammar"),
            _edit("p00011", "Он сказал что придёт позже.",
                  "Он сказал, что придёт позже.", "пунктуация", "typo"),
        ]),
    )
    evaluator = RussianEditorEvaluator(backend)
    outcome = evaluator(chapter_id="0001", translation=translation)
    assert outcome.complete is True
    assert outcome.successful_chunks == 1
    assert outcome.failed_chunks == ()
    assert outcome.dropped == 0
    applied = dict(outcome.applied)
    # Fragment replaced, rest of the PID preserved.
    assert applied["p00010"] == (
        "Три этажа, с однокомнатной башней, выступающей на один этаж "
        "выше из одного из углов. Стены обшиты серым деревом."
    )
    assert applied["p00011"] == "Он сказал, что придёт позже. Она кивнула."
    assert applied["p00011"].endswith("Она кивнула.")


def test_evaluator_invented_original_chunk_fails_closed() -> None:
    """R-FIX2 fail-closed: a chunk containing a model-INVENTED original (not
    a substring of the PID) is FAILED — nothing from that chunk is applied."""
    translation = {"p00010": "Три этажа, с однокомнатной башней."}
    backend = _MockBackend(
        _ok([
            _edit("p00010", "Выдуманный текст без совпадения с PID.",
                  "фикс", "r", "typo"),
        ]),
    )
    evaluator = RussianEditorEvaluator(backend)
    outcome = evaluator(chapter_id="0001", translation=translation)
    assert outcome.complete is False
    assert outcome.failed_chunks == (1,)
    assert outcome.applied == ()
    assert outcome.candidates == ()


def test_evaluator_transport_failure_is_failed_chunk() -> None:
    translation = _translation(10)
    backend = _MockBackend(fail=True)
    evaluator = RussianEditorEvaluator(backend)
    outcome = evaluator(chapter_id="0001", translation=translation)
    assert outcome.complete is False
    assert outcome.failed_chunks == (1,)
    assert outcome.applied == ()


def test_evaluator_empty_translation_rejected() -> None:
    backend = _MockBackend()
    evaluator = RussianEditorEvaluator(backend)
    with pytest.raises(ValueError):
        evaluator(chapter_id="0001", translation={})


def test_outcome_payload_roundtrip() -> None:
    outcome_payload = RussianEditorConfig().to_payload()
    assert outcome_payload["chunk_size"] == DEFAULT_CHUNK_SIZE
    assert outcome_payload["safe_classes"] == sorted(SAFE_CLASSES)
    assert outcome_payload["prompt_version"] == RUSSIAN_EDITOR_PROMPT_VERSION
    assert outcome_payload["harness_version"] == RUSSIAN_EDITOR_HARNESS_VERSION
    assert outcome_payload["label"] == "phase3/russian_editor_v4"


def test_module_constants() -> None:
    assert RUSSIAN_EDITOR_SCHEMA == "pact-v4-russian-editor/v1"
    # R-FIX2 (run_012): v3 = substring-original contract (fragment quotes).
    assert RUSSIAN_EDITOR_PROMPT_VERSION == "pact-v4.2-russian-editor/v3"
    assert RUSSIAN_EDITOR_HARNESS_VERSION == "4.2"


def test_prompt_few_shot_example_includes_class() -> None:
    """A3 (run_011): the R prompt carries a few-shot JSON example WITH the
    ``class`` field — the manual 25-edit test showed Qwen without a class
    example omits ``class``, and the fail-closed parser then voids the whole
    chunk ('unknown edit class'). The example must include class explicitly."""
    prompt = render_russian_editor_prompt(
        chunk_id="0001/chunk1",
        edit_pairs=[
            _TranslationPairProxy("p00001", "Он сказал что придёт позже.")
        ],
        chunk_index=1,
        chunk_total=1,
    )
    assert "Example of a valid response" in prompt
    assert '"class": "typo"' in prompt
    assert '"pid": "p00042"' in prompt
    # The example itself is a valid R-parseable edit (round-trip through the
    # strict parser so the few-shot never teaches a malformed shape).
    import json as _json
    import re as _re
    match = _re.search(r"\{.*\}", prompt, _re.S)
    assert match is not None
    example = _json.loads(match.group(0))
    assert example["edits"][0]["class"] == "typo"


def test_prompt_original_fragment_contract_wording() -> None:
    """R-FIX2 (run_012): the prompt tells the model that ``original`` is a
    VERBATIM FRAGMENT of the PID text (one sentence or a shorter span that
    must appear word-for-word) — never a request to echo the whole PID."""
    prompt = render_russian_editor_prompt(
        chunk_id="0001/chunk1",
        edit_pairs=[
            _TranslationPairProxy("p00001", "Он сказал что придёт позже.")
        ],
        chunk_index=1,
        chunk_total=1,
    )
    assert "exact fragment you are fixing" in prompt
    assert "quoted verbatim from the PID text" in prompt
    assert "must appear in the PID text word-for-word" in prompt
    assert "exact current Russian text of that PID" not in prompt


class _TranslationPairProxy:
    def __init__(self, pid: str, text: str) -> None:
        self.pid = pid
        self.text = text


def test_evaluator_writes_raw_and_reasoning_artifacts(tmp_path) -> None:
    """A1 (run_011): the R evaluator persists ``r_editor_chunk{N}_raw.txt`` /
    ``_reasoning.txt`` for EVERY chunk — a parse failure then leaves a disk
    trail (run_011: 7/8 R chunks FAILED with no artifacts, undiagnosable)."""
    from pact_v4.runtime.backend_protocol import CompletionResponse

    class _RawBackend(_MockBackend):
        def __init__(self, *responses: str) -> None:
            super().__init__(*responses)
            self._i = 0

        def complete(self, request: CompletionRequest) -> CompletionResponse:
            self.requests.append(request)
            text = self._responses.pop(0) if self._responses else '{"edits": []}'
            self._i += 1
            return CompletionResponse(
                text=text,
                model="qwen-3.6-35b",
                finish_reason="stop",
                raw_metadata={"reasoning": f"reasoning-for-chunk-{self._i}"},
            )

    translation = _translation(60)  # 2 chunks (50 + 10)
    backend = _RawBackend(
        '{"edits": [{"pid": "p00001", "original": "Русский текст абзаца 1.", '
        '"rewritten": "Исправленный текст абзаца 1.", "reason": "r", '
        '"class": "typo"}]}',
        'not-json',  # second chunk parse-fails but MUST still leave artifacts
    )
    evaluator = RussianEditorEvaluator(backend)
    outcome = evaluator(
        chapter_id="0001", translation=translation,
        out_dir=tmp_path, out_base="r_editor",
    )
    assert outcome.complete is False  # second chunk invalid
    raw1 = tmp_path / "r_editor_chunk1_raw.txt"
    raw2 = tmp_path / "r_editor_chunk2_raw.txt"
    reason1 = tmp_path / "r_editor_chunk1_reasoning.txt"
    reason2 = tmp_path / "r_editor_chunk2_reasoning.txt"
    assert raw1.exists() and raw2.exists()
    assert reason1.exists() and reason2.exists()
    assert "Исправленный текст абзаца 1." in raw1.read_text(encoding="utf-8")
    # The FAILED chunk's raw body is preserved (diagnosis trail).
    assert "not-json" in raw2.read_text(encoding="utf-8")
    assert reason1.read_text(encoding="utf-8") == "reasoning-for-chunk-1"
    assert reason2.read_text(encoding="utf-8") == "reasoning-for-chunk-2"


def test_evaluator_no_out_dir_skips_artifacts() -> None:
    """A1: without ``out_dir`` the R evaluator writes nothing (pure default)."""
    translation = _translation(10)
    backend = _MockBackend(
        '{"edits": [{"pid": "p00001", "original": "Русский текст абзаца 1.", '
        '"rewritten": "Исправленный текст абзаца 1.", "reason": "r", '
        '"class": "typo"}]}'
    )
    evaluator = RussianEditorEvaluator(backend)
    evaluator(chapter_id="0001", translation=translation)
    # no exception, no disk writes required


def test_evaluator_transport_failure_writes_transport_error_artifact(tmp_path) -> None:
    """A1: a transport failure leaves a TRANSPORT_ERROR artifact (the audit
    pattern) so the failed chunk is diagnosable on disk."""
    translation = _translation(10)
    backend = _MockBackend(fail=True)
    evaluator = RussianEditorEvaluator(backend)
    outcome = evaluator(
        chapter_id="0001", translation=translation,
        out_dir=tmp_path, out_base="r_editor",
    )
    assert outcome.complete is False
    raw = tmp_path / "r_editor_chunk1_raw.txt"
    assert raw.exists()
    assert raw.read_text(encoding="utf-8").startswith("TRANSPORT_ERROR:")
