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
    build_strict_lifecycle,
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


def test_whole_chapter_run_emits_wc_progress_events(tmp_path):
    # V4.1 M (monitor card): the whole-chapter run writes the wc_* generation
    # events into phase_progress.ndjson — the monitor renders "GEN attempt
    # N/M (reason)" live and the final PID validation from them.
    cfg = _make_cfg(tmp_path, n_paragraphs=24)
    cfg = type(cfg)(
        chapter_id=cfg.chapter_id, chapter_html_path=cfg.chapter_html_path,
        memory_dir=cfg.memory_dir, out_dir=cfg.out_dir, backend=cfg.backend,
        whole_chapter=True,
    )
    caller = StubModelCaller()
    _run_whole_chapter(cfg, model_caller=caller)

    events = [
        json.loads(line)
        for line in (cfg.out_dir / "phase_progress.ndjson").read_text(encoding="utf-8").splitlines()
    ]
    names = [e["event"] for e in events]
    assert "wc_generation_started" in names
    assert "wc_generation_done" in names
    assert "wc_validated" in names

    started = next(e for e in events if e["event"] == "wc_generation_started")
    assert started["pid_count"] == 24
    assert started["max_attempts"] == 3
    assert started["model"]  # never empty
    done = next(e for e in events if e["event"] == "wc_generation_done")
    assert done["finish_reason"] == "complete"
    assert done["pid_count"] == 24
    validated = next(e for e in events if e["event"] == "wc_validated")
    assert validated == {"schema": "pact-v4-phase-progress/ndjson/v1",
                         "event": "wc_validated", "json_ok": True,
                         "pids_ok": True, "order_ok": True,
                         "ts": validated["ts"]}
    # No retry events on a clean single-shot generation.
    assert "wc_retry_attempt" not in names


def test_whole_chapter_failed_generation_emits_retry_events(tmp_path):
    # V4.1 M: a malformed-forever caller drives wc_retry_attempt per attempt
    # with the "malformed" reason (the monitor's live "GEN attempt N/M
    # (reason)"), and the final wc_validated honestly reports json_ok=False.
    cfg = _make_cfg(tmp_path, n_paragraphs=24)
    cfg = type(cfg)(
        chapter_id=cfg.chapter_id, chapter_html_path=cfg.chapter_html_path,
        memory_dir=cfg.memory_dir, out_dir=cfg.out_dir, backend=cfg.backend,
        whole_chapter=True,
    )

    class _BrokenCaller:
        def __init__(self):
            self.calls = []

        def __call__(self, bundle):
            self.calls.append(bundle)
            return "{not json"

    result = _run_whole_chapter(cfg, model_caller=_BrokenCaller())
    assert result.incomplete_generation_count == 1

    events = [
        json.loads(line)
        for line in (cfg.out_dir / "phase_progress.ndjson").read_text(encoding="utf-8").splitlines()
    ]
    retries = [e for e in events if e["event"] == "wc_retry_attempt"]
    assert [(e["attempt"], e["reason"]) for e in retries] == [
        (1, "malformed"), (2, "malformed"), (3, "malformed"),
    ]
    validated = next(e for e in events if e["event"] == "wc_validated")
    assert validated["json_ok"] is False
    assert validated["pids_ok"] is False
    done = next(e for e in events if e["event"] == "wc_generation_done")
    assert done["finish_reason"] == "incomplete"


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


@pytest.mark.parametrize(
    "corrupt",
    [
        "missing",
        "extra",
        "reordered",
        "duplicate",
        "non_string",
        "invalid_json",
    ],
)
def test_whole_chapter_resume_rejects_corrupt_raw_snapshot(tmp_path, corrupt):
    # A1 raw-vs-final resume safety: a selected journal entry may resume ONLY
    # from a raw snapshot that conforms to the strict full-PID {pid: text}
    # contract (exact PID set, exact source order, string values). Any
    # corruption — missing/extra/reordered/duplicate PID, non-string value,
    # invalid JSON — fails closed with a loud Data-loss ValueError, no
    # generation is attempted, and the final translations.json alias is never
    # rewritten from a partial raw snapshot.
    cfg = _make_cfg(tmp_path, n_paragraphs=24)
    cfg = type(cfg)(
        chapter_id=cfg.chapter_id, chapter_html_path=cfg.chapter_html_path,
        memory_dir=cfg.memory_dir, out_dir=cfg.out_dir, backend=cfg.backend,
        whole_chapter=True,
    )
    _run_whole_chapter(cfg)
    raw_path = cfg.out_dir / "translations_raw.json"
    good = json.loads(raw_path.read_text(encoding="utf-8"))
    assert len(good) == 24

    pids = list(good)
    if corrupt == "missing":
        payload = dict(good)
        payload.pop(pids[0])
        raw = json.dumps(payload, ensure_ascii=False)
    elif corrupt == "extra":
        payload = dict(good)
        payload["p_extra"] = "Лишний"
        raw = json.dumps(payload, ensure_ascii=False)
    elif corrupt == "reordered":
        # Re-insert the first PID at the end so the key order differs from
        # source order.
        payload = dict(good)
        payload[pids[0]] = payload.pop(pids[0])
        raw = json.dumps(payload, ensure_ascii=False)
    elif corrupt == "duplicate":
        # Literal duplicate key in the raw JSON text — plain json.loads
        # would collapse it to last-write-wins before validation can see it.
        raw = (
            '{"' + pids[0] + '": "Первый", '
            '"' + pids[0] + '": "Второй", '
            + ",".join(
                f'"{pid}": {json.dumps(good[pid], ensure_ascii=False)}'
                for pid in pids[1:]
            )
            + "}"
        )
    elif corrupt == "non_string":
        payload = dict(good)
        payload[pids[0]] = 42
        raw = json.dumps(payload, ensure_ascii=False)
    else:  # invalid_json
        raw = "{not json"

    raw_path.write_text(raw, encoding="utf-8")
    final_before = (cfg.out_dir / "translations.json").read_text(encoding="utf-8")

    caller = StubModelCaller()
    with pytest.raises(ValueError, match="Data loss"):
        _run_whole_chapter(cfg, model_caller=caller)
    # Fail closed: no generation was attempted and the final alias was never
    # rewritten from the partial/corrupt raw snapshot.
    assert len(caller.calls) == 0
    assert (cfg.out_dir / "translations.json").read_text(encoding="utf-8") == final_before
    # The raw snapshot itself is not silently repaired either.
    assert raw_path.read_text(encoding="utf-8") == raw


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


# ---------------------------------------------------------------------------
# A1 provenance fail-closed resume validation (RV2 t_c63205de findings).
#
# A selected whole-chapter journal entry may resume ONLY when the generation
# provenance exists, matches this run's identities, holds exactly one valid
# whole_chapter record, and that record links to the journal's selected
# candidate/role. Any violation raises a Data loss/provenance ValueError and
# never rewrites translations/raw/selection/provenance artifacts. The resume
# journal itself must contain exactly one well-formed whole_chapter entry.
# ---------------------------------------------------------------------------


def _whole_chapter_cfg(tmp_path):
    cfg = _make_cfg(tmp_path, n_paragraphs=24)
    return type(cfg)(
        chapter_id=cfg.chapter_id, chapter_html_path=cfg.chapter_html_path,
        memory_dir=cfg.memory_dir, out_dir=cfg.out_dir, backend=cfg.backend,
        whole_chapter=True,
    )


