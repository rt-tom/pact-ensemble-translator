"""Strict single-resident driver: Phase 1C -> 2A -> 2B -> 2C for one chapter,
with real per-chunk model swapping instead of assuming Gemma and Qwen are
both resident (``v4_phase12_draft_runner.run_chapter``) or approximating
``left_context`` from an unverified draft (``v4_phase12_sequential_runner``,
``SEQUENTIAL_MODEL_CAVEAT``).

Backing spec: ``docs/plans/V4_STRICT_DRIVER_CHAPTER_TRIAL_TASK_RU.md``
(task card), ``docs/plans/V4_SINGLE_RESIDENT_DRIVER_ARCHITECTURE_RU.md``
(§ "2. Строгий stop-and-switch", § "Model lifecycle", § "Ошибки между
gate и commit", § "Resume после обрыва", § "Подсчёт перезапусков").

How this reuses existing code rather than re-implementing it:

* The per-chunk decision tree (``pact_v4.phase2.cascade.select_candidate``)
  and every gate function it calls are imported unchanged. This module
  never re-derives the cascade's pass/fail logic.
* ``left_context`` assembly (``_left_ru_for_chunk``), glossary parsing
  (``_glossary_entries``), risk pre-screen (``_risk_for_chunk``), and the
  generation/selection serialization helpers are imported from
  ``pact_v4.pipeline._shared_runner_helpers`` rather than duplicated. The
  same tolerance is already established by ``v4_shadow_reselect_two_pass.py``,
  whose docstring notes it duplicates only *orchestration*, never gate logic.
* Model swapping is delegated to ``pact_v4.runtime.model_lifecycle``
  (``LifecycleAdapter``/``ModelRouter``, the same validated mechanics as
  Measurement 2's bench script) via the ``Lifecycle*`` wrapper adapters in
  ``pact_v4.runtime.model_lifecycle_adapters`` -- ``select_candidate`` and
  ``generate_for_chunk`` are handed lifecycle-aware callables and have no
  idea a model swap ever happens underneath them.

What is genuinely new here (not present in either existing driver):

1. **Durable, append-only, per-chunk journal** (one JSON line per
   completed chunk, flushed immediately) with the fields
   ``docs/plans/V4_SINGLE_RESIDENT_DRIVER_ARCHITECTURE_RU.md``'s "Model
   lifecycle" section requires: ``chunk_id``, ``parent_chunk_id``,
   ``parent_context_state_hash``, ``left_context_kind``, ``snapshot_hash``,
   ``chunk_plan_hash``, ``config_identity``, ``candidate_ids``, gate
   trace, ``selected_candidate_id`` or a terminal non-selection state, and
   the hash of the left-context actually fed to generation.
2. **Resume**: at start, an existing journal is replayed to reconstruct
   ``selected_text_by_chunk`` and skip already-processed chunks, after
   verifying the replayed entries' identities (snapshot/plan/config) match
   this run's freshly computed ones -- a mismatch raises rather than
   silently reusing stale state. Resume here is **per-chunk granularity**
   (redo at most one chunk's generation+gates after an interruption mid-
   chunk), not the finer sub-chunk checkpoints the architecture doc's
   "Resume после обрыва" table describes (generation-only /
   gate-trace-only checkpoints) -- a deliberate scope simplification for
   an 11-chunk chapter trial, noted here rather than silently narrowed.
3. **Lifecycle timing per switch** (``cold_acquire_seconds``,
   ``unload_seconds``, ``peak_vram_mb``, ``load_retries``), recorded via
   ``ModelRouter.switches`` and attached to the chunk(s) that triggered
   them, plus chapter-level aggregates in the same shape Measurement 2
   used, so the two are directly comparable.
4. **Operational policy on repeated non-selection**: pinned *before* the
   run (``max_consecutive_terminal_nonselections``), not decided post hoc
   by reading the logs. Exceeding it halts the chapter with an explicit
   reason recorded in provenance -- never an unbounded
   ``empty_after_nonselection`` cascade to the end of the chapter.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from pact_v4.phase0b.source_html import SourceBlock, load_source
from pact_v4.phase1.chunker import (
    DEFAULT_MAX_WORDS,
    DEFAULT_MIN_WORDS,
    DEFAULT_TARGET_WORDS,
    ChunkPlanner,
)
from pact_v4.phase1.models import (
    Candidate,
    ChunkPlanArtifact,
    ConfigArtifact,
    GateResult,
    Provenance,
    canonical_json_hash,
)
from pact_v4.phase2.cascade import DeterministicGateData, SelectionResult, select_candidate
from pact_v4.phase2.generation import GenerationCache, GenerationParams, generate_for_chunk
from pact_v4.phase3.assembly import AssembledChapter
from pact_v4.phase3.audit import AuditCache, run_chapter_audit
from pact_v4.phase4.quarantined_retry import (
    QUARANTINED_RETRY_POLICY_VERSION,
    QUARANTINED_RETRY_SCHEMA,
    QuarantinedRetryAttempt,
    debt_mentions_chunk,
    debt_mentions_pid,
    merge_retry_generation_records,
    quarantined_chunks_with_debt,
    run_quarantined_retry,
)
from pact_v4.phase4.repair import (
    REPAIR_REPORT_SCHEMA,
    RepairCache,
    RepairPhaseResult,
    SoftFindingsPolicy,
    _reaudit_chunks,
    _run_repair_round,
    decide_terminal_state,
    filter_soft_findings,
    plan_repairs_for_chunk,
    run_integrity_check,
    run_repair_phase,
)
from pact_v4.phase5.formatting import (
    FORMATTING_REPORT_SCHEMA,
    run_formatting_align,
)
from pact_v4.pipeline._shared_runner_helpers import (
    _glossary_entries,
    _left_ru_for_chunk,
    _record_selection,
    _risk_for_chunk,
    _serialize_generation_outcome,
)
from pact_v4.pipeline.phase_progress import PhaseProgressWriter
from pact_v4.runtime.model_lifecycle import ModelRouter
from pact_v4.runtime.model_lifecycle_adapters import (
    GEMMA_MODEL_KEY,
    QWEN_MODEL_KEY,
    LifecycleGemmaAuditEvaluator,
    LifecycleGemmaSelector,
    LifecycleModelCaller,
    LifecycleQwenAuditEvaluator,
    LifecycleQwenEvaluator,
)
from pact_v4.runtime.runtime_config import (
    BackendRuntimeConfig,
    LocalLlamaBackendConfig,
    StrictBackendConfig,
)
from pact_v4.runtime.runtime_coordinator import (
    EVENT_KIND_LOCAL_SWITCH,
    LocalLifecycleCoordinator,
    RuntimeCoordinator,
)
from pact_v4.runtime.snapshot_factory import (
    ChapterMemory,
    build_config_artifact,
    build_snapshot,
    build_source_artifact,
)
from pact_v4._integrity_checks import (
    bible_script_tokens,
    combine_script_tokens,
    extract_script_tokens,
    glossary_script_tokens,
    source_derived_allowlist,
)

LOG = logging.getLogger(__name__)

JOURNAL_SCHEMA = "pact-v4-strict-chapter-trial-journal/v2"
RECORD_SCHEMA = "pact-v4-strict-chapter-trial/v2"
AUDIT_CACHE_SCHEMA = "pact-v4-strict-audit-cache/v1"
AUDIT_FINDINGS_SCHEMA = "pact-v4-strict-audit-findings/v1"
HANDOFF_SCHEMA = "pact-v4-step6-b2-handoff/v1"
SELECTION_META_SCHEMA = "pact-v4-strict-selection-meta/v1"
REPAIR_CACHE_SCHEMA = "pact-v4-phase4-repair-cache/v1"
REPAIR_REPORT_SCHEMA = "pact-v4-phase4-repair-report/v1"

NO_LEFT_CONTEXT_SENTINEL = "pact-v4-strict/no-left-context"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# The historical local-only backend config is now a tagged runtime config
# (``pact_v4.runtime.runtime_config.LocalLlamaBackendConfig``, plan §9.2).
# The name ``StrictBackendConfig`` is preserved as an alias so existing
# imports/tests and old local run configs keep working unchanged.
StrictBackendConfig = LocalLlamaBackendConfig


@dataclass(frozen=True)
class StrictRunConfig:
    chapter_id: str
    chapter_html_path: Path
    memory_dir: Path
    out_dir: Path
    backend: BackendRuntimeConfig
    min_chunk_words: int = DEFAULT_MIN_WORDS
    target_chunk_words: int = DEFAULT_TARGET_WORDS
    max_chunk_words: int = DEFAULT_MAX_WORDS
    right_context_pids: int = 0
    temperature: float = 0.2
    seed: int = 7
    max_tokens: int = 8192
    deterministic_glossary_terms: Tuple[Tuple[str, str], ...] = ()
    deterministic_names: Tuple[Tuple[str, str], ...] = ()
    deterministic_mixed_script_allow: Tuple[str, ...] = ()
    config_version: str = "pact-v4-driver/phase12/strict/v1"
    run_label: str = "v4-phase12-strict"
    # Operational policy pinned before the run (see module docstring #4),
    # not decided post hoc: N consecutive chunks that fail to produce a
    # selected translation halt the chapter rather than cascading empty
    # left_context to the end.
    max_consecutive_terminal_nonselections: int = 3
    # Phase 5 formatting policy (§8.14, B3): blocking-integrity limit on
    # unresolved required spans and the policy identity. Production default
    # ``max_formatting_incidents=0`` — any unresolved required inline span
    # prevents ``complete`` (the chapter degrades to ``accepted_degraded``
    # when the PID map stays structurally valid).
    max_formatting_incidents: int = 0
    formatting_required: bool = True
    formatting_policy_version: str = "pact-v4-formatting/v1"

    def to_config_artifact(self, *, model_profile: str) -> ConfigArtifact:
        return build_config_artifact(
            version=self.config_version,
            values={
                "chapter_id": self.chapter_id,
                "model_profile": model_profile,
                "chunk_min_words": self.min_chunk_words,
                "chunk_target_words": self.target_chunk_words,
                "chunk_max_words": self.max_chunk_words,
                "right_context_pids": self.right_context_pids,
                "generation": {
                    "temperature": self.temperature,
                    "seed": self.seed,
                    "max_tokens": self.max_tokens,
                    "reasoning": 0,
                },
                "formatting": {
                    "required": self.formatting_required,
                    "max_incidents": self.max_formatting_incidents,
                    "policy_version": self.formatting_policy_version,
                },
                # B5 mixed_script-политика: the manual allowlist is a gate-policy
                # input, so it is part of the run's config identity — changing it
                # invalidates cache/resume exactly like a memory/source change.
                "deterministic_mixed_script_allow": list(self.deterministic_mixed_script_allow),
            },
        )


def build_strict_lifecycle(
    backend: StrictBackendConfig, *, log_dir: Path,
) -> Tuple[ModelRouter, Any, Any, Any, Any, Any]:
    """Wire up the real ``llama-server``-backed lifecycle for a live run.

    Returns ``(router, model_caller, qwen_evaluator, gemma_selector,
    qwen_audit_evaluator, gemma_audit_evaluator)``, the six objects
    ``run_chapter_strict`` needs injected. Kept separate from
    ``run_chapter_strict`` itself so tests can inject fakes instead
    (``tests/pact_v4/pipeline/test_v4_phase12_strict_runner.py``) without
    ever constructing a real ``LifecycleAdapter`` / spawning
    ``llama-server``. The router is built once here and handed to the
    coordinator; the runner adapters are the lifecycle-aware wrappers over
    that same router.
    """
    runtime = backend.build_runtime(log_dir=log_dir)
    router = runtime.router
    model_caller = LifecycleModelCaller(router, model_name=backend.model_names[GEMMA_MODEL_KEY])
    qwen_evaluator = LifecycleQwenEvaluator(router, model_name=backend.model_names[QWEN_MODEL_KEY])
    gemma_selector = LifecycleGemmaSelector(router, model_name=backend.model_names[GEMMA_MODEL_KEY])
    qwen_audit_evaluator = LifecycleQwenAuditEvaluator(
        router, model_name=backend.model_names[QWEN_MODEL_KEY],
    )
    gemma_audit_evaluator = LifecycleGemmaAuditEvaluator(
        router, model_name=backend.model_names[GEMMA_MODEL_KEY],
    )
    return router, model_caller, qwen_evaluator, gemma_selector, qwen_audit_evaluator, gemma_audit_evaluator


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------

@dataclass
class JournalEntry:
    chunk_index: int
    chunk_id: str
    parent_chunk_id: Optional[str]
    parent_context_state_hash: str
    left_context_kind: str
    left_context_hash: str
    snapshot_hash: str
    chunk_plan_hash: str
    config_identity: str
    backend_identity_hash: str
    candidate_ids: List[str]
    gate_trace: List[Dict[str, Any]]
    outcome: str  # "selected" | "quarantined" | "needs_synthesis" | "incomplete_generation"
    selected_candidate_id: Optional[str]
    selected_role: Optional[str]
    switch_indices: List[int]  # indices into the run's flat local-switch list
    backend_event_indices: List[int] = field(default_factory=list)  # indices into the run's flat backend-event list

    def to_json(self) -> Dict[str, Any]:
        return {"schema": JOURNAL_SCHEMA, **self.__dict__}


def _left_context_hash(left_context: Tuple[Tuple[str, str], ...]) -> str:
    if not left_context:
        return canonical_json_hash({"left_context": NO_LEFT_CONTEXT_SENTINEL})
    return canonical_json_hash({"left_context": list(left_context)})


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Write-then-rename so a crash mid-write never leaves a truncated file.

    Used for ``translations.json``, which resume reads back to
    reconstruct ``selected_text_by_chunk``. Without this, a crash between
    ``open(..., 'w')`` and the write completing could leave a partial/
    corrupt file that a later resume would silently misread as having
    fewer committed PIDs than the journal says are selected.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _load_journal(journal_path: Path) -> List[Dict[str, Any]]:
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
# Result
# ---------------------------------------------------------------------------

@dataclass
class StrictChapterRunResult:
    chapter_id: str
    out_dir: Path
    chunk_count: int
    processed_count: int
    selected_count: int
    quarantined_count: int
    needs_synthesis_count: int
    incomplete_generation_count: int
    selected_role_counts: Dict[str, int]
    halted_early: bool
    halt_reason: Optional[str]
    resumed_from_index: int
    switches: List[Any]
    translations_path: Path
    journal_path: Path
    record_path: Path
    record: Dict[str, Any]
    step6: Dict[str, Any] = field(default_factory=dict)
    step7: Dict[str, Any] = field(default_factory=dict)
    step8: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Phase 3B Step 6 audit (assembled-chapter audit), per DECISIONS 2026-08-01
# ---------------------------------------------------------------------------

# The Step 6 audit reconstructs the winning ``Candidate`` objects from what
# was actually committed (journal-driven selection records + committed
# translations). ``Candidate.create`` re-validates every candidate against
# source/snapshot/plan/config, so a stale or fabricated identity cannot enter
# the assembled chapter.


class _IncompleteSelectionError(ValueError):
    """A selected chunk's committed translation does not cover its plan PIDs.

    Raised by ``_audit_candidate_map`` instead of letting ``Candidate.create``
    fail with a generic ownership ``ValueError``, so the Step 6 audit can
    report the distinct ``incomplete_translation`` skip reason. Normally
    unreachable (the strict driver only writes complete per-chunk committed
    text); it fires when prior translations were tampered or a resume
    reconstruction came up short of the plan.
    """


# ---------------------------------------------------------------------------
# Best-variant selection for quarantined chunks (owner decision 2026-08-02)
# ---------------------------------------------------------------------------

# The deterministic rule recorded in ``b2_handoff.json``: a quarantined
# chunk's best-variant is the one with the most passed gates on its own
# ``decision_trace``, ties broken by role priority, then lexicographic
# ``candidate_id``. The rule is recorded verbatim in the artifact so a
# reviewer never has to infer it from behaviour.
BEST_VARIANT_RULE = (
    "max_gates_passed>role(fidelity_first>balanced_literary>synthesis)>candidate_id"
)

_ROLE_PRIORITY = {"fidelity_first": 0, "balanced_literary": 1, "synthesis": 2}


def _gates_passed(decision_trace: List[Dict[str, Any]]) -> int:
    return sum(1 for gate in decision_trace if gate.get("passed"))


def _pick_best_variant(variants: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Deterministically pick one best-variant among a quarantined chunk's
    produced variants (serialized generation-outcome records).

    ``None`` when there are no variants. The selection is a total order, so it
    is reproducible across resume sessions — a resumed run reconstructs the
    same best-variant candidate, hence the same assembled chapter and audit
    cache identity.
    """
    if not variants:
        return None

    def _key(variant: Dict[str, Any]) -> Tuple[int, int, str]:
        return (
            -_gates_passed(variant.get("decision_trace", [])),
            _ROLE_PRIORITY.get(variant.get("role"), 99),
            variant["candidate_id"],
        )

    return min(variants, key=_key)


