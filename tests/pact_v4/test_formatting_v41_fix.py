"""v41 formatting fix coverage: dynamic budgets, fallback, diagnostics, reasoning override."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pact_v4.phase0b.source_html import SourceBlock, SourceSpan
from pact_v4.phase5.formatting import (
    DEFAULT_FORMATTING_CFG,
    _FORMATTING_MAX_TOKENS_CAP,
    _FORMATTING_MIN_TOKENS,
    _effective_max_tokens,
    resolve_format_mappings,
)


def _span(sid: str, text: str = "emph") -> SourceSpan:
    return SourceSpan(span_id=sid, tag="em", text=text, attrs={}, occurrence=1)


def _blocks(n_spans: int, n_pids: int = 1, text_len: int = 20):
    blocks = []
    spans_per_pid = max(1, n_spans // n_pids)
    remaining = n_spans
    for i in range(n_pids):
        pid = f"p{i:05d}"
        take = min(spans_per_pid, remaining) if i < n_pids - 1 else remaining
        spans = tuple(_span(f"em{j:02d}", "word") for j in range(take))
        txt = "x" * text_len
        blocks.append(SourceBlock(pid=pid, index=i, tag="p", text=txt, html=f"<p>{txt}</p>", structural_role="body", inline_spans=spans, word_count=len(txt.split())))
        remaining -= take
        if remaining <= 0:
            break
    return blocks


class _FakeGen:
    def __init__(self, content: str, finish_reason="stop", usage=None, reasoning="", response_format_attempted=True):
        self.content = content
        self.text = content
        self.finish_reason = finish_reason
        self.usage = usage or {"prompt_tokens": 10, "completion_tokens": 5}
        self.reasoning = reasoning
        self.reasoning_content = reasoning
        self.response_format_attempted = response_format_attempted


class _FakeClient:
    def __init__(self, gen: _FakeGen):
        self.gen = gen
        self.calls = []

    def complete(self, messages, cfg, max_tokens, label=None):
        self.calls.append((messages, dict(cfg), max_tokens, label))
        return self.gen


def test_default_cfg_is_dynamic_sentinel():
    from pact_v4.phase5.formatting import DEFAULT_FORMATTING_CFG as DFC
    assert DFC["max_tokens"] is None
    import pact_full_pipeline_runner_v1.v4_book_run as br
    assert br._DEFAULT_FORMATTING_CFG["max_tokens"] is None


def test_effective_max_tokens_dynamic_values():
    # 5 spans: 40*5+500=700 -> min 800
    assert _effective_max_tokens(5, None) == 800
    assert _effective_max_tokens(5, 0) == 800
    # explicit larger overrides dynamic
    assert _effective_max_tokens(5, 1600) == 1600
    # 69 spans: 40*69+500=3260
    assert _effective_max_tokens(69, None) == 3260
    assert _effective_max_tokens(69, 0) == 3260
    # small explicit larger than needed respects it
    assert _effective_max_tokens(69, 4000) == 4000
    # cap at 8192
    assert _effective_max_tokens(300, None) == _FORMATTING_MAX_TOKENS_CAP
    assert _effective_max_tokens(10, 9000) == _FORMATTING_MAX_TOKENS_CAP


def test_resolve_dynamic_budget_small_chapter(tmp_path: Path):
    blocks = _blocks(5, n_pids=1)
    translations = {b.pid: "привет мир word" for b in blocks}
    gen = _FakeGen(content=json.dumps({"mappings": [{"pid": blocks[0].pid, "span_id": "em00", "target_text": "привет", "occurrence": 1}]}))
    client = _FakeClient(gen)
    resolve_format_mappings(client, {"max_tokens": None}, blocks, translations, out_dir=tmp_path)
    # 5 spans -> effective 800, not 1600
    assert client.calls[0][2] == 800


def test_resolve_dynamic_budget_large_chapter(tmp_path: Path):
    blocks = _blocks(69, n_pids=10)
    translations = {b.pid: "привет мир " * 10 for b in blocks}
    mappings = [{"pid": b.pid, "span_id": s.span_id, "target_text": "привет", "occurrence": 1} for b in blocks for s in b.inline_spans]
    gen = _FakeGen(content=json.dumps({"mappings": mappings}))
    client = _FakeClient(gen)
    resolve_format_mappings(client, {}, blocks, translations, out_dir=tmp_path)
    # 69 spans -> 3260 in single-call mode (default)
    assert client.calls[0][2] == 3260


def test_single_call_fallback_over_80_spans(tmp_path: Path):
    # 85 spans across 20 pids -> fallback to batches (12 pids per batch => 2 calls)
    blocks = _blocks(85, n_pids=20)
    translations = {b.pid: "привет мир " * 5 for b in blocks}
    gen = _FakeGen(content=json.dumps({"mappings": []}))
    client = _FakeClient(gen)
    resolve_format_mappings(client, {"max_tokens": None}, blocks, translations, out_dir=tmp_path)
    assert len(client.calls) > 1


def test_single_call_fallback_prompt_too_large(tmp_path: Path):
    # force prompt >12000 chars via many pids with large text
    blocks = _blocks(30, n_pids=15, text_len=4000)
    translations = {b.pid: "x" * 4000 for b in blocks}
    gen = _FakeGen(content=json.dumps({"mappings": []}))
    client = _FakeClient(gen)
    resolve_format_mappings(client, {"max_tokens": None, "formatting_single_call_whole_chapter": True}, blocks, translations, out_dir=tmp_path)
    assert len(client.calls) > 1


def test_diagnostics_artifacts_include_finish_reason_usage(tmp_path: Path):
    blocks = _blocks(2, n_pids=1)
    translations = {b.pid: "привет мир" for b in blocks}
    gen = _FakeGen(
        content=json.dumps({"mappings": [{"pid": blocks[0].pid, "span_id": "em00", "target_text": "привет", "occurrence": 1}]}),
        finish_reason="stop",
        usage={"prompt_tokens": 123, "completion_tokens": 45, "total_tokens": 168},
        reasoning="some reasoning",
        response_format_attempted=True,
    )
    client = _FakeClient(gen)
    resolve_format_mappings(client, {}, blocks, translations, out_dir=tmp_path)
    meta_path = tmp_path / "formatting_batch1_meta.json"
    assert meta_path.exists()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["finish_reason"] == "stop"
    assert meta["usage"]["prompt_tokens"] == 123
    assert meta["response_format_attempted"] is True
    assert meta["effective_max_tokens"] == 800  # 2 spans -> 580 -> min 800
    assert (tmp_path / "formatting_batch1_raw.txt").exists()
    assert (tmp_path / "formatting_batch1_reasoning.txt").read_text(encoding="utf-8") == "some reasoning"
    assert (tmp_path / "formatting_batch1_messages.json").exists()


def test_diagnostics_on_parse_failure_logs_and_writes_meta(tmp_path: Path, caplog):
    blocks = _blocks(1, n_pids=1)
    translations = {b.pid: "привет мир" for b in blocks}
    # Bad JSON triggers parse failure
    gen = _FakeGen(content="not json at all", finish_reason="length", usage={"prompt_tokens": 10}, reasoning="bad", response_format_attempted=True)
    client = _FakeClient(gen)
    import logging
    caplog.set_level(logging.WARNING)
    resolve_format_mappings(client, {"max_tokens": None, "generation_retries": 1}, blocks, translations, out_dir=tmp_path)
    # Should have warning with finish_reason and usage
    assert any("finish_reason" in rec.message and "length" in rec.message for rec in caplog.records)
    meta_path = tmp_path / "formatting_batch1_meta.json"
    assert meta_path.exists()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["finish_reason"] == "length"


def test_build_formatting_client_overrides_reasoning_for_runtime_config(tmp_path: Path, monkeypatch):
    import pact_full_pipeline_runner_v1.v4_book_run as br
    from pact_full_pipeline_runner_v1.v4_phase12_strict_run import _gemma_server_args_for_reasoning
    from pact_v4.runtime.runtime_config import LocalLlamaBackendConfig

    # Create a backend with reasoning 2048
    backend_with_reasoning = LocalLlamaBackendConfig(
        exe=Path("/tmp/fake.exe"),
        device="CPU",
        host="127.0.0.1",
        model_paths={"gemma": Path("/tmp/gemma.gguf"), "qwen": Path("/tmp/qwen.gguf")},
        model_names={"gemma": "gemma", "qwen": "qwen"},
        server_args={"gemma": _gemma_server_args_for_reasoning(2048), "qwen": []},
        port=8094,
    )
    # Patch loader to return our backend
    monkeypatch.setattr(br, "_load_runtime_config_file", lambda p: backend_with_reasoning) if hasattr(br, "_load_runtime_config_file") else None
    # Actually patch the function imported inside _build_formatting_client
    import pact_full_pipeline_runner_v1.v4_phase12_strict_run as strict_cli
    orig_load = strict_cli._load_runtime_config_file if hasattr(strict_cli, "_load_runtime_config_file") else None

    # Patch at the module where _build_formatting_client imports it
    # by monkeypatching the file-based loader path handling
    # Simpler: create a temp runtime config file and mock loader via monkeypatch dict
    captured = {}

    class DummyRuntime:
        def close(self): pass

    def fake_build_runtime(self, log_dir=None):
        # Verify gemma args have reasoning 0
        assert self.server_args["gemma"] != _gemma_server_args_for_reasoning(2048)
        assert self.server_args["gemma"] == _gemma_server_args_for_reasoning(0)
        captured["checked"] = True
        return DummyRuntime()

    monkeypatch.setattr(LocalLlamaBackendConfig, "build_runtime", fake_build_runtime)
    # Mock build_role_backend to avoid needing real backend
    import pact_v4.runtime.runtime_config as rc
    orig_build_role = rc.build_role_backend
    monkeypatch.setattr(rc, "build_role_backend", lambda backend, runtime: MagicMock())

    # Need to also mock _load_runtime_config_file import inside function via patching strict_cli
    if orig_load:
        monkeypatch.setattr(strict_cli, "_load_runtime_config_file", lambda p: backend_with_reasoning)

    args = MagicMock()
    args.memory_dir = str(tmp_path)
    args.runtime_config = None
    args.translator = None
    args.reviewer = None
    args.providers_config = None
    # Create a temp file to satisfy rc_path existence (value doesn't matter due to mock)
    rc_file = tmp_path / "runtime.yaml"
    rc_file.write_text("dummy", encoding="utf-8")
    extra = ["--runtime-config", str(rc_file)]

    fmt_cfg = {"enabled": True}
    client = br._build_formatting_client(args, extra, fmt_cfg)
    assert captured.get("checked") is True
    # Cleanup
    if orig_load:
        monkeypatch.setattr(strict_cli, "_load_runtime_config_file", orig_load)
    monkeypatch.setattr(rc, "build_role_backend", orig_build_role)


def test_formatting_backend_client_propagates_response_format_from_metadata(tmp_path: Path):
    """v41 round2: _FormattingBackendClient must propagate raw_metadata value, not req intent.
    When ApiClient fell back after grammar rejection, response_format_attempted is False
    even though req.response_schema is set. """
    import pact_full_pipeline_runner_v1.v4_book_run as br

    # Mock backend returning CompletionResponse with raw_metadata indicating fallback
    class _FakeResp:
        def __init__(self, attempted):
            self.text = '{"mappings": []}'
            self.finish_reason = "stop"
            self.usage = {"prompt_tokens": 5}
            self.raw_metadata = {"response_format_attempted": attempted, "reasoning": ""}

    class _FakeBackend:
        def __init__(self, attempted):
            self._attempted = attempted
            self.last_req = None
            self.descriptor = MagicMock(model_bindings={})
        def complete(self, req):
            self.last_req = req
            return _FakeResp(self._attempted)

    # Case 1: backend reports False (fallback) — must propagate False despite req schema True
    backend_false = _FakeBackend(attempted=False)
    client_false = br._FormattingBackendClient(backend_false, runtime=None)
    gen_false = client_false.complete([{"role": "user", "content": "hi"}], {"temperature": 0.1}, 800, label="test")
    assert gen_false.response_format_attempted is False
    # req had response_schema set (json_object), but backend said False
    assert backend_false.last_req.response_schema == {"type": "json_object"}

    # Case 2: backend reports True — propagate True
    backend_true = _FakeBackend(attempted=True)
    client_true = br._FormattingBackendClient(backend_true, runtime=None)
    gen_true = client_true.complete([{"role": "user", "content": "hi"}], {"temperature": 0.1}, 800, label="test")
    assert gen_true.response_format_attempted is True

    # Case 3: missing key — fallback to request intent (True)
    class _FakeRespNoKey:
        def __init__(self):
            self.text = '{"mappings": []}'
            self.finish_reason = "stop"
            self.usage = {}
            self.raw_metadata = {}
    class _FakeBackendNoKey:
        descriptor = MagicMock(model_bindings={})
        def complete(self, req):
            return _FakeRespNoKey()
    client_nokey = br._FormattingBackendClient(_FakeBackendNoKey(), runtime=None)
    gen_nokey = client_nokey.complete([{"role": "user", "content": "hi"}], {"temperature": 0.1}, 800, label="test")
    assert gen_nokey.response_format_attempted is True


def test_retry_preserves_failed_attempt_artifacts(tmp_path: Path):
    """v41 round2: retry must not overwrite failed-attempt diagnostics; per-attempt files retained."""
    blocks = _blocks(1, n_pids=1)
    translations = {b.pid: "привет мир" for b in blocks}
    pid = blocks[0].pid

    # First attempt: invalid JSON (parse failure) with reasoning "bad1" and finish_reason length
    # Second attempt: valid JSON with reasoning "good2"
    bad_gen = _FakeGen(
        content="not json at all",
        finish_reason="length",
        usage={"prompt_tokens": 10},
        reasoning="bad1",
        response_format_attempted=True,
    )
    good_content = json.dumps({"mappings": [{"pid": pid, "span_id": "em00", "target_text": "привет", "occurrence": 1}]})
    good_gen = _FakeGen(
        content=good_content,
        finish_reason="stop",
        usage={"prompt_tokens": 10, "completion_tokens": 5},
        reasoning="good2",
        response_format_attempted=True,
    )

    class _SeqClient:
        def __init__(self, gens):
            self.gens = list(gens)
            self.calls = []
        def complete(self, messages, cfg, max_tokens, label=None):
            self.calls.append(label)
            return self.gens[len(self.calls) - 1]

    client = _SeqClient([bad_gen, good_gen])
    result = resolve_format_mappings(client, {"max_tokens": None, "generation_retries": 2}, blocks, translations, out_dir=tmp_path)
    # Should have succeeded on second attempt
    assert result[(pid, "em00")][0] == "привет"
    assert len(client.calls) == 2

    # Per-attempt files must exist for both attempts
    assert (tmp_path / "formatting_batch1_attempt1_raw.txt").exists()
    assert (tmp_path / "formatting_batch1_attempt2_raw.txt").exists()
    assert (tmp_path / "formatting_batch1_attempt1_reasoning.txt").read_text(encoding="utf-8") == "bad1"
    assert (tmp_path / "formatting_batch1_attempt2_reasoning.txt").read_text(encoding="utf-8") == "good2"
    # Attempt1 meta must capture length finish_reason, attempt2 stop
    meta1 = json.loads((tmp_path / "formatting_batch1_attempt1_meta.json").read_text(encoding="utf-8"))
    meta2 = json.loads((tmp_path / "formatting_batch1_attempt2_meta.json").read_text(encoding="utf-8"))
    assert meta1["finish_reason"] == "length"
    assert meta1["attempt"] == 1
    assert meta2["finish_reason"] == "stop"
    assert meta2["attempt"] == 2
    # Canonical should reflect final (successful) attempt
    assert (tmp_path / "formatting_batch1_raw.txt").read_text(encoding="utf-8") == good_content
    assert (tmp_path / "formatting_batch1_reasoning.txt").read_text(encoding="utf-8") == "good2"
    # Canonical meta should reflect last attempt
    meta_canonical = json.loads((tmp_path / "formatting_batch1_meta.json").read_text(encoding="utf-8"))
    assert meta_canonical["attempt"] == 2
    assert meta_canonical["finish_reason"] == "stop"


def test_retry_parse_failure_then_transport_failure_no_stale_generation(tmp_path: Path):
    """HIGH round3: parse-failure followed by transport failure must not reuse attempt1's generation.
    Before fix, attempt2's artifacts/log/meta reused attempt1's raw/reasoning/finish_reason."""
    blocks = _blocks(1, n_pids=1)
    translations = {b.pid: "привет мир" for b in blocks}

    bad_gen = _FakeGen(
        content="not json at all",
        finish_reason="length",
        usage={"prompt_tokens": 10},
        reasoning="bad1",
        response_format_attempted=True,
    )

    class _SeqFailClient:
        def __init__(self, first_gen):
            self.first_gen = first_gen
            self.calls = 0

        def complete(self, messages, cfg, max_tokens, label=None):
            self.calls += 1
            if self.calls == 1:
                return self.first_gen
            raise RuntimeError("transport down")

    client = _SeqFailClient(bad_gen)
    result = resolve_format_mappings(
        client, {"max_tokens": None, "generation_retries": 2}, blocks, translations, out_dir=tmp_path
    )
    # Both attempts failed -> empty result (debt)
    assert result == {}
    assert client.calls == 2

    # Attempt1 diagnostics must preserve bad Gen
    assert (tmp_path / "formatting_batch1_attempt1_raw.txt").read_text(encoding="utf-8") == "not json at all"
    assert (tmp_path / "formatting_batch1_attempt1_reasoning.txt").read_text(encoding="utf-8") == "bad1"
    meta1 = json.loads((tmp_path / "formatting_batch1_attempt1_meta.json").read_text(encoding="utf-8"))
    assert meta1["finish_reason"] == "length"
    assert meta1["attempt"] == 1

    # Attempt2 must NOT reuse attempt1's generation — transport failure has no generation
    # Hence attempt2_raw/reasoning must not exist or not contain stale "bad1" / "not json"
    attempt2_raw = tmp_path / "formatting_batch1_attempt2_raw.txt"
    attempt2_reasoning = tmp_path / "formatting_batch1_attempt2_reasoning.txt"
    # After fix, transport failure writes no raw/reasoning (gen_obj is None -> only messages+meta)
    assert not attempt2_raw.exists(), "attempt2 raw must not be written from stale generation"
    assert not attempt2_reasoning.exists(), "attempt2 reasoning must not be written from stale generation"

    meta2 = json.loads((tmp_path / "formatting_batch1_attempt2_meta.json").read_text(encoding="utf-8"))
    assert meta2["attempt"] == 2
    assert meta2["finish_reason"] is None
    assert meta2["usage"] is None
    assert "transport down" in meta2["error"]
    # Must not leak bad1 finish_reason
    assert meta2["finish_reason"] != "length"
    assert (tmp_path / "formatting_batch1_attempt2_messages.json").exists()