def _whole_chapter_journal(cfg):
    return [
        json.loads(line)
        for line in (cfg.out_dir / "journal.ndjson").read_text(encoding="utf-8").splitlines()
    ]


def test_whole_chapter_resume_preserves_provenance_linkage(tmp_path):
    # A clean whole-chapter resume must preserve the exactly-one journal
    # contract AND the candidate audit trail: generation_outcomes.json keeps
    # its single whole_chapter record, selection_results.json still links
    # candidate_count=1 to the journal's selected candidate, no new generation
    # call fires and no extra journal entry is appended.
    cfg = _whole_chapter_cfg(tmp_path)
    first = _run_whole_chapter(cfg)
    assert first.selected_count == 1
    assert first.resumed_from_index == 0

    journal = _whole_chapter_journal(cfg)
    assert len(journal) == 1
    assert journal[0]["outcome"] == "selected"
    assert journal[0]["selected_role"] == "balanced_literary"
    assert journal[0]["selected_candidate_id"].startswith("whole_chapter:balanced_literary:")
    assert journal[0]["candidate_ids"] == [journal[0]["selected_candidate_id"]]

    outcomes = json.loads((cfg.out_dir / "generation_outcomes.json").read_text(encoding="utf-8"))
    assert len(outcomes["outcomes"]) == 1
    rec_candidate = outcomes["outcomes"][0]["candidates"]["balanced_literary"]["candidate_id"]
    assert rec_candidate == journal[0]["selected_candidate_id"]

    caller = StubModelCaller()
    resumed = _run_whole_chapter(cfg, model_caller=caller)
    assert len(caller.calls) == 0
    assert resumed.selected_count == 1
    assert resumed.resumed_from_index == 1
    assert resumed.processed_count == 1

    # One whole_chapter generation record survives the resume untouched...
    outcomes_after = json.loads(
        (cfg.out_dir / "generation_outcomes.json").read_text(encoding="utf-8")
    )
    assert len(outcomes_after["outcomes"]) == 1
    assert outcomes_after["outcomes"] == outcomes["outcomes"]
    # ...and selection_results.json still links candidate_count=1 to the
    # journal's selected candidate (never an empty candidate_count=0 rewrite).
    sel = json.loads((cfg.out_dir / "selection_results.json").read_text(encoding="utf-8"))
    assert sel["schema"] == WHOLE_CHAPTER_SELECTION_SCHEMA
    assert sel["candidate_count"] == 1
    assert sel["generation_record_id"] == journal[0]["selected_candidate_id"]
    # The journal is not extended by the resume.
    assert len(_whole_chapter_journal(cfg)) == 1


def test_whole_chapter_resume_fails_when_generation_outcomes_missing(tmp_path):
    # RV2 Finding 1: a selected journal entry with NO generation_outcomes.json
    # must fail closed (Data loss), never silently rewrite empty provenance
    # and selection_results.json with candidate_count=0 while reporting
    # selected_count=1. No generation call, no artifact rewrite.
    cfg = _whole_chapter_cfg(tmp_path)
    _run_whole_chapter(cfg)
    gen_path = cfg.out_dir / "generation_outcomes.json"
    assert gen_path.exists()
    gen_path.unlink()

    final_before = (cfg.out_dir / "translations.json").read_text(encoding="utf-8")
    raw_before = (cfg.out_dir / "translations_raw.json").read_text(encoding="utf-8")
    sel_before = (cfg.out_dir / "selection_results.json").read_text(encoding="utf-8")
    journal_before = (cfg.out_dir / "journal.ndjson").read_text(encoding="utf-8")

    caller = StubModelCaller()
    with pytest.raises(ValueError, match="Data loss.*generation_outcomes.json.*missing"):
        _run_whole_chapter(cfg, model_caller=caller)
    assert len(caller.calls) == 0
    # Fail closed: nothing is rewritten or recreated.
    assert (cfg.out_dir / "translations.json").read_text(encoding="utf-8") == final_before
    assert (cfg.out_dir / "translations_raw.json").read_text(encoding="utf-8") == raw_before
    assert (cfg.out_dir / "selection_results.json").read_text(encoding="utf-8") == sel_before
    assert (cfg.out_dir / "journal.ndjson").read_text(encoding="utf-8") == journal_before
    assert not gen_path.exists()


@pytest.mark.parametrize(
    "corrupt,expect",
    [
        ("empty_object", "Data loss"),
        ("empty_outcomes", "Data loss"),
        ("no_whole_record", "Data loss"),
        ("mismatched_identity", "Foreign identity"),
        ("duplicate_record", "Data loss"),
        ("invalid_json", "Data loss"),
        ("not_object", "Data loss"),
    ],
)
def test_whole_chapter_resume_rejects_empty_or_mismatched_generation_outcomes(
    tmp_path, corrupt, expect
):
    # RV2 Finding 1: generation_outcomes.json that is empty, lacks a
    # whole_chapter record, carries foreign identities, holds duplicate
    # records, or is unparseable must fail closed with a Data loss /
    # provenance ValueError — never silently rewritten as empty provenance.
    cfg = _whole_chapter_cfg(tmp_path)
    _run_whole_chapter(cfg)
    gen_path = cfg.out_dir / "generation_outcomes.json"
    good = json.loads(gen_path.read_text(encoding="utf-8"))

    if corrupt == "empty_object":
        payload = {}
    elif corrupt == "empty_outcomes":
        payload = {
            k: good[k]
            for k in ("chapter_id", "snapshot_hash", "chunk_plan_hash", "config_identity")
        }
        payload["outcomes"] = []
    elif corrupt == "no_whole_record":
        payload = dict(good)
        payload["outcomes"] = [dict(good["outcomes"][0], chunk_id="chunk_0")]
    elif corrupt == "mismatched_identity":
        payload = dict(good)
        payload["snapshot_hash"] = "deadbeef" * 4
    elif corrupt == "duplicate_record":
        payload = dict(good)
        payload["outcomes"] = [good["outcomes"][0], good["outcomes"][0]]
    elif corrupt == "invalid_json":
        payload = None  # written raw below
        gen_path.write_text("{not json", encoding="utf-8")
    else:  # not_object
        payload = None
        gen_path.write_text("[1, 2, 3]", encoding="utf-8")
    if payload is not None:
        gen_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    final_before = (cfg.out_dir / "translations.json").read_text(encoding="utf-8")
    raw_before = (cfg.out_dir / "translations_raw.json").read_text(encoding="utf-8")
    sel_before = (cfg.out_dir / "selection_results.json").read_text(encoding="utf-8")
    corrupt_artifact_before = gen_path.read_text(encoding="utf-8")

    caller = StubModelCaller()
    with pytest.raises(ValueError, match=expect):
        _run_whole_chapter(cfg, model_caller=caller)
    assert len(caller.calls) == 0
    # Fail closed: final alias, raw snapshot and selection results are never
    # rewritten, and the corrupt provenance artifact is not silently repaired.
    assert (cfg.out_dir / "translations.json").read_text(encoding="utf-8") == final_before
    assert (cfg.out_dir / "translations_raw.json").read_text(encoding="utf-8") == raw_before
    assert (cfg.out_dir / "selection_results.json").read_text(encoding="utf-8") == sel_before
    assert gen_path.read_text(encoding="utf-8") == corrupt_artifact_before