def _candidate_from_generation_record(
    variant: Dict[str, Any],
    *,
    chunk_id: str,
    source: Any,
    snapshot: Any,
    chunk_plan: ChunkPlanArtifact,
    config: ConfigArtifact,
) -> Candidate:
    """Reconstruct one ``Candidate`` from a serialized generation-outcome record.

    ``Candidate.create`` re-validates every identity against
    source/snapshot/plan/config, so a stale or fabricated variant cannot enter
    the audit. Translation PID order is rebuilt from the chunk plan (the
    persisted record stores a PID->text map, not an ordered tuple).
    """
    chunk = chunk_plan.chunk(chunk_id)
    translation_map = variant["translation"]
    translation = tuple((pid, translation_map[pid]) for pid in chunk.pids)
    decision_trace = tuple(
        GateResult(
            gate=str(gate["gate"]),
            passed=bool(gate.get("passed", False)),
            detail=str(gate.get("detail", "")),
        )
        for gate in variant.get("decision_trace", [])
    )
    return Candidate.create(
        candidate_id=variant["candidate_id"],
        chunk_id=chunk_id,
        role=variant["role"],
        translation=translation,
        source=source,
        snapshot=snapshot,
        chunk_plan=chunk_plan,
        config=config,
        decision_trace=decision_trace,
    )


def _audit_candidate_map(
    *,
    selection_records: List[Dict[str, Any]],
    selected_text_by_chunk: Dict[str, Dict[str, str]],
    generation_records: List[Dict[str, Any]],
    chunk_plan: ChunkPlanArtifact,
    source: Any,
    snapshot: Any,
    config: ConfigArtifact,
) -> Tuple[Dict[str, Candidate], List[Dict[str, Any]]]:
    """Build the Step 6 candidate map + the per-chunk ``b2_handoff.json`` rows.

    Rules (owner decision 2026-08-02):

      * selected chunk — the committed winning candidate (identity re-validated
        through ``Candidate.create``);
      * quarantined chunk — the deterministic **best-variant** among the
        variants it actually produced (``_pick_best_variant``), also recreated
        through ``Candidate.create``. This is a *diagnostic* read, never an
        acceptance: the chunk stays ``quarantined`` in the handoff even if its
        best-variant audits clean;
      * needs_synthesis / incomplete_generation / never-processed chunk — no
        candidate; the deterministic audit layer covers its PIDs as ``missing``
        (tagged ``pact_v4.phase3.audit.NO_CANDIDATE_MARKER``), and no model
        unit is attempted for it.

    Handoff-row contract (B2 input, pinned at review of PR #108):

      * ``uncovered_pids`` is **structural** coverage only: the chunk's plan
        PIDs that have no candidate translation in the assembled chapter
        (always empty for a chunk with an audited candidate). It says nothing
        about audit *completeness* — that is carried by ``audit_status``
        (filled in ``_run_step6_audit`` after the audit runs):
        ``clean`` / ``findings_present`` / ``unit_failed`` for a chunk with an
        audited candidate, ``no_candidate`` otherwise. A selected chunk whose
        Qwen or Gemma unit failed therefore reports ``uncovered_pids=[]`` but
        ``audit_status="unit_failed"`` — B2 must key off ``audit_status``, not
        infer it from ``uncovered_pids`` + ``committed``.
      * ``gate_trace`` reads either ``decision_trace`` (selection records,
        generation records) or ``gate_trace`` (journal) — both spell the same
        cascade trace; do not "normalise" one side without the other.

    Raises ``_IncompleteSelectionError`` (data-integrity, ``incomplete_translation``)
    when a chunk is selected in the journal but its committed translation no
    longer covers the chunk's plan PIDs.
    """
    selected_meta = {
        rec["chunk_id"]: rec
        for rec in selection_records
        if rec.get("status") == "selected" and rec.get("selected_candidate_id")
    }
    gen_by_chunk = {rec["chunk_id"]: rec for rec in generation_records}

    candidates: Dict[str, Candidate] = {}
    handoff_rows: List[Dict[str, Any]] = []

    for chunk in chunk_plan.chunks:
        chunk_id = chunk.chunk_id
        plan_pids = list(chunk.pids)
        record = next((r for r in selection_records if r["chunk_id"] == chunk_id), None)
        status = record.get("status") if record else "incomplete_generation"
        gen = gen_by_chunk.get(chunk_id)
        variants = list(gen["candidates"].values()) if gen else []
        gate_trace = (
            list(record.get("decision_trace") or record.get("gate_trace") or [])
            if record else []
        )
        available_variants = [
            {
                "candidate_id": variant["candidate_id"],
                "role": variant["role"],
                "gates_passed": _gates_passed(variant.get("decision_trace", [])),
            }
            for variant in variants
        ]

        row: Dict[str, Any] = {
            "chunk_id": chunk_id,
            "plan_pids": plan_pids,
            "status": "audited" if status == "selected" else status,
            "committed": status == "selected",
            "audited_candidate_id": None,
            "audited_role": None,
            "best_variant_rule": None,
            "available_variants": available_variants,
            "quarantine_reason": record.get("quarantine_reason") if record else None,
            "gate_trace": gate_trace,
            "uncovered_pids": plan_pids,
            # Filled in _run_step6_audit once the audit outcome is known.
            "audit_status": None,
        }

        if status == "selected":
            rec = selected_meta.get(chunk_id)
            text = selected_text_by_chunk.get(chunk_id)
            if text is None:
                raise _IncompleteSelectionError(
                    f"chunk {chunk_id}: selected in the journal but has no "
                    "committed translation to audit"
                )
            if set(text) != set(chunk.pids):
                raise _IncompleteSelectionError(
                    f"chunk {chunk_id}: committed translation covers "
                    f"{len(text)}/{len(chunk.pids)} PIDs; refusing to audit a "
                    "partially reconstructed chunk"
                )
            candidate = Candidate.create(
                candidate_id=rec["selected_candidate_id"],
                chunk_id=chunk_id,
                role=rec["selected_role"],
                translation=tuple(text.items()),
                source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config,
            )
            candidates[chunk_id] = candidate
            row["committed"] = True
            row["audited_candidate_id"] = candidate.candidate_id
            row["audited_role"] = candidate.role
            row["uncovered_pids"] = []
        elif status == "quarantined":
            best = _pick_best_variant(variants)
            if best is not None:
                try:
                    candidate = _candidate_from_generation_record(
                        best, chunk_id=chunk_id, source=source, snapshot=snapshot,
                        chunk_plan=chunk_plan, config=config,
                    )
                except ValueError:
                    # Our own generation records are validated when written, so
                    # a reconstruction failure means corrupt/foreign data. The
                    # audit is diagnostic: cover the chunk as missing rather
                    # than fabricate or crash the whole chapter audit.
                    LOG.exception(
                        "Step 6: best-variant for quarantined chunk %s could not "
                        "be reconstructed; covering it as missing coverage",
                        chunk_id,
                    )
                    candidate = None
                if candidate is not None:
                    candidates[chunk_id] = candidate
                    row["audited_candidate_id"] = candidate.candidate_id
                    row["audited_role"] = candidate.role
                    row["best_variant_rule"] = BEST_VARIANT_RULE
                    row["uncovered_pids"] = []

        handoff_rows.append(row)

    return candidates, handoff_rows


def _fill_audit_status(
    handoff_rows: List[Dict[str, Any]],
    *,
    candidates: Mapping[str, Candidate],
    outcome: Any,
) -> None:
    """Tag every handoff row with its per-chunk ``audit_status``.

    Values (see ``_audit_candidate_map`` docstring):

      * ``no_candidate`` — chunk has no auditable candidate; deterministic
        ``missing`` coverage only (no model unit existed to fail).
      * ``unit_failed`` — at least one (chunk, detector) model unit failed;
        the audit for this chunk is incomplete, so it is NOT ``clean`` even
        if ``uncovered_pids`` is empty.
      * ``findings_present`` — both model units succeeded and at least one
        finding (any detector) was recorded for the chunk.
      * ``clean`` — both model units succeeded with no findings.

    ``audit_status`` is orthogonal to the row's ``status``: a quarantined
    chunk whose best-variant audited clean stays ``status="quarantined"``
    with ``audit_status="clean"`` (diagnostic, not acceptance).
    """
    failed_by_chunk = {unit[0] for unit in outcome.failed_units}
    for row in handoff_rows:
        chunk_id = row["chunk_id"]
        if row["audited_candidate_id"] is None or chunk_id not in candidates:
            row["audit_status"] = "no_candidate"
        elif chunk_id in failed_by_chunk:
            row["audit_status"] = "unit_failed"
        elif any(finding.chunk_id == chunk_id for finding in outcome.store):
            row["audit_status"] = "findings_present"
        else:
            row["audit_status"] = "clean"


def _audit_cache_path(out_dir: Path) -> Path:
    return out_dir / "audit_cache.json"


def _audit_findings_path(out_dir: Path) -> Path:
    return out_dir / "audit_findings.json"


def _b2_handoff_path(out_dir: Path) -> Path:
    return out_dir / "b2_handoff.json"


