"""REAL kill-path acceptance tests (t_aa4a125c, RV MEDIUM 04:39 on t_2d16962c).

The reviewer's finding: the full-run kill-sims in
``test_b3_kill_safe_incremental.py`` first complete a normal run and THEN
hand-rewrite ``audit_cache_b3.json`` via ``_craft_kill_state`` — no real
subprocess interruption, no injected exception, no checkpoint hook. The suite
would stay green if ``_save_stage_progress()`` were never wired, wrote once at
the end, or died at a named save point, and it does not cover "kill after
every save point in a big chapter".

This module closes that gap. Every test runs a REAL full B3 pipeline (mock
backends, 0 real model calls) and interrupts it deterministically with an
injected ``_KillSimulated`` exception raised INSIDE the Nth incremental save
(i.e. exactly inside ``_save_stage_progress()``, after the Nth chunk/batch
was persisted). A BaseException subclass is used deliberately: a real
Ctrl+C / SIGKILL is a BaseException, so the pipeline's ``except Exception``
progress-hook guards and the strict runner's B3 wrapper cannot swallow it —
the run genuinely aborts at the kill point, exactly like a killed process.

Then we assert, at EVERY save point of the big chapter (8 R + 8 audit +
6 repair + 4 reaudit = 26 save points, including the stage junctions):

1. the persisted ``audit_cache_b3.json`` contains EXACTLY the completed
   prefix (done_chunks/done_batches == executed slices; everything later is
   pending/empty);
2. the file is valid: ``B3AuditCache.load()`` returns a cache (not None) and
   ``partial_resume_hash`` recomputes to the persisted value;
3. a resume run reuses ONLY the GOOD slices (0 model calls for them) and
   makes ONLY the residual model calls, and the chapter completes and is
   released as audited.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

import pytest

from pact_v4.phase1.models import canonical_json_hash
from pact_v4.pipeline.b3_audit_repair import B3AuditCache, B3AuditRepairConfig
from pact_v4.runtime.backend_protocol import (
    CompletionError,
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
# Big-chapter matrix layout (deterministic fixture, verified by probe):
# 8 R chunks + 8 audit chunks + 6 repair batches + 4 reaudit chunks = 26
# incremental save points. Kill after EVERY one of them.
# ---------------------------------------------------------------------------
R_SAVES = 8
AUDIT_SAVES = 8
REPAIR_SAVES = 6
REAUDIT_SAVES = 4
TOTAL_SAVES = R_SAVES + AUDIT_SAVES + REPAIR_SAVES + REAUDIT_SAVES  # 26

KILL_ORDINALS = list(range(1, TOTAL_SAVES + 1))


class _KillSimulated(BaseException):
    """Deterministic stand-in for a real process kill (Ctrl+C / SIGKILL).

    BaseException (NOT Exception) on purpose: the evaluators' progress hooks
    and the strict runner's B3 wrapper swallow ``except Exception`` — a real
    kill must bypass them and abort the run, which is exactly what a
    BaseException does.
    """


class _ScopedB3Backend(_B3MockBackend):
    """Deterministic B3 mock for the big-chapter matrix.

    * audit: emits the canned issues ONLY for the chunk that owns the issue's
      pid (parsed from the request's AUDIT_PAIRS), so with max_input_tokens=1
      every pid is its own chunk and each issue is answered exactly once;
    * repair: emits one result per FINDINGS pid of the batch (parsed from the
      request's ``[index] pid | ...`` lines), decision=repair for
      ``repair_pids`` and pass otherwise;
    * reaudit: no residual issues.
    """

    def __init__(
        self,
        *,
        audit_issues: Sequence[Mapping[str, Any]],
        repair_pids: Sequence[str],
        reaudit_issues: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        super().__init__(
            audit_issues=list(audit_issues),
            repair_results=[],
            reaudit_issues=list(reaudit_issues),
        )
        self._repair_pids = set(repair_pids)

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        # Record the request EXACTLY ONCE (the base _B3MockBackend.complete
        # also appends — so do not delegate to it for the labels handled
        # here, or russian_editor/entity requests would be double-counted).
        self.requests.append(request)
        label = request.label or ""
        if "russian_editor" in label:
            return _ok_response(self._r_editor_payload(request))
        if "entity_extractor" in label:
            if self._fail_entity:
                raise CompletionError("simulated entity extraction failure")
            return _ok_response(self._entity_payload)
        if "qwen_chapter_audit" in label:
            prompt = request.messages[0].content
            audit_section = prompt.split("AUDIT_PAIRS (")[-1]
            if audit_section == prompt.split("AUDIT_PAIRS (")[0]:
                audit_section = prompt.split("AUDIT_PAIRS:", 1)[-1]
            scoped = [
                dict(issue) for issue in self._audit_issues
                if f'id="{issue["id"]}"' in audit_section
            ]
            return _ok_response({"issues": scoped})
        if "selective_repair" in label:
            return _ok_response(self._repair_payload(request))
        if "reaudit" in label:
            return _ok_response({"issues": self._reaudit_issues})
        raise AssertionError(f"unexpected B3 request label {label!r}")

    def _repair_payload(self, request: CompletionRequest) -> dict:
        """Per-batch scoped repair: one result per finding pid of the batch."""
        prompt = request.messages[0].content
        current: dict = {}
        in_translation = False
        for line in prompt.splitlines():
            line = line.strip()
            if line.startswith("TRANSLATION"):
                in_translation = True
                continue
            if in_translation:
                if line.startswith("FINDINGS") or not line:
                    break
                if ":" not in line:
                    continue
                pid, _, text = line.partition(":")
                pid = pid.strip()
                text = text.strip()
                if pid.startswith("p") and text:
                    current[pid] = text
        # FINDINGS block lines: "  [1] p00001 | CONFIRMED | category | ..."
        findings_pids: list[str] = []
        in_findings = False
        for line in prompt.splitlines():
            line = line.strip()
            if line.startswith("FINDINGS"):
                in_findings = True
                continue
            if in_findings:
                if not line:
                    break
                body = line.split("]", 1)[-1].strip()
                pid = body.split("|", 1)[0].strip()
                if pid.startswith("p"):
                    findings_pids.append(pid)
        results = []
        for idx, pid in enumerate(findings_pids, 1):
            if pid in self._repair_pids:
                base = current.get(pid, "")
                results.append({
                    "index": idx,
                    "decision": "repair", "pid": pid,
                    "repaired_translation": base + " (исправлено)",
                    "reason": "убрал дубль",
                })
            else:
                results.append({
                    "index": idx,
                    "decision": "pass", "pid": pid,
                    "repaired_translation": "",
                    "reason": "verified",
                })
        return {"results": results}


# ---------------------------------------------------------------------------
# Big-chapter fixture
# ---------------------------------------------------------------------------


def _big_chapter_override() -> B3AuditRepairConfig:
    """Deterministic big-chapter B3 config: 8 R + 8 audit chunks (each pid
    its own chunk), 6 repair batches (6 findings, one per batch), 4 reaudit
    chunks (4 committed pids, one per chunk, neighbour window 0)."""
    return B3AuditRepairConfig(
        entity_context_enabled=False,
        russian_editor_enabled=True,
        russian_editor_chunk_size=1,
        russian_editor_overlap_pairs=0,
        russian_editor_retry_max_retries=0,
        russian_editor_retry_base_delay_seconds=0,
        max_input_tokens=1,
        audit_transport_max_retries=0,
        audit_transport_base_delay_seconds=0,
        repair_microbatch_trigger=1,
        repair_microbatch_target=1,
        repair_reaudit_max_input_tokens=1,
        repair_reaudit_overlap_tokens=0,
        repair_reaudit_min_overlap_pairs=0,
        repair_reaudit_max_overlap_pairs=0,
        repair_reaudit_neighbour_window=0,
    )


def _big_chapter_issues(n: int) -> list:
    return [
        {"id": f"p{i:05d}", "category": "addition", "severity": "major",
         "confidence": "high", "note": "дублирование", "excerpt": "текст"}
        for i in range(1, n + 1)
    ]


def _big_chapter_backend(*, repair_pids: Sequence[str]) -> _ScopedB3Backend:
    return _ScopedB3Backend(
        audit_issues=_big_chapter_issues(REPAIR_SAVES),
        repair_pids=list(repair_pids),
        reaudit_issues=[],
    )


# ---------------------------------------------------------------------------
# Kill injection: raise inside the Nth incremental save (after the real write)
# ---------------------------------------------------------------------------


def _install_kill(monkeypatch: pytest.MonkeyPatch, kill_ordinal: int) -> None:
    """Patch ``B3AuditCache.save`` so the ``kill_ordinal``-th incremental save
    (the one carrying ``stage_progress`` — i.e. a call from
    ``_save_stage_progress()``) writes the file first, then raises
    ``_KillSimulated``. The counter is monotonic across both runs of a test
    (killed run + resume run), so the kill fires exactly once, at the Nth
    save of the killed run."""
    import pact_v4.pipeline.b3_audit_repair as module

    real_save = module.B3AuditCache.save
    state = {"n": 0}

    def _patched(self: Any, *args: Any, **kwargs: Any) -> Any:
        incremental = kwargs.get("stage_progress") is not None
        if incremental:
            state["n"] += 1
        result = real_save(self, *args, **kwargs)
        if incremental and state["n"] == kill_ordinal:
            raise _KillSimulated(
                f"kill after save point {kill_ordinal} "
                f"(incremental save #{state['n']})"
            )
        return result

    monkeypatch.setattr(module.B3AuditCache, "save", _patched)


def _run_killed(cfg: Any, backend: _ScopedB3Backend, override: B3AuditRepairConfig) -> None:
    with pytest.raises(_KillSimulated):
        _run_with_b3(cfg, backend, config_override=override)


# ---------------------------------------------------------------------------
# Expected state at each save ordinal (the completed prefix + residual calls)
# ---------------------------------------------------------------------------


def _expected_done(kill_ordinal: int) -> Dict[str, int]:
    """How many slices of each stage are persisted after the kill."""
    if kill_ordinal <= R_SAVES:
        return {"r_editor": kill_ordinal, "audit": 0, "repair": 0, "reaudit": 0}
    if kill_ordinal <= R_SAVES + AUDIT_SAVES:
        return {"r_editor": R_SAVES, "audit": kill_ordinal - R_SAVES,
                "repair": 0, "reaudit": 0}
    if kill_ordinal <= R_SAVES + AUDIT_SAVES + REPAIR_SAVES:
        return {"r_editor": R_SAVES, "audit": AUDIT_SAVES,
                "repair": kill_ordinal - R_SAVES - AUDIT_SAVES, "reaudit": 0}
    return {"r_editor": R_SAVES, "audit": AUDIT_SAVES, "repair": REPAIR_SAVES,
            "reaudit": kill_ordinal - R_SAVES - AUDIT_SAVES - REPAIR_SAVES}


_TOTALS = {"r_editor": R_SAVES, "audit": AUDIT_SAVES,
           "repair": REPAIR_SAVES, "reaudit": REAUDIT_SAVES}


def _expected_residual_calls(kill_ordinal: int) -> Dict[str, int]:
    """Model calls the RESUME run must make (total minus replayed GOOD)."""
    done = _expected_done(kill_ordinal)
    return {stage: _TOTALS[stage] - done[stage] for stage in _TOTALS}


def _assert_prefix_exact(
    cache_path: Path,
    kill_ordinal: int,
    *,
    override: B3AuditRepairConfig,
) -> None:
    """Assert the persisted cache equals EXACTLY the completed prefix."""
    done = _expected_done(kill_ordinal)
    payload = _read_json(cache_path)

    # The incremental save wrote a partial payload, never a completed one.
    assert payload["audit_complete"] is False
    sp = payload["stage_progress"]
    assert sp["r_editor"]["enabled"] is True

    # r_editor block
    if done["r_editor"]:
        assert sp["r_editor"]["done_chunks"] == list(range(1, done["r_editor"] + 1))
        assert sp["r_editor"]["failed_chunks"] == []
        status = "complete" if done["r_editor"] == R_SAVES else "partial"
        assert sp["r_editor"]["status"] == status
        assert len(sp["r_editor"]["outcome"]["chunks"]) == done["r_editor"]
    else:
        assert sp["r_editor"]["done_chunks"] == []
        assert sp["r_editor"]["outcome"] is None

    # audit block
    if done["audit"]:
        assert sp["audit"]["done_chunks"] == list(range(1, done["audit"] + 1))
        assert sp["audit"]["failed_chunks"] == []
        status = "complete" if done["audit"] == AUDIT_SAVES else "partial"
        assert sp["audit"]["status"] == status
        assert len(sp["audit"]["chunks"]) == done["audit"]
    else:
        assert sp["audit"]["status"] == "pending"
        assert sp["audit"]["done_chunks"] == []
        assert sp["audit"]["chunks"] == []
        assert sp["audit"]["issues"] == []

    # repair block
    if done["repair"]:
        assert sp["repair"]["done_batches"] == list(range(1, done["repair"] + 1))
        # done_batches == GOOD batches; committed accumulates repairs of them.
        assert len(sp["repair"]["outcome"]["batches"]) == done["repair"]
    else:
        assert sp["repair"]["status"] == "pending"
        assert sp["repair"]["done_batches"] == []
        assert sp["repair"]["committed"] == {}
        assert sp["repair"]["passed"] == []
        assert sp["repair"]["outcome"] is None

    # reaudit block
    if done["reaudit"]:
        assert len(sp["reaudit"]["done_chunks"]) == done["reaudit"]
        status = "complete" if done["reaudit"] == REAUDIT_SAVES else "partial"
        assert sp["reaudit"]["status"] == status
    else:
        assert sp["reaudit"]["status"] == "pending"
        assert sp["reaudit"]["done_chunks"] == []
        assert sp["reaudit"]["issues"] == []

    # The file is valid: load() accepts it (identity matches) and the hash
    # recomputes to the persisted value.
    loaded = B3AuditCache.load(
        cache_path,
        snapshot_hash=payload["snapshot_hash"],
        translation_hash=payload["translation_hash"],
        config_identity=payload["config_identity"],
        backend_identity_hash=payload["backend_identity_hash"],
        prompt_version=override.prompt_version,
        harness_version=override.harness_version,
        entity_context_hash=payload["entity_context_hash"],
        entity_context_enabled=payload["entity_context_enabled"],
        r_editor_enabled=True,
        expected_pids=list(payload["translations_repaired"]),
        current_text=dict(payload["translations_repaired"]),
    )
    assert loaded is not None, "persisted kill-state cache must load"
    recomputed = canonical_json_hash({
        "r_editor": sp["r_editor"],
        "audit": sp["audit"],
        "repair": sp["repair"],
        "reaudit": sp["reaudit"],
    })
    assert payload["partial_resume_hash"] == recomputed


def _assert_resume_residual(
    cfg: Any,
    override: B3AuditRepairConfig,
    kill_ordinal: int,
) -> None:
    """Resume on the same out_dir: ONLY residual model calls happen."""
    backend = _big_chapter_backend(
        repair_pids=[f"p{i:05d}" for i in range(1, 5)]
    )
    second = _run_with_b3(cfg, backend, config_override=override)
    expected = _expected_residual_calls(kill_ordinal)
    assert backend.r_editor_calls() == expected["r_editor"]
    assert backend.audit_calls() == expected["audit"]
    assert backend.repair_calls() == expected["repair"]
    assert backend.reaudit_calls() == expected["reaudit"]
    # step6.partial_resume is the AUDIT-reuse flag (audit_resume only); the
    # production resume signal covering BOTH R and audit reuse is the
    # journal's audit_started.partial_resume event (bool(audit_resume or
    # r_editor_resume)) — assert it, plus step6 for audit-touching kills.
    journal = [
        json.loads(line)
        for line in (cfg.out_dir / "audit_journal.ndjson")
        .read_text(encoding="utf-8").splitlines()
    ]
    started = next(
        e for e in reversed(journal) if e.get("event") == "audit_started"
    )
    assert started["partial_resume"] is True
    if _expected_done(kill_ordinal)["audit"] > 0:
        assert second.step6["partial_resume"] is True
    assert second.step8["released_as_audited"] is True


# ---------------------------------------------------------------------------
# Matrix: kill after EVERY save point of the big chapter (26 points)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kill_ordinal", KILL_ORDINALS,
    ids=[f"save-{n:02d}" for n in KILL_ORDINALS],
)
def test_b3_kill_safe_real_kill_after_every_save_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kill_ordinal: int,
) -> None:
    """Real full B3 run killed AFTER the Nth incremental save: the persisted
    cache holds EXACTLY the completed prefix, the file is valid, and a resume
    run makes ONLY the residual model calls (0 for every GOOD slice)."""
    cfg = _whole_chapter_cfg(tmp_path)
    override = _big_chapter_override()
    backend = _big_chapter_backend(
        repair_pids=[f"p{i:05d}" for i in range(1, 5)]
    )
    _install_kill(monkeypatch, kill_ordinal)

    # The killed run aborts with the simulated kill (never completes).
    _run_killed(cfg, backend, override)

    # 1+2. Persisted prefix is exact and valid.
    _assert_prefix_exact(cfg.out_dir / "audit_cache_b3.json", kill_ordinal,
                         override=override)

    # 3. Resume reuses GOOD slices only.
    _assert_resume_residual(cfg, override, kill_ordinal)


# ---------------------------------------------------------------------------
# Stage junctions (the last save point of each stage)
# ---------------------------------------------------------------------------


def test_b3_kill_safe_real_kill_at_audit_to_repair_junction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kill at the LAST audit save (ordinal 16): the whole audit survives
    (0 audit calls on resume), repair starts fresh (all 6 batches) and the
    committed repair is applied; the chapter is released."""
    kill_ordinal = R_SAVES + AUDIT_SAVES  # 16
    cfg = _whole_chapter_cfg(tmp_path)
    override = _big_chapter_override()
    backend = _big_chapter_backend(
        repair_pids=[f"p{i:05d}" for i in range(1, 5)]
    )
    _install_kill(monkeypatch, kill_ordinal)
    _run_killed(cfg, backend, override)

    payload = _read_json(cfg.out_dir / "audit_cache_b3.json")
    sp = payload["stage_progress"]
    # Audit fully complete (all 8 GOOD chunks + 6 issues), repair pending.
    assert sp["audit"]["status"] == "complete"
    assert sp["audit"]["done_chunks"] == list(range(1, 9))
    assert len(sp["audit"]["issues"]) == 6
    assert sp["repair"]["status"] == "pending"
    assert sp["reaudit"]["status"] == "pending"
    assert sp["r_editor"]["status"] == "complete"

    _assert_resume_residual(cfg, override, kill_ordinal)

    # The repair really committed on resume: the repaired map is persisted.
    repaired = _read_json(cfg.out_dir / "translations_repaired.json")
    assert repaired["translations"]["p00001"].endswith("(исправлено)")


def test_b3_kill_safe_real_kill_at_repair_to_reaudit_junction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kill at the LAST repair batch save (ordinal 22): every GOOD batch is
    replayed (0 repair calls on resume), the re-audit of the committed pids
    starts fresh (all 4 reaudit chunks) and the chapter is released."""
    kill_ordinal = R_SAVES + AUDIT_SAVES + REPAIR_SAVES  # 22
    cfg = _whole_chapter_cfg(tmp_path)
    override = _big_chapter_override()
    backend = _big_chapter_backend(
        repair_pids=[f"p{i:05d}" for i in range(1, 5)]
    )
    _install_kill(monkeypatch, kill_ordinal)
    _run_killed(cfg, backend, override)

    payload = _read_json(cfg.out_dir / "audit_cache_b3.json")
    sp = payload["stage_progress"]
    # Repair fully done (all 6 batches GOOD), reaudit pending.
    assert sp["audit"]["status"] == "complete"
    assert sp["repair"]["done_batches"] == list(range(1, 7))
    assert sp["reaudit"]["status"] == "pending"
    assert len(sp["repair"]["outcome"]["batches"]) == 6

    _assert_resume_residual(cfg, override, kill_ordinal)