def test_whole_chapter_resume_rejects_duplicate_journal_entry(tmp_path):
    # RV2 Finding 2: two whole_chapter journal entries break the documented
    # exactly-one contract. Resume must fail closed (never use the first
    # entry, never report resumed_from_index=2/processed_count=2 success),
    # without appending anything to the journal or rewriting artifacts.
    cfg = _whole_chapter_cfg(tmp_path)
    _run_whole_chapter(cfg)
    journal_path = cfg.out_dir / "journal.ndjson"
    lines = journal_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    duplicated = "\n".join(lines + lines) + "\n"
    journal_path.write_text(duplicated, encoding="utf-8")

    final_before = (cfg.out_dir / "translations.json").read_text(encoding="utf-8")
    sel_before = (cfg.out_dir / "selection_results.json").read_text(encoding="utf-8")

    caller = StubModelCaller()
    with pytest.raises(ValueError, match="exactly one entry, found 2"):
        _run_whole_chapter(cfg, model_caller=caller)
    assert len(caller.calls) == 0
    assert journal_path.read_text(encoding="utf-8") == duplicated
    assert (cfg.out_dir / "translations.json").read_text(encoding="utf-8") == final_before
    assert (cfg.out_dir / "selection_results.json").read_text(encoding="utf-8") == sel_before


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_chunk_id",
        "invalid_outcome",
        "selected_without_candidate",
        "selected_candidate_ids_mismatch",
        "incomplete_with_candidates",
        "non_object_entry",
    ],
)
def test_whole_chapter_resume_rejects_malformed_journal_shape(tmp_path, mutation):
    # RV2 Finding 2: a single journal entry with the wrong chunk_id, an
    # invalid outcome, missing/inconsistent selected fields, or a non-object
    # line is a malformed journal and must fail closed with a Data-loss
    # ValueError — no generation call, no artifact rewrite.
    cfg = _whole_chapter_cfg(tmp_path)
    _run_whole_chapter(cfg)
    journal_path = cfg.out_dir / "journal.ndjson"
    entry = json.loads(journal_path.read_text(encoding="utf-8").strip())

    if mutation == "wrong_chunk_id":
        entry["chunk_id"] = "chunk_0"
    elif mutation == "invalid_outcome":
        entry["outcome"] = "quarantined"
    elif mutation == "selected_without_candidate":
        entry.pop("selected_candidate_id")
    elif mutation == "selected_candidate_ids_mismatch":
        entry["candidate_ids"] = ["whole_chapter:balanced_literary:other"]
    elif mutation == "incomplete_with_candidates":
        entry["outcome"] = "incomplete_generation"
        entry["candidate_ids"] = ["whole_chapter:balanced_literary:other"]
        entry["selected_candidate_id"] = None
        entry["selected_role"] = None

    if mutation == "non_object_entry":
        journal_path.write_text("[1, 2, 3]\n", encoding="utf-8")
    else:
        journal_path.write_text(json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8")

    final_before = (cfg.out_dir / "translations.json").read_text(encoding="utf-8")
    sel_before = (cfg.out_dir / "selection_results.json").read_text(encoding="utf-8")

    caller = StubModelCaller()
    with pytest.raises(ValueError, match="Data loss"):
        _run_whole_chapter(cfg, model_caller=caller)
    assert len(caller.calls) == 0
    assert (cfg.out_dir / "translations.json").read_text(encoding="utf-8") == final_before
    assert (cfg.out_dir / "selection_results.json").read_text(encoding="utf-8") == sel_before


def test_whole_chapter_resume_incomplete_replays_honestly(tmp_path):
    # An incomplete_generation journal entry resumes honestly (halted_early,
    # selected_count=0, no fabricated candidate) when its generation record
    # exists — the provenance contract still holds.
    cfg = _whole_chapter_cfg(tmp_path)

    class _BrokenCaller:
        def __init__(self):
            self.calls = []

        def __call__(self, bundle):
            self.calls.append(bundle)
            return "{not json"

    first = _run_whole_chapter(cfg, model_caller=_BrokenCaller())
    assert first.incomplete_generation_count == 1
    journal = _whole_chapter_journal(cfg)
    assert journal[0]["outcome"] == "incomplete_generation"
    assert journal[0]["selected_candidate_id"] is None
    assert journal[0]["candidate_ids"] == []

    caller = StubModelCaller()
    resumed = _run_whole_chapter(cfg, model_caller=caller)
    assert len(caller.calls) == 0
    assert resumed.incomplete_generation_count == 1
    assert resumed.selected_count == 0
    assert resumed.halted_early is True
    assert len(_whole_chapter_journal(cfg)) == 1


def test_whole_chapter_resume_incomplete_requires_generation_outcomes(tmp_path):
    # The generation provenance is mandatory for ANY whole-chapter resume:
    # an incomplete journal entry with a deleted generation_outcomes.json must
    # fail closed instead of silently rewriting empty provenance.
    cfg = _whole_chapter_cfg(tmp_path)

    class _BrokenCaller:
        def __init__(self):
            self.calls = []

        def __call__(self, bundle):
            self.calls.append(bundle)
            return "{not json"

    first = _run_whole_chapter(cfg, model_caller=_BrokenCaller())
    assert first.incomplete_generation_count == 1
    gen_path = cfg.out_dir / "generation_outcomes.json"
    assert gen_path.exists()
    gen_path.unlink()

    caller = StubModelCaller()
    with pytest.raises(ValueError, match="Data loss.*generation_outcomes.json.*missing"):
        _run_whole_chapter(cfg, model_caller=caller)
    assert len(caller.calls) == 0
    assert not gen_path.exists()


# ---------------------------------------------------------------------------
# RV3 (t_27de970d) malformed-generation-record regression coverage.
#
# A sole whole_chapter record with a LINKED candidate_id is only a valid
# resume provenance when the record itself conforms to the writer's
# serialized GenerationOutcome schema: required record fields/types/status,
# expected_roles, well-formed candidates/errors, and a selected candidate
# carrying the fields provenance needs (translation/role/candidate_id/
# decision_trace) plus raw/provenance consistency with the raw snapshot.
# Every mutation below keeps the journal linkage intact, so the old weak
# check (candidates[selected_role].candidate_id matches) alone would have
# accepted it and rewritten selection/provenance. Each case must fail closed
# with a Data loss ValueError, zero model calls, and byte-for-byte unchanged
# artifacts (final alias, raw snapshot, selection results, generation
# provenance, journal) with no new record.
# ---------------------------------------------------------------------------


def _snapshot_artifacts(cfg):
    names = (
        "translations.json",
        "translations_raw.json",
        "selection_results.json",
        "generation_outcomes.json",
        "journal.ndjson",
        "strict_chapter_trial_record.json",
    )
    snap = {}
    for name in names:
        path = cfg.out_dir / name
        snap[name] = None if not path.exists() else path.read_text(encoding="utf-8")
    return snap


def _assert_unchanged(cfg, before):
    for name, content in before.items():
        path = cfg.out_dir / name
        if content is None:
            assert not path.exists(), f"{name} was created by a rejected resume"
        else:
            assert path.read_text(encoding="utf-8") == content, (
                f"{name} was rewritten by a rejected resume"
            )
    # The journal still holds exactly the one whole_chapter entry: no new
    # record was appended by the failed resume.
    assert len(_whole_chapter_journal(cfg)) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "remove_record_status",
        "remove_record_expected_roles",
        "remove_record_risk_band",
        "remove_record_candidates",
        "remove_record_errors",
        "invalid_record_status",
        "record_status_incomplete_on_selected",
        "empty_expected_roles",
        "empty_candidates",
        "candidate_not_object",
        "remove_candidate_translation",
        "remove_candidate_role",
        "remove_candidate_candidate_id",
        "remove_candidate_decision_trace",
        "candidate_translation_not_dict",
        "candidate_translation_empty",
        "mismatched_raw_translation",
        "error_malformed",
        "error_for_selected_role",
        "strip_record_and_candidate",
        "extra_non_object_entry",
        "extra_foreign_record",
    ],
)
def test_whole_chapter_resume_rejects_malformed_linked_generation_record(
    tmp_path, mutation
):
    # RV3 finding: a sole whole_chapter record whose linked candidate_id
    # matches the journal was previously accepted even with record fields
    # (status/expected_roles) and candidate provenance fields
    # (translation/role) stripped — resume returned selected_count=1 and
    # rewrote selection/provenance from damaged data. The record must now
    # conform to the writer's serialized GenerationOutcome schema, and the
    # selected candidate must be raw/provenance-consistent, or the resume
    # fails closed before any artifact write.
    cfg = _whole_chapter_cfg(tmp_path)
    _run_whole_chapter(cfg)
    gen_path = cfg.out_dir / "generation_outcomes.json"
    good = json.loads(gen_path.read_text(encoding="utf-8"))
    rec = good["outcomes"][0]
    cand = rec["candidates"]["balanced_literary"]
    raw = json.loads((cfg.out_dir / "translations_raw.json").read_text(encoding="utf-8"))

    if mutation == "remove_record_status":
        rec.pop("status")
    elif mutation == "remove_record_expected_roles":
        rec.pop("expected_roles")
    elif mutation == "remove_record_risk_band":
        rec.pop("risk_band")
    elif mutation == "remove_record_candidates":
        rec.pop("candidates")
    elif mutation == "remove_record_errors":
        rec.pop("errors")
    elif mutation == "invalid_record_status":
        rec["status"] = "quarantined"
    elif mutation == "record_status_incomplete_on_selected":
        rec["status"] = "incomplete"
    elif mutation == "empty_expected_roles":
        rec["expected_roles"] = []
    elif mutation == "empty_candidates":
        rec["candidates"] = {}
    elif mutation == "candidate_not_object":
        rec["candidates"]["balanced_literary"] = ["not", "an", "object"]
    elif mutation == "remove_candidate_translation":
        cand.pop("translation")
    elif mutation == "remove_candidate_role":
        cand.pop("role")
    elif mutation == "remove_candidate_candidate_id":
        cand.pop("candidate_id")
    elif mutation == "remove_candidate_decision_trace":
        cand.pop("decision_trace")
    elif mutation == "candidate_translation_not_dict":
        cand["translation"] = "не словарь"
    elif mutation == "candidate_translation_empty":
        cand["translation"] = {}
    elif mutation == "mismatched_raw_translation":
        # Linked candidate_id kept, but the candidate's serialized translation
        # no longer equals the raw snapshot the resume would replay.
        cand["translation"] = dict(raw)
        first = next(iter(cand["translation"]))
        cand["translation"][first] = "ДРУГОЙ ПЕРЕВОД"
    elif mutation == "error_malformed":
        rec["errors"] = {"balanced_literary": {"code": 42}}
    elif mutation == "error_for_selected_role":
        rec["errors"] = {
            "balanced_literary": {"code": "invalid_json", "detail": "x"}
        }
    elif mutation == "strip_record_and_candidate":
        # The exact RV3 adversarial reproduction: the sole whole_chapter
        # record keeps chunk_id and the linked candidate_id, but the record
        # loses status/expected_roles and the candidate loses
        # translation/role.
        rec.pop("status")
        rec.pop("expected_roles")
        cand.pop("translation")
        cand.pop("role")
    elif mutation == "extra_non_object_entry":
        good["outcomes"].append("junk")
    elif mutation == "extra_foreign_record":
        good["outcomes"].append(dict(rec, chunk_id="chunk_0"))

    gen_path.write_text(json.dumps(good, ensure_ascii=False), encoding="utf-8")
    before = _snapshot_artifacts(cfg)

    caller = StubModelCaller()
    with pytest.raises(ValueError, match="Data loss"):
        _run_whole_chapter(cfg, model_caller=caller)
    # Fail closed: no generation call, and every artifact (including the
    # corrupt provenance itself) is byte-for-byte unchanged.
    assert len(caller.calls) == 0
    _assert_unchanged(cfg, before)


