"""V4.1 A1 whole-chapter mode tests for the strict runner.

Covers the whole-chapter runner contract (docs/plans/V4_1_WHOLE_CHAPTER_
ARCHITECTURE_PLAN_RU.md §8 A1): one generation call per chapter against the
full ordered PID map; always-written selection_results.json with schema
pact-v4-whole-chapter-selection/v1; translations_raw.json raw snapshot;
Steps 6/7/8 recorded as skipped; resume distinguishes the raw generator
snapshot from the final translations.json alias.
"""
from __future__ import annotations

import json

import pytest

from pact_v4.phase1.models import WholeChapterPidMap
from pact_v4.pipeline.v4_phase12_strict_runner import (
    WHOLE_CHAPTER_CHUNK_ID,
    WHOLE_CHAPTER_SELECTION_SCHEMA,
    run_chapter_strict,
)
from pact_v4.runtime.model_lifecycle import ModelRouter
from tests.pact_v4.pipeline.test_v4_phase12_strict_runner import (
    FakeLifecycleAdapter,
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
    _make_backend,
    _make_cfg,
)


def _make_router() -> ModelRouter:
    return ModelRouter(
        FakeLifecycleAdapter(),
        role_profile_names={"gemma": "Gemma", "qwen": "Qwen"},
        role_args={"gemma": [], "qwen": []},
    )


def _run_whole_chapter(cfg, *, model_caller=None):
    router = _make_router()
    model_caller = _LifecycleAwareModelCaller(
        router, model_caller or StubModelCaller()
    )
    result = run_chapter_strict(
        cfg, router=router, model_caller=model_caller,
        qwen_evaluator=_LifecycleAwareQwen(router, StubQwen()),
        gemma_selector=_LifecycleAwareGemmaSelector(router, StubGemma()),
        qwen_audit_evaluator=_LifecycleAwareQwenAudit(router, StubQwenAudit()),
        gemma_audit_evaluator=_LifecycleAwareGemmaAudit(router, StubGemmaAudit()),
    )
    return result


def test_whole_chapter_mode_generates_one_call_full_pid_map(tmp_path):
    cfg = _make_cfg(tmp_path, n_paragraphs=24)
    # V4.1 A1: whole_chapter flag is part of the config identity.
    cfg = type(cfg)(
        chapter_id=cfg.chapter_id, chapter_html_path=cfg.chapter_html_path,
        memory_dir=cfg.memory_dir, out_dir=cfg.out_dir, backend=cfg.backend,
        whole_chapter=True,
    )
    caller = StubModelCaller()
    result = _run_whole_chapter(cfg, model_caller=caller)

    # Exactly ONE generation call, against the full chapter as a single unit.
    assert len(caller.calls) == 1
    bundle = caller.calls[0]
    assert bundle.chunk_id == WHOLE_CHAPTER_CHUNK_ID
    source_pids = tuple(pid for pid, _ in bundle.owned_source)
    assert bundle.owned_pids == source_pids

    # The full PID map: all PIDs of the chapter, in source order.
    from tests.pact_v4.pipeline.test_v4_phase12_strict_runner import _build_artifacts
    _, snapshot, chunk_plan, _ = _build_artifacts(cfg)
    pid_map = WholeChapterPidMap.derive(chunk_plan, snapshot)
    assert bundle.owned_pids == pid_map.pids == snapshot.pids

    # Translations: complete PID map in the raw snapshot AND the final alias.
    translations = json.loads(result.translations_path.read_text(encoding="utf-8"))
    assert set(translations) == set(pid_map.pids)
    raw = json.loads((cfg.out_dir / "translations_raw.json").read_text(encoding="utf-8"))
    assert raw == translations
    assert len(raw) == len(pid_map.pids)

    # selection_results.json: the v1 not_applicable schema, always written.
    sel = json.loads((cfg.out_dir / "selection_results.json").read_text(encoding="utf-8"))
    assert sel["schema"] == WHOLE_CHAPTER_SELECTION_SCHEMA
    assert sel["mode"] == "not_applicable"
    assert sel["candidate_count"] == 1
    assert sel["selection_performed"] is False
    assert sel["coverage"] == "full_pid_map"
    assert sel["generation_record_id"].startswith("whole_chapter:balanced_literary:")
    assert sel["chapter_id"] == cfg.chapter_id
    assert sel["config_identity"] == result.record["identities"]["config_identity"]

    # Steps 6/7/8 are out of A1 scope and recorded as skipped.
    assert result.step6 == {"status": "skipped", "reason": "whole_chapter_generation_only"}
    assert result.step7 == {"status": "skipped", "reason": "whole_chapter_generation_only"}
    assert result.step8 == {"status": "skipped", "reason": "whole_chapter_generation_only"}
    assert result.halted_early is False

    # Journal: exactly one whole_chapter entry.
    journal = [
        json.loads(line)
        for line in (cfg.out_dir / "journal.ndjson").read_text(encoding="utf-8").splitlines()
    ]
    assert len(journal) == 1
    assert journal[0]["chunk_id"] == WHOLE_CHAPTER_CHUNK_ID
    assert journal[0]["outcome"] == "selected"

    # generation_outcomes.json: one whole-chapter record with the candidate id.
    outcomes = json.loads((cfg.out_dir / "generation_outcomes.json").read_text(encoding="utf-8"))
    assert len(outcomes["outcomes"]) == 1
    assert outcomes["outcomes"][0]["chunk_id"] == WHOLE_CHAPTER_CHUNK_ID
    cand = outcomes["outcomes"][0]["candidates"]["balanced_literary"]
    assert cand["candidate_id"] == sel["generation_record_id"]

    # max_output_tokens=32768 is in the run identity (Gate 0 §8.5).
    artifact = cfg.to_config_artifact(model_profile="test")
    assert artifact.values["generation"]["max_tokens"] == 32768


