"""V4.2 R: Russian-only editor stage tests (card t_4707e6e5, contract v4.2-R1).

Covers: the 25-edit run_010 scenario reproduction (SAFE auto-applied with
the diff-gate, REVIEW never applied), chunking (50 PIDs + CONTEXT_ONLY),
fail-closed parsing (original mismatch / unknown class / no-op diff-gate),
context-only pid edits dropped per-edit with a WARNING (R-PID-SCOPE), and
the evaluator over a scripted in-memory backend
(0 real Qwen calls anywhere in this file).
"""
from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Optional, Sequence

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
    edits, errors, _ = parse_editor_edits(
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


def test_parse_drops_context_only_pid_with_warning() -> None:
    """R-PID-SCOPE (t_db376195, owner contract 2026-08-13): an edit for a
    pid that is NOT owned by the current chunk (a CONTEXT_ONLY pid given
    for continuity, run_remote_007 chunk5 p00195, or a completely foreign
    pid) is dropped per-edit with a WARNING — never a structural error: the
    chunk stays GOOD, the owned edits survive, the dropped edit is never
    applied and never forwarded to another chunk."""
    current = {"p00001": "текст"}
    edits, errors, warnings = parse_editor_edits(
        _ok([_edit("p00099", "текст", "другой", "r", "typo")]),
        ["p00001"], current,
    )
    assert edits == ()
    assert errors == ()
    assert len(warnings) == 1
    assert "p00099" in warnings[0]
    assert "not in the current chunk" in warnings[0]


def test_parse_owned_plus_context_edit_keeps_owned() -> None:
    """R-PID-SCOPE acceptance: a chunk whose edit list mixes ONE owned pid
    edit with ONE context-only pid edit is GOOD — the owned edit is
    accepted, the context edit is dropped with a WARNING and never applied
    (edit_count=1, warning_count=1)."""
    current = {"p00001": "текст один", "p00002": "текст два"}
    edits, errors, warnings = parse_editor_edits(
        _ok([
            _edit("p00001", "текст один", "текст один испр", "r", "typo"),
            _edit("p00002", "текст два", "текст два испр", "r", "typo"),
        ]),
        ["p00001"], current,  # chunk owns ONLY p00001; p00002 is context
    )
    assert errors == ()
    assert [e.pid for e in edits] == ["p00001"]
    assert len(warnings) == 1
    assert "p00002" in warnings[0]
    # The context-only edit is NOT applied and NOT forwarded anywhere.
    assert all(e.pid != "p00002" for e in edits)


def test_parse_all_context_edits_chunk_stays_good() -> None:
    """R-PID-SCOPE acceptance: a chunk whose edits are ALL context-only
    pids is GOOD with 0 edits and warning_count=N — NOT FAILED."""
    current = {"p00001": "текст один", "p00002": "текст два"}
    edits, errors, warnings = parse_editor_edits(
        _ok([
            _edit("p00002", "текст два", "текст два испр", "r", "typo"),
            _edit("p00003", "нет", "такого", "r", "typo"),
        ]),
        ["p00001"], current,  # chunk owns ONLY p00001; both edits are foreign
    )
    assert edits == ()
    assert errors == ()
    assert len(warnings) == 2
    assert all("not in the current chunk" in w for w in warnings)


def test_parse_out_of_scope_unknown_class_fails_closed() -> None:
    """RV t_f4111b48: an out-of-scope (context-only) pid edit carrying an
    UNKNOWN class is a structural error — the scope check must not mask
    malformed fields. errors, NO warning, whole chunk FAILED."""
    current = {"p00001": "текст один", "p00002": "текст два"}
    edits, errors, warnings = parse_editor_edits(
        _ok([_edit("p00002", "текст два", "текст два испр", "r", "bogus")]),
        ["p00001"], current,  # chunk owns ONLY p00001; p00002 is context
    )
    assert edits == ()
    assert any("unknown edit class" in e for e in errors)
    assert warnings == ()


def test_parse_out_of_scope_missing_or_non_string_original_fails_closed() -> None:
    """RV t_f4111b48: a foreign pid edit with a MISSING or NON-STRING
    original is fail-closed (errors, no warning) — never a WARNING drop."""
    current = {"p00001": "текст"}
    # missing original
    edits, errors, warnings = parse_editor_edits(
        _ok([{"pid": "p99999", "rewritten": "другой", "reason": "r",
              "class": "typo"}]),
        ["p00001"], current,
    )
    assert edits == ()
    assert any("original is missing or not a string" in e for e in errors)
    assert warnings == ()
    # non-string original
    edits, errors, warnings = parse_editor_edits(
        _ok([{"pid": "p99999", "original": 123, "rewritten": "другой",
              "reason": "r", "class": "typo"}]),
        ["p00001"], current,
    )
    assert edits == ()
    assert any("original is missing or not a string" in e for e in errors)
    assert warnings == ()


def test_parse_out_of_scope_non_substring_original_dropped_per_edit() -> None:
    """R-SUBSTRING-DROP (owner 2026-08-15): an imprecise original on a
    KNOWN pid text is a per-edit WARNING drop — the chunk stays GOOD and
    the OTHER valid edits of the chunk survive (previously: whole chunk
    failed)."""
    current = {"p00001": "текст один", "p00002": "текст два"}
    edits, errors, warnings = parse_editor_edits(
        _ok([
            _edit("p00001", "текст один", "текст один испр", "r", "typo"),
            _edit("p00002", "совсем другой текст", "фикс", "r", "typo"),
        ]),
        ["p00001", "p00002"], current,
    )
    assert errors == ()
    assert len(edits) == 1  # the valid owned edit survives
    assert edits[0].pid == "p00001"
    assert any("not a substring" in w for w in warnings)


def test_parse_mixed_owned_valid_foreign_malformed_fails_closed() -> None:
    """RV t_f4111b48: ONE malformed out-of-scope edit in a chunk that also
    carries a VALID owned edit fails the WHOLE chunk — the valid owned edit
    is NOT retained (edits == (), errors present). R-SUBSTRING-DROP does
    NOT soften this: unknown class stays a whole-chunk failure."""
    current = {"p00001": "текст один", "p00002": "текст два"}
    edits, errors, warnings = parse_editor_edits(
        _ok([
            _edit("p00001", "текст один", "текст один испр", "r", "typo"),
            _edit("p99999", "текст два", "текст два испр", "r", "bogus"),
        ]),
        ["p00001"], current,
    )
    assert edits == ()
    assert any("unknown edit class" in e for e in errors)
    assert warnings == ()


def test_parse_rejects_original_mismatch_dropped_per_edit() -> None:
    current = {"p00001": "текст А"}
    edits, errors, warnings = parse_editor_edits(
        _ok([_edit("p00001", "текст Б", "текст В", "r", "typo")]),
        ["p00001"], current,
    )
    assert errors == ()
    assert edits == ()  # the bad edit is dropped, not applied
    assert any("not a substring" in w for w in warnings)


def test_parse_rejects_original_leading_trailing_whitespace_dropped_per_edit() -> None:
    # RV fd7ee8e + R-FIX2: verbatim substring — ' текст А ' must NOT match
    # 'текст А' (leading/trailing whitespace is not a verbatim substring).
    # R-SUBSTRING-DROP (2026-08-15): a whitespace-wrapped original is now a
    # per-edit WARNING drop, not a whole-chunk failure.
    current = {"p00001": "текст А"}
    edits, errors, warnings = parse_editor_edits(
        _ok([_edit("p00001", " текст А ", "текст Б", "r", "typo")]),
        ["p00001"], current,
    )
    assert errors == ()
    assert edits == ()
    assert any("not a substring" in w for w in warnings)


def test_parse_rejects_original_trailing_whitespace_dropped_per_edit() -> None:
    # Even a single trailing space is not a verbatim substring — per-edit
    # WARNING drop (R-SUBSTRING-DROP, 2026-08-15).
    current = {"p00001": "текст А"}
    edits, errors, warnings = parse_editor_edits(
        _ok([_edit("p00001", "текст А ", "текст Б", "r", "typo")]),
        ["p00001"], current,
    )
    assert errors == ()
    assert edits == ()
    assert any("not a substring" in w for w in warnings)


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
    edits, errors, _ = parse_editor_edits(
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


def test_parse_rejects_original_not_a_substring_invented_dropped_per_edit() -> None:
    # R-FIX2 + R-SUBSTRING-DROP (2026-08-15): a model-INVENTED original
    # (not a substring of the PID) is dropped per-edit with a WARNING —
    # never applied, but the chunk's OTHER valid edits survive.
    current = {"p00010": "Три этажа, с однокомнатной башней."}
    edits, errors, warnings = parse_editor_edits(
        _ok([
            _edit("p00010", "Совершенно другой текст, которого нет в PID.",
                  "какой-то фикс", "выдумка", "typo"),
            _edit("p00010", "Три этажа, с однокомнатной башней.",
                  "Три этажа, с однокомнатной башней — исправлено.",
                  "грамматика", "grammar"),
        ]),
        ["p00010"], current,
    )
    assert errors == ()
    assert len(edits) == 1  # the valid edit survives
    assert edits[0].klass == "grammar"
    assert any("not a substring" in w for w in warnings)


def test_parse_preserves_exact_rewritten_no_strip() -> None:
    # RV fd7ee8e: an accepted SAFE edit's rewritten is preserved VERBATIM —
    # ' изменён ' must NOT be silently stripped to 'изменён' before
    # auto-apply; route returns the exact rewritten string.
    current = {"p00001": "текст А"}
    edits, errors, _ = parse_editor_edits(
        _ok([_edit("p00001", "текст А", " изменён ", "r", "typo")]),
        ["p00001"], current,
    )
    assert errors == ()
    assert len(edits) == 1
    assert edits[0].original == "текст А"
    assert edits[0].rewritten == " изменён "
    applied, candidates, dropped, _ = route_edits(
        edits, current_by_pid=current
    )
    assert applied == (("p00001", " изменён "),)
    assert candidates == ()
    assert dropped == 0


def test_parse_rejects_unknown_class() -> None:
    current = {"p00001": "текст"}
    edits, errors, _ = parse_editor_edits(
        _ok([_edit("p00001", "текст", "другой", "r", "bogus")]),
        ["p00001"], current,
    )
    assert edits == ()
    assert any("unknown edit class" in e for e in errors)


def test_parse_accepts_duplicate_pid_up_to_cap() -> None:
    """R-RETRY (t_8ab8ab35, owner contract 2026-08-13): a duplicate pid is
    NOT a structural error anymore — the model legitimately returns 2+
    problems for one pid (typo + grammar, run_remote_002 chunk4 p00180).
    Both edits are accepted (different fragments), the chunk stays GOOD."""
    current = {"p00001": "текст один два"}
    edits, errors, warnings = parse_editor_edits(
        _ok([
            _edit("p00001", "текст", "текстИспр", "r", "typo"),
            _edit("p00001", "один два", "один", "r", "grammar"),
        ]),
        ["p00001"], current,
    )
    assert errors == ()
    assert len(edits) == 2
    assert [e.pid for e in edits] == ["p00001", "p00001"]
    assert warnings == ()


def test_parse_drops_over_cap_duplicate_with_warning() -> None:
    """R-RETRY (t_8ab8ab35): the 11th+ edit of the same pid is dropped
    per-edit with a WARNING (journal), never a structural error — the chunk
    stays GOOD (fail-closed preserved only for unknown class / original
    not substring / invalid JSON; a pid outside the chunk is a per-edit
    WARNING drop since R-PID-SCOPE)."""
    current = {"p00001": "текст " + " ".join(f"слово{i}" for i in range(1, 13))}
    edits = [
        _edit("p00001", f"слово{i}", f"слово{i}Испр", "r", "typo")
        for i in range(1, 12)  # 11 edits for ONE pid, cap = 10
    ]
    parsed, errors, warnings = parse_editor_edits(
        _ok(edits), ["p00001"], current,
    )
    assert errors == ()
    assert len(parsed) == 10  # cap: 10 accepted
    assert len(warnings) == 1  # 11th dropped with WARNING, not an error
    assert "dropped" in warnings[0] and "MAX_EDITS_PER_PID" in warnings[0]
    # max_edits_per_pid is configurable (identity-bearing knob).
    parsed2, errors2, warnings2 = parse_editor_edits(
        _ok(edits), ["p00001"], current, max_edits_per_pid=12,
    )
    assert errors2 == ()
    assert len(parsed2) == 11
    assert warnings2 == ()



def test_parse_rejects_non_json_and_missing_edits() -> None:
    edits, errors, _ = parse_editor_edits("not json", ["p00001"], {"p00001": "x"})
    assert edits == ()
    assert any("not valid JSON" in e for e in errors)
    edits, errors, _ = parse_editor_edits(
        json.dumps({"issues": []}), ["p00001"], {"p00001": "x"}
    )
    assert edits == ()
    assert any("no 'edits' array" in e for e in errors)


def test_parse_accepts_fenced_json() -> None:
    # RESILIENCE (t_406fc48c, run_remote_001 chunk1): the R model wrapped
    # its edits response in ```json fences, and the phase failed with
    # 'response is not valid JSON: Expecting value: line 1 column 1'. The
    # tolerant parse must accept the fence-wrapped body.
    payload = {
        "edits": [
            {
                "pid": "p00070",
                "original": "«Не важно».",
                "rewritten": "«Неважно».",
                "reason": "В значении «не имеет значения» наречие пишется слитно.",
                "class": "typo",
            },
        ]
    }
    fenced = "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
    edits, errors, _ = parse_editor_edits(
        fenced, ["p00070"], {"p00070": "«Не важно»."}
    )
    assert not errors, f"unexpected errors: {errors}"
    assert len(edits) == 1
    assert edits[0].pid == "p00070"
    assert edits[0].klass == "typo"


def test_parse_accepts_prose_wrapped_json() -> None:
    # Prose around the JSON block ('Here is the JSON: {...}').
    payload = {
        "edits": [
            {
                "pid": "p00001",
                "original": "Перевод номер1 номер1",
                "rewritten": "Перевод номер1",
                "reason": "дубль",
                "class": "duplicate",
            },
        ]
    }
    prose = "Here is the JSON: " + json.dumps(payload, ensure_ascii=False)
    edits, errors, _ = parse_editor_edits(
        prose, ["p00001"], {"p00001": "Перевод номер1 номер1"}
    )
    assert not errors, f"unexpected errors: {errors}"
    assert len(edits) == 1


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
    applied, candidates, dropped, _ = route_edits(
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
    applied, candidates, dropped, _ = route_edits(
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
    applied, candidates, dropped, _ = route_edits(
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
    applied, candidates, dropped, _ = route_edits(
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
    applied, candidates, dropped, _ = route_edits(
        edits, current_by_pid={"p00042": pid_text}
    )
    assert applied == (("p00042", "Он сказал, что придёт позже. Она кивнула."),)
    assert candidates == ()
    assert dropped == 0


def test_route_sequential_safe_apply_same_pid() -> None:
    """R-RETRY (t_8ab8ab35, owner contract 2026-08-13): SAFE edits of the
    SAME pid apply SEQUENTIALLY — the working text updates between edits,
    so a later edit replaces against the ACTUAL current text (run_remote_002
    chunk4 p00180: typo «рубашка» + grammar «острыми» both applied)."""
    pid_text = "рубашка была белой с острыми рукавами"
    edits = (
        EditorEdit("p00180", "рубашка", "рубашкаИспр", "опечатка", "typo"),
        EditorEdit("p00180", "острыми", "острымиИспр", "грамматика", "grammar"),
    )
    applied, candidates, dropped, warnings = route_edits(
        edits, current_by_pid={"p00180": pid_text}
    )
    assert dropped == 0
    assert candidates == ()
    assert warnings == ()
    # One (pid, new_text) per applied SAFE edit: the SECOND edit replaced
    # against the text AFTER the first edit — both fragments survived
    # (non-overlapping), so both are applied and the final text (last
    # entry, what dict(applied) keeps) carries both fixes.
    assert applied == (
        ("p00180", "рубашкаИспр была белой с острыми рукавами"),
        ("p00180", "рубашкаИспр была белой с острымиИспр рукавами"),
    )
    assert dict(applied)["p00180"] == "рубашкаИспр была белой с острымиИспр рукавами"


def test_route_same_pid_fragment_gone_warns_and_skips() -> None:
    """R-RETRY (t_8ab8ab35, owner contract 2026-08-13): if a later edit's
    fragment stopped being a substring after the earlier edits (overlapping
    fragments), it is dropped per-edit with a WARNING — never a structural
    error, the chunk stays GOOD."""
    pid_text = "она сказала что придёт"
    edits = (
        EditorEdit("p00001", "она сказала что", "она сказала, что", "r", "grammar"),
        # second edit's original OVERLAPS the first replace region
        EditorEdit("p00001", "сказала что", "сказала, что", "r", "typo"),
    )
    applied, candidates, dropped, warnings = route_edits(
        edits, current_by_pid={"p00001": pid_text}
    )
    assert applied == (("p00001", "она сказала, что придёт"),)
    assert dropped == 0
    assert len(warnings) == 1
    assert "no longer a substring" in warnings[0]


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
    evaluator = RussianEditorEvaluator(
        backend, config=RussianEditorConfig(retry_max_retries=0)
    )
    outcome = evaluator(chapter_id="0001", translation=translation)
    assert outcome.complete is False
    assert outcome.failed_chunks == (2,)
    # Fail-closed is PER-CHUNK (RESILIENCE t_406fc48c): the good first
    # chunk's edits are collected (outcome.applied) and applied by B3; the
    # failed chunk contributes NO edits. The audit still protects the
    # chapter.
    assert outcome.applied != ()
    assert dict(outcome.applied)["p00001"].startswith("Исправленный текст")
    assert outcome.chunk_count == 2


def test_evaluator_partial_apply_isolates_failed_chunks_5of8() -> None:
    """RESILIENCE acceptance (run_remote_001 shape): with 8 chunks where 2
    fail (broken JSON) and 6 are GOOD carrying 17 edits, the outcome
    carries ONLY the GOOD chunks' edits — a failed chunk contributes none
    (per-chunk fail-closed), so the B3 partial-apply can safely apply the
    successful work. The chunk whose edit names a foreign pid (p99999) is
    GOOD with 0 edits + a WARNING (R-PID-SCOPE) — never applied either."""
    translation = _translation(40)  # 8 chunks of 5 (chunk_size=5)
    backend = _MockBackend(
        # chunk 1 GOOD — 2 SAFE edits
        _ok([
            _edit("p00001", "Русский текст абзаца 1.", "Русский текст абзаца 1 исправлен.",
                  "r", "typo"),
            _edit("p00002", "Русский текст абзаца 2.", "Русский текст абзаца 2 исправлен.",
                  "r", "grammar"),
        ]),
        # chunk 2 FAILED (fence-wrapped JSON — now parseable, kept GOOD for
        # the isolation check below; here we make it fail via broken JSON)
        '{"edits": [',
        # chunk 3 GOOD — 3 SAFE edits
        _ok([
            _edit("p00011", "Русский текст абзаца 11.", "Русский текст абзаца 11 исправлен.",
                  "r", "duplicate"),
            _edit("p00012", "Русский текст абзаца 12.", "Русский текст абзаца 12 исправлен.",
                  "r", "preposition"),
            _edit("p00013", "Русский текст абзаца 13.", "Русский текст абзаца 13 исправлен.",
                  "r", "typo"),
        ]),
        # chunk 4 FAILED (foreign pid)
        _ok([
            _edit("p99999", "не важно", "не важно 2", "r", "typo"),
        ]),
        # chunk 5 GOOD — 4 SAFE edits
        _ok([
            _edit(f"p{i:05d}", f"Русский текст абзаца {i}.",
                  f"Русский текст абзаца {i} исправлен.", "r", "grammar")
            for i in (21, 22, 23, 24)
        ]),
        # chunk 6 FAILED (transport)
        '{"edits": [',
        # chunk 7 GOOD — 5 SAFE edits
        _ok([
            _edit(f"p{i:05d}", f"Русский текст абзаца {i}.",
                  f"Русский текст абзаца {i} исправлен.", "r", "typo")
            for i in (31, 32, 33, 34, 35)
        ]),
        # chunk 8 GOOD — 3 SAFE edits
        _ok([
            _edit(f"p{i:05d}", f"Русский текст абзаца {i}.",
                  f"Русский текст абзаца {i} исправлен.", "r", "duplicate")
            for i in (36, 37, 38)
        ]),
    )
    evaluator = RussianEditorEvaluator(
        backend, config=RussianEditorConfig(chunk_size=5, retry_max_retries=0)
    )
    outcome = evaluator(chapter_id="0001", translation=translation)
    assert outcome.complete is False
    assert outcome.chunk_count == 8
    assert outcome.successful_chunks == 6
    assert outcome.failed_chunks == (2, 6)
    # 2+3+4+5+3 = 17 edits from the GOOD chunks only (chunk 4 is GOOD but
    # its only edit named a foreign pid — dropped, 0 edits).
    assert len(outcome.edits) == 17
    # The foreign-pid edit of chunk 4 is a per-edit WARNING, not an error
    # (R-PID-SCOPE) — the chunk stays GOOD, the edit is never applied.
    assert outcome.warning_count == 1
    applied_pids = {pid for pid, _ in outcome.applied}
    # Failed-chunk pids never appear (chunk2: p00006-p00010, chunk6:
    # p00026-p00030).
    assert not (applied_pids & {"p00006", "p00007", "p00008", "p00009", "p00010"})
    assert not (applied_pids & {"p00026", "p00027", "p00028", "p00029", "p00030"})
    assert not (applied_pids & {"p99999"})
    # GOOD-chunk edits are all applied.
    assert len(applied_pids) == len(outcome.edits)
    assert applied_pids == {
        "p00001", "p00002", "p00011", "p00012", "p00013",
        "p00021", "p00022", "p00023", "p00024",
        "p00031", "p00032", "p00033", "p00034", "p00035",
        "p00036", "p00037", "p00038",
    }


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


def test_evaluator_invented_original_chunk_survives() -> None:
    """R-SUBSTRING-DROP (2026-08-15): a chunk with a model-INVENTED
    original is NOT failed anymore — the bad edit is dropped per-edit with
    a WARNING, the chunk stays GOOD with its other valid edits applied."""
    translation = {"p00010": "Три этажа, с однокомнатной башней."}
    backend = _MockBackend(
        _ok([
            _edit("p00010", "Выдуманный текст без совпадения с PID.",
                  "фикс", "r", "typo"),
            _edit("p00010", "Три этажа, с однокомнатной башней.",
                  "Три этажа, с однокомнатной башней — исправлено.",
                  "грамматика", "grammar"),
        ]),
    )
    evaluator = RussianEditorEvaluator(backend)
    outcome = evaluator(chapter_id="0001", translation=translation)
    assert outcome.complete is True
    assert outcome.failed_chunks == ()
    assert len(outcome.applied) == 1  # the valid grammar edit is applied
    assert outcome.applied[0][0] == "p00010"


def test_evaluator_transport_failure_is_failed_chunk() -> None:
    translation = _translation(10)
    backend = _MockBackend(fail=True)
    evaluator = RussianEditorEvaluator(backend)
    outcome = evaluator(chapter_id="0001", translation=translation)
    assert outcome.complete is False
    assert outcome.failed_chunks == (1,)
    assert outcome.applied == ()


def test_evaluator_transport_retry_recovers() -> None:
    """R-RETRY (t_8ab8ab35, acceptance: run_remote_001 chunk3 empty body):
    a transport failure is retried (bounded) and a later attempt succeeds —
    the chunk is GOOD, not FAILED."""
    translation = _translation(10)
    backend = _FlakyTransportBackend(
        # attempt 1 -> transport error, attempt 2 -> valid empty edits
        [{"raise": "ConnectionError", "text": None},
         {"text": '{"edits": []}'}],
    )
    events: list = []
    evaluator = RussianEditorEvaluator(
        backend, config=RussianEditorConfig(retry_base_delay_seconds=0.0),
        on_chunk_event=lambda kind, fields: events.append((kind, fields)),
    )
    outcome = evaluator(chapter_id="0001", translation=translation)
    assert outcome.complete is True
    assert outcome.failed_chunks == ()
    assert backend.call_count() == 2
    retries = [e for e in events if e[0] == "retry"]
    assert len(retries) == 1
    assert retries[0][1]["attempt"] == 1
    assert "ConnectionError" in retries[0][1]["error"]
    done = [e for e in events if e[0] == "done"]
    assert done and done[0][1]["status"] == "GOOD"


def test_evaluator_invalid_json_retry_recovers() -> None:
    """R-RETRY (t_8ab8ab35, acceptance: run_remote_002 chunk3 empty body /
    run_remote_001 chunk1 truncated): invalid JSON/empty body is retried
    (bounded) and a later attempt succeeds."""
    translation = _translation(10)
    backend = _FlakyTransportBackend(
        [{"text": ""},  # empty body (max_tokens exhausted inside <think>)
         {"text": '{"edits": []}'}],
    )
    events: list = []
    evaluator = RussianEditorEvaluator(
        backend, config=RussianEditorConfig(retry_base_delay_seconds=0.0),
        on_chunk_event=lambda kind, fields: events.append((kind, fields)),
    )
    outcome = evaluator(chapter_id="0001", translation=translation)
    assert outcome.complete is True
    assert backend.call_count() == 2
    retries = [e for e in events if e[0] == "retry"]
    assert len(retries) == 1
    assert "not valid JSON" in retries[0][1]["error"]


def test_evaluator_transport_retry_exhausted_still_fails() -> None:
    """R-RETRY (t_8ab8ab35): after the bounded retries are exhausted the
    chunk stays FAILED — fail-closed is preserved."""
    translation = _translation(10)
    backend = _FlakyTransportBackend(
        [{"raise": "ConnectionError", "text": None}] * 3,
    )
    events: list = []
    evaluator = RussianEditorEvaluator(
        backend, config=RussianEditorConfig(retry_base_delay_seconds=0.0),
        on_chunk_event=lambda kind, fields: events.append((kind, fields)),
    )
    outcome = evaluator(chapter_id="0001", translation=translation)
    assert outcome.complete is False
    assert outcome.failed_chunks == (1,)
    assert backend.call_count() == 3  # 1 + 2 retries
    retries = [e for e in events if e[0] == "retry"]
    assert len(retries) == 2
    done = [e for e in events if e[0] == "done"]
    assert done and done[0][1]["status"] == "FAILED"


def test_evaluator_structural_error_not_retried() -> None:
    """R-RETRY (t_8ab8ab35): a STRUCTURAL error (unknown edit class) is
    NOT retried — it is not randomness, fail-closed as-is (single call)."""
    translation = _translation(10)
    backend = _MockBackend(
        _ok([_edit("p00001", "Русский текст абзаца 1.", "фикс", "r", "нет_такого_класса")]),
    )
    events: list = []
    evaluator = RussianEditorEvaluator(
        backend,
        on_chunk_event=lambda kind, fields: events.append((kind, fields)),
    )
    outcome = evaluator(chapter_id="0001", translation=translation)
    assert outcome.complete is False
    assert outcome.failed_chunks == (1,)
    assert backend.call_count() == 1  # NO retry for structural errors
    assert not [e for e in events if e[0] == "retry"]


def test_evaluator_context_only_edit_dropped_chunk_good() -> None:
    """R-PID-SCOPE acceptance (run_remote_007 chunk5 shape): the model
    edits a CONTEXT_ONLY pid — a pid shown to it for continuity but owned
    by ANOTHER chunk (or foreign). The edit is dropped per-edit with a
    WARNING, the chunk stays GOOD (0 edits + warning_count=1), the outcome
    is complete — never FAILED, and the owned chunk where the pid lives is
    untouched (the edit is not transferred; the model proposes it there)."""
    # chunk_size=1: chunk 1 owns p00001, chunk 2 owns p00002; chunk 2's
    # context_pairs include p00001 (get_editor_overlap walks backwards).
    translation = {"p00001": "текст один", "p00002": "текст два"}
    backend = _MockBackend(
        '{"edits": []}',
        _ok([_edit("p00001", "текст один", "текст один испр", "r", "typo")]),
    )
    events: list = []
    evaluator = RussianEditorEvaluator(
        backend, config=RussianEditorConfig(chunk_size=1),
        on_chunk_event=lambda kind, fields: events.append((kind, fields)),
    )
    outcome = evaluator(chapter_id="0001", translation=translation)
    assert outcome.complete is True
    assert outcome.failed_chunks == ()
    assert outcome.successful_chunks == 2
    assert outcome.edits == ()
    assert outcome.applied == ()
    assert outcome.warning_count == 1
    done = [e for e in events if e[0] == "done"]
    # chunk 2's event: GOOD, 0 edits, 1 warning (the context-only edit).
    assert done[1][1]["status"] == "GOOD"
    assert done[1][1]["edit_count"] == 0
    assert done[1][1]["warning_count"] == 1
    # chunk 1 (the pid's owner) was NOT asked to apply it here — the edit
    # is dropped, never transferred to the owning chunk.
    assert not any("p00001" in (e[1].get("error") or "") for e in done)


def test_evaluator_owned_and_context_mixed_drops_context() -> None:
    """R-PID-SCOPE acceptance: a chunk whose edit list mixes an OWNED pid
    edit with a CONTEXT_ONLY pid edit — the owned edit is applied, the
    context edit is dropped with a WARNING, edit_count=1 warning_count=1."""
    # chunk_size=1: chunk 2 owns p00002; the model returns BOTH an owned
    # edit (p00002) and a context edit (p00001).
    translation = {"p00001": "текст один", "p00002": "текст два"}
    backend = _MockBackend(
        '{"edits": []}',
        _ok([
            _edit("p00001", "текст один", "текст один испр", "r", "typo"),
            _edit("p00002", "текст два", "текст два испр", "r", "typo"),
        ]),
    )
    events: list = []
    evaluator = RussianEditorEvaluator(
        backend, config=RussianEditorConfig(chunk_size=1),
        on_chunk_event=lambda kind, fields: events.append((kind, fields)),
    )
    outcome = evaluator(chapter_id="0001", translation=translation)
    assert outcome.complete is True
    assert outcome.failed_chunks == ()
    assert [e.pid for e in outcome.edits] == ["p00002"]
    assert dict(outcome.applied) == {"p00002": "текст два испр"}
    assert outcome.warning_count == 1
    done = [e for e in events if e[0] == "done"]
    assert done[1][1]["status"] == "GOOD"
    assert done[1][1]["edit_count"] == 1
    assert done[1][1]["warning_count"] == 1


def test_evaluator_mixed_owned_valid_foreign_malformed_chunk_failed() -> None:
    """RV t_f4111b48: a chunk whose edit list mixes a VALID owned edit with
    a MALFORMED foreign-pid edit (unknown class) is FAILED — the whole
    chunk contributes NO edits, the valid owned edit is NOT applied."""
    translation = {
        "p00001": "текст один", "p00002": "текст два",
        "p00003": "текст три", "p00004": "текст четыре",
    }
    backend = _MockBackend(
        _ok([
            _edit("p00001", "текст один", "текст один испр", "r", "typo"),
            _edit("p00003", "текст три", "текст три испр", "r", "bogus"),
        ]),
    )
    events: list = []
    evaluator = RussianEditorEvaluator(
        backend, config=RussianEditorConfig(chunk_size=2),
        on_chunk_event=lambda kind, fields: events.append((kind, fields)),
    )
    outcome = evaluator(chapter_id="0001", translation=translation)
    assert outcome.complete is False
    assert outcome.failed_chunks == (1,)
    assert outcome.edits == ()
    assert outcome.applied == ()
    assert outcome.candidates == ()
    assert outcome.warning_count == 0
    done = [e for e in events if e[0] == "done"]
    assert done[0][1]["status"] == "FAILED"
    assert "unknown edit class" in done[0][1]["error"]
    # chunk 2 (p00003's owner) was never asked to apply the foreign edit —
    # the malformed record failed chunk 1, nothing was transferred.
    assert done[1][1]["status"] == "GOOD"


def test_evaluator_out_of_scope_missing_original_chunk_failed() -> None:
    """RV t_f4111b48: an out-of-scope edit with a MISSING required field
    is fail-closed at the evaluator level — the chunk is FAILED (errors,
    no warning drop), nothing from it is applied."""
    translation = {
        "p00001": "текст один", "p00002": "текст два",
        "p00003": "текст три", "p00004": "текст четыре",
    }
    backend = _MockBackend(
        _ok({"pid": "p00003", "rewritten": "другой", "reason": "r",
             "class": "typo"}),
    )
    evaluator = RussianEditorEvaluator(
        backend, config=RussianEditorConfig(chunk_size=2)
    )
    outcome = evaluator(chapter_id="0001", translation=translation)
    assert outcome.complete is False
    assert outcome.failed_chunks == (1,)
    assert outcome.edits == ()
    assert outcome.applied == ()
    assert outcome.warning_count == 0


class _FlakyTransportBackend(_MockBackend):
    """Scripted backend: each entry is ``{"text": ...}`` or
    ``{"raise": "ConnectionError"}`` — the evaluator's bounded retry should
    recover when a later attempt succeeds."""

    def __init__(self, script: Sequence[Mapping[str, Any]]) -> None:
        super().__init__()
        self._script = list(script)

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        entry = self._script.pop(0) if self._script else {"text": '{"edits": []}'}
        if entry.get("raise"):
            raise ConnectionError(f"simulated {entry['raise']}")
        from pact_v4.runtime.backend_protocol import CompletionResponse as _CR

        return _CR(
            text=entry.get("text") or "",
            model="qwen-3.6-35b",
            finish_reason="stop",
        )


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
    # R-RETRY (t_8ab8ab35): the per-pid cap and the bounded retry policy
    # ride the config payload (identity-bearing, F5).
    assert outcome_payload["max_edits_per_pid"] == 10
    assert outcome_payload["r_editor_retry"] == {
        "max_retries": 2, "base_delay_seconds": 1.0,
    }


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
    evaluator = RussianEditorEvaluator(
        backend, config=RussianEditorConfig(retry_max_retries=0)
    )
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


def test_evaluator_streams_reasoning_live_during_call(tmp_path) -> None:
    """REASONING-STREAM acceptance: the R editor's reasoning file is created
    BEFORE the call and grows live — a scripted backend firing
    on_reasoning_chunk mid-call sees the file already populated, and the
    authoritative post-completion write still carries the full reasoning."""
    observed: Dict[str, str] = {}

    class _StreamingBackend(_MockBackend):
        def complete(self, request: CompletionRequest) -> CompletionResponse:
            self.requests.append(request)
            assert request.on_reasoning_chunk is not None
            request.on_reasoning_chunk("live-")
            request.on_reasoning_chunk("edits")
            observed["during"] = (
                tmp_path / "r_editor_chunk1_reasoning.txt"
            ).read_text(encoding="utf-8")
            return CompletionResponse(
                text='{"edits": []}',
                model="qwen-3.6-35b",
                finish_reason="stop",
                raw_metadata={"reasoning": "full-reasoning"},
            )

    evaluator = RussianEditorEvaluator(_StreamingBackend())
    outcome = evaluator(
        chapter_id="0001",
        translation=_translation(10),
        out_dir=tmp_path, out_base="r_editor",
    )
    assert outcome.complete is True
    assert observed["during"] == "live-edits"
    assert (tmp_path / "r_editor_chunk1_reasoning.txt").read_text(
        encoding="utf-8"
    ) == "full-reasoning"
