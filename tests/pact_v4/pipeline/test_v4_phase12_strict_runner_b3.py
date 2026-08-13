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

from pact_v4.audit.entity_extractor import VALIDATION_REPORT_SCHEMA
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

    Dispatches on the request ``label``: the Russian editor
    (``russian_editor_v4``), the entity extractor
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
        r_editor_edits: Optional[Sequence[Mapping[str, Any]]] = None,
        fail_audit: bool = False,
        fail_entity: bool = False,
    ) -> None:
        self._audit_issues = list(audit_issues or [])
        self._repair_results = list(repair_results or [])
        self._reaudit_issues = list(reaudit_issues or [])
        self._entity_payload = entity_payload or {"entities": []}
        # V4.2 R: canned Russian-editor edits (default empty -> no edits).
        self._r_editor_edits = list(r_editor_edits or [])
        self._fail_audit = fail_audit
        self._fail_entity = fail_entity
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
        if "russian_editor" in label:
            return _ok_response(self._r_editor_payload(request))
        if "entity_extractor" in label:
            if self._fail_entity:
                raise CompletionError("simulated entity extraction failure")
            return _ok_response(self._entity_payload)
        if "qwen_chapter_audit" in label:
            if self._fail_audit:
                raise CompletionError("simulated audit transport failure")
            return _ok_response({"issues": self._audit_issues})
        if "selective_repair" in label:
            return _ok_response(self._repair_payload(request))
        if "reaudit" in label:
            return _ok_response({"issues": self._reaudit_issues})
        raise AssertionError(f"unexpected B3 request label {label!r}")

    def _r_editor_payload(self, request: CompletionRequest) -> dict:
        """Canned Russian-editor edits, with ``original`` resolved from the
        request's own EDIT_PAIRS text (the model must echo the EXACT current
        text, so the mock derives it from the prompt instead of hardcoding
        fixture text). Entries are ``(pid, klass, rewritten_suffix)``; the
        rewritten text = current text + suffix."""
        prompt = request.messages[0].content
        current: dict = {}
        for line in prompt.splitlines():
            line = line.strip()
            if line.startswith("EDIT_PAIRS"):
                continue
            if ":" not in line:
                continue
            pid, _, text = line.partition(":")
            pid = pid.strip()
            text = text.strip()
            if pid.startswith("p") and text:
                current[pid] = text
        edits = []
        for entry in self._r_editor_edits:
            pid, klass, suffix = entry
            original = current.get(pid, "")
            if not original:
                continue
            edits.append({
                "pid": pid,
                "original": original,
                "rewritten": original + suffix,
                "reason": "mock",
                "class": klass,
            })
        return {"edits": edits}

    def _repair_payload(self, request: CompletionRequest) -> dict:
        """Resolve canned repair results against the request's TRANSLATION.

        ``repaired_translation`` entries of the literal token ``"<current>"``
        are replaced with the request's own current text for that PID (parsed
        from the TRANSLATION block, mirroring ``_r_editor_payload``), so a
        mock repair always echoes a FULL-LENGTH text — never a fragment that
        the run_011 text-preservation gate would (correctly) reject. Any other
        canned string is returned verbatim (explicit-fixture tests).
        """
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
        results = []
        for entry in self._repair_results:
            item = dict(entry)
            repaired = item.get("repaired_translation")
            if isinstance(repaired, str) and repaired.startswith("<current>"):
                suffix = repaired[len("<current>"):]
                item["repaired_translation"] = (
                    current.get(item.get("pid", ""), "") + suffix
                )
            results.append(item)
        return {"results": results}

    def close(self) -> None:
        pass

    def call_records(self) -> Sequence[BackendCallRecord]:
        return []

    # counters ---------------------------------------------------------

    def r_editor_calls(self) -> int:
        return sum(1 for r in self.requests if "russian_editor" in (r.label or ""))

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
    config_override: Optional[B3AuditRepairConfig] = None,
) -> Any:
    router = _make_router()
    model_caller = _LifecycleAwareModelCaller(router, caller or _DefectiveWholeChapterCaller())
    bundle = B3AuditRepair(
        audit_backend=backend,
        repair_backend=backend,
        config=(
            config_override
            if config_override is not None
            else B3AuditRepairConfig(entity_context_enabled=entity_context_enabled)
        ),
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


def test_b3_full_flow_noop_repair_converted_to_pass(tmp_path: Path) -> None:
    # REPAIR-2 (t_768537b9, run_013 batch1): a repair batch where ONE index
    # is a no-op (repaired_translation == current) must NOT fail the batch —
    # the no-op index is converted to a per-index PASS (journaled as a
    # WARNING), the other index is repaired normally, and the chapter is
    # released as audited (run_013: the no-op index failed the whole batch
    # and pushed 4 real findings into debt).
    cfg = _whole_chapter_cfg(tmp_path)
    backend = _B3MockBackend(
        audit_issues=[{
            "id": "p00001", "category": "addition", "severity": "major",
            "confidence": "high", "note": "дублирование слова",
            "excerpt": "номер1 номер1",
        }],
        repair_results=[
            # index 1: NO-OP — "<current>" echoes the request's own current
            # text back unchanged (the mock's no-op contract).
            {"index": 1, "decision": "repair", "pid": "p00001",
             "repaired_translation": "<current>", "reason": "no-op"},
        ],
        reaudit_issues=[],
    )
    result = _run_with_b3(cfg, backend)

    # Batch survived the no-op index: repair completed, chapter released.
    assert result.step7["repair_complete"] is True
    assert result.step7["committed_pids"] == []
    assert result.step7["passed_pids"] == ["p00001"]
    assert result.step8 == {
        "status": "complete", "audit_complete": True, "released_as_audited": True,
    }
    # The no-op is journaled as a non-fatal WARNING (never batch-fatal debt).
    assert any("no-op repair converted to pass" in w for w in result.step7["warnings"])
    journal = [
        json.loads(line)
        for line in (cfg.out_dir / "audit_journal.ndjson").read_text(encoding="utf-8").splitlines()
    ]
    round_event = next(e for e in journal if e["event"] == "repair_round")
    assert any("no-op repair converted to pass" in w for w in round_event["warnings"])
    assert round_event["committed_pids"] == []
    assert round_event["passed_pids"] == ["p00001"]
    assert round_event["repair_complete"] is True


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
# B3-DIAG: entity_context_validation_report.json — what the model PROPOSED
# vs what the code ACCEPTED. Written next to entity_context_cache.json only
# when a fresh validation actually ran (drop/downgrade decisions with
# reasons); an extractor failure writes neither file.
# ---------------------------------------------------------------------------


# Mixed model output against the synthetic chapter (8 paragraphs of
# "word0 word1 ... word34" each): one fully valid entity (no entry), one
# claim dropped for a dead PID, one relation downgraded (verified->candidate).
ENTITY_PAYLOAD_MIXED = {
    "entities": [
        {
            "entity": "the thing",
            "canonical_type": "word0",
            "anchor": {"pid": "p00001", "span": "word0"},
            "aliases": [
                {"surface": "word1", "pid": "p00002", "span": "word1"},
            ],
            "claims": [
                {
                    "kind": "object_identity",
                    "value": "word1 = word0",
                    "status": "candidate",
                    "evidence": [
                        {"pid": "p00001", "span": "word0"},
                        {"pid": "p00002", "span": "word1"},
                    ],
                    "evidence_windows": [["p00001", "p00002"]],
                },
            ],
        },
        {
            "entity": "the ghost",
            "canonical_type": "word2",
            "anchor": {"pid": "p00003", "span": "word2"},
            "aliases": [],
            "claims": [
                {
                    "kind": "object_identity",
                    "value": "ghost = word3",
                    "status": "candidate",
                    "evidence": [{"pid": "p99999", "span": "word3"}],
                    "evidence_windows": [["p00003", "p00004"]],
                },
            ],
        },
        {
            "entity": "the shadow",
            "canonical_type": "word4",
            "anchor": {"pid": "p00005", "span": "word4"},
            "aliases": [],
            "claims": [
                {
                    "kind": "object_identity",
                    "value": "shadow = word5",
                    "status": "verified",
                    "evidence": [
                        {"pid": "p00005", "span": "word4"},
                        {"pid": "p00006", "span": "word5"},
                    ],
                    "evidence_windows": [["p00005", "p00006"]],
                },
            ],
        },
    ],
}


def test_b3_entity_validation_report_records_drop_and_downgrade(
    tmp_path: Path,
) -> None:
    cfg = _whole_chapter_cfg(tmp_path)
    backend = _B3MockBackend(
        audit_issues=[], reaudit_issues=[],
        entity_payload=ENTITY_PAYLOAD_MIXED,
    )
    result = _run_with_b3(cfg, backend)
    assert result.step6["entity_context_enabled"] is True

    report_path = cfg.out_dir / "entity_context_validation_report.json"
    assert report_path.exists()
    report = _read_json(report_path)
    assert report["schema"] == VALIDATION_REPORT_SCHEMA
    entries = report["entries"]
    # The fully-valid entity produced no entry; the dead-PID claim was
    # dropped and the verified relation was downgraded — both with reasons.
    assert [e["entity"] for e in entries] == ["the ghost", "the shadow"]
    dropped = next(e for e in entries if e["action"] == "dropped")
    downgraded = next(e for e in entries if e["action"] == "downgraded")
    assert dropped["entity"] == "the ghost"
    assert "dead PID p99999" in dropped["reason"]
    assert downgraded["entity"] == "the shadow"
    assert "same_entity relation is semantic" in downgraded["reason"]
    # Every entry carries the entity/claim/action/reason quad.
    for entry in entries:
        assert {"entity", "claim", "action", "reason"} <= set(entry)

    # The validated context still carries the surviving entities.
    cache = _read_json(cfg.out_dir / "entity_context_cache.json")
    assert cache["schema"] == "pact-v4-entity-context-cache/v1"


def test_b3_entity_extractor_failure_writes_no_validation_report(
    tmp_path: Path,
) -> None:
    cfg = _whole_chapter_cfg(tmp_path)
    backend = _B3MockBackend(
        audit_issues=[], reaudit_issues=[], fail_entity=True,
    )
    result = _run_with_b3(cfg, backend)

    # The extractor never reached validation -> B3 failed; neither the
    # entity cache nor the validation report exists.
    assert result.step6["status"] == "failed"
    assert not (cfg.out_dir / "entity_context_cache.json").exists()
    assert not (cfg.out_dir / "entity_context_validation_report.json").exists()


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
    # F5: the audit identity block carries EVERY authoritative B3 knob —
    # repair policy, prompt/harness/extractor versions — so changing any of
    # them invalidates cache/resume (a stale cached repaired map can never
    # be reused under a different repair policy).
    assert a.values["audit"] == {
        "run": True,
        "entity_context_enabled": True,
        "max_input_tokens": 3600,
        "max_tokens": 12000,
        "overlap_tokens": 400,
        "reasoning_budget": 8192,
        "repair_findings_cap": 100,
        "repair_microbatch_trigger": 4,
        "repair_microbatch_target": 4,
        "repair_context_window": 3,
        "repair_context_window_by_category": {
            "invented_gender": 10, "referent": 10, "omission": 10,
        },
        "repair_reaudit_neighbour_window": 2,
        "repair_reaudit_chunk": {
            "max_input_tokens": 3600,
            "overlap_tokens": 400,
            "min_overlap_pairs": 2,
            "max_overlap_pairs": 6,
            "delta_format": "pact-v4-reaudit-delta/v1",
        },
        "repair_reaudit_max_tokens": 20000,
        "repair_reaudit_retry": {"max_retries": 2, "base_delay_seconds": 1.0},
        "prompt_version": "pact-v4-reviewer-qwen-audit/v4.1",
        "harness_version": "4.1",
        "extractor_version": "pact-v4-entity-extractor/v1",
        # CANDIDATE-MERGE (t_0ffe56e1, RV2 HIGH finding): the REPAIR prompt
        # version participates in the identity — a cache written under a
        # different repair prompt must never replay the repaired map.
        "repair_prompt_version": "pact-v4-repair-as-verifier/v4",
        "repair_harness_version": "1.0",
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


def test_b3_repair_policy_knobs_part_of_config_identity(tmp_path: Path) -> None:
    # F5 mutation test: every repair-policy knob and prompt/extractor
    # version participates in the config identity — a policy change can
    # never silently reuse a stale cached repaired map.
    base = _whole_chapter_cfg(tmp_path)
    base_id = base.to_config_artifact(model_profile="test").config_identity
    mutations = {
        "repair_findings_cap": dict(audit_repair_findings_cap=7),
        "microbatch_trigger": dict(audit_repair_microbatch_trigger=6),
        "microbatch_target": dict(audit_repair_microbatch_target=2),
        "repair_context_window": dict(audit_repair_context_window=8),
        "repair_context_window_by_category": dict(
            audit_repair_context_window_by_category={"invented_gender": 12}
        ),
        "reaudit_neighbour_window": dict(audit_repair_reaudit_neighbour_window=4),
        "reaudit_chunk_max_input": dict(audit_repair_reaudit_max_input_tokens=1800),
        "reaudit_chunk_overlap": dict(audit_repair_reaudit_overlap_tokens=200),
        "reaudit_delta_format": dict(audit_repair_reaudit_delta_format="pact-v4-reaudit-delta/v2"),
        "reaudit_max_tokens": dict(audit_repair_reaudit_max_tokens=25000),
        "reaudit_max_retries": dict(audit_repair_reaudit_max_retries=5),
        "reaudit_base_delay_seconds": dict(audit_repair_reaudit_base_delay_seconds=2.5),
        "reasoning_budget": dict(audit_reasoning_budget=4096),
        "prompt_version": dict(audit_prompt_version="pact-v4-reviewer-qwen-audit/v9.9"),
        "harness_version": dict(audit_harness_version="9.9"),
        "extractor_version": dict(audit_extractor_version="pact-v4-entity-extractor/v9.9"),
        # CANDIDATE-MERGE (t_0ffe56e1, RV2 HIGH finding): the REPAIR prompt
        # version is identity-bearing — flipping it invalidates the cached
        # repaired map (F5).
        "repair_prompt_version": dict(
            audit_repair_prompt_version="pact-v4-repair-as-verifier/v9.9"
        ),
        "repair_harness_version": dict(audit_repair_harness_version="9.9"),
    }
    for label, overrides in mutations.items():
        mutated = _whole_chapter_cfg(tmp_path, **overrides)
        mutated_id = mutated.to_config_artifact(model_profile="test").config_identity
        assert mutated_id != base_id, f"{label} mutation did not change identity"


def test_b3_reaudit_budget_and_retry_wired_from_run_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # RV 71b7cbc HIGH finding (F5): the production B3 path must carry the
    # re-audit output budget AND the bounded retry policy from the run
    # config through B3AuditRepairConfig into SelectiveRepairConfig — a
    # cache produced under a different budget/policy must never replay.
    import pact_full_pipeline_runner_v1.v4_phase12_strict_run as strict_run_mod

    cfg = _whole_chapter_cfg(
        tmp_path,
        audit_repair_reaudit_max_tokens=25000,
        audit_repair_reaudit_max_retries=5,
        audit_repair_reaudit_base_delay_seconds=2.5,
    )
    backend = _B3MockBackend()
    monkeypatch.setattr(strict_run_mod, "build_role_backend", lambda _b, _r: backend)
    bundle = strict_run_mod._build_b3_audit_repair(cfg, None, None)
    assert bundle is not None
    assert bundle._config.repair_reaudit_max_tokens == 25000
    assert bundle._config.repair_reaudit_max_retries == 5
    assert bundle._config.repair_reaudit_base_delay_seconds == 2.5


def test_b3_repair_prompt_version_wired_from_run_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # CANDIDATE-MERGE (t_0ffe56e1, RV2 HIGH finding, F5): the production B3
    # path must carry the REPAIR prompt/harness version from the run config
    # through B3AuditRepairConfig into the repair evaluator — a cache
    # produced under a different repair prompt must never replay (defaults
    # mirror the module constants).
    import pact_full_pipeline_runner_v1.v4_phase12_strict_run as strict_run_mod

    cfg = _whole_chapter_cfg(
        tmp_path,
        audit_repair_prompt_version="pact-v4-repair-as-verifier/v9.9",
        audit_repair_harness_version="9.9",
    )
    backend = _B3MockBackend()
    monkeypatch.setattr(strict_run_mod, "build_role_backend", lambda _b, _r: backend)
    bundle = strict_run_mod._build_b3_audit_repair(cfg, None, None)
    assert bundle is not None
    assert bundle._config.repair_prompt_version == "pact-v4-repair-as-verifier/v9.9"
    assert bundle._config.repair_harness_version == "9.9"

    default_bundle = strict_run_mod._build_b3_audit_repair(
        _whole_chapter_cfg(tmp_path), None, None,
    )
    assert default_bundle._config.repair_prompt_version == "pact-v4-repair-as-verifier/v4"
    assert default_bundle._config.repair_harness_version == "1.0"


def test_b3_reaudit_chunk_settings_wired_from_run_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # REPAIR-CTX (t_97b31f81, F5): the re-audit chunk/overlap settings and
    # the REPAIRED CHANGES delta format are wired from the run config through
    # B3AuditRepairConfig into SelectiveRepairConfig — a cache produced under
    # a different chunk/delta policy must never replay (defaults mirror the
    # module constants).
    import pact_full_pipeline_runner_v1.v4_phase12_strict_run as strict_run_mod

    cfg = _whole_chapter_cfg(
        tmp_path,
        audit_repair_reaudit_max_input_tokens=1800,
        audit_repair_reaudit_overlap_tokens=200,
        audit_repair_reaudit_delta_format="pact-v4-reaudit-delta/v2",
    )
    backend = _B3MockBackend()
    monkeypatch.setattr(strict_run_mod, "build_role_backend", lambda _b, _r: backend)
    bundle = strict_run_mod._build_b3_audit_repair(cfg, None, None)
    assert bundle is not None
    assert bundle._config.repair_reaudit_max_input_tokens == 1800
    assert bundle._config.repair_reaudit_overlap_tokens == 200
    assert bundle._config.repair_reaudit_delta_format == "pact-v4-reaudit-delta/v2"

    default_bundle = strict_run_mod._build_b3_audit_repair(
        _whole_chapter_cfg(tmp_path), None, None,
    )
    assert default_bundle._config.repair_reaudit_max_input_tokens == 3600
    assert default_bundle._config.repair_reaudit_delta_format == "pact-v4-reaudit-delta/v1"


def test_b3_repair_context_window_wired_from_run_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # REPAIR-CTX (t_97b31f81, F5): the production B3 path must carry the
    # local-context window from the run config through B3AuditRepairConfig
    # into SelectiveRepairConfig — a cache produced under a different window
    # must never replay (the default mirrors the module constant).
    import pact_full_pipeline_runner_v1.v4_phase12_strict_run as strict_run_mod

    cfg = _whole_chapter_cfg(tmp_path, audit_repair_context_window=8)
    backend = _B3MockBackend()
    monkeypatch.setattr(strict_run_mod, "build_role_backend", lambda _b, _r: backend)
    bundle = strict_run_mod._build_b3_audit_repair(cfg, None, None)
    assert bundle is not None
    assert bundle._config.repair_context_window == 8

    default_bundle = strict_run_mod._build_b3_audit_repair(
        _whole_chapter_cfg(tmp_path), None, None,
    )
    assert default_bundle._config.repair_context_window == 3


def test_b3_per_category_windows_wired_from_run_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # REPAIR-2 (t_768537b9, F5): the per-category window map is wired from
    # the run config through B3AuditRepairConfig into SelectiveRepairConfig —
    # a cache produced under a different per-category window must never
    # replay (defaults mirror the module constant).
    import pact_full_pipeline_runner_v1.v4_phase12_strict_run as strict_run_mod

    cfg = _whole_chapter_cfg(
        tmp_path,
        audit_repair_context_window_by_category={"invented_gender": 12},
    )
    backend = _B3MockBackend()
    monkeypatch.setattr(strict_run_mod, "build_role_backend", lambda _b, _r: backend)
    bundle = strict_run_mod._build_b3_audit_repair(cfg, None, None)
    assert bundle is not None
    assert bundle._config.repair_context_window_by_category == {
        "invented_gender": 12,
    }

    default_bundle = strict_run_mod._build_b3_audit_repair(
        _whole_chapter_cfg(tmp_path), None, None,
    )
    assert default_bundle._config.repair_context_window_by_category == {
        "invented_gender": 10, "referent": 10, "omission": 10,
    }


def test_b3_reaudit_request_carries_configured_budget_and_retry(tmp_path: Path) -> None:
    # RV 71b7cbc HIGH finding: run the full production B3 flow with a
    # non-default re-audit budget/retry; the re-audit backend request must
    # carry max_output_tokens == configured budget and an EMPTY re-audit
    # body must be retried max_retries+1 times before debt (never released).
    cfg = _whole_chapter_cfg(tmp_path)

    class _EmptyReauditBackend(_B3MockBackend):
        def complete(self, request: CompletionRequest) -> CompletionResponse:
            # Record EVERY call (including re-audit retries) and serve the
            # empty re-audit body; the non-reaudit roles reuse the base
            # dispatch. The base appends too, so only append for the
            # re-audit calls we intercept here (no double-counting).
            if "reaudit" in (request.label or ""):
                self.requests.append(request)
                return CompletionResponse(
                    text="", model="qwen-3.6-35b", finish_reason="stop"
                )
            return super().complete(request)

    backend = _EmptyReauditBackend(
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
    result = _run_with_b3(
        cfg, backend,
        config_override=B3AuditRepairConfig(
            repair_reaudit_max_tokens=25000,
            repair_reaudit_max_retries=3,
            repair_reaudit_base_delay_seconds=0.0,
        ),
    )

    # The re-audit request carried the configured output budget.
    reaudit_requests = [r for r in backend.requests if "reaudit" in (r.label or "")]
    assert reaudit_requests, "re-audit request expected after a committed repair"
    assert all(r.max_output_tokens == 25000 for r in reaudit_requests)

    # Empty re-audit JSON retried max_retries+1 times (4 attempts), then debt.
    assert backend.reaudit_calls() == 4
    assert result.step8["released_as_audited"] is False
    assert any("failed re-audit" in d for d in result.step8["debt_trace"])


def test_b3_audit_repair_config_payload_carries_reaudit_budget_and_retry() -> None:
    # The B3 config/report payload carries the re-audit budget and retry
    # policy (RV 71b7cbc, F5) — defaults and explicit overrides.
    payload = B3AuditRepairConfig().to_payload()
    assert payload["repair_reaudit_max_tokens"] == 20000
    assert payload["repair_reaudit_retry"] == {
        "max_retries": 2, "base_delay_seconds": 1.0,
    }
    custom = B3AuditRepairConfig(
        repair_reaudit_max_tokens=25000,
        repair_reaudit_max_retries=5,
        repair_reaudit_base_delay_seconds=2.5,
    ).to_payload()
    assert custom["repair_reaudit_max_tokens"] == 25000
    assert custom["repair_reaudit_retry"] == {
        "max_retries": 5, "base_delay_seconds": 2.5,
    }


def test_b3_config_payload_carries_repair_prompt_version() -> None:
    # CANDIDATE-MERGE (t_0ffe56e1, RV2 HIGH finding, F5): the REPAIR prompt
    # version is part of the B3 config payload/report (the repaired map is a
    # function of the repair prompt) — defaults mirror the module constant
    # and explicit overrides are preserved.
    from pact_v4.repair.selective_repair import (
        REPAIR_HARNESS_VERSION,
        REPAIR_PROMPT_VERSION,
    )
    assert REPAIR_PROMPT_VERSION == "pact-v4-repair-as-verifier/v4"
    payload = B3AuditRepairConfig().to_payload()
    assert payload["repair_prompt_version"] == "pact-v4-repair-as-verifier/v4"
    assert payload["repair_harness_version"] == REPAIR_HARNESS_VERSION
    custom = B3AuditRepairConfig(
        repair_prompt_version="pact-v4-repair-as-verifier/v9.9",
        repair_harness_version="9.9",
    ).to_payload()
    assert custom["repair_prompt_version"] == "pact-v4-repair-as-verifier/v9.9"
    assert custom["repair_harness_version"] == "9.9"


def test_b3_config_payload_carries_repair_context_window() -> None:
    # REPAIR-CTX (t_97b31f81): the local-context window is part of the B3
    # config payload (identity) — default 3 (owner 2026-08-12) and overrides.
    from pact_v4.runtime.prompts_runtime import DEFAULT_REPAIR_CONTEXT_WINDOW
    assert DEFAULT_REPAIR_CONTEXT_WINDOW == 3
    assert B3AuditRepairConfig().to_payload()["repair_context_window"] == 3
    custom = B3AuditRepairConfig(repair_context_window=8).to_payload()
    assert custom["repair_context_window"] == 8


def test_b3_config_payload_carries_per_category_windows() -> None:
    # REPAIR-2 (t_768537b9, F5): the per-category window map is part of the
    # B3 config payload (identity) — default widens invented_gender /
    # referent / omission (owner 2026-08-12), changed_fact/addition stay ±3.
    from pact_v4.runtime.prompts_runtime import (
        DEFAULT_REPAIR_CONTEXT_WINDOW_BY_CATEGORY,
    )
    assert DEFAULT_REPAIR_CONTEXT_WINDOW_BY_CATEGORY == {
        "invented_gender": 10, "referent": 10, "omission": 10,
    }
    payload = B3AuditRepairConfig().to_payload()
    assert payload["repair_context_window_by_category"] == {
        "invented_gender": 10, "referent": 10, "omission": 10,
    }
    custom = B3AuditRepairConfig(
        repair_context_window_by_category={"referent": 25}
    ).to_payload()
    assert custom["repair_context_window_by_category"] == {"referent": 25}


def test_b3_config_payload_carries_reaudit_chunk_settings() -> None:
    # REPAIR-CTX (t_97b31f81, F5): the re-audit chunk/overlap settings and
    # the REPAIRED CHANGES delta format are identity-bearing — defaults and
    # overrides.
    payload = B3AuditRepairConfig().to_payload()
    assert payload["repair_reaudit_chunk"] == {
        "max_input_tokens": 3600,
        "overlap_tokens": 400,
        "min_overlap_pairs": 2,
        "max_overlap_pairs": 6,
        "delta_format": "pact-v4-reaudit-delta/v1",
    }
    assert "repair_reaudit_full_threshold" not in payload  # whole-chapter mode cancelled
    custom = B3AuditRepairConfig(
        repair_reaudit_max_input_tokens=1800,
        repair_reaudit_overlap_tokens=200,
        repair_reaudit_delta_format="pact-v4-reaudit-delta/v2",
    ).to_payload()
    assert custom["repair_reaudit_chunk"] == {
        "max_input_tokens": 1800,
        "overlap_tokens": 200,
        "min_overlap_pairs": 2,
        "max_overlap_pairs": 6,
        "delta_format": "pact-v4-reaudit-delta/v2",
    }


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

    # V4.2 R: Russian editor ON by default; --no-russian-editor flips it off
    # (4.1 scheme) and is part of the config identity.
    assert default_cfg.russian_editor_enabled is True
    no_r = _build_run_config(
        build_argparser().parse_args(base_args + ["--no-russian-editor"]), None
    )
    assert no_r.russian_editor_enabled is False
    assert (
        default_cfg.to_config_artifact(model_profile="test").config_identity
        != no_r.to_config_artifact(model_profile="test").config_identity
    )

    # --run-audit and --skip-audit are mutually exclusive at parse time.
    with pytest.raises(SystemExit):
        build_argparser().parse_args(base_args + ["--run-audit", "--skip-audit"])


# ---------------------------------------------------------------------------
# F1 (B3 review): publication gate — repair_complete=False / failed re-audit
# must NEVER produce step8 complete/released_as_audited=True, including a
# cache replay of a failed repair.
# ---------------------------------------------------------------------------


def test_b3_repair_failed_never_released(tmp_path: Path) -> None:
    # Audit finds a real issue, but the repair response is empty/invalid ->
    # batch FAILED -> repair_complete=False. The chapter must NOT be
    # released as audited (debt explicit, not PASS).
    cfg = _whole_chapter_cfg(tmp_path)
    backend = _B3MockBackend(
        audit_issues=[{
            "id": "p00001", "category": "addition", "severity": "major",
            "confidence": "high", "note": "дублирование слова",
            "excerpt": "номер1 номер1",
        }],
        repair_results=[],  # empty repair response -> batch FAILED (missing answers)
        reaudit_issues=[],
    )
    result = _run_with_b3(cfg, backend)

    assert result.step6["audit_complete"] is True
    assert result.step7["repair_complete"] is False
    assert result.step7["status"] == "incomplete"
    assert result.step8["status"] == "accepted_degraded"
    assert result.step8["released_as_audited"] is False
    assert result.step8["repair_complete"] is False
    # Debt is explicit and NOT a PASS: no repair was committed.
    assert result.step7["committed_pids"] == []

    # Journal gate agrees: not released.
    journal = [
        json.loads(line)
        for line in (cfg.out_dir / "audit_journal.ndjson").read_text(encoding="utf-8").splitlines()
    ]
    gate = next(e for e in journal if e["event"] == "gate")
    assert gate["released_as_audited"] is False
    assert gate["repair_complete"] is False

    # The repaired map stays the raw translation (no commit), but the
    # terminal state is honest.
    repaired = _read_json(cfg.out_dir / "translations_repaired.json")
    assert repaired["translations"]["p00001"] == "Перевод номер1 номер1"


def test_b3_reaudit_failed_never_released(tmp_path: Path) -> None:
    # Repair commits, but the post-repair re-audit FAILS (transport) ->
    # repair_complete=False (selective_repair fail-closed). The chapter
    # must NOT be released as audited.
    cfg = _whole_chapter_cfg(tmp_path)

    class _ReauditFailingBackend(_B3MockBackend):
        def complete(self, request: CompletionRequest) -> CompletionResponse:
            if "reaudit" in (request.label or ""):
                raise CompletionError("simulated re-audit transport failure")
            return super().complete(request)

    backend = _ReauditFailingBackend(
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

    assert result.step6["audit_complete"] is True
    assert result.step7["repair_complete"] is False
    assert result.step8["status"] == "accepted_degraded"
    assert result.step8["released_as_audited"] is False
    assert "failed re-audit" in result.step8["debt_trace"] or any(
        "re-audit" in d for d in result.step8["debt_trace"]
    )


def test_b3_cache_replay_of_failed_repair_not_released(tmp_path: Path) -> None:
    # F1 cache replay: a cache written with repair_complete=False must be
    # replayed as NOT released — the cache hit must never upgrade debt into
    # an audited release.
    cfg = _whole_chapter_cfg(tmp_path)
    first = _B3MockBackend(
        audit_issues=[{
            "id": "p00001", "category": "addition", "severity": "major",
            "confidence": "high", "note": "дублирование слова",
            "excerpt": "номер1 номер1",
        }],
        repair_results=[],  # empty -> repair fails
        reaudit_issues=[],
    )
    first_result = _run_with_b3(cfg, first)
    assert first_result.step8["released_as_audited"] is False

    # Resume: full cache hit (identity matches), but the cached repair is
    # incomplete -> replayed as accepted_degraded / NOT released.
    second = _B3MockBackend(
        audit_issues=[],
        repair_results=[],
        reaudit_issues=[],
    )
    second_result = _run_with_b3(cfg, second)
    assert second_result.step6["from_cache"] is True
    assert second_backend_calls(second) == 0
    assert second_result.step8["status"] == "accepted_degraded"
    assert second_result.step8["released_as_audited"] is False
    assert second_result.step8["from_cache"] is True

    journal = [
        json.loads(line)
        for line in (cfg.out_dir / "audit_journal.ndjson").read_text(encoding="utf-8").splitlines()
    ]
    gates = [e for e in journal if e["event"] == "gate"]
    assert gates[-1]["released_as_audited"] is False
    assert gates[-1]["from_cache"] is True


def second_backend_calls(backend: _B3MockBackend) -> int:
    return backend.entity_calls() + backend.audit_calls() + backend.repair_calls() + backend.reaudit_calls()


# ---------------------------------------------------------------------------
# F4 (B3 review): cache integrity — tampered translations_repaired (extra /
# missing / reordered PIDs, non-string values, hash mismatch) is a cache
# MISS: the audit re-runs and the tampered map is never published.
# ---------------------------------------------------------------------------


def _tamper_cache(cfg: StrictRunConfig, *, mutate: Any) -> None:
    path = cfg.out_dir / "audit_cache_b3.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_b4_cache_tamper_extra_pid_is_miss(tmp_path: Path) -> None:
    cfg = _whole_chapter_cfg(tmp_path)

    def _factory() -> _B3MockBackend:
        return _B3MockBackend(
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

    _run_with_b3(cfg, _factory())
    # Tamper: extra foreign PID + TAMPERED value on p00001, identity intact.
    def _tamper(payload: dict) -> None:
        repaired = dict(payload["translations_repaired"])
        repaired["foreign_PID_999"] = "TAMPERED"
        repaired["p00001"] = "TAMPERED"
        payload["translations_repaired"] = repaired

    _tamper_cache(cfg, mutate=_tamper)

    resume = _B3MockBackend(
        audit_issues=[],
        repair_results=[],
        reaudit_issues=[],
    )
    result = _run_with_b3(cfg, resume)
    # Tampered cache rejected -> full re-run (0 from_cache), audit re-ran.
    assert result.step6["from_cache"] is False
    assert resume.audit_calls() == 1
    # The tampered map was NOT published.
    final = _read_json(result.translations_path)
    assert "foreign_PID_999" not in final
    assert final["p00001"] != "TAMPERED"


def test_b4_cache_tamper_missing_pid_is_miss(tmp_path: Path) -> None:
    cfg = _whole_chapter_cfg(tmp_path)

    def _factory() -> _B3MockBackend:
        return _B3MockBackend(audit_issues=[], reaudit_issues=[])

    _run_with_b3(cfg, _factory())

    def _tamper(payload: dict) -> None:
        repaired = dict(payload["translations_repaired"])
        repaired.pop("p00001", None)
        payload["translations_repaired"] = repaired

    _tamper_cache(cfg, mutate=_tamper)
    resume = _B3MockBackend(audit_issues=[], reaudit_issues=[])
    result = _run_with_b3(cfg, resume)
    assert result.step6["from_cache"] is False
    assert resume.audit_calls() == 1


def test_b4_cache_tamper_reordered_pids_is_miss(tmp_path: Path) -> None:
    cfg = _whole_chapter_cfg(tmp_path)

    def _factory() -> _B3MockBackend:
        return _B3MockBackend(audit_issues=[], reaudit_issues=[])

    _run_with_b3(cfg, _factory())

    def _tamper(payload: dict) -> None:
        repaired = dict(payload["translations_repaired"])
        keys = list(repaired)
        keys.reverse()
        payload["translations_repaired"] = {k: repaired[k] for k in keys}

    _tamper_cache(cfg, mutate=_tamper)
    resume = _B3MockBackend(audit_issues=[], reaudit_issues=[])
    result = _run_with_b3(cfg, resume)
    assert result.step6["from_cache"] is False
    assert resume.audit_calls() == 1


def test_b4_cache_tamper_non_string_value_is_miss(tmp_path: Path) -> None:
    cfg = _whole_chapter_cfg(tmp_path)

    def _factory() -> _B3MockBackend:
        return _B3MockBackend(audit_issues=[], reaudit_issues=[])

    _run_with_b3(cfg, _factory())

    def _tamper(payload: dict) -> None:
        repaired = dict(payload["translations_repaired"])
        repaired["p00001"] = {"not": "a string"}
        payload["translations_repaired"] = repaired

    _tamper_cache(cfg, mutate=_tamper)
    resume = _B3MockBackend(audit_issues=[], reaudit_issues=[])
    result = _run_with_b3(cfg, resume)
    assert result.step6["from_cache"] is False
    assert resume.audit_calls() == 1


def test_b4_cache_hash_mismatch_old_schema_is_miss(tmp_path: Path) -> None:
    cfg = _whole_chapter_cfg(tmp_path)

    def _factory() -> _B3MockBackend:
        return _B3MockBackend(audit_issues=[], reaudit_issues=[])

    _run_with_b3(cfg, _factory())

    def _tamper(payload: dict) -> None:
        # Old schema: drop the canonical repaired-map hash -> must miss.
        payload.pop("translations_repaired_hash", None)

    _tamper_cache(cfg, mutate=_tamper)
    resume = _B3MockBackend(audit_issues=[], reaudit_issues=[])
    result = _run_with_b3(cfg, resume)
    assert result.step6["from_cache"] is False
    assert resume.audit_calls() == 1


def test_b4_cache_hash_mismatch_tampered_value_is_miss(tmp_path: Path) -> None:
    cfg = _whole_chapter_cfg(tmp_path)

    def _factory() -> _B3MockBackend:
        return _B3MockBackend(audit_issues=[], reaudit_issues=[])

    _run_with_b3(cfg, _factory())

    def _tamper(payload: dict) -> None:
        # Value tampered but hash NOT updated -> recomputed hash mismatch.
        payload["translations_repaired"]["p00001"] = "TAMPERED"

    _tamper_cache(cfg, mutate=_tamper)
    resume = _B3MockBackend(audit_issues=[], reaudit_issues=[])
    result = _run_with_b3(cfg, resume)
    assert result.step6["from_cache"] is False
    assert resume.audit_calls() == 1


# ---------------------------------------------------------------------------
# F6 (B3 review): a structurally corrupt entity cache is a MISS, never an
# abort — B3 must recompute instead of raising inside EntityContextCache.
# ---------------------------------------------------------------------------


def test_b6_malformed_entity_cache_is_miss_not_abort(tmp_path: Path) -> None:
    cfg = _whole_chapter_cfg(tmp_path)
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    # JSON list (not an object) — AttributeError path in from_payload.
    (cfg.out_dir / "entity_context_cache.json").write_text(
        "[1, 2, 3]", encoding="utf-8"
    )
    backend = _B3MockBackend(audit_issues=[], reaudit_issues=[])
    result = _run_with_b3(cfg, backend)
    # No abort: entity prepass re-ran (1 call), audit completed and released.
    assert backend.entity_calls() == 1
    assert result.step6["audit_complete"] is True
    assert result.step8["released_as_audited"] is True


def test_b6_malformed_entity_cache_missing_key_is_miss_not_abort(tmp_path: Path) -> None:
    cfg = _whole_chapter_cfg(tmp_path)
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    # Object with an entry missing the 'context' key — KeyError path.
    (cfg.out_dir / "entity_context_cache.json").write_text(
        json.dumps({
            "schema": "pact-v4-entity-context-cache/v1",
            "entries": [{"key": "abc"}],
        }),
        encoding="utf-8",
    )
    backend = _B3MockBackend(audit_issues=[], reaudit_issues=[])
    result = _run_with_b3(cfg, backend)
    assert backend.entity_calls() == 1
    assert result.step8["released_as_audited"] is True


def test_b6_malformed_entity_cache_type_error_is_miss_not_abort(tmp_path: Path) -> None:
    cfg = _whole_chapter_cfg(tmp_path)
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    # 'entries' is a string, not a list — TypeError path in from_payload.
    (cfg.out_dir / "entity_context_cache.json").write_text(
        json.dumps({
            "schema": "pact-v4-entity-context-cache/v1",
            "entries": "not-a-list",
        }),
        encoding="utf-8",
    )
    backend = _B3MockBackend(audit_issues=[], reaudit_issues=[])
    result = _run_with_b3(cfg, backend)
    assert backend.entity_calls() == 1
    assert result.step8["released_as_audited"] is True


# ---------------------------------------------------------------------------
# F7 (B3 review): journal causality — audit_chunk_started is emitted BEFORE
# the model call and a terminal audit_chunk_done (incl. failures) after it.
# ---------------------------------------------------------------------------


def _journal_events(out_dir: Path) -> list:
    return [
        json.loads(line)
        for line in (out_dir / "audit_journal.ndjson").read_text(encoding="utf-8").splitlines()
    ]


def test_b7_journal_started_before_done_success(tmp_path: Path) -> None:
    cfg = _whole_chapter_cfg(tmp_path)
    backend = _B3MockBackend(audit_issues=[], reaudit_issues=[])
    _run_with_b3(cfg, backend)
    events = _journal_events(cfg.out_dir)
    started = [e for e in events if e["event"] == "audit_chunk_started"]
    done = [e for e in events if e["event"] == "audit_chunk_done"]
    assert started and done
    # The FIRST started must precede the FIRST done (causality).
    assert events.index(started[0]) < events.index(done[0])
    for e in done:
        assert e["status"] in ("GOOD", "GOOD_RETRIED")


def test_b7_journal_started_before_failed_done_transport_error(tmp_path: Path) -> None:
    cfg = _whole_chapter_cfg(tmp_path)
    backend = _B3MockBackend(fail_audit=True)
    _run_with_b3(cfg, backend)
    events = _journal_events(cfg.out_dir)
    started = [e for e in events if e["event"] == "audit_chunk_started"]
    done = [e for e in events if e["event"] == "audit_chunk_done"]
    assert started, "started event must exist even when the chunk fails"
    assert done, "terminal done event must exist after a failed chunk"
    assert events.index(started[0]) < events.index(done[0])
    failed = [e for e in done if e["status"] == "TRANSPORT_ERROR"]
    assert failed, "terminal TRANSPORT_ERROR done must be recorded"
    assert failed[0]["error"]


def test_b7_r_editor_chunk_events_in_journal(tmp_path: Path) -> None:
    """A2 (run_011): R emits per-chunk ``r_editor_chunk_started`` /
    ``r_editor_chunk_done`` journal events — a failed R chunk carries the
    REAL reason (parse/transport error), not just r_editor_started/done."""
    from pact_v4.pipeline.b3_audit_repair import B3AuditRepairConfig

    cfg = _whole_chapter_cfg(tmp_path)

    class _FlakyREditorBackend(_B3MockBackend):
        """Serves a valid first r_editor chunk and a BROKEN second one."""
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._r_editor_served = 0

        def complete(self, request: CompletionRequest) -> CompletionResponse:
            if "russian_editor" in (request.label or ""):
                self.requests.append(request)
                self._r_editor_served += 1
                if self._r_editor_served > 1:
                    # run_011 shape: the model omits ``class`` -> parse fails
                    # fail-closed 'unknown edit class' — the journal must name
                    # it. The edit targets the FIRST pid of THIS chunk and
                    # echoes its exact current text (chunk 2 = p00005..).
                    first_pid, current_text = self._first_chunk_pair(request)
                    return _ok_response({"edits": [{
                        "pid": first_pid,
                        "original": current_text,
                        "rewritten": current_text + " испр.",
                        "reason": "дубль",
                        # no "class" key
                    }]})
                return _ok_response({"edits": []})
            return super().complete(request)

        @staticmethod
        def _first_chunk_pair(request: CompletionRequest) -> tuple:
            import re as _re
            in_edit_pairs = False
            for line in request.messages[0].content.splitlines():
                line = line.strip()
                if line.startswith("EDIT_PAIRS"):
                    in_edit_pairs = True
                    continue
                if not in_edit_pairs or ":" not in line:
                    continue
                pid, _, text = line.partition(":")
                pid = pid.strip()
                text = text.strip()
                # Only real PIDs (p00001-style) inside EDIT_PAIRS, never
                # schema lines ("pid: string ...") or CONTEXT_ONLY pids.
                if _re.fullmatch(r"p\d{5}", pid) and text:
                    return pid, text
            return "p00001", ""

    backend = _FlakyREditorBackend(
        audit_issues=[], repair_results=[], reaudit_issues=[],
    )
    _run_with_b3(
        cfg, backend,
        config_override=B3AuditRepairConfig(
            entity_context_enabled=False,
            russian_editor_enabled=True,
            russian_editor_chunk_size=4,  # 2 chunks for 8 pids
        ),
    )
    events = _journal_events(cfg.out_dir)
    started = [e for e in events if e["event"] == "r_editor_chunk_started"]
    done = [e for e in events if e["event"] == "r_editor_chunk_done"]
    assert len(started) == 2 and len(done) == 2
    # Causality: each started precedes its matching done.
    assert events.index(started[0]) < events.index(done[0])
    # The SECOND chunk failed and the journal names the REAL reason.
    failed = [e for e in done if e["status"] == "FAILED"]
    assert failed, "a failed R chunk must carry a terminal done event"
    assert failed[0]["error"], "the fail reason must be in the journal"
    assert "class" in failed[0]["error"]
    # R raw artifacts are on disk for BOTH chunks (A1 trail).
    assert (cfg.out_dir / "r_editor_chunk1_raw.txt").exists()
    assert (cfg.out_dir / "r_editor_chunk2_raw.txt").exists()
    assert (cfg.out_dir / "r_editor_chunk2_reasoning.txt").exists()


def test_b3_repair_and_reaudit_raw_artifacts_written(tmp_path: Path) -> None:
    """B1/C1 (run_011): repair-batch + re-audit raw/reasoning artifacts appear
    in the out_dir next to the audit cache."""
    cfg = _whole_chapter_cfg(tmp_path)
    backend = _B3MockBackend(
        audit_issues=[{
            "id": "p00001", "category": "addition", "severity": "major",
            "confidence": "high", "note": "дублирование слова",
            "excerpt": "номер1 номер1",
        }],
        repair_results=[{
            "index": 1, "decision": "repair", "pid": "p00001",
            "repaired_translation": "<current> — убран дубль",
            "reason": "убрал дубль",
        }],
        reaudit_issues=[],
    )
    result = _run_with_b3(cfg, backend)
    assert result.step8["released_as_audited"] is True
    assert (cfg.out_dir / "b3_repair_batch1_raw.txt").exists()
    assert (cfg.out_dir / "b3_repair_batch1_reasoning.txt").exists()
    # REPAIR-CTX: the re-audit is chunked like the audit — per-chunk artifacts
    assert (cfg.out_dir / "b3_repair_reaudit_chunk1_raw.txt").exists()
    assert (cfg.out_dir / "b3_repair_reaudit_chunk1_reasoning.txt").exists()


def test_b3_truncated_repair_rejected_in_journal(tmp_path: Path) -> None:
    """B3 (run_011): a repair that truncates the PID text >60% is REJECTED —
    the batch FAILS with 'truncated repair' and the journal records it in the
    repair_round debt_trace (never committed, never released)."""
    cfg = _whole_chapter_cfg(tmp_path)
    backend = _B3MockBackend(
        audit_issues=[{
            "id": "p00001", "category": "addition", "severity": "major",
            "confidence": "high", "note": "дублирование слова",
            "excerpt": "номер1 номер1",
        }],
        # A FRAGMENT: ~6 chars vs the ~18-char current text -> <40% -> rejected.
        repair_results=[{
            "index": 1, "decision": "repair", "pid": "p00001",
            "repaired_translation": "дубль",
            "reason": "убрал дубль",
        }],
        reaudit_issues=[],
    )
    result = _run_with_b3(cfg, backend)
    # The truncated repair is REJECTED: the gate holds, nothing committed.
    assert result.step7["committed_pids"] == []
    assert result.step8["released_as_audited"] is False
    events = _journal_events(cfg.out_dir)
    rounds = [e for e in events if e["event"] == "repair_round"]
    assert rounds and any("truncated repair" in d for d in rounds[0]["debt_trace"])
    # The raw artifact preserves the fragment for diagnosis (B1 trail).
    raw = cfg.out_dir / "b3_repair_batch1_raw.txt"
    assert raw.exists()
    assert '"repaired_translation": "дубль"' in raw.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# F8 (B3 review): the artifact manifest advertises only artifacts that were
# actually created — skip/fail runs must not list nonexistent B3 outputs.
# ---------------------------------------------------------------------------


def test_b8_manifest_omits_b3_when_skipped(tmp_path: Path) -> None:
    cfg = _whole_chapter_cfg(tmp_path, run_audit=False)
    backend = _B3MockBackend()
    result = _run_with_b3(cfg, backend)
    artefacts = result.record["artefacts"]
    assert "b3_audit_journal" not in artefacts
    assert "b3_audit_cache" not in artefacts
    assert "b3_entity_context_cache" not in artefacts
    assert "b3_entity_validation_report" not in artefacts


def test_b8_manifest_omits_b3_when_no_machinery(tmp_path: Path) -> None:
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
    artefacts = result.record["artefacts"]
    assert "b3_audit_journal" not in artefacts
    assert "b3_audit_cache" not in artefacts
    assert "b3_entity_context_cache" not in artefacts
    assert "b3_entity_validation_report" not in artefacts


def test_b8_manifest_lists_b3_when_full_flow(tmp_path: Path) -> None:
    cfg = _whole_chapter_cfg(tmp_path)
    backend = _B3MockBackend(audit_issues=[], reaudit_issues=[])
    result = _run_with_b3(cfg, backend)
    artefacts = result.record["artefacts"]
    assert artefacts["b3_audit_journal"].endswith("audit_journal.ndjson")
    assert artefacts["b3_audit_cache"].endswith("audit_cache_b3.json")
    assert artefacts["b3_entity_context_cache"].endswith("entity_context_cache.json")
    assert artefacts["b3_entity_validation_report"].endswith(
        "entity_context_validation_report.json"
    )
    # The advertised files actually exist.
    for key in (
        "b3_audit_journal", "b3_audit_cache",
        "b3_entity_context_cache", "b3_entity_validation_report",
    ):
        assert Path(artefacts[key]).exists(), key


# ---------------------------------------------------------------------------
# F2 (RV2 B3 review): a NON-transport evaluator exception (pre/model-call
# failure — CoverageError/empty input, BudgetOverflowError, missing role)
# must leave a TERMINAL audit_failed event + fail-closed gate in the B3
# journal BEFORE the exception propagates. Previously the journal ended on
# audit_started/started with no terminal failure event.
# ---------------------------------------------------------------------------


def test_b3_evaluator_budget_overflow_writes_terminal_failure_event(
    tmp_path: Path,
) -> None:
    # F2 adversarial: a BudgetOverflowError raised INSIDE the evaluator
    # (before any model call — the audit input budget cannot fit the
    # chapter) propagates out of ChunkedAuditEvaluator. The B3 journal must
    # still record a terminal audit_failed event with the error AND a
    # fail-closed gate (audit_complete=False, released_as_audited=False)
    # before the exception is re-raised to the strict runner.
    cfg = _whole_chapter_cfg(tmp_path)
    backend = _B3MockBackend(audit_issues=[], reaudit_issues=[])
    result = _run_with_b3(
        cfg, backend,
        config_override=B3AuditRepairConfig(
            entity_context_enabled=False,
            # Zero input budget -> validate_input_budget computes
            # pair_budget = min(0, …) = 0 < 1 and raises BudgetOverflowError
            # inside the evaluator before any model call.
            max_input_tokens=0,
        ),
    )

    # The strict runner records the failed B3 step (never a crash).
    assert result.step6["status"] == "failed"
    assert result.step7["status"] == "failed"
    assert result.step8["status"] == "failed"

    # The B3 journal has a TERMINAL failure event + fail-closed gate.
    events = _journal_events(cfg.out_dir)
    names = [e["event"] for e in events]
    assert "audit_started" in names
    assert "audit_failed" in names
    assert "gate" in names
    failed = next(e for e in events if e["event"] == "audit_failed")
    assert "BudgetOverflowError" in failed["error"]
    assert failed["audit_complete"] is False
    gate = next(e for e in events if e["event"] == "gate")
    assert gate["audit_complete"] is False
    assert gate["released_as_audited"] is False
    assert "BudgetOverflowError" in gate["error"]
    # The terminal gate is the LAST event (nothing appended after it).
    assert names[-1] == "gate"


def test_b3_evaluator_coverage_error_writes_terminal_failure_event(
    tmp_path: Path,
) -> None:
    # F2 adversarial (CoverageError/empty input): a translation map missing
    # source PIDs raises CoverageError from pairs_from_maps BEFORE any model
    # call. The journal must terminate with audit_failed + fail-closed gate,
    # never end on audit_started alone.
    from pact_v4.audit.chunked_audit import CoverageError
    from pact_v4.phase1.models import SourceArtifact

    cfg = _whole_chapter_cfg(tmp_path)
    backend = _B3MockBackend(audit_issues=[], reaudit_issues=[])
    bundle = B3AuditRepair(
        audit_backend=backend,
        repair_backend=backend,
        config=B3AuditRepairConfig(
            entity_context_enabled=False,
            max_input_tokens=3600,
        ),
    )
    source = SourceArtifact(
        chapter_id=cfg.chapter_id,
        source=(("p00001", "Source one"), ("p00002", "Source two")),
    )
    with pytest.raises(CoverageError):
        bundle.run(
            chapter_id=cfg.chapter_id,
            source=source,
            snapshot_hash="snap-hash",
            # p00002 missing -> pairs_from_maps raises CoverageError.
            translation={"p00001": "Перевод один"},
            book_memory={},
            out_dir=cfg.out_dir,
            config_identity="cid",
            backend_identity_hash="bid",
        )

    # The B3 journal has a TERMINAL failure event + fail-closed gate even
    # though the exception propagated to the caller.
    events = _journal_events(cfg.out_dir)
    names = [e["event"] for e in events]
    assert "audit_started" in names
    assert "audit_failed" in names
    assert "gate" in names
    failed = next(e for e in events if e["event"] == "audit_failed")
    assert "CoverageError" in failed["error"]
    assert failed["audit_complete"] is False
    gate = next(e for e in events if e["event"] == "gate")
    assert gate["audit_complete"] is False
    assert gate["released_as_audited"] is False
    assert "CoverageError" in gate["error"]
    # The terminal gate is the LAST event (nothing appended after it).
    assert names[-1] == "gate"



# ---------------------------------------------------------------------------
# V4.2 R: Russian-only editor stage integration (card t_4707e6e5)
# ---------------------------------------------------------------------------


def test_b3_r_editor_runs_and_applies_safe_edits(tmp_path: Path) -> None:
    """R runs on the whole-chapter flow (default ON): SAFE edits are
    auto-applied to the audited map, REVIEW edits become candidates, the
    repair verifier accepts/rejects them, and the artifacts are written.

    Mock contract: r_editor_edits entries are ``(pid, klass, suffix)``; the
    mock derives ``original`` from the request's own EDIT_PAIRS text (the
    model must echo the exact current text)."""
    cfg = _whole_chapter_cfg(tmp_path)
    # p00001 typo (SAFE, applied), p00002 calque (REVIEW, candidate).
    backend = _B3MockBackend(
        r_editor_edits=[
            ("p00001", "typo", " — исправлено"),
            ("p00002", "calque", " — переформулировано"),
        ],
        audit_issues=[{
            "id": "p00003", "category": "addition", "severity": "major",
            "confidence": "high", "note": "дублирование",
            "excerpt": "номер3 номер3",
        }],
        # Audit finding index 1 (p00003) + review candidate index 2 (p00002).
        # ``<current>`` resolves to the request's full current text (the
        # mock echoes a FULL-LENGTH repair, as the verifier contract demands —
        # a fragment would be rejected by the run_011 preservation gate).
        repair_results=[
            {"index": 1, "decision": "repair", "pid": "p00003",
             "repaired_translation": "<current> — убран дубль",
             "reason": "убран дубль"},
            {"index": 2, "decision": "repair", "pid": "p00002",
             "repaired_translation": "<current> — переформулировано",
             "reason": "калька подтверждена"},
        ],
        reaudit_issues=[],
    )
    result = _run_with_b3(cfg, backend)
    assert backend.r_editor_calls() == 1
    assert result.step8["released_as_audited"] is True

    # SAFE edit applied to the audited/repaired map.
    repaired = _read_json(cfg.out_dir / "translations_repaired.json")
    repaired = repaired["translations"]
    assert repaired["p00001"].endswith("— исправлено")
    # REVIEW candidate accepted by the verifier -> committed.
    assert repaired["p00002"].endswith("— переформулировано")
    # Audit repair committed too (full-length text echoed, per the verifier
    # contract — never a fragment).
    assert repaired["p00003"].endswith("— убран дубль")
    assert len(repaired["p00003"]) > len(repaired["p00001"])  # paragraph kept

    # Artifacts written with identity.
    edited = _read_json(cfg.out_dir / "translations_edited.json")
    assert edited["schema"] == "pact-v4-translations-edited/v1"
    assert edited["translations"]["p00001"].endswith("— исправлено")
    # REVIEW NOT applied to the edited map (still the raw text).
    assert not edited["translations"]["p00002"].endswith("— переформулировано")
    candidates = _read_json(cfg.out_dir / "edit_candidates.json")
    assert candidates["schema"] == "pact-v4-edit-candidates/v1"
    assert candidates["candidates"][0]["pid"] == "p00002"
    assert candidates["candidates"][0]["class"] == "calque"

    # Trial-record R report: journal with accept/reject verdicts.
    r_report = result.record["russian_editor"]
    assert r_report["status"] == "complete"
    assert r_report["safe_classes"] == sorted(
        ["typo", "grammar", "duplicate", "preposition"]
    )
    journal = {e["pid"]: e for e in r_report["review_journal"]}
    assert journal["p00002"]["verdict"] == "accepted"
    assert journal["p00002"]["committed_text"].endswith("— переформулировано")
    # The candidate entry appears in the B3 audit journal too.
    events = _journal_events(cfg.out_dir)
    names = [e["event"] for e in events]
    assert "r_editor_started" in names
    assert "r_editor_done" in names


def test_b3_r_editor_disabled_scheme_41(tmp_path: Path) -> None:
    """--no-russian-editor (russian_editor_enabled=False) restores the 4.1
    scheme: the raw map is audited directly, 0 R calls, no R artifacts."""
    from pact_v4.pipeline.b3_audit_repair import B3AuditRepairConfig

    cfg = _whole_chapter_cfg(tmp_path)
    backend = _B3MockBackend(audit_issues=[], repair_results=[], reaudit_issues=[])
    result = _run_with_b3(
        cfg, backend,
        config_override=B3AuditRepairConfig(
            entity_context_enabled=False, russian_editor_enabled=False,
        ),
    )
    assert backend.r_editor_calls() == 0
    assert not (cfg.out_dir / "translations_edited.json").exists()
    assert not (cfg.out_dir / "edit_candidates.json").exists()
    assert result.record["russian_editor"] is None
    assert result.step8["released_as_audited"] is True


def test_b3_r_editor_incomplete_applies_nothing(tmp_path: Path) -> None:
    """An incomplete R pass (invalid chunk) applies NO edits and forwards NO
    candidates (fail-closed at the stage level); the audit still runs on the
    raw map and protects the chapter."""
    from pact_v4.pipeline.b3_audit_repair import B3AuditRepairConfig

    cfg = _whole_chapter_cfg(tmp_path)

    class _FlakyREditorBackend(_B3MockBackend):
        """Serves a valid first r_editor chunk and a broken second one."""

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._r_editor_served = 0

        def complete(self, request: CompletionRequest) -> CompletionResponse:
            if "russian_editor" in (request.label or ""):
                self.requests.append(request)
                self._r_editor_served += 1
                if self._r_editor_served > 1:
                    return _ok_response({"edits": "not-an-array"})
                return _ok_response({"edits": [
                    {
                        "pid": "p00001",
                        "original": "Перевод номер1 номер1",
                        "rewritten": "Перевод номер1",
                        "reason": "дубль",
                        "class": "duplicate",
                    },
                ]})
            return super().complete(request)

    backend = _FlakyREditorBackend(
        audit_issues=[], repair_results=[], reaudit_issues=[],
    )
    result = _run_with_b3(
        cfg, backend,
        config_override=B3AuditRepairConfig(
            entity_context_enabled=False,
            russian_editor_enabled=True,
            russian_editor_chunk_size=4,  # 2 chunks for 8 pids
        ),
    )
    # R incomplete -> nothing applied, audit proceeded on the raw map.
    assert result.record["russian_editor"]["status"] == "incomplete"
    repaired = _read_json(cfg.out_dir / "translations_repaired.json")
    assert repaired["translations"]["p00001"] == "Перевод номер1 номер1"
    assert not (cfg.out_dir / "translations_edited.json").exists()
    assert result.step8["released_as_audited"] is True  # audit still protects


def test_b3_r_editor_evaluator_exception_status_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RV fd7ee8e: an ENABLED R stage whose evaluator RAISES must be
    reported as ``status='failed'`` — never ``disabled`` (the old code
    recorded ``disabled`` when ``enabled=True``, erasing the failed/debt
    state). No R edits are applied, no R artifacts are written, and the
    audit still protects the chapter (fail-closed debt, like a failed
    repair batch)."""
    from pact_v4.pipeline import b3_audit_repair as b3_mod
    from pact_v4.audit.russian_editor import (
        RussianEditorEvaluator as _RealREditorEvaluator,
    )

    class _ExplodingREditorEvaluator(_RealREditorEvaluator):
        """Evaluator that raises on the FIRST chunk call (transport-level
        crash outside the per-chunk handling, e.g. a broken prompt render or
        model-ref resolution)."""

        def __call__(self, **kwargs: Any) -> Any:
            raise RuntimeError("simulated r_editor evaluator crash")

    monkeypatch.setattr(
        b3_mod, "RussianEditorEvaluator", _ExplodingREditorEvaluator
    )
    cfg = _whole_chapter_cfg(tmp_path)
    backend = _B3MockBackend(audit_issues=[], repair_results=[], reaudit_issues=[])
    result = _run_with_b3(
        cfg, backend,
        config_override=B3AuditRepairConfig(
            entity_context_enabled=False,
            russian_editor_enabled=True,
        ),
    )
    r_report = result.record["russian_editor"]
    assert r_report is not None
    assert r_report["enabled"] is True
    assert r_report["status"] == "failed"
    assert r_report["outcome"] is None
    # No R edits applied; the audit proceeded on the RAW map.
    repaired = _read_json(cfg.out_dir / "translations_repaired.json")
    assert repaired["translations"]["p00001"] == "Перевод номер1 номер1"
    assert not (cfg.out_dir / "translations_edited.json").exists()
    assert not (cfg.out_dir / "edit_candidates.json").exists()
    # The append-only journal records the failure as debt.
    events = _journal_events(cfg.out_dir)
    done = [e for e in events if e["event"] == "r_editor_done"]
    assert done and done[-1]["status"] == "failed"
    assert "error" in done[-1] and "simulated" in done[-1]["error"]
    # The audit still protects the chapter (fail-closed, never a crash).
    assert result.step8["released_as_audited"] is True


def test_b3_r_editor_config_part_of_identity(tmp_path: Path) -> None:
    """F5 lesson: russian_editor_version + chunk settings + class threshold
    participate in the config identity — flipping any invalidates the
    repaired cache."""
    base = _whole_chapter_cfg(tmp_path)
    base_id = base.to_config_artifact(model_profile="test").config_identity
    mutations = {
        "enabled": dict(russian_editor_enabled=False),
        "version": dict(russian_editor_version="pact-v4.2-russian-editor/v9.9"),
        "chunk_size": dict(russian_editor_chunk_size=25),
        "overlap_pairs": dict(russian_editor_overlap_pairs=3),
        "max_tokens": dict(russian_editor_max_tokens=16000),
        "safe_classes": dict(russian_editor_safe_classes=("typo", "grammar")),
    }
    for label, overrides in mutations.items():
        mutated = _whole_chapter_cfg(tmp_path, **overrides)
        mutated_id = mutated.to_config_artifact(model_profile="test").config_identity
        assert mutated_id != base_id, f"{label} mutation did not change identity"


def test_b3_r_editor_cache_hit_restores_report_zero_calls(tmp_path: Path) -> None:
    """A full audit-cache hit restores the stored R report (candidates +
    journal) with 0 model calls — resume never re-runs the Russian editor."""
    cfg = _whole_chapter_cfg(tmp_path)
    backend = _B3MockBackend(
        r_editor_edits=[
            ("p00001", "typo", " — исправлено"),
            ("p00002", "calque", " — переформулировано"),
        ],
        audit_issues=[],
        repair_results=[
            {"index": 1, "decision": "repair", "pid": "p00002",
             "repaired_translation": "Перевод номер2 — переформулировано",
             "reason": "принято"},
        ],
        reaudit_issues=[],
    )
    first = _run_with_b3(cfg, backend)
    assert first.record["russian_editor"]["status"] == "complete"
    assert backend.r_editor_calls() == 1

    second_backend = _B3MockBackend(
        audit_issues=[], repair_results=[], reaudit_issues=[]
    )
    second = _run_with_b3(cfg, second_backend)
    assert second.step6["from_cache"] is True
    assert second_backend.r_editor_calls() == 0
    assert second_backend.audit_calls() == 0
    # The stored R report (with the accept journal) is restored verbatim.
    assert second.record["russian_editor"]["status"] == "complete"
    journal = {
        e["pid"]: e for e in second.record["russian_editor"]["review_journal"]
    }
    assert journal["p00002"]["verdict"] == "accepted"
    repaired = _read_json(cfg.out_dir / "translations_repaired.json")
    assert repaired["translations"]["p00002"].endswith("— переформулировано")


def test_b3_r_editor_old_cache_does_not_replay_under_new_policy(
    tmp_path: Path,
) -> None:
    """A repaired cache written under R ON must NOT replay under R OFF (and
    vice versa): the config identity carries the R keys. Tested at the B3
    bundle level (the whole-chapter runner already refuses to resume a
    journal under a different config identity — that fail-closed gate makes
    the runner-level scenario raise before B3)."""
    from pact_v4.pipeline.b3_audit_repair import B3AuditRepair
    from pact_v4.runtime.snapshot_factory import (
        build_snapshot,
        build_source_artifact,
    )
    from pact_v4.phase0b.source_html import load_source
    from pact_v4.phase1.chunker import ChunkPlanner
    from pact_v4.phase1.models import ChunkPlanArtifact

    cfg = _whole_chapter_cfg(tmp_path)  # russian_editor_enabled=True default
    blocks, _ = load_source(cfg.chapter_html_path)
    source = build_source_artifact(chapter_id=cfg.chapter_id, blocks=blocks)
    from pact_v4.runtime.snapshot_factory import ChapterMemory

    memory = ChapterMemory.from_directory(cfg.memory_dir)
    snapshot = build_snapshot(
        chapter_id=cfg.chapter_id, source=source, memory=memory,
        context=f"chapter_html={cfg.chapter_html_path};memory_dir={cfg.memory_dir}",
    )
    planner = ChunkPlanner()
    plans = planner.plan(
        blocks, snapshot_hash=snapshot.snapshot_hash,
        following_blocks=cfg.right_context_pids,
    )
    chunk_plan = ChunkPlanArtifact.create(snapshot, tuple(plans))
    translation = dict(source.source)

    def _run_bundle(r_editor_enabled: bool) -> Any:
        backend = _B3MockBackend(
            audit_issues=[], repair_results=[], reaudit_issues=[],
        )
        bundle = B3AuditRepair(
            audit_backend=backend,
            repair_backend=backend,
            config=B3AuditRepairConfig(
                entity_context_enabled=False,
                russian_editor_enabled=r_editor_enabled,
            ),
        )
        run_cfg = _whole_chapter_cfg(
            tmp_path, russian_editor_enabled=r_editor_enabled
        )
        run_config = run_cfg.to_config_artifact(
            model_profile=run_cfg.backend.config_profile_name()
        )
        return bundle.run(
            chapter_id=cfg.chapter_id,
            source=source,
            snapshot_hash=snapshot.snapshot_hash,
            translation=dict(translation),
            book_memory={},
            out_dir=cfg.out_dir,
            config_identity=run_config.config_identity,
            backend_identity_hash=cfg.backend.identity_hash,
        ), backend

    first, first_backend = _run_bundle(r_editor_enabled=True)
    assert first.from_cache is False
    assert first_backend.r_editor_calls() == 1

    # Same out-dir, R now DISABLED -> config identity differs -> cache miss
    # and the audit re-runs (never replay the repaired map under a new R
    # policy — the F5 lesson).
    second, second_backend = _run_bundle(r_editor_enabled=False)
    assert second.from_cache is False
    assert second_backend.audit_calls() == 1
    assert second_backend.r_editor_calls() == 0