@pytest.mark.parametrize(
    "mutation",
    [
        "record_status_complete_on_incomplete",
        "candidates_on_incomplete",
        "no_errors_on_incomplete",
        "error_malformed_on_incomplete",
    ],
)
def test_whole_chapter_resume_rejects_malformed_incomplete_record(tmp_path, mutation):
    # The same writer-schema contract holds for an incomplete_generation
    # resume: the record must say status=incomplete with no candidates and a
    # well-formed error map. A malformed incomplete record must fail closed
    # with zero model calls and untouched artifacts.
    cfg = _whole_chapter_cfg(tmp_path)

    class _BrokenCaller:
        def __init__(self):
            self.calls = []

        def __call__(self, bundle):
            self.calls.append(bundle)
            return "{not json"

    first = _run_whole_chapter(cfg, model_caller=_BrokenCaller())
    assert first.incomplete_generation_count == 1
    gen_path = cfg.out_dir / "generation_outcomes.json"
    good = json.loads(gen_path.read_text(encoding="utf-8"))
    rec = good["outcomes"][0]

    if mutation == "record_status_complete_on_incomplete":
        rec["status"] = "complete"
    elif mutation == "candidates_on_incomplete":
        rec["candidates"] = {
            "balanced_literary": {
                "candidate_id": "whole_chapter:balanced_literary:deadbeef",
                "role": "balanced_literary",
                "translation": {"p00001": "Текст"},
                "decision_trace": [],
            }
        }
    elif mutation == "no_errors_on_incomplete":
        rec["errors"] = {}
    elif mutation == "error_malformed_on_incomplete":
        rec["errors"] = {"balanced_literary": {"detail": "нет кода"}}

    gen_path.write_text(json.dumps(good, ensure_ascii=False), encoding="utf-8")
    before = _snapshot_artifacts(cfg)

    caller = StubModelCaller()
    with pytest.raises(ValueError, match="Data loss"):
        _run_whole_chapter(cfg, model_caller=caller)
    assert len(caller.calls) == 0
    _assert_unchanged(cfg, before)


