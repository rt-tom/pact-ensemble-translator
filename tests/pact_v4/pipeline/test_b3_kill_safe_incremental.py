"""KILL-SAFE-INCREMENTAL (t_2d16962c) acceptance tests.

The owner decision 2026-08-15: a kill/Ctrl+C at ANY point of the B3 stage
must preserve the accumulated structured results. The single mechanism is
the incremental REWRITE of audit_cache_b3.json after every completed
chunk/batch — the payload carries the identity fields plus an accumulated
``stage_progress`` block (per-stage done/failed slices), bound to a
``partial_resume_hash``. Resume reuses GOOD chunks/batches (0 model calls),
re-runs only the missing ones, and any tamper / coverage gap / foreign
schema is a FULL miss (fail-closed, never a partial replay).

Covered here (ПРИЁМКА):

* cache-level resume plans for the exact acceptance ratios: R 3/5, audit
  2/8, repair 2/4, reaudit 1/3 — the GOOD slices are reusable, the rest
  re-runs;
* the audit->repair junction: a fully-completed audit survives (every GOOD
  chunk replayed, 0 audit calls) while repair starts fresh;
* fail-closed tamper: any mutation of stage_progress (contiguity violation
  even with a recomputed hash, foreign edit class) is a full miss;
* full-run kill-sims: a cache written as a killed process would leave it is
  loaded on resume, GOOD chunks replayed with 0 model calls, the remaining
  chunks re-run, and the chapter completes and is released as audited.

All model calls go through scripted in-memory backends — 0 real calls.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

from pact_v4.phase1.models import canonical_json_hash
from pact_v4.pipeline.b3_audit_repair import B3AuditCache, B3AuditRepairConfig
from pact_v4.runtime.backend_protocol import (
    CompletionRequest,
    CompletionResponse,
)
from tests.pact_v4.pipeline.test_v4_phase12_strict_runner_b3 import (
    _B3MockBackend,
    _ok_response,
    _read_json,
    _run_with_b3,
    _whole_chapter_cfg,
)

# ---------------------------------------------------------------------------
# Helpers: craft a stage_progress cache exactly as the incremental save would
# ---------------------------------------------------------------------------


def _pids(n: int) -> list:
    return [f"p{i:05d}" for i in range(1, n + 1)]


def _translation(n: int) -> dict:
    return {pid: f"Текст абзаца {i}." for i, pid in enumerate(_pids(n), 1)}


def _r_editor_pending_stage() -> dict:
    """The R stage as it is persisted when R is DISABLED in the run that
    wrote the cache (a disabled stage is the only valid persisted
    representation of 'R has not run'; an enabled-R cache always carries a
    partial/complete/incomplete outcome after its first chunk)."""
    return {
        "status": "disabled", "enabled": False, "done_chunks": [],
        "failed_chunks": [], "outcome": None,
    }


def _audit_pending_stage() -> dict:
    return {
        "status": "pending", "done_chunks": [], "failed_chunks": [],
        "chunks": [], "issues": [],
    }


def _repair_pending_stage() -> dict:
    return {
        "status": "pending", "done_batches": [], "committed": {},
        "passed": [], "outcome": None,
    }


def _reaudit_pending_stage() -> dict:
    return {"status": "pending", "done_chunks": [], "issues": []}


def _save_stage_progress(
    tmp_path: Path,
    *,
    translations: Mapping[str, str],
    stage_progress: Mapping[str, Any],
) -> Path:
    """Write an incremental stage_progress cache via the production save()
    path (identity fields are synthetic but self-consistent with load())."""
    path = tmp_path / "audit_cache_b3.json"
    cache = B3AuditCache(path)
    cache.save(
        snapshot_hash="snap",
        translation_hash="trans",
        config_identity="cfg",
        backend_identity_hash="be",
        entity_context_hash=None,
        entity_context_enabled=False,
        translations_repaired=dict(translations),
        stage_progress=stage_progress,
        prompt_version="pv",
        harness_version="hv",
    )
    return path


def _load_stage_progress(
    path: Path,
    *,
    translations: Mapping[str, str],
    r_editor_enabled: bool = True,
):
    return B3AuditCache.load(
        path,
        snapshot_hash="snap",
        translation_hash="trans",
        config_identity="cfg",
        backend_identity_hash="be",
        prompt_version="pv",
        harness_version="hv",
        entity_context_hash=None,
        entity_context_enabled=False,
        r_editor_enabled=r_editor_enabled,
        expected_pids=list(translations),
        current_text=dict(translations),
    )


def _stage_progress_with(
    *,
    r_editor: Mapping[str, Any],
    audit: Mapping[str, Any],
    repair: Mapping[str, Any],
    reaudit: Mapping[str, Any],
) -> dict:
    return {"r_editor": r_editor, "audit": audit, "repair": repair, "reaudit": reaudit}


# ---------------------------------------------------------------------------
# Cache-level kill-sims: the GOOD slices are reusable, the rest re-runs
# ---------------------------------------------------------------------------


def test_kill_safe_cache_r_editor_3of5_resume_plan(tmp_path: Path) -> None:
    """kill-sim R 3/5: stage_progress.r_editor with 3 GOOD chunks of 5 ->
    resume plan reuses exactly those 3 (0 model calls), chunks 4-5 re-run."""
    translations = _translation(5)
    r_editor = {
        "status": "partial", "enabled": True,
        "done_chunks": [1, 2, 3], "failed_chunks": [],
        "outcome": {
            "chunk_size": 1,
            "chunks": [
                {"chunk": 1, "first_pid": "p00001", "last_pid": "p00001",
                 "status": "GOOD", "edits": []},
                {"chunk": 2, "first_pid": "p00002", "last_pid": "p00002",
                 "status": "GOOD", "edits": []},
                {"chunk": 3, "first_pid": "p00003", "last_pid": "p00003",
                 "status": "GOOD", "edits": []},
            ],
        },
    }
    stage = _stage_progress_with(
        r_editor=r_editor,
        audit=_audit_pending_stage(),
        repair=_repair_pending_stage(),
        reaudit=_reaudit_pending_stage(),
    )
    path = _save_stage_progress(tmp_path, translations=translations, stage_progress=stage)
    cache = _load_stage_progress(path, translations=translations, r_editor_enabled=True)
    assert cache is not None and cache.is_partial()
    plan = cache.r_editor_resume_plan()
    assert sorted(plan) == [1, 2, 3]
    # The replayed chunks carry their validated edits (empty here) and the
    # resume contract says only chunks 4-5 hit the model.
    assert cache.audit_resume_plan() == {}


def test_kill_safe_cache_audit_2of8_resume_plan(tmp_path: Path) -> None:
    """kill-sim audit 2/8: stage_progress.audit with 2 GOOD chunks of 8 ->
    resume plan reuses exactly those 2 (0 model calls), chunks 3-8 re-run."""
    translations = _translation(8)
    audit = {
        "status": "partial",
        "done_chunks": [1, 2], "failed_chunks": [],
        "chunks": [
            {"chunk": 1, "first_pid": "p00001", "last_pid": "p00001",
             "pair_count": 1, "context_count": 0, "status": "GOOD",
             "finish_reason": "stop", "reasoning_chars": 0,
             "reasoning_file": "b3_audit_chunk1_raw.txt",
             "issue_count": 0},
            {"chunk": 2, "first_pid": "p00002", "last_pid": "p00002",
             "pair_count": 1, "context_count": 0, "status": "GOOD",
             "finish_reason": "stop", "reasoning_chars": 0,
             "reasoning_file": "b3_audit_chunk2_raw.txt",
             "issue_count": 0},
        ],
        "issues": [],
    }
    stage = _stage_progress_with(
        r_editor=_r_editor_pending_stage(),
        audit=audit,
        repair=_repair_pending_stage(),
        reaudit=_reaudit_pending_stage(),
    )
    path = _save_stage_progress(tmp_path, translations=translations, stage_progress=stage)
    cache = _load_stage_progress(path, translations=translations, r_editor_enabled=False)
    assert cache is not None and cache.is_partial()
    plan = cache.audit_resume_plan()
    assert sorted(plan) == [1, 2]
    assert plan[1]["first_pid"] == "p00001" and plan[1]["issues"] == []
    assert cache.r_editor_resume_plan() == {}


def test_kill_safe_cache_audit_resume_plan_preserves_dropped_count(
    tmp_path: Path,
) -> None:
    """CONTEXT-PID-DROP: the audit resume plan carries the persisted
    ``dropped_count`` (including zero) so a replayed GOOD chunk re-emits the
    exact ``audit_chunk_done dropped_count`` instead of 0 (RV t_7e7cfe6f
    finding 1: the plan used to omit the key -> replay read ``None -> 0``)."""
    translations = _translation(8)
    audit = {
        "status": "partial",
        "done_chunks": [1, 2], "failed_chunks": [],
        "chunks": [
            {"chunk": 1, "first_pid": "p00001", "last_pid": "p00001",
             "pair_count": 1, "context_count": 0, "status": "GOOD",
             "finish_reason": "stop", "reasoning_chars": 0,
             "reasoning_file": "b3_audit_chunk1_raw.txt",
             "issue_count": 0,
             "dropped_count": 3},
            {"chunk": 2, "first_pid": "p00002", "last_pid": "p00002",
             "pair_count": 1, "context_count": 0, "status": "GOOD",
             "finish_reason": "stop", "reasoning_chars": 0,
             "reasoning_file": "b3_audit_chunk2_raw.txt",
             "issue_count": 0,
             "dropped_count": 0},
        ],
        "issues": [],
    }
    stage = _stage_progress_with(
        r_editor=_r_editor_pending_stage(),
        audit=audit,
        repair=_repair_pending_stage(),
        reaudit=_reaudit_pending_stage(),
    )
    path = _save_stage_progress(tmp_path, translations=translations, stage_progress=stage)
    cache = _load_stage_progress(path, translations=translations, r_editor_enabled=False)
    assert cache is not None and cache.is_partial()
    plan = cache.audit_resume_plan()
    assert sorted(plan) == [1, 2]
    # The exact persisted counts ride the plan (zero included) — the
    # evaluator replay reconstructs ChunkMeta(dropped_count=...) from these.
    assert plan[1]["dropped_count"] == 3
    assert plan[2]["dropped_count"] == 0


def test_kill_safe_cache_audit_malformed_dropped_count_full_miss(
    tmp_path: Path,
) -> None:
    """CONTEXT-PID-DROP fail-closed: an audit chunk payload whose persisted
    ``dropped_count`` is negative / non-int is a FULL miss — never a trusted
    replay with a fabricated warning count (RV t_7e7cfe6f finding 1)."""
    translations = _translation(3)
    audit = {
        "status": "partial",
        "done_chunks": [1], "failed_chunks": [],
        "chunks": [
            {"chunk": 1, "first_pid": "p00001", "last_pid": "p00001",
             "pair_count": 1, "context_count": 0, "status": "GOOD",
             "finish_reason": "stop", "reasoning_chars": 0,
             "reasoning_file": "b3_audit_chunk1_raw.txt",
             "dropped_count": -1},
        ],
        "issues": [],
    }
    stage = _stage_progress_with(
        r_editor=_r_editor_pending_stage(),
        audit=audit,
        repair=_repair_pending_stage(),
        reaudit=_reaudit_pending_stage(),
    )
    path = _save_stage_progress(tmp_path, translations=translations, stage_progress=stage)
    assert _load_stage_progress(path, translations=translations) is None

    # Non-int dropped_count is equally malformed (fail-closed, never coerced).
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["stage_progress"]["audit"]["chunks"][0]["dropped_count"] = "3"
    payload["partial_resume_hash"] = canonical_json_hash({
        "r_editor": payload["stage_progress"]["r_editor"],
        "audit": payload["stage_progress"]["audit"],
        "repair": payload["stage_progress"]["repair"],
        "reaudit": payload["stage_progress"]["reaudit"],
    })
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert _load_stage_progress(path, translations=translations) is None


def test_kill_safe_cache_repair_2of4_resume_plan(tmp_path: Path) -> None:
    """kill-sim repair 2/4: stage_progress.repair with 2 GOOD batches of 4 ->
    resume plan reuses those 2 (committed not repeated), batches 3-4 redone."""
    translations = _translation(4)
    finding = lambda i: {  # noqa: E731 — _repair_batches_payload schema
        "index": 1, "pid": f"p{i:05d}", "tier": "TIER_A",
        "category": "addition", "severity": "major", "confidence": "high",
        "source_stage": "fidelity_auditor", "sources": [],
    }
    repair = {
        "status": "partial",
        "done_batches": [1, 2],
        "committed": {"p00001": "исправленный текст 1."},
        "passed": ["p00002"],
        "outcome": {
            "batch_count": 4,
            "batches": [
                {"batch_index": 1, "status": "GOOD",
                 "findings": [finding(1)],
                 "results": [{"index": 1, "decision": "repair",
                              "pid": "p00001",
                              "repaired_translation": "исправленный текст 1.",
                              "reason": "mock"}],
                 "error": "", "warnings": [], "missing_indices": []},
                {"batch_index": 2, "status": "GOOD",
                 "findings": [finding(2)],
                 "results": [{"index": 1, "decision": "pass",
                              "pid": "", "repaired_translation": "",
                              "reason": "verified"}],
                 "error": "", "warnings": [], "missing_indices": []},
            ],
        },
    }
    stage = _stage_progress_with(
        r_editor=_r_editor_pending_stage(),
        audit=_audit_pending_stage(),
        repair=repair,
        reaudit=_reaudit_pending_stage(),
    )
    path = _save_stage_progress(tmp_path, translations=translations, stage_progress=stage)
    cache = _load_stage_progress(path, translations=translations, r_editor_enabled=False)
    assert cache is not None and cache.is_partial()
    plan = cache.repair_resume_plan()
    assert plan is not None and sorted(plan) == [1, 2]
    assert plan[1]["findings_pids"] == ["p00001"]
    assert plan[1]["results"][0]["repaired_translation"] == "исправленный текст 1."
    # The committed pid is NOT re-requested from the model on resume: it
    # lives in the cached batch's results, replayed verbatim.
    assert cache.reaudit_resume_plan() == {}


def test_kill_safe_cache_reaudit_1of3_resume_plan(tmp_path: Path) -> None:
    """kill-sim reaudit 1/3: stage_progress.reaudit with 1 done chunk of 3 ->
    resume plan reuses that chunk, chunks 2-3 re-run."""
    translations = _translation(3)
    reaudit = {
        "status": "partial",
        "done_chunks": [
            {"chunk": 1, "first_pid": "p00001", "last_pid": "p00001",
             "issues": [], "failed": False},
        ],
        "issues": [],
    }
    stage = _stage_progress_with(
        r_editor=_r_editor_pending_stage(),
        audit=_audit_pending_stage(),
        repair=_repair_pending_stage(),
        reaudit=reaudit,
    )
    path = _save_stage_progress(tmp_path, translations=translations, stage_progress=stage)
    cache = _load_stage_progress(path, translations=translations, r_editor_enabled=False)
    assert cache is not None and cache.is_partial()
    plan = cache.reaudit_resume_plan()
    assert sorted(plan) == [1]
    assert plan[1]["first_pid"] == "p00001"


def test_kill_safe_cache_reaudit_resume_plan_preserves_dropped(
    tmp_path: Path,
) -> None:
    """CONTEXT-PID-DROP: the reaudit resume plan carries the persisted
    ``dropped`` issue objects so a replayed re-audit chunk keeps its
    journaled context/foreign diagnostics (RV t_7e7cfe6f finding 2: the plan
    used to strip the field)."""
    translations = _translation(3)
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
    reaudit = {
        "status": "partial",
        "done_chunks": [
            {"chunk": 1, "first_pid": "p00001", "last_pid": "p00001",
             "issues": [], "dropped": [dict(dropped_issue)],
             "failed": False},
        ],
        "issues": [],
    }
    stage = _stage_progress_with(
        r_editor=_r_editor_pending_stage(),
        audit=_audit_pending_stage(),
        repair=_repair_pending_stage(),
        reaudit=reaudit,
    )
    path = _save_stage_progress(tmp_path, translations=translations, stage_progress=stage)
    cache = _load_stage_progress(path, translations=translations, r_editor_enabled=False)
    assert cache is not None and cache.is_partial()
    plan = cache.reaudit_resume_plan()
    assert sorted(plan) == [1]
    assert plan[1]["dropped"] == [dropped_issue]


def test_kill_safe_cache_reaudit_malformed_dropped_full_miss(tmp_path: Path) -> None:
    """CONTEXT-PID-DROP fail-closed: a reaudit done_chunks record whose
    ``dropped`` field is not a list / not well-formed issue objects is a FULL
    miss — never a trusted replay that silently loses the diagnostics (RV
    t_7e7cfe6f finding 2)."""
    translations = _translation(3)
    reaudit = {
        "status": "partial",
        "done_chunks": [
            {"chunk": 1, "first_pid": "p00001", "last_pid": "p00001",
             "issues": [], "dropped": "oops-not-a-list", "failed": False},
        ],
        "issues": [],
    }
    stage = _stage_progress_with(
        r_editor=_r_editor_pending_stage(),
        audit=_audit_pending_stage(),
        repair=_repair_pending_stage(),
        reaudit=reaudit,
    )
    path = _save_stage_progress(tmp_path, translations=translations, stage_progress=stage)
    assert _load_stage_progress(path, translations=translations) is None

    # A dropped element that is not a well-formed issue object is equally a
    # full miss (never coerced/filtered into a lossy replay).
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["stage_progress"]["reaudit"]["done_chunks"][0]["dropped"] = ["junk"]
    payload["partial_resume_hash"] = canonical_json_hash({
        "r_editor": payload["stage_progress"]["r_editor"],
        "audit": payload["stage_progress"]["audit"],
        "repair": payload["stage_progress"]["repair"],
        "reaudit": payload["stage_progress"]["reaudit"],
    })
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert _load_stage_progress(path, translations=translations) is None


def _reaudit_stage_with_dropped(dropped: Any) -> dict:
    """One done reaudit chunk (chunk 1 over p00001) carrying ``dropped``."""
    return {
        "status": "partial",
        "done_chunks": [
            {"chunk": 1, "first_pid": "p00001", "last_pid": "p00001",
             "issues": [], "dropped": dropped, "failed": False},
        ],
        "issues": [],
    }


def _reaudit_dropped_with(**overrides: Any) -> dict:
    """A well-formed persisted dropped issue object (RV2 t_61af1bb2)."""
    issue = {
        "id": "p00099", "category": "addition", "severity": "major",
        "confidence": "high", "note": "foreign pid", "excerpt": "text",
        "_debug": {
            "chunk": 1,
            "reasoning_file": "b3_repair_reaudit_chunk1_reasoning.txt",
        },
    }
    issue.update(overrides)
    return issue


def _reaudit_cache_with_dropped(
    tmp_path: Path,
    dropped: Any,
) -> Path:
    """Persist a reaudit stage_progress cache carrying the given dropped
    payload (valid or not) with a recomputed canonical hash, so the ONLY
    rejection path is the dropped-object validation itself. The R stage is
    DISABLED in the persisted stage (the pending-stage fixture), so loads
    must pass ``r_editor_enabled=False`` — see ``_load_reaudit_cache``."""
    translations = _translation(3)
    stage = _stage_progress_with(
        r_editor=_r_editor_pending_stage(),
        audit=_audit_pending_stage(),
        repair=_repair_pending_stage(),
        reaudit=_reaudit_stage_with_dropped(dropped),
    )
    return _save_stage_progress(
        tmp_path, translations=translations, stage_progress=stage
    )


def _load_reaudit_cache(path: Path, translations: Mapping[str, str]):
    """Load a reaudit stage_progress cache with R DISABLED (the pending-R
    fixture the dropped tests persist), so only the dropped-object
    validation is exercised."""
    return _load_stage_progress(
        path, translations=translations, r_editor_enabled=False
    )


def test_kill_safe_cache_reaudit_dropped_missing_keys_full_miss(
    tmp_path: Path,
) -> None:
    """RV2 t_61af1bb2 (reviewer reproducer): a dropped issue object that
    carries ONLY an id (``[{\"id\": \"p99999\"}]``) is NOT a well-formed
    persisted issue — missing category/severity/confidence/note/excerpt/
    _debug must be a FULL cache miss before any resume plan is built, never
    a trusted replay of the malformed object."""
    translations = _translation(3)
    path = _reaudit_cache_with_dropped(
        tmp_path, [{"id": "p99999"}],
    )
    assert _load_reaudit_cache(path, translations) is None


def test_kill_safe_cache_reaudit_dropped_invalid_vocab_full_miss(
    tmp_path: Path,
) -> None:
    """RV2 t_61af1bb2: invalid category/severity/confidence in a persisted
    dropped issue is a full miss (same vocab contract as cached audit
    issues)."""
    translations = _translation(3)
    for overrides in (
        {"category": "bogus_category"},
        {"severity": "catastrophic"},
        {"confidence": "certain"},
    ):
        path = _reaudit_cache_with_dropped(
            tmp_path, [_reaudit_dropped_with(**overrides)],
        )
        assert _load_reaudit_cache(path, translations) is None


def test_kill_safe_cache_reaudit_dropped_empty_note_excerpt_full_miss(
    tmp_path: Path,
) -> None:
    """RV2 t_61af1bb2: an empty/missing note or excerpt in a persisted
    dropped issue is a full miss (never coerced into a lossy replay)."""
    translations = _translation(3)
    for overrides in (
        {"note": ""},
        {"note": "   "},
        {"excerpt": ""},
        {"excerpt": "   "},
        {"note": None},
    ):
        path = _reaudit_cache_with_dropped(
            tmp_path, [_reaudit_dropped_with(**overrides)],
        )
        assert _load_reaudit_cache(path, translations) is None


def test_kill_safe_cache_reaudit_dropped_malformed_debug_full_miss(
    tmp_path: Path,
) -> None:
    """RV2 t_61af1bb2: a malformed ``_debug`` (missing, non-object, wrong
    chunk, non-string reasoning_file) in a persisted dropped issue is a full
    miss — the harness attribution must match the journaling chunk exactly."""
    translations = _translation(3)
    for overrides in (
        {"_debug": None},
        {"_debug": "chunk1"},
        {"_debug": {"chunk": "1", "reasoning_file": "r.txt"}},
        {"_debug": {"chunk": 1, "reasoning_file": None}},
        {"_debug": {"chunk": 1}},
        {"_debug": {"chunk": 2, "reasoning_file": "r.txt"}},
        {"_debug": {"chunk": 1, "reasoning_file": "r.txt", "extra": 1}},
    ):
        path = _reaudit_cache_with_dropped(
            tmp_path, [_reaudit_dropped_with(**overrides)],
        )
        assert _load_reaudit_cache(path, translations) is None


def test_kill_safe_cache_reaudit_dropped_in_chunk_span_full_miss(
    tmp_path: Path,
) -> None:
    """RV2 t_61af1bb2 safe PID equivalent: a dropped issue whose id lies
    INSIDE the journaling record's own pid span (first_pid..last_pid) is an
    impossible drop — a chunk-owned pid would be a valid issue, never a
    dropped one — so the cache is tampered and a full miss. Context/foreign
    ids outside the span (or absent from the map) stay valid."""
    translations = _translation(3)
    # p00001 IS the record's own pid -> inside the span -> impossible drop.
    path = _reaudit_cache_with_dropped(
        tmp_path, [_reaudit_dropped_with(id="p00001")],
    )
    assert _load_reaudit_cache(path, translations) is None


def test_kill_safe_cache_reaudit_dropped_extra_key_full_miss(
    tmp_path: Path,
) -> None:
    """RV3 t_c9eb65d4 fail-closed counterpart: a persisted dropped issue
    object carrying an unknown EXTRA top-level key (a model response field
    that survived journaling) is a FULL miss — the exact-_ISSUE_KEYS
    contract is never weakened, so the fix must happen at journaling time
    (retain canonical fields), never by tolerating the foreign key on
    reload."""
    translations = _translation(3)
    path = _reaudit_cache_with_dropped(
        tmp_path, [_reaudit_dropped_with(extra="model-noise-field")],
    )
    assert _load_reaudit_cache(path, translations) is None


def test_kill_safe_cache_reaudit_dropped_valid_replay(tmp_path: Path) -> None:
    """RV2 t_61af1bb2: a well-formed persisted dropped issue object (exact
    _ISSUE_KEYS, valid vocab, harness _debug matching the journaling chunk,
    id outside the chunk span) loads and rides reaudit_resume_plan exactly —
    valid context/foreign dropped diagnostics are preserved, not rejected."""
    translations = _translation(3)
    dropped_issue = _reaudit_dropped_with()
    path = _reaudit_cache_with_dropped(tmp_path, [dict(dropped_issue)])
    cache = _load_reaudit_cache(path, translations)
    assert cache is not None and cache.is_partial()
    plan = cache.reaudit_resume_plan()
    assert sorted(plan) == [1]
    assert plan[1]["dropped"] == [dropped_issue]


def test_kill_safe_cache_reaudit_failed_marker_excluded_from_plan(
    tmp_path: Path,
) -> None:
    """CONTEXT-PID-DROP (RV5 t_f82ed9ad): a done reaudit chunk record
    marked ``failed: True`` loads (the marker is a valid bool) but is NEVER
    replayable — reaudit_resume_plan excludes it, so the next run re-runs
    the chunk fail-closed (debt/diagnostic preserved) instead of replaying
    it as complete with 0 model calls."""
    translations = _translation(3)
    reaudit = {
        "status": "failed",
        "done_chunks": [
            {"chunk": 1, "first_pid": "p00001", "last_pid": "p00001",
             "issues": [], "dropped": [], "failed": True},
        ],
        "issues": [],
    }
    stage = _stage_progress_with(
        r_editor=_r_editor_pending_stage(),
        audit=_audit_pending_stage(),
        repair=_repair_pending_stage(),
        reaudit=reaudit,
    )
    path = _save_stage_progress(tmp_path, translations=translations, stage_progress=stage)
    cache = _load_stage_progress(path, translations=translations, r_editor_enabled=False)
    assert cache is not None and cache.is_partial()
    plan = cache.reaudit_resume_plan()
    assert plan == {}
    assert 1 not in plan


def test_kill_safe_cache_reaudit_failed_marker_missing_full_miss(
    tmp_path: Path,
) -> None:
    """CONTEXT-PID-DROP (RV5 t_f82ed9ad): a done reaudit chunk record
    WITHOUT the ``failed`` bool marker (a cache written before the marker
    existed, or a tampered one) is a FULL miss — never a trusted replay
    that could silently upgrade a failed chunk to complete with 0 model
    calls."""
    translations = _translation(3)
    reaudit = {
        "status": "partial",
        "done_chunks": [
            {"chunk": 1, "first_pid": "p00001", "last_pid": "p00001",
             "issues": [], "dropped": []},
        ],
        "issues": [],
    }
    stage = _stage_progress_with(
        r_editor=_r_editor_pending_stage(),
        audit=_audit_pending_stage(),
        repair=_repair_pending_stage(),
        reaudit=reaudit,
    )
    path = _save_stage_progress(tmp_path, translations=translations, stage_progress=stage)
    assert _load_stage_progress(
        path, translations=translations, r_editor_enabled=False,
    ) is None


def test_kill_safe_cache_reaudit_failed_marker_nonbool_full_miss(
    tmp_path: Path,
) -> None:
    """CONTEXT-PID-DROP (RV5 t_f82ed9ad): a done reaudit chunk record
    whose ``failed`` marker is NOT a bool (e.g. a string) is a FULL miss —
    the marker is part of the fail-closed contract and any foreign value is
    rejected."""
    translations = _translation(3)
    reaudit = {
        "status": "partial",
        "done_chunks": [
            {"chunk": 1, "first_pid": "p00001", "last_pid": "p00001",
             "issues": [], "dropped": [], "failed": "yes"},
        ],
        "issues": [],
    }
    stage = _stage_progress_with(
        r_editor=_r_editor_pending_stage(),
        audit=_audit_pending_stage(),
        repair=_repair_pending_stage(),
        reaudit=reaudit,
    )
    path = _save_stage_progress(tmp_path, translations=translations, stage_progress=stage)
    assert _load_stage_progress(
        path, translations=translations, r_editor_enabled=False,
    ) is None


def test_kill_safe_cache_audit_complete_repair_pending_junction(
    tmp_path: Path,
) -> None:
    """kill at the audit->repair junction: the audit completed (all 8 GOOD)
    but repair never started — resume reuses EVERY audit chunk (0 calls) and
    repair begins fresh (repair_resume_plan is None)."""
    translations = _translation(8)
    chunks = [
        {"chunk": i, "first_pid": f"p{i:05d}", "last_pid": f"p{i:05d}",
         "pair_count": 1, "context_count": 0, "status": "GOOD",
         "finish_reason": "stop", "reasoning_chars": 0,
         "reasoning_file": f"b3_audit_chunk{i}_raw.txt", "issue_count": 0}
        for i in range(1, 9)
    ]
    audit = {
        "status": "complete",
        "done_chunks": list(range(1, 9)), "failed_chunks": [],
        "chunks": chunks, "issues": [],
    }
    stage = _stage_progress_with(
        r_editor=_r_editor_pending_stage(),
        audit=audit,
        repair=_repair_pending_stage(),
        reaudit=_reaudit_pending_stage(),
    )
    path = _save_stage_progress(tmp_path, translations=translations, stage_progress=stage)
    cache = _load_stage_progress(path, translations=translations, r_editor_enabled=False)
    assert cache is not None and cache.is_partial()
    audit_plan = cache.audit_resume_plan()
    assert sorted(audit_plan) == list(range(1, 9))
    # Repair never started: no batches to reuse -> it re-runs from scratch.
    assert cache.repair_resume_plan() is None
    assert cache.reaudit_resume_plan() == {}


# ---------------------------------------------------------------------------
# Fail-closed tamper: any stage_progress mutation is a full miss
# ---------------------------------------------------------------------------


def test_kill_safe_cache_tamper_done_chunks_full_miss(tmp_path: Path) -> None:
    """tamper stage_progress -> полный miss: moving a done_chunk breaks the
    contiguous 1..N coverage (a kill between chunks must never look
    complete) — the cache is rejected even with a recomputed hash."""
    translations = _translation(5)
    r_editor = {
        "status": "partial", "enabled": True,
        "done_chunks": [1, 2, 3], "failed_chunks": [],
        "outcome": {
            "chunk_size": 1,
            "chunks": [
                {"chunk": 1, "first_pid": "p00001", "last_pid": "p00001",
                 "status": "GOOD", "edits": []},
                {"chunk": 2, "first_pid": "p00002", "last_pid": "p00002",
                 "status": "GOOD", "edits": []},
                {"chunk": 3, "first_pid": "p00003", "last_pid": "p00003",
                 "status": "GOOD", "edits": []},
            ],
        },
    }
    stage = _stage_progress_with(
        r_editor=r_editor,
        audit=_audit_pending_stage(),
        repair=_repair_pending_stage(),
        reaudit=_reaudit_pending_stage(),
    )
    path = _save_stage_progress(tmp_path, translations=translations, stage_progress=stage)

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["stage_progress"]["r_editor"]["done_chunks"] = [1, 2, 4]
    payload["partial_resume_hash"] = canonical_json_hash({
        "r_editor": payload["stage_progress"]["r_editor"],
        "audit": payload["stage_progress"]["audit"],
        "repair": payload["stage_progress"]["repair"],
        "reaudit": payload["stage_progress"]["reaudit"],
    })
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    assert _load_stage_progress(path, translations=translations) is None


def test_kill_safe_cache_tamper_edit_class_full_miss(tmp_path: Path) -> None:
    """tamper stage_progress -> полный miss: a cached R edit with a foreign
    class (even with a recomputed hash binding the payload) is rejected —
    never replayed, never coerced."""
    translations = _translation(3)
    r_editor = {
        "status": "partial", "enabled": True,
        "done_chunks": [1], "failed_chunks": [],
        "outcome": {
            "chunk_size": 1,
            "chunks": [
                {"chunk": 1, "first_pid": "p00001", "last_pid": "p00001",
                 "status": "GOOD",
                 "edits": [{"pid": "p00001", "original": "Текст абзаца 1.",
                            "rewritten": "Текст абзаца 1!",
                            "reason": "mock", "class": "typo"}]},
            ],
        },
    }
    stage = _stage_progress_with(
        r_editor=r_editor,
        audit=_audit_pending_stage(),
        repair=_repair_pending_stage(),
        reaudit=_reaudit_pending_stage(),
    )
    path = _save_stage_progress(tmp_path, translations=translations, stage_progress=stage)

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["stage_progress"]["r_editor"]["outcome"]["chunks"][0]["edits"][0]["class"] = "bogus-class"
    payload["partial_resume_hash"] = canonical_json_hash({
        "r_editor": payload["stage_progress"]["r_editor"],
        "audit": payload["stage_progress"]["audit"],
        "repair": payload["stage_progress"]["repair"],
        "reaudit": payload["stage_progress"]["reaudit"],
    })
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    assert _load_stage_progress(path, translations=translations) is None


# ---------------------------------------------------------------------------
# FIX RV2 (t_d996bbf7): strict replay-payload validation — a recomputed hash
# must NOT smuggle unauthorized repair/reaudit/R content into the evaluators
# ---------------------------------------------------------------------------


def _tamper_and_load(
    path: Path,
    translations: Mapping[str, str],
    mutate,
    r_editor_enabled: bool = False,
):
    """Tamper stage_progress, RECOMPUTE the partial_resume_hash (so only the
    strict schema/boundary/text validation can catch the mutation), and load.
    Returns the load result (None == full miss, the fail-closed contract)."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload["stage_progress"])
    payload["partial_resume_hash"] = canonical_json_hash({
        "r_editor": payload["stage_progress"]["r_editor"],
        "audit": payload["stage_progress"]["audit"],
        "repair": payload["stage_progress"]["repair"],
        "reaudit": payload["stage_progress"]["reaudit"],
    })
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return _load_stage_progress(
        path, translations=translations, r_editor_enabled=r_editor_enabled
    )


def _repair_finding(pid: str) -> dict:
    """A finding record in the exact _repair_batches_payload schema."""
    return {
        "index": 1, "pid": pid, "tier": "TIER_A", "category": "addition",
        "severity": "major", "confidence": "high",
        "source_stage": "fidelity_auditor", "sources": [],
    }


def _repair_stage_with_batch(
    *,
    finding_pid: str,
    result: Mapping[str, Any],
    committed: Mapping[str, str],
    passed: Sequence[str] = (),
) -> dict:
    """A production-schema partial repair stage with ONE GOOD batch."""
    return {
        "status": "partial",
        "done_batches": [1],
        "committed": dict(committed),
        "passed": list(passed),
        "outcome": {
            "batch_count": 1,
            "batches": [{
                "batch_index": 1, "status": "GOOD",
                "findings": [_repair_finding(finding_pid)],
                "results": [dict(result)],
                "error": "", "warnings": [], "missing_indices": [],
            }],
        },
    }


def test_kill_safe_tamper_repair_result_text_full_miss(tmp_path: Path) -> None:
    """RV2 HIGH probe: a cached GOOD batch whose repair result text was
    replaced with UNAUTHORIZED is a FULL miss even with a recomputed hash —
    the stored committed map no longer matches what replaying the batches
    would commit, so the unauthorized text can never reach the evaluator."""
    translations = _translation(3)
    repair = _repair_stage_with_batch(
        finding_pid="p00001",
        result={"index": 1, "decision": "repair", "pid": "p00001",
                "repaired_translation": "исправленный текст 1.",
                "reason": "mock"},
        committed={"p00001": "исправленный текст 1."},
    )
    stage = _stage_progress_with(
        r_editor=_r_editor_pending_stage(), audit=_audit_pending_stage(),
        repair=repair, reaudit=_reaudit_pending_stage(),
    )
    path = _save_stage_progress(tmp_path, translations=translations, stage_progress=stage)
    assert _load_stage_progress(
        path, translations=translations, r_editor_enabled=False
    ) is not None  # the untouched payload is valid

    # The reviewer's deterministic probe: replace ONLY the result text —
    # the evaluator would otherwise commit {'p00001': 'UNAUTHORIZED'} with
    # 0 model calls. committed is untouched, so the recomputed-hash payload
    # is caught by the committed<->results binding.
    assert _tamper_and_load(path, translations, lambda sp: sp["repair"]
        ["outcome"]["batches"][0]["results"][0]
        .__setitem__("repaired_translation", "UNAUTHORIZED")) is None


def test_kill_safe_tamper_repair_result_truncated_full_miss(tmp_path: Path) -> None:
    """RV2: even when the tamperer ALSO rewrites committed to match (and
    recomputes the hash), a repair text that preserves <40% of the current
    text is rejected by the same truncation gate the fresh parse_repair_batch
    applies — a cached batch can never contain a truncated repair."""
    translations = _translation(3)
    repair = _repair_stage_with_batch(
        finding_pid="p00001",
        result={"index": 1, "decision": "repair", "pid": "p00001",
                "repaired_translation": "исправленный текст 1.",
                "reason": "mock"},
        committed={"p00001": "исправленный текст 1."},
    )
    stage = _stage_progress_with(
        r_editor=_r_editor_pending_stage(), audit=_audit_pending_stage(),
        repair=repair, reaudit=_reaudit_pending_stage(),
    )
    path = _save_stage_progress(tmp_path, translations=translations, stage_progress=stage)

    def mutate(sp):
        sp["repair"]["outcome"]["batches"][0]["results"][0][
            "repaired_translation"] = "X"
        sp["repair"]["committed"]["p00001"] = "X"

    assert _tamper_and_load(path, translations, mutate) is None


def test_kill_safe_tamper_repair_result_pid_full_miss(tmp_path: Path) -> None:
    """RV2: a repair result naming a pid other than its finding's pid breaks
    the index/PID contract (same rule as the fresh path) — full miss."""
    translations = _translation(3)
    repair = _repair_stage_with_batch(
        finding_pid="p00001",
        result={"index": 1, "decision": "repair", "pid": "p00001",
                "repaired_translation": "исправленный текст 1.",
                "reason": "mock"},
        committed={"p00001": "исправленный текст 1."},
    )
    stage = _stage_progress_with(
        r_editor=_r_editor_pending_stage(), audit=_audit_pending_stage(),
        repair=repair, reaudit=_reaudit_pending_stage(),
    )
    path = _save_stage_progress(tmp_path, translations=translations, stage_progress=stage)

    def mutate(sp):
        sp["repair"]["outcome"]["batches"][0]["results"][0]["pid"] = "p00099"

    assert _tamper_and_load(path, translations, mutate) is None


def test_kill_safe_tamper_repair_result_decision_full_miss(tmp_path: Path) -> None:
    """RV2: a cached result with a decision outside {pass, repair} is a full
    miss — the replay path would otherwise commit it verbatim."""
    translations = _translation(3)
    repair = _repair_stage_with_batch(
        finding_pid="p00001",
        result={"index": 1, "decision": "repair", "pid": "p00001",
                "repaired_translation": "исправленный текст 1.",
                "reason": "mock"},
        committed={"p00001": "исправленный текст 1."},
    )
    stage = _stage_progress_with(
        r_editor=_r_editor_pending_stage(), audit=_audit_pending_stage(),
        repair=repair, reaudit=_reaudit_pending_stage(),
    )
    path = _save_stage_progress(tmp_path, translations=translations, stage_progress=stage)

    def mutate(sp):
        sp["repair"]["outcome"]["batches"][0]["results"][0][
            "decision"] = "approve"

    assert _tamper_and_load(path, translations, mutate) is None


def test_kill_safe_tamper_reaudit_issue_schema_full_miss(tmp_path: Path) -> None:
    """RV2 HIGH: a malformed cached reaudit issue (invalid category) is a
    full miss — reaudit_resume_plan()/_run_reaudit() copy cached issues
    verbatim with 0 model calls, so the same strict validator as a fresh
    re-audit chunk applies at load time."""
    translations = _translation(3)
    issue = {"id": "p00001", "category": "addition",
             "severity": "major", "confidence": "high"}
    reaudit = {
        "status": "partial",
        "done_chunks": [
            {"chunk": 1, "first_pid": "p00001", "last_pid": "p00001",
             "failed": False, "issues": [dict(issue)]},
        ],
        "issues": [dict(issue)],
    }
    stage = _stage_progress_with(
        r_editor=_r_editor_pending_stage(), audit=_audit_pending_stage(),
        repair=_repair_pending_stage(), reaudit=reaudit,
    )
    path = _save_stage_progress(tmp_path, translations=translations, stage_progress=stage)
    assert _load_stage_progress(
        path, translations=translations, r_editor_enabled=False
    ) is not None

    # Tamper the category in BOTH the per-chunk list and the aggregate (and
    # recompute the hash) so only the issue-schema validation can catch it.
    def mutate(sp):
        for issues in (sp["reaudit"]["done_chunks"][0]["issues"],
                       sp["reaudit"]["issues"]):
            issues[0]["category"] = "bogus-category"

    assert _tamper_and_load(path, translations, mutate) is None


def test_kill_safe_tamper_reaudit_issue_out_of_span_full_miss(tmp_path: Path) -> None:
    """RV2 HIGH: a cached reaudit issue whose id is NOT inside its chunk's pid
    span is a full miss — the replayed chunk would otherwise publish
    out-of-scope evidence with 0 model calls."""
    translations = _translation(3)
    issue = {"id": "p00001", "category": "addition",
             "severity": "major", "confidence": "high"}
    reaudit = {
        "status": "partial",
        "done_chunks": [
            {"chunk": 1, "first_pid": "p00001", "last_pid": "p00001",
             "failed": False, "issues": [dict(issue)]},
        ],
        "issues": [dict(issue)],
    }
    stage = _stage_progress_with(
        r_editor=_r_editor_pending_stage(), audit=_audit_pending_stage(),
        repair=_repair_pending_stage(), reaudit=reaudit,
    )
    path = _save_stage_progress(tmp_path, translations=translations, stage_progress=stage)

    # Move the issue onto p00002 in BOTH lists (aggregate stays consistent)
    # — the per-chunk pid-span binding must reject it.
    def mutate(sp):
        for issues in (sp["reaudit"]["done_chunks"][0]["issues"],
                       sp["reaudit"]["issues"]):
            issues[0]["id"] = "p00002"

    assert _tamper_and_load(path, translations, mutate) is None


def test_kill_safe_tamper_r_edit_original_mismatch_full_miss(
    tmp_path: Path,
) -> None:
    """RV2 MEDIUM: a cached R edit whose ``original`` is not a verbatim
    substring of the CURRENT text of its pid is a full miss — without the
    current-text binding a recomputed hash could replay an edit the model
    never saw."""
    translations = _translation(3)
    r_editor = {
        "status": "partial", "enabled": True,
        "done_chunks": [1], "failed_chunks": [],
        "outcome": {
            "chunk_size": 1,
            "chunks": [
                {"chunk": 1, "first_pid": "p00001", "last_pid": "p00001",
                 "status": "GOOD",
                 "edits": [{"pid": "p00001", "original": "Текст абзаца 1.",
                            "rewritten": "Текст абзаца 1!",
                            "reason": "mock", "class": "typo"}]},
            ],
        },
    }
    stage = _stage_progress_with(
        r_editor=r_editor, audit=_audit_pending_stage(),
        repair=_repair_pending_stage(), reaudit=_reaudit_pending_stage(),
    )
    path = _save_stage_progress(tmp_path, translations=translations, stage_progress=stage)
    assert _load_stage_progress(
        path, translations=translations, r_editor_enabled=True
    ) is not None

    def mutate(sp):
        sp["r_editor"]["outcome"]["chunks"][0]["edits"][0][
            "original"] = "Текст абзаца 999."

    assert _tamper_and_load(path, translations, mutate) is None


def test_kill_safe_tamper_unknown_schema_key_full_miss(tmp_path: Path) -> None:
    """RV2 MEDIUM: an extra/unknown key in ANY stage record (here: a repair
    batch) is a foreign-schema payload — full miss, never silently ignored."""
    translations = _translation(3)
    repair = _repair_stage_with_batch(
        finding_pid="p00001",
        result={"index": 1, "decision": "repair", "pid": "p00001",
                "repaired_translation": "исправленный текст 1.",
                "reason": "mock"},
        committed={"p00001": "исправленный текст 1."},
    )
    stage = _stage_progress_with(
        r_editor=_r_editor_pending_stage(), audit=_audit_pending_stage(),
        repair=repair, reaudit=_reaudit_pending_stage(),
    )
    path = _save_stage_progress(tmp_path, translations=translations, stage_progress=stage)

    def mutate(sp):
        sp["repair"]["outcome"]["batches"][0]["evil_key"] = "smuggle"

    assert _tamper_and_load(path, translations, mutate) is None


def _craft_kill_state(
    cache_path: Path,
    *,
    base: Mapping[str, Any],
    stage_progress: Mapping[str, Any],
) -> None:
    """Rewrite the cache as the incremental save would after a kill: identity
    + translations_repaired from a real run, audit_complete=False, the
    accumulated stage_progress block."""
    B3AuditCache(cache_path).save(
        snapshot_hash=base["snapshot_hash"],
        translation_hash=base["translation_hash"],
        config_identity=base["config_identity"],
        backend_identity_hash=base["backend_identity_hash"],
        entity_context_hash=base["entity_context_hash"],
        entity_context_enabled=base["entity_context_enabled"],
        translations_repaired=base["translations_repaired"],
        stage_progress=stage_progress,
        prompt_version=base["prompt_version"],
        harness_version=base["harness_version"],
    )


def test_b3_kill_safe_incremental_audit_2of8_resume_reuses_good(
    tmp_path: Path,
) -> None:
    """kill-sim audit 2/8 through the full run: a cache left by a process
    killed after 2 of 8 audit chunks resumes with 2 GOOD chunks replayed
    (0 audit calls for them), chunks 3-8 re-run, chapter released."""
    cfg = _whole_chapter_cfg(tmp_path)
    override = B3AuditRepairConfig(
        entity_context_enabled=False,
        russian_editor_enabled=False,
        max_input_tokens=1,  # 8 single-pair audit chunks
        audit_transport_max_retries=0,
        audit_transport_base_delay_seconds=0,
    )
    # First run establishes identity + the real 8-chunk payload.
    first = _run_with_b3(
        cfg,
        _B3MockBackend(audit_issues=[], repair_results=[], reaudit_issues=[]),
        config_override=override,
    )
    assert first.step8["released_as_audited"] is True
    cache_path = cfg.out_dir / "audit_cache_b3.json"
    base = _read_json(cache_path)
    assert len(base["chunks"]) == 8

    # Simulate the kill: only chunks 1-2 of the audit completed.
    stage = _stage_progress_with(
        r_editor={"status": "disabled", "enabled": False, "done_chunks": [],
                  "failed_chunks": [], "outcome": None},
        audit={"status": "partial", "done_chunks": [1, 2], "failed_chunks": [],
               "chunks": base["chunks"][:2], "issues": []},
        repair=_repair_pending_stage(),
        reaudit=_reaudit_pending_stage(),
    )
    _craft_kill_state(cache_path, base=base, stage_progress=stage)

    resume = _B3MockBackend(audit_issues=[], repair_results=[], reaudit_issues=[])
    second = _run_with_b3(cfg, resume, config_override=override)
    assert second.step6["partial_resume"] is True
    # 2 GOOD chunks replayed (0 calls), chunks 3-8 re-run.
    assert resume.audit_calls() == 6
    assert second.step8["released_as_audited"] is True
    # The completed run rewrites the cache as the legacy full payload.
    final = _read_json(cache_path)
    assert final["audit_complete"] is True
    assert "stage_progress" not in final


def test_b3_kill_safe_incremental_r_editor_3of8_resume_reuses_good(
    tmp_path: Path,
) -> None:
    """kill-sim R 3/8 through the full run: a cache left by a process killed
    after 3 of 8 R chunks resumes with 3 GOOD R chunks replayed (0 R calls
    for them), chunks 4-8 re-run, audit re-runs fresh, chapter released."""
    cfg = _whole_chapter_cfg(tmp_path)
    override = B3AuditRepairConfig(
        entity_context_enabled=False,
        russian_editor_enabled=True,
        russian_editor_chunk_size=1,  # 8 single-pid R chunks
        russian_editor_overlap_pairs=0,
        russian_editor_retry_max_retries=0,
        russian_editor_retry_base_delay_seconds=0,
        max_input_tokens=1,  # 8 single-pair audit chunks
        audit_transport_max_retries=0,
        audit_transport_base_delay_seconds=0,
    )
    first = _run_with_b3(
        cfg,
        _B3MockBackend(
            r_editor_edits=[("p00001", "typo", " — исправлено")],
            audit_issues=[], repair_results=[], reaudit_issues=[],
        ),
        config_override=override,
    )
    assert first.step8["released_as_audited"] is True
    cache_path = cfg.out_dir / "audit_cache_b3.json"
    base = _read_json(cache_path)
    r_chunks = base["r_editor"]["outcome"]["chunks"]
    assert len(r_chunks) == 8

    # Simulate the kill: only R chunks 1-3 completed.
    stage = _stage_progress_with(
        r_editor={"status": "partial", "enabled": True,
                  "done_chunks": [1, 2, 3], "failed_chunks": [],
                  "outcome": {"chunk_size": 1, "chunks": r_chunks[:3]}},
        audit=_audit_pending_stage(),
        repair=_repair_pending_stage(),
        reaudit=_reaudit_pending_stage(),
    )
    _craft_kill_state(cache_path, base=base, stage_progress=stage)

    resume = _B3MockBackend(
        r_editor_edits=[("p00001", "typo", " — исправлено")],
        audit_issues=[], repair_results=[], reaudit_issues=[],
    )
    second = _run_with_b3(cfg, resume, config_override=override)
    # 3 GOOD R chunks replayed (0 calls), chunks 4-8 re-run (5 calls);
    # the audit was not started at kill time -> all 8 chunks fresh.
    assert resume.r_editor_calls() == 5
    assert resume.audit_calls() == 8
    assert second.step8["released_as_audited"] is True
    # The replayed chunk 1 edit is applied to the raw map.
    edited = _read_json(cfg.out_dir / "translations_edited.json")
    assert edited["translations"]["p00001"] == "Перевод номер1 номер1 — исправлено"


class _ScopedAuditBackend(_B3MockBackend):
    """B3 mock that emits the canned audit issues ONLY for the chunk that
    actually owns the issue's pid (parsed from the request's AUDIT_PAIRS).
    With max_input_tokens=1 the 8-pair chapter is 8 single-pair chunks, and
    a p00001 issue returned for chunks 2-8 would be out-of-scope evidence
    (the audit validator rejects a pid outside the chunk's own span)."""

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        label = request.label or ""
        if "qwen_chapter_audit" in label and self._audit_issues:
            prompt = request.messages[0].content
            # Scope to the AUDIT_PAIRS block only — a pid present in the
            # CONTEXT_ONLY overlap of a neighbouring chunk is NOT audited by
            # that chunk, and an issue for it would be out-of-scope evidence.
            # Split on the block HEADER ("AUDIT_PAIRS (chunk X of Y):" or
            # "AUDIT_PAIRS:" for the single-chunk form), not the
            # instructions' mention ("Audit ONLY AUDIT_PAIRS.").
            audit_section = prompt.split("AUDIT_PAIRS (")[-1]
            if audit_section == prompt.split("AUDIT_PAIRS (")[0]:
                audit_section = prompt.split("AUDIT_PAIRS:", 1)[-1]
            scoped = [
                dict(issue) for issue in self._audit_issues
                if f'id="{issue["id"]}"' in audit_section
            ]
            return _ok_response({"issues": scoped})
        return super().complete(request)


def test_b3_kill_safe_incremental_audit_to_repair_junction(tmp_path: Path) -> None:
    """kill at the audit->repair junction: the audit completed (8 GOOD chunks
    with a confirmed finding) but repair never started — resume replays every
    audit chunk (0 audit calls), repair starts fresh (1 batch + re-audit),
    and the chapter is released."""
    cfg = _whole_chapter_cfg(tmp_path)
    override = B3AuditRepairConfig(
        entity_context_enabled=False,
        russian_editor_enabled=False,
        max_input_tokens=1,  # 8 single-pair audit chunks
        audit_transport_max_retries=0,
        audit_transport_base_delay_seconds=0,
    )
    issue = {
        "id": "p00001", "category": "addition", "severity": "major",
        "confidence": "high", "note": "дублирование слова",
        "excerpt": "номер1 номер1",
    }
    first = _run_with_b3(
        cfg,
        _ScopedAuditBackend(
            audit_issues=[issue],
            repair_results=[{
                "index": 1, "decision": "repair", "pid": "p00001",
                "repaired_translation": "Перевод номер1",
                "reason": "убрал дубль",
            }],
            reaudit_issues=[],
        ),
        config_override=override,
    )
    assert first.step8["released_as_audited"] is True
    cache_path = cfg.out_dir / "audit_cache_b3.json"
    base = _read_json(cache_path)
    assert len(base["chunks"]) == 8

    # The stored issues carry _debug.chunk attribution; reuse them so the
    # replayed audit produces the same confirmed finding.
    stored_issues = [dict(i) for i in base["issues"]]
    assert stored_issues and stored_issues[0]["id"] == "p00001"

    # Simulate the kill: audit fully done (all 8 GOOD + issues), repair pending.
    stage = _stage_progress_with(
        r_editor={"status": "disabled", "enabled": False, "done_chunks": [],
                  "failed_chunks": [], "outcome": None},
        audit={"status": "complete", "done_chunks": list(range(1, 9)),
               "failed_chunks": [], "chunks": base["chunks"],
               "issues": stored_issues},
        repair=_repair_pending_stage(),
        reaudit=_reaudit_pending_stage(),
    )
    _craft_kill_state(cache_path, base=base, stage_progress=stage)

    resume = _B3MockBackend(
        audit_issues=[issue],  # must NOT be consumed: audit fully replayed
        repair_results=[{
            "index": 1, "decision": "repair", "pid": "p00001",
            "repaired_translation": "Перевод номер1",
            "reason": "убрал дубль",
        }],
        reaudit_issues=[],
    )
    second = _run_with_b3(cfg, resume, config_override=override)
    # The audit fully survives: every GOOD chunk replayed, 0 audit calls.
    assert second.step6["partial_resume"] is True
    assert resume.audit_calls() == 0
    # Repair starts fresh: 1 batch + 1 re-audit.
    assert resume.repair_calls() == 1
    assert resume.reaudit_calls() == 1
    assert second.step8["released_as_audited"] is True
    repaired = _read_json(cfg.out_dir / "translations_repaired.json")
    assert repaired["translations"]["p00001"] == "Перевод номер1"


def test_b3_kill_safe_incremental_tamper_full_miss(tmp_path: Path) -> None:
    """kill-sim + tamper: after a kill leaves the stage_progress cache, ANY
    mutation of stage_progress (here: a done_chunk moved out of the
    contiguous prefix) is a FULL miss — every chunk re-runs fresh, nothing
    replayed."""
    cfg = _whole_chapter_cfg(tmp_path)
    override = B3AuditRepairConfig(
        entity_context_enabled=False,
        russian_editor_enabled=False,
        max_input_tokens=1,  # 8 single-pair audit chunks
        audit_transport_max_retries=0,
        audit_transport_base_delay_seconds=0,
    )
    _run_with_b3(
        cfg,
        _B3MockBackend(audit_issues=[], repair_results=[], reaudit_issues=[]),
        config_override=override,
    )
    cache_path = cfg.out_dir / "audit_cache_b3.json"
    base = _read_json(cache_path)

    stage = _stage_progress_with(
        r_editor={"status": "disabled", "enabled": False, "done_chunks": [],
                  "failed_chunks": [], "outcome": None},
        audit={"status": "partial", "done_chunks": [1, 2], "failed_chunks": [],
               "chunks": base["chunks"][:2], "issues": []},
        repair=_repair_pending_stage(),
        reaudit=_reaudit_pending_stage(),
    )
    _craft_kill_state(cache_path, base=base, stage_progress=stage)

    # Tamper: done_chunks [1,2] -> [1,3] (coverage gap; recompute the hash so
    # only the schema/contiguity validation can catch it).
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    payload["stage_progress"]["audit"]["done_chunks"] = [1, 3]
    payload["partial_resume_hash"] = canonical_json_hash({
        "r_editor": payload["stage_progress"]["r_editor"],
        "audit": payload["stage_progress"]["audit"],
        "repair": payload["stage_progress"]["repair"],
        "reaudit": payload["stage_progress"]["reaudit"],
    })
    cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    resume = _B3MockBackend(audit_issues=[], repair_results=[], reaudit_issues=[])
    second = _run_with_b3(cfg, resume, config_override=override)
    # Full miss: ALL 8 audit chunks re-run fresh, 0 replayed.
    assert second.step6["partial_resume"] is False
    assert resume.audit_calls() == 8
    assert second.step8["released_as_audited"] is True


# ---------------------------------------------------------------------------
# FIX RV2-findings (t_006f3a79): unconditional audit coverage + malformed
# chunk-record fail-closed (a miss, never an AttributeError escaping load())
# ---------------------------------------------------------------------------


def test_kill_safe_audit_unmarked_chunk_coverage_full_miss(tmp_path: Path) -> None:
    """Finding A (t_006f3a79): audit.done_chunks=[] with a GOOD record still
    carried in audit.chunks is an UNMARKED chunk — the chunk-index coverage
    check runs UNCONDITIONALLY (not only under ``if done``), so load() is a
    full miss even when partial_resume_hash is recomputed over the payload:
    no unmarked chunk may ever enter a resume plan."""
    translations = _translation(2)
    audit = {
        "status": "partial", "done_chunks": [1, 2], "failed_chunks": [],
        "chunks": [
            {"chunk": 1, "first_pid": "p00001", "last_pid": "p00001",
             "pair_count": 1, "context_count": 0, "status": "GOOD",
             "finish_reason": "stop", "reasoning_chars": 0,
             "reasoning_file": "b3_audit_chunk1_raw.txt", "issue_count": 0},
            {"chunk": 2, "first_pid": "p00002", "last_pid": "p00002",
             "pair_count": 1, "context_count": 0, "status": "GOOD",
             "finish_reason": "stop", "reasoning_chars": 0,
             "reasoning_file": "b3_audit_chunk2_raw.txt", "issue_count": 0},
        ],
        "issues": [],
    }
    stage = _stage_progress_with(
        r_editor=_r_editor_pending_stage(),
        audit=audit,
        repair=_repair_pending_stage(),
        reaudit=_reaudit_pending_stage(),
    )
    path = _save_stage_progress(tmp_path, translations=translations, stage_progress=stage)

    # Tamper: drop the done marks while the GOOD chunk records remain, then
    # RECOMPUTE the hash — only the unconditional coverage check can reject.
    assert _tamper_and_load(
        path, translations,
        lambda sp: sp["audit"].update(done_chunks=[], failed_chunks=[]),
    ) is None


def test_kill_safe_r_editor_malformed_chunk_record_full_miss(tmp_path: Path) -> None:
    """Finding B (t_006f3a79): r_editor.outcome.chunks=[ [ ] ] is a malformed
    chunk record — the item type is validated BEFORE field access, so load()
    returns a clean miss (None) instead of raising AttributeError."""
    translations = _translation(2)
    r_editor = {
        "status": "partial", "enabled": True,
        "done_chunks": [1], "failed_chunks": [],
        "outcome": {"chunk_size": 1, "chunks": [[]]},
    }
    stage = _stage_progress_with(
        r_editor=r_editor,
        audit=_audit_pending_stage(),
        repair=_repair_pending_stage(),
        reaudit=_reaudit_pending_stage(),
    )
    path = _save_stage_progress(tmp_path, translations=translations, stage_progress=stage)
    assert _load_stage_progress(path, translations=translations, r_editor_enabled=True) is None


def test_kill_safe_audit_malformed_chunk_record_full_miss(tmp_path: Path) -> None:
    """Finding B (t_006f3a79): audit.chunks=[ [ ] ] with done_chunks=[1] is a
    malformed chunk record — the item type is validated BEFORE field access,
    so load() returns a clean miss (None) instead of raising AttributeError."""
    translations = _translation(2)
    audit = {
        "status": "partial", "done_chunks": [1], "failed_chunks": [],
        "chunks": [[]], "issues": [],
    }
    stage = _stage_progress_with(
        r_editor=_r_editor_pending_stage(),
        audit=audit,
        repair=_repair_pending_stage(),
        reaudit=_reaudit_pending_stage(),
    )
    path = _save_stage_progress(tmp_path, translations=translations, stage_progress=stage)
    assert _load_stage_progress(path, translations=translations, r_editor_enabled=False) is None