def test_whole_chapter_resume_reads_raw_snapshot_not_final(tmp_path):
    cfg = _make_cfg(tmp_path, n_paragraphs=24)
    cfg = type(cfg)(
        chapter_id=cfg.chapter_id, chapter_html_path=cfg.chapter_html_path,
        memory_dir=cfg.memory_dir, out_dir=cfg.out_dir, backend=cfg.backend,
        whole_chapter=True,
    )
    caller = StubModelCaller()
    first = _run_whole_chapter(cfg, model_caller=caller)
    assert first.processed_count == 1
    assert len(caller.calls) == 1

    # Tamper the FINAL alias so the raw-vs-final distinction is observable:
    # resume must reconstruct from translations_raw.json (the raw generator
    # snapshot), NOT from the tampered final translations.json.
    raw = json.loads((cfg.out_dir / "translations_raw.json").read_text(encoding="utf-8"))
    (cfg.out_dir / "translations.json").write_text(
        json.dumps({pid: "TAMPERED" for pid in raw}, ensure_ascii=False),
        encoding="utf-8",
    )

    caller2 = StubModelCaller()
    resumed = _run_whole_chapter(cfg, model_caller=caller2)
    # Resume replays the journal: no second generation call, no new journal
    # entry beyond the single whole-chapter one.
    assert len(caller2.calls) == 0
    assert resumed.resumed_from_index == 1
    journal = [
        json.loads(line)
        for line in (cfg.out_dir / "journal.ndjson").read_text(encoding="utf-8").splitlines()
    ]
    assert len(journal) == 1
    # The final alias is rewritten from the RAW snapshot, not the tampered text.
    final = json.loads(resumed.translations_path.read_text(encoding="utf-8"))
    assert final == raw
    assert all(v != "TAMPERED" for v in final.values())