# ---------------------------------------------------------------------------
# RV4 (t_86913123) whole-chapter role-set consistency regression coverage.
#
# The writer emits exactly one expected role ([balanced_literary]) and keys
# its candidate/error maps exactly to that declared role set. The three
# bypasses below each keep the journal linkage (and, where relevant, a fully
# well-formed candidate/error value) intact, so the old per-field checks
# alone accepted them and resume proceeded with zero model calls and artifact
# rewrites. Each must now fail closed with a Data loss ValueError, zero model
# calls, and byte-for-byte unchanged artifacts — including the pre-existing
# strict_chapter_trial_record.json (no new record).
# ---------------------------------------------------------------------------


def test_whole_chapter_resume_rejects_foreign_candidate_role(tmp_path):
    # RV4 Finding 1: adding a valid-but-foreign fidelity_first candidate to
    # the sole record was accepted (set(candidates) vs expected_roles was
    # never compared). The whole-chapter writer never emits a candidate for
    # a role outside the exact [balanced_literary] contract.
    cfg = _whole_chapter_cfg(tmp_path)
    _run_whole_chapter(cfg)
    gen_path = cfg.out_dir / "generation_outcomes.json"
    good = json.loads(gen_path.read_text(encoding="utf-8"))
    rec = good["outcomes"][0]
    bl = rec["candidates"]["balanced_literary"]
    foreign = {
        "candidate_id": "whole_chapter:fidelity_first:deadbeef",
        "role": "fidelity_first",
        "translation": dict(bl["translation"]),
        "decision_trace": [{"gate": "gate_1", "passed": True, "detail": "x"}],
    }
    rec["candidates"]["fidelity_first"] = foreign
    gen_path.write_text(json.dumps(good, ensure_ascii=False), encoding="utf-8")
    before = _snapshot_artifacts(cfg)

    caller = StubModelCaller()
    with pytest.raises(ValueError, match="Data loss.*fidelity_first"):
        _run_whole_chapter(cfg, model_caller=caller)
    assert len(caller.calls) == 0
    _assert_unchanged(cfg, before)


def test_whole_chapter_resume_rejects_extra_expected_role(tmp_path):
    # RV4 Finding 2: mutating expected_roles to ['balanced_literary',
    # 'fidelity_first'] while retaining only the selected candidate was
    # accepted (only selected_role in expected_roles was checked). The
    # whole-chapter writer declares exactly one expected role.
    cfg = _whole_chapter_cfg(tmp_path)
    _run_whole_chapter(cfg)
    gen_path = cfg.out_dir / "generation_outcomes.json"
    good = json.loads(gen_path.read_text(encoding="utf-8"))
    rec = good["outcomes"][0]
    rec["expected_roles"] = ["balanced_literary", "fidelity_first"]
    gen_path.write_text(json.dumps(good, ensure_ascii=False), encoding="utf-8")
    before = _snapshot_artifacts(cfg)

    caller = StubModelCaller()
    with pytest.raises(ValueError, match="Data loss.*expected_roles"):
        _run_whole_chapter(cfg, model_caller=caller)
    assert len(caller.calls) == 0
    _assert_unchanged(cfg, before)


def test_whole_chapter_resume_rejects_foreign_error_role(tmp_path):
    # RV4 Finding 3: for an incomplete_generation resume, replacing the
    # writer's error map with a valid error keyed by foreign_role was
    # accepted (incomplete_generation_count=1, zero model calls), although
    # the writer emits errors keyed by the expected role.
    cfg = _whole_chapter_cfg(tmp_path)

    class _BrokenCaller:
        def __init__(self):
            self.calls = []

        def __call__(self, bundle):
            self.calls.append(bundle)
            return "{not json"

    first = _run_whole_chapter(cfg, model_caller=_BrokenCaller())
    assert first.incomplete_generation_count == 1
    gen_path = cfg.out_dir / "generation_outcomes.json"
    good = json.loads(gen_path.read_text(encoding="utf-8"))
    rec = good["outcomes"][0]
    rec["errors"] = {"foreign_role": {"code": "invalid_json", "detail": "x"}}
    gen_path.write_text(json.dumps(good, ensure_ascii=False), encoding="utf-8")
    before = _snapshot_artifacts(cfg)

    caller = StubModelCaller()
    with pytest.raises(ValueError, match="Data loss.*foreign_role"):
        _run_whole_chapter(cfg, model_caller=caller)
    assert len(caller.calls) == 0
    _assert_unchanged(cfg, before)


# ---------------------------------------------------------------------------
# V4.1 A2 (§7): translations_repaired.json + translation_diffs.json snapshots
# ---------------------------------------------------------------------------


def test_whole_chapter_writes_snapshots_with_identity_and_empty_diffs(tmp_path):
    # A2 snapshot contract: translations_repaired.json + translation_diffs.json
    # are written atomically (write-then-rename) with identity in every
    # snapshot; the diff stages (raw->repaired, repaired->final) are SEPARATE.
    # In A2 there is no repair/formatting yet (B/B2/C), so repaired == raw and
    # both diff stages are empty — the files establish the mechanism B2/C will
    # populate. translations.json stays the final alias (never a competing
    # source of truth).
    cfg = _whole_chapter_cfg(tmp_path)
    result = _run_whole_chapter(cfg)
    assert result.selected_count == 1

    repaired_path = cfg.out_dir / "translations_repaired.json"
    diffs_path = cfg.out_dir / "translation_diffs.json"
    assert repaired_path.exists()
    assert diffs_path.exists()

    repaired = json.loads(repaired_path.read_text(encoding="utf-8"))
    assert repaired["schema"] == "pact-v4-snapshot-translations-repaired/v1"
    # Identity in every snapshot.
    assert repaired["chapter_id"] == cfg.chapter_id
    assert repaired["snapshot_hash"] == result.record["identities"]["snapshot_hash"]
    assert repaired["chunk_plan_hash"] == result.record["identities"]["chunk_plan_hash"]
    assert repaired["config_identity"] == result.record["identities"]["config_identity"]
    # repaired == raw == final in A2 (no repair/formatting yet).
    raw = json.loads((cfg.out_dir / "translations_raw.json").read_text(encoding="utf-8"))
    final = json.loads(result.translations_path.read_text(encoding="utf-8"))
    assert repaired["translations"] == raw == final

    diffs = json.loads(diffs_path.read_text(encoding="utf-8"))
    assert diffs["schema"] == "pact-v4-translation-diffs/v1"
    assert diffs["chapter_id"] == cfg.chapter_id
    assert set(diffs["diffs"]) == {"raw->repaired", "repaired->final"}
    # No changes between stages in A2 -> both diff maps empty.
    assert diffs["diffs"]["raw->repaired"] == {}
    assert diffs["diffs"]["repaired->final"] == {}
    # Atomic write: write-then-rename, no leftover temp files.
    assert not list(cfg.out_dir.glob("*.tmp"))

    # The snapshots are attribution artifacts, not a competing final source:
    # translations.json remains the final alias equal to raw.
    assert final == raw


