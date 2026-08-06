"""Tests for the D1 per-call remote-usage record (usage.ndjson).

Covers the D1 acceptance criteria
(``docs/plans/V4_D1_USAGE_RECORD_TASK_RU.md``):

  * every completed remote call (success and failure) is written to
    ``usage.ndjson`` at the moment it finishes — crash-safe inside a phase,
    not at phase boundaries — via the backend's per-call completion sink;
  * the artifact is append-only and crash-safe (a partial trailing line
    never breaks reading), and only the usage keys the provider reported
    are written;
  * ``run_chapter_strict`` attaches the writer to remote/composite
    runtimes; local-only runs write no ``usage.ndjson`` (local calls stay
    in ``local_lifecycle``);
  * resume (a fresh backend) appends only new calls, never duplicates;
  * the read-only aggregator CLI reports tokens by model/role, per-call
    input/output TPS and averages, and falls back to the record's
    ``runtime.remote_calls`` aggregate on a catalog without ``usage.ndjson``.

No subprocess / HTTP / real ``llama-server``: the remote path uses the same
offline fake OpenCode server as the C2 remote runner tests.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pact_full_pipeline_runner_v1 import v4_usage
from pact_v4.pipeline.usage_record import (
    USAGE_FILENAME,
    USAGE_SCHEMA,
    UsageRecordWriter,
)
from pact_v4.runtime.backend_protocol import BackendCallRecord, CompletionRequest, Message
from pact_v4.runtime.runtime_coordinator import (
    RemoteRuntimeCoordinator,
)
from pact_v4.pipeline.v4_phase12_strict_runner import (
    StrictRunConfig,
    run_chapter_strict,
)
from pact_v4.runtime.backend_role_adapters import (
    BackendGemmaAuditEvaluator,
    BackendGemmaSelector,
    BackendModelCaller,
    BackendQwenAuditEvaluator,
    BackendQwenEvaluator,
)
from pact_v4.runtime.opencode_backend import OpenCodeServerBackend
from pact_v4.runtime.runtime_config import OpenCodeBackendConfig
from tests.pact_v4.pipeline.test_v4_phase12_strict_runner import (
    StubGemma,
    StubGemmaAudit,
    StubModelCaller,
    StubQwen,
    StubQwenAudit,
    _LifecycleAwareGemmaAudit,
    _LifecycleAwareGemmaSelector,
    _LifecycleAwareModelCaller,
    _LifecycleAwareQwen,
    _LifecycleAwareQwenAudit,
    _make_router,
)
from tests.pact_v4.pipeline.test_v4_phase12_strict_runner_remote import (
    _make_cfg as _make_remote_cfg,
    _remote_backend_config,
)
from tests.pact_v4.runtime.opencode_dynamic_fake import DynamicFakeOpenCodeServer


def _fixed_now(iso: str):
    def _now() -> datetime:
        return datetime.fromisoformat(iso)
    return _now


def _load_rows(path: Path) -> list:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _success_record(label: str = "generator", model_ref: str = "opencode-go/deepseek-v4-flash") -> BackendCallRecord:
    return BackendCallRecord(
        label=label,
        model_ref=model_ref,
        request_id=f"req_{label}",
        session_id=f"ses_{label}",
        retry_count=0,
        finish_reason="end_turn",
        usage={"input_tokens": 10, "output_tokens": 20,
               "reasoning_tokens": 0, "cached_input_tokens": 3,
               "cached_write_tokens": 0, "reported_cost": 0.01},
        wall_seconds=1.5,
        raw_metadata={},
    )


def _failed_record(label: str = "generator") -> BackendCallRecord:
    return BackendCallRecord(
        label=label,
        model_ref="opencode-go/deepseek-v4-flash",
        request_id=f"req_{label}_fail",
        session_id=f"ses_{label}_fail",
        retry_count=2,
        finish_reason=None,
        usage={},
        wall_seconds=0.4,
        raw_metadata={"error_class": "rate_limit_error"},
    )


class _SinkBackend:
    """CompletionBackend whose call_records are scripted (no network).

    Mirrors the real OpenCodeServerBackend's per-call usage sink: each
    scripted record fires the sink exactly once when it first becomes
    visible through ``call_records()`` (the real backend fires at
    completion), so a fresh backend (resume) only emits its own new calls.
    """

    def __init__(self, records) -> None:
        self._records = list(records)
        self._emitted = 0
        self._sink = None
        self.closed = False
        from pact_v4.runtime.backend_protocol import BackendDescriptor
        self.descriptor = BackendDescriptor(
            kind="opencode_server", transport_version="test/v1",
            endpoint_family="test", public_endpoint="http://127.0.0.1:9",
            model_bindings={"generator": "opencode-go/x"},
            effective_options={},
        )

    def set_usage_sink(self, sink) -> None:
        self._sink = sink

    def complete(self, request) -> Any:
        raise AssertionError("_SinkBackend.complete is not exercised here")

    def call_records(self):
        # Emit each not-yet-emitted record exactly once (per-call write at
        # materialization, like the real backend emits at completion).
        if self._sink is not None:
            while self._emitted < len(self._records):
                self._sink(self._records[self._emitted])
                self._emitted += 1
        return list(self._records)

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Writer unit tests
# ---------------------------------------------------------------------------


def test_writer_appends_one_line_per_remote_call_with_full_fields(tmp_path: Path):
    writer = UsageRecordWriter(tmp_path, now=_fixed_now("2026-08-06T12:00:00+00:00"))
    writer.write_call(_success_record("generator"))
    writer.write_call(_failed_record("qwen_audit"))
    writer.close()

    rows = _load_rows(tmp_path / USAGE_FILENAME)
    assert len(rows) == 2

    ok, failed = rows
    for row in rows:
        assert row["schema"] == USAGE_SCHEMA
        assert row["ts"] == "2026-08-06T12:00:00+00:00"
        assert row["provider"] == "opencode-go"
        assert row["model"] == "deepseek-v4-flash"
        assert row["request_id"] and row["session_id"]
        assert row["wall_seconds"] is not None
        assert row["finish_reason"] is not None or row.get("error_class")

    assert ok["label"] == "generator"
    assert ok["model_ref"] == "opencode-go/deepseek-v4-flash"
    assert ok["input_tokens"] == 10
    assert ok["output_tokens"] == 20
    assert ok["reasoning_tokens"] == 0
    assert ok["cached_input_tokens"] == 3
    assert ok["cached_write_tokens"] == 0
    assert ok["reported_cost"] == 0.01
    assert ok["retry_count"] == 0
    assert "error_class" not in ok

    assert failed["label"] == "qwen_audit"
    assert failed["error_class"] == "rate_limit_error"
    assert failed["retry_count"] == 2
    # Failed call reported no usage -> usage keys absent, never invented.
    assert "input_tokens" not in failed
    assert "reported_cost" not in failed


def test_writer_accepts_backend_call_record_directly(tmp_path: Path):
    """The backend sink hands BackendCallRecord objects to write_call."""
    writer = UsageRecordWriter(tmp_path)
    writer.write_call(_success_record("generator"))
    writer.write_call(_failed_record("qwen_audit"))
    writer.close()
    rows = _load_rows(tmp_path / USAGE_FILENAME)
    assert len(rows) == 2
    assert rows[0]["label"] == "generator"
    assert rows[1]["error_class"] == "rate_limit_error"


def test_writer_ignores_local_switch_events(tmp_path: Path):
    writer = UsageRecordWriter(tmp_path)
    from pact_v4.runtime.runtime_coordinator import (
        EVENT_KIND_LOCAL_SWITCH,
        BackendEvent,
    )
    writer.write_call(BackendEvent(kind=EVENT_KIND_LOCAL_SWITCH, label="switch_to_x", to_model="x"))
    writer.close()
    assert not (tmp_path / USAGE_FILENAME).exists()


def test_writer_usage_keys_only_what_provider_reported(tmp_path: Path):
    rec = BackendCallRecord(
        label="generator", model_ref="opencode-go/deepseek-v4-flash",
        request_id="r1", session_id="s1", retry_count=0, finish_reason="end_turn",
        usage={"input_tokens": 5},  # provider only reported input
        wall_seconds=1.0, raw_metadata={},
    )
    writer = UsageRecordWriter(tmp_path)
    writer.write_call(rec)
    writer.close()
    row = _load_rows(tmp_path / USAGE_FILENAME)[0]
    assert row["input_tokens"] == 5
    assert "output_tokens" not in row
    assert "reported_cost" not in row


def test_writer_is_crash_safe_partial_trailing_line_tolerated(tmp_path: Path):
    writer = UsageRecordWriter(tmp_path)
    writer.write_call(_success_record())
    writer.close()
    path = tmp_path / USAGE_FILENAME
    # Simulate a crash mid-write: a partial trailing line.
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"schema": "pact-v4-usage/ndjson/v1", "label": "genera')
    rows = _load_rows(path)
    assert len(rows) == 1
    # The aggregator's reader (same tolerant loader) also survives.
    assert len(v4_usage._read_ndjson(path)) == 1


def test_writer_write_failure_never_raises(tmp_path: Path, monkeypatch):
    """A failing write (disk full) disables the writer, never breaks the run.

    The failure is injected into the FIRST write, before any handle exists,
    so the disabled path is genuinely exercised.
    """
    from io import StringIO

    real_open = open
    calls = {"count": 0}

    def _failing_open(path, mode="r", encoding=None, *args, **kwargs):
        calls["count"] += 1
        if mode == "a":  # first append-open fails as if disk is full
            raise OSError("disk full")
        return real_open(path, mode, encoding=encoding, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _failing_open)
    writer = UsageRecordWriter(tmp_path)
    writer.write_call(_success_record())  # must not raise
    writer.write_call(_success_record("generator"))  # must not raise (disabled)
    writer.close()
    assert not (tmp_path / USAGE_FILENAME).exists()


# ---------------------------------------------------------------------------
# Backend per-call sink (crash-safe inside a phase, final failure)
# ---------------------------------------------------------------------------


def _completion_request(label: str = "generator") -> CompletionRequest:
    return CompletionRequest(
        model_ref="opencode-go/deepseek-v4-flash",
        # The dynamic fake server only answers known Pact prompt families.
        messages=(Message(role="user", content="candidate_id=aaa"),),
        max_output_tokens=100,
        temperature=0.0,
        response_schema=None,
        label=label,
    )


def test_backend_sink_writes_each_completed_call_immediately(tmp_path: Path):
    """A completed call is in usage.ndjson at completion — before any phase
    boundary or run teardown (crash inside a phase loses nothing)."""
    fake = DynamicFakeOpenCodeServer()
    backend = OpenCodeServerBackend(config=_remote_backend_config(), session=fake)
    writer = UsageRecordWriter(tmp_path)
    backend.set_usage_sink(writer.write_call)

    resp = backend.complete(_completion_request("generator"))
    assert resp.text
    # Written at completion, without any drain/phase boundary call.
    rows = _load_rows(tmp_path / USAGE_FILENAME)
    assert len(rows) == 1
    assert rows[0]["label"] == "generator"
    assert rows[0]["input_tokens"] == 10
    assert rows[0]["output_tokens"] == 20
    assert rows[0]["provider"] == "opencode-go"

    backend.complete(_completion_request("qwen_audit"))
    rows = _load_rows(tmp_path / USAGE_FILENAME)
    assert len(rows) == 2
    assert rows[1]["label"] == "qwen_audit"
    writer.close()


def test_backend_sink_writes_final_failure_with_error_class(tmp_path: Path, monkeypatch):
    """A final failed call is written too, with error_class and no usage."""
    fake = DynamicFakeOpenCodeServer()
    backend = OpenCodeServerBackend(config=_remote_backend_config(), session=fake)
    writer = UsageRecordWriter(tmp_path)
    backend.set_usage_sink(writer.write_call)

    from pact_v4.runtime.opencode_backend import (
        ERROR_TRANSPORT_NETWORK,
        OpenCodeError,
    )

    def _boom(*args, **kwargs):
        raise OpenCodeError(
            ERROR_TRANSPORT_NETWORK, "connection refused",
            session_id="ses_fail", request_id="req_fail",
        )

    monkeypatch.setattr(backend, "_post_message", _boom)
    try:
        backend.complete(_completion_request("generator"))
    except OpenCodeError:
        pass
    rows = _load_rows(tmp_path / USAGE_FILENAME)
    assert len(rows) == 1
    assert rows[0]["error_class"] == ERROR_TRANSPORT_NETWORK
    assert "input_tokens" not in rows[0]
    assert rows[0]["session_id"]  # the session actually created
    writer.close()


# ---------------------------------------------------------------------------
# Runner wiring
# ---------------------------------------------------------------------------


def _run_remote(cfg: StrictRunConfig):
    fake = DynamicFakeOpenCodeServer()
    backend = OpenCodeServerBackend(
        config=_remote_backend_config(), session=fake,
    )
    runtime = RemoteRuntimeCoordinator(backend)
    result = run_chapter_strict(
        cfg, runtime=runtime,
        model_caller=BackendModelCaller(backend),
        qwen_evaluator=BackendQwenEvaluator(backend),
        gemma_selector=BackendGemmaSelector(backend),
        qwen_audit_evaluator=BackendQwenAuditEvaluator(backend),
        gemma_audit_evaluator=BackendGemmaAuditEvaluator(backend),
    )
    return result, fake


def test_remote_run_writes_usage_ndjson_per_call(tmp_path: Path):
    remote_cfg = _make_remote_cfg(tmp_path, backend=OpenCodeBackendConfig(
        server=_remote_backend_config(),
    ))
    result, fake = _run_remote(remote_cfg)
    assert result.chunk_count == 2
    path = remote_cfg.out_dir / USAGE_FILENAME
    assert path.exists()
    rows = _load_rows(path)
    # One line per remote message POST, successes only in this happy path.
    assert len(rows) == fake.message_count()
    assert len(rows) >= 4
    assert all(row["schema"] == USAGE_SCHEMA for row in rows)
    assert all(row["provider"] == "opencode-go" for row in rows)
    labels = {row["label"] for row in rows}
    # Role labels carry the role the adapter was built for.
    assert any("fidelity_first" in label for label in labels)
    assert any("qwen_chapter_audit" in label for label in labels)


def test_local_run_writes_no_usage_ndjson(tmp_path: Path):
    from tests.pact_v4.pipeline.test_v4_phase12_strict_runner import _make_cfg as _local_cfg
    cfg = _local_cfg(tmp_path, n_paragraphs=8)
    router = _make_router()
    model_caller = _LifecycleAwareModelCaller(router, StubModelCaller())
    run_chapter_strict(
        cfg, router=router, model_caller=model_caller,
        qwen_evaluator=_LifecycleAwareQwen(router, StubQwen()),
        gemma_selector=_LifecycleAwareGemmaSelector(router, StubGemma()),
        qwen_audit_evaluator=_LifecycleAwareQwenAudit(router, StubQwenAudit()),
        gemma_audit_evaluator=_LifecycleAwareGemmaAudit(router, StubGemmaAudit()),
    )
    assert not (cfg.out_dir / USAGE_FILENAME).exists()


def test_resume_appends_without_duplicates(tmp_path: Path):
    # Runner-level resume semantics: production resume = a NEW process with a
    # fresh backend, so call_records() holds only the new session's calls.
    # Two runs over the same out_dir with fresh scripted backends must
    # append: run 1 writes its 2 calls, run 2 writes only its new call.
    from tests.pact_v4.pipeline.test_v4_phase12_strict_runner import _make_cfg as _local_cfg
    cfg = _local_cfg(tmp_path, n_paragraphs=8)

    def _run_with_records(records):
        runtime = RemoteRuntimeCoordinator(_SinkBackend(records))
        result = run_chapter_strict(
            cfg, runtime=runtime,
            model_caller=StubModelCaller(),
            qwen_evaluator=StubQwen(),
            gemma_selector=StubGemma(),
            qwen_audit_evaluator=StubQwenAudit(),
            gemma_audit_evaluator=StubGemmaAudit(),
        )
        return result

    first = _run_with_records([_success_record("phase2b/generator/c1"),
                               _success_record("phase2c/qwen_fidelity")])
    path = cfg.out_dir / USAGE_FILENAME
    rows1 = _load_rows(path)
    assert len(rows1) == 2
    assert first.processed_count >= 1

    # Resume: a fresh backend whose records only cover the new session.
    second = _run_with_records([_success_record("phase3/qwen_chapter_audit")])
    assert second.resumed_from_index >= 1
    rows2 = _load_rows(path)
    # Append-only: old lines untouched, only the new call added.
    assert rows2[:2] == rows1
    assert len(rows2) == 3
    assert rows2[2]["label"] == "phase3/qwen_chapter_audit"
    request_ids = [row["request_id"] for row in rows2]
    assert len(request_ids) == len(set(request_ids))


# ---------------------------------------------------------------------------
# Read-only aggregator
# ---------------------------------------------------------------------------


def _write_ndjson(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def test_aggregator_totals_by_model_role_tps_and_cost(tmp_path: Path):
    out = tmp_path / "run"
    _write_ndjson(out / USAGE_FILENAME, [
        {"schema": USAGE_SCHEMA, "ts": "t", "label": "generator",
         "model_ref": "opencode-go/deepseek-v4-flash", "provider": "opencode-go",
         "model": "deepseek-v4-flash", "input_tokens": 100, "output_tokens": 50,
         "wall_seconds": 10.0, "reported_cost": 0.01, "request_id": "r1",
         "session_id": "s1", "finish_reason": "end_turn", "retry_count": 0},
        {"schema": USAGE_SCHEMA, "ts": "t", "label": "generator",
         "model_ref": "opencode-go/deepseek-v4-flash", "provider": "opencode-go",
         "model": "deepseek-v4-flash", "input_tokens": 200, "output_tokens": 100,
         "wall_seconds": 20.0, "reported_cost": 0.02, "request_id": "r2",
         "session_id": "s2", "finish_reason": "end_turn", "retry_count": 0},
        {"schema": USAGE_SCHEMA, "ts": "t", "label": "qwen_audit",
         "model_ref": "opencode-go/qwen3.7-plus", "provider": "opencode-go",
         "model": "qwen3.7-plus", "input_tokens": 10, "output_tokens": 5,
         "wall_seconds": 2.0, "error_class": "rate_limit_error",
         "request_id": "r3", "session_id": "s3", "retry_count": 2},
    ])
    report = v4_usage.render_usage_report(out)
    assert "opencode-go/deepseek-v4-flash: 2 call(s) (failed=0)" in report
    assert "opencode-go/qwen3.7-plus: 1 call(s) (failed=1)" in report
    assert "input_tokens=300" in report
    assert "output_tokens=150" in report
    assert "reported_cost: 0.03" in report
    assert "input tps (avg): " in report  # coarse-throughput line present
    assert "calls: 3 (failed=1)" in report
    assert "by role (label)" in report
    assert "generator: 2 call(s)" in report
    assert "qwen_audit: 1 call(s)" in report


def test_aggregator_shows_per_call_input_output_tps(tmp_path: Path):
    out = tmp_path / "run"
    _write_ndjson(out / USAGE_FILENAME, [
        {"schema": USAGE_SCHEMA, "ts": "t", "label": "generator",
         "model_ref": "opencode-go/deepseek-v4-flash", "provider": "opencode-go",
         "model": "deepseek-v4-flash", "input_tokens": 100, "output_tokens": 50,
         "wall_seconds": 10.0, "request_id": "r1", "session_id": "s1",
         "finish_reason": "end_turn", "retry_count": 0},
        {"schema": USAGE_SCHEMA, "ts": "t", "label": "qwen_audit",
         "model_ref": "opencode-go/qwen3.7-plus", "provider": "opencode-go",
         "model": "qwen3.7-plus", "input_tokens": 10, "output_tokens": 5,
         "wall_seconds": 2.0, "request_id": "r2", "session_id": "s2",
         "finish_reason": "end_turn", "retry_count": 0},
    ])
    report = v4_usage.render_usage_report(out)
    assert "-- per-call rates" in report
    # 100 tokens / 10s = 10.0 in-tps, 50/10 = 5.0 out-tps
    assert "generator opencode-go/deepseek-v4-flash in=10.0/s out=5.0/s wall=10.0s" in report
    # 10 tokens / 2s = 5.0 in-tps, 5/2 = 2.5 out-tps
    assert "qwen_audit opencode-go/qwen3.7-plus in=5.0/s out=2.5/s wall=2.0s" in report
    # Averages are still present.
    assert "input tps (avg)" in report


def test_aggregator_crash_safe_read(tmp_path: Path):
    out = tmp_path / "run"
    path = out / USAGE_FILENAME
    _write_ndjson(path, [
        {"schema": USAGE_SCHEMA, "ts": "t", "label": "generator",
         "model_ref": "opencode-go/x", "provider": "opencode-go", "model": "x",
         "input_tokens": 1, "output_tokens": 1, "wall_seconds": 1.0,
         "request_id": "r1", "session_id": "s1", "retry_count": 0},
    ])
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"schema": "pact-v4-usage/ndjson/v1", "label": "gene')
    report = v4_usage.render_usage_report(out)
    assert "1 call(s)" in report
    assert "input_tokens: 1" in report


def test_aggregator_crash_safe_read_truncated_multibyte_utf8_tail(tmp_path: Path):
    """A partial trailing line that is an incomplete UTF-8 multibyte
    sequence (crash mid-write of a non-ASCII record) must not crash the
    reader: prior complete lines are still reported."""
    out = tmp_path / "run"
    path = out / USAGE_FILENAME
    _write_ndjson(path, [
        {"schema": USAGE_SCHEMA, "ts": "t", "label": "generator",
         "model_ref": "opencode-go/x", "provider": "opencode-go", "model": "x",
         "input_tokens": 1, "output_tokens": 1, "wall_seconds": 1.0,
         "request_id": "r1", "session_id": "s1", "retry_count": 0},
    ])
    # Incomplete multibyte UTF-8: 'Р' (U+0420) is 0xD0 0xA0; write only the
    # leading byte 0xC3 would be an invalid start — use 0xD0 alone (an
    # incomplete 2-byte sequence) to model a crash mid non-ASCII write.
    with open(path, "ab") as handle:
        handle.write(b'{"schema": "pact-v4-usage/ndjson/v1", "label": "\xd0')
    rows = v4_usage._read_ndjson(path)
    assert len(rows) == 1
    assert rows[0]["label"] == "generator"
    report = v4_usage.render_usage_report(out)
    assert "1 call(s)" in report
    assert "input_tokens: 1" in report


def test_aggregator_falls_back_to_record_aggregate_without_usage_ndjson(tmp_path: Path):
    out = tmp_path / "run"
    out.mkdir(parents=True, exist_ok=True)
    (out / "strict_chapter_trial_record.json").write_text(json.dumps({
        "runtime": {
            "remote_calls": {
                "count": 7, "input_tokens": 1000, "output_tokens": 500,
                "cached_input_tokens": 50, "reported_cost": 0.1,
            },
        },
    }), encoding="utf-8")
    report = v4_usage.render_usage_report(out)
    assert "record aggregate" in report
    assert "calls: 7" in report
    assert "input_tokens: 1000" in report
    assert "reported_cost: 0.1" in report
    assert "per-call breakdown" in report


def test_aggregator_nothing_to_report_when_no_artifacts(tmp_path: Path):
    out = tmp_path / "run"
    out.mkdir(parents=True, exist_ok=True)
    report = v4_usage.render_usage_report(out)
    assert "nothing to report" in report