def _generation_outcomes_path(out_dir: Path) -> Path:
    return out_dir / "generation_outcomes.json"


def _selection_meta_path(out_dir: Path) -> Path:
    return out_dir / "selection_meta.json"


def _merge_selection_meta(
    out_dir: Path,
    current_records: List[Dict[str, Any]],
    *,
    snapshot: Any,
    chunk_plan: ChunkPlanArtifact,
    config: ConfigArtifact,
) -> List[Dict[str, Any]]:
    """Merge the persisted ``selection_meta.json`` with this session's records.

    ``selection_meta.json`` is the cumulative per-chunk selection sidecar that
    survives resume (same lifecycle as ``generation_outcomes.json``). The
    journal (v1) does not persist ``quarantine_reason``/``synthesis_reason``,
    so without this sidecar a resumed run's Step 6 handoff would silently
    record ``quarantine_reason=None`` for quarantined chunks of earlier
    sessions (review PR #108, issue 2). This merge also fixes the
    ``selection_results.json`` reconstruction gap recorded in ``DECISIONS.md``
    2026-08-01: prior sessions' rich records are restored.

    Precedence: the persisted record wins for a chunk that was **resumed**
    (its current-session journal-derived entry is only a stub), while a chunk
    actually processed in this session overrides its prior record. A resumed
    stub is kept as a **fallback** when no richer persisted record exists at
    all — e.g. a pre-sidecar run (no ``selection_meta.json``) resumed in full,
    where dropping the stubs would empty the map and Step 6 would report
    ``no_selected_chunks`` instead of auditing the committed text.
    """
    path = _selection_meta_path(out_dir)
    prior: List[Dict[str, Any]] = []
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema") != SELECTION_META_SCHEMA:
            raise ValueError(
                f"Foreign identity: selection_meta schema={payload.get('schema')!r}"
            )
        if (
            payload.get("snapshot_hash") != snapshot.snapshot_hash
            or payload.get("chunk_plan_hash") != chunk_plan.plan_hash
            or payload.get("config_identity") != config.config_identity
        ):
            raise ValueError(
                "Foreign identity: selection_meta.json was written under a "
                "different snapshot/plan/config than this run -- refusing to "
                "mix selection records across runs."
            )
        records = payload.get("records")
        if not isinstance(records, list):
            raise ValueError("selection_meta.json: records must be an array")
        prior = records
    merged = {rec.get("chunk_id"): rec for rec in prior if rec.get("chunk_id")}
    for rec in current_records:
        if not rec.get("chunk_id"):
            continue
        if rec.get("resumed"):
            merged.setdefault(rec["chunk_id"], rec)
        else:
            merged[rec["chunk_id"]] = rec
    return [merged[chunk.chunk_id] for chunk in chunk_plan.chunks if chunk.chunk_id in merged]


def _merge_generation_outcomes(
    out_dir: Path,
    current_records: List[Dict[str, Any]],
    *,
    snapshot: Any,
    chunk_plan: ChunkPlanArtifact,
    config: ConfigArtifact,
) -> List[Dict[str, Any]]:
    """Merge the persisted ``generation_outcomes.json`` with this session's
    records so Step 6 can select best-variants for quarantined chunks of
    *previous* sessions too.

    Resume only appends records for chunks processed in the current session,
    so without this the quarantined chunks of earlier sessions would have no
    recoverable variants and would silently degrade to ``missing`` coverage.
    The final write below persists the merged list, making the file cumulative
    across resumes (fixes the ``generation_outcomes.json`` reconstruction gap
    recorded in ``DECISIONS.md`` 2026-08-01).
    """
    path = _generation_outcomes_path(out_dir)
    prior: List[Dict[str, Any]] = []
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            payload.get("snapshot_hash") != snapshot.snapshot_hash
            or payload.get("chunk_plan_hash") != chunk_plan.plan_hash
            or payload.get("config_identity") != config.config_identity
        ):
            raise ValueError(
                "Foreign identity: generation_outcomes.json was written under "
                "a different snapshot/plan/config than this run -- refusing to "
                "mix generation records across runs."
            )
        outcomes = payload.get("outcomes")
        if not isinstance(outcomes, list):
            raise ValueError("generation_outcomes.json: outcomes must be an array")
        prior = outcomes
    merged = {rec.get("chunk_id"): rec for rec in prior if rec.get("chunk_id")}
    for rec in current_records:
        if rec.get("chunk_id"):
            merged[rec["chunk_id"]] = rec
    return [merged[chunk.chunk_id] for chunk in chunk_plan.chunks if chunk.chunk_id in merged]


def _load_audit_cache(
    path: Path, *, chapter_hash: str, snapshot_hash: str,
    chunk_plan_hash: str, config_identity: str,
    backend_identity_hashes: Sequence[str],
) -> Optional[AuditCache]:
    """Reload a previously persisted audit cache, refusing foreign identity.

    Every audit unit is keyed on ``chapter_hash`` + ``chunk_id`` +
    ``candidate_id`` + detector + policy version, so a cache whose assembled
    chapter differs cannot serve any unit (every key would miss). Two
    identities therefore play different roles here:

      * ``backend_identity_hashes`` (every hash this config considers
        resumable, e.g. a local config's legacy ``StrictBackendConfig`` hash)
        is the only envelope identity NOT captured by a unit key (the model
        that produced the findings is not part of the hash) — a cache written
        under a different backend must be rejected outright, never silently
        reused.
      * a ``chapter_hash`` / snapshot / plan / config mismatch means the
        assembled chapter legitimately changed — e.g. a resumed run commits
        more chunks than the previous session (auditing a partial chapter
        before this change produced no cache at all; now a partial audit
        cache exists and the next resume grows the chapter). Every old unit
        key misses anyway, so the cache is discarded in favour of a fresh
        one rather than treated as foreign-data corruption.
    """
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != AUDIT_CACHE_SCHEMA:
        raise ValueError(
            f"Foreign identity: audit cache schema={payload.get('schema')!r}"
        )
    if payload.get("backend_identity_hash") not in backend_identity_hashes:
        raise ValueError(
            f"Foreign identity: audit cache backend_identity_hash="
            f"{payload.get('backend_identity_hash')!r}, expected one of "
            f"{list(backend_identity_hashes)!r} -- refusing to resume against a "
            "cache written under a different model backend."
        )
    for field, expected in (
        ("chapter_hash", chapter_hash),
        ("snapshot_hash", snapshot_hash),
        ("chunk_plan_hash", chunk_plan_hash),
        ("config_identity", config_identity),
    ):
        if payload.get(field) != expected:
            LOG.info(
                "Audit cache from a different assembled chapter (%s differs); "
                "all unit keys miss, starting a fresh cache",
                field,
            )
            return None
    return AuditCache.from_payload(payload["cache"])


def _run_step6_audit(
    *,
    cfg: StrictRunConfig,
    source: Any,
    snapshot: Any,
    chunk_plan: ChunkPlanArtifact,
    config: ConfigArtifact,
    det_data: DeterministicGateData,
    selection_records: List[Dict[str, Any]],
    selected_text_by_chunk: Dict[str, Dict[str, str]],
    generation_records: List[Dict[str, Any]],
    qwen_audit_evaluator: Any,
    gemma_audit_evaluator: Any,
    backend_identity_hash: str,
    backend_identity_hashes: Sequence[str],
    progress: Optional[Any] = None,
) -> Dict[str, Any]:
    """Run the Step 6 assembled-chapter audit and persist its artifacts.

    Since the B1 follow-up (owner decision 2026-08-02) the audit runs over
    **every chunk**: selected chunks use their committed winner, quarantined
    chunks use the deterministic best-variant among their produced variants
    (``_audit_candidate_map``), and chunks with no candidate at all
    (needs_synthesis / incomplete_generation / never-processed) are covered by
    the deterministic ``missing`` layer without any model unit. The old
    ``skipped/partial_selection`` branch is gone — a partially selected run is
    audited exactly like a full one, and ``b2_handoff.json`` (the B2 input
    contract) records each chunk's true status, including the fact that
    auditing a quarantined chunk's best-variant is a *diagnosis*, never an
    acceptance: the chunk stays ``quarantined`` in the handoff even if the
    variant audited clean.

    ``reason="no_selected_chunks"`` (defense-in-depth) remains for a run with
    zero candidates at all (no selected chunk and no quarantined chunk with a
    recoverable variant), and ``reason="incomplete_translation"`` covers the
    data-integrity case where a chunk is selected in the journal but its
    committed text does not cover the chunk's plan PIDs (tampered/partial
    prior translations).

    Each handoff row additionally gets ``audit_status`` (``clean`` /
    ``findings_present`` / ``unit_failed`` / ``no_candidate``) once the audit
    outcome is known — so a selected chunk whose Qwen/Gemma unit failed is
    distinguishable from a clean one without cross-referencing
    ``audit_findings.json`` (review PR #108, issue 1).

    Findings are persisted as a dedicated run artifact via ``FindingStore``
    (append-only evidence, region resolver included); the journal stays v2.
    The ``AuditCache`` is persisted for resume: a resumed run reloads it and
    ``run_chapter_audit`` re-attempts only the unfinished ``(chunk_id,
    detector)`` units. ``b2_handoff.json`` is written whenever the audit runs
    (i.e. whenever at least one candidate exists).

    Returns ``(report, phase4_inputs)``: ``report`` is the JSON-serializable
    Step 6 summary recorded in the run record; ``phase4_inputs`` carries the
    Phase 4 (B2) objects derived from the audit (candidate map, handoff rows,
    findings store, region plan, assembled chapter) or ``None`` when the
    audit was skipped.
    """
    try:
        candidates, handoff_chunks = _audit_candidate_map(
            selection_records=selection_records,
            selected_text_by_chunk=selected_text_by_chunk,
            generation_records=generation_records,
            chunk_plan=chunk_plan, source=source, snapshot=snapshot, config=config,
        )
    except _IncompleteSelectionError as exc:
        return {
            "status": "skipped", "reason": "incomplete_translation",
            "detail": str(exc),
        }, None
    if not candidates:
        return {"status": "skipped", "reason": "no_selected_chunks"}, None

    chapter = AssembledChapter.assemble(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config,
        candidates=candidates,
    )
    cache_path = _audit_cache_path(cfg.out_dir)
    cache = _load_audit_cache(
        cache_path,
        chapter_hash=chapter.chapter_hash,
        snapshot_hash=snapshot.snapshot_hash,
        chunk_plan_hash=chunk_plan.plan_hash,
        config_identity=config.config_identity,
        backend_identity_hashes=backend_identity_hashes,
    ) or AuditCache()

    outcome = run_chapter_audit(
        chapter=chapter, source=source, chunk_plan=chunk_plan, candidates=candidates,
        qwen_evaluator=qwen_audit_evaluator, gemma_evaluator=gemma_audit_evaluator,
        det_data=det_data, cache=cache, progress=progress,
    )

    _atomic_write_json(cache_path, {
        "schema": AUDIT_CACHE_SCHEMA,
        "chapter_hash": chapter.chapter_hash,
        "snapshot_hash": snapshot.snapshot_hash,
        "chunk_plan_hash": chunk_plan.plan_hash,
        "config_identity": config.config_identity,
        "backend_identity_hash": backend_identity_hash,
        "cache": cache.to_payload(),
    })
    findings_payload = {
        "schema": AUDIT_FINDINGS_SCHEMA,
        "chapter_id": cfg.chapter_id,
        "source_hash": source.source_hash,
        "snapshot_hash": snapshot.snapshot_hash,
        "chunk_plan_hash": chunk_plan.plan_hash,
        "config_identity": config.config_identity,
        "backend_identity_hash": backend_identity_hash,
        "chapter_hash": outcome.chapter_hash,
        "status": outcome.status,
        "failed_units": [list(unit) for unit in outcome.failed_units],
        "store": outcome.store.to_payload(),
        "region_plan": outcome.region_plan.to_payload(),
    }
    _atomic_write_json(_audit_findings_path(cfg.out_dir), findings_payload)

    _fill_audit_status(handoff_chunks, candidates=candidates, outcome=outcome)

    handoff_payload = {
        "schema": HANDOFF_SCHEMA,
        "chapter_id": cfg.chapter_id,
        "source_hash": source.source_hash,
        "snapshot_hash": snapshot.snapshot_hash,
        "chunk_plan_hash": chunk_plan.plan_hash,
        "config_identity": config.config_identity,
        "backend_identity_hash": backend_identity_hash,
        "chapter_hash": outcome.chapter_hash,
        "best_variant_rule": BEST_VARIANT_RULE,
        "chunks": handoff_chunks,
    }
    _atomic_write_json(_b2_handoff_path(cfg.out_dir), handoff_payload)

    return {
        "status": outcome.status,
        "chapter_hash": outcome.chapter_hash,
        "finding_count": len(outcome.store),
        "region_count": len(outcome.region_plan),
        "failed_units": [list(unit) for unit in outcome.failed_units],
        "covered_chunks": len(candidates),
        "uncovered_chunks": len(chunk_plan.chunks) - len(candidates),
        "audit_cache_path": str(cache_path),
        "audit_findings_path": str(_audit_findings_path(cfg.out_dir)),
        "b2_handoff_path": str(_b2_handoff_path(cfg.out_dir)),
    }, {
        # Phase 4 (B2) inputs derived from the Step 6 audit: the assembled
        # chapter, the per-chunk candidate map, the handoff rows and the
        # findings/region plan. Consumed by ``_run_step7_repair`` when repair
        # adapters are configured; ``None`` when the audit was skipped.
        "candidates": candidates,
        "handoff_chunks": handoff_chunks,
        "findings_store": outcome.store,
        "region_plan": outcome.region_plan,
        "chapter": chapter,
    }


