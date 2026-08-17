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
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from pact_v4.audit.chunked_audit import (
    DEFAULT_REASONING_BUDGET,
    DEFAULT_TRANSPORT_MAX_RETRIES,
    DEFAULT_TRANSPORT_BASE_DELAY_SECONDS,
    HARNESS_VERSION,
    PROMPT_VERSION,
)
from pact_v4.audit.entity_extractor import EXTRACTOR_VERSION
from pact_v4.audit.russian_editor import (
    RUSSIAN_EDITOR_HARNESS_VERSION,
    RUSSIAN_EDITOR_PROMPT_VERSION,
    SAFE_CLASSES as RUSSIAN_EDITOR_SAFE_CLASSES,
    DEFAULT_CHUNK_SIZE as RUSSIAN_EDITOR_CHUNK_SIZE,
    DEFAULT_MAX_TOKENS as RUSSIAN_EDITOR_MAX_TOKENS,
    DEFAULT_OVERLAP_PAIRS as RUSSIAN_EDITOR_OVERLAP_PAIRS,
    DEFAULT_RETRY_MAX_RETRIES as RUSSIAN_EDITOR_RETRY_MAX_RETRIES,
    DEFAULT_RETRY_BASE_DELAY_SECONDS as RUSSIAN_EDITOR_RETRY_BASE_DELAY_SECONDS,
    MAX_EDITS_PER_PID as RUSSIAN_EDITOR_MAX_EDITS_PER_PID,
)
from pact_v4.pipeline.b3_audit_repair import render_entity_context_block
from pact_v4.phase0b.source_html import SourceBlock, load_source
from pact_v4.phase1.chunker import (
    DEFAULT_MAX_WORDS,
    DEFAULT_MIN_WORDS,
    DEFAULT_TARGET_WORDS,
    ChunkPlanner,
)
from pact_v4.phase1.models import (
    CHUNK_PLAN_MODE_WHOLE_CHAPTER,
    CHUNK_PLAN_NOTE_WHOLE_CHAPTER,
    Candidate,
    ChunkPlanArtifact,
    ConfigArtifact,
    GateResult,
    Provenance,
    WholeChapterPidMap,
    canonical_json_hash,
)
from pact_v4.phase2.cascade import DeterministicGateData, SelectionResult, select_candidate
from pact_v4.phase2.generation import (
    GenerationCache,
    GenerationErrorCode,
    GenerationParams,
    WholeChapterRetryPolicy,
    _GenerationValidationError,
    _whole_chapter_risk,
    generate_for_chunk,
    generate_whole_chapter,
    validate_whole_chapter_raw,
)
from pact_v4.phase3.assembly import AssembledChapter
from pact_v4.phase3.audit import AuditCache, run_chapter_audit
from pact_v4.phase4.quarantined_retry import (
    OUTCOME_QUARANTINED_FINAL,
    OUTCOME_SELECTED,
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
    REPAIR_POLICY_VERSION,
    REPAIR_REPORT_SCHEMA,
    RepairCache,
    RepairPhaseResult,
    SoftFindingsPolicy,
    _chapter_text_identity_hash,
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
from pact_v4.repair.selective_repair import (
    DEFAULT_REAUDIT_BASE_DELAY_SECONDS,
    DEFAULT_REAUDIT_MAX_INPUT_TOKENS,
    DEFAULT_REAUDIT_MAX_OVERLAP_PAIRS,
    DEFAULT_REAUDIT_MAX_RETRIES,
    DEFAULT_REAUDIT_MAX_TOKENS,
    DEFAULT_REAUDIT_MIN_OVERLAP_PAIRS,
    DEFAULT_REAUDIT_NEIGHBOUR_WINDOW,
    DEFAULT_REAUDIT_OVERLAP_TOKENS,
    DEFAULT_REPAIR_CONTEXT_WINDOW,
    DEFAULT_REPAIR_CONTEXT_WINDOW_BY_CATEGORY,
    DEFAULT_REPAIR_MAX_TOKENS,
    DEFAULT_REPAIR_REASONING,
    MICROBATCH_TARGET,
    MICROBATCH_TRIGGER,
    REAUDIT_DELTA_FORMAT,
    REPAIR_FINDINGS_CAP,
    REPAIR_HARNESS_VERSION,
    REPAIR_PROMPT_VERSION,
)
from pact_v4.pipeline._shared_runner_helpers import (
    _glossary_entries,
    _glossary_entries_for_chunk,
    _left_ru_for_chunk,
    _narrator_glossary_terms,
    _record_selection,
    _risk_for_chunk,
    _serialize_generation_outcome,
)
from pact_v4.pipeline.phase_progress import PhaseProgressWriter
from pact_v4.pipeline.usage_record import UsageRecordWriter
from pact_v4.runtime.model_lifecycle import ModelRouter
from pact_v4.runtime.json_resilience import JsonRetryPolicy
from pact_v4.runtime.reasoning_writer import open_reasoning_writer
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
    check_narrator_gender,
    combine_script_tokens,
    extract_script_tokens,
    glossary_script_tokens,
    normalize_inline_markup,
    source_derived_allowlist,
)
from pact_v4.runtime.bible_renderer import render_bible_section, extract_narrator_gender

LOG = logging.getLogger(__name__)