def test_whole_chapter_writes_glossary_budget_report_whole_chapter(tmp_path):
    # A2 (§5.3): the whole-chapter path records the full-chapter glossary
    # budget (kept/dropped pairs) in glossary_budget_report.json.
    cfg = _whole_chapter_cfg(tmp_path)
    _run_whole_chapter(cfg)
    report_path = cfg.out_dir / "glossary_budget_report.json"
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["chapter_id"] == cfg.chapter_id
    assert "whole_chapter" in report["chunks"]
    row = report["chunks"]["whole_chapter"]
    assert set(row) == {"kept", "dropped", "dropped_count"}
    assert row["dropped_count"] == len(row["dropped"])
    # The kept glossary feeds the whole-chapter bundle: every kept term is
    # present in the chapter text or always_include.
    assert not list(cfg.out_dir.glob("*.tmp"))


# ---------------------------------------------------------------------------
# A2 RV (review of commit 4ab250b): snapshot identity now binds source_hash +
# chapter_index_hash. Resume against a changed source text (same PIDs) or a
# changed chapter_index.json must fail closed — never silently replay a stale
# translation built from different inputs.
# ---------------------------------------------------------------------------


def test_whole_chapter_resume_fails_closed_on_source_text_change(tmp_path):
    # RV finding 1 (HIGH): two SourceArtifacts with the same PID set but
    # different text used to share one snapshot_hash, so resume replayed the
    # old translation against the changed source. The snapshot identity now
    # includes source_hash: a source text change (same PIDs) must fail closed
    # on resume with zero generation calls and no artifact rewrite.
    cfg = _whole_chapter_cfg(tmp_path)
    _run_whole_chapter(cfg)
    assert len(_whole_chapter_journal(cfg)) == 1

    # Rewrite the chapter HTML with DIFFERENT words but the SAME paragraph
    # count -> same PIDs, different source text (the resume must fail closed).
    from pathlib import Path

    def _write_changed_chapter_html(path: Path, n_paragraphs: int) -> None:
        paragraph_text = " ".join(f"changed{i}" for i in range(35))
        body = "\n".join(f"<p>{paragraph_text}</p>" for _ in range(n_paragraphs))
        path.write_text("<html><body>" + body + "</body></html>", encoding="utf-8")

    _write_changed_chapter_html(cfg.chapter_html_path, n_paragraphs=24)
    assert (cfg.chapter_html_path).exists()
    assert "changed0" in cfg.chapter_html_path.read_text(encoding="utf-8")

    before = _snapshot_artifacts(cfg)
    caller = StubModelCaller()
    with pytest.raises(ValueError, match="Foreign identity.*different snapshot/plan/config"):
        _run_whole_chapter(cfg, model_caller=caller)
    assert len(caller.calls) == 0
    _assert_unchanged(cfg, before)


def test_whole_chapter_resume_fails_closed_on_chapter_index_change(tmp_path):
    # RV finding 2 (HIGH): chapter_index.json participates in the bible prompt
    # but was absent from the snapshot identity, so changing it did not
    # invalidate resume. The snapshot identity now includes the SELECTED
    # chapter's index record: a changed record must fail closed on resume.
    cfg = _whole_chapter_cfg(tmp_path)
    index_path = cfg.memory_dir / "chapter_index.json"
    index_path.write_text(json.dumps({
        cfg.chapter_id: {"characters": ["Blake"], "facts": [], "address": []},
    }, ensure_ascii=False), encoding="utf-8")
    _run_whole_chapter(cfg)
    assert len(_whole_chapter_journal(cfg)) == 1

    # Change the SELECTED chapter's record -> different bible prompt.
    index_path.write_text(json.dumps({
        cfg.chapter_id: {"characters": ["Blake", "Duncan"], "facts": [], "address": []},
    }, ensure_ascii=False), encoding="utf-8")

    before = _snapshot_artifacts(cfg)
    caller = StubModelCaller()
    with pytest.raises(ValueError, match="Foreign identity.*different snapshot/plan/config"):
        _run_whole_chapter(cfg, model_caller=caller)
    assert len(caller.calls) == 0
    _assert_unchanged(cfg, before)


def test_whole_chapter_resume_chapter_index_other_chapter_change_is_noop(tmp_path):
    # The SELECTED record is the identity input: a change to ANOTHER chapter's
    # index record must NOT invalidate this chapter's resume (the bible prompt
    # for this chapter is unchanged).
    cfg = _whole_chapter_cfg(tmp_path)
    index_path = cfg.memory_dir / "chapter_index.json"
    index_path.write_text(json.dumps({
        cfg.chapter_id: {"characters": ["Blake"], "facts": [], "address": []},
        "ch999": {"characters": ["Other"], "facts": [], "address": []},
    }, ensure_ascii=False), encoding="utf-8")
    _run_whole_chapter(cfg)

    index_path.write_text(json.dumps({
        cfg.chapter_id: {"characters": ["Blake"], "facts": [], "address": []},
        "ch999": {"characters": ["Other", "Changed"], "facts": [], "address": []},
    }, ensure_ascii=False), encoding="utf-8")

    caller = StubModelCaller()
    resumed = _run_whole_chapter(cfg, model_caller=caller)
    assert len(caller.calls) == 0
    assert resumed.resumed_from_index == 1


def test_whole_chapter_record_identities_include_chapter_index_hash(tmp_path):
    # RV finding 2: the strict_chapter_trial_record.json identities must carry
    # a verifiable chapter_index_hash (the snapshot identity input).
    cfg = _whole_chapter_cfg(tmp_path)
    index_path = cfg.memory_dir / "chapter_index.json"
    index_path.write_text(json.dumps({
        cfg.chapter_id: {"characters": ["Blake"], "facts": [], "address": []},
    }, ensure_ascii=False), encoding="utf-8")
    result = _run_whole_chapter(cfg)
    record = json.loads(result.record_path.read_text(encoding="utf-8"))
    identities = record["identities"]
    assert "chapter_index_hash" in identities
    assert len(identities["chapter_index_hash"]) == 64
    assert identities["snapshot_hash"] == result.record["identities"]["snapshot_hash"]


# ---------------------------------------------------------------------------
# A2 RV finding 3: whole-chapter empty/truncated model output through the REAL
# BackendModelCaller must yield a bounded, honest incomplete_generation (no
# uncaught adapter JSON exception, no partial translation accepted). The
# adapter retries empty/truncated bodies with its own JsonRetryPolicy budget
# and re-raises EmptyResponseError/TruncatedJSONError on exhaustion — the
# whole-chapter generation layer must catch/classify those, not crash.
# ---------------------------------------------------------------------------


def _scripted_model_caller(*scripts: str, adapter_max_retries: int = 0):
    from tests.pact_v4.runtime.test_backend_role_adapters import (
        ScriptedBackend,
        _text_response,
    )
    from pact_v4.runtime.backend_role_adapters import (
        BackendModelCaller,
        BackendModelCallerConfig,
    )
    from pact_v4.runtime.json_resilience import JsonRetryPolicy

    backend = ScriptedBackend([_text_response(s) for s in scripts])
    caller = BackendModelCaller(
        backend,
        config=BackendModelCallerConfig(
            max_tokens=32768,
            # 0 (default here) = the generation layer owns retry (what the CLI
            # wires for whole-chapter runs); >0 = the adapter's own B4/B10
            # JSON-retry budget, which on exhaustion re-raises the empty/
            # truncated exception INTO the generation layer (the crash the A2
            # RV found).
            retry=JsonRetryPolicy(max_retries=adapter_max_retries),
        ),
    )
    return caller, backend


