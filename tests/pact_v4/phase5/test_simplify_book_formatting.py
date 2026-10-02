"""simplify-book-formatting: compact request, budget planner, policy, recovery, safety.

Covers OpenSpec tasks 3.1-3.3 target checks (no production pipeline):
- tagged-only compact input, no-tag no-call, 78 PID/102 spans single call,
  budget (not PID-count) splitting;
- resolved role-policy budgeting, missing-policy diagnosed debt without a
  model call, formatting-only zero-reasoning override + transitions;
- length/context split-only-failed-group, valid-partial addressed follow-up,
  bounded transport retry, debt for unsplittable groups, diagnostics;
- exact-substring/occurrence validation, overlap/foreign rejection,
  deterministic wrap byte-preservation, multi-tag PIDs.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pact_v4.phase0b.source_html import SourceBlock, SourceSpan
from pact_v4.phase5.formatting import (
    FORMATTING_POLICY_VERSION,
    formatting_messages,
    parse_format_mappings,
    resolve_format_mappings,
    run_formatting_align,
)
from pact_v4.runtime.runtime_config import (
    LocalModelSpec,
    OutputBudgetPolicy,
    ResolvedModelPair,
    RoleBudget,
)


def _span(sid: str, text: str = "word", tag: str = "em") -> SourceSpan:
    return SourceSpan(span_id=sid, tag=tag, text=text, attrs={}, occurrence=1)


def _block(pid: str, index: int, spans, text: str = "ctx text") -> SourceBlock:
    return SourceBlock(
        pid=pid, index=index, tag="p", text=text, html=f"<p>{text}</p>",
        structural_role="body", inline_spans=tuple(spans),
        word_count=len(text.split()),
    )


class _FakeGen:
    def __init__(self, content: str, finish_reason="stop", usage=None):
        self.content = content
        self.text = content
        self.finish_reason = finish_reason
        self.usage = usage or {}
        self.reasoning = ""
        self.reasoning_content = ""
        self.response_format_attempted = True


class _FakeClient:
    def __init__(self, handler):
        self._handler = handler
        self.calls = []

    def complete(self, messages, cfg, max_tokens, label=None):
        self.calls.append((messages, dict(cfg), max_tokens, label))
        return self._handler(messages, max_tokens, len(self.calls))


def _policy(base=16192, per_span=64, ceiling=32768) -> RoleBudget:
    return RoleBudget(
        max_output_tokens=base,
        output_budget=OutputBudgetPolicy(
            mode="span_formula", base_tokens=base,
            per_span_tokens=per_span, ceiling=ceiling,
        ),
        reasoning_budget=0,
    )


def _chapter_78_102():
    # 78 tagged PIDs holding exactly 102 spans: first 24 PIDs carry 2, rest 1.
    blocks = []
    for i in range(78):
        pid = f"p{i:05d}"
        n = 2 if i < 24 else 1
        spans = tuple(_span(f"em{j:02d}", f"w{i}_{j}") for j in range(n))
        blocks.append(_block(pid, i, spans, text=f"english context {i}"))
    assert sum(len(b.inline_spans) for b in blocks) == 102
    translations = {b.pid: f"русский перевод {b.index} слово" for b in blocks}
    return blocks, translations


def _echo_handler(blocks):
    by_pid = {b.pid: b for b in blocks}

    def _handle(messages, max_tokens, call_no):
        # Echo: map every requested span to a distinct occurrence of a word
        # repeated in its translation (valid non-overlapping bindings).
        user_text = messages[1]["content"] if len(messages) > 1 else messages[0]["content"]
        import html as _html
        import re as _re
        mappings = []
        for item in _re.split(r"<FORMAT_ITEM pid=\"", user_text)[1:]:
            item = _html.unescape(item)
            pid = item.split('"', 1)[0]
            if pid not in by_pid:
                continue
            sids = sorted(set(_re.findall(r'"span_id": "([^"]+)"', item.split("</FORMAT_ITEM>", 1)[0])))
            for occ, sid in enumerate(sids, start=1):
                mappings.append(
                    {"pid": pid, "span_id": sid, "target_text": "перевод", "occurrence": occ}
                )
        return _FakeGen(json.dumps({"mappings": mappings}))
    return _handle


def _repeat_translations(blocks):
    return {b.pid: ("перевод " * (len(b.inline_spans) + 1) + f"хвост {b.index}").strip() for b in blocks}


# ---------------------------------------------------------------------------
# 3.1 compact request + budget planner
# ---------------------------------------------------------------------------


def test_untagged_pids_excluded_no_extra_call(tmp_path: Path):
    tagged = _block("p00001", 0, [_span("em01", "bright")], text="a bright day")
    plain = _block("p00002", 1, [], text="plain text")
    translations = {"p00001": "яркий день", "p00002": "простой текст"}
    client = _FakeClient(lambda m, t, n: _FakeGen(json.dumps({"mappings": [
        {"pid": "p00001", "span_id": "em01", "target_text": "яркий", "occurrence": 1}]})))
    out = resolve_format_mappings(
        client, {"max_tokens": None}, [tagged, plain], translations,
        out_dir=tmp_path, role_policy=_policy(),
    )
    assert out[("p00001", "em01")][0] == "яркий"
    assert len(client.calls) == 1
    user_text = client.calls[0][0][1]["content"]
    assert 'pid="p00001"' in user_text
    assert 'pid="p00002"' not in user_text


def test_no_spans_no_call(tmp_path: Path):
    blocks = [_block("p00001", 0, [], text="plain")]
    client = _FakeClient(lambda m, t, n: (_ for _ in ()).throw(AssertionError("must not call")))
    out = resolve_format_mappings(
        client, {}, blocks, {"p00001": "текст"}, out_dir=tmp_path, role_policy=_policy())
    assert out == {}
    assert client.calls == []


def test_78_pid_102_spans_single_call_when_fitting(tmp_path: Path):
    from pact_v4.phase5.formatting import _estimate_prompt_tokens
    blocks, _ = _chapter_78_102()
    translations = _repeat_translations(blocks)
    client = _FakeClient(_echo_handler(blocks))
    block_map = {b.pid: b for b in blocks}
    prompt = _estimate_prompt_tokens(formatting_messages([b.pid for b in blocks], block_map, translations))
    window = prompt + 22720 + 512  # exact fit by construction
    out = resolve_format_mappings(
        client, {"max_tokens": None}, blocks, translations, out_dir=tmp_path,
        role_policy=_policy(), context_window=window)
    assert len(client.calls) == 1, f"78 tagged PIDs fitting the envelope must take one call, got {len(client.calls)}"
    assert len(out) == 102
    plan = json.loads((tmp_path / "formatting_plan.json").read_text(encoding="utf-8"))
    assert plan["strategy"] == "single_fit"
    assert plan["tagged_pids"] == 78 and plan["tagged_spans"] == 102


def test_oversized_request_splits_by_budget_not_pid_count(tmp_path: Path):
    blocks, _ = _chapter_78_102()
    translations = _repeat_translations(blocks)
    client = _FakeClient(_echo_handler(blocks))
    # Window below the whole-set need but above single-group need: the
    # planner must split by budget (not by a fixed PID count).
    out = resolve_format_mappings(
        client, {"max_tokens": None}, blocks, translations, out_dir=tmp_path,
        role_policy=_policy(), context_window=20000)
    assert 1 < len(client.calls) < 78
    # Order preserved across groups; every span resolved.
    assert len(out) == 102
    plan = json.loads((tmp_path / "formatting_plan.json").read_text(encoding="utf-8"))
    assert plan["strategy"] == "budget_split"
    assert plan["context_window"] == 20000


# ---------------------------------------------------------------------------
# Policy-owned budgeting + missing-policy debt (D3)
# ---------------------------------------------------------------------------


def test_policy_budget_recorded_not_legacy_fallback(tmp_path: Path):
    blocks = [_block("p00001", 0, [_span("em01", "x")])]
    translations = {"p00001": "перевод слово"}
    # Policy with a distinctive fixed budget: recorded value must come from it.
    policy = RoleBudget(max_output_tokens=4321, output_budget=None, reasoning_budget=0)
    client = _FakeClient(lambda m, t, n: _FakeGen(json.dumps({"mappings": []})))
    resolve_format_mappings(
        client, {"max_tokens": None}, blocks, translations,
        out_dir=tmp_path, role_policy=policy)
    assert client.calls[0][2] == 4321
    meta = json.loads((tmp_path / "formatting_batch1_meta.json").read_text(encoding="utf-8"))
    assert meta["effective_max_tokens"] == 4321


def test_missing_policy_means_diagnosed_debt_without_model_call(tmp_path: Path):
    blocks = [_block("p00001", 0, [_span("em01", "x")])]

    def _boom(messages, cfg, max_tokens, label=None):
        raise AssertionError("no model call allowed without a role policy")

    client = _FakeClient(lambda m, t, n: _boom(m, t, n))
    out = resolve_format_mappings(
        client, {"max_tokens": None}, blocks, {"p00001": "перевод"},
        out_dir=tmp_path, role_policy=None, require_policy=True)
    assert out == {}
    assert client.calls == []
    meta = json.loads((tmp_path / "formatting_batch1_meta.json").read_text(encoding="utf-8"))
    assert meta["cause"] == "formatting_policy_missing"
    assert meta["finish_reason"] is None


# ---------------------------------------------------------------------------
# D5 recovery
# ---------------------------------------------------------------------------


def test_length_truncated_group_splits_strict_subset(tmp_path: Path):
    blocks = [_block(f"p{i:05d}", i, [_span("em00", f"w{i}")]) for i in range(4)]
    translations = {b.pid: "русский перевод" for b in blocks}
    seen_sizes = []

    def _handle(messages, max_tokens, call_no):
        import re as _re
        user_text = messages[1]["content"]
        pids = _re.findall(r'<FORMAT_ITEM pid="([^"]+)"', user_text)
        seen_sizes.append(len(pids))
        if len(pids) == 4:
            return _FakeGen("truncated…", finish_reason="length")
        mappings = [{"pid": pid, "span_id": "em00", "target_text": "перевод", "occurrence": 1} for pid in pids]
        return _FakeGen(json.dumps({"mappings": mappings}), finish_reason="stop")

    client = _FakeClient(_handle)
    out = resolve_format_mappings(
        client, {"max_tokens": None}, blocks, translations,
        out_dir=tmp_path, role_policy=_policy())
    assert len(out) == 4
    assert seen_sizes[0] == 4
    assert all(s < 4 for s in seen_sizes[1:]), "follow-up calls must carry strict subsets"


def test_valid_partial_keeps_mappings_and_refetches_only_unresolved(tmp_path: Path):
    blocks = [_block(f"p{i:05d}", i, [_span("em00", f"w{i}")]) for i in range(3)]
    translations = {b.pid: "русский перевод слово" for b in blocks}
    requested = []

    def _handle(messages, max_tokens, call_no):
        import re as _re
        user_text = messages[1]["content"]
        spans = _re.findall(r'"span_id": "(em\d+)"', user_text)
        pids = _re.findall(r'<FORMAT_ITEM pid="([^"]+)"', user_text)
        requested.append((tuple(pids), call_no))
        if call_no == 1:
            # Valid JSON covering only the first PID.
            return _FakeGen(json.dumps({"mappings": [
                {"pid": "p00000", "span_id": "em00", "target_text": "перевод", "occurrence": 1}]}))
        mappings = [{"pid": pid, "span_id": "em00", "target_text": "перевод", "occurrence": 1} for pid in pids]
        return _FakeGen(json.dumps({"mappings": mappings}))

    client = _FakeClient(_handle)
    out = resolve_format_mappings(
        client, {"max_tokens": None}, blocks, translations,
        out_dir=tmp_path, role_policy=_policy())
    assert len(out) == 3
    # Follow-up must not re-request the resolved span.
    follow_pids = requested[1][0]
    assert "p00000" not in follow_pids
    assert set(follow_pids) == {"p00001", "p00002"}


def test_unsplittable_group_becomes_debt_with_call_cap(tmp_path: Path):
    blocks = [_block("p00001", 0, [_span("em00", "x")])]
    client = _FakeClient(lambda m, t, n: _FakeGen("garbage{{{", finish_reason="stop"))
    out = resolve_format_mappings(
        client, {"max_tokens": None, "max_calls": 5}, blocks, {"p00001": "перевод"},
        out_dir=tmp_path, role_policy=_policy())
    assert out == {}
    assert len(client.calls) <= 5


def test_transport_retry_bounded_to_one(tmp_path: Path):
    blocks = [_block("p00001", 0, [_span("em00", "x")])]

    class _Down:
        def __init__(self):
            self.calls = 0

        def complete(self, messages, cfg, max_tokens, label=None):
            self.calls += 1
            raise RuntimeError("down")

    client = _Down()
    out = resolve_format_mappings(
        client, {"max_tokens": None, "generation_retries": 9}, blocks,
        {"p00001": "перевод"}, out_dir=tmp_path, role_policy=_policy())
    assert out == {}
    assert client.calls == 2, "transient transport retry is bounded to one repeat"


# ---------------------------------------------------------------------------
# 3.3 accuracy / safety
# ---------------------------------------------------------------------------


def test_repeated_substring_occurrence_resolution():
    blocks = [_block("p00001", 0, [_span("em01", "кот"), _span("em02", "кот")])]
    translation = {"p00001": "кот Guillemot кот"}
    mappings = {("p00001", "em01"): ("кот", 1), ("p00001", "em02"): ("кот", 2)}
    out = run_formatting_align(
        blocks=blocks, translation=translation, backend_identity_hash="h",
        policy_version=FORMATTING_POLICY_VERSION, mappings=mappings)
    assert out.incident_count == 0
    text = dict(out.formatted_text)["p00001"]
    assert text.count("<em>") == 2
    assert text == "<em>кот</em> Guillemot <em>кот</em>"


def test_foreign_and_overlapping_mappings_rejected():
    blocks = [_block("p00001", 0, [_span("em01", "sun"), _span("em02", "moon")])]
    translation = {"p00001": "солнце светит"}
    mappings = {
        ("p00001", "em01"): ("солнце", 1),      # valid
        ("p00001", "em02"): ("солнце", 1),      # overlaps em01 -> rejected
        ("p99999", "emXX"): ("солнце", 1),      # foreign pid -> rejected
        ("p00001", "emZZ"): ("марс", 1),        # absent substring -> rejected
    }
    out = run_formatting_align(
        blocks=blocks, translation=translation, backend_identity_hash="h",
        policy_version=FORMATTING_POLICY_VERSION, mappings=mappings)
    assert out.resolved_count == 1
    assert out.incident_count == 1
    text = dict(out.formatted_text)["p00001"]
    assert text == "<em>солнце</em> светит"


def test_multiple_tags_one_pid_and_byte_preservation():
    inner = [_span("em01", "quick"), _span("st01", "brown", tag="strong")]
    blocks = [_block("p00001", 0, inner, text="the quick brown fox")]
    translation = {"p00001": "быстрая коричневая лиса идёт"}
    mappings = {
        ("p00001", "em01"): ("быстрая", 1),
        ("p00001", "st01"): ("коричневая", 1),
    }
    out = run_formatting_align(
        blocks=blocks, translation=translation, backend_identity_hash="h",
        policy_version=FORMATTING_POLICY_VERSION, mappings=mappings)
    assert out.incident_count == 0
    text = dict(out.formatted_text)["p00001"]
    assert text == "<em>быстрая</em> <strong>коричневая</strong> лиса идёт"
    import re as _re
    assert _re.sub(r"</?(em|strong|i|b|a)[^>]*>", "", text) == translation["p00001"]


def test_policy_version_bumped():
    assert FORMATTING_POLICY_VERSION == "pact-v4-formatting/v2"


# ---------------------------------------------------------------------------
# local-model-registry: formatting-only zero override (D4)
# ---------------------------------------------------------------------------


def _pair():
    tr = LocalModelSpec(
        model_key="gemma31", model_path="/tmp/gemma.gguf", model_name="gemma.gguf",
        server_args=("--reasoning-budget", "2000", "-c", "44000"),
        reasoning_budget=2000, request={"temperature": 1.0},
    )
    rv = LocalModelSpec(
        model_key="qwen38", model_path="/tmp/qwen.gguf", model_name="qwen.gguf",
        server_args=("--reasoning-budget", "8192", "-c", "44000"),
        reasoning_budget=8192, request={"temperature": 0.2},
    )
    budgets = {
        "generator": RoleBudget(max_output_tokens=74048, reasoning_budget=2000),
        "repair": RoleBudget(max_output_tokens=18432, reasoning_budget=0),
        "gemma_audit": RoleBudget(max_output_tokens=6144, reasoning_budget=0),
        "formatting": RoleBudget(max_output_tokens=16192, reasoning_budget=0, reasoning_budget_override=0),
        "qwen_audit": RoleBudget(max_output_tokens=22192, reasoning_budget=2000),
        "fidelity_reviewer": RoleBudget(max_output_tokens=24576, reasoning_budget=0),
        "russian_selector": RoleBudget(max_output_tokens=9216, reasoning_budget=0),
        "entity_extractor": RoleBudget(max_output_tokens=30192, reasoning_budget=2000),
        "russian_editor": RoleBudget(max_output_tokens=20192, reasoning_budget=0),
        "glossary_resolver": RoleBudget(max_output_tokens=12288, reasoning_budget=0),
    }
    return ResolvedModelPair(translator_model=tr, reviewer_model=rv, role_budgets=budgets)


def test_formatting_override_zero_and_transitions():
    pair = _pair()
    assert pair.effective_reasoning_budget("formatting") == 0
    assert pair.effective_reasoning_budget("qwen_audit") == 10192
    assert pair.effective_reasoning_budget("entity_extractor") == 10192
    assert pair.effective_reasoning_budget("generator") == 4000
    args = pair.launch_args_for_role("formatting")
    assert args.count("--reasoning-budget") == 1
    assert args[args.index("--reasoning-budget") + 1] == "0"
    prov = pair.reasoning_provenance_for_role("formatting")
    assert prov["effective"] == 0
    # Transition back restores the role budget.
    assert pair.launch_args_for_role("entity_extractor")[
        pair.launch_args_for_role("entity_extractor").index("--reasoning-budget") + 1] == "10192"


def test_invalid_override_rejected():
    with pytest.raises(ValueError):
        RoleBudget(max_output_tokens=100, reasoning_budget_override=5)
    with pytest.raises(ValueError):
        RoleBudget(max_output_tokens=100, reasoning_budget_override="0")  # type: ignore[arg-type]  # intentional negative test
    pair = _pair()
    bad_budgets = dict(pair.role_budgets)
    bad_budgets["repair"] = RoleBudget(max_output_tokens=18432, reasoning_budget_override=0)
    bad_pair = ResolvedModelPair(
        translator_model=pair.translator_model, reviewer_model=pair.reviewer_model,
        role_budgets=bad_budgets)
    with pytest.raises(ValueError):
        bad_pair.effective_reasoning_budget("repair")


def test_registry_formatting_carries_zero_override():
    from pact_v4.runtime.runtime_config import _load_shared_role_budgets_from_registry
    budgets = _load_shared_role_budgets_from_registry()
    assert budgets["formatting"].reasoning_budget_override == 0
    assert all(
        getattr(b, "reasoning_budget_override", None) is None
        for role, b in budgets.items() if role != "formatting"
    )


# ---------------------------------------------------------------------------
# pact-rev batched fixes: out-of-range occurrence, retry delay, group counts
# ---------------------------------------------------------------------------


def test_out_of_range_occurrence_rejected_in_align():
    # Only one "кот" exists but occurrence 5 is requested: the mapping must
    # be rejected (incident), never bound to occurrence 1 with index 5 kept.
    blocks = [_block("p00001", 0, [_span("em01", "cat")])]
    translation = {"p00001": "кот спит"}
    mappings = {("p00001", "em01"): ("кот", 5)}
    out = run_formatting_align(
        blocks=blocks, translation=translation, backend_identity_hash="h",
        policy_version=FORMATTING_POLICY_VERSION, mappings=mappings)
    assert out.resolved_count == 0
    assert out.incident_count == 1
    assert out.incidents[0].reason == "target_not_found"
    assert "5" in out.incidents[0].detail
    assert dict(out.formatted_text)["p00001"] == "кот спит"


def test_out_of_range_occurrence_rejected_in_resolve(tmp_path: Path):
    # Same rule on the resolve path: the out-of-range mapping is dropped and
    # the span stays unresolved (debt), not retained with a wrong index.
    blocks = [_block("p00001", 0, [_span("em01", "cat")])]
    translations = {"p00001": "кот спит"}
    client = _FakeClient(lambda m, t, n: _FakeGen(json.dumps({"mappings": [
        {"pid": "p00001", "span_id": "em01", "target_text": "кот", "occurrence": 5}]})))
    out = resolve_format_mappings(
        client, {"max_tokens": None}, blocks, translations,
        out_dir=tmp_path, role_policy=_policy())
    assert out == {}


def test_transport_retry_uses_bounded_delay(tmp_path: Path):
    from pact_v4.phase5 import formatting as _fmt
    assert 0 < _fmt._TRANSPORT_RETRY_DELAY_SECONDS <= _fmt._TRANSPORT_RETRY_DELAY_MAX_SECONDS <= 5.0
    blocks = [_block("p00001", 0, [_span("em00", "x")])]
    sleeps: list = []

    class _Flaky:
        def __init__(self):
            self.calls = 0

        def complete(self, messages, cfg, max_tokens, label=None):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("down")
            return _FakeGen(json.dumps({"mappings": [
                {"pid": "p00001", "span_id": "em00", "target_text": "перевод", "occurrence": 1}]}))

    client = _Flaky()
    out = resolve_format_mappings(
        client, {"max_tokens": None}, blocks, {"p00001": "перевод"},
        out_dir=tmp_path, role_policy=_policy(),
        transport_sleep=sleeps.append)
    assert out == {("p00001", "em00"): ("перевод", 1)}
    assert client.calls == 2
    assert sleeps == [_fmt._TRANSPORT_RETRY_DELAY_SECONDS]


def test_plan_and_attempt_carry_pid_and_span_counts(tmp_path: Path):
    blocks = [_block(f"p{i:05d}", i, [_span("em00", f"w{i}"), _span("em01", f"v{i}")]) for i in range(3)]
    translations = {b.pid: f"русский перевод {b.index} слово" for b in blocks}
    client = _FakeClient(lambda m, t, n: _FakeGen(json.dumps({"mappings": []})))
    resolve_format_mappings(
        client, {"max_tokens": None}, blocks, translations,
        out_dir=tmp_path, role_policy=_policy(), context_window=20000)
    plan = json.loads((tmp_path / "formatting_plan.json").read_text(encoding="utf-8"))
    assert "group_sizes" in plan and "group_span_counts" in plan
    assert len(plan["group_sizes"]) == len(plan["group_span_counts"]) == plan["groups"]
    assert sum(plan["group_sizes"]) == 3
    assert sum(plan["group_span_counts"]) == 6
    assert all(s == 2 * p for p, s in zip(plan["group_sizes"], plan["group_span_counts"]))
    meta = json.loads((tmp_path / "formatting_batch1_meta.json").read_text(encoding="utf-8"))
    assert meta["pid_count"] is not None and meta["span_count"] is not None
    assert meta["span_count"] == 2 * meta["pid_count"], "every group here carries 2 spans per PID"
