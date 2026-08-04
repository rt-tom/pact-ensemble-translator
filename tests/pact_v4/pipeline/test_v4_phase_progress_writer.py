"""Tests for the Phase 12 run-progress writer and its runner wiring.

Covers the acceptance criteria for the write side (``AGENTS.md`` / the task
card ``docs/plans/V4_PHASE12_RUN_PROGRESS_TRACKER_TASK_RU.md``):

  * events are written at every point of the strict driver run
    (``run_started``, per-chunk, Step 6 units, Step 7 regions/re-audits,
    formatting, terminal);
  * the artifact is append-only NDJSON and crash-safe (a partial trailing
    line never breaks reading);
  * ``run_chapter_strict`` writes ``phase_progress.ndjson`` even when no
    explicit writer is injected, and the pipeline/journal/schema/identity
    semantics are unchanged (the existing suite already guards that);
  * resume keeps appending and the tracker sees the cumulative stream.

No subprocess / HTTP / real ``llama-server``: the same stub harness as
``tests/pact_v4/pipeline/test_v4_phase12_strict_runner.py``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from pact_v4.pipeline import v4_phase12_strict_runner as runner
from pact_v4.pipeline.phase_progress import (
    PHASE_PROGRESS_FILENAME,
    PHASE_PROGRESS_SCHEMA,
    PhaseProgressWriter,
)
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
    _make_cfg,
    _make_router,
)

DETECTORS = ("qwen_chapter_audit", "gemma_russian_review")


def _load_events(path: Path):
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _fixed_now(iso: str):
    def _now() -> datetime:
        return datetime.fromisoformat(iso)
    return _now


# ---------------------------------------------------------------------------
# Writer unit tests
# ---------------------------------------------------------------------------


def test_writer_appends_ndjson_lines_with_schema(tmp_path: Path):
    writer = PhaseProgressWriter(tmp_path, now=_fixed_now("2026-08-03T12:00:00+00:00"))
    writer.run_started(
        chapter_id="046", out_dir=tmp_path, started_at="2026-08-03T11:59:00+00:00",
        backend_identity_hash="h1", resumed_from_index=0,
    )
    writer.chunk_started(chunk_id="chunk0001")
    writer.chunk_done(chunk_id="chunk0001", outcome="selected")
    writer.audit_unit_started(chunk_id="chunk0001", detector="qwen_chapter_audit")
    writer.audit_unit_done(chunk_id="chunk0001", detector="qwen_chapter_audit", status="ok")
    writer.audit_done(status="complete")
    writer.terminal(status="complete")
    writer.close()

    events = _load_events(tmp_path / PHASE_PROGRESS_FILENAME)
    assert [e["event"] for e in events] == [
        "run_started", "chunk_started", "chunk_done",
        "audit_unit_started", "audit_unit_done", "audit_done", "terminal",
    ]
    for event in events:
        assert event["schema"] == PHASE_PROGRESS_SCHEMA
        assert event["ts"]
    assert events[0]["resumed_from_index"] == 0
    assert events[3]["detector"] == "qwen_chapter_audit"
    assert events[6]["status"] == "complete"


def test_writer_is_crash_safe_partial_trailing_line_tolerated(tmp_path: Path):
    writer = PhaseProgressWriter(tmp_path)
    writer.chunk_done(chunk_id="a", outcome="selected")
    writer.close()
    path = tmp_path / PHASE_PROGRESS_FILENAME
    # Simulate a crash mid-write: a partial trailing line.
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"schema": "pact-v4-phase-progress/ndjson/v1", "event": "chunk_s')
    events = _load_events(path)
    assert [e["event"] for e in events] == ["chunk_done"]
    # The tracker's reader (same tolerant loader) also survives.
    from pact_full_pipeline_runner_v1.v4_phase_progress import _load_events as tracker_load
    assert [e["event"] for e in tracker_load(tmp_path)] == ["chunk_done"]


def test_writer_write_failure_never_raises(tmp_path: Path, monkeypatch):
    writer = PhaseProgressWriter(tmp_path)
    writer.emit("chunk_started", chunk_id="x")

    def _boom(path, mode, encoding):
        raise OSError("disk full")
    monkeypatch.setattr("builtins.open", _boom)
    writer.emit("chunk_done", chunk_id="x", outcome="selected")  # must not raise
    writer.close()


# ---------------------------------------------------------------------------
# Runner wiring: run_started / chunk / step6 / step7 / terminal events
# ---------------------------------------------------------------------------


def _run(cfg, *, router=None, qwen=None, gemma=None, qwen_audit=None, gemma_audit=None,
         repair_adapters=None, formatting_adapters=None):
    router = router or _make_router()
    model_caller = _LifecycleAwareModelCaller(router, StubModelCaller())
    qwen_evaluator = _LifecycleAwareQwen(router, qwen or StubQwen())
    gemma_selector = _LifecycleAwareGemmaSelector(router, gemma or StubGemma())
    qwen_audit_evaluator = _LifecycleAwareQwenAudit(router, qwen_audit or StubQwenAudit())
    gemma_audit_evaluator = _LifecycleAwareGemmaAudit(router, gemma_audit or StubGemmaAudit())
    return runner.run_chapter_strict(
        cfg, router=router, model_caller=model_caller,
        qwen_evaluator=qwen_evaluator, gemma_selector=gemma_selector,
        qwen_audit_evaluator=qwen_audit_evaluator,
        gemma_audit_evaluator=gemma_audit_evaluator,
        repair_adapters=repair_adapters,
        formatting_adapters=formatting_adapters,
    )


def test_runner_writes_phase_progress_file_and_run_started(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=8)
    _run(cfg)
    path = cfg.out_dir / PHASE_PROGRESS_FILENAME
    assert path.exists()
    events = _load_events(path)
    assert events[0]["event"] == "run_started"
    assert events[0]["chapter_id"] == cfg.chapter_id
    assert events[0]["out_dir"] == str(cfg.out_dir)
    assert events[0]["backend_identity_hash"] == cfg.backend.identity_hash
    assert events[0]["resumed_from_index"] == 0


def test_runner_emits_chunk_and_audit_unit_events(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=8)
    router = _make_router()
    model_caller = _LifecycleAwareModelCaller(router, StubModelCaller())
    result = runner.run_chapter_strict(
        cfg, router=router, model_caller=model_caller,
        qwen_evaluator=_LifecycleAwareQwen(router, StubQwen()),
        gemma_selector=_LifecycleAwareGemmaSelector(router, StubGemma()),
        qwen_audit_evaluator=_LifecycleAwareQwenAudit(router, StubQwenAudit()),
        gemma_audit_evaluator=_LifecycleAwareGemmaAudit(router, StubGemmaAudit()),
    )
    assert result.processed_count >= 1
    events = _load_events(cfg.out_dir / PHASE_PROGRESS_FILENAME)
    chunk_started = [e for e in events if e["event"] == "chunk_started"]
    chunk_done = [e for e in events if e["event"] == "chunk_done"]
    assert len(chunk_started) == result.processed_count
    assert len(chunk_done) == result.processed_count
    assert {e["chunk_id"] for e in chunk_started} == {e["chunk_id"] for e in chunk_done}
    # Each chunk_done carries a journal outcome.
    for e in chunk_done:
        assert e["outcome"] in {"selected", "quarantined", "needs_synthesis", "incomplete_generation"}
    # Step 6 audit units: 2 per processed chunk, with started/done pairs.
    audit_started = {(e["chunk_id"], e["detector"]) for e in events if e["event"] == "audit_unit_started"}
    audit_done = {(e["chunk_id"], e["detector"]) for e in events if e["event"] == "audit_unit_done"}
    assert audit_started == audit_done
    assert len(audit_done) == 2 * result.processed_count
    assert {d for _c, d in audit_done} == set(DETECTORS)
    assert events[-1]["event"] == "audit_done"


def test_runner_step7_repair_and_terminal_events(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=8)
    router = _make_router()
    model_caller = _LifecycleAwareModelCaller(router, StubModelCaller())

    class _FlaggingAudit(StubQwenAudit):
        def __call__(self, *, chunk_id, source, translation):
            if "p00001" in translation:
                return json.dumps({"issues": [{"pid": "p00001", "category": "omission", "note": "x"}]})
            return json.dumps({"issues": []})

    class _RepairCaller:
        def __init__(self):
            self.calls = []

        def __call__(self, *, chunk_id, source, translation, region, findings):
            self.calls.append(chunk_id)
            return json.dumps({"repaired": {region.pid: "Исправлено."}, "reason": "x"})

    class _StubRegionGate:
        def __call__(self, *, source_text, repaired_text, region):
            from pact_v4.phase1.models import GateResult
            return GateResult(gate="region_fidelity", passed=True, detail="ok")

    class _StubRepairGemmaAudit(StubGemmaAudit):
        def __call__(self, *, chunk_id, translation):
            return json.dumps({"issues": []})

    class _StubRepairQwenAudit(StubQwenAudit):
        def __call__(self, *, chunk_id, source, translation):
            return json.dumps({"issues": []})

    repair_adapters = (
        _RepairCaller(), _StubRegionGate(),
        _StubRepairQwenAudit(), _StubRepairGemmaAudit(),
    )
    result = runner.run_chapter_strict(
        cfg, router=router, model_caller=model_caller,
        qwen_evaluator=_LifecycleAwareQwen(router, StubQwen()),
        gemma_selector=_LifecycleAwareGemmaSelector(router, StubGemma()),
        qwen_audit_evaluator=_LifecycleAwareQwenAudit(router, _FlaggingAudit()),
        gemma_audit_evaluator=_LifecycleAwareGemmaAudit(router, StubGemmaAudit()),
        repair_adapters=repair_adapters,
    )
    assert result.step7["status"] in ("complete", "accepted_degraded")
    events = _load_events(cfg.out_dir / PHASE_PROGRESS_FILENAME)
    event_names = [e["event"] for e in events]
    assert "repair_round_started" in event_names
    assert "region_started" in event_names
    assert "region_done" in event_names
    assert "repair_done" in event_names
    assert "terminal" in event_names
    region_started = [e for e in events if e["event"] == "region_started"]
    region_done = [e for e in events if e["event"] == "region_done"]
    assert len(region_started) == len(region_done)
    for e in region_started:
        assert e["chunk_id"] and e["repair_id"] and e["target_pids"] and e["action"]
    terminal = [e for e in events if e["event"] == "terminal"][-1]
    assert terminal["status"] == result.step8["status"]


def test_runner_resume_appends_run_started_with_resumed_from_index(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=8, max_consecutive=1)

    class _FailingQwen(StubQwen):
        def __init__(self):
            super().__init__(passed=False, reason="gate fail")

    router = _make_router()
    model_caller = _LifecycleAwareModelCaller(router, StubModelCaller())
    runner.run_chapter_strict(
        cfg, router=router, model_caller=model_caller,
        qwen_evaluator=_LifecycleAwareQwen(router, _FailingQwen()),
        gemma_selector=_LifecycleAwareGemmaSelector(router, StubGemma()),
        qwen_audit_evaluator=_LifecycleAwareQwenAudit(router, StubQwenAudit()),
        gemma_audit_evaluator=_LifecycleAwareGemmaAudit(router, StubGemmaAudit()),
    )
    path = cfg.out_dir / PHASE_PROGRESS_FILENAME
    events_before = _load_events(path)
    assert events_before[0]["event"] == "run_started"
    assert events_before[0]["resumed_from_index"] == 0

    # Resume: second run appends a new run_started with resumed_from_index > 0.
    router2 = _make_router()
    model_caller2 = _LifecycleAwareModelCaller(router2, StubModelCaller())
    result2 = runner.run_chapter_strict(
        cfg, router=router2, model_caller=model_caller2,
        qwen_evaluator=_LifecycleAwareQwen(router2, StubQwen()),
        gemma_selector=_LifecycleAwareGemmaSelector(router2, StubGemma()),
        qwen_audit_evaluator=_LifecycleAwareQwenAudit(router2, StubQwenAudit()),
        gemma_audit_evaluator=_LifecycleAwareGemmaAudit(router2, StubGemmaAudit()),
    )
    assert result2.resumed_from_index >= 1
    events_after = _load_events(path)
    # File is append-only: the first run_started is still first.
    assert events_after[0] == events_before[0]
    run_started = [e for e in events_after if e["event"] == "run_started"]
    assert run_started[-1]["resumed_from_index"] == result2.resumed_from_index
    assert len(run_started) == 2