@pytest.mark.parametrize(
    "bad_output",
    [
        "",  # empty body
        "{truncated json",  # truncated / malformed JSON
    ],
)
def test_whole_chapter_empty_or_truncated_output_via_backend_model_caller(
    tmp_path, bad_output
):
    # The whole-chapter retry budget (WholeChapterRetryPolicy.max_attempts=3)
    # is the SINGLE retry owner (adapter JSON retry disabled, as the CLI wires
    # for whole-chapter runs); each generation attempt is exactly one backend
    # call. An always-bad model exhausts the budget honestly:
    # incomplete_generation, zero selected, no translations_raw written, no
    # uncaught exception.
    from pact_v4.phase2.generation import WholeChapterRetryPolicy

    cfg = _whole_chapter_cfg(tmp_path)
    caller, backend = _scripted_model_caller(*([bad_output] * WholeChapterRetryPolicy().max_attempts))
    result = _run_whole_chapter(cfg, model_caller=caller)

    assert result.incomplete_generation_count == 1
    assert result.selected_count == 0
    assert result.halted_early is True
    assert "whole_chapter generation incomplete" in (result.halt_reason or "")
    assert len(backend.requests) == WholeChapterRetryPolicy().max_attempts
    assert not (cfg.out_dir / "translations_raw.json").exists()

    sel = json.loads((cfg.out_dir / "selection_results.json").read_text(encoding="utf-8"))
    assert sel["candidate_count"] == 0
    assert sel["generation_record_id"] is None

    journal = _whole_chapter_journal(cfg)
    assert journal[0]["outcome"] == "incomplete_generation"


@pytest.mark.parametrize(
    "bad_output",
    [
        "",  # empty body
        "{truncated json",  # truncated / malformed JSON
    ],
)
def test_whole_chapter_adapter_budget_exhaustion_is_classified_not_crash(
    tmp_path, bad_output
):
    # A2 RV finding 3 reproduction: with the DEFAULT adapter JSON retry budget
    # (max_retries=2), BackendModelCaller retries the bad body 3 times and then
    # re-raises EmptyResponseError/TruncatedJSONError. The whole-chapter
    # generation layer must catch/classify that (INVALID_JSON inside its own
    # bounded loop) and return an honest incomplete_generation — NOT crash
    # with an uncaught adapter exception. Total model calls stay bounded:
    # max_attempts(3) × adapter budget(3) = 9.
    from pact_v4.phase2.generation import WholeChapterRetryPolicy
    from pact_v4.runtime.json_resilience import JsonRetryPolicy

    cfg = _whole_chapter_cfg(tmp_path)
    adapter_calls_per_attempt = JsonRetryPolicy().max_retries + 1  # 3
    total = WholeChapterRetryPolicy().max_attempts * adapter_calls_per_attempt
    caller, backend = _scripted_model_caller(
        *([bad_output] * total), adapter_max_retries=JsonRetryPolicy().max_retries,
    )
    result = _run_whole_chapter(cfg, model_caller=caller)

    assert result.incomplete_generation_count == 1
    assert result.selected_count == 0
    assert result.halted_early is True
    assert "whole_chapter generation incomplete" in (result.halt_reason or "")
    assert len(backend.requests) == total
    assert not (cfg.out_dir / "translations_raw.json").exists()
    journal = _whole_chapter_journal(cfg)
    assert journal[0]["outcome"] == "incomplete_generation"


# ---------------------------------------------------------------------------
# A2 RV finding 7: resource cleanup must run on EVERY exit path — including
# fail-closed resume validation errors (which occur after progress.run_started
# and used to bypass runtime.close()/writers close).
# ---------------------------------------------------------------------------


class _CloseTrackingProgress:
    def __init__(self):
        self.closed = False
        self.started = 0

    def run_started(self, **kwargs):
        self.started += 1

    def chunk_started(self, **kwargs):
        pass

    def chunk_done(self, **kwargs):
        pass

    def wc_generation_started(self, **kwargs):
        pass

    def wc_retry_attempt(self, **kwargs):
        pass

    def wc_generation_done(self, **kwargs):
        pass

    def wc_validated(self, **kwargs):
        pass

    def close(self):
        self.closed = True


class _CloseTrackingUsageWriter:
    def __init__(self):
        self.closed = False

    def write_call(self, *args, **kwargs):
        pass

    def close(self):
        self.closed = True


class _CloseTrackingRuntime:
    def __init__(self):
        self.closed = False
        self._events = []

    def set_usage_writer(self, writer):
        pass

    def event_count(self):
        return len(self._events)

    def events_since(self, index):
        return self._events[index:]

    def local_switch_event_indices(self, start):
        return []

    def release(self):
        pass

    def close(self):
        self.closed = True

    def summary(self):
        return {"local_lifecycle": None, "remote_calls": []}

    @property
    def backend_descriptor(self):
        from pact_v4.runtime.backend_protocol import BackendDescriptor

        return BackendDescriptor(
            kind="local_llama",
            transport_version="openai-chat-completions/v1",
            endpoint_family="openai_chat_completions",
            public_endpoint="http://127.0.0.1:8094",
            model_bindings={},
            effective_options={},
        )


def test_whole_chapter_resume_validation_failure_closes_resources(tmp_path):
    # A malformed whole-chapter resume (missing generation_outcomes.json)
    # raises a fail-closed Data-loss ValueError AFTER progress.run_started —
    # runtime/progress/usage writers must still be closed (the wrapper's
    # finally guarantees cleanup while the error propagates unchanged).
    cfg = _whole_chapter_cfg(tmp_path)
    _run_whole_chapter(cfg)
    (cfg.out_dir / "generation_outcomes.json").unlink()

    runtime = _CloseTrackingRuntime()
    progress = _CloseTrackingProgress()
    usage = _CloseTrackingUsageWriter()
    caller = StubModelCaller()
    with pytest.raises(ValueError, match="Data loss.*generation_outcomes.json.*missing"):
        run_chapter_strict(
            cfg, runtime=runtime, model_caller=caller,
            qwen_evaluator=StubQwen(), gemma_selector=StubGemma(),
            qwen_audit_evaluator=StubQwenAudit(), gemma_audit_evaluator=StubGemmaAudit(),
            progress=progress, usage_writer=usage,
        )
    assert len(caller.calls) == 0
    assert progress.started >= 1
    assert runtime.closed is True
    assert progress.closed is True
    assert usage.closed is True


def test_whole_chapter_success_closes_resources(tmp_path):
    # Cleanup also runs on the SUCCESS path (wrapper finally, not just the
    # old successful-tail close).
    cfg = _whole_chapter_cfg(tmp_path)
    runtime = _CloseTrackingRuntime()
    progress = _CloseTrackingProgress()
    usage = _CloseTrackingUsageWriter()
    result = run_chapter_strict(
        cfg, runtime=runtime, model_caller=_LifecycleAwareModelCaller(
            _make_router(), StubModelCaller()
        ),
        qwen_evaluator=StubQwen(), gemma_selector=StubGemma(),
        qwen_audit_evaluator=StubQwenAudit(), gemma_audit_evaluator=StubGemmaAudit(),
        progress=progress, usage_writer=usage,
    )
    assert result.selected_count == 1
    assert runtime.closed is True
    assert progress.closed is True
    assert usage.closed is True