def test_whole_chapter_generation_failure_is_honest_incomplete(tmp_path):
    cfg = _make_cfg(tmp_path, n_paragraphs=24)
    cfg = type(cfg)(
        chapter_id=cfg.chapter_id, chapter_html_path=cfg.chapter_html_path,
        memory_dir=cfg.memory_dir, out_dir=cfg.out_dir, backend=cfg.backend,
        whole_chapter=True,
    )

    class _BrokenCaller:
        """Always returns malformed JSON: bounded retry exhausts honestly."""

        def __init__(self):
            self.calls = []

        def __call__(self, bundle):
            self.calls.append(bundle)
            return "{not json"

    caller = _BrokenCaller()
    result = _run_whole_chapter(cfg, model_caller=caller)

    # Honest failure: no partial success, no fabricated translation.
    assert result.incomplete_generation_count == 1
    assert result.selected_count == 0
    assert result.halted_early is True
    assert "whole_chapter generation incomplete" in (result.halt_reason or "")
    assert len(caller.calls) == 3  # bounded retry budget
    assert not (cfg.out_dir / "translations_raw.json").exists()

    # selection_results.json is STILL always written, with the v1 schema and
    # no fabricated candidate id.
    sel = json.loads((cfg.out_dir / "selection_results.json").read_text(encoding="utf-8"))
    assert sel["schema"] == WHOLE_CHAPTER_SELECTION_SCHEMA
    assert sel["candidate_count"] == 0
    assert sel["generation_record_id"] is None

    # Journal records the honest incomplete outcome.
    journal = [
        json.loads(line)
        for line in (cfg.out_dir / "journal.ndjson").read_text(encoding="utf-8").splitlines()
    ]
    assert journal[0]["outcome"] == "incomplete_generation"


def test_whole_chapter_resume_fails_loudly_when_raw_snapshot_missing(tmp_path):
    # Data-loss guard: a journal saying "selected" with a missing raw snapshot
    # must fail loudly (never silently resume to an empty chapter).
    cfg = _make_cfg(tmp_path, n_paragraphs=24)
    cfg = type(cfg)(
        chapter_id=cfg.chapter_id, chapter_html_path=cfg.chapter_html_path,
        memory_dir=cfg.memory_dir, out_dir=cfg.out_dir, backend=cfg.backend,
        whole_chapter=True,
    )
    _run_whole_chapter(cfg)
    assert (cfg.out_dir / "translations_raw.json").exists()
    (cfg.out_dir / "translations_raw.json").unlink()
    with pytest.raises(ValueError, match="Data loss"):
        _run_whole_chapter(cfg)


def test_whole_chapter_config_identity_rejects_chunked_resume(tmp_path):
    # A chunked run's journal/config must not be resumable by a whole-chapter
    # run and vice versa: whole_chapter is part of the config identity.
    cfg = _make_cfg(tmp_path, n_paragraphs=24)
    whole = type(cfg)(
        chapter_id=cfg.chapter_id, chapter_html_path=cfg.chapter_html_path,
        memory_dir=cfg.memory_dir, out_dir=cfg.out_dir, backend=cfg.backend,
        whole_chapter=True,
    )
    chunked = type(cfg)(
        chapter_id=cfg.chapter_id, chapter_html_path=cfg.chapter_html_path,
        memory_dir=cfg.memory_dir, out_dir=cfg.out_dir, backend=cfg.backend,
        whole_chapter=False,
    )
    # Same inputs, different whole_chapter flag -> different config identity.
    from pact_v4.runtime.snapshot_factory import build_config_artifact
    a = whole.to_config_artifact(model_profile="test")
    b = chunked.to_config_artifact(model_profile="test")
    assert a.config_identity != b.config_identity
    assert a.values["whole_chapter"] is True
    assert b.values["whole_chapter"] is False


def test_whole_chapter_max_tokens_in_identity_and_cli_default(tmp_path):
    # StrictRunConfig default max_tokens is 32768 (A1 owner decision).
    cfg = _make_cfg(tmp_path, n_paragraphs=24)
    assert cfg.max_tokens == 32768
    artifact = cfg.to_config_artifact(model_profile="test")
    assert artifact.values["generation"]["max_tokens"] == 32768