# ---------------------------------------------------------------------------
# Phase 4 Step 7/8: repair + convergence + terminal (B2)
# ---------------------------------------------------------------------------


def _repair_cache_path(out_dir: Path) -> Path:
    return out_dir / "repair_cache.json"


def _repair_report_path(out_dir: Path) -> Path:
    return out_dir / "repair_report.json"


def _formatting_report_path(out_dir: Path) -> Path:
    return out_dir / "formatting_report.json"


def _load_repair_cache(
    path: Path,
    *,
    chapter_hash: str,
    snapshot_hash: str,
    chunk_plan_hash: str,
    config_identity: str,
    backend_identity_hashes: Sequence[str],
) -> Optional[RepairCache]:
    """Reload a previously persisted repair cache, refusing foreign identity.

    Mirrors ``_load_audit_cache``: the cache is only reusable when the
    enclosing run's identities (chapter/snapshot/plan/config/backend) match,
    so a resumed run deterministically reuses already-committed repairs
    (same findings/re-gates) instead of re-paying model calls or silently
    reusing a cache written under a different backend.
    """
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != REPAIR_CACHE_SCHEMA:
        raise ValueError(
            f"Foreign identity: repair cache schema={payload.get('schema')!r}"
        )
    if payload.get("backend_identity_hash") not in backend_identity_hashes:
        raise ValueError(
            f"Foreign identity: repair cache backend_identity_hash="
            f"{payload.get('backend_identity_hash')!r}, expected one of "
            f"{list(backend_identity_hashes)!r} -- refusing to resume against a "
            "repair cache written under a different model backend."
        )
    for field, expected in (
        ("chapter_hash", chapter_hash),
        ("snapshot_hash", snapshot_hash),
        ("chunk_plan_hash", chunk_plan_hash),
        ("config_identity", config_identity),
    ):
        if payload.get(field) != expected:
            LOG.info(
                "Repair cache from a different chapter/run (%s differs); "
                "starting a fresh repair cache",
                field,
            )
            return None
    return RepairCache.from_payload(payload["cache"])


def _build_phase4_provenance(
    *,
    source: Any,
    snapshot: Any,
    chunk_plan: ChunkPlanArtifact,
    config: ConfigArtifact,
    chapter_hash: str,
) -> Provenance:
    """Build the Phase 4 provenance binding the terminal decision to this run.

    ``prompt_bundle_hash`` is derived from the run identities + chapter hash
    (the strict driver has no single prompt bundle; the value is a
    deterministic content identity, not a caller-supplied string).
    """
    return Provenance(
        source_hash=source.source_hash,
        chapter_snapshot_hash=snapshot.snapshot_hash,
        chunk_plan_hash=chunk_plan.plan_hash,
        prompt_bundle_hash=canonical_json_hash({
            "artifact": "pact-v4-phase4/v1",
            "chapter_hash": chapter_hash,
            "config_identity": config.config_identity,
        }),
        config_identity=config.config_identity,
        code_version="pact-v4-b2/1",
        policy_versions={
            "repair": "pact-v4-repair-policy/v1",
            "terminal": "pact-v4-terminal/v1",
        },
    )


def _run_step7_repair(
    *,
    cfg: StrictRunConfig,
    source: Any,
    snapshot: Any,
    chunk_plan: ChunkPlanArtifact,
    config: ConfigArtifact,
    det_data: DeterministicGateData,
    phase4_inputs: Dict[str, Any],
    repair_adapters: Sequence[Any],
    backend_identity_hash: str,
    backend_identity_hashes: Sequence[str],
    now: Any,
    formatting: Optional[Any] = None,
    progress: Optional[Any] = None,
) -> Dict[str, Any]:
    """Run Phase 4 (Step 7 repair + Step 8 terminal) and persist its artifacts.

    ``repair_adapters`` is the ``(repair_caller, region_fidelity_gate,
    qwen_audit_evaluator, gemma_audit_evaluator)`` tuple built by
    ``build_repair_adapters`` (Backend adapters over the coordinator
    ``CompletionBackend``; ``region_fidelity_gate`` is the L2b narrow
    per-region Qwen re-gate). ``phase4_inputs`` is the second element returned
    by ``_run_step6_audit`` (candidates, handoff rows, findings store, region
    plan, assembled chapter).

    ``formatting`` (B3) is the Phase 5 formatting step (a closure over
    ``pact_v4.phase5.formatting.run_formatting_align`` built by
    ``run_chapter_strict``) applied between convergence and Step 8. When
    present, its outcome is persisted as a dedicated ``formatting_report.json``
    (with backend identity) and summarized in the returned Step 7/8 block.

    The repair cache is loaded with identity checks on resume so a resumed
    run reuses committed repairs deterministically. The ``repair_report.json``
    (with backend identity) records rounds, gate history, debt trace and the
    monotonic terminal state. B6: the report also carries the
    ``quarantined_final`` / ``retry_attempts`` defaults, updated by the
    separate quarantined-retry cycle (``_run_quarantined_retry_cycle``) when
    one runs.

    Returns ``(summary, phase_result)`` — the Step 7/8 summary recorded in the
    run record plus the full ``RepairPhaseResult`` the B6 retry cycle needs to
    inspect the repair debt and the final translation.
    """
    repair_caller, region_fidelity_gate, qwen_audit_evaluator, gemma_audit_evaluator = repair_adapters

    candidates = phase4_inputs["candidates"]
    handoff_chunks = phase4_inputs["handoff_chunks"]
    findings_store = phase4_inputs["findings_store"]
    region_plan = phase4_inputs["region_plan"]
    chapter = phase4_inputs["chapter"]
    current_translation = chapter.as_pid_map()

    cache_path = _repair_cache_path(cfg.out_dir)
    cache = _load_repair_cache(
        cache_path,
        chapter_hash=chapter.chapter_hash,
        snapshot_hash=snapshot.snapshot_hash,
        chunk_plan_hash=chunk_plan.plan_hash,
        config_identity=config.config_identity,
        backend_identity_hashes=backend_identity_hashes,
    ) or RepairCache()

    provenance = _build_phase4_provenance(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan,
        config=config, chapter_hash=chapter.chapter_hash,
    )

    result = run_repair_phase(
        source=source,
        snapshot=snapshot,
        chunk_plan=chunk_plan,
        config=config,
        provenance=provenance,
        det_data=det_data,
        handoff_chunks=handoff_chunks,
        findings_store=findings_store,
        candidates=candidates,
        current_translation=current_translation,
        repair_caller=repair_caller,
        region_fidelity_gate=region_fidelity_gate,
        qwen_audit_evaluator=qwen_audit_evaluator,
        gemma_audit_evaluator=gemma_audit_evaluator,
        backend_identity_hash=backend_identity_hash,
        cache=cache,
        max_rounds=2,
        chapter_hash=chapter.chapter_hash,
        formatting=formatting,
        progress=progress,
    )

    _atomic_write_json(cache_path, {
        "schema": REPAIR_CACHE_SCHEMA,
        "chapter_hash": chapter.chapter_hash,
        "snapshot_hash": snapshot.snapshot_hash,
        "chunk_plan_hash": chunk_plan.plan_hash,
        "config_identity": config.config_identity,
        "backend_identity_hash": backend_identity_hash,
        "cache": cache.to_payload(),
    })
    report = result.to_payload()
    report["chapter_id"] = cfg.chapter_id
    report["finished_at"] = now().isoformat(timespec="seconds")
    # B6 defaults: the separate quarantined-retry cycle (if any) updates these
    # to the retry's real outcome before the report is read by consumers.
    report["quarantined_final"] = False
    report["retry_attempts"] = 0
    _atomic_write_json(_repair_report_path(cfg.out_dir), report)

    formatting_block: Optional[Dict[str, Any]] = None
    if result.formatting is not None:
        formatting_payload = result.formatting.to_payload()
        _atomic_write_json(_formatting_report_path(cfg.out_dir), {
            "schema": FORMATTING_REPORT_SCHEMA,
            "chapter_id": cfg.chapter_id,
            "source_hash": source.source_hash,
            "snapshot_hash": snapshot.snapshot_hash,
            "chunk_plan_hash": chunk_plan.plan_hash,
            "config_identity": config.config_identity,
            "backend_identity_hash": backend_identity_hash,
            "outcome": formatting_payload,
        })
        formatting_block = {
            "status": "blocking" if formatting_payload["blocking"] else "ok",
            "resolved_count": formatting_payload["resolved_count"],
            "incident_count": formatting_payload["incident_count"],
            "model_fallback_count": formatting_payload["model_fallback_count"],
            "max_formatting_incidents": formatting_payload["max_formatting_incidents"],
            "report_path": str(_formatting_report_path(cfg.out_dir)),
        }

    summary = {
        "status": result.status,
        "rounds": len(result.rounds),
        "repair_count": sum(
            len(round.records) for round in result.rounds
        ),
        "committed_count": sum(
            sum(1 for rec in round.records if rec.committed)
            for round in result.rounds
        ),
        "debt_count": len(result.debt_trace),
        "integrity": result.integrity,
        "terminal": result.terminal.status,
        "formatting": formatting_block,
        "report_path": str(_repair_report_path(cfg.out_dir)),
        "cache_path": str(cache_path),
    }
    return summary, result


# ---------------------------------------------------------------------------
# B6: separate quarantined-retry cycle (V4_B6_QUARANTINED_RETRY_TASK_RU.md)
# ---------------------------------------------------------------------------


def _quarantined_retry_path(out_dir: Path) -> Path:
    return out_dir / "quarantined_retry.json"


def _load_prior_quarantined_retries(
    out_dir: Path,
    *,
    snapshot: Any,
    chunk_plan: ChunkPlanArtifact,
    config: ConfigArtifact,
    acceptable_backend_hashes: Sequence[str],
) -> Dict[str, QuarantinedRetryAttempt]:
    """Reload a prior session's retry history, refusing foreign identity.

    Mirrors ``_merge_generation_outcomes`` / ``_load_repair_cache``: the
    retry history is only reusable when the enclosing run's identities
    (snapshot/plan/config/backend) match, so a resumed run deterministically
    reuses already-recorded attempts instead of re-paying the bounded
    regeneration or silently mixing retry state across runs.
    """
    path = _quarantined_retry_path(out_dir)
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != QUARANTINED_RETRY_SCHEMA:
        raise ValueError(
            f"Foreign identity: quarantined_retry schema={payload.get('schema')!r}"
        )
    if (
        payload.get("snapshot_hash") != snapshot.snapshot_hash
        or payload.get("chunk_plan_hash") != chunk_plan.plan_hash
        or payload.get("config_identity") != config.config_identity
    ):
        raise ValueError(
            "Foreign identity: quarantined_retry.json was written under a "
            "different snapshot/plan/config than this run -- refusing to mix "
            "quarantined retry state across runs."
        )
    if payload.get("backend_identity_hash") not in acceptable_backend_hashes:
        raise ValueError(
            f"Foreign identity: quarantined_retry.json backend_identity_hash="
            f"{payload.get('backend_identity_hash')!r}, expected one of "
            f"{list(acceptable_backend_hashes)!r} -- refusing to resume against "
            "retry history written under a different model backend."
        )
    attempts = payload.get("attempts") or []
    if not isinstance(attempts, list):
        raise ValueError("quarantined_retry.json: attempts must be an array")
    return {
        item["chunk_id"]: QuarantinedRetryAttempt.from_payload(item)
        for item in attempts
        if item.get("chunk_id")
    }


