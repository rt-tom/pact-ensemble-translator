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
