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
             "reasoning_file": "b3_audit_chunk1_raw.txt"},
            {"chunk": 2, "first_pid": "p00002", "last_pid": "p00002",
             "pair_count": 1, "context_count": 0, "status": "GOOD",
             "finish_reason": "stop", "reasoning_chars": 0,
             "reasoning_file": "b3_audit_chunk2_raw.txt"},
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


def test_kill_safe_cache_repair_2of4_resume_plan(tmp_path: Path) -> None:
    """kill-sim repair 2/4: stage_progress.repair with 2 GOOD batches of 4 ->
    resume plan reuses those 2 (committed not repeated), batches 3-4 redone."""
    translations = _translation(4)
    repair = {
        "status": "partial",
        "done_batches": [1, 2],
        "committed": {"p00001": "исправленный текст 1."},
        "passed": [],
        "outcome": {
            "batch_count": 4,
            "batches": [
                {"batch_index": 1, "status": "GOOD",
                 "findings": [{"pid": "p00001"}],
                 "results": [{"index": 1, "decision": "repair",
                              "pid": "p00001",
                              "repaired_translation": "исправленный текст 1.",
                              "reason": "mock"}]},
                {"batch_index": 2, "status": "GOOD",
                 "findings": [{"pid": "p00002"}],
                 "results": [{"index": 1, "decision": "pass",
                              "pid": "p00002", "repaired_translation": "",
                              "reason": "verified"}]},
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
             "issues": []},
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
         "reasoning_file": f"b3_audit_chunk{i}_raw.txt"}
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
# Full-run kill-sims: a cache left by a killed process is loaded on resume,
# GOOD chunks replayed with 0 model calls, the rest re-run, chapter released
# ---------------------------------------------------------------------------


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
