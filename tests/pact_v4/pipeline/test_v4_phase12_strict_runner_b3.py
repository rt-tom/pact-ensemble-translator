"""V4.1 B3 production audit/repair integration tests (whole-chapter).

Covers the B3 contract (docs/plans/V4_1_AUDIT_B1_RU.md §10 B3 + §9.4):
whole-chapter generation -> ChunkedAuditEvaluator -> apply_hard_filters ->
selective repair -> re-audit -> updated translations_repaired.json; the
audit_complete fail-closed gate; the configurable entity context stage
(``entity_context_enabled`` true/false); the audit cache/resume identity
(full hit -> 0 model calls; incomplete audit -> re-run); and the CLI flags
(--run-audit/--skip-audit/--entity-context/--no-entity-context).

All model calls go through a scripted in-memory CompletionBackend — 0 real
Qwen/Gemma calls anywhere in this file.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pytest

from pact_v4.pipeline.b3_audit_repair import (
    B3AuditRepair,
    B3AuditRepairConfig,
)
from pact_v4.pipeline.v4_phase12_strict_runner import (
    StrictRunConfig,
    run_chapter_strict,
)
from pact_v4.runtime.backend_protocol import (
    BackendCallRecord,
    BackendDescriptor,
    CompletionBackend,
    CompletionError,
    CompletionRequest,
    CompletionResponse,
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

# ---------------------------------------------------------------------------
# Scripted in-memory backend for the B3 stage (audit + entity + repair)
# ---------------------------------------------------------------------------


class _B3MockBackend(CompletionBackend):
    """CompletionBackend serving the B3 stage roles from canned payloads.

    Dispatches on the request ``label``: the entity extractor
    (``b1.2/entity_extractor``), the chunked audit
    (``phase3/qwen_chapter_audit_v4``), the selective repair
    (``phase3/selective_repair_v4``) and the re-audit
    (``phase3/reaudit_scope_v4``). ``fail_audit`` raises a transport error
    on every audit call so the audit can never complete (fail-closed test).
    """

    _BINDINGS = {
        "default": "qwen-3.6-35b",
        "generator": "gemma-4-26b",
        "qwen_audit": "qwen-3.6-35b",
        "fidelity_reviewer": "qwen-3.6-35b",
        "entity_extractor": "qwen-3.6-35b",
    }

    def __init__(
        self,
        *,
        audit_issues: Optional[Sequence[Mapping[str, Any]]] = None,
        repair_results: Optional[Sequence[Mapping[str, Any]]] = None,
        reaudit_issues: Optional[Sequence[Mapping[str, Any]]] = None,
        entity_payload: Optional[Mapping[str, Any]] = None,
        fail_audit: bool = False,
    ) -> None:
        self._audit_issues = list(audit_issues or [])
        self._repair_results = list(repair_results or [])
        self._reaudit_issues = list(reaudit_issues or [])
        self._entity_payload = entity_payload or {"entities": []}
        self._fail_audit = fail_audit
        self.requests: List[CompletionRequest] = []

    @property
    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(
            kind="local_llama",
            transport_version="openai-chat-completions/v1",
            endpoint_family="openai_chat_completions",
            public_endpoint="http://127.0.0.1:8094/v1/chat/completions",
            model_bindings=dict(self._BINDINGS),
            effective_options={"temperature": 0.0, "context_size": 49152},
        )

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        label = request.label or ""
        if "entity_extractor" in label:
            return _ok_response(self._entity_payload)
        if "qwen_chapter_audit" in label:
            if self._fail_audit:
                raise CompletionError("simulated audit transport failure")
            return _ok_response({"issues": self._audit_issues})
        if "selective_repair" in label:
            return _ok_response({"results": self._repair_results})
        if "reaudit" in label:
            return _ok_response({"issues": self._reaudit_issues})
        raise AssertionError(f"unexpected B3 request label {label!r}")

    def close(self) -> None:
        pass

    def call_records(self) -> Sequence[BackendCallRecord]:
        return []

    # counters ---------------------------------------------------------

    def entity_calls(self) -> int:
        return sum(1 for r in self.requests if "entity_extractor" in (r.label or ""))

    def audit_calls(self) -> int:
        return sum(1 for r in self.requests if "qwen_chapter_audit" in (r.label or ""))

    def repair_calls(self) -> int:
        return sum(1 for r in self.requests if "selective_repair" in (r.label or ""))

    def reaudit_calls(self) -> int:
        return sum(1 for r in self.requests if "reaudit" in (r.label or ""))


def _ok_response(payload: Mapping[str, Any]) -> CompletionResponse:
    return CompletionResponse(
        text=json.dumps(payload, ensure_ascii=False),
        model="qwen-3.6-35b",
        finish_reason="stop",
    )


# ---------------------------------------------------------------------------
# Model caller: whole-chapter generator with one reproducible defect
# ---------------------------------------------------------------------------


class _DefectiveWholeChapterCaller(StubModelCaller):
    """StubModelCaller whose output has an adjacent duplicate on p00001.

    The B3 mock audit reports ``addition`` on p00001; the adjacent
    duplicate in the translation makes the Tier A hard filter CONFIRM it,
    so the full audit -> filter -> repair -> re-audit chain is exercised
    deterministically.
    """

    def __init__(self) -> None:
        super().__init__()
        self._patched = False

    def __call__(self, bundle: Any) -> str:
        raw = super().__call__(bundle)
        payload = json.loads(raw)
        if "p00001" in payload and not self._patched:
            payload["p00001"] = "Перевод номер1 номер1"
            self._patched = True
        return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_router() -> ModelRouter:
    return ModelRouter(
        FakeLifecycleAdapter(),
        role_profile_names={"gemma": "Gemma", "qwen": "Qwen"},
        role_args={"gemma": [], "qwen": []},
    )


def _whole_chapter_cfg(tmp_path: Path, **overrides: Any) -> StrictRunConfig:
    cfg = _make_cfg(tmp_path, n_paragraphs=8)
    return type(cfg)(
        chapter_id=cfg.chapter_id,
        chapter_html_path=cfg.chapter_html_path,
        memory_dir=cfg.memory_dir,
        out_dir=cfg.out_dir,
        backend=cfg.backend,
        whole_chapter=True,
        **overrides,
    )


def _run_with_b3(
    cfg: StrictRunConfig,
    backend: _B3MockBackend,
    *,
    caller: Optional[StubModelCaller] = None,
    entity_context_enabled: bool = True,
) -> Any:
    router = _make_router()
    model_caller = _LifecycleAwareModelCaller(router, caller or _DefectiveWholeChapterCaller())
    bundle = B3AuditRepair(
        audit_backend=backend,
        repair_backend=backend,
        config=B3AuditRepairConfig(entity_context_enabled=entity_context_enabled),
    )
    result = run_chapter_strict(
        cfg, router=router, model_caller=model_caller,
        qwen_evaluator=_LifecycleAwareQwen(router, StubQwen()),
        gemma_selector=_LifecycleAwareGemmaSelector(router, StubGemma()),
        qwen_audit_evaluator=_LifecycleAwareQwenAudit(router, StubQwenAudit()),
        gemma_audit_evaluator=_LifecycleAwareGemmaAudit(router, StubGemmaAudit()),
        b3_audit_repair=bundle,
    )
    return result


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Acceptance 1: full flow generation -> audit -> filters -> repair ->
# re-audit -> translations_repaired.json updated (0 real model calls)
# ---------------------------------------------------------------------------


def test_b3_full_flow_repairs_and_updates_snapshots(tmp_path: Path) -> None:
    cfg = _whole_chapter_cfg(tmp_path)
    backend = _B3MockBackend(
        audit_issues=[{
            "id": "p00001", "category": "addition", "severity": "major",
            "confidence": "high", "note": "дублирование слова",
            "excerpt": "номер1 номер1",
        }],
        repair_results=[{
            "index": 1, "decision": "repair", "pid": "p00001",
            "repaired_translation": "Перевод номер1",
            "reason": "убрал дубль",
        }],
        reaudit_issues=[],
    )
    result = _run_with_b3(cfg, backend)

    # The chapter is released as audited; the audit ran (1 chunk for the
    # 8-paragraph synthetic chapter) and repair committed the fix.
    assert result.step6["audit_complete"] is True
    assert result.step6["issue_count"] == 1
    assert result.step7["committed_pids"] == ["p00001"]
    assert result.step8 == {
        "status": "complete", "audit_complete": True, "released_as_audited": True,
    }

    # translations_repaired.json updated: p00001 no longer has the duplicate.
    repaired = _read_json(cfg.out_dir / "translations_repaired.json")
    assert repaired["translations"]["p00001"] == "Перевод номер1"
    assert repaired["config_identity"] == result.record["identities"]["config_identity"]

    # translation_diffs.json: raw->repaired real, repaired->final empty.
    diffs = _read_json(cfg.out_dir / "translation_diffs.json")
    assert "p00001" in diffs["diffs"]["raw->repaired"]
    assert diffs["diffs"]["repaired->final"] == {}

    # translations.json (final alias) carries the repaired map.
    final = _read_json(result.translations_path)
    assert final["p00001"] == "Перевод номер1"

    # Journal: audit + repair + re-audit + gate events recorded.
    journal = [
        json.loads(line)
        for line in (cfg.out_dir / "audit_journal.ndjson").read_text(encoding="utf-8").splitlines()
    ]
    events = [entry["event"] for entry in journal]
    assert "audit_started" in events
    assert "audit_chunk_started" in events and "audit_chunk_done" in events
    assert "audit_complete" in events
    assert "finding" in events
    assert "repair_round" in events
    assert "reaudit_scope" in events
    assert "gate" in events
    gate = next(e for e in journal if e["event"] == "gate")
    assert gate["audit_complete"] is True and gate["released_as_audited"] is True

    # Audit cache persisted with the identity block; entity cache too.
    cache = _read_json(cfg.out_dir / "audit_cache_b3.json")
    assert cache["schema"] == "pact-v4-b3-audit-cache/v1"
    assert cache["audit_complete"] is True
    assert cache["snapshot_hash"] == result.record["identities"]["snapshot_hash"]
    assert cache["config_identity"] == result.record["identities"]["config_identity"]
    assert cache["entity_context_hash"] is not None  # entity prepass ran
    assert (cfg.out_dir / "entity_context_cache.json").exists()

    # Call accounting: entity(1) + audit(1) + repair(1) + re-audit(1);
    # all served by the scripted backend — 0 real model calls.
    assert backend.entity_calls() == 1
    assert backend.audit_calls() == 1
    assert backend.repair_calls() == 1
    assert backend.reaudit_calls() == 1


# ---------------------------------------------------------------------------
# Acceptance 2: audit_complete=false -> fail-closed (never released)
# ---------------------------------------------------------------------------


def test_b3_audit_incomplete_fail_closed(tmp_path: Path) -> None:
    cfg = _whole_chapter_cfg(tmp_path)
    backend = _B3MockBackend(fail_audit=True)
    result = _run_with_b3(cfg, backend)

    # The audit failed -> the chapter is NOT released as passed audit.
    assert result.step6["status"] == "incomplete"
    assert result.step6["audit_complete"] is False
    assert result.step6["failed_chunks"] != []
    assert result.step7 == {"status": "skipped", "reason": "audit_incomplete_fail_closed"}
    assert result.step8["status"] == "fail_closed_audit_incomplete"
    assert result.step8["released_as_audited"] is False

    # No repair was attempted (fail-closed).
    assert backend.repair_calls() == 0
    assert backend.reaudit_calls() == 0

    # translations_repaired == raw (no repair), and the audit cache records
    # audit_complete=false so a resume NEVER skips this incomplete audit.
    repaired = _read_json(cfg.out_dir / "translations_repaired.json")
    assert repaired["translations"]["p00001"] == "Перевод номер1 номер1"
    cache = _read_json(cfg.out_dir / "audit_cache_b3.json")
    assert cache["audit_complete"] is False

    # Journal gate says not released.
    journal = [
        json.loads(line)
        for line in (cfg.out_dir / "audit_journal.ndjson").read_text(encoding="utf-8").splitlines()
    ]
    gate = next(e for e in journal if e["event"] == "gate")
    assert gate["released_as_audited"] is False


# ---------------------------------------------------------------------------
# Acceptance 3: entity_context_enabled true/false both branches work
# ---------------------------------------------------------------------------


def test_b3_entity_enabled_runs_prepass_and_feeds_filters(tmp_path: Path) -> None:
    cfg = _whole_chapter_cfg(tmp_path)
    backend = _B3MockBackend(audit_issues=[], reaudit_issues=[])
    result = _run_with_b3(cfg, backend, entity_context_enabled=True)

    # Entity prepass ran exactly once (cache hit on resume would be 0).
    assert backend.entity_calls() == 1
    assert result.step6["entity_context_enabled"] is True
    assert result.step6["entity_context_hash"] is not None
    cache = _read_json(cfg.out_dir / "audit_cache_b3.json")
    assert cache["entity_context_hash"] is not None
    assert cache["entity_context_enabled"] is True


def test_b3_entity_disabled_skips_prepass(tmp_path: Path) -> None:
    cfg = _whole_chapter_cfg(tmp_path)
    backend = _B3MockBackend(audit_issues=[], reaudit_issues=[])
    result = _run_with_b3(cfg, backend, entity_context_enabled=False)

    # Entity prepass never called; audit ran without the entity block.
    assert backend.entity_calls() == 0
    assert result.step6["entity_context_enabled"] is False
    assert result.step6["entity_context_hash"] is None
    cache = _read_json(cfg.out_dir / "audit_cache_b3.json")
    assert cache["entity_context_hash"] is None
    assert cache["entity_context_enabled"] is False
    # No entity cache file was created.
    assert not (cfg.out_dir / "entity_context_cache.json").exists()


# ---------------------------------------------------------------------------
# Acceptance 4: cache/resume — full hit skips the audit (0 calls); an
# incomplete audit re-runs.
# ---------------------------------------------------------------------------


def test_b3_resume_full_cache_hit_skips_audit(tmp_path: Path) -> None:
    cfg = _whole_chapter_cfg(tmp_path)
    backend = _B3MockBackend(
        audit_issues=[{
            "id": "p00001", "category": "addition", "severity": "major",
            "confidence": "high", "note": "дублирование слова",
            "excerpt": "номер1 номер1",
        }],
        repair_results=[{
            "index": 1, "decision": "repair", "pid": "p00001",
            "repaired_translation": "Перевод номер1",
            "reason": "убрал дубль",
        }],
        reaudit_issues=[],
    )
    first = _run_with_b3(cfg, backend)
    assert first.step8["released_as_audited"] is True
    first_audit_calls = backend.audit_calls()
    first_entity_calls = backend.entity_calls()
    assert first_audit_calls == 1

    # Resume the SAME out-dir: generation replays from the journal, the B3
    # audit cache is a full identity hit -> 0 new audit/entity calls, and
    # the repaired map is restored from the cache.
    second_backend = _B3MockBackend(
        audit_issues=[],  # must NOT be consumed on a full cache hit
        repair_results=[],
        reaudit_issues=[],
    )
    second = _run_with_b3(cfg, second_backend)
    assert second.step6["from_cache"] is True
    assert second_backend.audit_calls() == 0
    assert second_backend.entity_calls() == 0
    assert second_backend.repair_calls() == 0

    repaired = _read_json(cfg.out_dir / "translations_repaired.json")
    assert repaired["translations"]["p00001"] == "Перевод номер1"
    assert second.step8["released_as_audited"] is True


def test_b3_resume_incomplete_audit_reruns(tmp_path: Path) -> None:
    cfg = _whole_chapter_cfg(tmp_path)
    # First run: audit transport failure -> cache written with
    # audit_complete=false.
    failing = _B3MockBackend(fail_audit=True)
    first = _run_with_b3(cfg, failing)
    assert first.step8["released_as_audited"] is False
    cache = _read_json(cfg.out_dir / "audit_cache_b3.json")
    assert cache["audit_complete"] is False

    # Resume: the cached audit is incomplete -> the audit MUST re-run
    # (fail-closed, never skip an incomplete audit) and repair may proceed.
    good = _B3MockBackend(
        audit_issues=[],
        reaudit_issues=[],
    )
    second = _run_with_b3(cfg, good)
    assert second.step6["from_cache"] is False
    assert good.audit_calls() == 1
    assert second.step8["released_as_audited"] is True


# ---------------------------------------------------------------------------
# Runner behavior without injected B3 machinery / with run_audit off
# ---------------------------------------------------------------------------


def test_b3_no_machinery_steps_stay_skipped(tmp_path: Path) -> None:
    cfg = _whole_chapter_cfg(tmp_path)
    router = _make_router()
    model_caller = _LifecycleAwareModelCaller(router, _DefectiveWholeChapterCaller())
    result = run_chapter_strict(
        cfg, router=router, model_caller=model_caller,
        qwen_evaluator=_LifecycleAwareQwen(router, StubQwen()),
        gemma_selector=_LifecycleAwareGemmaSelector(router, StubGemma()),
        qwen_audit_evaluator=_LifecycleAwareQwenAudit(router, StubQwenAudit()),
        gemma_audit_evaluator=_LifecycleAwareGemmaAudit(router, StubGemmaAudit()),
        # b3_audit_repair omitted
    )
    # No audit machinery -> steps recorded as skipped (A1 behavior), never
    # fabricated as complete; translations_repaired == raw.
    assert result.step6 == {"status": "skipped", "reason": "whole_chapter_generation_only"}
    repaired = _read_json(cfg.out_dir / "translations_repaired.json")
    assert repaired["translations"]["p00001"] == "Перевод номер1 номер1"
    assert not (cfg.out_dir / "audit_cache_b3.json").exists()


def test_b3_run_audit_off_skips_even_with_machinery(tmp_path: Path) -> None:
    cfg = _whole_chapter_cfg(tmp_path, run_audit=False)
    backend = _B3MockBackend()
    result = _run_with_b3(cfg, backend)
    # run_audit=False -> the B3 stage is skipped entirely (no audit calls).
    assert backend.audit_calls() == 0
    assert backend.entity_calls() == 0
    assert result.step6 == {"status": "skipped", "reason": "whole_chapter_generation_only"}
    assert not (cfg.out_dir / "audit_cache_b3.json").exists()


def test_b3_stop_after_generation_skips_audit(tmp_path: Path) -> None:
    # --stop-after-generation semantics: Steps 6/7/8 are skipped even when
    # the audit machinery is injected and run_audit is on (the owner asked
    # for generation only).
    cfg = _whole_chapter_cfg(tmp_path, stop_after="generation")
    backend = _B3MockBackend()
    result = _run_with_b3(cfg, backend)
    assert backend.audit_calls() == 0
    assert backend.entity_calls() == 0
    assert result.step6 == {"status": "skipped", "reason": "whole_chapter_generation_only"}
    assert not (cfg.out_dir / "audit_cache_b3.json").exists()


# ---------------------------------------------------------------------------
# Config identity: B3 flags are part of it (flipping invalidates resume)
# ---------------------------------------------------------------------------


def test_b3_flags_part_of_config_identity(tmp_path: Path) -> None:
    base = _whole_chapter_cfg(tmp_path)
    a = base.to_config_artifact(model_profile="test")
    assert a.values["audit"] == {
        "run": True,
        "entity_context_enabled": True,
        "max_input_tokens": 3600,
        "max_tokens": 12000,
        "overlap_tokens": 400,
    }
    on = _whole_chapter_cfg(tmp_path)
    off = _whole_chapter_cfg(tmp_path, run_audit=False)
    no_entity = _whole_chapter_cfg(tmp_path, entity_context_enabled=False)
    budget = _whole_chapter_cfg(tmp_path, audit_max_input_tokens=1800)
    ids = {
        "on": on.to_config_artifact(model_profile="test").config_identity,
        "off": off.to_config_artifact(model_profile="test").config_identity,
        "no_entity": no_entity.to_config_artifact(model_profile="test").config_identity,
        "budget": budget.to_config_artifact(model_profile="test").config_identity,
    }
    assert ids["off"] != ids["on"]
    assert ids["no_entity"] != ids["on"]
    assert ids["budget"] != ids["on"]


# ---------------------------------------------------------------------------
# CLI flags
# ---------------------------------------------------------------------------


def test_b3_cli_flags_parse(tmp_path: Path) -> None:
    from pact_full_pipeline_runner_v1.v4_phase12_strict_run import (
        _build_run_config,
        build_argparser,
    )

    base_args = [
        "--chapter-id", "046", "--chapter-html", "D:/x/046.html",
        "--memory-dir", "D:/x/mem", "--out-dir", str(tmp_path / "out"),
    ]
    default_cfg = _build_run_config(build_argparser().parse_args(base_args), None)
    assert default_cfg.run_audit is True
    assert default_cfg.entity_context_enabled is True

    skip_cfg = _build_run_config(
        build_argparser().parse_args(base_args + ["--skip-audit"]), None
    )
    assert skip_cfg.run_audit is False

    run_cfg = _build_run_config(
        build_argparser().parse_args(base_args + ["--run-audit"]), None
    )
    assert run_cfg.run_audit is True

    no_entity = _build_run_config(
        build_argparser().parse_args(base_args + ["--no-entity-context"]), None
    )
    assert no_entity.entity_context_enabled is False

    entity = _build_run_config(
        build_argparser().parse_args(base_args + ["--entity-context"]), None
    )
    assert entity.entity_context_enabled is True

    # --run-audit and --skip-audit are mutually exclusive at parse time.
    with pytest.raises(SystemExit):
        build_argparser().parse_args(base_args + ["--run-audit", "--skip-audit"])