def _run_quarantined_retry_cycle(
    *,
    cfg: StrictRunConfig,
    source: Any,
    snapshot: Any,
    chunk_plan: ChunkPlanArtifact,
    config: ConfigArtifact,
    det_data_base: DeterministicGateData,
    det_data_full: DeterministicGateData,
    risk_by_chunk: Mapping[str, Any],
    glossary: Sequence[Any],
    generation_params: GenerationParams,
    model_caller: Any,
    gen_cache: Any,
    qwen_evaluator: Any,
    gemma_selector: Any,
    selected_text_by_chunk: Dict[str, Dict[str, str]],
    phase4_inputs: Dict[str, Any],
    repair_phase_result: RepairPhaseResult,
    repair_adapters: Sequence[Any],
    formatting_step: Optional[Any],
    backend_identity_hash: str,
    acceptable_backend_hashes: Sequence[str],
    now: Any,
    progress: Optional[Any],
    existing_generation_records: Sequence[Mapping[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Run the separate quarantined-retry cycle (V4 B6).

    Triggered after Step 7/8 when at least one quarantined chunk still has
    repair debt. The cycle (Variant A of the card, bounded 1 retry):

      1. regenerate the quarantined chunks with look-ahead right_context (the
         next chunk's English source) and re-run the cascade;
      2. a cascade winner **replaces the best-variant**: its text is written
         into the final translation and the chunk is re-audited
         (``_reaudit_chunks``) and re-repaired (one ``_run_repair_round``),
         so stale debt on the old best-variant is dropped, never carried;
      3. a chunk that still fails the cascade is accepted as final with its
         best-variant (``quarantined_final``) and its debt stays.

    Persists ``quarantined_retry.json`` (history, resume-validated),
    re-writes ``repair_report.json`` / ``repair_cache.json`` /
    ``formatting_report.json`` with the retry outcome, and returns the
    retry summary (including the merged generation records so the caller's
    ``generation_outcomes.json`` write carries the new candidates).

    Returns ``None`` when there is nothing to retry. A transport/gate failure
    inside the cycle is the caller's concern (the driver records it as
    ``step7.quarantined_retry.status="failed"``, never a semantic terminal
    status).
    """
    handoff_chunks = phase4_inputs["handoff_chunks"]
    prior_attempts = _load_prior_quarantined_retries(
        cfg.out_dir,
        snapshot=snapshot,
        chunk_plan=chunk_plan,
        config=config,
        acceptable_backend_hashes=acceptable_backend_hashes,
    )
    # Trigger = quarantined chunks that carry current repair debt OR have a
    # prior retry attempt. The prior-history branch matters on resume: the
    # prior session's retry candidate may already be the best-variant (so
    # Step 6 audits it clean and there is no *fresh* repair debt), but the
    # retry markers/terminal must still be recomputed consistently and the
    # prior attempt reused instead of re-paying the regeneration.
    debt_chunks = set(quarantined_chunks_with_debt(handoff_chunks, repair_phase_result))
    quarantined_chunks = {
        row["chunk_id"]
        for row in handoff_chunks
        if row.get("status") == "quarantined"
    }
    chunk_ids = sorted(debt_chunks | (set(prior_attempts) & quarantined_chunks))
    if not chunk_ids:
        return None

    result = run_quarantined_retry(
        chunk_ids=chunk_ids,
        source=source,
        snapshot=snapshot,
        chunk_plan=chunk_plan,
        config=config,
        det_data_base=det_data_base,
        risk_by_chunk=risk_by_chunk,
        glossary=tuple(glossary),
        selected_text_by_chunk=selected_text_by_chunk,
        generation_params=generation_params,
        model_caller=model_caller,
        gen_cache=gen_cache,
        qwen_evaluator=qwen_evaluator,
        gemma_selector=gemma_selector,
        prior_attempts=prior_attempts,
    )

    _atomic_write_json(_quarantined_retry_path(cfg.out_dir), {
        "schema": QUARANTINED_RETRY_SCHEMA,
        "policy_version": QUARANTINED_RETRY_POLICY_VERSION,
        "chapter_id": cfg.chapter_id,
        "source_hash": source.source_hash,
        "snapshot_hash": snapshot.snapshot_hash,
        "chunk_plan_hash": chunk_plan.plan_hash,
        "config_identity": config.config_identity,
        "backend_identity_hash": backend_identity_hash,
        "attempts": [attempt.to_payload() for attempt in result.attempts],
    })

    selected_chunks = list(result.selected_chunk_ids)
    # A cascade winner replaces the best-variant text; the candidate map is
    # updated so the re-audit / repair round keys off the new winner's role.
    final_map = dict(repair_phase_result.final_translation)
    candidates = dict(phase4_inputs["candidates"])
    for chunk_id, candidate in result.candidates:
        final_map.update(candidate.as_pid_map())
        candidates[chunk_id] = candidate

    retry_debt: List[str] = []
    retry_round_payload: Optional[Dict[str, Any]] = None
    if selected_chunks:
        chunk_translation = {
            chunk_id: {
                pid: final_map.get(pid, "")
                for pid in chunk_plan.chunk(chunk_id).pids
            }
            for chunk_id in selected_chunks
        }
        reaudit_findings = _reaudit_chunks(
            source=source,
            snapshot=snapshot,
            chunk_plan=chunk_plan,
            config=config,
            det_data=det_data_full,
            translation_by_chunk=chunk_translation,
            chunk_ids=selected_chunks,
            qwen_audit_evaluator=repair_adapters[2],
            gemma_audit_evaluator=repair_adapters[3],
            progress=progress,
        )

        plans_by_chunk: Dict[str, list] = {}
        policy = SoftFindingsPolicy()
        for chunk_id in selected_chunks:
            chunk_findings = tuple(
                finding for finding in reaudit_findings
                if finding.chunk_id == chunk_id
            )
            if not chunk_findings:
                continue
            repairable, skipped = filter_soft_findings(chunk_findings, policy)
            for finding in skipped:
                note = (
                    str(finding.evidence.get("note", ""))
                    if isinstance(finding.evidence, Mapping)
                    else str(finding.evidence)
                )
                retry_debt.append(
                    f"{chunk_id}: {finding.region.pid}: soft Gemma finding "
                    f"({finding.category}) skipped by L3 policy after "
                    f"quarantined retry: {note}"
                )
            if not repairable:
                continue
            plans = plan_repairs_for_chunk(
                chunk=chunk_plan.chunk(chunk_id),
                findings=repairable,
                current_text={
                    pid: final_map.get(pid, "")
                    for pid in chunk_plan.chunk(chunk_id).pids
                },
                backend_identity_hash=backend_identity_hash,
            )
            if plans:
                plans_by_chunk[chunk_id] = list(plans)
            else:
                for finding in repairable:
                    retry_debt.append(
                        f"{chunk_id}: {finding.region.pid}: finding "
                        f"({finding.category}) unresolved after quarantined "
                        "retry (no region repair could be planned)"
                    )

        if plans_by_chunk:
            repair_cache = _load_repair_cache(
                _repair_cache_path(cfg.out_dir),
                chapter_hash=phase4_inputs["chapter"].chapter_hash,
                snapshot_hash=snapshot.snapshot_hash,
                chunk_plan_hash=chunk_plan.plan_hash,
                config_identity=config.config_identity,
                backend_identity_hashes=acceptable_backend_hashes,
            ) or RepairCache()
            if progress is not None:
                progress.repair_round_started(round_number=3)
            round_records, _changed = _run_repair_round(
                chapter_hash=phase4_inputs["chapter"].chapter_hash,
                chunk_plan=chunk_plan,
                plans_by_chunk=plans_by_chunk,
                candidates=candidates,
                source=source,
                snapshot=snapshot,
                config=config,
                det_data=det_data_full,
                translation_by_chunk={
                    chunk_id: {
                        pid: final_map.get(pid, "")
                        for pid in chunk_plan.chunk(chunk_id).pids
                    }
                    for chunk_id in plans_by_chunk
                },
                repair_caller=repair_adapters[0],
                region_fidelity_gate=repair_adapters[1],
                gemma_audit_evaluator=repair_adapters[3],
                backend_identity_hash=backend_identity_hash,
                cache=repair_cache,
                progress=progress,
            )
            for record in round_records:
                if record.committed:
                    for pid in record.target_pids:
                        final_map[pid] = dict(record.new_translation).get(pid, "")
                else:
                    retry_debt.append(
                        f"{record.chunk_id}: quarantined-retry repair "
                        f"{record.repair_id[:12]} not committed ({record.reason})"
                    )
            retry_round_payload = {
                "round_number": 3,
                "records": [record.to_payload() for record in round_records],
                "reaudit_findings": [
                    finding.to_payload() for finding in reaudit_findings
                ],
                "changed_chunk_ids": selected_chunks,
            }
            _atomic_write_json(_repair_cache_path(cfg.out_dir), {
                "schema": REPAIR_CACHE_SCHEMA,
                "chapter_hash": phase4_inputs["chapter"].chapter_hash,
                "snapshot_hash": snapshot.snapshot_hash,
                "chunk_plan_hash": chunk_plan.plan_hash,
                "config_identity": config.config_identity,
                "backend_identity_hash": backend_identity_hash,
                "cache": repair_cache.to_payload(),
            })

    # Phase 5 formatting re-run over the updated text (B3 span contract), so
    # Step 8 / the terminal transition see exactly the text that goes into
    # `complete` — the same invariant the main repair phase maintains.
    formatting_outcome = None
    if formatting_step is not None:
        formatting_outcome = formatting_step(translation=dict(final_map))
        formatted_map = dict(formatting_outcome.formatted_text)
        if set(formatted_map) != set(final_map):
            raise ValueError(
                "Quarantined-retry formatting must preserve the PID map; got "
                f"{len(formatted_map)} PIDs, expected {len(final_map)}"
            )
        for pid, text in final_map.items():
            if text and not formatted_map.get(pid):
                raise ValueError(
                    f"Quarantined-retry formatting dropped the text of PID {pid}"
                )
        final_map = formatted_map
        for incident in formatting_outcome.incidents:
            retry_debt.append(
                f"formatting:{incident.pid}:{incident.span_id}: "
                f"unresolved required span ({incident.reason}, "
                f"tier={incident.tier})"
            )
        if progress is not None:
            progress.formatting_done(
                incidents=formatting_outcome.incident_count,
                blocking=formatting_outcome.blocking,
            )

    # Debt carry-forward: a retried chunk that got selected replaced its text,
    # so its old debt (on the old best-variant) and its old formatting
    # incidents are dropped; everything else stays. Chunks still quarantined
    # keep their debt and are marked quarantined_final.
    selected_pids = frozenset(
        pid for chunk_id in selected_chunks for pid in chunk_plan.chunk(chunk_id).pids
    )
    carried_debt = [
        debt
        for debt in repair_phase_result.debt_trace
        if not any(debt_mentions_chunk(debt, chunk_id) for chunk_id in selected_chunks)
        and not any(debt_mentions_pid(debt, pid) for pid in selected_pids)
    ]
    debt = tuple(dict.fromkeys([*carried_debt, *retry_debt]))

    provenance = _build_phase4_provenance(
        source=source,
        snapshot=snapshot,
        chunk_plan=chunk_plan,
        config=config,
        chapter_hash=phase4_inputs["chapter"].chapter_hash,
    )
    reaudited_pids = (
        frozenset(
            pid
            for chunk_id in selected_chunks
            for pid in chunk_plan.chunk(chunk_id).pids
        )
        if selected_chunks
        else frozenset()
    )
    integrity = run_integrity_check(
        source=source,
        snapshot=snapshot,
        chunk_plan=chunk_plan,
        config=config,
        det_data=det_data_full,
        final_translation=dict(final_map),
        original_translation=dict(repair_phase_result.final_translation),
        reaudited_pids=reaudited_pids,
    )
    terminal = decide_terminal_state(
        chunk_plan=chunk_plan,
        final_translation=dict(final_map),
        debt_reasons=debt,
        provenance=provenance,
    )
    if progress is not None:
        progress.terminal(status=terminal.status)

    # Re-write the repair report with the retry outcome (schema additions:
    # quarantined_final / retry_attempts / quarantined_retry block).
    report = dict(repair_phase_result.to_payload())
    report["status"] = terminal.status
    report["rounds"] = report.get("rounds", []) + (
        [retry_round_payload] if retry_round_payload is not None else []
    )
    report["debt_trace"] = list(debt)
    report["final_translation"] = [
        [pid, final_map.get(pid, "")] for pid in snapshot.pids
    ]
    report["integrity"] = integrity
    report["terminal"] = {"state_id": terminal.state_id, "status": terminal.status}
    # The retry may re-run Phase 5 formatting over the updated text, so the
    # report's formatting block must reflect the re-run outcome, not the
    # pre-retry one (the formatting_report.json is re-written below too).
    if formatting_outcome is not None:
        report["formatting"] = formatting_outcome.to_payload()
    report["quarantined_final"] = result.quarantined_final
    report["retry_attempts"] = result.retry_attempts
    report["quarantined_retry"] = {
        "ran": True,
        "policy_version": QUARANTINED_RETRY_POLICY_VERSION,
        "retried_chunk_ids": list(result.retried_chunk_ids),
        "selected_chunk_ids": list(result.selected_chunk_ids),
        "quarantined_final_chunk_ids": list(result.quarantined_final_chunk_ids),
        "attempts": [attempt.to_payload() for attempt in result.attempts],
    }
    report["chapter_id"] = cfg.chapter_id
    report["finished_at"] = now().isoformat(timespec="seconds")
    _atomic_write_json(_repair_report_path(cfg.out_dir), report)

    if formatting_outcome is not None:
        _atomic_write_json(_formatting_report_path(cfg.out_dir), {
            "schema": FORMATTING_REPORT_SCHEMA,
            "chapter_id": cfg.chapter_id,
            "source_hash": source.source_hash,
            "snapshot_hash": snapshot.snapshot_hash,
            "chunk_plan_hash": chunk_plan.plan_hash,
            "config_identity": config.config_identity,
            "backend_identity_hash": backend_identity_hash,
            "outcome": formatting_outcome.to_payload(),
        })

    merged_generation_records = merge_retry_generation_records(
        existing_generation_records,
        result.generation_records,
    )

    summary: Dict[str, Any] = {
        "status": "ran",
        "policy_version": QUARANTINED_RETRY_POLICY_VERSION,
        "retried_chunk_ids": list(result.retried_chunk_ids),
        "attempts": [attempt.to_payload() for attempt in result.attempts],
        "retry_attempts": result.retry_attempts,
        "quarantined_final": result.quarantined_final,
        "selected_chunk_ids": list(result.selected_chunk_ids),
        "quarantined_final_chunk_ids": list(result.quarantined_final_chunk_ids),
        "terminal": terminal.status,
        # The full integrity dict, matching step7["integrity"]'s shape (the
        # driver copies it into step8 verbatim).
        "integrity": integrity,
        "report_path": str(_repair_report_path(cfg.out_dir)),
        "quarantined_retry_path": str(_quarantined_retry_path(cfg.out_dir)),
        "generation_records": merged_generation_records,
    }
    if formatting_outcome is not None:
        summary["formatting"] = {
            "status": "blocking" if formatting_outcome.blocking else "ok",
            "resolved_count": formatting_outcome.resolved_count,
            "incident_count": formatting_outcome.incident_count,
            "model_fallback_count": formatting_outcome.model_fallback_count,
            "max_formatting_incidents": formatting_outcome.max_formatting_incidents,
            "report_path": str(_formatting_report_path(cfg.out_dir)),
        }
    return summary


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run_chapter_strict(
    cfg: StrictRunConfig,
    *,
    router: Optional[ModelRouter] = None,
    runtime: Optional[RuntimeCoordinator] = None,
    model_caller: Any,
    qwen_evaluator: Any,
    gemma_selector: Any,
    qwen_audit_evaluator: Any,
    gemma_audit_evaluator: Any,
    repair_adapters: Optional[Sequence[Any]] = None,
    formatting_adapters: Optional[Sequence[Any]] = None,
    now: Optional[Any] = None,
    progress: Optional[Any] = None,
) -> StrictChapterRunResult:
    """Run the strict single-resident driver for one chapter.

    ``model_caller``/``qwen_evaluator``/``gemma_selector``/
    ``qwen_audit_evaluator``/``gemma_audit_evaluator`` are injected, exactly
    like ``run_chapter``'s -- this function has no opinion about whether they
    are the real ``Lifecycle*`` wrappers over a live ``llama-server`` (see
    ``build_strict_lifecycle``) or backend-role adapters over an OpenCode
    server, or test stubs over a fake in-memory router.

    Backend lifecycle is observed through a ``RuntimeCoordinator`` (plan
    §9.1). ``runtime`` may be supplied directly (remote/composite runs); if
    omitted, a ``router`` is required and wrapped in a
    ``LocalLifecycleCoordinator`` (the historical local-only call shape).
    ``cfg.backend`` is required in both cases: it is the identity recorded in
    provenance/journal and validated on resume.

    After Phase 1-2 completes, Step 6 runs the assembled-chapter audit
    (``pact_v4.phase3.audit.run_chapter_audit``) over **every chunk** (owner
    decision 2026-08-02): selected chunks use their committed winner,
    quarantined chunks use the deterministic best-variant among their produced
    variants (diagnostic only — the chunk stays quarantined), and chunks with
    no candidate at all are covered by the deterministic ``missing`` layer
    without any model unit. Its ``AuditCache``/findings and the B2 input
    contract ``b2_handoff.json`` are persisted as dedicated run artifacts and
    restored on resume (only unfinished ``(chunk_id, detector)`` units are
    re-attempted; a chapter that grew across resume sessions simply starts a
    fresh audit cache, since every old unit key misses).

    Step 7/8 (Phase 4, B2) run after Step 6 only when ``repair_adapters``
    (``(repair_caller, region_fidelity_gate, qwen_audit_evaluator,
    gemma_audit_evaluator)``, built by
    ``pact_v4.runtime.runtime_config.build_repair_adapters`` over the
    coordinator ``CompletionBackend``) is provided — local/remote/composite
    runs wire the same Backend adapters. Without it the repair phase is
    recorded as ``skipped`` (e.g. test stubs that only cover Phase 1-2 + Step
    6).

    Phase 5 formatting (B3) runs between Step 7 convergence and Step 8 when
    ``formatting_adapters`` (``(formatting_caller,)``, built by
    ``pact_v4.runtime.runtime_config.build_formatting_adapters``) is also
    provided. Its model-fallback tier goes through ``BackendFormattingCaller``
    over the coordinator ``CompletionBackend`` — never a local lifecycle
    adapter. The formatted text is what the Step 8 integrity check and the
    terminal transition see.
    """
    if runtime is None:
        if router is None:
            raise ValueError(
                "run_chapter_strict: either router or runtime must be provided"
            )
        runtime = LocalLifecycleCoordinator(
            router, descriptor=cfg.backend.build_descriptor()
        )
    now_fn = now or (lambda: datetime.now(timezone.utc))
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    started_at = now_fn().isoformat(timespec="seconds")
    wall_t0 = time.monotonic()
    progress_writer = progress or PhaseProgressWriter(cfg.out_dir, now=now_fn)

    # ------------------------------------------------------------------
    # Rebuild source/snapshot/plan -- identical to run_chapter/run_generate.
    # ------------------------------------------------------------------
    blocks, _raw_sha = load_source(cfg.chapter_html_path)
    if not blocks:
        raise ValueError(f"Chapter {cfg.chapter_id}: no source blocks parsed")
    source = build_source_artifact(chapter_id=cfg.chapter_id, blocks=blocks)
    memory = ChapterMemory.from_directory(cfg.memory_dir)
    snapshot = build_snapshot(
        chapter_id=cfg.chapter_id, source=source, memory=memory,
        context=f"chapter_html={cfg.chapter_html_path};memory_dir={cfg.memory_dir}",
    )
    config = cfg.to_config_artifact(model_profile=cfg.backend.config_profile_name())
    planner = ChunkPlanner(
        target_words=cfg.target_chunk_words, min_words=cfg.min_chunk_words,
        max_words=cfg.max_chunk_words,
    )
    plans = planner.plan(blocks, snapshot_hash=snapshot.snapshot_hash,
                          following_blocks=cfg.right_context_pids)
    if not plans:
        raise ValueError(f"Chapter {cfg.chapter_id}: planner returned no chunks")
    chunk_plan = ChunkPlanArtifact.create(snapshot, tuple(plans))
    chunk_plan_path = cfg.out_dir / "chunk_plan.json"
    chunk_plan_path.write_text(
        json.dumps(chunk_plan.to_payload(), ensure_ascii=False, indent=2), encoding="utf-8",
    )

    glossary = _glossary_entries(memory)
    source_map = dict(source.source)
    risk_by_chunk = {
        pc.chunk_id: _risk_for_chunk(chunk=pc, source_map=source_map, glossary=glossary)
        for pc in chunk_plan.chunks
    }

    # ------------------------------------------------------------------
    # Resume: replay journal, verify identities, reconstruct state.
    # ------------------------------------------------------------------
    journal_path = cfg.out_dir / "journal.ndjson"
    translations_path = cfg.out_dir / "translations.json"
    prior_entries = _load_journal(journal_path)
    selected_text_by_chunk: Dict[str, Dict[str, str]] = {}
    final_text_by_pid: Dict[str, str] = {}
    selected_role_counts: Dict[str, int] = {}
    quarantined_count = 0
    needs_synthesis_count = 0
    incomplete_generation_count = 0
    generation_records: List[Dict[str, Any]] = []
    selection_records: List[Dict[str, Any]] = []

    acceptable_backend_hashes = list(cfg.backend.acceptable_identity_hashes())
    for entry in prior_entries:
        if entry.get("snapshot_hash") != snapshot.snapshot_hash or \
                entry.get("chunk_plan_hash") != chunk_plan.plan_hash or \
                entry.get("config_identity") != config.config_identity or \
                entry.get("backend_identity_hash") not in acceptable_backend_hashes:
            raise ValueError(
                "Foreign identity: journal entry for "
                f"{entry.get('chunk_id')} was written under a different "
                "snapshot/plan/config than this run -- refusing to resume "
                "against a stale journal."
            )
    resumed_from_index = len(prior_entries)
    if prior_entries:
        LOG.info("Resuming %s from chunk index %d (%d chunks already journaled)",
                  cfg.chapter_id, resumed_from_index, resumed_from_index)

    progress_writer.run_started(
        chapter_id=cfg.chapter_id,
        out_dir=cfg.out_dir,
        started_at=started_at,
        backend_identity_hash=cfg.backend.identity_hash,
        resumed_from_index=resumed_from_index,
    )

    # A resumed run's committed translations live in the *previous* run's
    # translations.json (this run overwrites that path only at the very
    # end). Load it before the loop so selected_text_by_chunk can be
    # reconstructed from chunk_plan.pids -> text, not re-derived from the
    # journal (which deliberately does not store translation text).
    prior_translations: Dict[str, str] = {}
    if prior_entries and translations_path_exists(cfg.out_dir):
        prior_translations = json.loads(
            (cfg.out_dir / "translations.json").read_text(encoding="utf-8")
        )
    final_text_by_pid.update(prior_translations)

    for entry in prior_entries:
        outcome = entry["outcome"]
        selection_records.append({
            "chunk_id": entry["chunk_id"], "status": outcome,
            "selected_candidate_id": entry.get("selected_candidate_id"),
            "selected_role": entry.get("selected_role"),
            # gate_trace is carried from the journal so Step 6's b2_handoff
            # still shows a resumed chunk's cascade trace; quarantine_reason
            # is not persisted in the journal (v1), so it stays absent here
            # and the handoff records it as None for resumed chunks.
            "gate_trace": list(entry.get("gate_trace") or []),
            "resumed": True,
        })
        if outcome == "selected":
            selected_role_counts[entry["selected_role"]] = (
                selected_role_counts.get(entry["selected_role"], 0) + 1
            )
            plan_chunk = chunk_plan.chunk(entry["chunk_id"])
            selected_text_by_chunk[entry["chunk_id"]] = {
                pid: prior_translations[pid] for pid in plan_chunk.pids
                if pid in prior_translations
            }
        elif outcome == "quarantined":
            quarantined_count += 1
        elif outcome == "needs_synthesis":
            needs_synthesis_count += 1
        elif outcome == "incomplete_generation":
            incomplete_generation_count += 1

    generation_params = GenerationParams(
        temperature=cfg.temperature, seed=cfg.seed, max_tokens=cfg.max_tokens,
    )
    gen_cache = GenerationCache()
    # B5 mixed_script-политика (V4_B5_MIXED_SCRIPT_POLICY_TASK_RU.md):
    # combined allowlist = book_memory + glossary + source-derived + manual
    # config. The static part (book_memory/glossary/manual) is derived once;
    # the source-derived part is per-text (tokens present in BOTH the source
    # and the translation under check), so per-chunk and per-phase det_data
    # are built from this base below. The bible source is the v4
    # ``book_memory`` (per V4_MVP_SPEC_RU.md §6 characters/facts/address
    # register/voice notes; the task card's "book_bible.json" was a naming
    # error — a real v4 book_bible is a B7 concern). Manual config entries are
    # tokenized the same way as book_memory/glossary entries, so an entry like
    # "R.D.T." unblocks the tokens R/D/T that ``find_mixed_script`` sees.
    static_allow = combine_script_tokens(
        bible_script_tokens(memory.book_memory),
        glossary_script_tokens(memory.glossary),
        extract_script_tokens(" ".join(cfg.deterministic_mixed_script_allow)),
    )
    det_data_base = DeterministicGateData(
        glossary_terms=cfg.deterministic_glossary_terms,
        names=cfg.deterministic_names,
        mixed_script_allow=static_allow,
    )

    halted_early = False
    halt_reason: Optional[str] = None
    consecutive_nonselections = 0
    # Itemized per-chunk reasons for the current non-selection streak, so
    # halt_reason names what actually went wrong (e.g. specific Qwen/
    # deterministic failures) instead of only a generic chunk count --
    # reviewers shouldn't have to open selection_results.json separately
    # to see why an operational-policy halt fired.
    recent_nonselection_reasons: List[str] = []

    try:
        with open(journal_path, "a", encoding="utf-8") as journal_file:
            for index, plan_chunk in enumerate(chunk_plan.chunks):
                # selected_text_by_chunk / final_text_by_pid for already-
                # journaled chunks were reconstructed above, from the
                # prior run's translations.json -- nothing to redo here.
                if index < resumed_from_index:
                    continue

                risk = risk_by_chunk[plan_chunk.chunk_id]
                left_context = _left_ru_for_chunk(
                    chunk_index=index, chunk_plan=chunk_plan,
                    selected_text_by_chunk=selected_text_by_chunk,
                )
                right_context = tuple(
                    (pid, source_map[pid]) for pid in plan_chunk.context.right_en
                    if pid in source_map
                )
                left_context_kind = (
                    "none_first_chunk" if index == 0 else
                    ("selected" if left_context else "empty_after_nonselection")
                )
                parent_chunk_id = chunk_plan.chunks[index - 1].chunk_id if index > 0 else None
                parent_context_state_hash = _left_context_hash(left_context)

                events_before = runtime.event_count()

                progress_writer.chunk_started(chunk_id=plan_chunk.chunk_id)

                outcome = generate_for_chunk(
                    chunk_id=plan_chunk.chunk_id, risk=risk, source=source, snapshot=snapshot,
                    chunk_plan=chunk_plan, left_context=left_context, right_context=right_context,
                    glossary=glossary, style_constraints={}, config=config, params=generation_params,
                    model_caller=model_caller, cache=gen_cache,
                )
                generation_records.append(_serialize_generation_outcome(outcome))

                if outcome.status != "complete":
                    incomplete_generation_count += 1
                    consecutive_nonselections += 1
                    recent_nonselection_reasons.append(
                        f"{plan_chunk.chunk_id}: incomplete_generation "
                        f"({', '.join(f'{r}={e.detail}' for r, e in outcome.errors.items())})"
                    )
                    entry = JournalEntry(
                        chunk_index=index, chunk_id=plan_chunk.chunk_id,
                        parent_chunk_id=parent_chunk_id,
                        parent_context_state_hash=parent_context_state_hash,
                        left_context_kind=left_context_kind,
                        left_context_hash=_left_context_hash(left_context),
                        snapshot_hash=snapshot.snapshot_hash, chunk_plan_hash=chunk_plan.plan_hash,
                        config_identity=config.config_identity,
                        backend_identity_hash=cfg.backend.identity_hash,
                        candidate_ids=list(outcome.candidates.keys()),
                        gate_trace=[], outcome="incomplete_generation",
                        selected_candidate_id=None, selected_role=None,
                        switch_indices=runtime.local_switch_event_indices(events_before),
                        backend_event_indices=list(range(events_before, runtime.event_count())),
                    )
                    journal_file.write(json.dumps(entry.to_json(), ensure_ascii=False) + "\n")
                    journal_file.flush()
                    progress_writer.chunk_done(
                        chunk_id=plan_chunk.chunk_id, outcome="incomplete_generation"
                    )
                    selection_records.append({
                        "chunk_id": plan_chunk.chunk_id, "status": "incomplete_generation",
                        "risk_band": outcome.risk_band, "expected_roles": list(outcome.expected_roles),
                        "candidates_produced": list(outcome.candidates),
                        "errors": {r: {"code": e.code.value, "detail": e.detail}
                                   for r, e in outcome.errors.items()},
                    })
                    if consecutive_nonselections >= cfg.max_consecutive_terminal_nonselections:
                        halted_early = True
                        halt_reason = (
                            f"{consecutive_nonselections} consecutive non-selected chunks "
                            f"(>= max_consecutive_terminal_nonselections="
                            f"{cfg.max_consecutive_terminal_nonselections}); halting per pinned "
                            "operational policy instead of cascading empty left_context further. "
                            "Reasons: " + " | ".join(recent_nonselection_reasons)
                        )
                        break
                    continue

                candidates: List[Candidate] = list(outcome.candidates.values())
                # B5: chunk-scoped source-derived allowlist. A Latin token in
                # the candidate translation that also appears in this chunk's
                # source is legitimate (e.g. source initials "R.D.T." preserved
                # in the translation); the union over the chunk's candidates
                # gives exactly "in source AND in translation" for every
                # checked candidate without loosening the gate for tokens that
                # never appear in the source.
                chunk_source_text = " ".join(
                    source_map[pid] for pid in plan_chunk.pids if pid in source_map
                )
                candidate_union_text = " ".join(
                    text for cand in candidates for _, text in cand.translation
                )
                det_data_chunk = replace(
                    det_data_base,
                    mixed_script_allow=combine_script_tokens(
                        static_allow,
                        source_derived_allowlist(chunk_source_text, candidate_union_text),
                    ),
                )
                try:
                    result: SelectionResult = select_candidate(
                        chunk_id=plan_chunk.chunk_id, candidates=candidates, source=source,
                        qwen_evaluator=qwen_evaluator, det_data=det_data_chunk,
                        gemma_selector=gemma_selector,
                    )
                except Exception as exc:  # noqa: BLE001 -- see run_chapter's identical handling
                    LOG.exception("select_candidate raised for %s", plan_chunk.chunk_id)
                    quarantined_count += 1
                    consecutive_nonselections += 1
                    recent_nonselection_reasons.append(
                        f"{plan_chunk.chunk_id}: cascade raised {exc!r}"
                    )
                    entry = JournalEntry(
                        chunk_index=index, chunk_id=plan_chunk.chunk_id,
                        parent_chunk_id=parent_chunk_id,
                        parent_context_state_hash=parent_context_state_hash,
                        left_context_kind=left_context_kind,
                        left_context_hash=_left_context_hash(left_context),
                        snapshot_hash=snapshot.snapshot_hash, chunk_plan_hash=chunk_plan.plan_hash,
                        config_identity=config.config_identity,
                        backend_identity_hash=cfg.backend.identity_hash,
                        candidate_ids=[c.candidate_id for c in candidates],
                        gate_trace=[], outcome="quarantined",
                        selected_candidate_id=None, selected_role=None,
                        switch_indices=runtime.local_switch_event_indices(events_before),
                        backend_event_indices=list(range(events_before, runtime.event_count())),
                    )
                    journal_file.write(json.dumps(entry.to_json(), ensure_ascii=False) + "\n")
                    journal_file.flush()
                    progress_writer.chunk_done(
                        chunk_id=plan_chunk.chunk_id, outcome="quarantined"
                    )
                    selection_records.append({
                        "chunk_id": plan_chunk.chunk_id, "status": "quarantined",
                        "quarantine_reason": f"cascade raised: {exc!r}",
                        "risk_band": outcome.risk_band,
                        "candidates_produced": [c.role for c in candidates],
                    })
                    if consecutive_nonselections >= cfg.max_consecutive_terminal_nonselections:
                        halted_early = True
                        halt_reason = (
                            f"{consecutive_nonselections} consecutive non-selected chunks; halting "
                            "per pinned operational policy. Reasons: "
                            + " | ".join(recent_nonselection_reasons)
                        )
                        break
                    continue

                q_delta, n_delta, selected_text = _record_selection(
                    selection_records=selection_records, final_text_by_pid=final_text_by_pid,
                    selected_role_counts=selected_role_counts, result=result, outcome=outcome,
                )
                quarantined_count += q_delta
                needs_synthesis_count += n_delta

                gate_trace = [
                    {"gate": g.gate, "passed": g.passed, "detail": g.detail}
                    for g in result.decision_trace
                ]
                if selected_text is not None:
                    selected_text_by_chunk[plan_chunk.chunk_id] = selected_text
                    consecutive_nonselections = 0
                    recent_nonselection_reasons.clear()
                    entry_outcome = "selected"
                else:
                    consecutive_nonselections += 1
                    entry_outcome = "needs_synthesis" if result.needs_synthesis else "quarantined"
                    reason_text = result.synthesis_reason if result.needs_synthesis else result.quarantine_reason
                    recent_nonselection_reasons.append(f"{plan_chunk.chunk_id}: {entry_outcome} ({reason_text})")

                entry = JournalEntry(
                    chunk_index=index, chunk_id=plan_chunk.chunk_id,
                    parent_chunk_id=parent_chunk_id,
                    parent_context_state_hash=parent_context_state_hash,
                    left_context_kind=left_context_kind,
                    left_context_hash=_left_context_hash(left_context),
                    snapshot_hash=snapshot.snapshot_hash, chunk_plan_hash=chunk_plan.plan_hash,
                    config_identity=config.config_identity,
                    backend_identity_hash=cfg.backend.identity_hash,
                    candidate_ids=[c.candidate_id for c in candidates],
                    gate_trace=gate_trace, outcome=entry_outcome,
                    selected_candidate_id=result.selected_candidate_id,
                    selected_role=result.selected_role if entry_outcome == "selected" else None,
                    switch_indices=runtime.local_switch_event_indices(events_before),
                    backend_event_indices=list(range(events_before, runtime.event_count())),
                )
                journal_file.write(json.dumps(entry.to_json(), ensure_ascii=False) + "\n")
                journal_file.flush()
                progress_writer.chunk_done(chunk_id=plan_chunk.chunk_id, outcome=entry_outcome)
                if entry_outcome == "selected":
                    # Written immediately, not just at the end of the run:
                    # if the process crashes before reaching the final
                    # write below, a later resume reads this file back to
                    # reconstruct selected_text_by_chunk. Without this,
                    # a crash after this journal entry is flushed but
                    # before the run's final translations.json write
                    # would leave the journal saying "selected" for a
                    # chunk whose text resume can no longer find --
                    # producing a wrongly-empty left_context for the next
                    # chunk on resume instead of the real committed text.
                    _atomic_write_json(translations_path, final_text_by_pid)

                if consecutive_nonselections >= cfg.max_consecutive_terminal_nonselections:
                    halted_early = True
                    halt_reason = (
                        f"{consecutive_nonselections} consecutive non-selected chunks; halting "
                        "per pinned operational policy instead of cascading empty left_context "
                        "further. Reasons: " + " | ".join(recent_nonselection_reasons)
                    )
                    break
    finally:
        # Non-terminal release: frees the resident model for a local
        # single-resident run (re-acquirable by Step 6) and is a no-op for
        # a remote backend, which must stay open until the very end.
        try:
            runtime.release()
        except Exception:  # noqa: BLE001
            LOG.exception("Failed to release runtime at end of Phase 1-2")

    # ------------------------------------------------------------------
    # Step 6: assembled-chapter audit (Phase 3B, DECISIONS 2026-08-01,
    # owner decision 2026-08-02: audit ALL chunks — best-variant for
    # quarantine). Runs after the Phase 1-2 loop, so the audit's
    # lifecycle-aware evaluators re-acquire models as needed; the
    # detector-outer loop inside run_chapter_audit batches all Qwen units
    # then all Gemma units, giving ~1-2 switches for the whole phase.
    # Audit failures never abort the completed Phase 1-2 run -- they are
    # recorded in the run record (and, for model/parse failures, as
    # incomplete units).
    # ------------------------------------------------------------------
    # Step 6 needs the generation records of *every* chunk — including
    # quarantined chunks from earlier sessions — to pick best-variants, so
    # merge the persisted generation_outcomes.json with this session's
    # records. A foreign-identity file raises like the journal check: mixing
    # generation data across runs would let a stale variant enter the audit.
    merged_generation_records = _merge_generation_outcomes(
        cfg.out_dir, generation_records,
        snapshot=snapshot, chunk_plan=chunk_plan, config=config,
    )
    # The journal (v1) does not persist quarantine_reason, so merge the
    # cumulative selection_meta.json sidecar to restore it (and the rest of
    # the per-chunk selection detail) for chunks journaled in earlier
    # sessions — without it the handoff would silently lose *why* a resumed
    # chunk was quarantined (review PR #108, issue 2).
    merged_selection_records = _merge_selection_meta(
        cfg.out_dir, selection_records,
        snapshot=snapshot, chunk_plan=chunk_plan, config=config,
    )
    # B5: whole-chapter source-derived allowlist for the Step 6 audit / Step 7
    # repair. The assembled chapter is built from the committed selections and
    # the quarantined chunks' best-variants, which are all carried by the
    # merged generation records (and, for a resumed session, the committed
    # translations). Unioning all candidate translations gives exactly "in
    # source AND in translation" for every token the audit/repair actually
    # checks, without loosening the gate for tokens that never appear in the
    # source.
    det_data_full = replace(
        det_data_base,
        mixed_script_allow=combine_script_tokens(
            static_allow,
            source_derived_allowlist(
                " ".join(text for _, text in source.source),
                " ".join(
                    text
                    for rec in merged_generation_records
                    for variant in rec.get("candidates", {}).values()
                    for _pid, text in variant.get("translation", {}).items()
                )
                + " " + " ".join(final_text_by_pid.values()),
            ),
        ),
    )
    events_before_step6 = runtime.event_count()
    step6: Dict[str, Any]
    phase4_inputs: Optional[Dict[str, Any]] = None
    try:
        step6, phase4_inputs = _run_step6_audit(
            cfg=cfg, source=source, snapshot=snapshot, chunk_plan=chunk_plan,
            config=config, det_data=det_data_full, selection_records=merged_selection_records,
            selected_text_by_chunk=selected_text_by_chunk,
            generation_records=merged_generation_records,
            qwen_audit_evaluator=qwen_audit_evaluator,
            gemma_audit_evaluator=gemma_audit_evaluator,
            backend_identity_hash=cfg.backend.identity_hash,
            backend_identity_hashes=acceptable_backend_hashes,
            progress=progress_writer,
        )
    except Exception as exc:  # noqa: BLE001 -- a Step 6 failure is a record, not a crash
        LOG.exception("Step 6 audit failed for %s", cfg.chapter_id)
        step6 = {"status": "failed", "error": str(exc)}
        phase4_inputs = None
    finally:
        try:
            runtime.release()
        except Exception:  # noqa: BLE001
            LOG.exception("Failed to release runtime after Step 6 audit")

    # ------------------------------------------------------------------
    # Step 7/8: Phase 4 repair + convergence + terminal (B2). Runs only
    # when repair adapters are configured (the CLI always wires them via
    # ``build_repair_adapters``; test stubs may omit them). Repair model
    # calls go through the same Backend boundary as Step 6 (never local
    # lifecycle adapters). A transport failure at a repair call is recorded
    # as debt/incomplete, never a semantic terminal status.
    # ------------------------------------------------------------------
    events_before_step7 = runtime.event_count()
    step7: Dict[str, Any]
    step8: Dict[str, Any]
    if repair_adapters is not None and phase4_inputs is not None:
        # Phase 5 formatting (B3): build the formatting step over the source
        # blocks + the injected formatting caller (a Backend adapter over the
        # coordinator CompletionBackend), so the model-fallback tier runs
        # through the backend boundary in local/remote/composite profiles
        # alike — never a local lifecycle adapter. Applied between Step 7
        # convergence and Step 8 inside run_repair_phase.
        #
        # ``cfg.formatting_required`` is the runtime master switch (§6.1
        # ``formatting.required=true``): even when formatting adapters are
        # configured, the step is skipped entirely when the policy says
        # formatting is not required — adapters alone never trigger it.
        formatting_step = None
        if formatting_adapters is not None and cfg.formatting_required:
            formatting_caller = formatting_adapters[0]

            def _formatting_step(*, translation):
                return run_formatting_align(
                    blocks=blocks,
                    translation=translation,
                    formatting_caller=formatting_caller,
                    backend_identity_hash=cfg.backend.identity_hash,
                    policy_version=cfg.formatting_policy_version,
                    max_formatting_incidents=cfg.max_formatting_incidents,
                )

            formatting_step = _formatting_step
        try:
            step7, repair_phase_result = _run_step7_repair(
                cfg=cfg, source=source, snapshot=snapshot, chunk_plan=chunk_plan,
                config=config, det_data=det_data_full, phase4_inputs=phase4_inputs,
                repair_adapters=repair_adapters,
                backend_identity_hash=cfg.backend.identity_hash,
                backend_identity_hashes=acceptable_backend_hashes,
                now=now_fn,
                formatting=formatting_step,
                progress=progress_writer,
            )
            step8 = {
                "status": step7["terminal"],
                "integrity": step7["integrity"],
                "debt_trace": None,  # recorded in repair_report.json
                "formatting": step7.get("formatting"),
            }
            # B6: separate bounded quarantined-retry cycle. A quarantined
            # chunk with repair debt is regenerated with look-ahead context and
            # re-cascaded; a winner replaces its best-variant, a still-failed
            # chunk is accepted as final (quarantined_final). A failure here is
            # recorded in step7, never a crash of the completed run.
            try:
                retry_summary = _run_quarantined_retry_cycle(
                    cfg=cfg,
                    source=source,
                    snapshot=snapshot,
                    chunk_plan=chunk_plan,
                    config=config,
                    det_data_base=det_data_base,
                    det_data_full=det_data_full,
                    risk_by_chunk=risk_by_chunk,
                    glossary=glossary,
                    generation_params=generation_params,
                    model_caller=model_caller,
                    gen_cache=gen_cache,
                    qwen_evaluator=qwen_evaluator,
                    gemma_selector=gemma_selector,
                    selected_text_by_chunk=selected_text_by_chunk,
                    phase4_inputs=phase4_inputs,
                    repair_phase_result=repair_phase_result,
                    repair_adapters=repair_adapters,
                    formatting_step=formatting_step,
                    backend_identity_hash=cfg.backend.identity_hash,
                    acceptable_backend_hashes=acceptable_backend_hashes,
                    now=now_fn,
                    progress=progress_writer,
                    existing_generation_records=merged_generation_records,
                )
            except Exception as exc:  # noqa: BLE001 -- a retry failure is a record, not a crash
                LOG.exception("Quarantined retry cycle failed for %s", cfg.chapter_id)
                step7 = dict(step7)
                step7["quarantined_retry"] = {"status": "failed", "error": str(exc)}
            else:
                if retry_summary is not None:
                    merged_generation_records = retry_summary["generation_records"]
                    retry_block = {
                        key: value
                        for key, value in retry_summary.items()
                        if key != "generation_records"
                    }
                    step7 = {**step7, "quarantined_retry": retry_block}
                    step8 = {
                        "status": retry_summary["terminal"],
                        "integrity": retry_summary["integrity"],
                        "debt_trace": None,  # recorded in repair_report.json
                        "formatting": (
                            retry_summary.get("formatting")
                            if "formatting" in retry_summary
                            else step7.get("formatting")
                        ),
                    }
        except Exception as exc:  # noqa: BLE001 -- a repair failure is a record, not a crash
            LOG.exception("Phase 4 repair failed for %s", cfg.chapter_id)
            step7 = {"status": "failed", "error": str(exc)}
            step8 = {"status": "failed", "error": str(exc)}
        finally:
            try:
                runtime.release()
            except Exception:  # noqa: BLE001
                LOG.exception("Failed to release runtime after Step 7 repair")
    else:
        reason = "repair_adapters_not_configured" if repair_adapters is None else "no_step6_phase4_inputs"
        step7 = {"status": "skipped", "reason": reason}
        step8 = {"status": "skipped", "reason": reason}

    step7_events = [
        event for event in runtime.events_since(events_before_step7)
        if event.kind == EVENT_KIND_LOCAL_SWITCH
    ]
    step7 = dict(step7)
    step7["switch_count"] = len(step7_events)
    step7["switches"] = [event.to_payload() for event in step7_events]

    # The audit phase's own lifecycle cost, recorded for run_003-style
    # validation: batching by detector should keep this at ~1-2 switches
    # (one Qwen acquire + one Qwen->Gemma switch) regardless of chunk count.
    # For remote/composite runs the Step 6 "switches" are the local switch
    # events only (remote call events are aggregated in the runtime block).
    step6_events = [
        event for event in runtime.events_since(events_before_step6)
        if event.kind == EVENT_KIND_LOCAL_SWITCH
    ]
    step6 = dict(step6)
    step6["switch_count"] = len(step6_events)
    step6["switches"] = [event.to_payload() for event in step6_events]

    wall_clock_seconds = time.monotonic() - wall_t0
    processed_count = len(_load_journal(journal_path))

    generation_path = _generation_outcomes_path(cfg.out_dir)
    generation_path.write_text(json.dumps({
        "chapter_id": cfg.chapter_id, "snapshot_hash": snapshot.snapshot_hash,
        "chunk_plan_hash": chunk_plan.plan_hash, "config_identity": config.config_identity,
        # Persist the merged list so a later resume can recover variants for
        # quarantined chunks of this session too (cumulative across resumes).
        "outcomes": merged_generation_records,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # Final write is redundant with the incremental one after each
    # selected chunk (below) but kept for a clean end-state file.
    _atomic_write_json(translations_path, final_text_by_pid)
    selection_path = cfg.out_dir / "selection_results.json"
    selection_path.write_text(json.dumps({
        "chapter_id": cfg.chapter_id, "snapshot_hash": snapshot.snapshot_hash,
        "chunk_plan_hash": chunk_plan.plan_hash, "config_identity": config.config_identity,
        # Write the merged list so a resumed run's selection_results.json is
        # as rich as a fresh run's (quarantine_reason, decision_trace, ...).
        "results": merged_selection_records,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    # Cumulative per-chunk selection sidecar (review PR #108, issue 2): the
    # journal (v1) does not persist quarantine_reason, so without this a later
    # resume loses *why* a chunk was quarantined. Written after selection and
    # Step 6, from the same merged list used above.
    _atomic_write_json(_selection_meta_path(cfg.out_dir), {
        "schema": SELECTION_META_SCHEMA,
        "chapter_id": cfg.chapter_id, "snapshot_hash": snapshot.snapshot_hash,
        "chunk_plan_hash": chunk_plan.plan_hash, "config_identity": config.config_identity,
        "records": merged_selection_records,
    })

    finished_at = now_fn().isoformat(timespec="seconds")
    runtime_summary = dict(runtime.summary())
    local_lifecycle = runtime_summary.get("local_lifecycle")
    remote_calls = runtime_summary.get("remote_calls")
    backend_block = dict(runtime.backend_descriptor.public_record())
    backend_block["config_identity_hash"] = cfg.backend.identity_hash
    record: Dict[str, Any] = {
        "schema": RECORD_SCHEMA,
        "run_label": cfg.run_label,
        "chapter_id": cfg.chapter_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "wall_clock_seconds": wall_clock_seconds,
        "identities": {
            "source_hash": source.source_hash, "snapshot_hash": snapshot.snapshot_hash,
            "chunk_plan_hash": chunk_plan.plan_hash, "config_identity": config.config_identity,
        },
        "backend": backend_block,
        "runtime": {
            "local_lifecycle": local_lifecycle,
            "remote_calls": remote_calls,
        },
        "operational_policy": {
            "max_consecutive_terminal_nonselections": cfg.max_consecutive_terminal_nonselections,
        },
        "mixed_script_policy": {
            "sources": {
                "book_memory": list(bible_script_tokens(memory.book_memory)),
                "glossary": list(glossary_script_tokens(memory.glossary)),
                "manual": list(cfg.deterministic_mixed_script_allow),
                "source_derived_chapter": list(
                    source_derived_allowlist(
                        " ".join(text for _, text in source.source),
                        " ".join(
                            text
                            for rec in merged_generation_records
                            for variant in rec.get("candidates", {}).values()
                            for _pid, text in variant.get("translation", {}).items()
                        ),
                    )
                ),
            },
            "combined_static_allow": list(static_allow),
        },
        "resumed_from_index": resumed_from_index,
        "halted_early": halted_early,
        "halt_reason": halt_reason,
        "counts": {
            "chunks_total": len(chunk_plan.chunks),
            "chunks_processed": processed_count,
            "selected": sum(selected_role_counts.values()),
            "quarantined": quarantined_count,
            "needs_synthesis": needs_synthesis_count,
            "incomplete_generation": incomplete_generation_count,
            "selected_role_counts": dict(selected_role_counts),
        },
        "step6": step6,
        "step7": step7,
        "step8": step8,
        # B6: mirror of step7.quarantined_retry for top-level readers (absent
        # when the cycle did not run or repair was skipped).
        "quarantined_retry": step7.get("quarantined_retry"),
        "lifecycle": local_lifecycle or {
            "startup_count": 0, "restart_count": 0,
            "switches": [], "aggregates_by_model": {},
        },
        "artefacts": {
            "chunk_plan": str(chunk_plan_path), "generation_outcomes": str(generation_path),
            "selection_results": str(selection_path), "translations": str(translations_path),
            "journal": str(journal_path),
            "audit_cache": str(_audit_cache_path(cfg.out_dir)),
            "audit_findings": str(_audit_findings_path(cfg.out_dir)),
            "b2_handoff": str(_b2_handoff_path(cfg.out_dir)),
            "selection_meta": str(_selection_meta_path(cfg.out_dir)),
            "repair_cache": str(_repair_cache_path(cfg.out_dir)),
            "repair_report": str(_repair_report_path(cfg.out_dir)),
            "formatting_report": str(_formatting_report_path(cfg.out_dir)),
            "quarantined_retry": str(_quarantined_retry_path(cfg.out_dir)),
        },
    }
    record_path = cfg.out_dir / "strict_chapter_trial_record.json"
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    # Terminal teardown only at the very end: closes the remote backend /
    # stops a managed server the runtime started, releases the local router.
    try:
        runtime.close()
    except Exception:  # noqa: BLE001
        LOG.exception("Failed to close runtime at end of run")
    progress_writer.close()

    return StrictChapterRunResult(
        chapter_id=cfg.chapter_id, out_dir=cfg.out_dir, chunk_count=len(chunk_plan.chunks),
        processed_count=processed_count, selected_count=sum(selected_role_counts.values()),
        quarantined_count=quarantined_count, needs_synthesis_count=needs_synthesis_count,
        incomplete_generation_count=incomplete_generation_count,
        selected_role_counts=dict(selected_role_counts), halted_early=halted_early,
        halt_reason=halt_reason, resumed_from_index=resumed_from_index,
        switches=((local_lifecycle or {}).get("switches") or []),
        translations_path=translations_path, journal_path=journal_path, record_path=record_path,
        record=record, step6=step6, step7=step7, step8=step8,
    )


def translations_path_exists(out_dir: Path) -> bool:
    return (out_dir / "translations.json").exists()