JOURNAL_SCHEMA = "pact-v4-strict-chapter-trial-journal/v2"
RECORD_SCHEMA = "pact-v4-strict-chapter-trial/v2"
AUDIT_CACHE_SCHEMA = "pact-v4-strict-audit-cache/v1"
AUDIT_FINDINGS_SCHEMA = "pact-v4-strict-audit-findings/v1"
HANDOFF_SCHEMA = "pact-v4-step6-b2-handoff/v1"
SELECTION_META_SCHEMA = "pact-v4-strict-selection-meta/v1"
GLOSSARY_BUDGET_SCHEMA = "pact-v4-glossary-budget/v1"
# V4 Efficiency A1.1: per-chunk glossary budget policy. A pair enters the
# generation bundle only when its source term is present in the chunk's text
# or it is always_include (fail-closed: narrator_gender name pairs,
# glossary_conflict-carrying entries, and entries tied to the chunk's
# required risk categories number_word/tone_profanity are never cut). The
# bible is NOT filtered (owner decision, plan rev.2 §0.1).
GLOSSARY_BUDGET_POLICY_VERSION = "pact-v4-glossary-budget/v1"
# B12-F4 (RV4 HIGH): the repair-cache envelope is versioned with the repair
# policy. ``_repair_unit_hash`` embeds ``REPAIR_POLICY_VERSION`` (now v2
# after the F3 fail-closed Qwen verdict fix), so a cache file written under
# the pre-F3 contract (schema v1, unit hashes under policy v1) must never be
# reused on resume — it may hold ``committed=True`` records fixed under the
# old ``bool("false")`` truthiness.
REPAIR_CACHE_SCHEMA = "pact-v4-phase4-repair-cache/v2"
LEGACY_REPAIR_CACHE_SCHEMA = "pact-v4-phase4-repair-cache/v1"
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
    # V4.1 A1 (owner decision 2026-08-08): the generator's output budget is
    # 32768 tokens (whole-chapter output of chapter 0001 is ~12-19k tokens,
    # the longest chapter 0077 ~21k; Gate 0 §8.5). The value is part of the
    # config identity (generation.max_tokens below), so changing it
    # invalidates cache/resume exactly like any other generation setting.
    # The OpenCode transport does not send it in the POST body (Gate 0 §2.4),
    # so this is an identity/record value, not a transport constraint; the
    # Qwen-role ceiling MAX_TOKENS_CEILING=24576 is untouched.
    max_tokens: int = 32768
    deterministic_glossary_terms: Tuple[Tuple[str, str], ...] = ()
    deterministic_names: Tuple[Tuple[str, str], ...] = ()
    deterministic_mixed_script_allow: Tuple[str, ...] = ()
    # P1 АРКИ (owner decision 2026-08-14): deterministic English→Russian
    # arc-name mapping (arc_names.json), e.g. ("Bonds", "Узы"). Rendered as
    # an "АРКИ:" block in the whole-chapter generation prompt so chapter
    # headings translate consistently (Bonds = Узы in every chapter). Part
    # of the config identity (to_config_artifact) — changing the mapping
    # invalidates cache/resume exactly like any other prompt input.
    deterministic_arc_names: Tuple[Tuple[str, str], ...] = ()
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
    # V4 Efficiency A2: single balanced_literary candidate per chunk with a
    # lazy fidelity_first fallback when the primary fails the Qwen/
    # deterministic gates. False restores the legacy 2-candidate A/B + Gemma
    # scheme (full rollback). It is part of the config identity (see
    # to_config_artifact) so flipping it invalidates resume/cache correctly.
    lazy_balanced: bool = True
    # V4.1 reasoning transport: Phase 2B generation reasoning budget (0=off,
    # 1=low, 2=medium, 3=high). Only generation (Phase 2B) consumes it; the
    # Qwen audit/repair/formatting phases are untouched. Part of the config
    # identity (to_config_artifact) so a reasoning change invalidates
    # cache/resume exactly like any other generation setting.
    reasoning: int = 0
    # V4.1 early-exit policy: "" runs the full cycle (audit/repair/
    # formatting); "generation" halts right after Phase 1-2 generation
    # (whole-chapter mode is generation-only in A1, so the whole-chapter path
    # records Steps 6/7/8 as skipped regardless). Part of the config identity
    # (to_config_artifact) — a run that stops early is a different run from a
    # full one. Renamed from "selection" (V4.1 A1: the CLI flag is now
    # --stop-after-generation; chunked runs keep the same halt point).
    stop_after: str = ""
    # V4.1 A1: whole-chapter mode — one generation call per chapter against
    # the full ordered PID map (WholeChapterPidMap), no chunking/selection.
    # Steps 6/7/8 (audit/repair/formatting) are NOT part of A1 (they are
    # A2/B/B2/C) and are recorded as skipped. Part of the config identity:
    # a whole-chapter run is not resumable from a chunked run's out-dir and
    # vice versa.
    whole_chapter: bool = False
    # V4.1 B3 (concept §10 B3, §9.4): production audit/repair after
    # whole-chapter generation. When True (production default) AND the B3
    # machinery is injected (``b3_audit_repair``), the whole-chapter path
    # runs ChunkedAuditEvaluator -> apply_hard_filters -> selective repair
    # -> re-audit and rewrites translations_repaired.json/translations.json
    # with the repaired map; ``--skip-audit`` turns the stage off (the
    # steps are then recorded as skipped, A1 behavior). Part of the config
    # identity — flipping it invalidates cache/resume exactly like any
    # other run setting.
    run_audit: bool = True
    # V4.1 B3 (owner decision 2026-08-10, B1.3 gate pending): the source-only
    # entity prepass (B1.2) feeds both the auditor and the hard filters
    # (entity-PID issues forced to TIER_B). Runtime config; default true;
    # false audits without the entity block. Part of the config identity.
    entity_context_enabled: bool = True
    # V4.1 B3 audit input budget (card §10 B3 "max_input/max_tokens/overlap
    # в config"); the Qwen audit server profile (MTP, reasoning 8192, 49k)
    # lives in the runtime config server_args. Part of the config identity
    # so a budget change invalidates cache/resume.
    audit_max_input_tokens: int = 3600
    audit_max_tokens: int = 12000
    audit_overlap_tokens: int = 400
    # R-RETRY (t_8ab8ab35, operator extension 2026-08-13, F5): the chunk-
    # level TRANSPORT_ERROR bounded retry policy (NEW session per attempt)
    # is identity-bearing and wired into B3AuditRepairConfig by
    # _build_b3_audit_repair — a cache written under a different
    # transport-retry policy must never replay a failed chunk.
    audit_transport_max_retries: int = DEFAULT_TRANSPORT_MAX_RETRIES
    audit_transport_base_delay_seconds: float = DEFAULT_TRANSPORT_BASE_DELAY_SECONDS
    # V4.1 B3 (review fix F5): EVERY authoritative B3 repair-policy knob and
    # prompt/extractor version participates in the config identity and is
    # wired into B3AuditRepairConfig by _build_b3_audit_repair. Before this
    # fix the identity carried only the audit budgets and the repair policy
    # silently used module defaults — flipping a repair knob could then
    # reuse a stale cached repaired map. Values mirror the module defaults
    # (pact_v4.audit.chunked_audit / entity_extractor / repair.selective_repair).
    audit_reasoning_budget: int = DEFAULT_REASONING_BUDGET
    audit_repair_findings_cap: int = REPAIR_FINDINGS_CAP
    audit_repair_microbatch_trigger: int = MICROBATCH_TRIGGER
    audit_repair_microbatch_target: int = MICROBATCH_TARGET
    # REPAIR-CTX (card t_97b31f81, F5): the repair-batch local context
    # window (±N neighbour pairs around each finding PID, default 3 — owner
    # decision 2026-08-12) is identity-bearing: a window change invalidates
    # cache/resume exactly like every other repair-policy knob, so a stale
    # cached repaired map (full-chapter batches) can never replay under the
    # local-context prompt. Wired into B3AuditRepairConfig by
    # _build_b3_audit_repair.
    audit_repair_context_window: int = DEFAULT_REPAIR_CONTEXT_WINDOW
    # REPAIR-2 (card t_768537b9, F5): the per-category window overrides
    # ({category: window}; categories not in the map fall back to
    # ``audit_repair_context_window`` — invented_gender/referent/omission
    # default ±10, changed_fact/addition stay ±3; owner decision 2026-08-12).
    # Identity-bearing: a per-category window change invalidates cache/resume
    # exactly like the scalar window, so a stale cached repaired map (narrow
    # gender window) can never replay under a wide-gender-window prompt.
    # Wired into B3AuditRepairConfig by _build_b3_audit_repair.
    audit_repair_context_window_by_category: Mapping[str, int] = field(
        default_factory=lambda: dict(DEFAULT_REPAIR_CONTEXT_WINDOW_BY_CATEGORY)
    )
    audit_repair_reaudit_neighbour_window: int = DEFAULT_REAUDIT_NEIGHBOUR_WINDOW
    # REPAIR-CTX (t_97b31f81, owner decision 2026-08-12): the re-audit is a
    # CHUNKED audit over the affected region — the whole-chapter re-audit
    # mode (old full_threshold) is CANCELLED. The re-audit chunk/overlap
    # settings and the REPAIRED CHANGES delta format are identity-bearing
    # (F5): changing them invalidates cache/resume so a stale cached repaired
    # map (full-chapter re-audit) can never replay under the chunked
    # local-context prompt. Wired into B3AuditRepairConfig by
    # _build_b3_audit_repair.
    audit_repair_reaudit_max_input_tokens: int = DEFAULT_REAUDIT_MAX_INPUT_TOKENS
    audit_repair_reaudit_overlap_tokens: int = DEFAULT_REAUDIT_OVERLAP_TOKENS
    audit_repair_reaudit_min_overlap_pairs: int = DEFAULT_REAUDIT_MIN_OVERLAP_PAIRS
    audit_repair_reaudit_max_overlap_pairs: int = DEFAULT_REAUDIT_MAX_OVERLAP_PAIRS
    audit_repair_reaudit_delta_format: str = REAUDIT_DELTA_FORMAT
    # V4.1 B3 (RV fix for 71b7cbc): the re-audit output budget and its
    # bounded B4 JSON retry policy are identity-bearing like every other
    # repair-policy knob (F5). The selective repair code sends the budget
    # as max_output_tokens and retries empty/truncated re-audit JSON per
    # the policy; WITHOUT these fields a cache produced under the old
    # 12000-token re-audit could be replayed under the 20000-token policy.
    # Defaults mirror the selective-repair module (20000 tokens; JsonRetryPolicy
    # max_retries=2 -> 3 attempts, base_delay_seconds=1.0).
    audit_repair_reaudit_max_tokens: int = DEFAULT_REAUDIT_MAX_TOKENS
    # REPAIR-MAX-TOKENS (owner decision 2026-08-15, "16к Делай"): the
    # per-batch repair OUTPUT budget (distinct from the re-audit budget) —
    # 4000 was exhausted by deepseek reasoning in run_0004-0005 (raw=0,
    # reasoning 26-33k bytes) → 5/6 repair batches failed. Identity-bearing
    # (F5): wired through _build_b3_audit_repair so a budget change
    # invalidates a stale cached repaired map.
    audit_repair_max_tokens: int = DEFAULT_REPAIR_MAX_TOKENS
    # REPAIR-ROBUST (card t_b6fd6cbd, run_0005): the per-batch repair
    # reasoning effort (0=off, 1=low, 2=medium, 3=high) for REMOTE
    # transports only — default 1 (low): deepseek high burned 32k reasoning
    # tokens on a repair batch and exhausted max_tokens before content
    # (run_0005 batch1: raw=0, finish=length). Identity-bearing (F5): wired
    # through _build_b3_audit_repair so a reasoning change invalidates a
    # stale cached repaired map. Inert locally (local server args govern;
    # LocalOpenAIBackend rejects request_options).
    audit_repair_reasoning: int = DEFAULT_REPAIR_REASONING
    audit_repair_reaudit_max_retries: int = DEFAULT_REAUDIT_MAX_RETRIES
    audit_repair_reaudit_base_delay_seconds: float = DEFAULT_REAUDIT_BASE_DELAY_SECONDS
    audit_prompt_version: str = PROMPT_VERSION
    audit_harness_version: str = HARNESS_VERSION
    audit_extractor_version: str = EXTRACTOR_VERSION
    # CANDIDATE-MERGE (t_0ffe56e1, RV2 HIGH finding): the REPAIR prompt
    # version (REPAIR_AS_VERIFIER_V1 v4 — the source_stage/merge contract)
    # is identity-bearing like every other B3 prompt/extractor version (F5).
    # Before this field, a cache written under the v3 repair prompt could
    # replay under v4 — the repaired map is a function of the repair prompt,
    # so its version MUST participate in the config identity and the B3
    # payload/report. Values mirror the module defaults
    # (pact_v4.repair.selective_repair); wired into B3AuditRepairConfig by
    # _build_b3_audit_repair.
    audit_repair_prompt_version: str = REPAIR_PROMPT_VERSION
    audit_repair_harness_version: str = REPAIR_HARNESS_VERSION
    # V4.2 R (card t_4707e6e5): Russian-only editor stage BEFORE the audit.
    # On by default (owner decision 2026-08-11 — R is production-default);
    # ``--no-russian-editor`` turns it off (scheme 4.1, backward compatible).
    # Every knob below participates in the config identity (F5 lesson): the
    # editor version, the chunk settings and the class threshold are all
    # part of to_config_artifact, so flipping any of them invalidates the
    # repaired cache — a repaired map produced under a different R policy
    # never replays.
    russian_editor_enabled: bool = True
    russian_editor_version: str = RUSSIAN_EDITOR_PROMPT_VERSION
    russian_editor_harness_version: str = RUSSIAN_EDITOR_HARNESS_VERSION
    russian_editor_chunk_size: int = RUSSIAN_EDITOR_CHUNK_SIZE
    russian_editor_overlap_pairs: int = RUSSIAN_EDITOR_OVERLAP_PAIRS
    russian_editor_max_tokens: int = RUSSIAN_EDITOR_MAX_TOKENS
    # Class threshold: SAFE classes (auto-applied with the diff-gate).
    russian_editor_safe_classes: tuple = tuple(sorted(RUSSIAN_EDITOR_SAFE_CLASSES))
    # R-RETRY (t_8ab8ab35, F5): the per-pid edit cap (duplicate pid is NOT
    # an error — up to this many edits per pid; 11th+ drops per-edit with a
    # WARNING) and the bounded retry policy (transport + empty/truncated
    # JSON) are identity-bearing — a cache written under a different
    # cap/retry policy must never replay the edited map.
    russian_editor_max_edits_per_pid: int = RUSSIAN_EDITOR_MAX_EDITS_PER_PID
    russian_editor_retry_max_retries: int = RUSSIAN_EDITOR_RETRY_MAX_RETRIES
    russian_editor_retry_base_delay_seconds: float = RUSSIAN_EDITOR_RETRY_BASE_DELAY_SECONDS

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
                    "reasoning": self.reasoning,
                },
                "stop_after": self.stop_after,
                "whole_chapter": self.whole_chapter,
                "formatting": {
                    "required": self.formatting_required,
                    "max_incidents": self.max_formatting_incidents,
                    "policy_version": self.formatting_policy_version,
                },
                # B5 mixed_script-политика: the manual allowlist is a gate-policy
                # input, so it is part of the run's config identity — changing it
                # invalidates cache/resume exactly like a memory/source change.
                "deterministic_mixed_script_allow": list(self.deterministic_mixed_script_allow),
                # P1 АРКИ (owner decision 2026-08-14): the deterministic arc
                # mapping renders an "АРКИ:" block into the generation prompt,
                # so it is part of the config identity — a changed mapping
                # invalidates cache/resume exactly like a glossary change.
                "deterministic_arc_names": [
                    list(pair) for pair in self.deterministic_arc_names
                ],
                # V4 Efficiency A1.1 (review fix, HIGH): the glossary budgeter
                # changes the actual generation prompts, so the policy version
                # MUST be part of the config identity. Without it, a journal
                # written before the policy (full-glossary prompts) passes the
                # resume identity check and its chunks are silently replayed
                # alongside post-policy filtered candidates, mixing two
                # different prompt regimes in one run. Versioning the identity
                # makes any pre-policy journal a foreign identity -> resume
                # refuses instead of silently reusing.
                "glossary_budget_policy_version": GLOSSARY_BUDGET_POLICY_VERSION,
                # V4 Efficiency A2: the lazy balanced-only generation scheme is
                # part of the run's config identity — flipping it changes which
                # candidates are generated (balanced-only vs A/B pair), so a
                # journal written under the other scheme must be refused on
                # resume (same reasoning as glossary_budget_policy_version).
                "efficiency": {"lazy_balanced": self.lazy_balanced},
                # V4.1 B3: the production audit/repair stage is part of the
                # run's config identity — flipping run_audit /
                # entity_context_enabled / audit budget invalidates
                # cache/resume exactly like any other generation setting.
                # F5: every repair-policy knob and prompt/extractor/harness
                # version is included, so changing the repair policy can
                # never silently reuse a stale cached repaired map.
                "audit": {
                    "run": self.run_audit,
                    "entity_context_enabled": self.entity_context_enabled,
                    "max_input_tokens": self.audit_max_input_tokens,
                    "max_tokens": self.audit_max_tokens,
                    "overlap_tokens": self.audit_overlap_tokens,
                    "reasoning_budget": self.audit_reasoning_budget,
                    "audit_transport_retry": {
                        "max_retries": self.audit_transport_max_retries,
                        "base_delay_seconds": self.audit_transport_base_delay_seconds,
                    },
                    "repair_findings_cap": self.audit_repair_findings_cap,
                    "repair_microbatch_trigger": self.audit_repair_microbatch_trigger,
                    "repair_microbatch_target": self.audit_repair_microbatch_target,
                    # REPAIR-CTX (t_97b31f81): the local-context window is
                    # identity-bearing — a change invalidates cache/resume
                    # (F5: an old full-chapter repaired map must never replay
                    # under a local-context prompt).
                    "repair_context_window": self.audit_repair_context_window,
                    # REPAIR-2 (t_768537b9): the per-category window
                    # overrides are identity-bearing — a change invalidates
                    # cache/resume (F5: a stale repaired map written under a
                    # narrow gender window must never replay under a
                    # wide-gender-window prompt).
                    "repair_context_window_by_category": dict(
                        self.audit_repair_context_window_by_category
                    ),
                    "repair_reaudit_neighbour_window": self.audit_repair_reaudit_neighbour_window,
                    # REPAIR-CTX (t_97b31f81): the re-audit chunk/overlap
                    # settings and the REPAIRED CHANGES delta format are
                    # identity-bearing — a change invalidates cache/resume
                    # (F5: an old full-chapter re-audit must never replay
                    # under the chunked local-context prompt).
                    "repair_reaudit_chunk": {
                        "max_input_tokens": self.audit_repair_reaudit_max_input_tokens,
                        "overlap_tokens": self.audit_repair_reaudit_overlap_tokens,
                        "min_overlap_pairs": self.audit_repair_reaudit_min_overlap_pairs,
                        "max_overlap_pairs": self.audit_repair_reaudit_max_overlap_pairs,
                        "delta_format": self.audit_repair_reaudit_delta_format,
                    },
                    # F5: the re-audit output budget and bounded retry policy
                    # are identity-bearing — a cache written under the old
                    # 12000-token re-audit must never replay under the
                    # 20000-token policy (RV 71b7cbc finding).
                    "repair_reaudit_max_tokens": self.audit_repair_reaudit_max_tokens,
                    # REPAIR-MAX-TOKENS (owner decision 2026-08-15): the
                    # per-batch repair OUTPUT budget is identity-bearing —
                    # a cache written under 4000 (empty deepseek repair
                    # responses, run_0004-0005) must never replay under
                    # 16000. F5: budget change invalidates cache/resume.
                    "repair_max_tokens": self.audit_repair_max_tokens,
                    # REPAIR-ROBUST (t_b6fd6cbd, F5): the repair reasoning
                    # effort is identity-bearing — a change invalidates a
                    # stale cached repaired map (old high-reasoning repairs
                    # must never replay under low).
                    "repair_reasoning": self.audit_repair_reasoning,
                    "repair_reaudit_retry": {
                        "max_retries": self.audit_repair_reaudit_max_retries,
                        "base_delay_seconds": self.audit_repair_reaudit_base_delay_seconds,
                    },
                    "prompt_version": self.audit_prompt_version,
                    "harness_version": self.audit_harness_version,
                    "extractor_version": self.audit_extractor_version,
                    # CANDIDATE-MERGE (t_0ffe56e1, RV2 HIGH finding, F5): the
                    # REPAIR prompt version participates in the identity —
                    # a cache written under a different repair prompt must
                    # never replay the repaired map.
                    "repair_prompt_version": self.audit_repair_prompt_version,
                    "repair_harness_version": self.audit_repair_harness_version,
                },
                # V4.2 R (card t_4707e6e5, F5 lesson): the Russian-only
                # editor stage is part of the run's config identity — its
                # version, chunk settings and class threshold are included,
                # so a cache written under a different R policy can never
                # replay the repaired map (same reasoning as the audit
                # block). ``--no-russian-editor`` flips ``enabled`` and thus
                # invalidates cache/resume (scheme 4.1 is a different run).
                "russian_editor": {
                    "enabled": self.russian_editor_enabled,
                    "version": self.russian_editor_version,
                    "harness_version": self.russian_editor_harness_version,
                    "chunk_size": self.russian_editor_chunk_size,
                    "overlap_pairs": self.russian_editor_overlap_pairs,
                    "max_tokens": self.russian_editor_max_tokens,
                    "safe_classes": list(self.russian_editor_safe_classes),
                    "max_edits_per_pid": self.russian_editor_max_edits_per_pid,
                    "r_editor_retry": {
                        "max_retries": self.russian_editor_retry_max_retries,
                        "base_delay_seconds": self.russian_editor_retry_base_delay_seconds,
                    },
                },
            },
        )


