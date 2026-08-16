"""KILL-SAFE-INCREMENTAL (t_2d16962c): repair / reaudit evaluator-level
kill-sims — cached GOOD batches / reaudit chunks replay with 0 model calls.

Acceptance:
- repair 2/4 batches committed -> resume: committed NOT repeated (cached
  GOOD batches replayed with 0 repair calls), the 2 missing batches redone;
- reaudit 1/3 chunks -> resume: 1 cached chunk replayed with 0 reaudit
  calls, the rest re-run.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from pact_v4.repair.selective_repair import (
    SelectiveRepairConfig,
    SelectiveRepairEvaluator,
)
from tests.pact_v4.pipeline.test_b3_kill_safe_incremental import (
    _audit_pending_stage,
    _load_stage_progress,
    _r_editor_pending_stage,
    _repair_pending_stage,
    _save_stage_progress,
    _stage_progress_with,
)
from tests.pact_v4.repair.test_selective_repair import (
    CONFIRMED,
    FilteredIssue,
    ScriptedRepairBackend,
    _hard_filtered,
    _issue,
    _reaudit_response,
    _repair_response,
)


def _confirmed_finding(pid: str) -> FilteredIssue:
    """A Tier-A CONFIRMED finding (repair-direct, no verify round)."""
    return FilteredIssue(
        issue=_issue(pid, "addition", note="дублирование", excerpt="text"),
        verdict=CONFIRMED, filter_name="test", reason="test",
    )


def test_kill_safe_repair_cached_2of4_batches_zero_calls() -> None:
    """kill-sim repair 2/4: 4 findings -> 4 single-finding batches; the
    resume plan carries 2 cached GOOD batches (committed/passed verbatim),
    so only batches 3-4 hit the model — 2 repair calls total, and the cached
    batch 1 commit is NOT re-requested."""
    source = {f"p{i:05d}": f"source text {i}" for i in range(1, 5)}
    translation = {f"p{i:05d}": f"перевод текста {i}" for i in range(1, 5)}
    filtered = [_confirmed_finding(f"p{i:05d}") for i in range(1, 5)]

    cached_batches = {
        1: {
            "status": "GOOD",
            "findings_pids": ["p00001"],
            "results": [{
                "index": 1, "decision": "repair", "pid": "p00001",
                "repaired_translation": "перевод текста 1 исправленный",
                "reason": "убрал дубль",
            }],
        },
        2: {
            "status": "GOOD",
            "findings_pids": ["p00002"],
            "results": [{
                "index": 1, "decision": "pass", "pid": "p00002",
                "repaired_translation": "",
                "reason": "verified",
            }],
        },
    }

    # Only batches 3 and 4 hit the model (2 repair calls), then the re-audit
    # (committed non-empty -> re-audit runs over the committed pids).
    backend = ScriptedRepairBackend([
        _repair_response([{
            "index": 1, "decision": "pass", "pid": "p00003",
            "reason": "verified",
        }]),
        _repair_response([{
            "index": 1, "decision": "pass", "pid": "p00004",
            "reason": "verified",
        }]),
        _reaudit_response([]),
    ])
    evaluator = SelectiveRepairEvaluator(
        backend,
        config=SelectiveRepairConfig(
            microbatch_trigger=1,
            microbatch_target=1,
            reaudit_enabled=False,  # isolate the batch replay from reaudit
        ),
    )
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered, cached_batches=cached_batches,
    )
    repair_calls = [
        r for r in backend.requests if "selective_repair" in (r.label or "")
    ]
    # 2 of 4 batches replayed (0 calls); only batches 3-4 fresh.
    assert len(repair_calls) == 2
    # The cached batch 1 commit was reused verbatim, NOT re-requested.
    committed = dict(outcome.committed)
    assert committed["p00001"] == "перевод текста 1 исправленный"
    assert outcome.repair_complete is True
    assert outcome.batches[0].status == "GOOD"
    assert outcome.batches[1].status == "GOOD"
    assert outcome.batches[2].status == "GOOD"
    assert outcome.batches[3].status == "GOOD"


def test_kill_safe_repair_cached_batch_finding_mismatch_fails_closed() -> None:
    """A cached GOOD batch whose finding pids do NOT match the current batch
    is a fail-closed re-run — never a partial replay of a batch whose
    evidence changed."""
    source = {"p00001": "source text 1", "p00002": "source text 2"}
    translation = {"p00001": "перевод текста 1", "p00002": "перевод текста 2"}
    filtered = [_confirmed_finding("p00001"), _confirmed_finding("p00002")]

    # Batch 1 is cached as GOOD but with a DIFFERENT finding pid set.
    cached_batches = {
        1: {
            "status": "GOOD",
            "findings_pids": ["p00099"],  # stale/mismatched evidence
            "results": [{
                "index": 1, "decision": "repair", "pid": "p00099",
                "repaired_translation": "stale",
                "reason": "stale",
            }],
        },
    }
    backend = ScriptedRepairBackend([
        _repair_response([{
            "index": 1, "decision": "pass", "pid": "p00001",
            "reason": "verified",
        }]),
        _repair_response([{
            "index": 1, "decision": "pass", "pid": "p00002",
            "reason": "verified",
        }]),
    ])
    evaluator = SelectiveRepairEvaluator(
        backend,
        config=SelectiveRepairConfig(
            microbatch_trigger=1,
            microbatch_target=1,
            reaudit_enabled=False,
        ),
    )
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered, cached_batches=cached_batches,
    )
    repair_calls = [
        r for r in backend.requests if "selective_repair" in (r.label or "")
    ]
    # The mismatched cache was NOT replayed: both batches hit the model.
    assert len(repair_calls) == 2
    assert "p00099" not in dict(outcome.committed)
    assert "stale" not in json.dumps(outcome.to_payload(), ensure_ascii=False)


def test_kill_safe_reaudit_cached_1of3_zero_calls() -> None:
    """kill-sim reaudit 1/3: the resume plan carries 1 cached reaudit chunk
    whose boundaries match the current chunk — it replays with 0 reaudit
    calls; the other 2 chunks re-run."""
    # 3 committed pids -> re-audit scope 3 pids -> 3 single-pid chunks.
    source = {f"p{i:05d}": f"source text {i}" for i in range(1, 4)}
    translation = {f"p{i:05d}": f"перевод текста {i}" for i in range(1, 4)}
    filtered = [_confirmed_finding(f"p{i:05d}") for i in range(1, 4)]

    cached_reaudit_chunks = {
        1: {
            "first_pid": "p00001",
            "last_pid": "p00001",
            "issues": [],
        },
    }
    # 3 repair batches (1 per finding), then re-audit: chunk 1 replayed
    # (0 calls), chunks 2-3 re-run (2 reaudit calls).
    backend = ScriptedRepairBackend([
        _repair_response([{
            "index": 1, "decision": "repair", "pid": "p00001",
            "repaired_translation": "перевод текста 1 исправленный",
            "reason": "fix",
        }]),
        _repair_response([{
            "index": 1, "decision": "repair", "pid": "p00002",
            "repaired_translation": "перевод текста 2 исправленный",
            "reason": "fix",
        }]),
        _repair_response([{
            "index": 1, "decision": "repair", "pid": "p00003",
            "repaired_translation": "перевод текста 3 исправленный",
            "reason": "fix",
        }]),
        _reaudit_response([]),  # chunk 2
        _reaudit_response([]),  # chunk 3
    ])
    evaluator = SelectiveRepairEvaluator(
        backend,
        config=SelectiveRepairConfig(
            microbatch_trigger=1,
            microbatch_target=1,
            reaudit_max_input_tokens=1,  # 1 pid per reaudit chunk
            reaudit_neighbour_window=0,
        ),
    )
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered, cached_reaudit_chunks=cached_reaudit_chunks,
    )
    reaudit_calls = [
        r for r in backend.requests if "reaudit" in (r.label or "")
    ]
    # 1 of 3 reaudit chunks replayed (0 calls); chunks 2-3 re-run.
    assert len(reaudit_calls) == 2
    assert outcome.repair_complete is True
    assert outcome.reaudit is not None and outcome.reaudit.complete


def test_kill_safe_reaudit_cached_chunk_preserves_dropped() -> None:
    """CONTEXT-PID-DROP (RV t_7e7cfe6f finding 2): a cached reaudit chunk
    whose fresh run journaled ``dropped`` context/foreign issue objects keeps
    them in the replayed chunk record — a killed/resumed re-audit no longer
    loses the diagnostics. The dropped issues stay OUT of the re-audit
    findings (all_issues/outcome.reaudit.issues)."""
    source = {f"p{i:05d}": f"source text {i}" for i in range(1, 4)}
    translation = {f"p{i:05d}": f"перевод текста {i}" for i in range(1, 4)}
    filtered = [_confirmed_finding(f"p{i:05d}") for i in range(1, 4)]

    dropped_issue = {
        "id": "p00099", "category": "addition", "severity": "major",
        "confidence": "high", "note": "foreign pid", "excerpt": "text",
        # harness _debug is attached at journal time (RV2 t_61af1bb2) —
        # the persisted dropped object satisfies the exact _ISSUE_KEYS
        # contract the incremental cache validator enforces on load.
        "_debug": {
            "chunk": 1,
            "reasoning_file": "b3_repair_reaudit_chunk1_reasoning.txt",
        },
    }
    cached_reaudit_chunks = {
        1: {
            "first_pid": "p00001",
            "last_pid": "p00001",
            "issues": [],
            # journaled by the fresh run — must survive the replay
            "dropped": [dict(dropped_issue)],
        },
    }
    progress_records: List[Dict[str, Any]] = []

    def _on_progress(kind: str, fields: Dict[str, Any]) -> None:
        if kind == "reaudit_chunk_done":
            progress_records.append(dict(fields))

    backend = ScriptedRepairBackend([
        _repair_response([{
            "index": 1, "decision": "repair", "pid": "p00001",
            "repaired_translation": "перевод текста 1 исправленный",
            "reason": "fix",
        }]),
        _repair_response([{
            "index": 1, "decision": "repair", "pid": "p00002",
            "repaired_translation": "перевод текста 2 исправленный",
            "reason": "fix",
        }]),
        _repair_response([{
            "index": 1, "decision": "repair", "pid": "p00003",
            "repaired_translation": "перевод текста 3 исправленный",
            "reason": "fix",
        }]),
        _reaudit_response([]),  # chunk 2
        _reaudit_response([]),  # chunk 3
    ])
    evaluator = SelectiveRepairEvaluator(
        backend,
        config=SelectiveRepairConfig(
            microbatch_trigger=1,
            microbatch_target=1,
            reaudit_max_input_tokens=1,  # 1 pid per reaudit chunk
            reaudit_neighbour_window=0,
        ),
        on_progress=_on_progress,
    )
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered, cached_reaudit_chunks=cached_reaudit_chunks,
    )
    reaudit_calls = [
        r for r in backend.requests if "reaudit" in (r.label or "")
    ]
    assert len(reaudit_calls) == 2  # chunk 1 replayed with 0 calls
    assert outcome.repair_complete is True
    assert outcome.reaudit is not None and outcome.reaudit.complete
    # The replayed chunk record keeps the journaled dropped diagnostics.
    replayed_records = [
        r for r in progress_records
        for c in r.get("done_chunks") or ()
        if c.get("chunk") == 1 and c.get("dropped")
    ]
    assert replayed_records, "replayed chunk record lost the dropped field"
    assert replayed_records[0]["done_chunks"][0]["dropped"] == [dropped_issue]
    # Dropped issues are NOT re-audit findings.
    assert outcome.reaudit.issues == ()


def test_reaudit_fresh_dropped_journaled_with_debug() -> None:
    """CONTEXT-PID-DROP (RV2 t_61af1bb2): a FRESH re-audit journals its
    dropped context/foreign issue objects as COMPLETE well-formed issue
    objects — with the harness ``_debug`` {chunk, reasoning_file} attached
    at journal time (same contract the incremental cache validator enforces
    on load). The dropped issue stays OUT of the re-audit findings."""
    issue = _issue("p00005", "invented_gender", note="n", confidence="high")
    source = {f"p{i:05d}": f"Source paragraph {i}." for i in range(1, 11)}
    translation = {f"p{i:05d}": f"Перевод абзаца {i}." for i in range(1, 11)}
    filtered = _hard_filtered([issue], source, translation)

    progress_records: List[Dict[str, Any]] = []

    def _on_progress(kind: str, fields: Dict[str, Any]) -> None:
        if kind == "reaudit_chunk_done":
            progress_records.append(dict(fields))

    backend = ScriptedRepairBackend([
        _repair_response([{
            "index": 1, "decision": "repair", "pid": "p00005",
            "repaired_translation": "Исправленный перевод абзаца 5.",
            "reason": "confirmed",
        }]),
        # re-audit response: one valid residual issue on an owned pid
        # (p00005) + one issue on a CONTEXT pid (p00002) -> dropped.
        _reaudit_response([
            {"id": "p00005", "category": "changed_fact", "severity": "major",
             "confidence": "high", "note": "residual", "excerpt": "text"},
            {"id": "p00002", "category": "changed_fact", "severity": "major",
             "confidence": "high", "note": "context-only", "excerpt": "text"},
        ]),
    ])
    evaluator = SelectiveRepairEvaluator(
        backend,
        config=SelectiveRepairConfig(reaudit_neighbour_window=2),
        on_progress=_on_progress,
    )
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered,
    )
    assert outcome.reaudit is not None and outcome.reaudit.complete
    # the context-pid issue is dropped — never a re-audit finding
    assert [i["id"] for i in outcome.reaudit.issues] == ["p00005"]
    # the fresh chunk record journals the dropped object COMPLETE with _debug
    journaled = [
        c for r in progress_records
        for c in (r.get("done_chunks") or ())
        if c.get("dropped")
    ]
    assert journaled, "fresh re-audit chunk record lost the dropped field"
    dropped = journaled[0]["dropped"]
    assert len(dropped) == 1
    assert dropped[0]["id"] == "p00002"
    assert dropped[0]["_debug"] == {
        "chunk": 1,
        "reasoning_file": "b3_repair_reaudit_chunk1_reasoning.txt",
    }
    assert set(dropped[0]) == {
        "id", "category", "severity", "confidence", "note", "excerpt",
        "_debug",
    }


def test_reaudit_fresh_dropped_extra_field_exact_schema_cache_survival(
    tmp_path: Path,
) -> None:
    """CONTEXT-PID-DROP (RV3 t_c9eb65d4): a FRESH re-audit dropped
    context/foreign issue whose model response carries an unknown EXTRA
    field (validate_chunk_json accepts it — well-formed vocab/id) is
    journaled with ONLY the canonical issue fields + the harness ``_debug``.
    The emitted dropped object is exactly ``_ISSUE_KEYS`` (extra field
    stripped), so the persisted stage-progress payload SURVIVES the
    incremental cache (load -> ``reaudit_resume_plan`` -> cached replay /
    chunk records) instead of tripping a full miss — and stays OUT of the
    re-audit findings (all_issues) throughout."""
    issue = _issue("p00005", "invented_gender", note="n", confidence="high")
    source = {f"p{i:05d}": f"Source paragraph {i}." for i in range(1, 11)}
    translation = {f"p{i:05d}": f"Перевод абзаца {i}." for i in range(1, 11)}
    filtered = _hard_filtered([issue], source, translation)

    progress_records: List[Dict[str, Any]] = []

    def _on_progress(kind: str, fields: Dict[str, Any]) -> None:
        if kind == "reaudit_chunk_done":
            progress_records.append(dict(fields))

    backend = ScriptedRepairBackend([
        _repair_response([{
            "index": 1, "decision": "repair", "pid": "p00005",
            "repaired_translation": "Исправленный перевод абзаца 5.",
            "reason": "confirmed",
        }]),
        # re-audit response: one valid residual issue on an owned pid
        # (p00005) + one issue on a CONTEXT pid (p00002) carrying an
        # unknown EXTRA model field -> dropped by validate_chunk_json.
        _reaudit_response([
            {"id": "p00005", "category": "changed_fact", "severity": "major",
             "confidence": "high", "note": "residual", "excerpt": "text"},
            {"id": "p00002", "category": "changed_fact", "severity": "major",
             "confidence": "high", "note": "context-only", "excerpt": "text",
             "extra": "model-extra-field"},
        ]),
    ])
    evaluator = SelectiveRepairEvaluator(
        backend,
        config=SelectiveRepairConfig(reaudit_neighbour_window=2),
        on_progress=_on_progress,
    )
    outcome = evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered,
    )
    assert outcome.reaudit is not None and outcome.reaudit.complete
    # the extra-field context issue is dropped — never a re-audit finding
    assert [i["id"] for i in outcome.reaudit.issues] == ["p00005"]
    # the fresh chunk record journals the dropped object EXACT-schema: the
    # extra model field is stripped, only canonical fields + _debug remain.
    journaled = [
        c for r in progress_records
        for c in (r.get("done_chunks") or ())
        if c.get("dropped")
    ]
    assert journaled, "fresh re-audit chunk record lost the dropped field"
    dropped = journaled[0]["dropped"]
    assert len(dropped) == 1
    assert dropped[0]["id"] == "p00002"
    assert "extra" not in dropped[0]
    assert set(dropped[0]) == {
        "id", "category", "severity", "confidence", "note", "excerpt",
        "_debug",
    }
    assert dropped[0]["_debug"] == {
        "chunk": 1,
        "reasoning_file": "b3_repair_reaudit_chunk1_reasoning.txt",
    }

    # ------------------------------------------------------------------
    # Persist the emitted chunk record into an incremental stage_progress
    # cache exactly as the pipeline's _on_repair_progress would — the
    # exact-schema dropped object must SURVIVE cache -> resume plan.
    # ------------------------------------------------------------------
    done_chunks = [
        dict(c) for r in progress_records
        for c in (r.get("done_chunks") or ())
    ]
    stage = _stage_progress_with(
        r_editor=_r_editor_pending_stage(),
        audit=_audit_pending_stage(),
        repair=_repair_pending_stage(),
        reaudit={
            "status": "complete",
            "done_chunks": done_chunks,
            "issues": [
                dict(i) for c in done_chunks for i in (c.get("issues") or ())
            ],
        },
    )
    path = _save_stage_progress(
        tmp_path, translations=translation, stage_progress=stage,
    )
    cache = _load_stage_progress(
        path, translations=translation, r_editor_enabled=False,
    )
    assert cache is not None and cache.is_partial()
    plan = cache.reaudit_resume_plan()
    assert sorted(plan) == [1]
    assert plan[1]["dropped"] == [dropped[0]]

    # ------------------------------------------------------------------
    # Cached replay: feed the resume plan back into a fresh evaluator run
    # — chunk 1 replays with 0 model calls and the replayed chunk record
    # keeps the exact-schema dropped diagnostic, still outside findings.
    # ------------------------------------------------------------------
    replay_records: List[Dict[str, Any]] = []

    def _on_replay_progress(kind: str, fields: Dict[str, Any]) -> None:
        if kind == "reaudit_chunk_done":
            replay_records.append(dict(fields))

    replay_backend = ScriptedRepairBackend([
        _repair_response([{
            "index": 1, "decision": "repair", "pid": "p00005",
            "repaired_translation": "Исправленный перевод абзаца 5.",
            "reason": "confirmed",
        }]),
    ])
    replay_evaluator = SelectiveRepairEvaluator(
        replay_backend,
        config=SelectiveRepairConfig(reaudit_neighbour_window=2),
        on_progress=_on_replay_progress,
    )
    replay_outcome = replay_evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered, cached_reaudit_chunks=plan,
    )
    assert replay_outcome.reaudit is not None and replay_outcome.reaudit.complete
    reaudit_calls = [
        r for r in replay_backend.requests if "reaudit" in (r.label or "")
    ]
    # chunk 1 replayed with 0 reaudit calls (boundaries match the plan)
    assert len(reaudit_calls) == 0
    replayed = [
        c for r in replay_records
        for c in (r.get("done_chunks") or ())
        if c.get("chunk") == 1 and c.get("dropped")
    ]
    assert replayed, "replayed chunk record lost the dropped field"
    assert replayed[0]["dropped"] == [dropped[0]]
    # the dropped diagnostic stays outside the re-audit findings on replay
    assert [i["id"] for i in replay_outcome.reaudit.issues] == ["p00005"]
