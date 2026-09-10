"""Book-chapter retry regressions (owner-approved change ``book-chapter-retry``).

Chapter level (chunked strict driver with stub backends, no model calls):
  * repair failure + ``--resume`` reuses generation/audit, reruns repair;
  * formatting failure + ``--resume`` reuses translation/repair artifacts;
  * ``--retry-incomplete`` rewinds to the first incomplete chunk and
    regenerates it plus the dependent tail, append-only with attempt markers;
  * the same journal without flags preserves the legacy positional resume;
  * foreign journal/stage identity stays fail-closed;
  * quarantined chunks are never retried;
  * durable stage checkpoints bind artifacts and roll back on corruption.

Book level (``run_book`` with the strict-chapter seam faked by the same stub
driver, so records/artifacts are genuine):
  * ready chapters skip model stages (promotion only);
  * a failed chapter resumes its failed stage while ready chapters and their
    observations stay intact (additive shared memory, no rollback);
  * repeated promotion is idempotent; ``--force-rerun-chapter`` wins;
  * ``--resume`` without an existing ``--out-base`` fails; changed
    chapter-run flags or chapter source fail closed; ``--retry-incomplete``
    is never forwarded book-wide.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from pact_v4.pipeline.v4_phase12_strict_runner import (
    StrictRunConfig,
    run_chapter_strict,
)
from pact_v4.pipeline import v4_retry
from pact_v4.pipeline.v4_retry import (
    chapter_extra_args,
    chapter_readiness,
    chapter_source_matches,
    entry_attempt,
    first_incomplete_index,
    first_unfinished_stage,
    load_stage_manifest,
    manifest_stage_complete,
    next_attempt,
    normalize_chapter_id,
    replay_entries,
    resume_index,
    should_skip_chapter,
    write_stage_checkpoint,
)
from tests.pact_v4.pipeline.test_v4_phase12_strict_runner_repair import (
    _flagging_audit as _repair_flagging_audit,
)
from tests.pact_v4.pipeline.test_v4_phase12_strict_runner import (
    _build_artifacts,
    StubGemma,
    StubGemmaAudit,
    StubModelCaller,
    StubQwen,
    StubQwenAudit,
    StubRegionGate,
    _LifecycleAwareGemmaAudit,
    _LifecycleAwareGemmaSelector,
    _LifecycleAwareModelCaller,
    _LifecycleAwareQwen,
    _LifecycleAwareQwenAudit,
    _make_backend,
    _make_cfg,
    _make_router,
    _write_chapter_html,
    _write_empty_memory,
)

THREE_CHUNKS = 40  # paragraphs -> 3 chunks (chunk0001..chunk0003)


# ---------------------------------------------------------------------------
# Chapter-level driver helpers
# ---------------------------------------------------------------------------


class _SelectiveFailCaller(StubModelCaller):
    """StubModelCaller that returns truncated JSON for armed chunk ids."""

    def __init__(self, fail_chunks=()) -> None:
        super().__init__()
        self.fail_chunks = set(fail_chunks)
        self.armed = True
        self.chunk_ids: List[str] = []

    def __call__(self, bundle) -> str:
        self.chunk_ids.append(bundle.chunk_id)
        if self.armed and bundle.chunk_id in self.fail_chunks:
            return '{"p00000": "перевод'
        return super().__call__(bundle)


class _StubRepairCaller:
    def __init__(self, text: str = "Исправленный перевод.") -> None:
        self._text = text
        self.calls: list = []

    def __call__(self, *, chunk_id, source, translation, region, findings) -> str:
        self.calls.append((chunk_id, region.pid))
        pid = region.pid
        return json.dumps({"repaired": {pid: self._text}, "reason": "scripted"},
                          ensure_ascii=False)


def _flagging_audit(pid: str) -> StubQwenAudit:
    class _Flagging(StubQwenAudit):
        def __call__(self, *, chunk_id, source, translation):
            if pid in translation:
                return json.dumps({"issues": [
                    {"pid": pid, "category": "omission", "note": "dropped clause"}
                ]})
            return json.dumps({"issues": []})
    return _Flagging()


def _run_chapter(
    cfg: StrictRunConfig,
    *,
    model_inner: Optional[StubModelCaller] = None,
    qwen: Optional[StubQwen] = None,
    qwen_audit: Optional[StubQwenAudit] = None,
    with_repair: bool = False,
    regate_passed: bool = True,
):
    """Run the chunked strict driver with stub backends; return (result, caller, repair)."""
    router = _make_router()
    inner = model_inner or StubModelCaller()
    repair_caller = _StubRepairCaller() if with_repair else None
    repair_adapters = None
    if with_repair:
        repair_adapters = (
            repair_caller,
            StubRegionGate(passed=regate_passed, reason="regate"),
            StubQwenAudit(),
            StubGemmaAudit(),
        )
    result = run_chapter_strict(
        cfg, router=router,
        model_caller=_LifecycleAwareModelCaller(router, inner),
        qwen_evaluator=_LifecycleAwareQwen(router, qwen or StubQwen()),
        gemma_selector=_LifecycleAwareGemmaSelector(router, StubGemma()),
        qwen_audit_evaluator=_LifecycleAwareQwenAudit(
            router, qwen_audit or StubQwenAudit()),
        gemma_audit_evaluator=_LifecycleAwareGemmaAudit(router, StubGemmaAudit()),
        repair_adapters=repair_adapters,
    )
    return result, inner, repair_caller


def _journal(out_dir: Path) -> List[Dict[str, Any]]:
    return [
        json.loads(line) for line in
        (out_dir / "journal.ndjson").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _fail_step_in_record(out_dir: Path, step: str) -> None:
    record_path = out_dir / "strict_chapter_trial_record.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record[step] = {"status": "failed", "error": "simulated stage failure"}
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                           encoding="utf-8")


# ---------------------------------------------------------------------------
# 3.3 / 3.4: retry-incomplete rewind vs legacy positional resume
# ---------------------------------------------------------------------------


def test_retry_incomplete_regenerates_tail_append_only(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=THREE_CHUNKS)
    caller = _SelectiveFailCaller(fail_chunks={"chunk0002"})
    result1, _, _ = _run_chapter(cfg, model_inner=caller)
    outcomes1 = [entry["outcome"] for entry in _journal(cfg.out_dir)]
    assert outcomes1 == ["selected", "incomplete_generation", "selected"]
    assert result1.incomplete_generation_count == 1
    journal_bytes_before = (cfg.out_dir / "journal.ndjson").read_bytes()

    caller.armed = False
    caller.chunk_ids.clear()
    cfg2 = dataclasses.replace(cfg, retry_incomplete=True)
    result2, _, _ = _run_chapter(cfg2, model_inner=caller)
    # Selected prefix reused without model calls; incomplete + tail regenerated.
    assert caller.chunk_ids == ["chunk0002", "chunk0003"]
    assert result2.incomplete_generation_count == 0
    # Journal stays append-only: old lines byte-identical, new lines appended
    # with monotonic attempt markers.
    journal_after = (cfg.out_dir / "journal.ndjson").read_bytes()
    assert journal_after.startswith(journal_bytes_before)
    assert len(journal_after) > len(journal_bytes_before)
    new_entries = _journal(cfg.out_dir)[3:]
    assert [entry["chunk_id"] for entry in new_entries] == ["chunk0002", "chunk0003"]
    assert all(entry_attempt(entry) == 1 for entry in new_entries)
    assert all(entry["outcome"] == "selected" for entry in new_entries)


def test_same_journal_without_flag_preserves_legacy_behavior(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=THREE_CHUNKS)
    caller = _SelectiveFailCaller(fail_chunks={"chunk0002"})
    _run_chapter(cfg, model_inner=caller)
    caller.armed = False
    caller.chunk_ids.clear()
    result2, _, _ = _run_chapter(cfg, model_inner=caller)
    # Legacy positional resume: the incomplete chunk is NOT regenerated.
    assert caller.chunk_ids == []
    assert result2.incomplete_generation_count == 1
    assert len(_journal(cfg.out_dir)) == 3


def test_retry_replay_picks_latest_attempt(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=THREE_CHUNKS)
    caller = _SelectiveFailCaller(fail_chunks={"chunk0002"})
    _run_chapter(cfg, model_inner=caller)
    caller.armed = False
    _run_chapter(dataclasses.replace(cfg, retry_incomplete=True), model_inner=caller)
    # Third run, no flags: replay uses the latest (selected) attempts, so no
    # stale incomplete is visible and nothing regenerates.
    caller.chunk_ids.clear()
    result3, _, _ = _run_chapter(cfg, model_inner=caller)
    assert caller.chunk_ids == []
    assert result3.incomplete_generation_count == 0
    replay = replay_entries(_journal(cfg.out_dir))
    assert [entry["chunk_id"] for entry in replay] == [
        "chunk0001", "chunk0002", "chunk0003"]
    assert all(entry["outcome"] == "selected" for entry in replay)


def test_quarantined_chunk_is_not_retried(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=THREE_CHUNKS)

    class _FailFirstGate(StubQwen):
        def __init__(self) -> None:
            super().__init__(passed=True)
            self._remaining = 2  # balanced + lazy fidelity_first of chunk1

        def __call__(self, source, translation):
            if self._remaining > 0:
                self._remaining -= 1
                from pact_v4.phase1.models import GateResult
                return GateResult(gate="qwen_fidelity", passed=False,
                                  detail="meaning drift")
            return super().__call__(source, translation)

    caller = _SelectiveFailCaller(fail_chunks={"chunk0002"})
    result1, _, _ = _run_chapter(cfg, model_inner=caller, qwen=_FailFirstGate())
    assert [entry["outcome"] for entry in _journal(cfg.out_dir)] == [
        "quarantined", "incomplete_generation", "selected"]

    caller.armed = False
    caller.chunk_ids.clear()
    _run_chapter(dataclasses.replace(cfg, retry_incomplete=True), model_inner=caller)
    # The quarantined prefix chunk gets no new generation attempt; only the
    # incomplete chunk and its dependent tail regenerate.
    assert caller.chunk_ids == ["chunk0002", "chunk0003"]


# ---------------------------------------------------------------------------
# 3.1 / 3.2: repair and formatting failure resume at their own stage
# ---------------------------------------------------------------------------


def test_repair_failure_resume_reruns_repair_not_generation(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=THREE_CHUNKS)
    result1, _, repair1 = _run_chapter(
        cfg, with_repair=True, qwen_audit=_repair_flagging_audit("p00001"))
    assert result1.step7["status"] in ("complete", "accepted_degraded")
    assert repair1.calls
    assert first_unfinished_stage(cfg.out_dir)[0] == "formatting"  # all satisfied
    # Simulate a repair failure after generation/audit completed.
    (cfg.out_dir / "repair_cache.json").unlink()
    (cfg.out_dir / "repair_report.json").unlink()
    _fail_step_in_record(cfg.out_dir, "step7")
    stage, _reason = first_unfinished_stage(cfg.out_dir)
    assert stage == "repair"

    caller2 = _SelectiveFailCaller()
    result2, _, repair2 = _run_chapter(
        dataclasses.replace(cfg, resume=True), model_inner=caller2,
        with_repair=True, qwen_audit=_repair_flagging_audit("p00001"))
    # Generation/audit reused: zero generation model calls; repair re-executed.
    assert caller2.chunk_ids == []
    assert repair2.calls
    assert result2.step7["status"] in ("complete", "accepted_degraded")
    assert (cfg.out_dir / "repair_report.json").exists()


def test_formatting_failure_resume_reruns_formatting_only(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=THREE_CHUNKS)
    result1, _, _ = _run_chapter(cfg, with_repair=True)
    assert result1.step8["status"] in ("complete", "accepted_degraded")
    assert (cfg.out_dir / "formatting_report.json").exists()
    # Simulate a formatting/finalization failure; repair stays valid.
    (cfg.out_dir / "formatting_report.json").unlink()
    _fail_step_in_record(cfg.out_dir, "step8")
    assert first_unfinished_stage(cfg.out_dir)[0] == "formatting"

    caller2 = _SelectiveFailCaller()
    result2, _, repair2 = _run_chapter(
        dataclasses.replace(cfg, resume=True), model_inner=caller2,
        with_repair=True)
    assert caller2.chunk_ids == []
    assert repair2.calls == []  # repair cache-resumed, not re-executed
    assert result2.step8["status"] in ("complete", "accepted_degraded")
    assert (cfg.out_dir / "formatting_report.json").exists()


# ---------------------------------------------------------------------------
# 3.5: foreign identity stays fail-closed
# ---------------------------------------------------------------------------


def test_foreign_identity_fail_closed_on_resume(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=THREE_CHUNKS)
    _run_chapter(cfg)
    foreign_backend = dataclasses.replace(
        _make_backend(), model_names={"gemma": "gemma-evil", "qwen": "qwen-fake"})
    with pytest.raises(ValueError, match="Foreign identity"):
        _run_chapter(dataclasses.replace(cfg, backend=foreign_backend, resume=True))
    with pytest.raises(ValueError, match="Foreign identity"):
        _run_chapter(dataclasses.replace(
            cfg, backend=foreign_backend, retry_incomplete=True))


def test_journal_identity_tamper_fail_closed(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=THREE_CHUNKS)
    _run_chapter(cfg)
    journal_path = cfg.out_dir / "journal.ndjson"
    lines = journal_path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[0])
    entry["config_identity"] = "tampered"
    lines[0] = json.dumps(entry, ensure_ascii=False)
    journal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Foreign identity"):
        _run_chapter(dataclasses.replace(cfg, resume=True))


# ---------------------------------------------------------------------------
# Additive shared memory: downstream observations do not invalidate stages
# ---------------------------------------------------------------------------


def test_memory_drift_tolerated_under_resume_flags(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=THREE_CHUNKS)
    _run_chapter(cfg, with_repair=True,
                 qwen_audit=_repair_flagging_audit("p00001"))
    # Downstream chapters promote observations into shared memory.
    glossary_path = cfg.memory_dir / "glossary.json"
    glossary = json.loads(glossary_path.read_text(encoding="utf-8"))
    glossary["DownstreamTerm"] = "НисходящийТермин"
    glossary_path.write_text(json.dumps(glossary, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    # A retry on current memory reuses completed stages instead of failing.
    caller = _SelectiveFailCaller()
    result, _, repair = _run_chapter(
        dataclasses.replace(cfg, resume=True), model_inner=caller,
        with_repair=True, qwen_audit=_repair_flagging_audit("p00001"))
    assert caller.chunk_ids == []
    assert result.step8["status"] in ("complete", "accepted_degraded")
    assert json.loads(glossary_path.read_text(encoding="utf-8"))["DownstreamTerm"] == \
        "НисходящийТермин"


def test_source_drift_still_fail_closed(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=THREE_CHUNKS)
    _run_chapter(cfg, with_repair=True)
    with open(cfg.chapter_html_path, "a", encoding="utf-8") as handle:
        handle.write("<p>Changed source text.</p>")
    with pytest.raises(ValueError, match="Foreign identity"):
        _run_chapter(dataclasses.replace(cfg, resume=True))


def test_lineage_helpers():
    from pact_v4.pipeline.v4_retry import (
        lineage_accepted,
        resolve_resume_lineage,
    )
    record = {
        "identities": {
            "source_hash": "src", "snapshot_hash": "snap1",
            "chunk_plan_hash": "plan1", "config_identity": "cfg",
        },
        "backend": {"identity_hash": "be1", "config_identity_hash": "be1"},
    }
    allowed, snap, plan = resolve_resume_lineage(
        record=record, source_hash="src", config_identity="cfg",
        acceptable_backend_hashes=["be1"],
    )
    assert (allowed, snap, plan) == (True, "snap1", "plan1")
    # Source drift never tolerates.
    assert resolve_resume_lineage(
        record=record, source_hash="CHANGED", config_identity="cfg",
        acceptable_backend_hashes=["be1"])[0] is False
    # Config drift never tolerates.
    assert resolve_resume_lineage(
        record=record, source_hash="src", config_identity="OTHER",
        acceptable_backend_hashes=["be1"])[0] is False
    # Backend drift never tolerates.
    assert resolve_resume_lineage(
        record=record, source_hash="src", config_identity="cfg",
        acceptable_backend_hashes=["other"])[0] is False
    # Pair matching: live pair, recorded pair (drift), or foreign.
    assert lineage_accepted("s", "p", live_snapshot_hash="s",
                            live_plan_hash="p") is True
    assert lineage_accepted("snap1", "plan1", live_snapshot_hash="s2",
                            live_plan_hash="p2", record_snapshot_hash="snap1",
                            record_plan_hash="plan1", allow_drift=True) is True
    assert lineage_accepted("snap1", "WRONG", live_snapshot_hash="s2",
                            live_plan_hash="p2", record_snapshot_hash="snap1",
                            record_plan_hash="plan1", allow_drift=True) is False
    assert lineage_accepted("snap1", "plan1", live_snapshot_hash="s2",
                            live_plan_hash="p2") is False


# ---------------------------------------------------------------------------
# Stage manifest + legacy inference
# ---------------------------------------------------------------------------


def test_stage_manifest_rolls_back_on_corrupt_artifact(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=THREE_CHUNKS)
    _run_chapter(cfg, with_repair=True)
    manifest = load_stage_manifest(cfg.out_dir)
    assert set(manifest) == {"generation", "audit", "repair", "formatting"}
    assert all(entry["status"] == "complete" for entry in manifest.values())
    # Corrupting a repair artifact rolls the resume point back to repair,
    # and a failed attempt never hides the last completed checkpoint.
    (cfg.out_dir / "repair_report.json").write_text("{corrupt", encoding="utf-8")
    ok, _reason = manifest_stage_complete(cfg.out_dir, "repair")
    assert not ok
    assert first_unfinished_stage(cfg.out_dir)[0] == "repair"
    write_stage_checkpoint(cfg.out_dir, "repair", status="failed", attempt=3)
    ok_after, _ = manifest_stage_complete(cfg.out_dir, "repair")
    assert not ok_after
    assert load_stage_manifest(cfg.out_dir)["repair"]["status"] == "complete"


def test_legacy_inference_without_manifest(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=THREE_CHUNKS)
    _run_chapter(cfg, with_repair=True)
    (cfg.out_dir / "stage_manifest.json").unlink()
    # Full legacy out-dir: record + artifacts verify.
    stage, _reason = first_unfinished_stage(cfg.out_dir)
    assert stage == "formatting"  # all stages satisfied
    # Missing repair artifacts roll back to repair even without a manifest.
    (cfg.out_dir / "repair_report.json").unlink()
    (cfg.out_dir / "repair_cache.json").unlink()
    _fail_step_in_record(cfg.out_dir, "step7")
    assert first_unfinished_stage(cfg.out_dir)[0] == "repair"
    # Missing journal means generation never committed.
    (cfg.out_dir / "journal.ndjson").unlink()
    assert first_unfinished_stage(cfg.out_dir)[0] == "generation"


def test_boundary_negatives_symlink_fifo_corrupt_manifest(tmp_path: Path):
    import os
    cfg = _make_cfg(tmp_path, n_paragraphs=THREE_CHUNKS)
    _run_chapter(cfg, with_repair=True)
    assert chapter_readiness(cfg.out_dir)["ready"] is True
    # Symlinked artifact: never a regular file -> not reusable, not ready.
    translations = cfg.out_dir / "translations.json"
    real = translations.read_bytes()
    translations.unlink()
    translations.symlink_to(cfg.out_dir / "selection_results.json")
    assert chapter_readiness(cfg.out_dir)["ready"] is False
    ok, _reason = manifest_stage_complete(cfg.out_dir, "formatting")
    assert ok is False
    translations.unlink()
    translations.write_bytes(real)
    assert chapter_readiness(cfg.out_dir)["ready"] is True
    # FIFO in place of an artifact: rejected, never read as JSON.
    formatting = cfg.out_dir / "formatting_report.json"
    formatting.unlink()
    os.mkfifo(formatting)
    ok, _reason = manifest_stage_complete(cfg.out_dir, "formatting")
    assert ok is False
    assert first_unfinished_stage(cfg.out_dir)[0] == "formatting"
    formatting.unlink()
    formatting.write_text('{"schema": "x"}', encoding="utf-8")
    # Corrupt manifest: never trusted, legacy inference takes over.
    (cfg.out_dir / "stage_manifest.json").write_text("{corrupt", encoding="utf-8")
    assert load_stage_manifest(cfg.out_dir) == {}
    assert first_unfinished_stage(cfg.out_dir)[0] == "formatting"


def test_unknown_manifest_stage_ignored(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=THREE_CHUNKS)
    _run_chapter(cfg, with_repair=True)
    manifest_path = cfg.out_dir / "stage_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["stages"]["exotic_future_stage"] = {"status": "complete"}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    # Unknown stages never affect the resume decision.
    assert first_unfinished_stage(cfg.out_dir)[0] == "formatting"


def test_chapter_readiness_and_source_gate(tmp_path: Path):
    cfg = _make_cfg(tmp_path, n_paragraphs=THREE_CHUNKS)
    _run_chapter(cfg, with_repair=True)
    readiness = chapter_readiness(cfg.out_dir)
    assert readiness["ready"] is True
    assert readiness["terminal_status"] == "complete"
    ok, _reason = chapter_source_matches(cfg.out_dir, cfg.chapter_html_path)
    assert ok is True
    # A changed chapter source fails the skip gate closed.
    with open(cfg.chapter_html_path, "a", encoding="utf-8") as handle:
        handle.write("<p>Extra paragraph changes the source hash.</p>")
    ok_changed, _reason = chapter_source_matches(cfg.out_dir, cfg.chapter_html_path)
    assert ok_changed is False


# ---------------------------------------------------------------------------
# v4_retry pure decision logic
# ---------------------------------------------------------------------------


def test_resume_index_and_replay_helpers():
    entries = [
        {"chunk_id": "a", "chunk_index": 0, "outcome": "selected"},
        {"chunk_id": "b", "chunk_index": 1, "outcome": "incomplete_generation"},
        {"chunk_id": "c", "chunk_index": 2, "outcome": "selected"},
    ]
    assert resume_index(entries) == 3
    assert resume_index(entries, retry_incomplete=True) == 1
    assert resume_index(entries, force_rerun=True) == 0
    assert first_incomplete_index(entries) == 1
    assert first_incomplete_index([]) is None
    assert next_attempt(entries) == 1
    assert next_attempt([]) == 1
    retried = entries + [
        {"chunk_id": "b", "chunk_index": 1, "outcome": "selected",
         "attempt": 1, "revision": 1},
    ]
    assert next_attempt(retried) == 2
    assert entry_attempt({"chunk_id": "x"}) == 0
    replay = replay_entries(retried)
    assert [entry["chunk_id"] for entry in replay] == ["a", "b", "c"]
    assert [entry["outcome"] for entry in replay] == [
        "selected", "selected", "selected"]


def test_skip_and_forwarding_helpers(tmp_path: Path):
    assert normalize_chapter_id("1") == normalize_chapter_id("0001")
    assert normalize_chapter_id("abc") == "abc"
    skip, _reason = should_skip_chapter(tmp_path, resume=False)
    assert skip is False
    skip, _reason = should_skip_chapter(
        tmp_path, resume=True, force_rerun_ids=["1"], chapter_id="0001")
    assert skip is False
    skip, _reason = should_skip_chapter(tmp_path, resume=True, chapter_id="0001")
    assert skip is False  # no record -> not ready
    assert chapter_extra_args([], resume=True) == ["--resume"]
    assert chapter_extra_args(["--resume"], resume=True) == ["--resume"]
    assert chapter_extra_args([], resume=True, force_rerun=True) == ["--force-rerun"]
    # --retry-incomplete is never added blindly.
    assert "--retry-incomplete" not in chapter_extra_args([], resume=True)


# ---------------------------------------------------------------------------
# Round-1 HIGH-1: readiness validates every completed stage checkpoint set.
# ---------------------------------------------------------------------------


def _full_complete_run(tmp_path: Path, *, flagging: bool = False):
    """Full stub run ending terminal-complete with all stage artifacts."""
    cfg = _make_cfg(tmp_path, n_paragraphs=THREE_CHUNKS)
    if flagging:
        result, _, _ = _run_chapter(
            cfg, with_repair=True, qwen_audit=_repair_flagging_audit("p00001"))
    else:
        result, _, _ = _run_chapter(cfg, with_repair=True)
    assert result.step8["status"] == "complete", result.step8
    assert chapter_readiness(cfg.out_dir)["ready"] is True
    return cfg


def test_readiness_rejects_deleted_audit_artifacts(tmp_path: Path):
    cfg = _full_complete_run(tmp_path)
    for name in ("audit_cache.json", "audit_findings.json", "b2_handoff.json"):
        payload = (cfg.out_dir / name).read_bytes()
        (cfg.out_dir / name).unlink()
        readiness = chapter_readiness(cfg.out_dir)
        assert readiness["ready"] is False, name
        assert readiness["resume_stage"] == "audit", (name, readiness)
        assert "audit" in readiness["reason"], (name, readiness)
        (cfg.out_dir / name).write_bytes(payload)
    assert chapter_readiness(cfg.out_dir)["ready"] is True


def test_readiness_rejects_deleted_repair_artifacts(tmp_path: Path):
    cfg = _full_complete_run(tmp_path)
    for name in ("repair_cache.json", "repair_report.json"):
        payload = (cfg.out_dir / name).read_bytes()
        (cfg.out_dir / name).unlink()
        readiness = chapter_readiness(cfg.out_dir)
        assert readiness["ready"] is False, name
        assert readiness["resume_stage"] == "repair", (name, readiness)
        (cfg.out_dir / name).write_bytes(payload)
    assert chapter_readiness(cfg.out_dir)["ready"] is True


def test_readiness_rejects_deleted_formatting_report(tmp_path: Path):
    cfg = _full_complete_run(tmp_path)
    payload = (cfg.out_dir / "formatting_report.json").read_bytes()
    (cfg.out_dir / "formatting_report.json").unlink()
    readiness = chapter_readiness(cfg.out_dir)
    assert readiness["ready"] is False
    assert readiness["resume_stage"] == "formatting"
    (cfg.out_dir / "formatting_report.json").write_bytes(payload)
    assert chapter_readiness(cfg.out_dir)["ready"] is True


def test_readiness_rejects_corrupt_generation_outcomes(tmp_path: Path):
    cfg = _full_complete_run(tmp_path)
    payload = (cfg.out_dir / "generation_outcomes.json").read_bytes()
    (cfg.out_dir / "generation_outcomes.json").write_text("{corrupt", encoding="utf-8")
    readiness = chapter_readiness(cfg.out_dir)
    assert readiness["ready"] is False
    assert readiness["resume_stage"] == "generation"
    (cfg.out_dir / "generation_outcomes.json").write_bytes(payload)
    assert chapter_readiness(cfg.out_dir)["ready"] is True


def test_readiness_rejects_integrity_tamper(tmp_path: Path):
    cfg = _full_complete_run(tmp_path)
    # Valid JSON with altered content: parse checks pass, the manifest
    # integrity hash must still catch it.
    translations = json.loads((cfg.out_dir / "translations.json").read_text(encoding="utf-8"))
    first_pid = sorted(translations)[0]
    raw = (cfg.out_dir / "translations.json").read_bytes()
    translations[first_pid] = translations[first_pid] + " X"
    (cfg.out_dir / "translations.json").write_text(
        json.dumps(translations, ensure_ascii=False, indent=2), encoding="utf-8")
    readiness = chapter_readiness(cfg.out_dir)
    assert readiness["ready"] is False
    assert "integrity mismatch" in readiness["reason"], readiness
    (cfg.out_dir / "translations.json").write_bytes(raw)
    assert chapter_readiness(cfg.out_dir)["ready"] is True


def test_readiness_legacy_validates_stage_sets(tmp_path: Path):
    cfg = _full_complete_run(tmp_path)
    (cfg.out_dir / "stage_manifest.json").unlink()
    assert chapter_readiness(cfg.out_dir)["ready"] is True
    # Legacy out-dir: each deleted stage artifact rolls readiness back to
    # its own stage (audit triple, repair pair, formatting report).
    cases = [
        ("audit_cache.json", "audit"),
        ("repair_cache.json", "repair"),
        ("formatting_report.json", "formatting"),
    ]
    for name, stage in cases:
        payload = (cfg.out_dir / name).read_bytes()
        (cfg.out_dir / name).unlink()
        readiness = chapter_readiness(cfg.out_dir)
        assert readiness["ready"] is False, name
        assert readiness["resume_stage"] == stage, (name, readiness)
        (cfg.out_dir / name).write_bytes(payload)
    assert chapter_readiness(cfg.out_dir)["ready"] is True


# ---------------------------------------------------------------------------
# Round-1 HIGH-2: stage-selected resume skips preceding stages for real.
# ---------------------------------------------------------------------------

import pact_v4.pipeline.v4_phase12_strict_runner as runner_mod


def _run_with_stage_spies(cfg, monkeypatch, **kwargs):
    """Run the chapter driver counting Step 6 / Step 7 invocations.

    Returns ``(result, model_caller, repair_caller, calls)`` where
    ``calls`` maps ``\"audit\"`` -> ``run_chapter_audit`` invocations and
    ``\"repair_phase\"`` -> ``run_repair_phase`` invocations.
    """
    calls = {"audit": 0, "repair_phase": 0}
    real_audit = runner_mod.run_chapter_audit
    real_repair = runner_mod.run_repair_phase

    def _spy_audit(*args, **kwargs):
        calls["audit"] += 1
        return real_audit(*args, **kwargs)

    def _spy_repair(*args, **kwargs):
        calls["repair_phase"] += 1
        return real_repair(*args, **kwargs)

    monkeypatch.setattr(runner_mod, "run_chapter_audit", _spy_audit)
    monkeypatch.setattr(runner_mod, "run_repair_phase", _spy_repair)
    result, inner, repair = _run_chapter(cfg, **kwargs)
    return result, inner, repair, calls


def test_audit_failure_resume_invokes_audit_and_dependents(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path, n_paragraphs=THREE_CHUNKS)
    _run_chapter(cfg, with_repair=True,
                 qwen_audit=_repair_flagging_audit("p00001"))
    # Simulate an audit failure: audit triple gone, record agrees.
    for name in ("audit_cache.json", "audit_findings.json", "b2_handoff.json"):
        (cfg.out_dir / name).unlink()
    _fail_step_in_record(cfg.out_dir, "step6")
    assert first_unfinished_stage(cfg.out_dir)[0] == "audit"
    caller2 = _SelectiveFailCaller()
    result2, _, _, calls = _run_with_stage_spies(
        dataclasses.replace(cfg, resume=True), monkeypatch,
        model_inner=caller2, with_repair=True,
        qwen_audit=_repair_flagging_audit("p00001"))
    # Audit re-ran with dependents; generation was not repeated.
    assert caller2.chunk_ids == []
    assert calls["audit"] >= 1
    assert calls["repair_phase"] >= 1
    assert result2.step6["status"] == "complete"
    assert result2.step6.get("reused") is not True
    assert result2.step7["status"] in ("complete", "accepted_degraded")
    assert result2.step8["status"] in ("complete", "accepted_degraded")


def test_repair_only_resume_skips_audit_stage(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path, n_paragraphs=THREE_CHUNKS)
    _run_chapter(cfg, with_repair=True,
                 qwen_audit=_repair_flagging_audit("p00001"))
    # Simulate a repair failure after generation/audit completed. The
    # record keeps step8 complete on purpose: manifest selection alone
    # must drive the repair-only resume.
    (cfg.out_dir / "repair_cache.json").unlink()
    (cfg.out_dir / "repair_report.json").unlink()
    _fail_step_in_record(cfg.out_dir, "step7")
    assert first_unfinished_stage(cfg.out_dir)[0] == "repair"
    caller2 = _SelectiveFailCaller()
    result2, _, repair2, calls = _run_with_stage_spies(
        dataclasses.replace(cfg, resume=True), monkeypatch,
        model_inner=caller2, with_repair=True,
        qwen_audit=_repair_flagging_audit("p00001"))
    # Audit stage NOT invoked; repair re-executed; generation untouched.
    assert calls["audit"] == 0
    assert calls["repair_phase"] >= 1
    assert caller2.chunk_ids == []
    assert repair2.calls, "repair must re-execute, not just cache-skip"
    assert result2.step6.get("reused") is True
    assert result2.step6["status"] == "complete"
    assert result2.step7["status"] in ("complete", "accepted_degraded")
    assert result2.step8["status"] in ("complete", "accepted_degraded")
    assert (cfg.out_dir / "repair_report.json").exists()


def test_formatting_only_resume_skips_audit_and_repair(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path, n_paragraphs=THREE_CHUNKS)
    result1, _, _ = _run_chapter(cfg, with_repair=True)
    assert result1.step8["status"] == "complete"
    before_translations = (cfg.out_dir / "translations.json").read_bytes()
    # Simulate a formatting/finalization failure; audit+repair stay valid.
    (cfg.out_dir / "formatting_report.json").unlink()
    _fail_step_in_record(cfg.out_dir, "step8")
    assert first_unfinished_stage(cfg.out_dir)[0] == "formatting"
    caller2 = _SelectiveFailCaller()
    result2, _, repair2, calls = _run_with_stage_spies(
        dataclasses.replace(cfg, resume=True), monkeypatch,
        model_inner=caller2, with_repair=True)
    # Neither audit nor repair invoked; no model calls at all; only
    # deterministic formatting ran and rewrote its report.
    assert calls == {"audit": 0, "repair_phase": 0}
    assert caller2.chunk_ids == []
    assert repair2.calls == []
    assert result2.step6.get("reused") is True
    assert (cfg.out_dir / "formatting_report.json").exists()
    assert result2.step8["status"] in ("complete", "accepted_degraded")
    assert result2.step7.get("formatting") is not None
    assert (cfg.out_dir / "translations.json").read_bytes() == before_translations


def test_repair_only_resume_under_memory_drift_skips_audit(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path, n_paragraphs=THREE_CHUNKS)
    _run_chapter(cfg, with_repair=True,
                 qwen_audit=_repair_flagging_audit("p00001"))
    # Downstream observations move the live snapshot (additive memory)...
    glossary_path = cfg.memory_dir / "glossary.json"
    glossary = json.loads(glossary_path.read_text(encoding="utf-8"))
    glossary["DownstreamTerm"] = "НисходящийТермин"
    glossary_path.write_text(json.dumps(glossary, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    # ...and repair failed afterwards.
    (cfg.out_dir / "repair_cache.json").unlink()
    (cfg.out_dir / "repair_report.json").unlink()
    _fail_step_in_record(cfg.out_dir, "step7")
    caller2 = _SelectiveFailCaller()
    result2, _, repair2, calls = _run_with_stage_spies(
        dataclasses.replace(cfg, resume=True), monkeypatch,
        model_inner=caller2, with_repair=True,
        qwen_audit=_repair_flagging_audit("p00001"))
    # Audit artifacts are reused (text-identity proof across drift), repair
    # re-executes on current memory, shared observations are not rolled back.
    assert calls["audit"] == 0
    assert calls["repair_phase"] >= 1
    assert caller2.chunk_ids == []
    assert repair2.calls
    assert result2.step6.get("reused") is True
    assert result2.step8["status"] in ("complete", "accepted_degraded")
    assert json.loads(glossary_path.read_text(encoding="utf-8"))["DownstreamTerm"] == \
        "НисходящийТермин"


def test_no_flags_never_skips_stages(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path, n_paragraphs=THREE_CHUNKS)
    _run_chapter(cfg, with_repair=True,
                 qwen_audit=_repair_flagging_audit("p00001"))
    (cfg.out_dir / "repair_cache.json").unlink()
    (cfg.out_dir / "repair_report.json").unlink()
    _fail_step_in_record(cfg.out_dir, "step7")
    # Without resume flags the legacy full path runs: audit invoked even
    # though its artifacts are valid.
    _, _, _, calls = _run_with_stage_spies(cfg, monkeypatch, with_repair=True,
                                           qwen_audit=_repair_flagging_audit("p00001"))
    assert calls["audit"] >= 1
    assert calls["repair_phase"] >= 1


def test_formatting_only_resume_under_memory_drift(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path, n_paragraphs=THREE_CHUNKS)
    result1, _, _ = _run_chapter(cfg, with_repair=True)
    assert result1.step8["status"] == "complete"
    before_translations = (cfg.out_dir / "translations.json").read_bytes()
    # Downstream observations move the live snapshot (additive memory)...
    glossary_path = cfg.memory_dir / "glossary.json"
    glossary = json.loads(glossary_path.read_text(encoding="utf-8"))
    glossary["DownstreamTerm"] = "НисходящийТермин"
    glossary_path.write_text(json.dumps(glossary, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    # ...and formatting failed afterwards.
    (cfg.out_dir / "formatting_report.json").unlink()
    _fail_step_in_record(cfg.out_dir, "step8")
    caller2 = _SelectiveFailCaller()
    result2, _, repair2, calls = _run_with_stage_spies(
        dataclasses.replace(cfg, resume=True), monkeypatch,
        model_inner=caller2, with_repair=True)
    # Formatting-only stays real across drift: the report belongs to the
    # same audited assembly whose findings were reused, and no predecessor
    # stage is invoked.
    assert calls == {"audit": 0, "repair_phase": 0}
    assert caller2.chunk_ids == []
    assert repair2.calls == []
    assert result2.step6.get("reused") is True
    assert (cfg.out_dir / "formatting_report.json").exists()
    assert result2.step8["status"] in ("complete", "accepted_degraded")
    assert (cfg.out_dir / "translations.json").read_bytes() == before_translations
    assert json.loads(glossary_path.read_text(encoding="utf-8"))["DownstreamTerm"] == \
        "НисходящийТермин"


# ---------------------------------------------------------------------------
# Round-2 HIGH-2: truncated manifests never validate (exact set + hash map).
# ---------------------------------------------------------------------------


def _rewrite_manifest(out_dir: Path, mutator) -> None:
    manifest_path = out_dir / "stage_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutator(manifest["stages"])
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                             encoding="utf-8")


def test_manifest_rejects_removed_artifact(tmp_path: Path):
    from pact_v4.pipeline.v4_retry import manifest_stage_complete
    cfg = _full_complete_run(tmp_path)
    # Consistent truncation (artifact AND its hash removed together) must
    # still be rejected via exact set validation.
    _rewrite_manifest(cfg.out_dir, lambda stages: (
        stages["audit"]["artifacts"].remove("b2_handoff.json"),
        stages["audit"]["integrity"].pop("b2_handoff.json", None),
    ))
    ok, reason = manifest_stage_complete(cfg.out_dir, "audit")
    assert not ok, reason
    assert "artifact set mismatch" in reason, reason
    assert first_unfinished_stage(cfg.out_dir)[0] == "audit"
    readiness = chapter_readiness(cfg.out_dir)
    assert readiness["ready"] is False
    assert readiness["resume_stage"] == "audit"


def test_manifest_rejects_removed_hash_entry(tmp_path: Path):
    from pact_v4.pipeline.v4_retry import manifest_stage_complete
    cfg = _full_complete_run(tmp_path)
    # Files untouched, but one hash entry deleted: exact map validation
    # must reject even though every listed file would verify.
    _rewrite_manifest(cfg.out_dir, lambda stages: (
        stages["repair"]["integrity"].pop("repair_cache.json", None),
    ))
    ok, reason = manifest_stage_complete(cfg.out_dir, "repair")
    assert not ok, reason
    assert "integrity map mismatch" in reason, reason
    assert first_unfinished_stage(cfg.out_dir)[0] == "repair"
    readiness = chapter_readiness(cfg.out_dir)
    assert readiness["ready"] is False
    assert readiness["resume_stage"] == "repair"


def test_manifest_rejects_added_artifact_and_bad_hash(tmp_path: Path):
    from pact_v4.pipeline.v4_retry import manifest_stage_complete
    cfg = _full_complete_run(tmp_path)
    # Added file with a correctly computed hash: still not a legitimate
    # complete set.
    _rewrite_manifest(cfg.out_dir, lambda stages: (
        stages["audit"]["artifacts"].append("selection_results.json"),
        stages["audit"]["integrity"].__setitem__(
            "selection_results.json",
            json.loads((cfg.out_dir / "stage_manifest.json").read_text())
            ["stages"]["generation"]["integrity"].get("selection_results.json",
                                                      "0" * 64)),
    ))
    ok, reason = manifest_stage_complete(cfg.out_dir, "audit")
    assert not ok, reason
    assert "artifact set mismatch" in reason, reason
    # Placeholder / malformed hash values are rejected, not compared.
    _rewrite_manifest(cfg.out_dir, lambda stages: (
        stages["repair"]["integrity"].__setitem__("repair_report.json", ""),
    ))
    ok, reason = manifest_stage_complete(cfg.out_dir, "repair")
    assert not ok, reason
    assert "invalid integrity hash" in reason, reason
    _rewrite_manifest(cfg.out_dir, lambda stages: (
        stages["repair"]["integrity"].__setitem__("repair_report.json", None),
    ))
    ok, _reason = manifest_stage_complete(cfg.out_dir, "repair")
    assert not ok


_FULL_INPUTS = {
    "snapshot_hash": "snap", "chunk_plan_hash": "plan",
    "config_identity": "cfg", "backend_identity_hash": "be",
}


def test_manifest_skipped_and_whole_chapter_empties(tmp_path: Path):
    from pact_v4.pipeline.v4_retry import manifest_stage_complete
    base = {
        "status": "complete", "attempt": 0,
        "inputs": {**_FULL_INPUTS, "step_status": "skipped_no_adapters"},
        "artifacts": [], "integrity": {},
    }
    manifest = {"audit": dict(base)}
    ok, _reason = manifest_stage_complete(tmp_path, "audit", manifest)
    assert ok is True
    # A skipped entry claiming files is malformed.
    tampered = {"audit": {**base, "artifacts": ["audit_cache.json"],
                          "integrity": {"audit_cache.json": "0" * 64}}}
    ok, _reason = manifest_stage_complete(tmp_path, "audit", tampered)
    assert not ok
    # Whole-chapter audit binds its real B3 artifacts (journal + cache):
    # an empty complete set is rejected in every mode -- empties are only
    # legitimate for genuinely skipped steps.
    wc_manifest = {
        "generation": {
            "status": "complete", "attempt": 0, "inputs": dict(_FULL_INPUTS),
            "artifacts": ["journal.ndjson", "generation_outcomes.json",
                          "selection_results.json", "translations_raw.json",
                          "translations.json"],
            "integrity": {name: "a" * 64 for name in [
                "journal.ndjson", "generation_outcomes.json",
                "selection_results.json", "translations_raw.json",
                "translations.json"]},
        },
        "audit": {"status": "complete", "attempt": 0,
                  "inputs": {**_FULL_INPUTS, "step_status": "complete"},
                  "artifacts": [], "integrity": {}},
    }
    ok, reason = manifest_stage_complete(tmp_path, "audit", wc_manifest)
    assert not ok, reason
    assert "artifact set mismatch" in reason, reason
    # ...and the same holds for a chunked-mode manifest with an empty set.
    chunked_manifest = {
        "generation": {
            "status": "complete", "attempt": 0, "inputs": dict(_FULL_INPUTS),
            "artifacts": ["journal.ndjson", "generation_outcomes.json",
                          "selection_results.json", "translations.json"],
            "integrity": {},
        },
        "audit": {"status": "complete", "attempt": 0,
                  "inputs": {**_FULL_INPUTS, "step_status": "complete"},
                  "artifacts": [], "integrity": {}},
    }
    ok, reason = manifest_stage_complete(tmp_path, "audit", chunked_manifest)
    assert not ok, reason
    assert "artifact set mismatch" in reason, reason


def test_manifest_rejects_missing_and_altered_inputs(tmp_path: Path):
    from pact_v4.pipeline.v4_retry import artifact_sha256, manifest_stage_complete
    (tmp_path / "repair_cache.json").write_text('{"cache": true}',
                                                encoding="utf-8")
    (tmp_path / "repair_report.json").write_text('{"report": true}',
                                                 encoding="utf-8")
    good = {
        "status": "complete", "attempt": 0,
        "inputs": dict(_FULL_INPUTS),
        "artifacts": ["repair_cache.json", "repair_report.json"],
        "integrity": {
            "repair_cache.json": artifact_sha256(
                tmp_path / "repair_cache.json"),
            "repair_report.json": artifact_sha256(
                tmp_path / "repair_report.json"),
        },
    }
    assert manifest_stage_complete(
        tmp_path, "repair", {"repair": dict(good)}) == (True, "ok")
    # Each missing input field rejects, even with a perfect file set.
    for field in ("snapshot_hash", "chunk_plan_hash", "config_identity",
                  "backend_identity_hash"):
        mutated = {**good, "inputs": {
            key: value for key, value in good["inputs"].items()
            if key != field}}
        ok, reason = manifest_stage_complete(
            tmp_path, "repair", {"repair": mutated})
        assert not ok, field
        assert f"missing checkpoint input {field}" in reason, (field, reason)
    # Altered (empty/non-string) input values reject the same way.
    for field, bad_value in (("snapshot_hash", ""), ("config_identity", None),
                             ("backend_identity_hash", 42)):
        mutated = {**good, "inputs": {**good["inputs"], field: bad_value}}
        ok, reason = manifest_stage_complete(
            tmp_path, "repair", {"repair": mutated})
        assert not ok, (field, bad_value)
        assert "missing checkpoint input" in reason, (field, reason)
    # Missing inputs mapping entirely also rejects.
    mutated = {key: value for key, value in good.items() if key != "inputs"}
    ok, _reason = manifest_stage_complete(
        tmp_path, "repair", {"repair": mutated})
    assert not ok


# ---------------------------------------------------------------------------
# Round-2 HIGH-1: fully satisfied chapter resumes with zero model calls.
# ---------------------------------------------------------------------------


def test_completed_resume_zero_model_calls(tmp_path, monkeypatch):
    cfg = _full_complete_run(tmp_path)
    journal_before = (cfg.out_dir / "journal.ndjson").read_bytes()
    translations_before = (cfg.out_dir / "translations.json").read_bytes()
    caller2 = _SelectiveFailCaller()
    result2, _, repair2, calls = _run_with_stage_spies(
        dataclasses.replace(cfg, resume=True), monkeypatch,
        model_inner=caller2, with_repair=True)
    # No stage invoked at all: no audit, no repair phase, no generation,
    # no repair calls -- only finalization/checkpoints below the stages.
    assert calls == {"audit": 0, "repair_phase": 0}
    assert caller2.chunk_ids == []
    assert repair2.calls == []
    assert result2.step6.get("reused") is True
    assert result2.step8["status"] == "complete"
    assert result2.step7["status"] == "complete"
    # Append-only journal untouched; converged translation byte-identical.
    assert (cfg.out_dir / "journal.ndjson").read_bytes() == journal_before
    assert (cfg.out_dir / "translations.json").read_bytes() == translations_before
    # Manifest still fully validates after the completed resume.
    assert first_unfinished_stage(cfg.out_dir) == (
        "formatting", "all stages satisfied")


def test_completed_resume_legacy_without_manifest(tmp_path, monkeypatch):
    cfg = _full_complete_run(tmp_path)
    (cfg.out_dir / "stage_manifest.json").unlink()
    assert chapter_readiness(cfg.out_dir)["ready"] is True
    caller2 = _SelectiveFailCaller()
    result2, _, repair2, calls = _run_with_stage_spies(
        dataclasses.replace(cfg, resume=True), monkeypatch,
        model_inner=caller2, with_repair=True)
    assert calls == {"audit": 0, "repair_phase": 0}
    assert caller2.chunk_ids == []
    assert repair2.calls == []
    assert result2.step6.get("reused") is True
    assert result2.step8["status"] == "complete"


def test_refresh_refuses_truncated_entry(tmp_path: Path):
    from pact_v4.pipeline.v4_retry import refresh_stage_hashes
    cfg = _full_complete_run(tmp_path)
    _rewrite_manifest(cfg.out_dir, lambda stages: (
        stages["audit"]["artifacts"].remove("b2_handoff.json"),
        stages["audit"]["integrity"].pop("b2_handoff.json", None),
    ))
    before = (cfg.out_dir / "stage_manifest.json").read_bytes()
    # Refresh must not bless the truncation into hash-validity.
    assert refresh_stage_hashes(cfg.out_dir, ("audit",)) is False
    assert (cfg.out_dir / "stage_manifest.json").read_bytes() == before
    assert first_unfinished_stage(cfg.out_dir)[0] == "audit"
    # ...while a conforming entry refreshes fine.
    assert refresh_stage_hashes(cfg.out_dir, ("generation",)) is True


# ---------------------------------------------------------------------------
# Round-3 HIGH-1: present-but-unreadable manifest blocks promotion.
# ---------------------------------------------------------------------------


def test_corrupt_manifest_blocks_promotion_selects_safe_resume(tmp_path: Path,
                                                            monkeypatch):
    cfg = _full_complete_run(tmp_path)
    assert chapter_readiness(cfg.out_dir)["ready"] is True
    manifest_path = cfg.out_dir / "stage_manifest.json"
    # Each corruption shape: promotion blocked, safe resume selected from
    # scratch-verified artifacts (all intact -> completed path available).
    for broken in ("{broken",
                   '{"schema": "foreign/v9", "stages": {}}',
                   "[1, 2]"):
        manifest_path.write_text(broken, encoding="utf-8")
        readiness = chapter_readiness(cfg.out_dir)
        assert readiness["ready"] is False, broken
        assert "manifest" in readiness["reason"], (broken, readiness)
        assert first_unfinished_stage(cfg.out_dir) == (
            "formatting", "all stages satisfied")
    # Genuinely absent manifest: legacy inference may still find ready.
    manifest_path.unlink()
    assert chapter_readiness(cfg.out_dir)["ready"] is True
    # End-to-end self-healing: chapter --resume on the corrupted manifest
    # completes with zero model calls and rewrites a valid manifest,
    # restoring readiness.
    manifest_path.write_text("{broken", encoding="utf-8")
    from pact_v4.pipeline import v4_retry as _retry_mod
    assert _retry_mod.stage_manifest_present(cfg.out_dir) is True
    caller2 = _SelectiveFailCaller()
    result2, _, repair2, calls = _run_with_stage_spies(
        dataclasses.replace(cfg, resume=True), monkeypatch,
        model_inner=caller2, with_repair=True)
    assert calls == {"audit": 0, "repair_phase": 0}
    assert caller2.chunk_ids == []
    assert result2.step8["status"] == "complete"
    assert chapter_readiness(cfg.out_dir)["ready"] is True


# ---------------------------------------------------------------------------
# Round-3 HIGH-2: repair resume never regenerates quarantined chunks (B6).
# ---------------------------------------------------------------------------


def _quarantine_chunk1_gate(chunk1_pids):
    """Qwen gate failing every unit whose source lies in chunk0001."""
    from pact_v4.phase1.models import GateResult

    class _FailChunk1(StubQwen):
        def __call__(self, source, translation):
            if source and set(source) <= set(chunk1_pids):
                return GateResult(gate="qwen_fidelity", passed=False,
                                  detail="meaning drift")
            return super().__call__(source, translation)

    return _FailChunk1()


def test_repair_resume_does_not_regenerate_quarantined(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path, n_paragraphs=THREE_CHUNKS)
    _, _, chunk_plan, _ = _build_artifacts(cfg)
    chunk1_pids = set(chunk_plan.chunks[0].pids)
    flag_pid = sorted(chunk1_pids)[0]
    # Run 1: chunk0001 quarantined with repair debt (failing re-gate), so
    # the B6 cycle fires and regenerates it (generation happens here).
    caller1 = _SelectiveFailCaller()
    result1, _, _ = _run_chapter(
        cfg, model_inner=caller1,
        qwen=_quarantine_chunk1_gate(chunk1_pids),
        qwen_audit=_repair_flagging_audit(flag_pid),
        with_repair=True, regate_passed=False)
    assert [entry["outcome"] for entry in _journal(cfg.out_dir)] == [
        "quarantined", "selected", "selected"]
    assert result1.step7.get("quarantined_retry", {}).get("status") == "ran"
    assert (cfg.out_dir / "quarantined_retry.json").exists()
    assert "chunk0001" in caller1.chunk_ids  # B6 regenerated in run 1
    retry_bytes_before = (cfg.out_dir / "quarantined_retry.json").read_bytes()
    journal_lines_before = len(_journal(cfg.out_dir))
    # Simulate a repair failure after generation/audit completed.
    (cfg.out_dir / "repair_cache.json").unlink()
    (cfg.out_dir / "repair_report.json").unlink()
    _fail_step_in_record(cfg.out_dir, "step7")
    assert first_unfinished_stage(cfg.out_dir)[0] == "repair"
    # Run 2: repair-stage resume reruns repair/finalization only. Audit is
    # skipped, and crucially the B6 cycle must NOT regenerate the
    # quarantined chunk: zero generation calls, history untouched.
    caller2 = _SelectiveFailCaller()
    result2, _, repair2, calls = _run_with_stage_spies(
        dataclasses.replace(cfg, resume=True), monkeypatch,
        model_inner=caller2,
        qwen=_quarantine_chunk1_gate(chunk1_pids),
        qwen_audit=_repair_flagging_audit(flag_pid),
        with_repair=True, regate_passed=False)
    assert calls["audit"] == 0
    assert calls["repair_phase"] >= 1
    assert caller2.chunk_ids == []
    assert repair2.calls, "repair must re-execute, not just cache-skip"
    assert "quarantined_retry" not in result2.step7
    assert (cfg.out_dir / "quarantined_retry.json").read_bytes() == retry_bytes_before
    assert len(_journal(cfg.out_dir)) == journal_lines_before
    assert [entry["outcome"] for entry in _journal(cfg.out_dir)] == [
        "quarantined", "selected", "selected"]


# ---------------------------------------------------------------------------
# Round-4 HIGH-2: the manifest file itself must be a regular non-symlink.
# ---------------------------------------------------------------------------


def test_symlink_manifest_blocks_promotion(tmp_path: Path):
    from pact_v4.pipeline.v4_retry import (
        load_stage_manifest,
        stage_manifest_present,
    )
    cfg = _full_complete_run(tmp_path)
    manifest_path = cfg.out_dir / "stage_manifest.json"
    real = manifest_path.read_bytes()
    backup = tmp_path / "manifest_backup.json"
    backup.write_bytes(real)
    manifest_path.unlink()
    manifest_path.symlink_to(backup)
    # Perfectly valid content behind a symlink: still present-invalid --
    # the link could point outside the chapter dir, so it never loads.
    assert stage_manifest_present(cfg.out_dir) is True
    assert load_stage_manifest(cfg.out_dir) == {}
    readiness = chapter_readiness(cfg.out_dir)
    assert readiness["ready"] is False
    assert "manifest" in readiness["reason"], readiness
    manifest_path.unlink()
    manifest_path.write_bytes(real)
    assert chapter_readiness(cfg.out_dir)["ready"] is True


def test_fifo_manifest_blocks_without_hanging(tmp_path: Path):
    import os
    from pact_v4.pipeline.v4_retry import (
        load_stage_manifest,
        stage_manifest_present,
    )
    cfg = _full_complete_run(tmp_path)
    manifest_path = cfg.out_dir / "stage_manifest.json"
    real = manifest_path.read_bytes()
    manifest_path.unlink()
    os.mkfifo(manifest_path)
    try:
        # A FIFO where the manifest belongs is present-invalid; loading
        # must reject it without ever opening it for reading (which would
        # block forever with no writer).
        assert stage_manifest_present(cfg.out_dir) is True
        assert load_stage_manifest(cfg.out_dir) == {}
        readiness = chapter_readiness(cfg.out_dir)
        assert readiness["ready"] is False
        assert "manifest" in readiness["reason"], readiness
    finally:
        os.unlink(manifest_path)
        manifest_path.write_bytes(real)
    assert chapter_readiness(cfg.out_dir)["ready"] is True


def test_manifest_presence_semantics(tmp_path: Path):
    from pact_v4.pipeline.v4_retry import stage_manifest_present
    cfg = _make_cfg(tmp_path, n_paragraphs=THREE_CHUNKS)
    assert stage_manifest_present(cfg.out_dir) is False
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    (cfg.out_dir / "stage_manifest.json").write_text("{broken", encoding="utf-8")
    assert stage_manifest_present(cfg.out_dir) is True


# ---------------------------------------------------------------------------
# Round-4 MEDIUM: checkpoint inputs bind to the trial record.
# ---------------------------------------------------------------------------


def test_manifest_inputs_must_bind_record(tmp_path: Path):
    from pact_v4.pipeline.v4_retry import manifest_inputs_bind_record
    cfg = _full_complete_run(tmp_path)
    record = json.loads(
        (cfg.out_dir / "strict_chapter_trial_record.json").read_text(
            encoding="utf-8"))
    manifest = load_stage_manifest(cfg.out_dir)
    assert manifest_inputs_bind_record(manifest, record) == (True, "ok")
    # Altered snapshot input on one entry breaks the binding...
    manifest["generation"]["inputs"]["snapshot_hash"] = "0" * 64
    ok, reason = manifest_inputs_bind_record(manifest, record)
    assert not ok
    assert "generation" in reason and "snapshot_hash" in reason, reason
    # ...as does an altered backend hash and a missing record identity.
    manifest["generation"]["inputs"]["snapshot_hash"] = record["identities"][
        "snapshot_hash"]
    manifest["repair"]["inputs"]["backend_identity_hash"] = "f" * 64
    ok, reason = manifest_inputs_bind_record(manifest, record)
    assert not ok
    assert "repair" in reason and "backend" in reason, reason
    broken_record = {"identities": {"snapshot_hash": "x"}}
    ok, _reason = manifest_inputs_bind_record(manifest, broken_record)
    assert not ok


def test_unbound_manifest_falls_back_to_legacy(tmp_path: Path):
    cfg = _full_complete_run(tmp_path)
    # Same-vintage files, but the manifest claims another run's inputs:
    # stage selection must ignore it and infer from scratch-verified bytes.
    _rewrite_manifest(cfg.out_dir, lambda stages: (
        stages["generation"]["inputs"].__setitem__("snapshot_hash", "0" * 64),
    ))
    assert first_unfinished_stage(cfg.out_dir) == (
        "formatting", "all stages satisfied")
    readiness = chapter_readiness(cfg.out_dir)
    assert readiness["ready"] is False
    assert "manifest invalid" in readiness["reason"], readiness
    assert "does not match trial record" in readiness["reason"], readiness


# ---------------------------------------------------------------------------
# Round-5: present-but-not-fully-valid manifests block promotion.
# ---------------------------------------------------------------------------


def _write_crafted_chapter(out_dir: Path) -> None:
    """Minimal complete chapter dir: record binding a manifest skeleton."""
    out_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "chapter_id": "046",
        "step8": {"status": "complete"},
        "step7": {"status": "complete"},
        "step6": {"status": "complete"},
        "identities": {
            "source_hash": "src", "snapshot_hash": "snap",
            "chunk_plan_hash": "plan", "config_identity": "cfg",
        },
        "backend": {"identity_hash": "be"},
    }
    (out_dir / "strict_chapter_trial_record.json").write_text(
        json.dumps(record), encoding="utf-8")
    manifest = {
        "schema": "pact-v4-stage-manifest/v1",
        "stages": {
            stage: {
                "status": "complete", "attempt": 0,
                "inputs": {
                    "snapshot_hash": "snap", "chunk_plan_hash": "plan",
                    "config_identity": "cfg", "backend_identity_hash": "be",
                },
                "artifacts": list(sets[0]),
                "integrity": {name: "d" * 64 for name in sets[0]},
            }
            for stage, sets in {
                "generation": (("journal.ndjson", "generation_outcomes.json",
                                "selection_results.json",
                                "translations.json"),),
                "audit": (("audit_cache.json", "audit_findings.json",
                           "b2_handoff.json"),),
                "repair": (("repair_cache.json", "repair_report.json"),),
                "formatting": (("formatting_report.json",
                                "translations.json"),),
            }.items()
        },
    }
    (out_dir / "stage_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")


def test_manifest_blocked_classification(tmp_path: Path):
    from pact_v4.pipeline.v4_retry import chapter_manifest_blocked
    out_dir = tmp_path / "chapter_0001"
    # Genuinely absent: legacy inference approved, never blocked.
    out_dir.mkdir(parents=True, exist_ok=True)
    assert chapter_manifest_blocked(out_dir) == (False, "")
    # Fully valid crafted manifest (structure + binding, no file checks
    # at this layer): not blocked.
    _write_crafted_chapter(out_dir)
    assert chapter_manifest_blocked(out_dir) == (False, "")
    # Unreadable content blocks...
    (out_dir / "stage_manifest.json").write_text("{broken", encoding="utf-8")
    blocked, reason = chapter_manifest_blocked(out_dir)
    assert blocked is True
    assert "manifest" in reason, reason
    # ...as does a schema-valid manifest with a truncated artifact set,
    # even when artifact and hash removals are mutually consistent...
    _write_crafted_chapter(out_dir)
    manifest_path = out_dir / "stage_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["stages"]["audit"]["artifacts"].remove("b2_handoff.json")
    del manifest["stages"]["audit"]["integrity"]["b2_handoff.json"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    blocked, reason = chapter_manifest_blocked(out_dir)
    assert blocked is True
    assert "mismatch" in reason, reason
    # ...a manifest with unknown-only stages...
    manifest = {"schema": "pact-v4-stage-manifest/v1",
                "stages": {"exotic": {"status": "complete"}}}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    blocked, reason = chapter_manifest_blocked(out_dir)
    assert blocked is True
    assert "canonical" in reason, reason
    # ...and complete inputs that do not bind to the trial record.
    _write_crafted_chapter(out_dir)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["stages"]["repair"]["inputs"]["snapshot_hash"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    blocked, reason = chapter_manifest_blocked(out_dir)
    assert blocked is True
    assert "bind" in reason or "match trial record" in reason, reason


# ---------------------------------------------------------------------------
# Final review HIGH: accepted_degraded terminal completion counts as
# completed in checkpoint/readiness semantics (actual degraded status
# preserved, never rewritten or hidden).
# ---------------------------------------------------------------------------


def _degraded_complete_run(tmp_path: Path):
    """Full stub run ending terminal-accepted_degraded with debt.

    chunk0001 stays quarantined with unclosable repair debt (failing
    re-gate); B6 runs in this session. Returns the cfg.
    """
    cfg = _make_cfg(tmp_path, n_paragraphs=THREE_CHUNKS)
    _, _, chunk_plan, _ = _build_artifacts(cfg)
    chunk1_pids = set(chunk_plan.chunks[0].pids)
    result, _, _ = _run_chapter(
        cfg, qwen=_quarantine_chunk1_gate(chunk1_pids),
        qwen_audit=_repair_flagging_audit(sorted(chunk1_pids)[0]),
        with_repair=True, regate_passed=False)
    assert result.step8["status"] == "accepted_degraded", result.step8
    return cfg


def test_degraded_terminal_records_completed_checkpoints(tmp_path: Path):
    cfg = _degraded_complete_run(tmp_path)
    # Degraded steps record completed checkpoints (actual degraded status
    # preserved in inputs.step_status, never rewritten or hidden)...
    manifest = load_stage_manifest(cfg.out_dir)
    for stage in ("audit", "repair", "formatting"):
        assert manifest[stage]["status"] == "complete", stage
    assert manifest["repair"]["inputs"]["step_status"] == "accepted_degraded"
    assert manifest["formatting"]["inputs"]["step_status"] == "accepted_degraded"
    # ...so stage selection treats the chapter as fully satisfied...
    assert first_unfinished_stage(cfg.out_dir) == (
        "formatting", "all stages satisfied")
    # ...and readiness accepts the degraded terminal for promotion skip.
    readiness = chapter_readiness(cfg.out_dir)
    assert readiness["ready"] is True
    assert readiness["terminal_status"] == "accepted_degraded"


def test_degraded_terminal_resume_reuses_without_rerun(tmp_path, monkeypatch):
    cfg = _degraded_complete_run(tmp_path)
    journal_before = (cfg.out_dir / "journal.ndjson").read_bytes()
    translations_before = (cfg.out_dir / "translations.json").read_bytes()
    _, _, chunk_plan, _ = _build_artifacts(cfg)
    chunk1_pids = set(chunk_plan.chunks[0].pids)
    caller2 = _SelectiveFailCaller()
    result2, _, repair2, calls = _run_with_stage_spies(
        dataclasses.replace(cfg, resume=True), monkeypatch,
        model_inner=caller2,
        qwen=_quarantine_chunk1_gate(chunk1_pids),
        qwen_audit=_repair_flagging_audit(sorted(chunk1_pids)[0]),
        with_repair=True, regate_passed=False)
    # No stage invoked: completed-resume path reuses everything.
    assert calls == {"audit": 0, "repair_phase": 0}
    assert caller2.chunk_ids == []
    assert repair2.calls == []
    assert result2.step6.get("reused") is True
    # Actual degraded status preserved verbatim -- neither upgraded to
    # complete nor collapsed to failed.
    assert result2.step8["status"] == "accepted_degraded"
    assert result2.step7["status"] == "accepted_degraded"
    assert (cfg.out_dir / "journal.ndjson").read_bytes() == journal_before
    assert (cfg.out_dir / "translations.json").read_bytes() == translations_before
    assert first_unfinished_stage(cfg.out_dir) == (
        "formatting", "all stages satisfied")


# ---------------------------------------------------------------------------
# Whole-chapter B3 artifact binding (review HIGH): completed WC stages bind
# real B3 artifacts instead of empty sets, so ready/degraded WC chapters
# cannot skip with zero B3 validation.
# ---------------------------------------------------------------------------

from pact_v4.pipeline.b3_audit_repair import B3AuditRepairResult


class _FakeB3:
    """Zero-model-call B3 stand-in writing real B3 artifacts.

    Mirrors the production contract under test: on ``run()`` it persists
    the audit journal + cache (plus the entity cache when enabled) and
    returns a converged result echoing the input translation. ``degraded``
    flips only the terminal step8 status (converged stages stay complete).
    ``entity_context_prepass`` returns None (entity-disabled path).
    """

    def __init__(self, *, degraded: bool = False, entity_enabled: bool = True):
        self.calls: List[str] = []
        self.degraded = degraded
        self.entity_enabled = entity_enabled

    def entity_context_prepass(self, *, source, out_dir):
        return None

    def run(self, *, chapter_id, source, snapshot_hash, translation,
            book_memory, glossary=(), out_dir, config_identity,
            backend_identity_hash, quarantined_pids=None,
            book_memory_role_views=None):
        from pathlib import Path as _Path
        self.calls.append(chapter_id)
        out_dir = _Path(out_dir)
        (out_dir / "audit_journal.ndjson").write_text(
            json.dumps({"event": "audit_complete",
                        "chapter_id": chapter_id}) + "\n",
            encoding="utf-8")
        (out_dir / "audit_cache_b3.json").write_text(
            json.dumps({"audit_complete": True, "chapter_id": chapter_id}),
            encoding="utf-8")
        if self.entity_enabled:
            (out_dir / "entity_context_cache.json").write_text(
                json.dumps({"entities": [], "chapter_id": chapter_id}),
                encoding="utf-8")
        step6 = {"status": "complete", "audit_complete": True,
                 "entity_context_enabled": self.entity_enabled}
        step7 = {"status": "complete", "repair_complete": True}
        step8 = {"status": "accepted_degraded" if self.degraded else "complete",
                 "audit_complete": True,
                 "released_as_audited": not self.degraded}
        return B3AuditRepairResult(
            step6=step6, step7=step7, step8=step8,
            translations_repaired=dict(translation), audit_complete=True,
            from_cache=False, entity_context_hash=None,
            audit_cache_path=out_dir / "audit_cache_b3.json",
            journal_path=out_dir / "audit_journal.ndjson",
            r_editor=None)


def _run_wc_chapter(cfg, *, b3=None, model_inner=None):
    """Run the whole-chapter driver with stub backends + optional fake B3."""
    router = _make_router()
    inner = model_inner or StubModelCaller()
    result = run_chapter_strict(
        cfg, router=router,
        model_caller=_LifecycleAwareModelCaller(router, inner),
        qwen_evaluator=_LifecycleAwareQwen(router, StubQwen()),
        gemma_selector=_LifecycleAwareGemmaSelector(router, StubGemma()),
        qwen_audit_evaluator=_LifecycleAwareQwenAudit(router, StubQwenAudit()),
        gemma_audit_evaluator=_LifecycleAwareGemmaAudit(router, StubGemmaAudit()),
        b3_audit_repair=b3,
    )
    return result, inner, b3


def _wc_cfg(tmp_path: Path, *, entity_enabled: bool = True):
    cfg = _make_cfg(tmp_path, n_paragraphs=8)
    return dataclasses.replace(cfg, whole_chapter=True,
                               entity_context_enabled=entity_enabled)


def test_wc_manifest_binds_b3_artifacts(tmp_path: Path):
    cfg = _wc_cfg(tmp_path)
    result, _, b3 = _run_wc_chapter(cfg, b3=_FakeB3())
    assert b3.calls == ["046"]
    assert result.step8["status"] == "complete"
    manifest = load_stage_manifest(cfg.out_dir)
    assert sorted(manifest["audit"]["artifacts"]) == [
        "audit_cache_b3.json", "audit_journal.ndjson",
        "entity_context_cache.json"]
    assert sorted(manifest["repair"]["artifacts"]) == [
        "audit_cache_b3.json", "translations_repaired.json"]
    assert manifest["formatting"]["artifacts"] == ["translations.json"]
    assert chapter_readiness(cfg.out_dir)["ready"] is True
    assert first_unfinished_stage(cfg.out_dir) == (
        "formatting", "all stages satisfied")


def test_wc_readiness_rejects_deleted_b3_artifacts(tmp_path: Path):
    cfg = _wc_cfg(tmp_path)
    _run_wc_chapter(cfg, b3=_FakeB3())
    assert chapter_readiness(cfg.out_dir)["ready"] is True
    cases = [
        ("audit_cache_b3.json", "audit"),
        ("audit_journal.ndjson", "audit"),
        ("entity_context_cache.json", "audit"),
        ("translations_repaired.json", "repair"),
    ]
    for name, stage in cases:
        payload = (cfg.out_dir / name).read_bytes()
        (cfg.out_dir / name).unlink()
        readiness = chapter_readiness(cfg.out_dir)
        assert readiness["ready"] is False, name
        assert readiness["resume_stage"] == stage, (name, readiness)
        (cfg.out_dir / name).write_bytes(payload)
    assert chapter_readiness(cfg.out_dir)["ready"] is True


def test_wc_readiness_rejects_corrupt_b3_journal(tmp_path: Path):
    cfg = _wc_cfg(tmp_path)
    _run_wc_chapter(cfg, b3=_FakeB3())
    journal = cfg.out_dir / "audit_journal.ndjson"
    real = journal.read_bytes()
    journal.write_text("{corrupt\n", encoding="utf-8")
    readiness = chapter_readiness(cfg.out_dir)
    assert readiness["ready"] is False
    assert readiness["resume_stage"] == "audit"
    journal.write_bytes(real)
    assert chapter_readiness(cfg.out_dir)["ready"] is True


def test_wc_entity_disabled_needs_no_entity_file(tmp_path: Path):
    cfg = _wc_cfg(tmp_path, entity_enabled=False)
    result, _, _ = _run_wc_chapter(cfg, b3=_FakeB3(entity_enabled=False))
    assert result.step8["status"] == "complete"
    assert not (cfg.out_dir / "entity_context_cache.json").exists()
    manifest = load_stage_manifest(cfg.out_dir)
    assert sorted(manifest["audit"]["artifacts"]) == [
        "audit_cache_b3.json", "audit_journal.ndjson"]
    assert chapter_readiness(cfg.out_dir)["ready"] is True


def test_wc_degraded_readiness_and_manifest(tmp_path: Path):
    cfg = _wc_cfg(tmp_path)
    result, _, _ = _run_wc_chapter(cfg, b3=_FakeB3(degraded=True))
    assert result.step8["status"] == "accepted_degraded"
    assert result.step7["status"] == "complete"
    manifest = load_stage_manifest(cfg.out_dir)
    for stage in ("generation", "audit", "repair", "formatting"):
        assert manifest[stage]["status"] == "complete", stage
    readiness = chapter_readiness(cfg.out_dir)
    assert readiness["ready"] is True
    assert readiness["terminal_status"] == "accepted_degraded"
    assert first_unfinished_stage(cfg.out_dir) == (
        "formatting", "all stages satisfied")


# ---------------------------------------------------------------------------
# Path safety: secure manifest writes never follow pre-planted symlinks and
# never block on FIFOs (review HIGH).
# ---------------------------------------------------------------------------


def test_atomic_write_never_follows_legacy_symlink_tmp(tmp_path: Path):
    from pact_v4.pipeline.v4_retry import _atomic_write_json
    target = tmp_path / "stage_manifest.json"
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    # The predictable legacy temp name, pre-created as a symlink pointing
    # outside: the old writer followed it (outside write); the secure
    # writer must use an unguessable exclusive temp instead.
    (tmp_path / "stage_manifest.json.tmp").symlink_to(outside)
    _atomic_write_json(target, {"schema": "ok"})
    assert json.loads(outside.read_text(encoding="utf-8")) == {}
    assert json.loads(target.read_text(encoding="utf-8")) == {"schema": "ok"}
    assert not target.is_symlink()


def test_atomic_write_ignores_fifo_tmp_without_hanging(tmp_path: Path):
    import os
    from pact_v4.pipeline.v4_retry import _atomic_write_json
    target = tmp_path / "stage_manifest.json"
    fifo = tmp_path / "stage_manifest.json.tmp"
    os.mkfifo(fifo)
    try:
        # Pre-fix this blocked forever (opening a FIFO for writing waits
        # for a reader that never comes); the secure writer never opens
        # the planted path, so this returns promptly.
        _atomic_write_json(target, {"schema": "ok"})
    finally:
        assert not fifo.is_symlink()
        os.unlink(fifo)
    assert json.loads(target.read_text(encoding="utf-8")) == {"schema": "ok"}


def test_atomic_replace_never_follows_target_symlink(tmp_path: Path):
    from pact_v4.pipeline.v4_retry import _atomic_write_json
    target = tmp_path / "stage_manifest.json"
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    # Even a symlink AT the target path must not redirect content: rename
    # replaces the link itself, leaving the outside file untouched and a
    # regular file behind.
    target.symlink_to(outside)
    _atomic_write_json(target, {"schema": "ok"})
    assert json.loads(outside.read_text(encoding="utf-8")) == {}
    assert not target.is_symlink()
    assert json.loads(target.read_text(encoding="utf-8")) == {"schema": "ok"}


# ---------------------------------------------------------------------------
# Additive-memory lineage: a drifted repair retry stays ready afterwards.
# ---------------------------------------------------------------------------


def _drifted_repair_heal(tmp_path: Path):
    """Complete run, downstream addition, repair failure, --resume heal.

    Returns ``(cfg, prior_pair)`` where ``prior_pair`` is the
    (snapshot, plan) pair the retained journal entries keep.
    """
    from pact_v4.pipeline.v4_retry import chapter_readiness
    cfg = _make_cfg(tmp_path, n_paragraphs=THREE_CHUNKS)
    _run_chapter(cfg, with_repair=True,
                 qwen_audit=_repair_flagging_audit("p00001"))
    assert chapter_readiness(cfg.out_dir)["ready"] is True
    prior = _journal(cfg.out_dir)
    prior_pair = (prior[0]["snapshot_hash"], prior[0]["chunk_plan_hash"])
    # Downstream chapters promote observations into shared memory.
    glossary_path = cfg.memory_dir / "glossary.json"
    glossary = json.loads(glossary_path.read_text(encoding="utf-8"))
    glossary["DownstreamTerm"] = "НисходящийТермин"
    glossary_path.write_text(json.dumps(glossary, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    # Simulate a repair failure after generation/audit completed.
    (cfg.out_dir / "repair_cache.json").unlink()
    (cfg.out_dir / "repair_report.json").unlink()
    _fail_step_in_record(cfg.out_dir, "step7")
    caller = _SelectiveFailCaller()
    result, _, repair = _run_chapter(
        dataclasses.replace(cfg, resume=True), model_inner=caller,
        with_repair=True, qwen_audit=_repair_flagging_audit("p00001"))
    assert caller.chunk_ids == []
    assert repair.calls, "repair must re-execute, not just cache-skip"
    assert result.step8["status"] == "complete"
    live = json.loads(
        (cfg.out_dir / "strict_chapter_trial_record.json").read_text(
            encoding="utf-8"))["identities"]
    live_pair = (live["snapshot_hash"], live["chunk_plan_hash"])
    # The drift must be real, or this fixture proves nothing.
    assert live_pair != prior_pair
    return cfg, prior_pair


def test_drifted_repair_retry_stays_ready(tmp_path: Path):
    from pact_v4.pipeline.v4_retry import chapter_readiness
    cfg, prior_pair = _drifted_repair_heal(tmp_path)
    # The retried run records the reused lineage pair...
    record = json.loads(
        (cfg.out_dir / "strict_chapter_trial_record.json").read_text(
            encoding="utf-8"))
    assert record.get("resumed_from") == [{
        "snapshot_hash": prior_pair[0], "chunk_plan_hash": prior_pair[1]}]
    # ...so readiness stays True although the retained journal entries
    # keep the original pair while the record carries the live one.
    readiness = chapter_readiness(cfg.out_dir)
    assert readiness["ready"] is True, readiness
    assert readiness["resume_stage"] is None
    # A second strict resume stays a no-op on generation.
    caller3 = _SelectiveFailCaller()
    result3, _, _ = _run_chapter(
        dataclasses.replace(cfg, resume=True), model_inner=caller3,
        with_repair=True, qwen_audit=_repair_flagging_audit("p00001"))
    assert caller3.chunk_ids == []
    assert result3.step8["status"] == "complete"


def test_resumed_from_key_is_load_bearing(tmp_path: Path):
    from pact_v4.pipeline.v4_retry import chapter_readiness
    cfg, prior_pair = _drifted_repair_heal(tmp_path)
    record_path = cfg.out_dir / "strict_chapter_trial_record.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    # Deleting the key loses the lineage proof: not ready again.
    del record["resumed_from"]
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    assert chapter_readiness(cfg.out_dir)["ready"] is False
    # A wrong pair proves nothing either (and never crashes).
    record["resumed_from"] = [{"snapshot_hash": "0" * 64,
                               "chunk_plan_hash": "1" * 64}]
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    assert chapter_readiness(cfg.out_dir)["ready"] is False
    # Malformed values are ignored, fail-closed (not ready, no crash).
    for bad in ("oops", {}, {"snapshot_hash": "x"},
                {"snapshot_hash": 5, "chunk_plan_hash": []},
                [{"snapshot_hash": "0" * 64}]):
        record["resumed_from"] = bad
        record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        assert chapter_readiness(cfg.out_dir)["ready"] is False
    # Restoring the recorded pair restores readiness.
    record["resumed_from"] = [{"snapshot_hash": prior_pair[0],
                               "chunk_plan_hash": prior_pair[1]}]
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    assert chapter_readiness(cfg.out_dir)["ready"] is True
    # Legacy single-mapping shape stays accepted (backward compatibility
    # with records written before the list form existed).
    record["resumed_from"] = {"snapshot_hash": prior_pair[0],
                              "chunk_plan_hash": prior_pair[1]}
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    assert chapter_readiness(cfg.out_dir)["ready"] is True


def test_wc_drifted_retry_stays_ready(tmp_path: Path):
    from pact_v4.pipeline.v4_retry import chapter_readiness
    cfg = _wc_cfg(tmp_path)
    result1, _, _ = _run_wc_chapter(cfg, b3=_FakeB3())
    assert result1.step8["status"] == "complete"
    assert chapter_readiness(cfg.out_dir)["ready"] is True
    prior_pair = (
        _journal(cfg.out_dir)[0]["snapshot_hash"],
        _journal(cfg.out_dir)[0]["chunk_plan_hash"],
    )
    glossary_path = cfg.memory_dir / "glossary.json"
    glossary = json.loads(glossary_path.read_text(encoding="utf-8"))
    glossary["DownstreamTerm"] = "НисходящийТермин"
    glossary_path.write_text(json.dumps(glossary, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    result2, _, _ = _run_wc_chapter(
        dataclasses.replace(cfg, resume=True), b3=_FakeB3())
    assert result2.step8["status"] == "complete"
    record = json.loads(
        (cfg.out_dir / "strict_chapter_trial_record.json").read_text(
            encoding="utf-8"))
    live_pair = (record["identities"]["snapshot_hash"],
                 record["identities"]["chunk_plan_hash"])
    assert live_pair != prior_pair  # drift must be real, else vacuous
    assert record.get("resumed_from") == [{
        "snapshot_hash": prior_pair[0], "chunk_plan_hash": prior_pair[1]}]
    assert chapter_readiness(cfg.out_dir)["ready"] is True


def _drift_memory(memory_dir: Path, term: str) -> None:
    glossary_path = memory_dir / "glossary.json"
    glossary = json.loads(glossary_path.read_text(encoding="utf-8"))
    glossary[term] = "НисходящийТермин"
    glossary_path.write_text(json.dumps(glossary, ensure_ascii=False, indent=2),
                             encoding="utf-8")


def _record_pair(out_dir: Path):
    record = json.loads(
        (out_dir / "strict_chapter_trial_record.json").read_text(
            encoding="utf-8"))["identities"]
    return (record["snapshot_hash"], record["chunk_plan_hash"])


def test_multidrift_regen_lineage_stays_ready(tmp_path: Path):
    """Successive drift retries accumulate lineage instead of dropping it.

    R1 complete (S1) -> drift M2 -> force-regenerate everything (S2 lines
    appended; record S2 + [S1]) -> drift M3 -> resume completes with zero
    generation and records [S1, S2]. Pre-fix, the second drift omitted the
    key (two non-live pairs) and readiness flipped false.
    """
    from pact_v4.pipeline.v4_retry import chapter_readiness
    cfg = _make_cfg(tmp_path, n_paragraphs=THREE_CHUNKS)
    result1, _, _ = _run_chapter(
        cfg, with_repair=True, qwen_audit=_repair_flagging_audit("p00001"))
    assert result1.step8["status"] == "complete"
    assert chapter_readiness(cfg.out_dir)["ready"] is True
    s1 = _record_pair(cfg.out_dir)
    _drift_memory(cfg.memory_dir, "DownstreamTermOne")
    # Retry under drift that regenerates: full force-rerun under M2.
    caller2 = _SelectiveFailCaller()
    result2, _, _ = _run_chapter(
        dataclasses.replace(cfg, force_rerun=True), model_inner=caller2,
        with_repair=True, qwen_audit=_repair_flagging_audit("p00001"))
    assert caller2.chunk_ids == ["chunk0001", "chunk0002", "chunk0003"]
    assert result2.step8["status"] == "complete"
    s2 = _record_pair(cfg.out_dir)
    assert s2 != s1  # both drifts must be real, else vacuous
    assert chapter_readiness(cfg.out_dir)["ready"] is True
    # Second memory drift, then a strict resume: no generation reruns, the
    # record accumulates both retained pairs, readiness stays True.
    _drift_memory(cfg.memory_dir, "DownstreamTermTwo")
    caller3 = _SelectiveFailCaller()
    result3, _, _ = _run_chapter(
        dataclasses.replace(cfg, resume=True), model_inner=caller3,
        with_repair=True, qwen_audit=_repair_flagging_audit("p00001"))
    assert caller3.chunk_ids == []
    assert result3.step8["status"] == "complete"
    record = json.loads(
        (cfg.out_dir / "strict_chapter_trial_record.json").read_text(
            encoding="utf-8"))
    s3 = (record["identities"]["snapshot_hash"],
          record["identities"]["chunk_plan_hash"])
    assert s3 != s2 != s1
    # Exact membership (order-insensitive: the writer sorts pairs
    # deterministically, and hash order is random per run).
    assert {
        (item["snapshot_hash"], item["chunk_plan_hash"])
        for item in record.get("resumed_from")
    } == {s1, s2}
    assert record.get("resumed_from") == sorted(
        record.get("resumed_from"),
        key=lambda item: (item["snapshot_hash"], item["chunk_plan_hash"]))
    readiness = chapter_readiness(cfg.out_dir)
    assert readiness["ready"] is True, readiness
    assert readiness["resume_stage"] is None
    # Dropping either retained pair breaks readiness again (load-bearing),
    # including the superseded pair whose lines are no longer replayed.
    trimmed = [pair for pair in record["resumed_from"]
               if pair["snapshot_hash"] != s1[0]]
    record["resumed_from"] = trimmed
    (cfg.out_dir / "strict_chapter_trial_record.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    assert chapter_readiness(cfg.out_dir)["ready"] is False
    # Dropping the other pair breaks it too (both listed pairs are live
    # requirements here, not just history).
    record["resumed_from"] = [
        {"snapshot_hash": s1[0], "chunk_plan_hash": s1[1]}]
    (cfg.out_dir / "strict_chapter_trial_record.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    assert chapter_readiness(cfg.out_dir)["ready"] is False


def _journal_pairs(out_dir: Path):
    return [
        {"snapshot_hash": entry["snapshot_hash"],
         "chunk_plan_hash": entry["chunk_plan_hash"]}
        for entry in _journal(out_dir)
    ]


def test_resumed_from_list_with_junk_fails_closed(tmp_path: Path):
    from pact_v4.pipeline.v4_retry import (
        authorized_resumed_pairs,
        chapter_readiness,
    )
    cfg, prior_pair = _drifted_repair_heal(tmp_path)
    record_path = cfg.out_dir / "strict_chapter_trial_record.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    entries = _journal(cfg.out_dir)
    # Junk anywhere in the list voids the whole key (not partial
    # acceptance): readiness falls back to False, never silently reuses
    # the valid pair. Nothing crashes on malformed shapes.
    for bad_list in (
        ["oops", {"snapshot_hash": prior_pair[0],
                  "chunk_plan_hash": prior_pair[1]}],
        [42, {"snapshot_hash": prior_pair[0],
              "chunk_plan_hash": prior_pair[1]}],
        [{"snapshot_hash": "x"},
         {"snapshot_hash": prior_pair[0],
          "chunk_plan_hash": prior_pair[1]}],
        [{"snapshot_hash": prior_pair[0],
          "chunk_plan_hash": prior_pair[1],
          "attempt": 3}],
        [{"snapshot_hash": 5, "chunk_plan_hash": []},
         {"snapshot_hash": prior_pair[0],
          "chunk_plan_hash": prior_pair[1]}],
    ):
        record["resumed_from"] = bad_list
        record_path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        assert chapter_readiness(cfg.out_dir)["ready"] is False
        assert authorized_resumed_pairs(record, entries) == frozenset()
    assert authorized_resumed_pairs({}, entries) == frozenset()
    assert authorized_resumed_pairs(None, entries) == frozenset()  # type: ignore[arg-type]
    # Restoring a clean, fully evidenced list restores readiness.
    record["resumed_from"] = [{"snapshot_hash": prior_pair[0],
                               "chunk_plan_hash": prior_pair[1]}]
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    assert chapter_readiness(cfg.out_dir)["ready"] is True


def test_resumed_from_phantom_pair_fails_closed(tmp_path: Path):
    """A valid-looking pair with no journal evidence validates nothing."""
    from pact_v4.pipeline.v4_retry import (
        authorized_resumed_pairs,
        chapter_readiness,
    )
    cfg, prior_pair = _drifted_repair_heal(tmp_path)
    record_path = cfg.out_dir / "strict_chapter_trial_record.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    entries = _journal(cfg.out_dir)
    phantom = {"snapshot_hash": "a" * 64, "chunk_plan_hash": "b" * 64}
    assert all(
        (entry.get("snapshot_hash"), entry.get("chunk_plan_hash"))
        != (phantom["snapshot_hash"], phantom["chunk_plan_hash"])
        for entry in entries if isinstance(entry, dict))
    # Phantom alongside the valid pair voids the whole key...
    record["resumed_from"] = [
        {"snapshot_hash": prior_pair[0], "chunk_plan_hash": prior_pair[1]},
        phantom,
    ]
    assert authorized_resumed_pairs(record, entries) == frozenset()
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    assert chapter_readiness(cfg.out_dir)["ready"] is False
    # ...and a phantom-only list validates nothing either.
    record["resumed_from"] = [phantom]
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    assert chapter_readiness(cfg.out_dir)["ready"] is False
    assert authorized_resumed_pairs(record, entries) == frozenset()
    # Malformed items void the key in whole-chapter mode too.
    record["resumed_from"] = [
        {"snapshot_hash": prior_pair[0], "chunk_plan_hash": prior_pair[1]},
        {"snapshot_hash": prior_pair[0]},
    ]
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    assert chapter_readiness(cfg.out_dir)["ready"] is False


def test_wc_phantom_pair_fails_closed(tmp_path: Path):
    """Whole-chapter mode: unevidenced listed pairs void the whole key."""
    from pact_v4.pipeline.v4_retry import (
        authorized_resumed_pairs,
        chapter_readiness,
    )
    cfg = _wc_cfg(tmp_path)
    result1, _, _ = _run_wc_chapter(cfg, b3=_FakeB3())
    assert result1.step8["status"] == "complete"
    prior_pair = (
        _journal(cfg.out_dir)[0]["snapshot_hash"],
        _journal(cfg.out_dir)[0]["chunk_plan_hash"],
    )
    _drift_memory(cfg.memory_dir, "DownstreamTermOne")
    result2, _, _ = _run_wc_chapter(
        dataclasses.replace(cfg, resume=True), b3=_FakeB3())
    assert result2.step8["status"] == "complete"
    record_path = cfg.out_dir / "strict_chapter_trial_record.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record.get("resumed_from") == [{
        "snapshot_hash": prior_pair[0], "chunk_plan_hash": prior_pair[1]}]
    assert chapter_readiness(cfg.out_dir)["ready"] is True
    entries = _journal(cfg.out_dir)
    phantom = {"snapshot_hash": "c" * 64, "chunk_plan_hash": "d" * 64}
    # Mixed valid + phantom, or phantom alone: nothing validates, no crash.
    for bad_list in (
        [{"snapshot_hash": prior_pair[0], "chunk_plan_hash": prior_pair[1]},
         phantom],
        [phantom],
    ):
        record["resumed_from"] = bad_list
        assert authorized_resumed_pairs(record, entries) == frozenset()
        record_path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        assert chapter_readiness(cfg.out_dir)["ready"] is False