def build_strict_lifecycle(
    backend: StrictBackendConfig, *, log_dir: Path, bible_text: str = "",
    json_retry_policy: Optional[JsonRetryPolicy] = None,
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

    ``json_retry_policy`` (A2 review fix, whole-chapter retry ownership) is
    the adapter-level JSON retry policy for the generation caller. In
    whole-chapter mode the CLI passes ``JsonRetryPolicy(max_retries=0)`` so
    the generation layer (``WholeChapterRetryPolicy``) is the single retry
    owner — total model attempts stay exactly ``max_attempts`` instead of
    ``max_attempts × adapter-budget``. When ``None`` (the default; chunked
    runs and test stubs) the historical ``JsonRetryPolicy()`` (max_retries=2)
    is preserved, so the chunked path keeps its current retry budget.
    """
    from pact_v4.runtime.qwen_evaluator import HttpQwenEvaluatorConfig
    from pact_v4.runtime.backend_role_adapters import (
        BackendQwenAuditEvaluatorConfig,
        BackendGemmaAuditEvaluatorConfig,
    )
    runtime = backend.build_runtime(log_dir=log_dir)
    router = runtime.router
    model_caller = LifecycleModelCaller(
        router, model_name=backend.model_names[GEMMA_MODEL_KEY],
        json_retry_policy=json_retry_policy,
    )
    qwen_evaluator = LifecycleQwenEvaluator(
        router, model_name=backend.model_names[QWEN_MODEL_KEY],
        config=HttpQwenEvaluatorConfig(bible_text=bible_text),
    )
    gemma_selector = LifecycleGemmaSelector(router, model_name=backend.model_names[GEMMA_MODEL_KEY])
    qwen_audit_evaluator = LifecycleQwenAuditEvaluator(
        router, model_name=backend.model_names[QWEN_MODEL_KEY],
        config=BackendQwenAuditEvaluatorConfig(bible_text=bible_text),
    )
    gemma_audit_evaluator = LifecycleGemmaAuditEvaluator(
        router, model_name=backend.model_names[GEMMA_MODEL_KEY],
        config=BackendGemmaAuditEvaluatorConfig(bible_text=bible_text),
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


def _pid_diffs(
    before: Mapping[str, str], after: Mapping[str, str]
) -> Dict[str, Dict[str, str]]:
    """``{pid: {before, after}}`` for PIDs whose text changed between stages.

    V4.1 A2 (§7): the translation diff report is split by stage
    (``raw->repaired`` and ``repaired->final``) so regressions can be
    attributed to the stage that introduced them. PIDs with identical
    text are omitted; only actually-changed PIDs appear.
    """
    diffs: Dict[str, Dict[str, str]] = {}
    for pid in sorted(set(before) | set(after)):
        before_text = before.get(pid)
        after_text = after.get(pid)
        if before_text != after_text:
            diffs[pid] = {"before": before_text or "", "after": after_text or ""}
    return diffs


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


def _merged_glossary_budget_chunks(
    *,
    report_path: Path,
    schema: str,
    policy_version: str,
    chapter_id: str,
    snapshot_hash: str,
    chunk_plan_hash: str,
    config_identity: str,
    current_chunks: Dict[str, Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Merge a prior session's glossary-budget rows into the current run's.

    V4 Efficiency A1.1 review fix (MEDIUM): partial resume replays chunks
    from the journal WITHOUT re-budgeting them, so this session's rows
    cover only the newly processed chunks. Overwriting the artifact with
    just those rows silently loses the replayed chunks' rows. A prior
    report is therefore merged in — but ONLY after schema/policy/run-
    identity validation: the prior report must carry the same schema,
    policy version, chapter, snapshot, chunk plan and config identity as
    this run. A prior report from a different run/policy is foreign (its
    rows describe a different prompt regime) and is NOT merged; a warning
    is logged and only this session's rows are returned, so the artifact
    always stays unambiguous. On a chunk-id collision (a chunk processed in
    both sessions under the same identity) the current session's row wins —
    under one identity the row is deterministic, so this only matters if a
    chunk is re-budgeted for some other reason.
    """
    if not report_path.exists():
        return current_chunks
    try:
        prior = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOG.warning(
            "glossary_budget_report.json exists but is unreadable; writing "
            "this session's rows only (no merge)"
        )
        return current_chunks
    if not isinstance(prior, dict):
        LOG.warning(
            "glossary_budget_report.json is not an object; writing this "
            "session's rows only (no merge)"
        )
        return current_chunks
    prior_chunks = prior.get("chunks")
    if not isinstance(prior_chunks, dict):
        LOG.warning(
            "glossary_budget_report.json has no chunks map; writing this "
            "session's rows only (no merge)"
        )
        return current_chunks
    same_run = (
        prior.get("schema") == schema
        and prior.get("policy_version") == policy_version
        and prior.get("chapter_id") == chapter_id
        and prior.get("snapshot_hash") == snapshot_hash
        and prior.get("chunk_plan_hash") == chunk_plan_hash
        and prior.get("config_identity") == config_identity
    )
    if not same_run:
        LOG.warning(
            "glossary_budget_report.json belongs to a different run or "
            "policy (schema/policy/chapter/snapshot/plan/config identity "
            "mismatch); NOT merging it — writing this session's rows only"
        )
        return current_chunks
    merged = dict(prior_chunks)
    merged.update(current_chunks)
    return merged


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
    # Keyed by chunk_id like _merge_generation_outcomes; coalesce any
    # duplicate records for one chunk (the A2 lazy rescue's primary +
    # lazy outcomes) so every produced candidate is visible to the
    # best-variant rule — a last-wins dict would silently drop the
    # primary balanced candidate (RV A2 finding 1).
    gen_by_chunk: Dict[str, Dict[str, Any]] = {}
    for rec in generation_records:
        chunk_id = rec.get("chunk_id")
        if not chunk_id:
            continue
        if chunk_id in gen_by_chunk:
            gen_by_chunk[chunk_id] = _coalesce_generation_outcome_records(
                [gen_by_chunk[chunk_id], rec]
            )
        else:
            gen_by_chunk[chunk_id] = rec

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


def _coalesce_generation_outcome_records(
    records: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Merge several serialized generation-outcome records of the SAME chunk
    into one cumulative record.

    V4 Efficiency A2 lazy rescue produces two records for one chunk_id: the
    primary ``balanced_literary`` outcome and the lazy ``fidelity_first``
    outcome. Downstream consumers key generation records by chunk_id
    (``_merge_generation_outcomes``, ``_audit_candidate_map``), so a
    last-wins merge would silently drop the primary candidate — Step 6 could
    no longer pick a deterministic best-variant among the variants the chunk
    actually produced. Coalescing keeps every candidate, every error trace
    and the union of expected roles in one record.

    ``status`` is re-derived from the coalesced content (``complete`` only
    when every expected role has a produced candidate), matching the
    semantics of ``_serialize_generation_outcome``.
    """
    if not records:
        return {}
    base = dict(records[0])
    candidates: Dict[str, Any] = dict(base.get("candidates") or {})
    errors: Dict[str, Any] = dict(base.get("errors") or {})
    expected_roles: List[str] = list(base.get("expected_roles") or [])
    for rec in records[1:]:
        for role, candidate in (rec.get("candidates") or {}).items():
            candidates[role] = candidate
        for role, error in (rec.get("errors") or {}).items():
            errors[role] = error
        for role in rec.get("expected_roles") or []:
            if role not in expected_roles:
                expected_roles.append(role)
    base["expected_roles"] = expected_roles
    base["candidates"] = candidates
    base["errors"] = errors
    base["status"] = "complete" if len(candidates) == len(expected_roles) else "incomplete"
    return base


def _coalesce_lazy_record_into_primary(
    generation_records: List[Dict[str, Any]],
    chunk_id: str,
    lazy_outcome: Any,
) -> None:
    """Coalesce a lazy A2 fidelity outcome into the chunk's PRIMARY
    generation record (in place).

    The primary balanced_literary record was appended when the chunk's
    initial generation completed; the lazy rescue produced a second outcome
    for the SAME chunk_id. Downstream consumers (``_merge_generation_outcomes``
    and Step 6's ``_audit_candidate_map``) key generation records by
    chunk_id, so a naive second append would make a last-wins merge drop the
    primary balanced candidate — Step 6 could not pick the deterministic
    best-variant among the variants the chunk actually produced. This helper
    merges the lazy outcome into the existing primary record so both
    candidates (and both decision traces / errors) survive in the cumulative
    record. The final selected/quarantine outcome is decided by the caller
    and is untouched here.
    """
    lazy_serialized = _serialize_generation_outcome(lazy_outcome)
    for index in range(len(generation_records) - 1, -1, -1):
        if generation_records[index].get("chunk_id") == chunk_id:
            generation_records[index] = _coalesce_generation_outcome_records(
                [generation_records[index], lazy_serialized]
            )
            return
    # No primary record found (should not happen — the primary always
    # precedes the lazy rescue) — fall back to appending so the lazy
    # outcome is at least persisted.
    generation_records.append(lazy_serialized)


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

    Records are keyed by chunk_id; multiple records for the same chunk (the
    A2 lazy rescue's primary + lazy outcomes) are coalesced into one
    cumulative record rather than last-wins, so every produced candidate
    survives into Step 6 and across resumes.
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
    by_chunk: Dict[str, List[Dict[str, Any]]] = {}
    for rec in list(prior) + list(current_records):
        chunk_id = rec.get("chunk_id")
        if chunk_id:
            by_chunk.setdefault(chunk_id, []).append(rec)
    return [
        _coalesce_generation_outcome_records(by_chunk[chunk.chunk_id])
        for chunk in chunk_plan.chunks
        if chunk.chunk_id in by_chunk
    ]


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


def _load_repair_report_final_translation(
    out_dir: Path,
) -> Optional[Dict[str, str]]:
    """Return the authoritative final translation from ``repair_report.json``.

    B13 (owner decision 2026-08-05): the single source of the chapter's
    final translation is ``repair_report.final_translation`` — the PID map
    after Step 7 repair, Phase 5 formatting and the B6 quarantined-retry
    cycle. The on-disk report is authoritative over the in-memory
    ``RepairPhaseResult`` because ``_run_quarantined_retry_cycle`` re-writes
    it with the merged map (retry winners replace the best-variant text), but
    the frozen dataclass is never updated. Returns ``None`` when the report
    is missing or carries no ``final_translation`` (the caller falls back to
    the historical selected-candidates map).
    """
    path = _repair_report_path(out_dir)
    if not path.exists():
        return None
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    final = report.get("final_translation")
    if not final:
        return None
    return {pid: text for pid, text in final}


def _normalize_final_markup(text: str) -> str:
    """Normalize the final chapter text's inline markup to clean tags.

    B13 (owner decision 2026-08-05): Phase 5 formatting restores the
    original's italic with ``<em>…</em>`` tags, but the model-fallback tier
    can double-escape them (``&lt;em&gt;…&lt;/em&gt;``). The final
    translation keeps the italics, so when it is merged into
    ``translations.json`` the markup is normalized to clean tags; the
    visible text is otherwise unchanged.

    B14: this delegates to the single shared deterministic helper
    ``normalize_inline_markup`` (``pact_v4._integrity_checks``) — entities
    of inline tags become clean tags and double wraps collapse into one.
    """
    return normalize_inline_markup(text)


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

    B12-F4 (RV4 HIGH): a cache whose envelope schema is the known legacy
    ``pact-v4-phase4-repair-cache/v1`` (pre-F3 repair policy, unit hashes
    under ``pact-v4-repair-policy/v1``) is deliberately not reusable — its
    ``committed=True`` records may have been fixed under the old
    ``bool("false")`` truthiness the F3 fail-closed fix removed. It is
    treated as a fresh start (return ``None``), never as a reusable cache.
    An unknown schema is still foreign identity and raises.
    """
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    schema = payload.get("schema")
    if schema == LEGACY_REPAIR_CACHE_SCHEMA:
        # B12-F4 (RV4 HIGH): a cache written under the pre-F3 repair policy
        # (unit hashes under ``pact-v4-repair-policy/v1``) is a known legacy
        # generation, not foreign data — it may hold ``committed=True``
        # records fixed under the old ``bool("false")`` truthiness. Never
        # reuse it: start a fresh cache so the fail-closed re-gate re-runs.
        LOG.info(
            "Repair cache from the pre-F4 repair policy (%s) is not reusable; "
            "starting a fresh repair cache",
            schema,
        )
        return None
    if schema != REPAIR_CACHE_SCHEMA:
        raise ValueError(
            f"Foreign identity: repair cache schema={schema!r}"
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
            "repair": REPAIR_POLICY_VERSION,
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
            "model_call_count": formatting_payload["model_call_count"],
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
) -> Tuple[Dict[str, QuarantinedRetryAttempt], Optional[str], bool]:
    """Reload a prior session's retry history, refusing foreign identity.

    Mirrors ``_merge_generation_outcomes`` / ``_load_repair_cache``: the
    retry history is only reusable when the enclosing run's identities
    (snapshot/plan/config/backend) match, so a resumed run deterministically
    reuses already-recorded attempts instead of re-paying the bounded
    regeneration or silently mixing retry state across runs.

    Returns ``(attempts, final_text_hash, policy_matches)``:

    * ``attempts`` — the prior per-chunk attempts (usable for candidate
      reconstruction regardless of policy: a candidate's text is not
      policy-bound, and ``Candidate.create`` re-validates its identity);
    * ``final_text_hash`` — the canonical hash of the final chapter text the
      prior session's retry cycle ended with (``None`` when the file is from
      a session that never completed the cycle);
    * ``policy_matches`` — whether the file's ``policy_version`` equals the
      current retry policy. When ``False`` the history is NOT eligible for
      the pure-resume lease skip (the re-audit / repair round / formatting
      re-run under the new policy), but the attempts still reconstruct the
      prior candidates.
    """
    path = _quarantined_retry_path(out_dir)
    if not path.exists():
        return {}, None, True
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
    policy_matches = payload.get("policy_version") == QUARANTINED_RETRY_POLICY_VERSION
    if not policy_matches:
        LOG.info(
            "quarantined_retry.json written under retry policy %r; current "
            "policy is %r — pure-resume lease skip disabled (re-audit will "
            "re-run under the current policy)",
            payload.get("policy_version"), QUARANTINED_RETRY_POLICY_VERSION,
        )
    attempts = payload.get("attempts") or []
    if not isinstance(attempts, list):
        raise ValueError("quarantined_retry.json: attempts must be an array")
    final_text_hash = payload.get("final_text_hash")
    return (
        {
            item["chunk_id"]: QuarantinedRetryAttempt.from_payload(item)
            for item in attempts
            if item.get("chunk_id")
        },
        final_text_hash,
        policy_matches,
    )


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
    bible_text: str = "",
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
    prior_attempts, prior_final_text_hash, retry_policy_matches = _load_prior_quarantined_retries(
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

    # Resume gate (V4 B6 owner decision 2026-08-04): on a clean resume — every
    # triggered chunk already has a prior attempt AND there is no fresh
    # repair debt — skip the model-leases (Qwen re-audit + Gemma + optional
    # repair round + formatting re-run) and just restore the prior attempt's
    # candidate + markers from ``quarantined_retry.json``. The re-audit result
    # is provably identical (no text changed), so the ~2 model leases/resume
    # are pure waste. The terminal / repair_report / formatting_report are
    # still re-written from the prior attempt so consumers see the same shape
    # as a non-resume run; only the lease cost is elided.
    #
    # A1c Phase 0 (review §3.6): the skip is only safe when the retry policy
    # AND the final chapter text are byte-identical to the session that wrote
    # the history. ``prior_final_text_hash`` was recorded against the prior
    # session's final text; the current session's final text (from the fresh
    # Step 7 repair) must hash identically — otherwise the re-audit verdict
    # is not provably reusable and the leases must be re-paid under the
    # current policy.
    fresh_debt = chunk_ids and bool(debt_chunks)
    current_final_text_hash = _chapter_text_identity_hash(
        source=source,
        snapshot=snapshot,
        chunk_plan=chunk_plan,
        config=config,
        translation=dict(repair_phase_result.final_translation),
    )
    pure_resume = (
        not fresh_debt
        and retry_policy_matches
        and prior_final_text_hash is not None
        and prior_final_text_hash == current_final_text_hash
        and all(chunk_id in prior_attempts for chunk_id in chunk_ids)
        and all(
            prior_attempts[chunk_id].outcome in (OUTCOME_SELECTED, OUTCOME_QUARANTINED_FINAL)
            for chunk_id in chunk_ids
        )
    )

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
        bible_text=bible_text,
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
    if selected_chunks and not pure_resume:
        chunk_translation = {
            chunk_id: {
                pid: final_map.get(pid, "")
                for pid in chunk_plan.chunk(chunk_id).pids
            }
            for chunk_id in selected_chunks
        }
        reaudit_outcome = _reaudit_chunks(
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
        reaudit_findings = reaudit_outcome.findings
        for chunk_id, detector in reaudit_outcome.failed_units:
            retry_debt.append(
                f"{chunk_id}: quarantined-retry re-audit {detector} failed "
                "(transport/parse) — findings incomplete"
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
                "failed_units": [list(unit) for unit in reaudit_outcome.failed_units],
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
    # `complete` — the same invariant the main repair phase maintains. Skipped
    # on a pure resume (no text changed since the prior session's formatting
    # run wrote formatting_report.json — re-running it would be the same model
    # call as the elided re-audit).
    formatting_outcome = None
    if formatting_step is not None and not pure_resume:
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

    # A1c Phase 0 (review §3.6): persist the final text hash the retry
    # outcome was verified against. The early write above is crash-safety for
    # the attempts; this final write adds the authoritative hash so a later
    # resume can prove a clean pure-resume (same policy + byte-identical
    # final text) and re-pay the re-audit leases otherwise.
    _atomic_write_json(_quarantined_retry_path(cfg.out_dir), {
        "schema": QUARANTINED_RETRY_SCHEMA,
        "policy_version": QUARANTINED_RETRY_POLICY_VERSION,
        "chapter_id": cfg.chapter_id,
        "source_hash": source.source_hash,
        "snapshot_hash": snapshot.snapshot_hash,
        "chunk_plan_hash": chunk_plan.plan_hash,
        "config_identity": config.config_identity,
        "backend_identity_hash": backend_identity_hash,
        "final_text_hash": integrity["frozen_hash"],
        "attempts": [attempt.to_payload() for attempt in result.attempts],
    })

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
            "model_call_count": formatting_outcome.model_call_count,
            "max_formatting_incidents": formatting_outcome.max_formatting_incidents,
            "report_path": str(_formatting_report_path(cfg.out_dir)),
        }
    return summary


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------


def _close_run_resources(runtime: Any, progress_writer: Any, usage_writer: Any) -> None:
    """Idempotently close the runtime / progress writer / usage writer.

    A2 review fix (pre-dispatch cleanup): the whole-chapter wrapper's
    ``finally`` already closes these on every path once dispatch has
    started, but failures BEFORE dispatch (source/snapshot/planner) used to
    leak them. This helper is the single guarded close routine: each close
    is individually guarded so a cleanup error is logged and NEVER masks the
    original exception the caller is propagating. ``close()`` is idempotent
    (writers null their handle, coordinators guard on ``_closed``), so
    calling this both here and in the wrapper's ``finally`` is safe.
    """
    try:
        runtime.close()
    except Exception:  # noqa: BLE001
        LOG.exception("Failed to close runtime during pre-dispatch cleanup")
    try:
        progress_writer.close()
    except Exception:  # noqa: BLE001
        LOG.exception("Failed to close progress writer during pre-dispatch cleanup")
    try:
        usage_writer.close()
    except Exception:  # noqa: BLE001
        LOG.exception("Failed to close usage writer during pre-dispatch cleanup")


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
    b3_audit_repair: Optional[Any] = None,
    now: Optional[Any] = None,
    progress: Optional[Any] = None,
    usage_writer: Optional[Any] = None,
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

    Phase 5 formatting (B3, card C) runs between Step 7 convergence and
    Step 8 when ``cfg.formatting_required`` is set. It is **model-free by
    rule** ("formatting = 0 model calls"): the deterministic tiers
    (``preserved`` / ``exact`` / ``occurrence_aware`` / ``fuzzy``) locate the
    source inline spans in the repaired text — including the whole-chapter
    case where the translation already carries the inline markup (the
    ``preserved`` tier). There is no injected ``FormattingCaller``; a span
    the deterministic tiers cannot locate becomes a blocking
    ``FormattingIncident`` (debt), never a model call. The formatted text is
    what the Step 8 integrity check and the terminal transition see.

    V4.1 B3 (concept §10 B3): in whole-chapter mode, when ``cfg.run_audit``
    AND ``b3_audit_repair`` (``pact_v4.pipeline.b3_audit_repair.B3AuditRepair``)
    is injected, the generation is followed by the production audit/repair
    stage (ChunkedAuditEvaluator -> apply_hard_filters -> selective repair ->
    re-audit) and ``translations_repaired.json`` / ``translations.json`` are
    rewritten with the repaired map. Without the injected machinery the
    steps stay recorded as skipped (A1 behavior) even when ``run_audit`` is
    True — the runner never fabricates an audit.
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
    usage_writer = usage_writer or UsageRecordWriter(cfg.out_dir, now=now_fn)
    # D1/MONITOR-V2: per-call usage writing. Remote/composite coordinators
    # forward the writer to their backend's per-call completion sink, so
    # every completed remote call (success and failure) is appended to
    # usage.ndjson at the moment it finishes — crash-safe inside a phase,
    # not at phase boundaries. Since MONITOR-V2 (2.3) the local
    # sub-coordinator forwards the writer too (LocalLifecycleCoordinator
    # gained set_usage_writer), so local llama-server calls are journaled
    # exactly like remote ones.
    # MONITOR-V2 (2.4, RV t_c9f9ea90 HIGH #2): the injected lifecycle
    # adapters (legacy/default local path — build_strict_lifecycle over the
    # router) each own their OWN LocalOpenAIBackend that is NOT the
    # coordinator's registered LocalRoutingBackend, so register them on the
    # local coordinator before attaching the writer; every completed local
    # call then lands in usage.ndjson no matter which path built the backend.
    register = getattr(runtime, "register_usage_backend", None)
    if register is not None:
        for adapter in (model_caller, qwen_evaluator, gemma_selector,
                        qwen_audit_evaluator, gemma_audit_evaluator):
            if adapter is not None and hasattr(adapter, "set_usage_sink"):
                register(adapter)
    attach = getattr(runtime, "set_usage_writer", None)
    if attach is not None:
        attach(usage_writer)

    # ------------------------------------------------------------------
    # Rebuild source/snapshot/plan -- identical to run_chapter/run_generate.
    # A2 review fix (pre-dispatch cleanup): the resources above are created /
    # attached BEFORE this setup, so a failure here (empty/malformed source,
    # snapshot construction, planner) must not leak them. The whole-chapter
    # wrapper's own finally only covers failures once dispatch has started;
    # this outer guard closes the resources on ANY pre-dispatch failure and
    # re-raises the ORIGINAL exception — a cleanup error never masks it
    # (_close_run_resources guards each close), and close() is idempotent.
    # ------------------------------------------------------------------
    try:
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
        chunk_plan_payload = chunk_plan.to_payload()
        if cfg.whole_chapter:
            # V4.1 audit W (§14): whole-chapter generation does NOT use the
            # real chunk boundaries — only the ordered PID map
            # (WholeChapterPidMap) matters. Annotate the persisted plan
            # explicitly (metadata only, never part of plan_hash) so it
            # cannot be misread as an active chunking contract; the ordered
            # PID source of truth is whole_chapter_pid_map.json.
            chunk_plan_payload["mode"] = CHUNK_PLAN_MODE_WHOLE_CHAPTER
            chunk_plan_payload["note"] = CHUNK_PLAN_NOTE_WHOLE_CHAPTER
        chunk_plan_path = cfg.out_dir / "chunk_plan.json"
        chunk_plan_path.write_text(
            json.dumps(chunk_plan_payload, ensure_ascii=False, indent=2), encoding="utf-8",
        )

        glossary = _glossary_entries(memory)
        # V4.1 A2 (§5.2): the bible is rendered per chapter from the
        # deterministic chapter_index (no "first N" caps); when no
        # chapter_index.json exists the renderer falls back to the legacy
        # full-memory render, so runs without an index keep working.
        bible_text = render_bible_section(
            cfg.chapter_id, memory.chapter_index, memory.book_memory
        )
        narrator_gender = extract_narrator_gender(memory.book_memory)
        narrator_source_terms = _narrator_glossary_terms(memory.book_memory)
    except BaseException:
        _close_run_resources(runtime, progress_writer, usage_writer)
        raise

    # V4.1 A1 whole-chapter mode: one generation call per chapter against the
    # full ordered PID map. This is a fundamentally different flow from the
    # per-chunk loop below (no chunking, no selection cascade, Steps 6/7/8 are
    # out of A1 scope and recorded as skipped), so it gets its own dedicated
    # path that still writes the same artifacts (journal, generation_outcomes,
    # selection_results with the v1 not_applicable schema, translations_raw,
    # translations, strict_chapter_trial_record).
    if cfg.whole_chapter:
        return _run_whole_chapter_strict(
            cfg=cfg, source=source, snapshot=snapshot, chunk_plan=chunk_plan,
            config=config, memory=memory, glossary=glossary, bible_text=bible_text,
            narrator_gender=narrator_gender, model_caller=model_caller,
            runtime=runtime, now_fn=now_fn, progress=progress_writer,
            usage_writer=usage_writer, started_at=started_at, wall_t0=wall_t0,
            b3_audit_repair=b3_audit_repair,
        )

    source_map = dict(source.source)
    risk_by_chunk = {
        pc.chunk_id: _risk_for_chunk(chunk=pc, source_map=source_map, glossary=glossary)
        for pc in chunk_plan.chunks
    }
    # V4 Efficiency A1.1 diagnostics: per-chunk kept/dropped glossary pairs
    # for the "dropped N pairs: [terms]" report (written at the end of the
    # run as glossary_budget_report.json; a diagnostic only — never read
    # back by the pipeline).
    glossary_budget_report: Dict[str, Dict[str, Any]] = {}

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
    # end). Load it before the loop so final_text_by_pid is seeded and
    # selected_text_by_chunk can be reconstructed from chunk_plan.pids ->
    # text, not re-derived from the journal (which deliberately does not
    # store translation text).
    #
    # B13: translations.json is now the chapter's FINAL translation
    # (repair + formatting + retry merged, owner decision 2026-08-05), so it
    # can no longer serve as the source for the ORIGINAL selected-candidate
    # text that Step 6/7 audit. The committed candidates' text lives in
    # generation_outcomes.json (identity-validated below); fall back to
    # prior_translations only for chunks without a persisted generation
    # record (e.g. a crash before generation_outcomes.json was written,
    # where the incremental translations.json still holds the un-repaired
    # candidates).
    prior_translations: Dict[str, str] = {}
    if prior_entries and translations_path_exists(cfg.out_dir):
        prior_translations = json.loads(
            (cfg.out_dir / "translations.json").read_text(encoding="utf-8")
        )
    final_text_by_pid.update(prior_translations)

    prior_generation_by_chunk: Dict[str, Dict[str, Any]] = {}
    if prior_entries:
        for _rec in _merge_generation_outcomes(
            cfg.out_dir, [], snapshot=snapshot, chunk_plan=chunk_plan,
            config=config,
        ):
            prior_generation_by_chunk[_rec["chunk_id"]] = _rec

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
            # B13: prefer the original selected candidate's committed text
            # from the persisted generation record — the exact text Step 6
            # audited in the previous session — so the assembled chapter and
            # the audit/repair cache keys are stable across resume. The
            # final translations.json (post-repair) must NOT feed the audit.
            chunk_text: Optional[Dict[str, str]] = None
            _gen = prior_generation_by_chunk.get(entry["chunk_id"])
            _selected_id = entry.get("selected_candidate_id")
            if _gen is not None and _selected_id:
                for _variant in _gen.get("candidates", {}).values():
                    if _variant.get("candidate_id") == _selected_id:
                        _translation = _variant.get("translation") or {}
                        chunk_text = {
                            pid: _translation[pid] for pid in plan_chunk.pids
                            if pid in _translation
                        }
                        break
            if chunk_text is None:
                chunk_text = {
                    pid: prior_translations[pid] for pid in plan_chunk.pids
                    if pid in prior_translations
                }
            selected_text_by_chunk[entry["chunk_id"]] = chunk_text
        elif outcome == "quarantined":
            quarantined_count += 1
        elif outcome == "needs_synthesis":
            needs_synthesis_count += 1
        elif outcome == "incomplete_generation":
            incomplete_generation_count += 1

    generation_params = GenerationParams(
        temperature=cfg.temperature, seed=cfg.seed, max_tokens=cfg.max_tokens,
        reasoning=cfg.reasoning,
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

                # V4 Efficiency A1.1: per-chunk glossary budget. The full
                # chapter glossary feeds the risk pre-screen above; the
                # *generation bundle* gets only the pairs relevant to this
                # chunk (term present in owned+left+right, or always_include
                # fail-closed). bundle_hash is derived by PromptBundle from
                # the filtered set, so a chunk whose glossary was not
                # filtered keeps its old hash — resume/cache are not
                # invalidated spuriously.
                chunk_glossary, dropped_glossary = _glossary_entries_for_chunk(
                    glossary,
                    chunk_text=" ".join(
                        [text for _, text in left_context]
                        + [text for _, text in right_context]
                        + [source_map[pid] for pid in plan_chunk.pids if pid in source_map]
                    ),
                    risk_feature_codes=(
                        feature.code for feature in risk.features
                    ),
                    narrator_gender=narrator_gender,
                    narrator_source_terms=narrator_source_terms,
                )
                glossary_budget_report[plan_chunk.chunk_id] = {
                    "kept": [entry.source_term for entry in chunk_glossary],
                    "dropped": list(dropped_glossary),
                    "dropped_count": len(dropped_glossary),
                }

                outcome = generate_for_chunk(
                    chunk_id=plan_chunk.chunk_id, risk=risk, source=source, snapshot=snapshot,
                    chunk_plan=chunk_plan, left_context=left_context, right_context=right_context,
                    glossary=chunk_glossary, style_constraints={}, bible_text=bible_text,
                    config=config, params=generation_params,
                    model_caller=model_caller, cache=gen_cache,
                    lazy_balanced=cfg.lazy_balanced,
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

                # ---- V4 Efficiency A2: lazy balanced-only fallback ----
                # The single balanced_literary candidate failed the
                # Qwen/deterministic gates. Lazily generate the fidelity_first
                # safety net and run the cascade on it alone: one passing
                # candidate → selected (Gemma is never invoked with a single
                # candidate), both failing → quarantined, exactly as the
                # pre-A2 2-candidate cascade would have. With
                # cfg.lazy_balanced=False this block never runs (legacy
                # 2-candidate + Gemma behavior, full rollback).
                if cfg.lazy_balanced and result.quarantine:
                    primary_quarantine_reason = result.quarantine_reason
                    lazy_outcome = generate_for_chunk(
                        chunk_id=plan_chunk.chunk_id, risk=risk, source=source, snapshot=snapshot,
                        chunk_plan=chunk_plan, left_context=left_context, right_context=right_context,
                        glossary=chunk_glossary, style_constraints={}, bible_text=bible_text,
                        config=config, params=generation_params,
                        model_caller=model_caller, cache=gen_cache,
                        roles=("fidelity_first",),
                    )
                    # RV A2 fix: the lazy fidelity outcome must join the
                    # chunk's PRIMARY generation record instead of being
                    # appended as a second record for the same chunk_id —
                    # downstream consumers key generation records by
                    # chunk_id (_merge_generation_outcomes, and Step 6's
                    # _audit_candidate_map builds gen_by_chunk the same way),
                    # so a last-wins merge would drop the primary balanced
                    # candidate and Step 6 could no longer pick a
                    # deterministic best-variant among the variants the chunk
                    # actually produced. Coalesce keeps both candidates (and
                    # both decision traces / errors) in one cumulative record
                    # without changing the final selected/quarantine outcome.
                    _coalesce_lazy_record_into_primary(
                        generation_records, plan_chunk.chunk_id, lazy_outcome,
                    )
                    if lazy_outcome.status == "complete":
                        candidates = list(lazy_outcome.candidates.values())
                        # The lazy fidelity candidate's own text must join the
                        # source-derived mixed-script allowlist, exactly as both
                        # candidates' texts did pre-A2 (the union was computed
                        # above from the balanced candidate alone). Without this
                        # a Latin token fidelity preserves (and balanced did
                        # not) — e.g. source initials — would be wrongly
                        # flagged by the deterministic gate and the lazy rescue
                        # would degrade a chunk the legacy cascade selected.
                        det_data_lazy = replace(
                            det_data_base,
                            mixed_script_allow=combine_script_tokens(
                                static_allow,
                                source_derived_allowlist(
                                    chunk_source_text,
                                    candidate_union_text + " " + " ".join(
                                        text for cand in candidates for _, text in cand.translation
                                    ),
                                ),
                            ),
                        )
                        try:
                            result = select_candidate(
                                chunk_id=plan_chunk.chunk_id, candidates=candidates, source=source,
                                qwen_evaluator=qwen_evaluator, det_data=det_data_lazy,
                                gemma_selector=gemma_selector,
                            )
                        except Exception as exc:  # noqa: BLE001 -- see primary select_candidate handling
                            LOG.exception(
                                "select_candidate raised on lazy fidelity for %s", plan_chunk.chunk_id
                            )
                            result = SelectionResult(
                                chunk_id=plan_chunk.chunk_id, quarantine=True,
                                quarantine_reason=f"lazy fidelity cascade raised: {exc!r}",
                                candidates_evaluated=len(candidates),
                            )
                        outcome = lazy_outcome
                        if result.quarantine:
                            # Both the primary balanced_literary and the lazy
                            # fidelity_first failed → quarantined; keep both
                            # reasons in the audit trail.
                            result = replace(
                                result,
                                quarantine_reason=(
                                    "balanced_literary failed the gates: "
                                    f"{primary_quarantine_reason} | lazy fidelity_first also failed: "
                                    f"{result.quarantine_reason}"
                                ),
                            )
                    else:
                        # Lazy generation itself failed validation (e.g.
                        # invalid JSON) → both attempts failed → quarantined.
                        outcome = lazy_outcome
                        candidates = []
                        result = SelectionResult(
                            chunk_id=plan_chunk.chunk_id, quarantine=True,
                            quarantine_reason=(
                                "balanced_literary failed the gates; lazy fidelity_first "
                                "generation incomplete: "
                                + ", ".join(
                                    f"{r}={e.detail}" for r, e in lazy_outcome.errors.items()
                                )
                            ),
                        )

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
    # V4.1 stop_after="generation" (renamed from "selection", A1): halt right
    # after Phase 1-2 (generation + per-chunk selection). The chunked
    # translation is already on disk (incremental translations.json writes);
    # Steps 6/7/8 (audit, repair, formatting) are intentionally skipped and
    # the record marks them as such. The shared finalization below still
    # writes every artifact a normal run writes (generation_outcomes.json,
    # selection_results.json, selection_meta.json, journal,
    # translations.json), with step6/step7/step8 set to the
    # "skipped_stop_after_generation" sentinel.
    if cfg.stop_after == "generation":
        step6 = {"status": "skipped_stop_after_generation"}
        step7 = {"status": "skipped_stop_after_generation"}
        step8 = {"status": "skipped_stop_after_generation"}
        halted_early = True
        halt_reason = "stop_after_generation"
        phase4_inputs = None
        repair_phase_result = None
    else:

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
        # B13: the repair phase result (or None when repair never ran / failed /
        # was skipped) decides the final translations.json write below.
        repair_phase_result: Optional[RepairPhaseResult] = None
        if repair_adapters is not None and phase4_inputs is not None:
            # Phase 5 formatting (B3, card C): build the formatting step over
            # the source blocks. Formatting is model-free by rule — there is
            # no injected caller, only the deterministic tiers (preserved /
            # exact / occurrence_aware / fuzzy). A span they cannot locate
            # becomes a blocking incident (debt), never a model call. Applied
            # between Step 7 convergence and Step 8 inside
            # run_repair_phase.
            #
            # ``cfg.formatting_required`` is the runtime master switch (§6.1
            # ``formatting.required=true``): when the policy says formatting
            # is not required, the step is skipped entirely.
            formatting_step = None
            if cfg.formatting_required:

                def _formatting_step(*, translation):
                    return run_formatting_align(
                        blocks=blocks,
                        translation=translation,
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
                        bible_text=bible_text,
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

        narrator_gender_findings: list = []
        if narrator_gender and final_text_by_pid:
            full_text = " ".join(final_text_by_pid.values())
            narrator_gender_findings = check_narrator_gender(full_text, narrator_gender)
        if narrator_gender_findings:
            # B7 invariant: ``integrity.status`` describes the detailed
            # check-level state (narrator_gender, formatting, mixed_script,
            # ...); ``step8.status`` is the chapter terminal state
            # (complete / accepted_degraded / failed / skipped). They are
            # NOT the same shape. narrator_gender failure is non-fatal at
            # chapter level (PID map is structurally valid), so step8 stays
            # in accepted_degraded; ``integrity.status == "failed"`` records
            # the specific finding for debugging/dashboards. A failed/skipped
            # step8 (from a prior failure) is never upgraded by a successful
            # narrator_gender check here — the check only downgrades an
            # existing complete to accepted_degraded.
            step8 = dict(step8)
            integrity = dict(step8.get("integrity") or {})
            integrity["narrator_gender"] = {
                "expected": narrator_gender,
                "mismatches": narrator_gender_findings,
            }
            if integrity.get("status") != "failed":
                integrity["status"] = "failed"
                integrity["reason"] = (
                    f"narrator_gender mismatch: expected {narrator_gender}, "
                    f"found {len(narrator_gender_findings)} mismatch(es)"
                )
            step8["integrity"] = integrity
            if step8.get("status") == "complete":
                step8["status"] = "accepted_degraded"

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

    # B13 (owner decision 2026-08-05): the single source of the chapter's
    # final translation is repair_report.final_translation — committed
    # repairs, formatting and healed quarantined-retry chunks would otherwise
    # be lost to book_run / B9, which read translations.json. When the
    # repair phase produced a result, translations.json is a full rewrite of
    # the authoritative on-disk final_translation (the retry cycle merges
    # retry winners into the report, which the in-memory frozen
    # RepairPhaseResult never reflects); the format stays {pid: text} and
    # HTML entities (&lt;em&gt; …) are normalized to clean tags so the
    # original's italics survive. Otherwise the historical fallback —
    # final_text_by_pid (the original selected candidates) — is written.
    # The incremental per-chunk writes above are untouched (resume safety).
    if repair_phase_result is not None and repair_phase_result.final_translation:
        final_translation = _load_repair_report_final_translation(cfg.out_dir)
        if final_translation is None:
            final_translation = dict(repair_phase_result.final_translation)
        final_translation = {
            pid: _normalize_final_markup(text)
            for pid, text in final_translation.items()
        }
    else:
        final_translation = final_text_by_pid
    _atomic_write_json(translations_path, final_translation)
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
    # V4 Efficiency A1.1 diagnostics: per-chunk glossary budget report
    # ("отброшено N пар: [термины]"). A diagnostic artifact only — the
    # pipeline never reads it back; the owner uses it to validate the
    # budget on a real run (card acceptance: dry-run report on run_005).
    # Rows cover the chunks processed in this session; resumed chunks
    # (replayed from the journal) were not re-budgeted, so a partial
    # resume MERGES the prior session's rows (after schema/policy/run-
    # identity validation, see _merged_glossary_budget_chunks) instead of
    # clobbering them. A full resume (every chunk replayed) writes
    # nothing, so the prior session's report is not clobbered by an empty
    # one — full-resume safety is preserved.
    if glossary_budget_report or resumed_from_index == 0:
        _atomic_write_json(cfg.out_dir / "glossary_budget_report.json", {
            "schema": GLOSSARY_BUDGET_SCHEMA,
            "policy_version": GLOSSARY_BUDGET_POLICY_VERSION,
            "chapter_id": cfg.chapter_id, "snapshot_hash": snapshot.snapshot_hash,
            "chunk_plan_hash": chunk_plan.plan_hash, "config_identity": config.config_identity,
            "glossary_total": len(glossary),
            "narrator_gender": narrator_gender,
            "chunks": _merged_glossary_budget_chunks(
                report_path=cfg.out_dir / "glossary_budget_report.json",
                schema=GLOSSARY_BUDGET_SCHEMA,
                policy_version=GLOSSARY_BUDGET_POLICY_VERSION,
                chapter_id=cfg.chapter_id,
                snapshot_hash=snapshot.snapshot_hash,
                chunk_plan_hash=chunk_plan.plan_hash,
                config_identity=config.config_identity,
                current_chunks=glossary_budget_report,
            ),
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
            # A2 review fix: the chapter_index (bible prompt) record that is
            # part of the snapshot identity — recorded so a verifiable hash
            # exists for audits/resume diagnosis.
            "chapter_index_hash": snapshot.chapter_index_hash,
        },
        "backend": backend_block,
        "runtime": {
            "local_lifecycle": local_lifecycle,
            "remote_calls": remote_calls,
        },
        "operational_policy": {
            "max_consecutive_terminal_nonselections": cfg.max_consecutive_terminal_nonselections,
            # V4.1: pinned-before-run generation reasoning budget and early-exit
            # policy — recorded so the owner can verify which experiment variant
            # produced a run without opening the config artifact.
            "reasoning": cfg.reasoning,
            "stop_after": cfg.stop_after,
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
    # D1 usage writes already happened per completed call via the backend
    # sink; only the writer handle needs closing.
    try:
        runtime.close()
    except Exception:  # noqa: BLE001
        LOG.exception("Failed to close runtime at end of run")
    progress_writer.close()
    usage_writer.close()

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


# ---------------------------------------------------------------------------
# V4.1 A1 whole-chapter mode: one generation call per chapter.
# ---------------------------------------------------------------------------

# Schema of the always-written whole-chapter selection artifact. Written even
# when no A/B selection exists, so resume/diagnostics/B9 never have to guess
# whether the artifact is missing or selection simply did not run.
WHOLE_CHAPTER_SELECTION_SCHEMA = "pact-v4-whole-chapter-selection/v1"

# V4.1 audit W (§14): schema of the whole-chapter ordered PID map artifact
# (whole_chapter_pid_map.json), the honest source of truth for the ordered
# PID list in whole-chapter mode. Each entry is {pid, order} in exact source
# order; the artifact header binds the map to the run's snapshot/source/
# plan/whole-chapter-map identities (snapshot_hash, source_hash,
# chunk_plan_hash, map_hash).
WHOLE_CHAPTER_PID_MAP_SCHEMA = "pact-v4-whole-chapter-pid-map/v1"

# Whole-chapter journal/count marker: the single journal entry's chunk_id and
# the generation record's chunk_id (the whole chapter is one processing unit).
WHOLE_CHAPTER_CHUNK_ID = "whole_chapter"

# The whole-chapter writer contract (V4.1 A1): exactly one candidate role.
# ``generate_whole_chapter`` is always invoked with this role, so a valid
# whole-chapter generation record must declare exactly this role in
# ``expected_roles`` and key its candidate/error maps to it.
WHOLE_CHAPTER_ROLE = "balanced_literary"


def _wc_generation_model(runtime: Any, cfg: StrictRunConfig) -> str:
    """Model label for ``wc_generation_started`` (diagnostics only).

    Prefers the backend descriptor's ``generator`` binding (the model that
    actually serves whole-chapter generation); falls back to the backend
    profile name, then to a stable ``<kind>:<hash>`` placeholder so the
    event never carries an empty model field.
    """
    try:
        bindings = runtime.backend_descriptor.model_bindings
        if bindings:
            name = bindings.get("generator") or bindings.get("default")
            if name:
                return str(name)
    except Exception:  # noqa: BLE001 — diagnostics, never a crash
        pass
    try:
        profile = cfg.backend.config_profile_name()
        if profile:
            return profile
    except Exception:  # noqa: BLE001
        pass
    return f"{cfg.backend.build_descriptor().kind}:{cfg.backend.identity_hash[:8]}"


def _wc_validation_flags(outcome: Any) -> Dict[str, bool]:
    """PID-contract validation flags for ``wc_validated`` (diagnostics).

    A completed whole-chapter outcome is by construction a fully validated
    ``{pid: text}`` map (json_ok/pids_ok/order_ok all True). For an
    incomplete outcome the flags reflect the LAST attempt's failure class:
    invalid JSON -> json_ok False; a PID-set/order violation -> pids_ok /
    order_ok False; a session abort -> nothing to validate (all False).
    """
    if outcome.status == "complete":
        return {"json_ok": True, "pids_ok": True, "order_ok": True}
    error = next(iter(outcome.errors.values()), None)
    code = error.code if error is not None else None
    if code == GenerationErrorCode.INVALID_JSON:
        return {"json_ok": False, "pids_ok": False, "order_ok": False}
    if code == GenerationErrorCode.PID_MISMATCH:
        return {"json_ok": True, "pids_ok": False, "order_ok": False}
    # SESSION_ABORT / CONTEXT_LEAKAGE / unknown: nothing validated.
    return {"json_ok": False, "pids_ok": False, "order_ok": False}


# V4.1 GEN-REASONING: schema of the compact per-attempt reasoning marker that
# rides inside the whole-chapter generation record (full text lives in the
# .txt files; the JSON carries only presence + char counts so the artifact
# stays small — owner decision 2026-08-13).
WHOLE_CHAPTER_REASONING_SCHEMA = "pact-v4-whole-chapter-reasoning/v1"


def _persist_whole_chapter_reasoning(
    out_dir: Path,
    reasoning_by_attempt: Mapping[int, str],
) -> Dict[str, Any]:
    """Write per-attempt reasoning text files and return the compact marker.

    For every attempt that produced reasoning text, the full text is written
    to ``whole_chapter_reasoning.txt`` (attempt 0) or
    ``whole_chapter_retry{N}_reasoning.txt`` (retry attempt N) — the same
    diagnostic pattern as the audit layer's ``b3_audit_chunkN_reasoning.txt``.
    The returned marker records, per attempt, only presence + char count
    (never the full text), so ``generation_outcomes.json`` stays compact.

    Reasoning is a diagnostics text artifact only: writing these files never
    affects ``whole_chapter_pid_map`` / ``wc_validated`` / cache / resume
    identity, and a write failure is a warning, not a gate.
    """
    attempts: Dict[str, Any] = {}
    for attempt, reasoning in sorted(reasoning_by_attempt.items()):
        attempts[str(attempt)] = {
            "present": bool(reasoning),
            "chars": len(reasoning),
        }
        if not reasoning:
            continue
        name = (
            "whole_chapter_reasoning.txt"
            if attempt == 0
            else f"whole_chapter_retry{attempt}_reasoning.txt"
        )
        try:
            (out_dir / name).write_text(reasoning, encoding="utf-8")
        except OSError as exc:
            LOG.warning(
                "whole-chapter reasoning artifact write failed (%s); "
                "reasoning is diagnostics-only, continuing",
                exc,
            )
    return {
        "schema": WHOLE_CHAPTER_REASONING_SCHEMA,
        "attempts": attempts,
    }


def _validate_whole_chapter_generation_record(
    rec: Dict[str, Any],
    *,
    outcome: str,
    selected_role: Optional[str],
    selected_candidate_id: Optional[str],
    raw_text_by_pid: Optional[Dict[str, str]],
) -> None:
    """Fail closed unless ``rec`` is a writer-produced whole-chapter record.

    RV3 (t_27de970d): resume previously accepted a sole ``whole_chapter``
    dict as the valid generation record as long as its ``chunk_id`` matched
    and — for a selected journal entry — ``candidates[selected_role]`` was a
    dict whose ``candidate_id`` equaled the journal's ``selected_candidate_id``.
    A record stripped of ``status``/``expected_roles`` and a candidate
    stripped of ``translation``/``role`` passed, was replayed, and selection/
    provenance were then rewritten from the damaged data. This validates the
    record against the exact schema ``_serialize_generation_outcome`` writes
    before any artifact is touched:

    * required record fields/types: ``chunk_id``, ``risk_band``,
      ``expected_roles``, ``status``, ``candidates``, ``errors``;
    * candidate fields used for provenance: ``candidate_id``, ``role``,
      ``translation`` (a non-empty ``{pid: text}`` map of strings) and the
      ``decision_trace`` audit trail;
    * error shape (``{code, detail}`` strings);
    * the exact whole-chapter role-set contract (RV4 t_86913123): the
      writer emits exactly one expected role, ``[balanced_literary]``, and
      keys its candidate/error maps exactly to that declared role set — a
      foreign/extra candidate role, an undeclared/duplicate/unknown expected
      role, or an error keyed to a foreign role could never have been
      produced by the writer and fails closed;
    * journal linkage: a ``selected`` outcome requires ``status ==
      "complete"``, ``selected_role in expected_roles``, a well-formed
      candidate for the role whose id/role match the journal's, and no error
      recorded for the selected role;
    * raw/provenance consistency: the selected candidate's serialized
      ``translation`` must equal the raw snapshot being replayed, so a
      damaged provenance can never seed a different translation;
    * an ``incomplete_generation`` outcome requires ``status ==
      "incomplete"`` with no candidates and a non-empty error map.

    Every violation raises a Data loss / provenance ``ValueError`` — the
    caller never reaches an artifact write.
    """
    if not isinstance(rec, dict):
        raise ValueError(
            "Data loss: whole_chapter generation record is not an object — "
            "refusing to resume against malformed provenance."
        )
    if rec.get("chunk_id") != WHOLE_CHAPTER_CHUNK_ID:
        raise ValueError(
            "Data loss: whole_chapter generation record has chunk_id "
            f"{rec.get('chunk_id')!r} — refusing to resume against "
            "malformed provenance."
        )
    risk_band = rec.get("risk_band")
    if not isinstance(risk_band, str) or not risk_band:
        raise ValueError(
            "Data loss: whole_chapter generation record has no valid "
            "risk_band — refusing to resume against malformed provenance."
        )
    expected_roles = rec.get("expected_roles")
    if (
        not isinstance(expected_roles, list)
        or not expected_roles
        or not all(isinstance(role, str) and role for role in expected_roles)
    ):
        raise ValueError(
            "Data loss: whole_chapter generation record has no valid "
            "expected_roles — refusing to resume against malformed "
            "provenance."
        )
    # RV4 (t_86913123): the whole-chapter writer emits exactly one expected
    # role. An extra/foreign/duplicate/unknown role in expected_roles could
    # never have been produced by the writer — refusing to replay selection/
    # provenance from a record whose declared role set is not the exact
    # writer shape.
    if expected_roles != [WHOLE_CHAPTER_ROLE]:
        raise ValueError(
            "Data loss: whole_chapter generation record expected_roles "
            f"{expected_roles!r} is not exactly [{WHOLE_CHAPTER_ROLE!r}] — "
            "refusing to resume against malformed provenance."
        )
    status = rec.get("status")
    if status not in ("complete", "incomplete"):
        raise ValueError(
            "Data loss: whole_chapter generation record has invalid status "
            f"{status!r} — refusing to resume against malformed provenance."
        )
    candidates = rec.get("candidates")
    if not isinstance(candidates, dict):
        raise ValueError(
            "Data loss: whole_chapter generation record has no valid "
            "candidates object — refusing to resume against malformed "
            "provenance."
        )
    errors = rec.get("errors")
    if not isinstance(errors, dict):
        raise ValueError(
            "Data loss: whole_chapter generation record has no valid "
            "errors object — refusing to resume against malformed "
            "provenance."
        )
    for role, cand in candidates.items():
        if not isinstance(cand, dict):
            raise ValueError(
                "Data loss: whole_chapter generation record candidate for "
                f"role {role!r} is not an object — refusing to resume "
                "against malformed provenance."
            )
        cand_id = cand.get("candidate_id")
        cand_role = cand.get("role")
        translation = cand.get("translation")
        trace = cand.get("decision_trace")
        if not isinstance(cand_id, str) or not cand_id:
            raise ValueError(
                "Data loss: whole_chapter generation record candidate for "
                f"role {role!r} has no valid candidate_id — refusing to "
                "resume against malformed provenance."
            )
        if not isinstance(cand_role, str) or not cand_role or cand_role != role:
            raise ValueError(
                "Data loss: whole_chapter generation record candidate for "
                f"role {role!r} has invalid role {cand_role!r} — refusing "
                "to resume against malformed provenance."
            )
        if (
            not isinstance(translation, dict)
            or not translation
            or not all(
                isinstance(pid, str) and isinstance(text, str)
                for pid, text in translation.items()
            )
        ):
            raise ValueError(
                "Data loss: whole_chapter generation record candidate for "
                f"role {role!r} has no valid translation PID map — "
                "refusing to resume against malformed provenance."
            )
        if (
            not isinstance(trace, list)
            or not trace
            or not all(
                isinstance(gate, dict)
                and isinstance(gate.get("gate"), str)
                and isinstance(gate.get("passed"), bool)
                and isinstance(gate.get("detail"), str)
                for gate in trace
            )
        ):
            raise ValueError(
                "Data loss: whole_chapter generation record candidate for "
                f"role {role!r} has no valid decision_trace — refusing to "
                "resume against malformed provenance."
            )
    for role, err in errors.items():
        if (
            not isinstance(err, dict)
            or not isinstance(err.get("code"), str)
            or not err.get("code")
            or not isinstance(err.get("detail"), str)
        ):
            raise ValueError(
                "Data loss: whole_chapter generation record error for role "
                f"{role!r} is malformed — refusing to resume against "
                "malformed provenance."
            )
    # RV4 (t_86913123): cross-field role-set consistency. The writer keys
    # candidates and errors EXACTLY by the declared expected role(s) — a
    # foreign/extra candidate role (e.g. a lazy-rescue fidelity_first
    # candidate) or an error keyed to an undeclared role could never have
    # been produced by the whole-chapter writer and must fail closed before
    # any artifact write.
    declared_roles = set(expected_roles)
    foreign_candidate_roles = set(candidates) - declared_roles
    if foreign_candidate_roles:
        raise ValueError(
            "Data loss: whole_chapter generation record candidate roles "
            f"{sorted(foreign_candidate_roles)!r} are not declared in "
            f"expected_roles {expected_roles!r} — refusing to resume "
            "against malformed provenance."
        )
    foreign_error_roles = set(errors) - declared_roles
    if foreign_error_roles:
        raise ValueError(
            "Data loss: whole_chapter generation record error roles "
            f"{sorted(foreign_error_roles)!r} are not declared in "
            f"expected_roles {expected_roles!r} — refusing to resume "
            "against malformed provenance."
        )
    if outcome == "selected":
        if status != "complete":
            raise ValueError(
                "Data loss: journal says whole_chapter generation was "
                f"selected but the generation record status is {status!r} — "
                "refusing to resume against malformed provenance."
            )
        if selected_role not in expected_roles:
            raise ValueError(
                "Data loss: whole_chapter generation record expected_roles "
                f"{expected_roles!r} does not include selected_role "
                f"{selected_role!r} — refusing to resume without journal "
                "linkage."
            )
        cand = candidates.get(selected_role)
        if not isinstance(cand, dict):
            raise ValueError(
                "Data loss: whole_chapter generation record has no "
                f"candidate for selected_role {selected_role!r} — refusing "
                "to resume without journal linkage."
            )
        if cand.get("candidate_id") != selected_candidate_id:
            raise ValueError(
                "Data loss: whole_chapter generation record candidate "
                f"{cand.get('candidate_id')!r} does not match the journal's "
                f"selected_candidate_id {selected_candidate_id!r}."
            )
        if selected_role in errors:
            raise ValueError(
                "Data loss: whole_chapter generation record records an "
                f"error for the selected role {selected_role!r} while "
                "claiming a complete outcome — refusing to resume against "
                "malformed provenance."
            )
        if raw_text_by_pid is not None and cand.get("translation") != dict(
            raw_text_by_pid
        ):
            raise ValueError(
                "Data loss: whole_chapter generation record candidate "
                "translation does not match the raw snapshot — refusing to "
                "resume against inconsistent provenance."
            )
    else:  # incomplete_generation
        if status != "incomplete":
            raise ValueError(
                "Data loss: journal says whole_chapter generation was "
                f"incomplete but the generation record status is {status!r} "
                "-- refusing to resume against malformed provenance."
            )
        if candidates:
            raise ValueError(
                "Data loss: whole_chapter generation record claims "
                "incomplete generation but carries candidates — refusing to "
                "resume against malformed provenance."
            )
        if not errors:
            raise ValueError(
                "Data loss: whole_chapter generation record claims "
                "incomplete generation but has no errors — refusing to "
                "resume against malformed provenance."
            )


def _run_whole_chapter_strict(
    cfg: StrictRunConfig,
    *,
    source: Any,
    snapshot: Any,
    chunk_plan: ChunkPlanArtifact,
    config: ConfigArtifact,
    memory: Any,
    glossary: Any,
    bible_text: str,
    narrator_gender: Optional[str],
    model_caller: Any,
    runtime: Any,
    now_fn: Any,
    progress: Any,
    usage_writer: Any,
    started_at: str,
    wall_t0: float,
    b3_audit_repair: Optional[Any] = None,
) -> StrictChapterRunResult:
    """V4.1 A1 whole-chapter generation: one call per chapter.

    V4.1 A2 review fix (RV, commit 4ab250b): resource cleanup (runtime /
    progress writer / usage writer) is guaranteed on EVERY exit path. The
    fail-closed resume validation (data-loss / foreign-identity ValueErrors
    raised after ``progress.run_started``) used to bypass the successful-tail
    cleanup and leak runtime/progress/usage resources; the impl now runs
    inside ``try/finally`` so cleanup always happens while the fail-closed
    error propagates unchanged.

    The generation/provenance contract itself lives in
    ``_run_whole_chapter_strict_impl``: one generation call per chapter with
    the strict ``{pid: text}`` JSON contract and bounded retry
    (``generate_whole_chapter``), plus the whole-chapter provenance contract
    (journal, ``translations_raw.json``, ``translations.json``,
    ``selection_results.json``, ``generation_outcomes.json``,
    ``strict_chapter_trial_record.json``); Steps 6/7/8 are out of A1 scope and
    recorded as skipped.
    """
    try:
        return _run_whole_chapter_strict_impl(
            cfg=cfg, source=source, snapshot=snapshot, chunk_plan=chunk_plan,
            config=config, memory=memory, glossary=glossary, bible_text=bible_text,
            narrator_gender=narrator_gender, model_caller=model_caller,
            runtime=runtime, now_fn=now_fn, progress=progress,
            usage_writer=usage_writer, started_at=started_at, wall_t0=wall_t0,
            b3_audit_repair=b3_audit_repair,
        )
    finally:
        # Terminal teardown on EVERY path (success, resume-validation
        # failure, unexpected error): closes the remote backend / stops a
        # managed server, releases the local router, closes the progress and
        # usage writers. close() is idempotent (writers null their handle,
        # coordinators guard on _closed), so running it here AND at the
        # successful tail is safe. _close_run_resources guards each close so
        # a cleanup error is logged and never masks the original exception.
        _close_run_resources(runtime, progress, usage_writer)


def _run_whole_chapter_strict_impl(
    cfg: StrictRunConfig,
    *,
    source: Any,
    snapshot: Any,
    chunk_plan: ChunkPlanArtifact,
    config: ConfigArtifact,
    memory: Any,
    glossary: Any,
    bible_text: str,
    narrator_gender: Optional[str],
    model_caller: Any,
    runtime: Any,
    now_fn: Any,
    progress: Any,
    usage_writer: Any,
    started_at: str,
    wall_t0: float,
    b3_audit_repair: Optional[Any] = None,
) -> StrictChapterRunResult:
    """Whole-chapter generation/provenance body (see the wrapper above).

    Derives the full ordered PID map (``WholeChapterPidMap``) from the
    authoritative multi-chunk ``ChunkPlanArtifact``, generates the whole
    chapter in a single call with the strict ``{pid: text}`` JSON contract
    and bounded retry (``generate_whole_chapter``), and writes the
    whole-chapter provenance contract. Steps 6/7/8 are out of A1 scope and
    recorded as skipped.
    """
    pid_map = WholeChapterPidMap.derive(chunk_plan, snapshot)
    pid_map_path = cfg.out_dir / "whole_chapter_pid_map.json"
    pid_map_path.write_text(json.dumps({
        "schema": WHOLE_CHAPTER_PID_MAP_SCHEMA,
        "chapter_id": cfg.chapter_id,
        "snapshot_hash": snapshot.snapshot_hash,
        "source_hash": source.source_hash,
        "chunk_plan_hash": chunk_plan.plan_hash,
        "map_hash": pid_map.map_hash,
        "pid_count": len(pid_map.pids),
        "entries": [
            {"pid": pid, "order": order}
            for order, pid in enumerate(pid_map.pids)
        ],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    journal_path = cfg.out_dir / "journal.ndjson"
    translations_path = cfg.out_dir / "translations.json"
    raw_translations_path = cfg.out_dir / "translations_raw.json"
    selection_path = cfg.out_dir / "selection_results.json"
    generation_path = _generation_outcomes_path(cfg.out_dir)
    record_path = cfg.out_dir / "strict_chapter_trial_record.json"

    # ------------------------------------------------------------------
    # Resume: replay the single whole-chapter journal entry and verify
    # identities exactly like the chunked path.
    # ------------------------------------------------------------------
    prior_entries = _load_journal(journal_path)
    acceptable_backend_hashes = list(cfg.backend.acceptable_identity_hashes())
    for entry in prior_entries:
        if not isinstance(entry, dict):
            raise ValueError(
                "Data loss: malformed whole-chapter journal entry — "
                f"expected a JSON object, found {type(entry).__name__} — "
                "refusing to resume against a corrupt journal."
            )
        if (
            entry.get("snapshot_hash") != snapshot.snapshot_hash
            or entry.get("chunk_plan_hash") != chunk_plan.plan_hash
            or entry.get("config_identity") != config.config_identity
            or entry.get("backend_identity_hash") not in acceptable_backend_hashes
        ):
            raise ValueError(
                "Foreign identity: journal entry for "
                f"{entry.get('chunk_id')} was written under a different "
                "snapshot/plan/config than this run -- refusing to resume "
                "against a stale journal."
            )
    resumed_from_index = len(prior_entries)

    final_text_by_pid: Dict[str, str] = {}
    selected_role_counts: Dict[str, int] = {}
    incomplete_generation_count = 0
    generation_records: List[Dict[str, Any]] = []
    halted_early = False
    halt_reason: Optional[str] = None
    # V4.1 A2 (§5.3): whole-chapter glossary budget diagnostic row; filled
    # on a fresh run, left empty on resume (a resumed run re-budgets
    # nothing and must not clobber the prior session's report).
    glossary_budget_report_whole: Dict[str, Any] = {
        "kept": [], "dropped": [], "dropped_count": 0,
    }

    progress.run_started(
        chapter_id=cfg.chapter_id,
        out_dir=cfg.out_dir,
        started_at=started_at,
        backend_identity_hash=cfg.backend.identity_hash,
        resumed_from_index=resumed_from_index,
    )

    if resumed_from_index > 0:
        # Whole-chapter resume journal contract: exactly ONE whole_chapter
        # entry. A duplicate or malformed journal is a data-integrity failure
        # and must fail closed — never silently replayed past via
        # prior_entries[0] with authoritative counts/provenance untrustworthy.
        if len(prior_entries) != 1:
            raise ValueError(
                "Data loss: whole-chapter resume journal must contain "
                f"exactly one entry, found {len(prior_entries)} — refusing "
                "to resume against a duplicate or corrupt journal."
            )
        entry = prior_entries[0]
        if entry.get("chunk_id") != WHOLE_CHAPTER_CHUNK_ID:
            raise ValueError(
                "Data loss: malformed whole-chapter journal entry — expected "
                f"chunk_id {WHOLE_CHAPTER_CHUNK_ID!r}, found "
                f"{entry.get('chunk_id')!r} — refusing to resume."
            )
        outcome = entry.get("outcome")
        if outcome == "selected":
            selected_candidate_id = entry.get("selected_candidate_id")
            selected_role = entry.get("selected_role")
            if not isinstance(selected_candidate_id, str) or not selected_candidate_id:
                raise ValueError(
                    "Data loss: malformed whole-chapter journal entry — "
                    "selected outcome without a selected_candidate_id."
                )
            if not isinstance(selected_role, str) or not selected_role:
                raise ValueError(
                    "Data loss: malformed whole-chapter journal entry — "
                    "selected outcome without a selected_role."
                )
            if entry.get("candidate_ids") != [selected_candidate_id]:
                raise ValueError(
                    "Data loss: malformed whole-chapter journal entry — "
                    "candidate_ids must be exactly [selected_candidate_id]."
                )
        elif outcome == "incomplete_generation":
            selected_candidate_id = None
            selected_role = None
            if entry.get("candidate_ids") not in (None, []):
                raise ValueError(
                    "Data loss: malformed whole-chapter journal entry — "
                    "incomplete_generation outcome with non-empty candidate_ids."
                )
            if (
                entry.get("selected_candidate_id") is not None
                or entry.get("selected_role") is not None
            ):
                raise ValueError(
                    "Data loss: malformed whole-chapter journal entry — "
                    "incomplete_generation outcome must not carry a "
                    "selected_candidate_id/selected_role."
                )
        else:
            raise ValueError(
                "Data loss: malformed whole-chapter journal entry — invalid "
                f"outcome {outcome!r}."
            )

        if outcome == "selected":
            # Resume distinguishes the RAW generator snapshot (the exact text
            # the generation contract produced) from the FINAL translations.json
            # alias (which after A2/B repair may diverge). Only the raw file may
            # seed a resumed whole-chapter run.
            if not raw_translations_path.exists():
                raise ValueError(
                    "Data loss: journal says whole_chapter generation was "
                    f"selected but {raw_translations_path.name} is missing — "
                    "the raw generator snapshot cannot be reconstructed."
                )
            # The raw snapshot must conform to the exact strict {pid: text}
            # contract the generator enforces (full PID set, exact source
            # order, string values, no duplicate keys): a damaged or partial
            # raw file is data loss, never a resume candidate. Fails closed
            # with the same failure taxonomy as a generation attempt
            # (ValueError for invalid JSON, _GenerationValidationError for
            # PID contract violations) — a partial raw can never seed a
            # partial final translations.json.
            try:
                final_text_by_pid = dict(
                    validate_whole_chapter_raw(
                        raw_translations_path.read_text(encoding="utf-8"),
                        pid_map,
                    )
                )
            except (ValueError, _GenerationValidationError) as exc:
                raise ValueError(
                    "Data loss: journal says whole_chapter generation was "
                    f"selected but {raw_translations_path.name} does not "
                    f"conform to the whole-chapter strict {{pid: text}} "
                    f"contract ({exc}) — refusing to resume against a corrupt "
                    "or partial raw snapshot."
                ) from exc
            selected_role_counts[selected_role] = 1
        elif outcome == "incomplete_generation":
            incomplete_generation_count = 1
            halted_early = True
            halt_reason = (
                "whole_chapter generation incomplete (resumed; bounded retry "
                "budget was exhausted in the prior session)"
            )

        # Generation provenance is mandatory for any whole-chapter resume:
        # the journal entry may only be replayed when generation_outcomes.json
        # exists, carries this run's identities, and contains exactly one
        # valid whole_chapter record. Missing/empty/mismatched provenance is
        # data loss — never a reason to silently rewrite empty provenance and
        # selection_results.json with candidate_count=0 while the journal and
        # translations claim a selected whole-chapter generation.
        if not generation_path.exists():
            raise ValueError(
                "Data loss: journal says whole_chapter generation was "
                f"{outcome} but {generation_path.name} is missing — the "
                "generation record cannot be reconstructed."
            )
        try:
            payload = json.loads(generation_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ValueError(
                "Data loss: journal says whole_chapter generation was "
                f"{outcome} but {generation_path.name} is corrupt JSON — "
                "refusing to resume against a broken provenance artifact."
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError(
                "Data loss: journal says whole_chapter generation was "
                f"{outcome} but {generation_path.name} is not a JSON object."
            )
        if not all(
            isinstance(payload.get(key), str) and payload.get(key)
            for key in ("snapshot_hash", "chunk_plan_hash", "config_identity")
        ):
            raise ValueError(
                "Data loss: journal says whole_chapter generation was "
                f"{outcome} but {generation_path.name} is missing the run "
                "identity fields (empty or malformed provenance artifact)."
            )
        if (
            payload.get("snapshot_hash") != snapshot.snapshot_hash
            or payload.get("chunk_plan_hash") != chunk_plan.plan_hash
            or payload.get("config_identity") != config.config_identity
        ):
            raise ValueError(
                "Foreign identity: generation_outcomes.json was written "
                "under a different snapshot/plan/config than this run -- "
                "refusing to mix generation records across runs."
            )
        outcomes = payload.get("outcomes")
        if not isinstance(outcomes, list):
            raise ValueError(
                "Data loss: journal says whole_chapter generation was "
                f"{outcome} but {generation_path.name} has no outcomes array."
            )
        # RV3: every entry in outcomes must itself be a well-formed
        # whole_chapter record object — the whole-chapter writer emits
        # exactly one whole_chapter record and nothing else, so a malformed
        # or foreign entry must fail closed rather than be silently ignored
        # while a sole whole_chapter dict is accepted as the valid record.
        if not all(
            isinstance(rec, dict) and rec.get("chunk_id") == WHOLE_CHAPTER_CHUNK_ID
            for rec in outcomes
        ):
            raise ValueError(
                "Data loss: journal says whole_chapter generation was "
                f"{outcome} but {generation_path.name} contains a malformed "
                "or foreign generation record — refusing to resume against "
                "damaged provenance."
            )
        whole_records = list(outcomes)
        if len(whole_records) != 1:
            raise ValueError(
                "Data loss: journal says whole_chapter generation was "
                f"{outcome} but {generation_path.name} must contain exactly "
                f"one whole_chapter record, found {len(whole_records)}."
            )
        # RV3: the sole whole_chapter record must conform to the writer's
        # serialized GenerationOutcome schema AND link to the journal's
        # selected candidate/role. A record stripped of its required fields
        # (status/expected_roles/candidates/errors) or a linked candidate
        # stripped of its provenance fields (translation/role/candidate_id)
        # is damaged provenance — failing closed here prevents selection/
        # provenance from being rewritten from data the writer could never
        # have produced. The selected candidate's serialized translation must
        # also equal the raw snapshot being replayed (raw/provenance
        # consistency), so a damaged provenance can never seed a different
        # translation than the journal selected.
        _validate_whole_chapter_generation_record(
            whole_records[0],
            outcome=outcome,
            selected_role=selected_role,
            selected_candidate_id=selected_candidate_id,
            raw_text_by_pid=final_text_by_pid if outcome == "selected" else None,
        )
        generation_records = [whole_records[0]]
    else:
        # ------------------------------------------------------------------
        # Fresh run: one whole-chapter generation call with bounded retry.
        # ------------------------------------------------------------------
        params = GenerationParams(
            temperature=cfg.temperature, seed=cfg.seed,
            max_tokens=cfg.max_tokens, reasoning=cfg.reasoning,
        )
        # V4.1 A2 (§5.3): the glossary is filtered with the text of the
        # WHOLE chapter (not per chunk) — only the chapter's terms + the
        # always_include set (risk categories / conflicts / narrator)
        # reach the prompt. Locked-policy variant (a): every established
        # glossary entry is authoritative (presence + always_include); no
        # separate locked artifact is introduced.
        whole_chapter_text = " ".join(text for _, text in source.source)
        whole_risk = _whole_chapter_risk(source, glossary)
        chapter_glossary, chapter_dropped = _glossary_entries_for_chunk(
            glossary,
            chunk_text=whole_chapter_text,
            risk_feature_codes=(feature.code for feature in whole_risk.features),
            narrator_gender=narrator_gender,
            narrator_source_terms=_narrator_glossary_terms(memory.book_memory),
        )
        glossary_budget_report_whole: Dict[str, Any] = {
            "kept": [entry.source_term for entry in chapter_glossary],
            "dropped": list(chapter_dropped),
            "dropped_count": len(chapter_dropped),
        }

        # V4.1 SAFE-MEMORY (owner decision 2026-08-14, P0): the source-only
        # entity prepass (B1.2) runs BEFORE translation — verified claims
        # reach the generation prompt as a CHAPTER ENTITY FACTS block
        # (candidate claims go ONLY to the audit; they are semantic
        # hypotheses, never prompt commands). The cache is persisted to
        # entity_context_cache.json so the B3 audit/repair stage later hits
        # the SAME cache with 0 extra model calls. The gate mirrors the B3
        # invocation below (run_audit + stop_after + machinery present +
        # entity context enabled), so a run that would not audit also does
        # not pay the prepass; without the machinery generation proceeds
        # without the entity block.
        entity_gen_block = ""
        if (
            cfg.entity_context_enabled
            and cfg.run_audit
            and cfg.stop_after != "generation"
            and b3_audit_repair is not None
        ):
            extraction = b3_audit_repair.entity_context_prepass(
                source=source, out_dir=cfg.out_dir,
            )
            if extraction is not None:
                entity_gen_block = render_entity_context_block(
                    extraction.context, verified_only=True,
                )
        # The generation prompt carries the verified entity facts as part of
        # the book-context block (deterministic, source-derived).
        gen_bible_text = bible_text
        if entity_gen_block:
            gen_bible_text = (
                f"{bible_text}\nCHAPTER ENTITY FACTS - SOURCE-DERIVED\n"
                f"{entity_gen_block}"
            )
        # P1 АРКИ (owner decision 2026-08-14): deterministic arc-name block
        # from arc_names.json so chapter headings translate consistently
        # (Bonds = Узы in every chapter). Part of the bundle identity via
        # bible_text — a changed mapping invalidates the generation cache.
        if cfg.deterministic_arc_names:
            arcs_block = "\n".join(
                f"- {en} → {ru}" for en, ru in cfg.deterministic_arc_names
            )
            gen_bible_text = f"{gen_bible_text}\nАРКИ:\n{arcs_block}"

        events_before = runtime.event_count()
        progress.chunk_started(chunk_id=WHOLE_CHAPTER_CHUNK_ID)
        # V4.1 M (monitor card): whole-chapter generation telemetry — the
        # phase-progress CLI renders "GEN attempt N/M (reason)" live from
        # these events. Diagnostics only: a write failure is swallowed by
        # the writer and never affects generation.
        wc_retry_policy = WholeChapterRetryPolicy()
        progress.wc_generation_started(
            pid_count=len(pid_map.pids),
            reasoning_budget=cfg.reasoning,
            model=_wc_generation_model(runtime, cfg),
            max_attempts=wc_retry_policy.max_attempts,
        )
        wc_t0 = time.monotonic()
        # V4.1 GEN-REASONING: per-attempt reasoning text collector for the
        # whole-chapter generation call. Reasoning is a diagnostics TEXT
        # artifact only — it never enters cache/resume identity (the record
        # below carries only presence/char-count markers; the full text lives
        # in whole_chapter_reasoning.txt / whole_chapter_retryN_reasoning.txt).
        reasoning_by_attempt: Dict[int, str] = {}

        def _wc_reasoning_sink(attempt: int, reasoning: str) -> None:
            reasoning_by_attempt[attempt] = reasoning

        # RAW-SINK (architect, run_remote_004/005): persist the raw model
        # response of EVERY whole-chapter attempt to a disk file. The run_011
        # lesson (raw on every attempt) previously covered R/audit/repair but
        # NOT generation — a TruncatedJSONError left no text trail and the
        # diagnosis (fences? pid-colon? provider budget?) was guesswork. Each
        # attempt writes whole_chapter_attempt{N}_raw.txt (attempt 0 ->
        # whole_chapter_attempt0_raw.txt). Best-effort: a write failure never
        # changes generation behavior.
        def _wc_raw_sink(attempt: int, raw: str) -> None:
            try:
                name = f"whole_chapter_attempt{attempt}_raw.txt"
                (cfg.out_dir / name).write_text(raw, encoding="utf-8")
            except Exception:  # noqa: BLE001 — diagnostics hook
                LOG.debug("whole-chapter raw_sink write failed", exc_info=True)

        # V4.1 GEN-STREAM: per-attempt live reasoning writer. The file is
        # created BEFORE the model call (open_reasoning_writer truncates and
        # returns an appender) and grows live while the model generates
        # (local llama-server streams reasoning_content; the OpenCode
        # transport delivers once after completion — both through the same
        # sink, the documented fallback). Gated on cfg.reasoning>0 so a
        # reasoning=0 run writes no file; the authoritative post-completion
        # write (_persist_whole_chapter_reasoning) then overwrites the same
        # path with the full text — the live writer is a diagnostics bonus,
        # never the source of truth, and the final flush is authoritative.
        def _wc_live_reasoning_writer(
            attempt: int,
        ) -> Optional[Callable[[str], None]]:
            if cfg.reasoning <= 0:
                return None
            name = (
                "whole_chapter_reasoning.txt"
                if attempt == 0
                else f"whole_chapter_retry{attempt}_reasoning.txt"
            )
            return open_reasoning_writer(cfg.out_dir / name)

        outcome = generate_whole_chapter(
            role="balanced_literary",
            source=source,
            snapshot=snapshot,
            chunk_plan=chunk_plan,
            pid_map=pid_map,
            glossary=chapter_glossary,
            bible_text=gen_bible_text,
            config=config,
            params=params,
            model_caller=model_caller,
            cache=GenerationCache(),
            retry=wc_retry_policy,
            on_retry=lambda attempt, reason: progress.wc_retry_attempt(
                attempt=attempt, reason=reason
            ),
            reasoning_sink=_wc_reasoning_sink,
            live_reasoning_writer=_wc_live_reasoning_writer,
            raw_sink=_wc_raw_sink,
        )
        progress.wc_generation_done(
            finish_reason="complete" if outcome.status == "complete" else "incomplete",
            pid_count=len(pid_map.pids),
            duration=time.monotonic() - wc_t0,
        )
        progress.wc_validated(**_wc_validation_flags(outcome))
        generation_record = _serialize_generation_outcome(outcome)
        if any(reasoning_by_attempt.values()):
            # GEN-REASONING: persist the full reasoning text per attempt and
            # carry a compact presence/char-count marker in the record so the
            # artifact stays small (the spec's decision: full text in the
            # .txt files, JSON gets length/presence only). When NO attempt
            # produced reasoning (reasoning=0 / transport reported none) the
            # record stays byte-identical to the pre-GEN-REASONING shape.
            reasoning_marker = _persist_whole_chapter_reasoning(
                cfg.out_dir, reasoning_by_attempt
            )
            generation_record["reasoning"] = reasoning_marker
        # GEN-STREAM cleanup: the live writer truncated the per-attempt
        # files BEFORE the calls (open_reasoning_writer), but an attempt
        # that ended up with NO reasoning (reasoning=0, a transport that
        # reported none, or a failed attempt superseded by a retry) leaves
        # an EMPTY live file behind. Remove those so the F8 manifest never
        # advertises an artifact that carries no reasoning text. A file that
        # already holds streamed content is kept (it is real data).
        for attempt, reasoning in reasoning_by_attempt.items():
            if reasoning:
                continue
            name = (
                "whole_chapter_reasoning.txt"
                if attempt == 0
                else f"whole_chapter_retry{attempt}_reasoning.txt"
            )
            try:
                path = cfg.out_dir / name
                if path.exists() and path.stat().st_size == 0:
                    path.unlink()
            except OSError as exc:
                LOG.warning(
                    "whole-chapter reasoning live-file cleanup failed: %s", exc
                )
        generation_records.append(generation_record)

        if outcome.status == "complete":
            candidate = outcome.candidates["balanced_literary"]
            final_text_by_pid = dict(candidate.as_pid_map())
            selected_role_counts["balanced_literary"] = 1
            # Raw snapshot: the validated generator output, BEFORE QA/repair.
            # Resume reads this file (never translations.json), so the raw
            # generator contract survives even after later stages diverge it
            # from the final alias.
            _atomic_write_json(raw_translations_path, final_text_by_pid)
            journal_outcome = "selected"
            selected_candidate_id = candidate.candidate_id
            candidate_ids = [candidate.candidate_id]
        else:
            incomplete_generation_count = 1
            halted_early = True
            halt_reason = (
                "whole_chapter generation incomplete: "
                + ", ".join(
                    f"{role}={err.detail}" for role, err in outcome.errors.items()
                )
            )
            journal_outcome = "incomplete_generation"
            selected_candidate_id = None
            candidate_ids = []

        entry = JournalEntry(
            chunk_index=0,
            chunk_id=WHOLE_CHAPTER_CHUNK_ID,
            parent_chunk_id=None,
            parent_context_state_hash=_left_context_hash(()),
            left_context_kind=WHOLE_CHAPTER_CHUNK_ID,
            left_context_hash=_left_context_hash(()),
            snapshot_hash=snapshot.snapshot_hash,
            chunk_plan_hash=chunk_plan.plan_hash,
            config_identity=config.config_identity,
            backend_identity_hash=cfg.backend.identity_hash,
            candidate_ids=candidate_ids,
            gate_trace=[],
            outcome=journal_outcome,
            selected_candidate_id=selected_candidate_id,
            selected_role=("balanced_literary" if journal_outcome == "selected" else None),
            switch_indices=runtime.local_switch_event_indices(events_before),
            backend_event_indices=list(range(events_before, runtime.event_count())),
        )
        with open(journal_path, "a", encoding="utf-8") as journal_file:
            journal_file.write(json.dumps(entry.to_json(), ensure_ascii=False) + "\n")
            journal_file.flush()
        progress.chunk_done(chunk_id=WHOLE_CHAPTER_CHUNK_ID, outcome=journal_outcome)

    # ------------------------------------------------------------------
    # Provenance artifacts (always written, same lifecycle as the chunked run).
    # ------------------------------------------------------------------
    generation_path.write_text(json.dumps({
        "chapter_id": cfg.chapter_id, "snapshot_hash": snapshot.snapshot_hash,
        "chunk_plan_hash": chunk_plan.plan_hash,
        "config_identity": config.config_identity,
        "outcomes": generation_records,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    # V4.1 B3 (concept §10 B3, §9.4): production audit/repair after
    # whole-chapter generation. Runs when ``cfg.run_audit`` AND the B3
    # machinery is injected; the repaired map becomes the final
    # translations.json alias and the raw->repaired diff stage becomes
    # real. Without the injected machinery the steps stay recorded as
    # skipped (A1 behavior) — the runner never fabricates an audit.
    # A B3 failure is recorded (step6/7/8 status "failed"), never a
    # crash of the completed generation run.
    # ------------------------------------------------------------------
    raw_final_text_by_pid = dict(final_text_by_pid)
    b3_audit_result: Optional[Any] = None
    b3_failed: Optional[str] = None
    if (
        cfg.run_audit
        and cfg.stop_after != "generation"
        and b3_audit_repair is not None
        and raw_final_text_by_pid
    ):
        try:
            b3_audit_result = b3_audit_repair.run(
                chapter_id=cfg.chapter_id,
                source=source,
                snapshot_hash=snapshot.snapshot_hash,
                translation=raw_final_text_by_pid,
                book_memory=memory.book_memory,
                out_dir=cfg.out_dir,
                config_identity=config.config_identity,
                backend_identity_hash=cfg.backend.identity_hash,
            )
        except Exception as exc:  # noqa: BLE001 — a B3 failure is a record, not a crash
            LOG.exception("B3 audit/repair failed for %s", cfg.chapter_id)
            b3_failed = str(exc)
        if b3_audit_result is not None:
            final_text_by_pid = dict(b3_audit_result.translations_repaired)

    # Final alias. In A1 there is no repair/formatting, so the final chapter
    # equals the raw generator snapshot; the two files remain distinct so a
    # later A2/B stage can diverge them without losing the raw contract.
    _atomic_write_json(translations_path, final_text_by_pid)

    # V4.1 A2 (§7): intermediate snapshots + diff report, written
    # atomically (write-then-rename) with identity in every snapshot.
    # `translations.json` remains the FINAL alias — these files are
    # attribution snapshots, never a competing source of truth. In A2 the
    # whole-chapter flow has no repair/formatting yet (B/B2/C), so
    # repaired == raw and the diff stages are empty; B3 makes the
    # raw->repaired stage real when the production audit/repair ran.
    if final_text_by_pid:
        repaired_path = cfg.out_dir / "translations_repaired.json"
        diffs_path = cfg.out_dir / "translation_diffs.json"
        snapshot_identity = {
            "schema": "pact-v4-snapshot-translations-repaired/v1",
            "chapter_id": cfg.chapter_id,
            "snapshot_hash": snapshot.snapshot_hash,
            "chunk_plan_hash": chunk_plan.plan_hash,
            "config_identity": config.config_identity,
        }
        _atomic_write_json(
            repaired_path,
            {**snapshot_identity, "translations": dict(final_text_by_pid)},
        )
        _atomic_write_json(
            diffs_path,
            {
                "schema": "pact-v4-translation-diffs/v1",
                "chapter_id": cfg.chapter_id,
                "snapshot_hash": snapshot.snapshot_hash,
                "chunk_plan_hash": chunk_plan.plan_hash,
                "config_identity": config.config_identity,
                "diffs": {
                    "raw->repaired": _pid_diffs(
                        raw_final_text_by_pid, final_text_by_pid
                    ),
                    "repaired->final": _pid_diffs(
                        final_text_by_pid, final_text_by_pid
                    ),
                },
            },
        )

    # V4.1 A2 (§5.3): whole-chapter glossary budget diagnostic — kept/dropped
    # pairs for the full chapter (same A1.1 diagnostic shape, one row).
    if resumed_from_index == 0:
        _atomic_write_json(cfg.out_dir / "glossary_budget_report.json", {
            "schema": GLOSSARY_BUDGET_SCHEMA,
            "policy_version": GLOSSARY_BUDGET_POLICY_VERSION,
            "chapter_id": cfg.chapter_id, "snapshot_hash": snapshot.snapshot_hash,
            "chunk_plan_hash": chunk_plan.plan_hash, "config_identity": config.config_identity,
            "glossary_total": len(glossary),
            "narrator_gender": narrator_gender,
            "chunks": {"whole_chapter": glossary_budget_report_whole},
        })

    generation_record_id = None
    for rec in generation_records:
        for _role, cand in (rec.get("candidates") or {}).items():
            generation_record_id = cand.get("candidate_id")
            break
        if generation_record_id:
            break
    selection_path.write_text(json.dumps({
        "schema": WHOLE_CHAPTER_SELECTION_SCHEMA,
        "chapter_id": cfg.chapter_id, "snapshot_hash": snapshot.snapshot_hash,
        "chunk_plan_hash": chunk_plan.plan_hash,
        "config_identity": config.config_identity,
        "mode": "not_applicable",
        "candidate_count": 1 if generation_record_id else 0,
        "selection_performed": False,
        "coverage": "full_pid_map",
        "generation_record_id": generation_record_id,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    if b3_audit_result is not None:
        step6 = b3_audit_result.step6
        step7 = b3_audit_result.step7
        step8 = b3_audit_result.step8
    elif b3_failed is not None:
        # The B3 machinery was configured but crashed — recorded honestly
        # as failed, never silently downgraded to "skipped".
        step6 = {"status": "failed", "error": b3_failed}
        step7 = {"status": "failed", "error": b3_failed}
        step8 = {"status": "failed", "error": b3_failed}
    else:
        # No B3 machinery was injected (or run_audit=False / generation
        # incomplete) — the steps are recorded as skipped (A1 behavior),
        # never fabricated as complete. Without machinery the run IS
        # generation-only, so the A1 reason stays accurate even when the
        # run_audit flag is on.
        step6 = {"status": "skipped", "reason": "whole_chapter_generation_only"}
        step7 = {"status": "skipped", "reason": "whole_chapter_generation_only"}
        step8 = {"status": "skipped", "reason": "whole_chapter_generation_only"}

    wall_clock_seconds = time.monotonic() - wall_t0
    processed_count = len(_load_journal(journal_path))
    finished_at = now_fn().isoformat(timespec="seconds")

    runtime_summary = dict(runtime.summary())
    local_lifecycle = runtime_summary.get("local_lifecycle")
    remote_calls = runtime_summary.get("remote_calls")
    backend_block = dict(runtime.backend_descriptor.public_record())
    backend_block["config_identity_hash"] = cfg.backend.identity_hash
    artefacts: Dict[str, Any] = {
        "chunk_plan": str(cfg.out_dir / "chunk_plan.json"),
        "whole_chapter_pid_map": str(pid_map_path),
        "generation_outcomes": str(generation_path),
        "selection_results": str(selection_path),
        "translations_raw": str(raw_translations_path),
        "translations_repaired": str(cfg.out_dir / "translations_repaired.json"),
        "translation_diffs": str(cfg.out_dir / "translation_diffs.json"),
        "translations": str(translations_path),
        "glossary_budget_report": str(cfg.out_dir / "glossary_budget_report.json"),
        "journal": str(journal_path),
    }
    # F8 (B3 review): the manifest advertises ONLY artifacts that were
    # actually created. When the B3 stage is skipped or failed, the B3
    # journal/cache/entity files do not exist — advertising their paths
    # would claim provenance/state the run never produced.
    for key, name in (
        ("b3_audit_journal", "audit_journal.ndjson"),
        ("b3_audit_cache", "audit_cache_b3.json"),
        ("b3_entity_context_cache", "entity_context_cache.json"),
        ("b3_entity_validation_report", "entity_context_validation_report.json"),
        # V4.2 R: the Russian-editor artifacts are advertised only when the
        # stage actually produced them (a full cache hit / disabled stage
        # leaves them absent — F8: never advertise nonexistent provenance).
        ("translations_edited", "translations_edited.json"),
        ("edit_candidates", "edit_candidates.json"),
        # V4.1 GEN-REASONING: the whole-chapter reasoning text artifact is
        # advertised only when reasoning>0 produced it (a reasoning=0 run or
        # a transport that reported no reasoning leaves it absent — F8).
        ("whole_chapter_reasoning", "whole_chapter_reasoning.txt"),
    ):
        candidate = cfg.out_dir / name
        if candidate.exists():
            artefacts[key] = str(candidate)
    record: Dict[str, Any] = {
        "schema": RECORD_SCHEMA,
        "run_label": cfg.run_label,
        "chapter_id": cfg.chapter_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "wall_clock_seconds": wall_clock_seconds,
        "identities": {
            "source_hash": source.source_hash,
            "snapshot_hash": snapshot.snapshot_hash,
            "chunk_plan_hash": chunk_plan.plan_hash,
            "config_identity": config.config_identity,
            "whole_chapter_pid_map_hash": pid_map.map_hash,
            # A2 review fix: the chapter_index (bible prompt) record that is
            # part of the snapshot identity — recorded so a verifiable hash
            # exists for audits/resume diagnosis.
            "chapter_index_hash": snapshot.chapter_index_hash,
        },
        "backend": backend_block,
        "runtime": {
            "local_lifecycle": local_lifecycle,
            "remote_calls": remote_calls,
        },
        "operational_policy": {
            "max_consecutive_terminal_nonselections": cfg.max_consecutive_terminal_nonselections,
            "reasoning": cfg.reasoning,
            "stop_after": cfg.stop_after,
            "whole_chapter": True,
            "generation_max_tokens": cfg.max_tokens,
            "whole_chapter_retry": {
                "max_attempts": WholeChapterRetryPolicy().max_attempts,
            },
            # V4.1 B3: the production audit/repair stage policy (the audit
            # input budget is part of the config identity too). F5: the
            # repair-policy knobs and prompt/extractor versions are recorded
            # alongside the identity so the record reflects what the stage
            # actually ran with.
            "audit": {
                "run": cfg.run_audit,
                "entity_context_enabled": cfg.entity_context_enabled,
                "max_input_tokens": cfg.audit_max_input_tokens,
                "max_tokens": cfg.audit_max_tokens,
                "overlap_tokens": cfg.audit_overlap_tokens,
                "reasoning_budget": cfg.audit_reasoning_budget,
                "audit_transport_retry": {
                    "max_retries": cfg.audit_transport_max_retries,
                    "base_delay_seconds": cfg.audit_transport_base_delay_seconds,
                },
                "repair_findings_cap": cfg.audit_repair_findings_cap,
                "repair_microbatch_trigger": cfg.audit_repair_microbatch_trigger,
                "repair_microbatch_target": cfg.audit_repair_microbatch_target,
                "repair_context_window": cfg.audit_repair_context_window,
                "repair_reaudit_neighbour_window": cfg.audit_repair_reaudit_neighbour_window,
                # REPAIR-CTX (t_97b31f81): the re-audit chunk/overlap
                # settings and the REPAIRED CHANGES delta format are
                # recorded alongside the identity so the record reflects
                # what the re-audit actually ran with.
                "repair_reaudit_chunk": {
                    "max_input_tokens": cfg.audit_repair_reaudit_max_input_tokens,
                    "overlap_tokens": cfg.audit_repair_reaudit_overlap_tokens,
                    "min_overlap_pairs": cfg.audit_repair_reaudit_min_overlap_pairs,
                    "max_overlap_pairs": cfg.audit_repair_reaudit_max_overlap_pairs,
                    "delta_format": cfg.audit_repair_reaudit_delta_format,
                },
                "repair_reaudit_max_tokens": cfg.audit_repair_reaudit_max_tokens,
                "repair_max_tokens": cfg.audit_repair_max_tokens,
                # REPAIR-ROBUST (t_b6fd6cbd): the repair reasoning effort is
                # recorded alongside the identity so the report reflects
                # what the repair stage actually ran with.
                "repair_reasoning": cfg.audit_repair_reasoning,
                "repair_reaudit_retry": {
                    "max_retries": cfg.audit_repair_reaudit_max_retries,
                    "base_delay_seconds": cfg.audit_repair_reaudit_base_delay_seconds,
                },
                "prompt_version": cfg.audit_prompt_version,
                "harness_version": cfg.audit_harness_version,
                "extractor_version": cfg.audit_extractor_version,
                # CANDIDATE-MERGE (t_0ffe56e1, RV2 HIGH finding): the REPAIR
                # prompt version rides the record alongside the identity so
                # the report reflects what the repair stage actually ran with.
                "repair_prompt_version": cfg.audit_repair_prompt_version,
                "repair_harness_version": cfg.audit_repair_harness_version,
            },
            # V4.2 R: the Russian-editor stage policy is recorded alongside
            # the identity (the config artifact carries the same keys).
            "russian_editor": {
                "enabled": cfg.russian_editor_enabled,
                "version": cfg.russian_editor_version,
                "harness_version": cfg.russian_editor_harness_version,
                "chunk_size": cfg.russian_editor_chunk_size,
                "overlap_pairs": cfg.russian_editor_overlap_pairs,
                "max_tokens": cfg.russian_editor_max_tokens,
                "safe_classes": list(cfg.russian_editor_safe_classes),
                "max_edits_per_pid": cfg.russian_editor_max_edits_per_pid,
                "r_editor_retry": {
                    "max_retries": cfg.russian_editor_retry_max_retries,
                    "base_delay_seconds": cfg.russian_editor_retry_base_delay_seconds,
                },
            },
        },
        "resumed_from_index": resumed_from_index,
        "halted_early": halted_early,
        "halt_reason": halt_reason,
        "counts": {
            "chunks_total": 1,
            "chunks_processed": processed_count,
            "selected": sum(selected_role_counts.values()),
            "quarantined": 0,
            "needs_synthesis": 0,
            "incomplete_generation": incomplete_generation_count,
            "selected_role_counts": dict(selected_role_counts),
        },
        "step6": step6,
        "step7": step7,
        "step8": step8,
        # V4.2 R: the Russian-editor stage report (edit_candidates +
        # accept/reject journal) recorded in the trial record; absent when
        # the R stage is disabled (4.1 scheme).
        "russian_editor": (
            b3_audit_result.r_editor if b3_audit_result is not None else None
        ),
        "lifecycle": local_lifecycle or {
            "startup_count": 0, "restart_count": 0,
            "switches": [], "aggregates_by_model": {},
        },
        "artefacts": artefacts,
    }
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    # Terminal teardown lives in the wrapper's finally (_run_whole_chapter_strict),
    # which runs on success AND on fail-closed resume-validation errors; nothing
    # to close here (close() is idempotent, but the wrapper owns it now).

    return StrictChapterRunResult(
        chapter_id=cfg.chapter_id, out_dir=cfg.out_dir, chunk_count=1,
        processed_count=processed_count,
        selected_count=sum(selected_role_counts.values()),
        quarantined_count=0, needs_synthesis_count=0,
        incomplete_generation_count=incomplete_generation_count,
        selected_role_counts=dict(selected_role_counts),
        halted_early=halted_early, halt_reason=halt_reason,
        resumed_from_index=resumed_from_index,
        switches=((local_lifecycle or {}).get("switches") or []),
        translations_path=translations_path, journal_path=journal_path,
        record_path=record_path, record=record,
        step6=step6, step7=step7, step8=step8,
    )