# ---------------------------------------------------------------------------
# A2 RV finding 2 (pre-dispatch cleanup): runtime/progress/usage are created
# and the usage writer is attached BEFORE source/snapshot/planner rebuild.
# A failure there (e.g. empty/malformed source -> "no source blocks parsed")
# used to leak all three because the whole-chapter wrapper's finally only
# covers failures once dispatch has started. The outer guard in
# run_chapter_strict must close them and re-raise the ORIGINAL exception
# (a cleanup error must never mask it).
# ---------------------------------------------------------------------------


def test_whole_chapter_pre_dispatch_source_failure_closes_resources(tmp_path):
    # Empty/malformed chapter html -> load_source parses no blocks -> the
    # pre-dispatch ValueError must close runtime/progress/usage and propagate
    # unchanged.
    cfg = _whole_chapter_cfg(tmp_path)
    cfg.chapter_html_path.write_text("<html><body></body></html>", encoding="utf-8")

    runtime = _CloseTrackingRuntime()
    progress = _CloseTrackingProgress()
    usage = _CloseTrackingUsageWriter()
    with pytest.raises(ValueError, match="no source blocks parsed"):
        run_chapter_strict(
            cfg, runtime=runtime, model_caller=StubModelCaller(),
            qwen_evaluator=StubQwen(), gemma_selector=StubGemma(),
            qwen_audit_evaluator=StubQwenAudit(), gemma_audit_evaluator=StubGemmaAudit(),
            progress=progress, usage_writer=usage,
        )
    assert runtime.closed is True
    assert progress.closed is True
    assert usage.closed is True


def test_whole_chapter_pre_dispatch_planner_failure_closes_resources(tmp_path):
    # A planner failure ("planner returned no chunks") is also pre-dispatch:
    # resources must close and the original ValueError must propagate. The
    # planner is patched to return no plans while load_source still parses
    # valid blocks, so the failure is exactly the planner branch.
    import pact_v4.pipeline.v4_phase12_strict_runner as runner

    orig_planner = runner.ChunkPlanner

    class _EmptyPlanner:
        def __init__(self, **kwargs):
            pass

        def plan(self, blocks, *, snapshot_hash, following_blocks):
            return []

    runner.ChunkPlanner = _EmptyPlanner
    try:
        runtime = _CloseTrackingRuntime()
        progress = _CloseTrackingProgress()
        usage = _CloseTrackingUsageWriter()
        with pytest.raises(ValueError, match="planner returned no chunks"):
            run_chapter_strict(
                _whole_chapter_cfg(tmp_path), runtime=runtime,
                model_caller=StubModelCaller(),
                qwen_evaluator=StubQwen(), gemma_selector=StubGemma(),
                qwen_audit_evaluator=StubQwenAudit(),
                gemma_audit_evaluator=StubGemmaAudit(),
                progress=progress, usage_writer=usage,
            )
        assert runtime.closed is True
        assert progress.closed is True
        assert usage.closed is True
    finally:
        runner.ChunkPlanner = orig_planner


# ---------------------------------------------------------------------------
# A2 RV finding 1 (whole-chapter retry ownership): the default-local CLI path
# (run_local_default -> build_strict_lifecycle -> LifecycleModelCaller ->
# HttpModelCaller -> BackendModelCallerConfig) must disable the adapter-level
# JsonRetryPolicy for whole-chapter runs (max_retries=0), so total model calls
# == WholeChapterRetryPolicy.max_attempts (single retry owner) — never
# max_attempts × adapter budget (9). The chunked path keeps the default
# max_retries=2.
# ---------------------------------------------------------------------------


class _URLAdapter(FakeLifecycleAdapter):
    """Fake lifecycle adapter with a base_url (ModelRouter.base_url needs it)."""

    base_url = "http://127.0.0.1:1"  # never contacted (backend swapped in test)


class _FakeLifecycleBackend:
    """Stand-in for the default-local StrictBackendConfig: fake runtime+router."""

    model_names = {"gemma": "gemma-fake", "qwen": "qwen-fake"}

    def build_runtime(self, log_dir=None):
        class _Runtime:
            pass

        runtime = _Runtime()
        runtime.router = ModelRouter(
            _URLAdapter(),
            role_profile_names={"gemma": "Gemma", "qwen": "Qwen"},
            role_args={"gemma": [], "qwen": []},
        )
        return runtime


def test_build_strict_lifecycle_wires_retry_policy_through_caller(tmp_path):
    # The lifecycle/default-local boundary must thread json_retry_policy into
    # the generation caller's BackendModelCallerConfig.retry: max_retries=0
    # for whole-chapter (adapter budget disabled), default max_retries=2 when
    # no policy is given (chunked path unchanged).
    from pact_v4.runtime.json_resilience import JsonRetryPolicy

    backend = _FakeLifecycleBackend()
    _, wc_caller, *_ = build_strict_lifecycle(
        backend, log_dir=tmp_path / "logs",
        json_retry_policy=JsonRetryPolicy(max_retries=0),
    )
    assert wc_caller._caller._impl._config.retry.max_retries == 0

    _, chunked_caller, *_ = build_strict_lifecycle(backend, log_dir=tmp_path / "logs")
    assert chunked_caller._caller._impl._config.retry.max_retries == JsonRetryPolicy().max_retries


def test_whole_chapter_default_local_lifecycle_bounds_calls_to_max_attempts(tmp_path):
    # Through the real default-local lifecycle (build_strict_lifecycle with
    # the same wiring run_local_default uses for --whole-chapter) an
    # always-bad model must cost exactly WholeChapterRetryPolicy.max_attempts
    # backend calls — the adapter-level JSON retry is disabled, so the
    # generation layer is the single retry owner (3 calls, not 3×3=9).
    from pact_v4.phase2.generation import WholeChapterRetryPolicy
    from pact_v4.runtime.json_resilience import JsonRetryPolicy
    from tests.pact_v4.runtime.test_backend_role_adapters import (
        ScriptedBackend,
        _text_response,
    )

    cfg = _whole_chapter_cfg(tmp_path)
    backend = _FakeLifecycleBackend()
    router, model_caller, *_ = build_strict_lifecycle(
        backend, log_dir=tmp_path / "logs",
        json_retry_policy=JsonRetryPolicy(max_retries=0),
    )
    # Swap the HTTP transport for a scripted always-bad backend: the
    # lifecycle caller still performs the router.ensure_resident + request
    # construction, only the final transport is scripted.
    scripted = ScriptedBackend(
        [_text_response("")] * WholeChapterRetryPolicy().max_attempts
    )
    model_caller._caller._impl._backend = scripted

    result = run_chapter_strict(
        cfg, router=router, model_caller=model_caller,
        qwen_evaluator=StubQwen(), gemma_selector=StubGemma(),
        qwen_audit_evaluator=StubQwenAudit(), gemma_audit_evaluator=StubGemmaAudit(),
    )

    assert result.incomplete_generation_count == 1
    assert result.selected_count == 0
    assert len(scripted.requests) == WholeChapterRetryPolicy().max_attempts
    assert not (cfg.out_dir / "translations_raw.json").exists()
