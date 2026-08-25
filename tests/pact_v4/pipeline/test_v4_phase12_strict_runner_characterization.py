"""Stage 1 characterization baseline for the strict runner (OpenSpec v4-strict-runner-characterization-baseline).

Contract-to-test gap map (tasks 1.1 / 1.2) — Stage 1 covers four invariants from
`openspec/changes/v4-phase12-strict-runner/contract-map.md`:

  (A) Resume / foreign-identity & append-only journal — §1  Journal/Resume
      Existing: test_v4_phase12_strict_runner.py::{test_resume_skips..., test_resume_rejects_journal_written_under_different_lazy_balanced,
        test_resume_rejects_journal_from_a_different_snapshot, test_resume_rejects_pre_policy..., test_merge_selection_meta...}
      Existing WC variant: test_v4_phase12_strict_runner_whole_chapter.py::{test_whole_chapter_config_identity_rejects_chunked_resume,
        test_whole_chapter_resume_rejects_foreign_*, test_whole_chapter_resume_fails_closed_on_source_text_change}
      Gaps pinned here (RV1): journal append-only identity preservation, whole-chapter vs chunked config-identity cross-rejection via public
        error prefix, empty trailing line tolerance and malformed trailing line fail-closed verified via public resume (not private helper).

  (B) Single-entry whole-chapter journal + PID order — §1 (Whole-chapter chunk id, single-entry invariant) + §2.2 WholeChapterPidMap
      Existing: test_v4_phase12_strict_runner_whole_chapter.py::{test_whole_chapter_mode_generates_one_call_full_pid_map,
        test_whole_chapter_resume_rejects_duplicate_journal_entry, test_whole_chapter_resume_rejects_malformed_journal_shape,
        test_whole_chapter_writes_pid_map_artifact, test_whole_chapter_chunk_plan_marked_whole_chapter_derived}
      Gaps pinned here: explicit PID order == source order, chunk_plan.json retains real chunk boundaries in WC mode (so later chunked audit
        still slices on chunk_plan.chunks, §2.5), candidate_ids == [selected_candidate_id] linkage. Verified via public artifacts only.

  (C) Whole-chapter generation with chunked audit — §2.5 Audit still chunked even in WC generation + §3.1 Step 6 — Audit
      Existing: test_v4_phase12_strict_runner_b3.py covers B3 audit slicing (per-chunk audit_unit events, CoverageError, repair slicing),
        test_v4_phase12_strict_runner_whole_chapter.py shows WC steps 6/7/8 skipped (generation_only), but no test explicitly proves the
        WC pid_map + chunk_plan.chunks partitioning invariant for future chunked audit.
      Gaps pinned here (RV1): WC pid_map flatten equals chunk_plan chunks flattened, plus genuine offline whole-chapter run with injected B3
        audit machinery asserting per-retained-chunk audit units/calls (not just partition arithmetic).

  (D) Terminal artifacts / provenance — §4 Persistent Artifacts + §3.4 Phase 5 / Formatting terminal
      Existing: test_v4_phase12_strict_runner.py::{test_run_writes_all_artefacts, test_selected_translations_written},
        test_v4_phase12_strict_runner_translations_final.py::{test_translations_json_is_full_final_map..., (former _normalize direct test replaced)},
        test_v4_phase12_strict_runner_retry.py / repair / formatting suites.
      Gaps pinned here: record identities linkage (chunk_plan_hash/config_identity/snapshot_hash/whole_chapter_pid_map_hash match persisted
        artifacts), artefacts paths exist (F8 manifest), translations.json normalized full-map equals repair_report final_translation via public
        run artifacts (RV1: no private _normalize import).

All tests are synthetic/offline (FakeLifecycleAdapter, StubModelCaller, tmp_path), assert public artifacts / result fields / failure prefixes only
(design decision: observe external contracts, not private helpers), and are deterministic with no network or pipeline execution.
Baseline command (design Goals, tasks 3.1): python3 -m pytest tests/pact_v4/pipeline/test_v4_phase12_strict_runner_characterization.py -q
RV1 alignment: max_tokens default is 70000 (raised 2026-08-19 from 32768) — see StrictRunConfig; stale whole-chapter expectations corrected test-only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pytest

from pact_v4.phase1.models import GateResult, WholeChapterPidMap
from pact_v4.pipeline.b3_audit_repair import B3AuditRepair, B3AuditRepairConfig
from pact_v4.pipeline.v4_phase12_strict_runner import (
    WHOLE_CHAPTER_CHUNK_ID,
    WHOLE_CHAPTER_SELECTION_SCHEMA,
    run_chapter_strict,
)
from pact_v4.runtime.backend_protocol import (
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
    _build_artifacts,
    _make_backend,
    _make_cfg,
)


def _make_router() -> ModelRouter:
    return ModelRouter(
        FakeLifecycleAdapter(),
        role_profile_names={"gemma": "Gemma", "qwen": "Qwen"},
        role_args={"gemma": [], "qwen": []},
    )


def _run_chunked(cfg, **overrides):
    router = _make_router()
    inner = StubModelCaller()
    model_caller = _LifecycleAwareModelCaller(router, inner)
    result = run_chapter_strict(
        cfg,
        router=router,
        model_caller=model_caller,
        qwen_evaluator=_LifecycleAwareQwen(router, StubQwen()),
        gemma_selector=_LifecycleAwareGemmaSelector(router, StubGemma()),
        qwen_audit_evaluator=_LifecycleAwareQwenAudit(router, StubQwenAudit()),
        gemma_audit_evaluator=_LifecycleAwareGemmaAudit(router, StubGemmaAudit()),
    )
    return result, inner, router


def _run_whole_chapter(cfg, model_caller=None):
    router = _make_router()
    caller = model_caller or StubModelCaller()
    wrapped = _LifecycleAwareModelCaller(router, caller)
    result = run_chapter_strict(
        cfg,
        router=router,
        model_caller=wrapped,
        qwen_evaluator=_LifecycleAwareQwen(router, StubQwen()),
        gemma_selector=_LifecycleAwareGemmaSelector(router, StubGemma()),
        qwen_audit_evaluator=_LifecycleAwareQwenAudit(router, StubQwenAudit()),
        gemma_audit_evaluator=_LifecycleAwareGemmaAudit(router, StubGemmaAudit()),
    )
    return result, caller, router


def _read_journal_entries(journal_path: Path) -> List[Dict[str, Any]]:
    """Read public journal.ndjson artifact, skipping empty lines — observes persisted artifact, not private helper."""
    if not journal_path.exists():
        return []
    entries: List[Dict[str, Any]] = []
    for line in journal_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        entries.append(json.loads(line))
    return entries


# ---------------------------------------------------------------------------
# Minimal B3 mock backend for genuine whole-chapter audit (RV1)
# ---------------------------------------------------------------------------


class _B3MockBackend(CompletionBackend):
    """Offline CompletionBackend serving B3 audit/repair/entity from canned payloads — 0 real model calls."""

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
        if "russian_editor" in label:
            return _ok_response({"edits": []})
        if "qwen_chapter_audit" in label:
            if self._fail_audit:
                raise CompletionError("simulated audit transport failure")
            return _ok_response({"issues": self._audit_issues})
        if "selective_repair" in label:
            # echo full current text for "<current>" token so preservation gate passes
            prompt = request.messages[0].content
            current: Dict[str, str] = {}
            in_translation = False
            for line in prompt.splitlines():
                ls = line.strip()
                if ls.startswith("TRANSLATION"):
                    in_translation = True
                    continue
                if in_translation:
                    if ls.startswith("FINDINGS") or not ls:
                        break
                    if ":" not in ls:
                        continue
                    pid, _, text = ls.partition(":")
                    pid = pid.strip()
                    text = text.strip()
                    if pid.startswith("p") and text:
                        current[pid] = text
            results = []
            for entry in self._repair_results:
                item = dict(entry)
                rep = item.get("repaired_translation")
                if isinstance(rep, str) and rep.startswith("<current>"):
                    suffix = rep[len("<current>") :]
                    item["repaired_translation"] = current.get(item.get("pid", ""), "") + suffix
                results.append(item)
            return _ok_response({"results": results})
        if "reaudit" in label:
            return _ok_response({"issues": self._reaudit_issues})
        raise AssertionError(f"unexpected B3 request label {label!r}")

    def close(self) -> None:
        pass

    def call_records(self) -> Sequence[Any]:
        return []

    def audit_calls(self) -> int:
        return sum(1 for r in self.requests if "qwen_chapter_audit" in (r.label or ""))

    def entity_calls(self) -> int:
        return sum(1 for r in self.requests if "entity_extractor" in (r.label or ""))

    def repair_calls(self) -> int:
        return sum(1 for r in self.requests if "selective_repair" in (r.label or ""))


def _ok_response(payload: Mapping[str, Any]) -> CompletionResponse:
    return CompletionResponse(
        text=json.dumps(payload, ensure_ascii=False),
        model="qwen-3.6-35b",
        finish_reason="stop",
    )


def _run_whole_chapter_with_b3(cfg, backend: _B3MockBackend, *, caller=None):
    router = _make_router()
    inner = caller or StubModelCaller()
    wrapped = _LifecycleAwareModelCaller(router, inner)
    bundle = B3AuditRepair(
        audit_backend=backend,
        repair_backend=backend,
        config=B3AuditRepairConfig(entity_context_enabled=True),
    )
    result = run_chapter_strict(
        cfg,
        router=router,
        model_caller=wrapped,
        qwen_evaluator=_LifecycleAwareQwen(router, StubQwen()),
        gemma_selector=_LifecycleAwareGemmaSelector(router, StubGemma()),
        qwen_audit_evaluator=_LifecycleAwareQwenAudit(router, StubQwenAudit()),
        gemma_audit_evaluator=_LifecycleAwareGemmaAudit(router, StubGemmaAudit()),
        b3_audit_repair=bundle,
    )
    return result, inner, router


# ---------------------------------------------------------------------------
# (A) Resume / foreign-identity & append-only journal
# ---------------------------------------------------------------------------


class TestAAppendOnlyAndForeignIdentity:
    """Contract §1 — Journal schema, append-only flush, foreign-identity refusal. Public artifacts only."""

    def test_a_first_run_without_journal_starts_clean(self, tmp_path: Path):
        cfg = _make_cfg(tmp_path, n_paragraphs=8)
        result, _, _ = _run_chunked(cfg)
        assert result.resumed_from_index == 0
        assert result.record["resumed_from_index"] == 0
        # No prior journal -> first run processes all chunks cleanly
        _, _, chunk_plan, _ = _build_artifacts(cfg)
        assert result.processed_count == len(chunk_plan.chunks)

    def test_a_journal_empty_trailing_line_tolerated_via_resume(self, tmp_path: Path):
        # Writer flushes per entry; crash may leave empty trailing newline. Resume must tolerate empty lines via public artifact.
        cfg = _make_cfg(tmp_path, n_paragraphs=24)
        wc_cfg = type(cfg)(
            chapter_id=cfg.chapter_id, chapter_html_path=cfg.chapter_html_path,
            memory_dir=cfg.memory_dir, out_dir=cfg.out_dir, backend=cfg.backend,
            whole_chapter=True,
        )
        first, _, _ = _run_whole_chapter(wc_cfg)
        assert first.processed_count == 1
        journal_path = wc_cfg.out_dir / "journal.ndjson"
        assert journal_path.exists()
        before_text = journal_path.read_text(encoding="utf-8")
        # Append empty trailing lines (simulated crash with extra newline)
        journal_path.write_text(before_text + "\n\n", encoding="utf-8")
        # Resume must succeed, ignoring empty lines, with no new generation calls
        caller2 = StubModelCaller()
        resumed, _, _ = _run_whole_chapter(wc_cfg, model_caller=caller2)
        assert len(caller2.calls) == 0
        assert resumed.resumed_from_index == 1
        assert resumed.processed_count == 1
        # Journal logical entry count remains 1 (empty lines not counted)
        assert len(_read_journal_entries(journal_path)) == 1

    def test_a_journal_malformed_trailing_line_fails_closed_on_resume(self, tmp_path: Path):
        # Malformed trailing JSON must fail closed on resume — not silently ignored.
        cfg = _make_cfg(tmp_path, n_paragraphs=24)
        wc_cfg = type(cfg)(
            chapter_id=cfg.chapter_id, chapter_html_path=cfg.chapter_html_path,
            memory_dir=cfg.memory_dir, out_dir=cfg.out_dir, backend=cfg.backend,
            whole_chapter=True,
        )
        _run_whole_chapter(wc_cfg)
        journal_path = wc_cfg.out_dir / "journal.ndjson"
        before_final = (wc_cfg.out_dir / "translations.json").read_text(encoding="utf-8")
        before_sel = (wc_cfg.out_dir / "selection_results.json").read_text(encoding="utf-8")
        # Append malformed trailing line
        journal_path.write_text(journal_path.read_text(encoding="utf-8") + "{not json\n", encoding="utf-8")
        caller = StubModelCaller()
        with pytest.raises((json.JSONDecodeError, ValueError)):
            _run_whole_chapter(wc_cfg, model_caller=caller)
        assert len(caller.calls) == 0
        # Fail closed: final artifacts not rewritten
        assert (wc_cfg.out_dir / "translations.json").read_text(encoding="utf-8") == before_final
        assert (wc_cfg.out_dir / "selection_results.json").read_text(encoding="utf-8") == before_sel

    def test_a_chunked_resume_rejects_foreign_snapshot(self, tmp_path: Path):
        # Foreign identity: journal written under different snapshot/plan/config must be refused.
        cfg = _make_cfg(tmp_path, n_paragraphs=8)
        first, _, _ = _run_chunked(cfg)
        _, _, chunk_plan, _ = _build_artifacts(cfg)
        assert first.processed_count == len(chunk_plan.chunks)
        cfg2 = _make_cfg(tmp_path, n_paragraphs=8)
        cfg2 = type(cfg2)(
            chapter_id=cfg2.chapter_id,
            chapter_html_path=cfg.chapter_html_path,
            memory_dir=cfg2.memory_dir,
            out_dir=cfg.out_dir,
            backend=cfg2.backend,
        )
        html_path = cfg2.chapter_html_path
        html_path.write_text(html_path.read_text(encoding="utf-8").replace("word0", "CHANGED0"), encoding="utf-8")
        with pytest.raises(ValueError, match="Foreign identity"):
            _run_chunked(cfg2)

    def test_a_whole_chapter_resume_rejects_chunked_config_identity(self, tmp_path: Path):
        # Whole-chapter vs chunked is part of config identity — cross-resume must be refused.
        base = _make_cfg(tmp_path, n_paragraphs=24)
        chunked_cfg = base
        _run_chunked(chunked_cfg)
        journal_path = chunked_cfg.out_dir / "journal.ndjson"
        assert journal_path.exists()
        wc_cfg = type(base)(
            chapter_id=base.chapter_id,
            chapter_html_path=base.chapter_html_path,
            memory_dir=base.memory_dir,
            out_dir=base.out_dir,
            backend=base.backend,
            whole_chapter=True,
        )
        with pytest.raises(ValueError, match="Foreign identity"):
            _run_whole_chapter(wc_cfg)

    def test_a_journal_is_append_only_not_overwritten_on_resume(self, tmp_path: Path):
        # Append-only: resume replays prior journal entries; journal length stays cumulative.
        cfg = _make_cfg(tmp_path, n_paragraphs=8)
        first, _, _ = _run_chunked(cfg)
        journal_before = (cfg.out_dir / "journal.ndjson").read_text(encoding="utf-8").splitlines()
        assert len(journal_before) == first.processed_count
        resumed_cfg = type(cfg)(
            chapter_id=cfg.chapter_id,
            chapter_html_path=cfg.chapter_html_path,
            memory_dir=cfg.memory_dir,
            out_dir=cfg.out_dir,
            backend=cfg.backend,
        )
        second, inner2, _ = _run_chunked(resumed_cfg)
        journal_after = (cfg.out_dir / "journal.ndjson").read_text(encoding="utf-8").splitlines()
        assert inner2.calls == []
        assert second.resumed_from_index == first.processed_count
        assert len(journal_after) == len(journal_before)
        assert journal_after == journal_before


# ---------------------------------------------------------------------------
# (B) Single-entry whole-chapter journal + PID order
# ---------------------------------------------------------------------------


class TestBSingleEntryWCPidOrder:
    """Contract §1 (whole-chapter chunk id) + §2.2 WholeChapterPidMap."""

    def test_b_whole_chapter_journal_is_single_entry(self, tmp_path: Path):
        cfg = _make_cfg(tmp_path, n_paragraphs=24)
        wc_cfg = type(cfg)(
            chapter_id=cfg.chapter_id, chapter_html_path=cfg.chapter_html_path,
            memory_dir=cfg.memory_dir, out_dir=cfg.out_dir, backend=cfg.backend,
            whole_chapter=True,
        )
        result, caller, _ = _run_whole_chapter(wc_cfg)
        assert caller.calls[0].chunk_id == WHOLE_CHAPTER_CHUNK_ID
        journal = _read_journal_entries(wc_cfg.out_dir / "journal.ndjson")
        assert len(journal) == 1, "whole-chapter must have exactly one journal entry"
        entry = journal[0]
        assert entry["chunk_id"] == WHOLE_CHAPTER_CHUNK_ID
        assert entry["candidate_ids"] == [entry["selected_candidate_id"]]
        assert entry["config_identity"] == result.record["identities"]["config_identity"]
        assert entry["chunk_plan_hash"] == result.record["identities"]["chunk_plan_hash"]

    def test_b_whole_chapter_pid_map_order_is_source_order(self, tmp_path: Path):
        cfg = _make_cfg(tmp_path, n_paragraphs=24)
        wc_cfg = type(cfg)(
            chapter_id=cfg.chapter_id, chapter_html_path=cfg.chapter_html_path,
            memory_dir=cfg.memory_dir, out_dir=cfg.out_dir, backend=cfg.backend,
            whole_chapter=True,
        )
        result, _, _ = _run_whole_chapter(wc_cfg)
        _, snapshot, chunk_plan, _ = _build_artifacts(wc_cfg)
        pid_map = WholeChapterPidMap.derive(chunk_plan, snapshot)
        assert pid_map.pids == snapshot.pids
        assert tuple(pid_map.pids) == tuple(snapshot.pids)
        payload = json.loads((wc_cfg.out_dir / "whole_chapter_pid_map.json").read_text(encoding="utf-8"))
        assert payload["pid_count"] == len(snapshot.pids)
        for idx, entry in enumerate(payload["entries"]):
            assert entry["pid"] == snapshot.pids[idx]
            assert entry["order"] == idx
        assert payload["map_hash"] == pid_map.map_hash
        assert result.record["identities"]["whole_chapter_pid_map_hash"] == pid_map.map_hash

    def test_b_whole_chapter_chunk_plan_retains_chunk_boundaries(self, tmp_path: Path):
        cfg = _make_cfg(tmp_path, n_paragraphs=24)
        wc_cfg = type(cfg)(
            chapter_id=cfg.chapter_id, chapter_html_path=cfg.chapter_html_path,
            memory_dir=cfg.memory_dir, out_dir=cfg.out_dir, backend=cfg.backend,
            whole_chapter=True,
        )
        _run_whole_chapter(wc_cfg)
        chunk_plan_payload = json.loads((wc_cfg.out_dir / "chunk_plan.json").read_text(encoding="utf-8"))
        assert chunk_plan_payload["mode"] == "whole-chapter-derived"
        _, snapshot, derived_plan, _ = _build_artifacts(wc_cfg)
        assert len(chunk_plan_payload["chunks"]) > 1
        flat_from_chunks = [pid for c in chunk_plan_payload["chunks"] for pid in c["pids"]]
        pid_map = WholeChapterPidMap.derive(derived_plan, snapshot)
        assert flat_from_chunks == list(pid_map.pids)
        assert flat_from_chunks == list(snapshot.pids)

    def test_b_wc_selection_results_schema_and_linkage(self, tmp_path: Path):
        cfg = _make_cfg(tmp_path, n_paragraphs=8)
        wc_cfg = type(cfg)(
            chapter_id=cfg.chapter_id, chapter_html_path=cfg.chapter_html_path,
            memory_dir=cfg.memory_dir, out_dir=cfg.out_dir, backend=cfg.backend,
            whole_chapter=True,
        )
        result, _, _ = _run_whole_chapter(wc_cfg)
        sel = json.loads((wc_cfg.out_dir / "selection_results.json").read_text(encoding="utf-8"))
        assert sel["schema"] == WHOLE_CHAPTER_SELECTION_SCHEMA
        assert sel["mode"] == "not_applicable"
        assert sel["coverage"] == "full_pid_map"
        assert sel["selection_performed"] is False
        assert sel["config_identity"] == result.record["identities"]["config_identity"]
        outcomes = json.loads((wc_cfg.out_dir / "generation_outcomes.json").read_text(encoding="utf-8"))
        gen_id = sel["generation_record_id"]
        rec = outcomes["outcomes"][0]
        assert rec["chunk_id"] == WHOLE_CHAPTER_CHUNK_ID
        assert rec["candidates"]["balanced_literary"]["candidate_id"] == gen_id
        journal = _read_journal_entries(wc_cfg.out_dir / "journal.ndjson")
        assert journal[0]["selected_candidate_id"] == gen_id


# ---------------------------------------------------------------------------
# (C) Whole-chapter generation with chunked audit evidence
# ---------------------------------------------------------------------------


class TestCWholeChapterGenerationChunkedAudit:
    """Contract §2.5 — audit still chunked even when generation was whole-chapter."""

    def test_c_whole_chapter_single_generation_call_full_pid_map(self, tmp_path: Path):
        cfg = _make_cfg(tmp_path, n_paragraphs=24)
        wc_cfg = type(cfg)(
            chapter_id=cfg.chapter_id, chapter_html_path=cfg.chapter_html_path,
            memory_dir=cfg.memory_dir, out_dir=cfg.out_dir, backend=cfg.backend,
            whole_chapter=True,
        )
        result, caller, _ = _run_whole_chapter(wc_cfg)
        assert len(caller.calls) == 1
        bundle = caller.calls[0]
        assert bundle.chunk_id == WHOLE_CHAPTER_CHUNK_ID
        _, snapshot, chunk_plan, _ = _build_artifacts(wc_cfg)
        pid_map = WholeChapterPidMap.derive(chunk_plan, snapshot)
        assert bundle.owned_pids == pid_map.pids == snapshot.pids
        raw = json.loads((wc_cfg.out_dir / "translations_raw.json").read_text(encoding="utf-8"))
        final = json.loads(result.translations_path.read_text(encoding="utf-8"))
        assert raw == final
        assert set(final) == set(pid_map.pids)

    def test_c_chunk_plan_chunks_partition_pid_map_for_audit_slicing(self, tmp_path: Path):
        cfg = _make_cfg(tmp_path, n_paragraphs=24)
        wc_cfg = type(cfg)(
            chapter_id=cfg.chapter_id, chapter_html_path=cfg.chapter_html_path,
            memory_dir=cfg.memory_dir, out_dir=cfg.out_dir, backend=cfg.backend,
            whole_chapter=True,
        )
        _run_whole_chapter(wc_cfg)
        _, snapshot, chunk_plan, _ = _build_artifacts(wc_cfg)
        pid_map = WholeChapterPidMap.derive(chunk_plan, snapshot)
        plan_payload = json.loads((wc_cfg.out_dir / "chunk_plan.json").read_text(encoding="utf-8"))
        flattened = [pid for chunk in plan_payload["chunks"] for pid in chunk["pids"]]
        assert flattened == list(pid_map.pids)
        assert len(set(flattened)) == len(flattened)
        assert set(flattened) == set(snapshot.pids)

    def test_c_whole_chapter_genuine_b3_audit_per_retained_chunk(self, tmp_path: Path):
        """Genuine offline whole-chapter run with injected B3 audit machinery — per-retained-chunk audit units/calls."""
        # Use larger chapter so default audit chunking yields >1 audit unit, proving audit remains chunked
        cfg = _make_cfg(tmp_path, n_paragraphs=48)
        wc_cfg = type(cfg)(
            chapter_id=cfg.chapter_id, chapter_html_path=cfg.chapter_html_path,
            memory_dir=cfg.memory_dir, out_dir=cfg.out_dir, backend=cfg.backend,
            whole_chapter=True,
        )
        _, snapshot, chunk_plan, _ = _build_artifacts(wc_cfg)
        assert len(chunk_plan.chunks) > 1, "fixture must have >1 retained chunk to prove per-chunk audit"
        backend = _B3MockBackend(audit_issues=[], reaudit_issues=[])
        result, caller, _ = _run_whole_chapter_with_b3(wc_cfg, backend)
        # Whole-chapter generation still single call covering full PID map
        assert len(caller.calls) == 1
        assert caller.calls[0].chunk_id == WHOLE_CHAPTER_CHUNK_ID
        pid_map = WholeChapterPidMap.derive(chunk_plan, snapshot)
        assert caller.calls[0].owned_pids == pid_map.pids
        # B3 audit was genuinely executed chunked — not collapsed to 1 unit
        # Assert via backend call counts, audit journal per-chunk events, and step6 counters
        assert backend.audit_calls() > 1, f"audit should be chunked, got {backend.audit_calls()} call(s)"
        assert backend.audit_calls() == result.step6["chunk_count"]
        assert result.step6["audit_complete"] is True
        journal = [
            json.loads(line)
            for line in (wc_cfg.out_dir / "audit_journal.ndjson").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        started = [e for e in journal if e.get("event") == "audit_chunk_started"]
        done = [e for e in journal if e.get("event") == "audit_chunk_done"]
        assert len(started) == backend.audit_calls()
        assert len(done) == backend.audit_calls()
        assert len(started) > 1
        # Chunk_plan still retains boundaries — audit slices the same PID set
        plan_payload = json.loads((wc_cfg.out_dir / "chunk_plan.json").read_text(encoding="utf-8"))
        flattened = [pid for c in plan_payload["chunks"] for pid in c["pids"]]
        assert flattened == list(pid_map.pids)
        assert len(plan_payload["chunks"]) > 1
        cache = json.loads((wc_cfg.out_dir / "audit_cache_b3.json").read_text(encoding="utf-8"))
        assert cache["audit_complete"] is True


# ---------------------------------------------------------------------------
# (D) Terminal artifacts / provenance
# ---------------------------------------------------------------------------


class TestDTerminalProvenance:
    """Contract §3.4 + §4 — translations_final / strict_chapter_trial_record provenance."""

    def test_d_strict_record_provenance_chunked(self, tmp_path: Path):
        cfg = _make_cfg(tmp_path, n_paragraphs=8)
        result, _ = _run_chunked(cfg)[:2]
        record = result.record
        for key in ("source_hash", "snapshot_hash", "chunk_plan_hash", "config_identity"):
            assert record["identities"][key], f"missing identity {key}"
        for art_key in ("chunk_plan", "generation_outcomes", "selection_results", "translations", "journal"):
            path = Path(record["artefacts"][art_key])
            assert path.exists(), f"record artefact {art_key} missing at {path}"
        assert record["counts"]["chunks_total"] == len(_build_artifacts(cfg)[2].chunks)
        assert record["counts"]["chunks_processed"] == result.processed_count
        _, snapshot, chunk_plan, config = _build_artifacts(cfg)
        assert record["identities"]["snapshot_hash"] == snapshot.snapshot_hash
        assert record["identities"]["chunk_plan_hash"] == chunk_plan.plan_hash
        assert record["identities"]["config_identity"] == config.config_identity

    def test_d_strict_record_provenance_whole_chapter(self, tmp_path: Path):
        cfg = _make_cfg(tmp_path, n_paragraphs=24)
        wc_cfg = type(cfg)(
            chapter_id=cfg.chapter_id, chapter_html_path=cfg.chapter_html_path,
            memory_dir=cfg.memory_dir, out_dir=cfg.out_dir, backend=cfg.backend,
            whole_chapter=True,
        )
        result, _, _ = _run_whole_chapter(wc_cfg)
        record = result.record
        assert record["operational_policy"]["whole_chapter"] is True
        _, snapshot, chunk_plan, config = _build_artifacts(wc_cfg)
        pid_map = WholeChapterPidMap.derive(chunk_plan, snapshot)
        assert record["identities"]["whole_chapter_pid_map_hash"] == pid_map.map_hash
        assert record["identities"]["chunk_plan_hash"] == chunk_plan.plan_hash
        assert record["identities"]["config_identity"] == config.config_identity
        assert Path(record["artefacts"]["whole_chapter_pid_map"]).exists()
        assert record["counts"]["chunks_total"] == 1
        assert record["counts"]["chunks_processed"] == 1

    def test_d_translations_json_full_map_and_artifact_identity(self, tmp_path: Path):
        cfg = _make_cfg(tmp_path, n_paragraphs=8)
        result, _ = _run_chunked(cfg)[:2]
        _, snapshot, _, _ = _build_artifacts(cfg)
        translations = json.loads(result.translations_path.read_text(encoding="utf-8"))
        assert set(translations) == set(snapshot.pids)
        assert all(isinstance(v, str) and v for v in translations.values())
        assert Path(result.record["artefacts"]["translations"]) == result.translations_path

    def test_d_final_markup_normalized_via_public_repair_artifact(self, tmp_path: Path):
        """Public-artefact proof that HTML entity markup is normalized to clean tags (B13/B14)."""
        # Chunked run with a committed repair whose text carries escaped entities — the public
        # translations.json must contain clean <em> tags while repair_report retains escaped form.
        from pact_v4.pipeline.v4_phase12_strict_runner import run_chapter_strict as _run
        from tests.pact_v4.pipeline.test_v4_phase12_strict_runner import _make_router as _mr
        from tests.pact_v4.pipeline.test_v4_phase12_strict_runner_retry import (
            ContentQwen,
            StubRepairCaller,
        )

        cfg = _make_cfg(tmp_path, n_paragraphs=8)

        class _OneIssueQwenAudit(StubQwenAudit):
            def __call__(self, *, chunk_id, source, translation):
                self.calls.append((chunk_id, dict(source), dict(translation)))
                pid = next(iter(translation))
                return json.dumps({"issues": [{"pid": pid, "category": "omission", "note": "dropped clause"}]})

        class _EscapedRepair(StubRepairCaller):
            def __call__(self, *, chunk_id, source, translation, region, findings):
                self.calls.append((chunk_id, region.pid))
                pid = region.pid
                return json.dumps(
                    {"repaired": {pid: "&lt;em&gt;курсив&lt;/em&gt;"}, "reason": "test"},
                    ensure_ascii=False,
                )

        class _PassGate:
            def __init__(self):
                self.calls: List[str] = []

            def __call__(self, *, source_text, repaired_text, region):
                self.calls.append(region.pid)
                return GateResult(gate="region_fidelity", passed=True, detail="ok")

        router = _mr()
        inner = StubModelCaller()
        model_caller = _LifecycleAwareModelCaller(router, inner)
        qwen_audit = _OneIssueQwenAudit()
        region_gate = _PassGate()
        repair_caller = _EscapedRepair()
        result = _run(
            cfg,
            router=router,
            model_caller=model_caller,
            qwen_evaluator=_LifecycleAwareQwen(router, StubQwen()),
            gemma_selector=_LifecycleAwareGemmaSelector(router, StubGemma()),
            qwen_audit_evaluator=_LifecycleAwareQwenAudit(router, qwen_audit),
            gemma_audit_evaluator=_LifecycleAwareGemmaAudit(router, StubGemmaAudit()),
            repair_adapters=(
                repair_caller,
                region_gate,
                _LifecycleAwareQwenAudit(router, StubQwenAudit()),
                _LifecycleAwareGemmaAudit(router, StubGemmaAudit()),
            ),
        )
        # Repair committed
        assert repair_caller.calls
        # Public artifacts: translations.json is normalized, repair_report retains escaped
        translations = json.loads(result.translations_path.read_text(encoding="utf-8"))
        has_clean = any("<em>курсив</em>" in v for v in translations.values())
        has_escaped = any("&lt;em&gt;" in v for v in translations.values())
        assert has_clean, "expected clean <em> tag in public translations.json"
        assert not has_escaped, "escaped entity should be normalized in public translations.json"
        report = json.loads((cfg.out_dir / "repair_report.json").read_text(encoding="utf-8"))
        # repair_report final_translation is list of [pid,text] pairs — diagnostic keeps escaped form
        final_list = report["final_translation"]
        if isinstance(final_list, dict):
            vals = final_list.values()
        else:
            vals = [v for _, v in final_list]
        assert any("&lt;em&gt;" in v for v in vals)

    def test_d_record_resumed_from_index_provenance(self, tmp_path: Path):
        cfg = _make_cfg(tmp_path, n_paragraphs=8)
        first, _, _ = _run_chunked(cfg)
        resumed_cfg = type(cfg)(
            chapter_id=cfg.chapter_id, chapter_html_path=cfg.chapter_html_path,
            memory_dir=cfg.memory_dir, out_dir=cfg.out_dir, backend=cfg.backend,
        )
        second, _, _ = _run_chunked(resumed_cfg)
        assert second.resumed_from_index == first.processed_count
        assert second.record["resumed_from_index"] == first.processed_count
        assert second.record["identities"]["chunk_plan_hash"] == first.record["identities"]["chunk_plan_hash"]
        assert second.record["identities"]["config_identity"] == first.record["identities"]["config_identity"]
